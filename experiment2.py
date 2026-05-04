"""Measure cold start using new session IDs on existing runtimes.

Each round:
  1. Invoke with a fresh session ID (cold) — forces a new microVM/session
  2. Validate uptime_s to confirm the session was actually cold
  3. Invoke with the same session ID (warm) — reuses the existing session
  4. Compute cold_start = (cold_invoke - cold_agent) - (warm_invoke - warm_agent)
  5. Wait for VM max lifetime so the session is fully destroyed before the next round

Requires runtimes to already be deployed (via deploy.py).

Usage:
    python experiment2.py --rounds 5
    python experiment2.py --rounds 5 --mode zip
    python experiment2.py --rounds 5 --wait 120
"""

import argparse
import json
import os
import time
import uuid

import config as cfg
import deploy
from invoke import invoke, stop_session

RESULTS_FILE = "results2.json"

# If uptime_s is above this threshold, the session was reused (not a true cold start)
# For Docker runtimes, the container pre-boots at deploy time, so uptime can be
# higher even on a genuine cold start. We use cold_start_ms as a secondary signal.
COLD_START_UPTIME_THRESHOLD = 15.0
COLD_START_MS_THRESHOLD = 1000.0  # Below this, likely a warm session


def run_round(arn, round_num):
    session_id = "bench_" + uuid.uuid4().hex
    result = {"round": round_num, "session_id": session_id, "ok": False}

    # Cold invoke — new session ID forces new microVM
    print(f"    cold invoke (session={session_id[:16]}…) …", end=" ", flush=True)
    try:
        cold_ms, cold_agent_ms, uptime_s = invoke(arn, session_id)
        print(f"{cold_ms:.0f}ms (agent={cold_agent_ms}ms, uptime={uptime_s}s)")
        result.update(cold_invoke_ms=round(cold_ms, 1), cold_agent_ms=cold_agent_ms, uptime_s=uptime_s)

        # Validate this was actually a cold start
        if uptime_s is not None and uptime_s > COLD_START_UPTIME_THRESHOLD:
            print(f"    ⚠ NOT a cold start — uptime {uptime_s}s > {COLD_START_UPTIME_THRESHOLD}s threshold (session was reused)")
            result["cold_start_valid"] = False
        else:
            result["cold_start_valid"] = True
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

            # Determine if this was a valid cold start:
            # - Low uptime (<15s) = definitely cold (ZIP-style, process just started)
            # - High uptime but high overhead (>1000ms) = cold (Docker pre-boots container)
            # - High uptime and low overhead (<1000ms) = warm (session was reused)
            if result.get("cold_start_valid", True):
                result["ok"] = True
            elif cold_start >= COLD_START_MS_THRESHOLD:
                # Docker pre-boot: uptime is high but overhead confirms it was cold
                result["ok"] = True
                result["cold_start_valid"] = True
                print(f"    ✓ Reclassified as cold start (overhead {cold_start:.0f}ms > {COLD_START_MS_THRESHOLD}ms)")
            else:
                result["ok"] = False

            status = "" if result["ok"] else " (INVALID — session was warm)"
            print(f"    → cold start overhead: {cold_start:.0f}ms{status}")
    except Exception as e:
        print(f"ERROR: {e}")
        result["error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="Cold start benchmark using session rotation")
    parser.add_argument("--rounds", type=int, default=cfg.DEFAULT_ROUNDS)
    parser.add_argument("--mode", choices=["zip", "docker"])
    parser.add_argument("--wait", type=int, default=cfg.MAX_LIFETIME_SECONDS + 30,
                        help=f"Seconds to wait between rounds for VM teardown (default: {cfg.MAX_LIFETIME_SECONDS + 30})")
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
            result = run_round(arn, i)
            results.append(result)

            # Explicitly stop the session to force VM teardown
            sid = result.get("session_id")
            if sid:
                print(f"    stopping session …", end=" ", flush=True)
                stop_session(arn, sid)
                print("done")

            if i < args.rounds:
                print(f"    waiting {args.wait}s for VM teardown …", flush=True)
                time.sleep(args.wait)

        valid = [r for r in results if r.get("ok")]
        invalid = [r for r in results if not r.get("ok")]
        print(f"\n  Summary: {len(valid)} valid cold starts, {len(invalid)} invalid/failed")
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
