"""Stage 14: SLOs, alerts and drift, evaluated over the real traffic of stages 07-12.

    make platform
    python 14-alerts/run.py
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from history import failed, points, records
from services.alerts import Engine, default_rules, load_points, window

sys.stdout.reconfigure(line_buffering=True)
PLATFORM = "http://127.0.0.1:8710"
OUT = Path(__file__).resolve().parent / "data"


def clock(t, t0):
    m = int((t - t0) // 60)
    return f"{m // 60}:{m % 60:02d}"


def what_happened(rows, t0):
    """The real events in the timeline, from the run logs (not from the alerts)."""
    out = []
    first_31 = next(r for r in rows if (r.get("models") or {}).get("gemini-3.1-flash-lite"))
    out.append((first_31["finished"], "the fallback model (3.1 Flash-Lite) starts answering: 3.5's daily quota is gone"))
    crashes = [r for r in rows if r.get("crashed")]
    if crashes:
        out.append((crashes[0]["finished"], f"both models out of quota: {len(crashes)} runs crash within "
                                            f"{(crashes[-1]['finished'] - crashes[0]['finished']) / 60:.0f} minutes"))
    worst = max(rows, key=lambda r: r.get("queued_s", 0))
    out.append((worst["finished"], f"the overloaded batch: a customer queued {worst['queued_s'] / 60:.0f} minutes"))
    return sorted(out)


if __name__ == "__main__":
    rows = records()
    t0, t_end = rows[0]["finished"] - 60, rows[-1]["finished"] + 900
    httpx.post(f"{PLATFORM}/admin/clear-metrics")
    httpx.post(f"{PLATFORM}/admin/clear-alerts")
    pts = points(rows)
    httpx.post(f"{PLATFORM}/v1/points", json=pts).raise_for_status()
    print(f"=== {len(rows)} real runs from stages 07-12 → {len(pts)} one-minute metric points, "
          f"{(rows[-1]['finished'] - rows[0]['finished']) / 60:.0f} minutes of traffic ===")
    for t, text in what_happened(rows, t0):
        print(f"  {clock(t, t0)}  {text}")

    stored = load_points(httpx.get(f"{PLATFORM}/metrics/points").json())
    rules = default_rules(ref_start=t0, ref_end=t0 + 900)
    print("\n=== Rules ===")
    for r in rules:
        print(f"  {r.name:<24} {r.description}")
    engine = Engine(rules)
    now, series = t0 + 300, []
    while now <= t_end:
        engine.tick(stored, now)
        w5 = window(stored, "gen_ai.invoke_workflow.duration", now - 300, now, slow_s=60)
        ttft = window(stored, "gen_ai.client.inference.time_to_first_chunk", now - 300, now)
        series.append({"t": now, "runs": w5.count, "failed": w5.failed, "slow": w5.slow, "ttft_buckets": ttft.buckets,
                       "ttft_bounds": ttft.bounds, "firing": sorted(engine.firing)})
        now += 60
    print("\n=== Alerts, evaluated every minute as the traffic happened ===")
    for e in engine.events:
        lasted = f" after {e['lasted_s'] / 60:.0f} min" if e["state"] == "resolved" else ""
        print(f"  {clock(e['t'], t0)}  {e['state'].upper():<9} {e['rule']:<24} {e['detail']}{lasted}")
    for rule in rules:
        if rule.name in engine.firing:
            print(f"  (still firing at the end: {rule.name})")
    httpx.post(f"{PLATFORM}/alerts", json=engine.events)
    total, bad = len(rows), sum(1 for r in rows if failed(r))
    slow = sum(1 for r in rows if r["seconds"] > 60)
    print(f"\n=== SLOs over the whole window ===")
    print(f"  availability: {1 - bad / total:.1%} of runs without a failure (objective 99%) · "
          f"error budget used: {bad / total / 0.01:.0f}x")
    print(f"  latency: {1 - slow / total:.1%} of runs within 60 s (objective 90%) · budget used: {slow / total / 0.10:.1f}x")
    OUT.mkdir(exist_ok=True)
    (OUT / "alerts.json").write_text(json.dumps({"t0": t0, "events": engine.events, "series": series,
                                                 "happened": what_happened(rows, t0)}, indent=1))
