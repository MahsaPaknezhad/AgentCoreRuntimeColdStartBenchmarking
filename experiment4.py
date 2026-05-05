"""End-to-end pre-warm pool observation experiment.

Deploys a fresh Docker runtime, then:
  1. Waits 2 minutes (lets the platform pre-warm VMs)
  2. Sends 10 concurrent invocations with unique session IDs
  3. Waits 5 minutes (lets VMs idle-timeout and pool potentially replenish)
  4. Sends 2 invocations with unique session IDs
  5. Prints uptime, vm_id, cold/warm classification for each invocation
  6. Tears down the runtime

This tests whether the platform pre-warms VMs after deployment and whether
the pool replenishes after sessions are consumed.

Usage:
    python experiment4.py
    python experiment4.py --keep          # don't delete runtime after
    python experiment4.py --mode zip      # test ZIP instead
"""

import argparse
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import config as cfg
import deploy
from invoke import invoke

RESULTS_FILE = "results4.json"
PRE_WARM_THRESHOLD = 10.0  # seconds


def send_batch(arn, count, label):
    """Send `count` concurrent invocations. Returns list of result dicts."""
    print(f"\n  {label}: sending {count} concurrent requests …", flush=True)

    def _invoke(i):
        sid = f"exp4_{uuid.uuid4().hex}"
        try:
            latency_ms, agent_ms, uptime_s, vm_id, pid = invoke(arn, sid)
            pre_warmed = uptime_s is not None and uptime_s > PRE_WARM_THRESHOLD
            return {
                "index": i, "session_id": sid,
                "latency_ms": round(latency_ms, 1),
                "agent_ms": agent_ms, "uptime_s": uptime_s,
                "vm_id": vm_id, "pid": pid,
                "classification": "WARM (pre-warmed)" if pre_warmed else "COLD",
                "ok": True,
            }
        except Exception as e:
            return {"index": i, "session_id": sid, "error": str(e), "ok": False}

    results = []
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = {pool.submit(_invoke, i): i for i in range(1, count + 1)}
        for f in as_completed(futures):
            results.append(f.result())

    results.sort(key=lambda r: r["index"])

    # Print
    for r in results:
        if r["ok"]:
            print(f"    [{r['index']:2d}] {r['classification']:18s}  "
                  f"latency={r['latency_ms']:7.0f}ms  "
                  f"uptime={r['uptime_s']:8.1f}s  "
                  f"vm={str(r['vm_id'])[:12]}…")
        else:
            print(f"    [{r['index']:2d}] ERROR: {r['error'][:60]}")

    ok = [r for r in results if r["ok"]]
    warm = [r for r in ok if "pre-warmed" in r.get("classification", "")]
    cold = [r for r in ok if r.get("classification") == "COLD"]
    print(f"    → {len(warm)} pre-warmed, {len(cold)} cold, {len(results) - len(ok)} errors")

    return results


