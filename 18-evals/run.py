"""Stage 18: evals. Deterministic checks read from traces, and the case set as an offline eval.

    make platform ; make collector
    python 18-evals/run.py                   # checks on every archived real run (no model calls)
    python 18-evals/run.py --cases           # the 18 cases, live, scored and checked
"""
import collections
import glob
import json
import sys
import time
from pathlib import Path

import httpx

from evals.checks import CHECKS, evaluate
from evals.emit import emit

sys.stdout.reconfigure(line_buffering=True)
ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "http://127.0.0.1:8710"


def archived_runs():
    by = collections.defaultdict(list)
    for f in sorted((ROOT / "history").glob("spans-*.jsonl")) + sorted((ROOT / "var" / "collector").glob("spans-*.jsonl")):
        for line in f.read_text().splitlines():
            s = json.loads(line)
            if "replay.of" not in s["attributes"] and not s["name"].startswith("evaluate "):
                by[s["trace_id"]].append(s)
    return {t: spans for t, spans in by.items()
            if any(not s["parent_id"] and s["name"].startswith("invoke_workflow") for s in spans)}


def run_records():
    rows = {}
    for f in glob.glob(str(ROOT / "history" / "*.jsonl")) + glob.glob(str(ROOT / "*" / "data" / "*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if r.get("run_id"):
                rows[r["run_id"]] = r
    return rows


def traces_part():
    from agentkit import telemetry
    telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")
    runs, records = archived_runs(), run_records()
    print(f"=== Deterministic checks on {len(runs)} archived real runs (stages 10-12) ===")
    results, joined = {}, 0
    for tid, spans in runs.items():
        root = next(s for s in spans if not s["parent_id"])
        rec = records.get(root["attributes"].get("refund_agent.run_id"))
        joined += rec is not None
        results[tid] = (root, evaluate(spans, rec))
    print(f"  {joined} of {len(runs)} runs joined to their record in a traffic log (for the spend check)")
    table = collections.defaultdict(lambda: [0, 0])
    for _, res in results.values():
        for name, r in res.items():
            table[name][0 if r["passed"] else 1] += 1
    print(f"\n  {'check':<34}{'pass':>6}{'fail':>6}")
    for check in CHECKS:
        p, f = table.get(check.__name__, [0, 0])
        print(f"  {check.__name__:<34}{p:>6}{f:>6}" + ("" if p + f else "   (didn't apply)"))
    print("\n  failures:")
    shown = collections.Counter()
    for tid, (root, res) in results.items():
        for name, r in res.items():
            if not r["passed"] and shown[name] < 3:
                shown[name] += 1
                print(f"    {name:<32} {root['attributes'].get('refund_agent.run_id')}: {r['explanation']}")
    # results into the traces, next to what they judged
    archive = (sorted((ROOT / "var" / "collector").glob("spans-*.jsonl")) or sorted((ROOT / "history").glob("spans-*.jsonl")))[-1]
    httpx.post(f"{PLATFORM}/admin/clear-traces")
    httpx.post(f"{PLATFORM}/admin/restore", params={"path": str(archive), "skip_prefix": "evaluate "})
    for tid, (root, res) in results.items():
        emit(tid, root["span_id"], res)
    telemetry.flush()
    time.sleep(2)
    example = next(t for t, (_, res) in results.items() if any(not r["passed"] for r in res.values()))
    print(f"\n  {sum(len(r) for _, r in results.values())} evaluation results written into their traces "
          f"(gen_ai.evaluation.result events); e.g. {PLATFORM}/ui/traces → trace {example[:12]}…")
    out = ROOT / "18-evals" / "data"
    out.mkdir(exist_ok=True)
    (out / "trace-checks.json").write_text(json.dumps({t: {"run_id": r["attributes"].get("refund_agent.run_id"),
                                                          "checks": res} for t, (r, res) in results.items()}, indent=1))


def cases_part():
    from agentkit import telemetry
    from agentkit.cases import CASES
    from agentkit.platform import Platform
    from agentkit.runner import outcome, run_case, summary
    telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")
    platform = Platform()
    print(f"\n=== Offline eval: the {len(CASES)} cases, live ===")
    rows = []
    for c in CASES:
        platform.reset()
        r = run_case(c, platform, "18 · evals", prefix="ev")
        ok = outcome(r) == c["expect"]
        rows.append({"case": c["id"], "correct": ok, "run_id": r["run_id"], "cost": r["usage"].cost,
                     "calls": r["usage"].calls, "seconds": r["seconds"]})
        print(f"  {'✓' if ok else '✗'} {c['id']:<24} {summary(r['state'])}")
    print(f"  {sum(r['correct'] for r in rows)}/{len(rows)} correct · ${sum(r['cost'] for r in rows):.4f} · "
          f"{sum(r['calls'] for r in rows)} model calls")
    (ROOT / "18-evals" / "data" / "cases.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    httpx.get(f"{PLATFORM}/health")
    if "--cases" in sys.argv:
        cases_part()
    else:
        traces_part()
