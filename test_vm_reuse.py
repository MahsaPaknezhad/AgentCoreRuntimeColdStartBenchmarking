"""Self-contained test: proves whether AgentCore reuses VMs across session IDs.

This script does everything end-to-end:
  1. Builds a Docker image with a vm_id marker baked in
  2. Pushes it to ECR
  3. Creates a fresh AgentCore runtime
  4. Waits for READY
  5. Invokes with N different session IDs
  6. Compares vm_id / pid across invocations
  7. Tears down the runtime
  8. Prints verdict

Usage:
    python test_vm_reuse.py
    python test_vm_reuse.py --sessions 10
    python test_vm_reuse.py --keep   # don't delete runtime after test
"""

import argparse
import io
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# ── Config ───────────────────────────────────────────────────────────

REGION = "ap-southeast-2"
ECR_REPO = "vm-reuse-test-agent"
RUNTIME_NAME = f"vm_reuse_test_{uuid.uuid4().hex[:6]}"
RESULTS_FILE = "vm_reuse_test_results.json"


def get_account_id():
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


# ── Step 1: Build agent code + Docker image ──────────────────────────

AGENT_CODE = textwrap.dedent('''\
    """Minimal agent that returns VM identity markers."""
    import os, time, uuid
    from fastapi import FastAPI, Request

    _START_TIME = time.time()
    _VM_ID = uuid.uuid4().hex
    _PID = os.getpid()

    app = FastAPI()

    @app.post("/invocations")
    async def invocations(request: Request):
        body = await request.json()
        return {
            "message": "hello",
            "uptime_s": round(time.time() - _START_TIME, 3),
            "vm_id": _VM_ID,
            "pid": _PID,
        }

    @app.get("/ping")
    async def ping():
        return {"status": "Healthy"}
''')

DOCKERFILE = textwrap.dedent('''\
    FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.11-bookworm-slim
    WORKDIR /app
    RUN uv pip install --system --no-cache fastapi uvicorn
    COPY main.py .
    EXPOSE 8080
    CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
''')


def build_and_push(account_id):
    """Build Docker image and push to ECR. Returns image URI."""
    image_uri = f"{account_id}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:latest"
    registry = image_uri.split("/")[0]

    # Ensure ECR repo
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        ecr.create_repository(repositoryName=ECR_REPO)
        print(f"  Created ECR repo: {ECR_REPO}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass

    # Write temp files
    tmp_dir = "/tmp/vm_reuse_test_build"
    os.makedirs(tmp_dir, exist_ok=True)
    with open(f"{tmp_dir}/main.py", "w") as f:
        f.write(AGENT_CODE)
    with open(f"{tmp_dir}/Dockerfile", "w") as f:
        f.write(DOCKERFILE)

    # ECR login
    pwd = subprocess.check_output(
        ["aws", "ecr", "get-login-password", "--region", REGION], text=True
    ).strip()
    subprocess.run(["docker", "login", "--username", "AWS", "--password", pwd, registry],
                   check=True, capture_output=True)

    # Build + push
    print("  Building and pushing Docker image …", flush=True)
    subprocess.run(
        ["docker", "buildx", "build", "--platform", "linux/arm64",
         "-t", image_uri, "--push", tmp_dir],
        check=True,
    )
    return image_uri


# ── Step 2: Create runtime ───────────────────────────────────────────

def create_runtime(ctrl, image_uri, role_arn):
    """Create AgentCore runtime. Returns ARN."""
    resp = ctrl.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role_arn,
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 60,
            "maxLifetime": 60,
        },
    )
    return resp["agentRuntimeArn"]


