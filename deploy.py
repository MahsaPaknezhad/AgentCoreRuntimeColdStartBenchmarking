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


def _ecr_client():
    return boto3.client("ecr", region_name=cfg.REGION)


def _sts_account_id():
    return boto3.client("sts", region_name=cfg.REGION).get_caller_identity()["Account"]


# ── helpers ──────────────────────────────────────────────────────────

def _wait_for_ready(client, arn, timeout=300):
    """Poll until runtime status is READY."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get_agent_runtime(agentRuntimeArn=arn)
        status = resp.get("status") or resp.get("agentRuntimeStatus")
        print(f"  status: {status}")
        if status in ("READY", "ACTIVE"):
            return resp
        if status in ("FAILED", "DELETE_FAILED"):
            sys.exit(f"Runtime {arn} entered {status}")
        time.sleep(10)
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

def _build_zip_bytes():
    """Create an in-memory ZIP of agent/app.py."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write("agent/app.py", "app.py")
    return buf.getvalue()


def deploy_zip():
    client = _control_client()
    existing = _runtime_exists(client, cfg.ZIP_RUNTIME_NAME)
    if existing:
        print(f"ZIP runtime already exists: {existing}")
        return existing

    print("Deploying ZIP runtime …")
    zip_bytes = _build_zip_bytes()
    resp = client.create_agent_runtime(
        agentRuntimeName=cfg.ZIP_RUNTIME_NAME,
        agentRuntimeArtifact={"s3Configuration": {"bucketArn": "inline", "objectKey": "inline"}},
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
    acct = cfg.ACCOUNT_ID if cfg.ACCOUNT_ID != "CHANGE_ME" else _sts_account_id()
    return f"{acct}.dkr.ecr.{cfg.REGION}.amazonaws.com/{cfg.ECR_REPO_NAME}:latest"


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
        return existing

    image_uri = _ensure_ecr_repo()
    _docker_build_and_push(image_uri)

    print("Deploying Docker runtime …")
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
            client.delete_agent_runtime(agentRuntimeArn=arn)
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
