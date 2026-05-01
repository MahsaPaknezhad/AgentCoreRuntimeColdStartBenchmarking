"""Deploy both ZIP and Docker agent runtimes to AgentCore.

Usage:
    python deploy.py          # deploy both
    python deploy.py --mode zip
    python deploy.py --mode docker
    python deploy.py --teardown

Also exposes create_runtime / delete_runtime / wait_for_ready for use by experiment.py.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import zipfile

import boto3

import config as cfg


def control_client():
    return boto3.client("bedrock-agentcore-control", region_name=cfg.REGION)


def _s3_client():
    return boto3.client("s3", region_name=cfg.REGION)


def _ecr_client():
    return boto3.client("ecr", region_name=cfg.REGION)


# ── helpers ──────────────────────────────────────────────────────────

def arn_to_id(arn):
    """Extract runtime ID from ARN."""
    return arn.rsplit("/", 1)[-1]


def wait_for_ready(client, arn, timeout=600):
    """Poll until runtime status is READY."""
    runtime_id = arn_to_id(arn)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status") or resp.get("agentRuntimeStatus")
        if status in ("READY", "ACTIVE"):
            return resp
        if status in ("FAILED", "DELETE_FAILED"):
            raise RuntimeError(f"Runtime {arn} entered {status}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for {arn}")


def wait_for_deleted(client, arn, timeout=600):
    """Poll until runtime is gone."""
    runtime_id = arn_to_id(arn)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as e:
            if "ResourceNotFound" in str(type(e).__name__) or "not found" in str(e).lower() or "ResourceNotFoundException" in str(e):
                return
            raise
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for deletion of {arn}")


def _lifecycle():
    return {
        "idleRuntimeSessionTimeout": cfg.IDLE_TIMEOUT_SECONDS,
        "maxLifetime": cfg.MAX_LIFETIME_SECONDS,
    }


def _runtime_exists(client, name):
    """Return ARN if a runtime with this name exists, else None."""
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []) + page.get("runtimes", []):
            rt_name = rt.get("agentRuntimeName") or rt.get("name", "")
            if rt_name == name:
                return rt.get("agentRuntimeArn") or rt.get("arn")
    return None


# ── ZIP ──────────────────────────────────────────────────────────────

_S3_BUCKET = None


def _get_s3_bucket():
    global _S3_BUCKET
    if _S3_BUCKET is None:
        _S3_BUCKET = f"bedrock-agentcore-code-{cfg.ACCOUNT_ID}-{cfg.REGION}"
    return _S3_BUCKET


def _ensure_s3_bucket():
    s3 = _s3_client()
    bucket = _get_s3_bucket()
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError:
        print(f"Creating S3 bucket: {bucket}")
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": cfg.REGION},
        )


def _build_deployment_zip():
    """Build a ZIP with ARM64 deps + agent code. Returns path to zip file."""
    import shutil
    pkg_dir = "deployment_package"
    zip_file = "deployment_package.zip"

    # Clean previous build
    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)
    if os.path.exists(zip_file):
        os.remove(zip_file)

    # Install ARM64 wheels into deployment_package/
    subprocess.run([
        "uv", "pip", "install",
        "--python-platform", "aarch64-manylinux2014",
        "--python-version", "3.13",
        "--target", pkg_dir,
        "--only-binary=:all:",
        "strands-agents", "fastapi", "uvicorn",
    ], check=True)

    # Create zip from deps
    subprocess.run(["zip", "-r", f"../{zip_file}", "."], cwd=pkg_dir, check=True,
                   capture_output=True)
    # Add agent code as main.py at zip root
    shutil.copy("agent/app.py", "main.py")
    subprocess.run(["zip", zip_file, "main.py"], check=True, capture_output=True)
    os.remove("main.py")

    return zip_file


def _upload_zip():
    s3 = _s3_client()
    bucket = _get_s3_bucket()
    key = f"{cfg.ZIP_RUNTIME_NAME}/deployment_package.zip"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        print(f"ZIP already exists at s3://{bucket}/{key}, skipping build")
        return key
    except s3.exceptions.ClientError:
        pass
    zip_file = _build_deployment_zip()
    print(f"Uploading {zip_file} → s3://{bucket}/{key}")
    s3.upload_file(zip_file, bucket, key,
                   ExtraArgs={"ExpectedBucketOwner": cfg.ACCOUNT_ID})
    return key


def ensure_zip_artifacts():
    """Ensure S3 bucket and ZIP are uploaded. Idempotent."""
    _ensure_s3_bucket()
    return _upload_zip()


def create_zip_runtime(client, name=None, s3_key=None):
    """Create a ZIP runtime and wait for READY. Returns ARN."""
    name = name or cfg.ZIP_RUNTIME_NAME
    if s3_key is None:
        s3_key = ensure_zip_artifacts()
    resp = client.create_agent_runtime(
        agentRuntimeName=name,
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {"s3": {"bucket": _get_s3_bucket(), "prefix": s3_key}},
                "runtime": "PYTHON_3_13",
                "entryPoint": ["main.py"],
            }
        },
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=cfg.ROLE_ARN,
        lifecycleConfiguration=_lifecycle(),
    )
    return resp["agentRuntimeArn"]


# ── Docker ───────────────────────────────────────────────────────────

def _ensure_ecr_repo():
    ecr = _ecr_client()
    try:
        ecr.create_repository(repositoryName=cfg.ECR_REPO_NAME)
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    return f"{cfg.ACCOUNT_ID}.dkr.ecr.{cfg.REGION}.amazonaws.com/{cfg.ECR_REPO_NAME}:latest"


def _docker_build_and_push(image_uri):
    registry = image_uri.split("/")[0]
    pwd = subprocess.check_output(
        ["aws", "ecr", "get-login-password", "--region", cfg.REGION], text=True
    ).strip()
    subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", registry],
                   input=pwd, text=True, check=True, capture_output=True)
    subprocess.run(["docker", "buildx", "build", "--platform", "linux/arm64",
                    "-t", image_uri, "--push", "."], check=True, capture_output=True)


def ensure_docker_artifacts():
    """Ensure ECR repo exists and image is pushed. Idempotent. Returns image URI."""
    image_uri = _ensure_ecr_repo()
    _docker_build_and_push(image_uri)
    return image_uri


def create_docker_runtime(client, image_uri, name=None):
    """Create a Docker runtime. Returns ARN (caller must wait_for_ready)."""
    name = name or cfg.DOCKER_RUNTIME_NAME
    resp = client.create_agent_runtime(
        agentRuntimeName=name,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=cfg.ROLE_ARN,
        lifecycleConfiguration=_lifecycle(),
    )
    return resp["agentRuntimeArn"]


def delete_runtime(client, arn):
    """Delete a runtime and wait for it to be gone."""
    try:
        client.delete_agent_runtime(agentRuntimeId=arn_to_id(arn))
    except Exception:
        pass
    wait_for_deleted(client, arn)


# ── ARN persistence ──────────────────────────────────────────────────

_ARN_FILE = "runtime_arns.json"


def save_arn(mode, arn):
    data = load_arns()
    data[mode] = arn
    with open(_ARN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_arns():
    if os.path.exists(_ARN_FILE):
        with open(_ARN_FILE) as f:
            return json.load(f)
    return {}


# ── CLI entrypoint ───────────────────────────────────────────────────

def deploy_zip():
    client = control_client()
    existing = _runtime_exists(client, cfg.ZIP_RUNTIME_NAME)
    if existing:
        print(f"ZIP runtime already exists: {existing}")
        save_arn("zip", existing)
        return existing
    print("Creating ZIP runtime …")
    arn = create_zip_runtime(client)
    print(f"  ARN: {arn}")
    wait_for_ready(client, arn)
    save_arn("zip", arn)
    return arn


def deploy_docker():
    client = control_client()
    existing = _runtime_exists(client, cfg.DOCKER_RUNTIME_NAME)
    if existing:
        print(f"Docker runtime already exists: {existing}")
        save_arn("docker", existing)
        return existing
    image_uri = ensure_docker_artifacts()
    print("Creating Docker runtime …")
    arn = create_docker_runtime(client, image_uri)
    print(f"  ARN: {arn}")
    wait_for_ready(client, arn)
    save_arn("docker", arn)
    return arn


def teardown():
    client = control_client()
    for mode, arn in load_arns().items():
        print(f"Deleting {mode} runtime: {arn}")
        delete_runtime(client, arn)
    if os.path.exists(_ARN_FILE):
        os.remove(_ARN_FILE)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Deploy AgentCore runtimes")
    parser.add_argument("--mode", choices=["zip", "docker"])
    parser.add_argument("--teardown", action="store_true")
    args = parser.parse_args()
    if args.teardown:
        teardown()
        return
    if args.mode in (None, "zip"):
        deploy_zip()
    if args.mode in (None, "docker"):
        deploy_docker()
    print("\nRuntime ARNs saved to", _ARN_FILE)


if __name__ == "__main__":
    main()
