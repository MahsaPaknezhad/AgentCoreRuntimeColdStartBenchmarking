"""Measure cold start using new session IDs on existing runtimes.

Each round:
  1. Invoke with a fresh session ID (cold) — forces a new microVM/session
  2. Invoke with the same session ID (warm) — reuses the existing session
  3. Compute cold_start = (cold_invoke - cold_agent) - (warm_invoke - warm_agent)
  4. Wait for idle timeout so the session is destroyed before the next round

Requires runtimes to already be deployed (via deploy.py).

Usage:
    python experiment2.py --rounds 5
    python experiment2.py --rounds 5 --mode zip
    python experiment2.py --rounds 5 --wait 150
"""

import argparse
import json
import os
import time
import uuid

import config as cfg
import deploy
from invoke import invoke

RESULTS_FILE = "results2.json"


def run_round(arn, round_num):
    session_id = "bench_" + uuid.uuid4().hex
    result = {"round": round_num, "session_id": session_id, "ok": False}

    # Cold invoke — new session ID forces new microVM
    print(f"    cold invoke (session={session_id[:16]}…) …", end=" ", flush=True)
    try:
        cold_ms, cold_agent_ms, uptime_s = invoke(arn, session_id)
        print(f"{cold_ms:.0f}ms (agent={cold_agent_ms}ms, uptime={uptime_s}s)")
        result.update(cold_invoke_ms=round(cold_ms, 1), cold_agent_ms=cold_agent_ms, uptime_s=uptime_s)
    except Exception as e:
        print(f"ERROR: {e}")
        result["error"] = str(e)
        return result

    # Warm invoke — same session ID reuses existing session
    print(f"    warm invoke …", end=" ", flush=True)
    try:
        warm_ms, warm_agent_ms, _ = invoke(arn, session_id)
        print(f"{warm_ms:.0f}ms (agent={warm_agent_ms}ms)")
        result.update(warm_invoke_ms=round(warm_ms, 1), warm_agent_ms=warm_agent_ms)

        if cold_agent_ms is None or warm_agent_ms is None:
            print(f"    ⚠ agent_ms missing, cannot compute cold start overhead")
            result["error"] = "agent_ms not returned by runtime"
        else:
            cold_start = (cold_ms - cold_agent_ms) - (warm_ms - warm_agent_ms)
            result["cold_start_ms"] = round(cold_start, 1)
            result["ok"] = True
            print(f"    → cold start overhead: {cold_start:.0f}ms")
    except Exception as e:
        print(f"ERROR: {e}")
        result["error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Cold start benchmark using session rotation")
    parser.add_argument("--rounds", type=int, default=cfg.DEFAULT_ROUNDS)
    parser.add_argument("--mode", choices=["zip", "docker"])
    parser.add_argument("--wait", type=int, default=cfg.IDLE_TIMEOUT_SECONDS + 30,
                        help=f"Seconds to wait between rounds for session teardown (default: {cfg.IDLE_TIMEOUT_SECONDS + 30})")
    args = parser.parse_args()

    arns = deploy.load_arns()
    if not arns:
        print("No runtimes found. Run 'python deploy.py' first.")
        return

    modes = [args.mode] if args.mode else [m for m in ["zip", "docker"] if m in arns]
    if not modes:
        print(f"No runtime found for requested mode. Available: {list(arns.keys())}")
        return

    all_results = {}
    for mode in modes:
        arn = arns[mode]
        print(f"\n{'='*60}")
        print(f"  {mode.upper()} — {args.rounds} rounds (wait={args.wait}s)")
        print(f"  ARN: {arn}")
        print(f"{'='*60}")
        results = []
        for i in range(1, args.rounds + 1):
            print(f"\n  round {i}/{args.rounds}:", flush=True)
            results.append(run_round(arn, i))
            if i < args.rounds:
                print(f"    waiting {args.wait}s for session teardown …", flush=True)
                time.sleep(args.wait)
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
