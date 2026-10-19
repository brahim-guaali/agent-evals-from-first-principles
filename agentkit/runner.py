"""Run one message through the agent end to end, the way every stage's demo does.

A manager answers if the run pauses for one. `outcome()` reduces a finished run to what
the case set checks (action, amount, whether a manager was asked), so stages can score
themselves against `cases.CASES`.
"""
import os
import time
import uuid

from langgraph.types import Command

from agent.graph import build, spec
from agentkit.llm import Gemini
from agentkit.trace import Tracer


def run_case(case, platform, stage, title=None, manager="approved", prefix="r", llm=None, meta=None, budget=None):
    thread = f"{prefix}-" + uuid.uuid4().hex[:6]
    budget = budget if budget is not None else (float(os.environ["RUN_BUDGET_USD"]) if os.environ.get("RUN_BUDGET_USD") else None)
    llm = llm or Gemini(run_id=thread, budget=budget, customer=case.get("customer"))
    platform = platform.for_run(thread, budget, case.get("customer"))
    tracer = Tracer(stage, title or case.get("id", "run"), usage=llm.usage, run_id=thread)
    graph = build(llm, platform, tracer)
    tracer.start(spec(graph))
    config = {"configurable": {"thread_id": thread}}
    t0 = time.monotonic()
    state = graph.invoke({"request": case, **({"meta": meta} if meta else {})}, config)
    asked = bool(graph.get_state(config).next) and not state.get("budget_stop")
    if asked:
        platform.decide_approval(state["approval_request"]["id"], manager)
        state = graph.invoke(Command(resume=manager), config)
    tracer.finished("done", summary(state))
    return {"state": state, "asked": asked, "usage": llm.usage, "seconds": time.monotonic() - t0,
            "run_id": thread}


def summary(state):
    d, reply = state.get("decision"), state.get("reply", {})
    if reply.get("queue") == "support-escalations":
        return f"escalated: {reply['why']}"
    if reply.get("ticket") and not d:
        return f"ticket: {reply['ticket']['category']}"
    return f"{d['action']} ${d['amount']:g} under {d['clause']}" if d else str(reply)[:60]


def outcome(run):
    s = run["state"]
    d = s.get("decision") or {}
    if s.get("reply", {}).get("ticket") and not d:
        action = "handoff"
    elif s.get("payment", {}).get("replayed"):
        action = "refuse"                 # a duplicate: no money moved, the customer is told so
    elif s.get("reply", {}).get("queue") == "support-escalations":
        action = "refuse" if s.get("fraud_review", {}).get("risk") == "high" else "escalate"
    else:
        action = d.get("action")
    return {"action": action, "amount": d.get("amount", 0) if action == "refund" else 0, "approval": run["asked"]}


def score_cases(cases, platform, stage, show=print):
    right, calls, tokens, seconds = 0, 0, 0, []
    for c in cases:
        platform.reset()
        r = run_case(c, platform, stage)
        ok = outcome(r) == c["expect"]
        right += ok
        calls += r["usage"].calls
        tokens += r["usage"].input_tokens + r["usage"].output_tokens
        seconds.append(r["seconds"])
        show(f"  {'✓' if ok else '✗'} {c['id']:<24} {summary(r['state']):<36} {r['seconds']:5.1f} s"
             + ("" if ok else f"   got {outcome(r)}, expected {c['expect']}"))
    show(f"  {right}/{len(cases)} right, {calls} model calls, {tokens:,} tokens, "
         f"median {sorted(seconds)[len(seconds) // 2]:.1f} s per case")
    return right
