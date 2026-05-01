"""Run cold start experiments against deployed AgentCore runtimes.

Usage:
    python experiment.py                # 5 rounds, both modes
    python experiment.py --rounds 10
    python experiment.py --mode zip     # only ZIP
"""

import argparse
import json
import os
import time
import uuid

import boto3

import config as cfg

_ARN_FILE = "runtime_arns.json"


def _load_arns():
    with open(_ARN_FILE) as f:
        return json.load(f)


def _data_client():
    return boto3.client("bedrock-agentcore", region_name=cfg.REGION)


def _invoke_cold(client, arn):
    """Invoke with a fresh session ID to guarantee a cold start.

    Returns (latency_ms, response_body).
    """
    session_id = "bench-" + uuid.uuid4().hex  # unique → new VM
    payload = json.dumps({"ping": True}).encode()

    t0 = time.monotonic()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=payload,
        qualifier="DEFAULT",
    )
    # Read the full response to capture end-to-end time
    body = resp["response"].read()
    latency_ms = (time.monotonic() - t0) * 1000
    return latency_ms, body.decode("utf-8", errors="replace")


def run_experiment(mode, arn, rounds, wait_seconds):
    """Run `rounds` cold start invocations and return list of latency_ms."""
    client = _data_client()
    results = []
    for i in range(1, rounds + 1):
        if i > 1:
            print(f"  waiting {wait_seconds}s for idle timeout …")
            time.sleep(wait_seconds)
        print(f"  [{mode}] round {i}/{rounds} … ", end="", flush=True)
        try:
            latency, body = _invoke_cold(client, arn)
            print(f"{latency:.0f} ms")
            results.append({"round": i, "latency_ms": round(latency, 1), "ok": True})
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({"round": i, "latency_ms": None, "ok": False, "error": str(e)})
    return results


def main():
    parser = argparse.ArgumentParser(description="Cold start experiment")
    parser.add_argument("--rounds", type=int, default=cfg.DEFAULT_ROUNDS)
    parser.add_argument("--mode", choices=["zip", "docker"])
    parser.add_argument("--wait", type=int, default=cfg.IDLE_TIMEOUT_SECONDS + 30,
                        help="Seconds to wait between rounds (default: idle timeout + 30)")
    args = parser.parse_args()

    arns = _load_arns()
    modes = [args.mode] if args.mode else [m for m in ("zip", "docker") if m in arns]

    all_results = {}
    for mode in modes:
        arn = arns[mode]
        print(f"\n{'='*50}")
        print(f"Running {args.rounds} cold start rounds for [{mode}]")
        print(f"  ARN: {arn}")
        print(f"  Wait between rounds: {args.wait}s")
        print(f"{'='*50}")
        all_results[mode] = run_experiment(mode, arn, args.rounds, args.wait)

    # Merge with any existing results
    existing = {}
    if os.path.exists(cfg.RESULTS_FILE):
        with open(cfg.RESULTS_FILE) as f:
            existing = json.load(f)
    existing.update(all_results)

    with open(cfg.RESULTS_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\nResults saved to {cfg.RESULTS_FILE}")


if __name__ == "__main__":
    main()
