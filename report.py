"""Print a summary report from results.json.

Usage:
    python report.py
    python report.py --json   # machine-readable output
"""

import argparse
import json
import sys

import config as cfg


def percentile(sorted_vals, p):
    """Return the p-th percentile (0–100) from a sorted list."""
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_vals) else f
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


def summarise(latencies):
    s = sorted(latencies)
    return {
        "count": len(s),
        "mean": round(sum(s) / len(s), 1),
        "p50": round(percentile(s, 50), 1),
        "p90": round(percentile(s, 90), 1),
        "p99": round(percentile(s, 99), 1),
        "min": round(s[0], 1),
        "max": round(s[-1], 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--file", default=cfg.RESULTS_FILE)
    args = parser.parse_args()

    try:
        with open(args.file) as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No results file found ({args.file}). Run experiment.py first.")

    report = {}
    for mode, rounds in data.items():
        latencies = [r["latency_ms"] for r in rounds if r.get("ok") and r["latency_ms"]]
        if not latencies:
            report[mode] = {"count": 0, "error": "no successful rounds"}
            continue
        report[mode] = summarise(latencies)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Table output
    header = f"{'Mode':<10}{'Rounds':>7}{'P50 (ms)':>10}{'P90 (ms)':>10}{'P99 (ms)':>10}{'Mean (ms)':>11}{'Min (ms)':>10}{'Max (ms)':>10}"
    print()
    print(header)
    print("-" * len(header))
    for mode, stats in report.items():
        if "error" in stats:
            print(f"{mode:<10}{'0':>7}  {'— no successful rounds —'}")
        else:
            print(f"{mode:<10}{stats['count']:>7}{stats['p50']:>10.0f}{stats['p90']:>10.0f}"
                  f"{stats['p99']:>10.0f}{stats['mean']:>11.0f}{stats['min']:>10.0f}{stats['max']:>10.0f}")
    print()


if __name__ == "__main__":
    main()
