"""Run true cold start experiments by deleting and recreating runtimes each round.

Each round:
  1. Create a fresh runtime
  2. Wait for READY
  3. Cold invoke — first request to brand new runtime
  4. Warm invoke — second request to same runtime
  5. Compute cold_start = (cold_invoke - cold_agent) - (warm_invoke - warm_agent)
  6. Delete the runtime

Usage:
    python experiment.py --rounds 3
    python experiment.py --rounds 3 --mode zip
"""

import argparse
import json
import os
import time
import uuid

import boto3

import config as cfg
import deploy
from invoke import invoke

RESULTS_FILE = cfg.RESULTS_FILE


def run_round(ctrl_client, mode, round_num, image_uri=None, s3_key=None):
    name = f"csbench_{mode}_r{round_num}_{uuid.uuid4().hex[:6]}"
    print(f"  [{mode}] round {round_num}: creating {name} …", flush=True)

    t_create = time.monotonic()
    if mode == "zip":
        arn = deploy.create_zip_runtime(ctrl_client, name=name, s3_key=s3_key)
    else:
        arn = deploy.create_docker_runtime(ctrl_client, image_uri, name=name)

    deploy.wait_for_ready(ctrl_client, arn)
    create_ms = (time.monotonic() - t_create) * 1000
    print(f"    READY in {create_ms:.0f}ms", flush=True)

    session_id = f"bench_{uuid.uuid4().hex}"
    result = {"round": round_num, "runtime_name": name, "session_id": session_id, "create_ms": round(create_ms, 1), "ok": False}

    # Cold invoke
    print(f"    cold invoke …", end=" ", flush=True)
    try:
        cold_ms, cold_agent_ms, uptime_s = invoke(arn, session_id=session_id)
        print(f"{cold_ms:.0f}ms (agent={cold_agent_ms}ms, uptime={uptime_s}s)")
        result.update(cold_invoke_ms=round(cold_ms, 1), cold_agent_ms=cold_agent_ms, uptime_s=uptime_s)
    except Exception as e:
        print(f"ERROR: {e}")
        result["error"] = str(e)
        deploy.delete_runtime(ctrl_client, arn)
        return result

    # Warm invoke (same session)
    print(f"    warm invoke …", end=" ", flush=True)
    try:
        warm_ms, warm_agent_ms, _ = invoke(arn, session_id=session_id)
        print(f"{warm_ms:.0f}ms (agent={warm_agent_ms}ms)")
        result.update(warm_invoke_ms=round(warm_ms, 1), warm_agent_ms=warm_agent_ms)

        if cold_agent_ms is None or warm_agent_ms is None:
            print(f"    ⚠ agent_ms missing (cold={cold_agent_ms}, warm={warm_agent_ms}), cannot compute cold start overhead")
            result["ok"] = False
            result["error"] = "agent_ms not returned by runtime"
        else:
            cold_overhead = cold_ms - cold_agent_ms
            warm_overhead = warm_ms - warm_agent_ms
            cold_start = cold_overhead - warm_overhead
            result["cold_start_ms"] = round(cold_start, 1)
            result["ok"] = True
            print(f"    → cold start overhead: {cold_start:.0f}ms")
    except Exception as e:
        print(f"ERROR: {e}")
        result["error"] = str(e)

    print(f"    deleting …", end=" ", flush=True)
    deploy.delete_runtime(ctrl_client, arn)
    print("done")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=cfg.DEFAULT_ROUNDS)
    parser.add_argument("--mode", choices=["zip", "docker"])
    args = parser.parse_args()

    ctrl = deploy.control_client()
    modes = [args.mode] if args.mode else ["zip", "docker"]

    image_uri = None
    s3_key = None
    if "zip" in modes:
        print("Preparing ZIP artifacts …")
        s3_key = deploy.ensure_zip_artifacts()
    if "docker" in modes:
        print("Preparing Docker artifacts …")
        image_uri = deploy.ensure_docker_artifacts()

    all_results = {}
    for mode in modes:
        print(f"\n{'='*60}")
        print(f"  {mode.upper()} — {args.rounds} rounds")
        print(f"{'='*60}")
        results = []
        for i in range(1, args.rounds + 1):
            results.append(run_round(ctrl, mode, i, image_uri=image_uri, s3_key=s3_key))
        all_results[mode] = results

    existing = {}
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            existing = json.load(f)
    existing.update(all_results)
    with open(RESULTS_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\nResults saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