def main():
    parser = argparse.ArgumentParser(description="Pre-warm pool observation: deploy → wait → invoke → wait → invoke")
    parser.add_argument("--mode", choices=["zip", "docker"], default="docker")
    parser.add_argument("--keep", action="store_true", help="Don't delete runtime after test")
    args = parser.parse_args()

    ctrl = deploy.control_client()

    # Step 1: Deploy fresh runtime
    print(f"\n{'='*60}")
    print(f"  EXPERIMENT 4: PRE-WARM POOL OBSERVATION ({args.mode.upper()})")
    print(f"{'='*60}")

    print(f"\n[1/6] Deploying fresh {args.mode} runtime …")
    if args.mode == "docker":
        image_uri = deploy.ensure_docker_artifacts()
        name = f"exp4_docker_{uuid.uuid4().hex[:6]}"
        arn = deploy.create_docker_runtime(ctrl, image_uri, name=name)
    else:
        s3_key = deploy.ensure_zip_artifacts()
        name = f"exp4_zip_{uuid.uuid4().hex[:6]}"
        arn = deploy.create_zip_runtime(ctrl, name=name, s3_key=s3_key)

    print(f"  Name: {name}")
    print(f"  ARN: {arn}")

    print(f"\n[2/6] Waiting for READY …", flush=True)
    deploy.wait_for_ready(ctrl, arn)
    ready_time = time.time()
    print(f"  ✓ Runtime is READY")

    # Step 2: Wait 2 minutes
    wait1 = 20
    print(f"\n[3/6] Waiting {wait1}s for platform to pre-warm VMs …", flush=True)
    time.sleep(wait1)
    elapsed_since_ready = time.time() - ready_time
    print(f"  {elapsed_since_ready:.0f}s since READY")

    # Step 3: Send 15 concurrent
    batch1 = send_batch(arn, 15, "Batch 1 (after 2 min)")

    # Step 4: Wait 5 minutes
    wait2 = 200
    print(f"\n[4/6] Waiting {wait2}s for pool replenishment …", flush=True)
    time.sleep(wait2)
    elapsed_since_ready = time.time() - ready_time
    print(f"  {elapsed_since_ready:.0f}s since READY")

    # Step 5: Send 2 invocations
    batch2 = send_batch(arn, 20, "Batch 2 (after 5 more min)")

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")

    b1_ok = [r for r in batch1 if r["ok"]]
    b1_warm = [r for r in b1_ok if "pre-warmed" in r.get("classification", "")]
    b1_cold = [r for r in b1_ok if r.get("classification") == "COLD"]

    b2_ok = [r for r in batch2 if r["ok"]]
    b2_warm = [r for r in b2_ok if "pre-warmed" in r.get("classification", "")]
    b2_cold = [r for r in b2_ok if r.get("classification") == "COLD"]

    print(f"\n  Batch 1 ({len(b1_ok)} concurrent, {wait1}s after deploy):")
    print(f"    Pre-warmed: {len(b1_warm)}")
    print(f"    Cold:       {len(b1_cold)}")
    if b1_warm:
        uptimes = [r["uptime_s"] for r in b1_warm]
        print(f"    Pre-warmed uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")
    if b1_cold:
        uptimes = [r["uptime_s"] for r in b1_cold if r.get("uptime_s")]
        if uptimes:
            print(f"    Cold uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")

    print(f"\n  Batch 2 ({len(b2_ok)} concurrent, {wait2}s after batch 1):")
    print(f"    Pre-warmed: {len(b2_warm)}")
    print(f"    Cold:       {len(b2_cold)}")
    if b2_warm:
        uptimes = [r["uptime_s"] for r in b2_warm]
        print(f"    Pre-warmed uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")
    if b2_cold:
        uptimes = [r["uptime_s"] for r in b2_cold if r.get("uptime_s")]
        if uptimes:
            print(f"    Cold uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")

    # Interpretation
    print(f"\n  Interpretation:")
    if b1_warm:
        print(f"    ✓ Platform pre-warms VMs after deployment ({len(b1_warm)} ready after 2 min)")
    else:
        print(f"    ✗ No pre-warming detected after 2 min — all {len(b1_cold)} were cold")

    if b2_warm:
        print(f"    ✓ Pool replenishes after draining ({len(b2_warm)} pre-warmed after 5 min wait)")
    else:
        print(f"    ✗ Pool did NOT replenish — all {len(b2_cold)} were cold after 5 min wait")

    # Save results
    output = {
        "mode": args.mode,
        "runtime_name": name,
        "runtime_arn": arn,
        "batch1_after_s": wait1,
        "batch1": batch1,
        "batch2_after_s": wait1 + wait2,
        "batch2": batch2,
        "summary": {
            "batch1_pre_warmed": len(b1_warm),
            "batch1_cold": len(b1_cold),
            "batch2_pre_warmed": len(b2_warm),
            "batch2_cold": len(b2_cold),
        },
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to {RESULTS_FILE}")

    # Cleanup
    if not args.keep:
        print(f"\n[6/6] Deleting runtime '{name}' …", end=" ", flush=True)
        deploy.delete_runtime(ctrl, arn)
        print("done")
    else:
        print(f"\n  Runtime kept: {arn}")


if __name__ == "__main__":
    main()
