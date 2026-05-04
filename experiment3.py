"""Discover the pre-warmed VM pool size for AgentCore Docker runtimes.

Strategy:
  Phase 1 — Binary search: send increasing concurrent requests to find
            the boundary where pre-warmed VMs run out and cold provisioning begins.
  Phase 2 — Replenishment: after draining the pool, wait and probe again
            to see how quickly the pool refills.

A VM is classified as "pre-warmed" if uptime_s > 10s (it was booted before
the request arrived). A "cold" VM has uptime_s < 10s (provisioned on demand).

Key AgentCore quotas (from docs):
  - New sessions created rate (container): 100 per minute per endpoint
  - Active session workloads per account: 500 (ap-southeast-2)
  - InvokeAgentRuntime API rate: 25 TPS per agent

Requires:
  - Docker runtime deployed with vm_id support in app.py
  - python deploy.py --mode docker

Usage:
    python experiment3.py
    python experiment3.py --max-concurrent 50
    python experiment3.py --replenish-probes 5 --replenish-interval 30
"""

import argparse
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import config as cfg
import deploy
from invoke import invoke, stop_session

RESULTS_FILE = "results3.json"
PRE_WARM_THRESHOLD = 10.0  # seconds — above this = pre-warmed


def send_concurrent(arn, count, label=""):
    """Send `count` concurrent requests with unique session IDs.
    Returns list of result dicts sorted by index."""
    if label:
        print(f"\n  {label}: sending {count} concurrent requests …", flush=True)

    results = []

    def _invoke(i):
        sid = f"pool_{uuid.uuid4().hex}"
        try:
            latency_ms, agent_ms, uptime_s, vm_id, pid = invoke(arn, sid)
            return {
                "index": i, "session_id": sid,
                "latency_ms": round(latency_ms, 1),
                "agent_ms": agent_ms, "uptime_s": uptime_s,
                "vm_id": vm_id, "pid": pid,
                "pre_warmed": uptime_s is not None and uptime_s > PRE_WARM_THRESHOLD,
                "ok": True,
            }
        except Exception as e:
            return {"index": i, "session_id": sid, "error": str(e), "ok": False}

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = {pool.submit(_invoke, i): i for i in range(1, count + 1)}
        for f in as_completed(futures):
            results.append(f.result())

    results.sort(key=lambda r: r["index"])

    # Print summary
    ok = [r for r in results if r["ok"]]
    pre = [r for r in ok if r["pre_warmed"]]
    cold = [r for r in ok if not r["pre_warmed"]]
    vms = set(r["vm_id"] for r in ok if r.get("vm_id"))
    errs = len(results) - len(ok)

    for r in results:
        if r["ok"]:
            tag = "WARM" if r["pre_warmed"] else "COLD"
            print(f"    [{r['index']:3d}] {tag}  latency={r['latency_ms']:7.0f}ms  "
                  f"uptime={r['uptime_s']:8.1f}s  vm={str(r['vm_id'])[:12]}…")
        else:
            print(f"    [{r['index']:3d}] ERR   {r['error'][:60]}")

    print(f"    → {len(pre)} pre-warmed, {len(cold)} cold, {len(vms)} unique VMs, {errs} errors")
    return results


def stop_all_sessions(arn, results):
    """Stop all sessions from a batch to release VMs back to pool."""
    sids = [r["session_id"] for r in results if r.get("ok")]
    print(f"  Stopping {len(sids)} sessions …", end=" ", flush=True)
    for sid in sids:
        try:
            stop_session(arn, sid)
        except Exception:
            pass
    print("done")


def count_pre_warmed(results):
    return sum(1 for r in results if r.get("ok") and r.get("pre_warmed"))


def count_cold(results):
    return sum(1 for r in results if r.get("ok") and not r.get("pre_warmed"))


# ── Phase 1: Binary search ───────────────────────────────────────────

def find_pool_size(arn, max_concurrent, wait_between):
    """Binary search for the pool size boundary."""
    print(f"\n{'='*60}")
    print(f"  PHASE 1: BINARY SEARCH FOR POOL SIZE")
    print(f"  Range: 1 – {max_concurrent}")
    print(f"{'='*60}")

    probes = []
    lo, hi = 1, max_concurrent
    pool_size_estimate = 0

    while lo <= hi:
        mid = (lo + hi) // 2
        print(f"\n  --- Probing with {mid} concurrent requests (range [{lo}, {hi}]) ---")

        results = send_concurrent(arn, mid, label=f"Probe n={mid}")
        pre = count_pre_warmed(results)
        cold = count_cold(results)
        probes.append({"n": mid, "pre_warmed": pre, "cold": cold})

        stop_all_sessions(arn, results)

        if cold == 0:
            # All pre-warmed — pool is at least this big
            pool_size_estimate = mid
            lo = mid + 1
            print(f"    All {pre} pre-warmed → pool >= {mid}, searching higher")
        else:
            # Some cold — pool is smaller than this
            hi = mid - 1
            print(f"    {cold} cold out of {mid} → pool < {mid}, searching lower")

        if lo <= hi:
            print(f"  Waiting {wait_between}s for pool to replenish …", flush=True)
            time.sleep(wait_between)

    print(f"\n  ★ Estimated pool size: {pool_size_estimate}")
    if pool_size_estimate == max_concurrent:
        print(f"    (Pool may be larger — increase --max-concurrent to find the limit)")

    return pool_size_estimate, probes


