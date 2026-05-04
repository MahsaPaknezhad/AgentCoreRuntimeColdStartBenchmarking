"""Shared invoke logic for cold start experiments."""

import json
import time
import uuid

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

import config as cfg


def arn_to_invoke_url(arn):
    """Convert runtime ARN to the HTTPS invoke URL."""
    region = arn.split(":")[3]
    encoded = arn.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"


def invoke(arn, session_id=None):
    """Invoke runtime via HTTPS POST with SigV4 signing. Returns (latency_ms, agent_ms, uptime_s)."""
    session_id = session_id or ("bench" + uuid.uuid4().hex)
    payload = json.dumps({"input": {"prompt": "say hello"}}).encode()
    url = arn_to_invoke_url(arn)

    session = boto3.Session(region_name=cfg.REGION)
    credentials = session.get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(
        method="POST",
        url=url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    SigV4Auth(credentials, "bedrock-agentcore", cfg.REGION).add_auth(aws_req)
    headers = dict(aws_req.headers.items())

    t0 = time.monotonic()
    resp = requests.post(url, data=payload, headers=headers, timeout=120)
    body = resp.content
    latency_ms = (time.monotonic() - t0) * 1000

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {body[:500]!r}")

    agent_ms = None
    uptime_s = None
    try:
        parsed = json.loads(body)
        agent_ms = parsed.get("agent_ms")
        uptime_s = parsed.get("uptime_s")
    except Exception:
        pass

    return latency_ms, agent_ms, uptime_s


def stop_session(arn, session_id):
    """Stop a runtime session to force VM teardown."""
    client = boto3.client("bedrock-agentcore", region_name=cfg.REGION)
    try:
        client.stop_runtime_session(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
        )
    except Exception as e:
        # Session may already be gone
        if "ResourceNotFoundException" not in str(type(e).__name__):
            print(f"    ⚠ stop_session error: {e}")