def wait_ready(ctrl, arn, timeout=600):
    runtime_id = arn.rsplit("/", 1)[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status") or resp.get("agentRuntimeStatus")
        if status in ("READY", "ACTIVE"):
            return
        if status in ("FAILED", "DELETE_FAILED"):
            raise RuntimeError(f"Runtime entered {status}")
        time.sleep(5)
    raise TimeoutError("Timed out waiting for READY")


def delete_runtime(ctrl, arn):
    runtime_id = arn.rsplit("/", 1)[-1]
    try:
        ctrl.delete_agent_runtime(agentRuntimeId=runtime_id)
    except Exception:
        pass
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
            time.sleep(5)
        except Exception:
            return
    print("  ⚠ Timed out waiting for deletion")


# ── Step 3: Invoke ───────────────────────────────────────────────────

def invoke_runtime(arn, session_id):
    """Invoke and return parsed response dict."""
    region = arn.split(":")[3]
    encoded = arn.replace(":", "%3A").replace("/", "%2F")
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"

    payload = json.dumps({"input": {"prompt": "hello"}}).encode()
    session = boto3.Session(region_name=REGION)
    creds = session.get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(
        method="POST", url=url, data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(aws_req)

    t0 = time.monotonic()
    resp = requests.post(url, data=payload, headers=dict(aws_req.headers.items()), timeout=120)
    latency_ms = (time.monotonic() - t0) * 1000

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.content[:300]}")

    parsed = resp.json()
    parsed["latency_ms"] = round(latency_ms, 1)
    parsed["session_id"] = session_id
    return parsed


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test VM reuse across AgentCore sessions")
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--keep", action="store_true", help="Don't delete runtime after test")
    args = parser.parse_args()

    account_id = get_account_id()
    role_arn = f"arn:aws:iam::{account_id}:role/agentcore-test-agent-role"
    ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Build
    print("\n[1/5] Building Docker image …")
    image_uri = build_and_push(account_id)

    # Create runtime
    print(f"\n[2/5] Creating runtime '{RUNTIME_NAME}' …")
    arn = create_runtime(ctrl, image_uri, role_arn)
    print(f"  ARN: {arn}")

    try:
        print("\n[3/5] Waiting for READY …", flush=True)
        wait_ready(ctrl, arn)
        print("  ✓ Runtime is READY")

        # Invoke with different session IDs
        print(f"\n[4/5] Invoking with {args.sessions} unique session IDs …\n")
        results = []
        for i in range(1, args.sessions + 1):
            sid = f"vmtest_{uuid.uuid4().hex}"
            print(f"  [{i}/{args.sessions}] session={sid[:24]}… ", end="", flush=True)
            try:
                r = invoke_runtime(arn, sid)
                print(f"vm_id={r.get('vm_id')}  pid={r.get('pid')}  "
                      f"uptime={r.get('uptime_s')}s  latency={r['latency_ms']:.0f}ms")
                results.append(r)
            except Exception as e:
                print(f"ERROR: {e}")
                results.append({"session_id": sid, "error": str(e)})

        # Analyze
        print(f"\n[5/5] Analysis\n{'='*60}")
        successful = [r for r in results if "vm_id" in r]

        if len(successful) < 2:
            print("  Not enough successful invocations.")
            return

        unique_vms = set(r["vm_id"] for r in successful)
        unique_pids = set(r["pid"] for r in successful)

        print(f"  Sessions invoked:  {len(successful)}")
        print(f"  Unique vm_ids:     {len(unique_vms)}  {unique_vms}")
        print(f"  Unique pids:       {len(unique_pids)}  {unique_pids}")

        if len(unique_vms) == 1:
            print(f"\n  ✗ PROVED: VM IS REUSED")
            print(f"    All {len(successful)} sessions hit the same process.")
            print(f"    vm_id={list(unique_vms)[0]}, pid={list(unique_pids)[0]}")
            print(f"    Per-session microVM isolation is NOT happening for Docker runtimes.")
            verdict = "VM_REUSED"
        elif len(unique_vms) == len(successful):
            print(f"\n  ✓ DISPROVED: Each session got its own VM.")
            print(f"    {len(unique_vms)} unique VMs for {len(successful)} sessions.")
            verdict = "VM_ISOLATED"
        else:
            print(f"\n  ~ PARTIAL REUSE: {len(unique_vms)} VMs for {len(successful)} sessions.")
            verdict = "PARTIAL_REUSE"

        # Save
        output = {
            "verdict": verdict,
            "runtime_name": RUNTIME_NAME,
            "runtime_arn": arn,
            "sessions_tested": len(successful),
            "unique_vms": len(unique_vms),
            "unique_pids": len(unique_pids),
            "invocations": results,
        }
        with open(RESULTS_FILE, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n  Results saved to {RESULTS_FILE}")

    finally:
        if not args.keep:
            print(f"\n  Cleaning up runtime '{RUNTIME_NAME}' …", end=" ", flush=True)
            delete_runtime(ctrl, arn)
            print("done")
        else:
            print(f"\n  Runtime kept: {arn}")


if __name__ == "__main__":
    main()