# ── Phase 2: Replenishment ───────────────────────────────────────────

def test_replenishment(arn, pool_size, probes_count, interval):
    """After draining the pool, check how quickly it refills."""
    if pool_size == 0:
        print("\n  Skipping replenishment test (pool size unknown)")
        return []

    drain_count = max(pool_size + 5, pool_size * 2)
    print(f"\n{'='*60}")
    print(f"  PHASE 2: POOL REPLENISHMENT")
    print(f"  Draining with {drain_count} concurrent requests, then probing")
    print(f"  {probes_count} times at {interval}s intervals")
    print(f"{'='*60}")

    # Drain
    drain_results = send_concurrent(arn, drain_count, label="Drain")
    stop_all_sessions(arn, drain_results)

    # Probe replenishment
    replenish_probes = []
    for i in range(1, probes_count + 1):
        print(f"\n  Waiting {interval}s …", flush=True)
        time.sleep(interval)

        results = send_concurrent(arn, pool_size or 5, label=f"Replenish probe {i}/{probes_count}")
        pre = count_pre_warmed(results)
        replenish_probes.append({
            "probe": i,
            "elapsed_s": i * interval,
            "pre_warmed": pre,
            "total": len([r for r in results if r["ok"]]),
        })
        stop_all_sessions(arn, results)
        print(f"    → {pre} pre-warmed after {i * interval}s")

    return replenish_probes


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Discover AgentCore pre-warmed VM pool size")
    parser.add_argument("--mode", choices=["zip", "docker"], default="docker")
    parser.add_argument("--max-concurrent", type=int, default=30,
                        help="Upper bound for binary search (default: 30)")
    parser.add_argument("--wait-between", type=int, default=180,
                        help="Seconds between binary search probes for pool to replenish (default: 180)")
    parser.add_argument("--replenish-probes", type=int, default=3,
                        help="Number of replenishment probes after draining (default: 3)")
    parser.add_argument("--replenish-interval", type=int, default=60,
                        help="Seconds between replenishment probes (default: 60)")
    parser.add_argument("--skip-replenish", action="store_true",
                        help="Skip the replenishment phase")
    parser.add_argument("--blast", action="store_true",
                        help="Skip binary search — just send --max-concurrent requests at once")
    args = parser.parse_args()

    arns = deploy.load_arns()
    if args.mode not in arns:
        print(f"No {args.mode} runtime found. Run 'python deploy.py --mode {args.mode}' first.")
        return

    arn = arns[args.mode]
    print(f"  Mode: {args.mode.upper()}")
    print(f"  ARN: {arn}")
    print(f"  Pre-warm threshold: >{PRE_WARM_THRESHOLD}s uptime")
    print(f"  Note: Container new session rate limit is 100/min per endpoint")

    # Phase 1
    if args.blast:
        print(f"\n{'='*60}")
        print(f"  BLAST MODE: sending {args.max_concurrent} concurrent requests")
        print(f"{'='*60}")
        results = send_concurrent(arn, args.max_concurrent, label=f"Blast n={args.max_concurrent}")
        ok = [r for r in results if r["ok"]]
        pool_size = sum(1 for r in ok if r.get("pre_warmed"))
        search_probes = [{"n": args.max_concurrent, "pre_warmed": pool_size, "cold": len(ok) - pool_size}]
        stop_all_sessions(arn, results)
        print(f"\n  ★ {pool_size} pre-warmed out of {len(ok)} successful")
    else:
        pool_size, search_probes = find_pool_size(arn, args.max_concurrent, args.wait_between)

    # Phase 2
    replenish_probes = []
    if not args.skip_replenish:
        replenish_probes = test_replenishment(
            arn, pool_size, args.replenish_probes, args.replenish_interval)

    # Final report
    print(f"\n{'='*60}")
    print(f"  FINAL REPORT")
    print(f"{'='*60}")
    print(f"  Estimated pool size:  {pool_size}")
    if replenish_probes:
        for p in replenish_probes:
            print(f"  After {p['elapsed_s']:3d}s: {p['pre_warmed']}/{p['total']} pre-warmed")

    print(f"\n  How to increase pool size:")
    print(f"  - No documented 'provisioned concurrency' setting for AgentCore")
    print(f"  - 'Active session workloads per account' quota (default 500) is adjustable")
    print(f"  - 'New sessions created rate' (100/min for containers) is adjustable")
    print(f"  - Contact AWS support or use Service Quotas console to request increases")

    # Save
    output = {
        "mode": args.mode,
        "runtime_arn": arn,
        "estimated_pool_size": pool_size,
        "search_probes": search_probes,
        "replenish_probes": replenish_probes,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
