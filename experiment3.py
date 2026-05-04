"""Probe the pre-warmed VM pool size for AgentCore Docker runtimes.

Sends N concurrent requests, each with a unique session ID, and examines
the uptime_s and vm_id of each response to determine:
  - How many VMs were pre-warmed (high uptime)
  - How many were cold-provisioned on demand (low uptime)
  - Whether the pool replenishes after draining

Requires:
  - Runtime already deployed with vm_id support (python deploy.py --mode docker)

Usage:
    python experiment3.py --concurrent 10
    python experiment3.py --concurrent 20 --mode docker
    python experiment3.py --concurrent 5 --waves 3 --wave-delay 30
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import config as cfg
import deploy
from invoke import invoke

RESULTS_FILE = "results3.json"
PRE_WARM_UPTIME_THRESHOLD = 10.0  # VMs with uptime > this were pre-warmed


def invoke_with_metadata(arn, index):
    """Invoke with a unique session ID and return result dict."""
    session_id = f"pool_{uuid.uuid4().hex}"
    t0 = time.monotonic()
    try:
        latency_ms, agent_ms, uptime_s, vm_id, pid = invoke(arn, session_id)
        return {
            "index": index,
            "session_id": session_id,
            "latency_ms": round(latency_ms, 1),
            "agent_ms": agent_ms,
            "uptime_s": uptime_s,
            "vm_id": vm_id,
            "pid": pid,
            "pre_warmed": uptime_s is not None and uptime_s > PRE_WARM_UPTIME_THRESHOLD,
            "ok": True,
        }
    except Exception as e:
        return {
            "index": index,
            "session_id": session_id,
            "error": str(e),
            "ok": False,
        }


def run_wave(arn, wave_num, concurrent):
    """Send concurrent requests and return results sorted by index."""
    print(f"\n  Wave {wave_num}: sending {concurrent} concurrent requests …", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {
            pool.submit(invoke_with_metadata, arn, i): i
            for i in range(1, concurrent + 1)
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r["index"])

    # Print results
    for r in results:
        if r["ok"]:
            tag = "PRE-WARMED" if r["pre_warmed"] else "COLD"
            print(f"    [{r['index']:2d}] {tag:10s}  latency={r['latency_ms']:7.0f}ms  "
                  f"uptime={r['uptime_s']:8.1f}s  vm={r['vm_id']}")
        else:
            print(f"    [{r['index']:2d}] ERROR: {r['error']}")

    # Summary
    successful = [r for r in results if r["ok"]]
    pre_warmed = [r for r in successful if r["pre_warmed"]]
    cold = [r for r in successful if not r["pre_warmed"]]
    unique_vms = set(r["vm_id"] for r in successful if r.get("vm_id"))

    print(f"\n    Summary: {len(pre_warmed)} pre-warmed, {len(cold)} cold, "
          f"{len(unique_vms)} unique VMs, {len(results) - len(successful)} errors")

    if pre_warmed:
        uptimes = [r["uptime_s"] for r in pre_warmed]
        print(f"    Pre-warmed uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")
    if cold:
        uptimes = [r["uptime_s"] for r in cold if r.get("uptime_s") is not None]
        if uptimes:
            print(f"    Cold uptime range: {min(uptimes):.1f}s – {max(uptimes):.1f}s")
        latencies = [r["latency_ms"] for r in cold]
        print(f"    Cold latency range: {min(latencies):.0f}ms – {max(latencies):.0f}ms")

    return results


def main():
    parser = argparse.ArgumentParser(description="Probe AgentCore pre-warmed VM pool size")
    parser.add_argument("--concurrent", type=int, default=10,
                        help="Number of concurrent requests per wave (default: 10)")
    parser.add_argument("--mode", choices=["zip", "docker"], default="docker")
    parser.add_argument("--waves", type=int, default=1,
                        help="Number of waves to send (default: 1). Multiple waves test pool replenishment.")
    parser.add_argument("--wave-delay", type=int, default=60,
                        help="Seconds to wait between waves (default: 60)")
    args = parser.parse_args()

    arns = deploy.load_arns()
    if args.mode not in arns:
        print(f"No {args.mode} runtime found. Run 'python deploy.py --mode {args.mode}' first.")
        return

    arn = arns[args.mode]
    print(f"{'='*60}")
    print(f"  VM POOL SIZE PROBE — {args.mode.upper()}")
    print(f"  ARN: {arn}")
    print(f"  Concurrent requests per wave: {args.concurrent}")
    print(f"  Waves: {args.waves}")
    print(f"  Pre-warm threshold: >{PRE_WARM_UPTIME_THRESHOLD}s uptime")
    print(f"{'='*60}")

    all_waves = []
    for w in range(1, args.waves + 1):
        wave_results = run_wave(arn, w, args.concurrent)
        all_waves.append({"wave": w, "results": wave_results})

        if w < args.waves:
            print(f"\n  Waiting {args.wave_delay}s before next wave …", flush=True)
            time.sleep(args.wave_delay)

    # Overall analysis
    print(f"\n{'='*60}")
    print(f"  OVERALL ANALYSIS")
    print(f"{'='*60}")

    all_successful = [r for wave in all_waves for r in wave["results"] if r["ok"]]
    all_pre_warmed = [r for r in all_successful if r["pre_warmed"]]
    all_cold = [r for r in all_successful if not r["pre_warmed"]]
    all_vms = set(r["vm_id"] for r in all_successful if r.get("vm_id"))

    print(f"  Total invocations: {len(all_successful)}")
    print(f"  Total unique VMs:  {len(all_vms)}")
    print(f"  Pre-warmed:        {len(all_pre_warmed)}")
    print(f"  Cold-provisioned:  {len(all_cold)}")

    if all_pre_warmed and not all_cold:
        print(f"\n  → Pool size >= {args.concurrent} (all requests served from pre-warmed VMs)")
        print(f"    Try increasing --concurrent to find the limit.")
    elif all_pre_warmed and all_cold:
        print(f"\n  → Estimated pool size: ~{len(all_pre_warmed)} pre-warmed VMs")
        print(f"    {len(all_cold)} requests had to cold-provision.")
    elif all_cold and not all_pre_warmed:
        print(f"\n  → No pre-warmed VMs detected. Pool may be empty or not used for {args.mode}.")

    # Save
    output = {
        "mode": args.mode,
        "runtime_arn": arn,
        "concurrent_per_wave": args.concurrent,
        "waves": args.waves,
        "total_invocations": len(all_successful),
        "unique_vms": len(all_vms),
        "pre_warmed_count": len(all_pre_warmed),
        "cold_count": len(all_cold),
        "wave_results": all_waves,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
