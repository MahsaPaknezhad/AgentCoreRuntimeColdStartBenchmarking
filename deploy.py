"""Deploy both ZIP and Docker agent runtimes to AgentCore.

Usage:
    python deploy.py          # deploy both
    python deploy.py --mode zip
    python deploy.py --mode docker
    python deploy.py --teardown
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


def _control_client():
    return boto3.client("bedrock-agentcore-control", region_name=cfg.REGION)


def _s3_client():
    return boto3.client("s3", region_name=cfg.REGION)


def _ecr_client():
    return boto3.client("ecr", region_name=cfg.REGION)


# ── helpers ──────────────────────────────────────────────────────────

def _arn_to_id(arn):
    """Extract runtime ID from ARN like arn:aws:bedrock-agentcore:...:runtime/name-XXXX"""
    return arn.rsplit("/", 1)[-1]


def _wait_for_ready(client, arn, timeout=600):
    """Poll until runtime status is READY/ACTIVE."""
    runtime_id = _arn_to_id(arn)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status") or resp.get("agentRuntimeStatus")
        print(f"  status: {status}")
        if status in ("READY", "ACTIVE"):
            return resp
        if status in ("FAILED", "DELETE_FAILED"):
            sys.exit(f"Runtime {arn} entered {status}")
        time.sleep(15)
    sys.exit(f"Timed out waiting for {arn}")


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


# ── ZIP deployment ───────────────────────────────────────────────────

_S3_BUCKET = f"bedrock-agentcore-code-{cfg.ACCOUNT_ID}-{cfg.REGION}"


def _ensure_s3_bucket():
    s3 = _s3_client()
    try:
        s3.head_bucket(Bucket=_S3_BUCKET)
    except s3.exceptions.ClientError:
        print(f"Creating S3 bucket: {_S3_BUCKET}")
        s3.create_bucket(
            Bucket=_S3_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": cfg.REGION},
        )


def _build_and_upload_zip():
    """Create a ZIP of agent/app.py as main.py and upload to S3."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("agent/app.py", "main.py")
    zip_bytes = buf.getvalue()

    s3 = _s3_client()
    key = f"{cfg.ZIP_RUNTIME_NAME}/deployment_package.zip"
    print(f"Uploading ZIP ({len(zip_bytes)} bytes) → s3://{_S3_BUCKET}/{key}")
    s3.put_object(
        Bucket=_S3_BUCKET,
        Key=key,
        Body=zip_bytes,
        ExpectedBucketOwner=cfg.ACCOUNT_ID,
    )
    return key


def deploy_zip():
    client = _control_client()
    existing = _runtime_exists(client, cfg.ZIP_RUNTIME_NAME)
    if existing:
        print(f"ZIP runtime already exists: {existing}")
        _save_arn("zip", existing)
        return existing

    _ensure_s3_bucket()
    s3_key = _build_and_upload_zip()

    print("Creating ZIP runtime …")
    resp = client.create_agent_runtime(
        agentRuntimeName=cfg.ZIP_RUNTIME_NAME,
        agentRuntimeArtifact={
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": _S3_BUCKET,
                        "prefix": s3_key,
                    }
                },
                "runtime": "PYTHON_3_13",
                "entryPoint": ["main.py"],
            }
        },
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=cfg.ROLE_ARN,
        lifecycleConfiguration=_lifecycle(),
    )
    arn = resp["agentRuntimeArn"]
    print(f"  ARN: {arn}")
    _wait_for_ready(client, arn)
    _save_arn("zip", arn)
    return arn


# ── Docker deployment ────────────────────────────────────────────────

def _ensure_ecr_repo():
    ecr = _ecr_client()
    try:
        ecr.create_repository(repositoryName=cfg.ECR_REPO_NAME)
        print(f"Created ECR repo: {cfg.ECR_REPO_NAME}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    return f"{cfg.ACCOUNT_ID}.dkr.ecr.{cfg.REGION}.amazonaws.com/{cfg.ECR_REPO_NAME}:latest"


def _docker_build_and_push(image_uri):
    print(f"Building & pushing ARM64 image → {image_uri}")
    registry = image_uri.split("/")[0]
    pwd = subprocess.check_output(
        ["aws", "ecr", "get-login-password", "--region", cfg.REGION], text=True
    ).strip()
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=pwd, text=True, check=True,
    )
    subprocess.run(
        ["docker", "buildx", "build", "--platform", "linux/arm64",
         "-t", image_uri, "--push", "."],
        check=True,
    )


def deploy_docker():
    client = _control_client()
    existing = _runtime_exists(client, cfg.DOCKER_RUNTIME_NAME)
    if existing:
        print(f"Docker runtime already exists: {existing}")
        _save_arn("docker", existing)
        return existing

    image_uri = _ensure_ecr_repo()
    _docker_build_and_push(image_uri)

    print("Creating Docker runtime …")
    resp = client.create_agent_runtime(
        agentRuntimeName=cfg.DOCKER_RUNTIME_NAME,
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": image_uri},
        },
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=cfg.ROLE_ARN,
        lifecycleConfiguration=_lifecycle(),
    )
    arn = resp["agentRuntimeArn"]
    print(f"  ARN: {arn}")
    _wait_for_ready(client, arn)
    _save_arn("docker", arn)
    return arn


# ── ARN persistence ──────────────────────────────────────────────────

_ARN_FILE = "runtime_arns.json"


def _save_arn(mode, arn):
    data = _load_arns()
    data[mode] = arn
    with open(_ARN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_arns():
    if os.path.exists(_ARN_FILE):
        with open(_ARN_FILE) as f:
            return json.load(f)
    return {}


# ── teardown ─────────────────────────────────────────────────────────

def teardown():
    client = _control_client()
    for mode, arn in _load_arns().items():
        print(f"Deleting {mode} runtime: {arn}")
        try:
            client.delete_agent_runtime(agentRuntimeId=_arn_to_id(arn))
        except Exception as e:
            print(f"  warning: {e}")
    if os.path.exists(_ARN_FILE):
        os.remove(_ARN_FILE)
    print("Done.")


# ── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deploy AgentCore runtimes")
    parser.add_argument("--mode", choices=["zip", "docker"], help="Deploy only one mode")
    parser.add_argument("--teardown", action="store_true", help="Delete runtimes")
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
