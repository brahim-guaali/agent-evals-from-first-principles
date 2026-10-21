"""Stage 20: online evals on live traffic, and a bad production run turned into a test case.

    make platform ; make collector
    python 20-online-evals/run.py              # online: real traces replayed as traffic, scored as they land
    python 20-online-evals/run.py --curate     # a flagged live run → a curated case → before/after a fix
    python 20-online-evals/run.py --timelines  # stage 17's labelled replies → the FAQ timeline check → a case
"""
import collections
import json
import sys
import threading
import time
from pathlib import Path

import httpx

from agentkit.span_replay import categorise, copy_trace, load_traces, plan, send
from evals.online import OnlineEvaluator

sys.stdout.reconfigure(line_buffering=True)
ROOT = Path(__file__).resolve().parents[1]
PLATFORM, COLLECTOR = "http://127.0.0.1:8710", "http://127.0.0.1:4318"


def online(n=150, per_s=3.0):
    httpx.post(f"{PLATFORM}/admin/clear-traces")
    httpx.post(f"{COLLECTOR}/config", params={"processors": ""})
    traces = [[s for s in t if not s["name"].startswith("evaluate ")]              # stage 18's results aren't traffic
              for f in sorted((ROOT / "history").glob("spans-*.jsonl")) + sorted((ROOT / "var" / "collector").glob("spans-*.jsonl"))
              for t in load_traces(f)
              if not any("replay.of" in s["attributes"] for s in t)]
    chosen, rng = plan(traces, n, seed=61, spread_s=n / per_s)
    print(f"=== {n} real runs replayed as live traffic ({per_s:g} a second); the evaluator follows ===")
    ev, start = OnlineEvaluator(PLATFORM), time.time()
    stop = threading.Event()
    scored = []

    def follow():
        while not stop.is_set():
            scored.append((time.time() - start, ev.poll(since=start - 1)))
            time.sleep(1.0)
    threading.Thread(target=follow, daemon=True).start()
    for spans, _ in chosen:
        _, copy = copy_trace(spans, time.time(), rng)
        send(f"{COLLECTOR}/v1/traces", copy, {"service.name": "refund-agent", "replay": True})
        time.sleep(1 / per_s)
    time.sleep(6)
    stop.set()
    time.sleep(1.2)
    ev.poll(since=start - 1)
    lag = [t for t, k in scored if k]
    print(f"  evaluated {len(ev.done)} runs while they arrived (first score {lag[0]:.1f} s after the first run, "
          f"last {lag[-1]:.1f} s)")
    print(f"\n  {'check':<34}{'pass':>6}{'fail':>6}  pass rate")
    names = sorted({n for n, _ in ev.counts})
    for name in names:
        p, f = ev.counts.get((name, "pass"), 0), ev.counts.get((name, "fail"), 0)
        print(f"  {name:<34}{p:>6}{f:>6}  {p / (p + f):.0%}")
    series = httpx.get(f"{PLATFORM}/metrics/series", params={"name": "refund_agent.evaluations", "window": 60,
                                                              "group_by": "gen_ai.evaluation.score.label"}).json()
    by = collections.defaultdict(dict)
    for row in series:
        by[row["t"]][row["group"]] = row["value"]
    print("\n  pass rate per minute, all checks: " + ", ".join(
        f"{v.get('pass', 0) / max(v.get('pass', 0) + v.get('fail', 0), 1):.0%}" for _, v in sorted(by.items())))
    (ROOT / "20-online-evals" / "data" / "online.json").write_text(json.dumps(
        {"counts": {f"{n}:{l}": v for (n, l), v in ev.counts.items()}, "series": series}, indent=1))


