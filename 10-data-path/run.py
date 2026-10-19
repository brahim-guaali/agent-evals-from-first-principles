"""Stage 10: the data path. OTLP from the agent, through our collector, to the viewer, an archive
and the dashboard; GenAI metrics next to the spans.

    make platform                                 # terminal 1
    make collector                                # terminal 2
    python 10-data-path/run.py                    # 30 requests of traffic, then the numbers
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "07-traffic"))

from agentkit import telemetry
from agentkit.platform import Platform
from agentkit.traffic import Replayer, plan
from report import load, report

sys.stdout.reconfigure(line_buffering=True)
STAGE = "10 · data path"
COLLECTOR = "http://127.0.0.1:4318"


def main(n=30):
    platform = Platform()
    platform.check()
    try:
        httpx.get(f"{COLLECTOR}/health", timeout=2)
    except httpx.HTTPError:
        sys.exit("Start the collector first: make collector")
    platform.reset()
    httpx.post(f"{platform.url}/admin/clear-traces")
    httpx.post(f"{platform.url}/admin/clear-metrics")
    telemetry.setup(otlp=COLLECTOR, environment="staging", metric_interval_s=5)
    batch = time.strftime("%Y-%m-%d-%H%M")
    log = Path(__file__).resolve().parent / "data" / f"{batch}.jsonl"
    print(f"=== {n} requests through agent → OTLP → collector → viewer · archive · dashboard ===")
    Replayer(platform, STAGE, log, workers=3, rate_per_min=4, seed=21, fault_rate=0.05,
             environment="staging").run(plan(n, seed=21), batch=batch)
    telemetry.flush()
    time.sleep(3)

    print("\n=== The data path, counted at each hop ===")
    s = httpx.get(f"{COLLECTOR}/stats").json()
    for k in ("received.requests", "received.spans", "batches", "exported.viewer", "exported.file",
              "received.points", "exported.points", "failed.viewer", "failed.file", "queued"):
        print(f"  {k:<20} {s.get(k, 0):>7,}")
    traces = httpx.get(f"{platform.url}/traces", params={"limit": 1000}).json()
    roots = [t for t in traces if t["root"].startswith("invoke_workflow")]
    print(f"  traces in the viewer: {len(roots)} runs ({sum(t['spans'] for t in traces):,} spans)")

    print("\n=== What the dashboard computes from the metrics (per minute) ===")
    for name, label in [("gen_ai.invoke_workflow.duration", "run"), ("gen_ai.client.inference.duration", "model call"),
                        ("gen_ai.client.inference.time_to_first_chunk", "first chunk")]:
        rows = httpx.get(f"{platform.url}/metrics/series", params={"name": name, "window": 60}).json()
        print(f"  {label}:")
        for r in rows:
            print(f"    minute {int((r['t'] - rows[0]['t']) / 60):>2}  {r['count']:>4} × p50 {r['p50']:6.2f} s  "
                  f"p95 {r['p95']:6.2f} s  p99 {r['p99']:6.2f} s")
        whole = httpx.get(f"{platform.url}/metrics/series", params={"name": name, "window": 86400}).json()
        if whole:
            w = whole[-1]
            print(f"    whole batch {w['count']:>4} × p50 {w['p50']:6.2f} s  p95 {w['p95']:6.2f} s  p99 {w['p99']:6.2f} s")
    tokens = sum(r["value"] for m in ("input", "output") for r in httpx.get(
        f"{platform.url}/metrics/series", params={"name": f"gen_ai.client.inference.usage.{m}_tokens", "window": 86400}).json())
    print(f"  tokens counted by the metric: {tokens:,.0f}")

    print("\n=== The same batch from the run log, for comparison ===")
    report(load(log))


if __name__ == "__main__":
    main(int(sys.argv[1]) if sys.argv[1:] else 30)
