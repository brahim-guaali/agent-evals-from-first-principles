"""Stage 09: spans. Every run is a trace; every step, model call, tool call and hand-off a span.

    make platform                                 # in another terminal
    python 09-spans/run.py                        # all scenes, then open http://127.0.0.1:8710/ui/traces
    python 09-spans/run.py grey                   # or one: grey | wait | where
    python 09-spans/run.py resume <run id>        # (used by the wait scene: the second process)
"""
import subprocess
import sys
import time
import uuid
from collections import defaultdict

from langgraph.types import Command

from agent.graph import build
from agentkit import telemetry
from agentkit.cases import CASES, REFUND
from agentkit.llm import Gemini
from agentkit.platform import Platform
from agentkit.runner import run_case, summary
from agentkit.trace import NullTracer

sys.stdout.reconfigure(line_buffering=True)
STAGE = "09 · spans"
memory = telemetry.InMemorySpanExporter()
telemetry.setup([telemetry.ViewerExporter(), memory])


def case(cid):
    return next(c for c in CASES if c["id"] == cid)


def trace_spans(run_id):
    telemetry.flush()
    spans = memory.get_finished_spans()
    root = next(s for s in spans if s.attributes.get("refund_agent.run_id") == run_id and not s.parent)
    return root, [s for s in spans if s.context.trace_id == root.context.trace_id]


def tree(root, spans, show=print, width=72):
    kids = defaultdict(list)
    for s in spans:
        if s.parent:
            kids[s.parent.span_id].append(s)
    t0 = root.start_time

    def walk(s, depth):
        dur = (s.end_time - s.start_time) / 1e9
        extra = []
        a = s.attributes
        if a.get("gen_ai.usage.input_tokens") is not None:
            extra.append(f"{a['gen_ai.usage.input_tokens']}+{a.get('gen_ai.usage.output_tokens', 0)} tokens")
        if a.get("rate_limiter.wait_s"):
            extra.append(f"waited {a['rate_limiter.wait_s']:.1f} s for our rate limiter")
        if a.get("gen_ai.response.time_to_first_chunk"):
            extra.append(f"first chunk {a['gen_ai.response.time_to_first_chunk']:.2f} s")
        if s.status.status_code.name == "ERROR":
            extra.append(f"ERROR {a.get('error.type')}")
        for e in s.events:
            if e.name.startswith("gen_ai.") or e.name in ("waiting_for_person", "stream.aborted"):
                extra.append(e.name.replace("gen_ai.", ""))
        label = ("  " * depth + s.name)[:44]
        show(f"  {(s.start_time - t0) / 1e9:6.2f}s {label:<44} {dur:6.2f}s  {'; '.join(extra)}"[:160])
        for k in sorted(kids[s.context.span_id], key=lambda x: x.start_time):
            walk(k, depth + 1)
    walk(root, 0)


def breakdown(root, spans):
    """Self time: each span's duration minus its children's, summed by what kind of work it is."""
    kids = defaultdict(list)
    for s in spans:
        if s.parent:
            kids[s.parent.span_id].append(s)
    buckets = defaultdict(float)
    for s in spans:
        dur = (s.end_time - s.start_time) / 1e9
        child = sum((k.end_time - k.start_time) / 1e9 for k in kids[s.context.span_id])
        own = max(0.0, dur - child)
        a, op = s.attributes, s.attributes.get("gen_ai.operation.name")
        if op == "chat":
            wait = a.get("rate_limiter.wait_s", 0)
            backoff = sum(e.attributes.get("wait", 0) for e in s.events)
            buckets["waiting for our rate limiter"] += wait
            buckets["back-off after 429/503"] += backoff
            buckets["the model answering"] += max(0.0, own - wait - backoff)
        elif s.name.startswith(("GET ", "POST ")):
            buckets["platform HTTP calls"] += own
        else:
            buckets["our own code, between calls"] += own
    return buckets


def where_the_time_went(root, spans, show=print):
    buckets, total = breakdown(root, spans), (root.end_time - root.start_time) / 1e9
    show(f"  where {total:.1f} s went (self time; parallel steps overlap, so the parts can add up to more):")
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        show(f"    {k:<32} {v:6.1f} s  {'█' * int(v / max(total, 1) * 40)}")


def grey(platform):
    print("\n=== 1. A grey-zone refund: the sub-agent's calls nest under the step that called it ===")
    r = run_case(case("grey-shared-device"), platform, STAGE, prefix="sp")
    root, spans = trace_spans(r["run_id"])
    print(f"  run {r['run_id']} · {summary(r['state'])} · trace {format(root.context.trace_id, '032x')} · {len(spans)} spans")
    tree(root, spans)
    where_the_time_went(root, spans)


def wait(platform):
    print("\n=== 2. $640: the run waits for the manager; another process resumes it ===")
    run_id = "sp-" + uuid.uuid4().hex[:6]
    graph = build(Gemini(), platform, NullTracer())
    config = {"configurable": {"thread_id": run_id}}
    state = graph.invoke({"request": REFUND}, config)
    root, spans = trace_spans(run_id)
    print(f"  process A: paused · trace {format(root.context.trace_id, '032x')} · {len(spans)} spans, "
          "the last one is node approval")
    platform.decide_approval(state["approval_request"]["id"], "approved")
    print("  (the manager approves; the first process is gone)")
    out = subprocess.run([sys.executable, __file__, "resume", run_id], capture_output=True, text=True, timeout=300)
    print(out.stdout.rstrip())


def resume(run_id):
    platform = Platform()
    graph = build(Gemini(), platform, NullTracer())
    state = graph.invoke(Command(resume="approved"), {"configurable": {"thread_id": run_id}})
    telemetry.flush()
    resumed = next(s for s in memory.get_finished_spans() if s.name.endswith("(resumed)"))
    link = resumed.links[0]
    print(f"  process B: resumed · new trace {format(resumed.context.trace_id, '032x')} · linked to trace "
          f"{format(link.context.trace_id, '032x')} ({link.attributes['link.reason']}) · {summary(state)}")


def where(platform):
    print("\n=== 3. Where does a run's time go? Six ordinary cases ===")
    totals, wall = defaultdict(float), 0.0
    for cid in ("late-grill", "missing-dessert", "slightly-late", "spoiled-pizza", "arabic-late", "late-office-lunch"):
        platform.reset()
        r = run_case(case(cid), platform, STAGE, prefix="sp")
        root, spans = trace_spans(r["run_id"])
        b = breakdown(root, spans)
        wall += (root.end_time - root.start_time) / 1e9
        for k, v in b.items():
            totals[k] += v
        top = max(b, key=b.get)
        print(f"  {cid:<18} {r['seconds']:5.1f} s · biggest part: {top} {b[top]:.1f} s · trace {format(root.context.trace_id, '032x')[:12]}…")
    print(f"  all six, {wall:.0f} s of runs:")
    for k, v in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<32} {v:6.1f} s  {v / wall:5.0%}  {'█' * int(v / wall * 40)}")


SCENES = {"grey": grey, "wait": wait, "where": where}

if __name__ == "__main__":
    if sys.argv[1:2] == ["resume"]:
        resume(sys.argv[2])
        sys.exit()
    platform = Platform()
    platform.check()
    for name in sys.argv[1:] or SCENES:
        platform.reset()
        SCENES[name](platform)
    telemetry.flush()