def curated():
    from agent.graph import build
    from agentkit import telemetry
    from agentkit.llm import Gemini
    from agentkit.platform import Platform
    from agentkit.cases import CASES
    from agentkit.trace import NullTracer
    from evals import curate
    telemetry.setup(otlp=COLLECTOR, environment="staging")
    platform = Platform()
    arabic = next(c for c in CASES if c["id"] == "arabic-late")
    print("=== 1. Live traffic until the online check flags a reply ===")
    graph, found = build(Gemini(), platform, NullTracer()), None
    for i in range(1, 7):
        platform.reset()
        tid = f"cur-{i}"
        state = graph.invoke({"request": {**arabic, "session_id": tid}}, {"configurable": {"thread_id": tid}})
        flagged = [g for g in state.get("guardrails", []) if g["stage"] == "output" and g["verdict"] == "flagged"]
        print(f"  run {tid}: output check {'FLAGGED ' + str(flagged[0]['evidence']) if flagged else 'pass'}")
        if flagged:
            found = tid
            break
    if not found:
        sys.exit("  no flagged reply in six runs this time")
    telemetry.flush()
    time.sleep(2)
    trace = next(t for t in httpx.get(f"{PLATFORM}/traces/search", params={"q": found}).json())
    print("\n=== 2. Promote it to a case: the message and order from the platform, the expectation from a reviewer ===")
    case = curate.promote(PLATFORM, trace["trace_id"], arabic["expect"], {"currency": "USD"},
                          note="reply named riyals for a dollar refund", case_id="curated-arabic-currency")
    print(f"  {case['id']}: \"{case['message']}\" · expect {case['expect']} · reply in USD · from trace {case['source_trace'][:12]}…")
    print("\n=== 3. The new case on compose v1 (today) and v2 (the fix), five times each ===")
    from agentkit import judgments
    from agent.experiments import COMPOSE_V2
    for label, prompt in (("v1", judgments.COMPOSE), ("v2", COMPOSE_V2)):
        fails = 0
        for k in range(5):
            r = Gemini().stream(prompt, json.dumps({"customer_message": case["message"],
                                                    "decision": {"action": "refund", "amount": 55, "clause": "§1"}},
                                                   ensure_ascii=False), lambda piece: True)
            problems = curate.reply_problems(case, r["text"])
            fails += bool(problems)
        print(f"  compose {label}: {fails}/5 replies fail the case")


def timelines():
    from agentkit import judgments
    from agentkit.cases import CASES
    from agentkit.llm import Gemini
    from evals import curate
    from evals.checks import timelines_from_faq
    from services.data import FAQ
    labelled = httpx.get(f"{PLATFORM}/annotations/export", params={"annotator": "reference"}).json()
    print("=== 1. The timeline check on stage 17's 30 labelled replies ===")
    counts = collections.Counter()
    for item in labelled:
        found = timelines_from_faq(item["snapshot"]["reply"])
        counts[("grounded" if item["labels"]["reply_grounded"] else "not grounded",
                "flagged" if found and not found[0] else "passed")] += 1
    for (label, verdict), n in sorted(counts.items()):
        print(f"  labelled {label:<13} check {verdict:<8} {n}")
    print("\n=== 2. One of them as a curated case: the message from the snapshot, the order from the platform ===")
    item = next(i for i in labelled if i["run_id"] == "lab-c9b34f")
    expect = next(c for c in CASES if c["id"] == "missing-dessert")["expect"]
    case = curate.from_annotation(PLATFORM, item, expect, {"currency": "USD", "timelines": "faq"},
                                  note="reply said 3 to 5 business days; FAQ F5 says 5 to 10 working days",
                                  case_id="curated-refund-timeline")
    print(f"  {case['id']}: \"{case['message']}\" · expect {case['expect']} · timelines from the FAQ · "
          f"from labelled run {case['source_annotation']}")
    print("\n=== 3. The case on compose v2 (no FAQ) and v3 (the FAQ entry for the decision), five times each ===")
    decision = {k: item["snapshot"]["decision"][k] for k in ("action", "amount", "clause")}
    for label, prompt, faq in (("v2", judgments.COMPOSE_V2, None), ("v3", judgments.COMPOSE, judgments.faq_for(decision, FAQ))):
        fails, said = 0, []
        for k in range(5):
            r = judgments.compose(Gemini(), case["message"], decision, lambda piece: True, prompt, faq)
            problems = curate.reply_problems(case, r["text"])
            fails += bool(problems)
            said += problems
        print(f"  compose {label}: {fails}/5 replies fail the case" + (f" ({collections.Counter(said).most_common(2)})" if said else ""))


if __name__ == "__main__":
    httpx.get(f"{PLATFORM}/health")
    curated() if "--curate" in sys.argv else timelines() if "--timelines" in sys.argv else online()
