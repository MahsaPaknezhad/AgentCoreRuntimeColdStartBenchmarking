"""Print cold start report from results.json."""

import argparse
import json
import sys
import config as cfg


def percentile(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def stats(vals):
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(s), "mean": round(sum(s)/len(s), 1), "p50": round(percentile(s, 50), 1),
            "p90": round(percentile(s, 90), 1), "min": round(s[0], 1), "max": round(s[-1], 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--file", default=cfg.RESULTS_FILE)
    args = parser.parse_args()

    try:
        with open(args.file) as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No results file. Run experiment.py first.")

    if args.json:
        print(json.dumps(data, indent=2))
        return

    for mode, rounds in data.items():
        ok = [r for r in rounds if r.get("ok")]
        print(f"\n{'='*100}")
        print(f"  {mode.upper()} — {len(ok)}/{len(rounds)} successful rounds")
        print(f"{'='*100}")

        # Per-round detail
        hdr = (f"{'Rnd':>3} {'ColdInv(ms)':>12} {'WarmInv(ms)':>12}"
               f" {'AgentMs':>9} {'ColdStart(ms)':>14} {'Uptime(s)':>10}")
        print(hdr)
        print("-" * len(hdr))
        for r in rounds:
            if not r.get("ok"):
                print(f"{r['round']:>3}  FAILED  {r.get('error','')[:70]}")
                continue
            print(f"{r['round']:>3} {r['cold_invoke_ms']:>12.0f}"
                  f" {r['warm_invoke_ms']:>12.0f} {r.get('cold_agent_ms',0):>9.0f}"
                  f" {r['cold_start_ms']:>14.0f} {r.get('uptime_s',0):>10.3f}")

        if not ok:
            continue

        # Summary
        cs = stats([r["cold_start_ms"] for r in ok])
        up = stats([r["uptime_s"] for r in ok if r.get("uptime_s")])
        ci = stats([r["cold_invoke_ms"] for r in ok])
        wi = stats([r["warm_invoke_ms"] for r in ok])

        print(f"\n  cold_start_ms (cold invoke overhead − warm invoke overhead):")
        print(f"    mean={cs['mean']:.0f}ms  p50={cs['p50']:.0f}ms  p90={cs['p90']:.0f}ms")

        if up:
            print(f"\n  uptime_s (app boot time — Python start → first request):")
            print(f"    mean={up['mean']:.3f}s  p50={up['p50']:.3f}s  range=[{up['min']:.3f}, {up['max']:.3f}]s")

        print(f"\n  latency_ms (cold invoke: API call → full response received):")
        print(f"    mean={ci['mean']:.0f}ms ({ci['mean']/1000:.1f}s)  p50={ci['p50']:.0f}ms  p90={ci['p90']:.0f}ms")

        print(f"\n  warm latency_ms (warm invoke: same, on already-warm runtime):")
        print(f"    mean={wi['mean']:.0f}ms ({wi['mean']/1000:.1f}s)  p50={wi['p50']:.0f}ms")
    print()


if __name__ == "__main__":
    main()
