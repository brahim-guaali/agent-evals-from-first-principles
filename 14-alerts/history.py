"""The last hours of real traffic as metric points, at the times they really happened.

Each run in the logs of stages 07, 08, 10 and 12 becomes what the SDK would have recorded
for it: a run duration (with error.type if it failed, crashed or was stopped), the time to
first chunk of its streamed reply, and its spend, merged into one-minute delta points with
the GenAI bucket boundaries, then posted to the platform like the collector would.
"""
import json
from collections import defaultdict
from pathlib import Path

from agentkit.telemetry import SECONDS, WORKFLOW

ROOT = Path(__file__).resolve().parents[1]
LOGS = ["07-traffic/data/2026-10-20-0137.jsonl", "08-signals/data/*.jsonl", "10-data-path/data/*.jsonl",
        "12-cost/data/*-costs.jsonl"]


def records():
    rows = []
    for pattern in LOGS:
        for f in sorted(ROOT.glob(pattern)):
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return sorted(rows, key=lambda r: r["finished"])


def failed(r):
    if r.get("crashed"):
        return "model_unavailable"
    if r.get("failures"):
        return "lookup_failed"
    if "budget" in str(r.get("summary")) or "no model" in str(r.get("summary")):
        return "stopped"
    return None


def bucket(bounds, v):
    for i, b in enumerate(bounds):
        if v <= b:
            return i
    return len(bounds)


def points(rows, window=60):
    hist = defaultdict(lambda: [0] * (len(WORKFLOW) + 1))
    ttft = defaultdict(lambda: [0] * (len(SECONDS) + 1))
    sums, cost = defaultdict(float), defaultdict(float)
    for r in rows:
        t = int(r["finished"] // window * window)
        err = failed(r)
        hist[(t, err)][bucket(WORKFLOW, r["seconds"])] += 1
        sums[(t, err)] += r["seconds"]
        if r.get("ttft"):
            model = max((r.get("models") or {"?": 1}).items(), key=lambda kv: kv[1])[0]
            ttft[(t, model)][bucket(SECONDS, r["ttft"])] += 1
        cost[t] += r.get("cost") or 0
    out = []
    for (t, err), b in hist.items():
        out.append({"name": "gen_ai.invoke_workflow.duration", "kind": "histogram", "t": t + window, "start": t,
                    "attributes": {"gen_ai.workflow.name": "refund_agent", **({"error.type": err} if err else {})},
                    "count": sum(b), "sum": sums[(t, err)], "bounds": WORKFLOW, "buckets": b, "environment": "replay"})
    for (t, model), b in ttft.items():
        out.append({"name": "gen_ai.client.inference.time_to_first_chunk", "kind": "histogram", "t": t + window,
                    "start": t, "attributes": {"gen_ai.response.model": model}, "count": sum(b), "sum": 0.0,
                    "bounds": SECONDS, "buckets": b, "environment": "replay"})
    for t, v in cost.items():
        out.append({"name": "refund_agent.cost_usd", "kind": "sum", "t": t + window, "start": t, "attributes": {},
                    "value": v, "environment": "replay"})
    return out
