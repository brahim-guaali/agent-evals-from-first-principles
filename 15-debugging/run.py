"""Stage 15: debugging. Find a bad run, replay it from its checkpoint with a fix, compare.

    make platform ; make collector
    python 15-debugging/run.py
"""
import sys
import time
import uuid

import httpx

from agent.graph import build
from agentkit import judgments, telemetry
from agentkit.cases import CASES
from agentkit.llm import Gemini
from agentkit.platform import Platform
from agentkit.trace import NullTracer

sys.stdout.reconfigure(line_buffering=True)
PLATFORM = "http://127.0.0.1:8710"
telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")
ARABIC = next(c for c in CASES if c["id"] == "arabic-late")
FIX = ("\nAmounts are always in US dollars: write them with a $ sign, whatever the customer's language. "
       "Never name another currency.")


def wait_for(trace_filter, tries=20):
    for _ in range(tries):
        telemetry.flush()
        time.sleep(1.5)
        found = httpx.get(f"{PLATFORM}/traces/search", params=trace_filter).json()
        if found:
            return found
    return []


if __name__ == "__main__":
    platform = Platform()
    platform.check()
    httpx.post(f"{PLATFORM}/admin/clear-traces")
    llm = Gemini()
    graph = build(llm, platform, NullTracer())

    print("=== 1. Find a bad run: the Arabic reply that names the wrong currency ===")
    bad = None
    for attempt in range(1, 6):
        platform.reset()
        config = {"configurable": {"thread_id": "dbg-" + uuid.uuid4().hex[:6]}}
        state = graph.invoke({"request": ARABIC}, config)
        flagged = [g for g in state.get("guardrails", []) if g["stage"] == "output" and g["verdict"] == "flagged"]
        print(f"  attempt {attempt}: run {config['configurable']['thread_id']} · output check "
              f"{'FLAGGED ' + str(flagged[0]['evidence']) if flagged else 'pass'}")
        if flagged:
            bad = config
            break
    if not bad:
        sys.exit("  the model said dollars five times in a row; nothing to debug this time")
    hits = wait_for({"attr": "guardrail.verdict=flagged"})
    print(f"  search attr guardrail.verdict=flagged → {len(hits)} run(s): {[h['run_id'] for h in hits]}")
    original = hits[0]

    print("\n=== 2. Replay it from the checkpoint before `reply`, with the compose prompt fixed ===")
    before_prompt = judgments.COMPOSE
    judgments.COMPOSE = judgments.COMPOSE + FIX
    replayed = graph.replay(bad, before="reply", why="compose prompt v2 (dollars only)")
    judgments.COMPOSE = before_prompt
    out = [g for g in replayed.get("guardrails", []) if g["stage"] == "output"]
    print(f"  replayed reply: \"{replayed['reply']['text'][:160]}…\"")
    print(f"  output check now: {out[-1]['verdict'] if out else '?'} · payments for A-9880: "
          f"{platform.payments('A-9880')['count']} (the replay reached the refund node: idempotent)")
    hits = wait_for({"q": "(replay)"})
    replay_trace = next(h for h in hits if h["root"].endswith("(replay)"))

    print("\n=== 3. Compare the two runs, span by span ===")
    c = httpx.get(f"{PLATFORM}/traces/compare", params={"a": original["trace_id"], "b": replay_trace["trace_id"]}).json()
    print(f"  duration {c['duration'][0]:.1f} s → {c['duration'][1]:.1f} s · model cost ${c['cost'][0]:.5f} → ${c['cost'][1]:.5f}")
    for r in c["rows"]:
        if r["diffs"] or r["a"] is None or r["b"] is None:
            side = " (only in A)" if r["b"] is None else " (only in B)" if r["a"] is None else ""
            print(f"  {r['path'].split('/', 1)[-1][:60]:<60}{side} {r['diffs'] if r['diffs'] else ''}")
    print(f"\n  open http://127.0.0.1:8710/ui/compare?a={original['trace_id']}&b={replay_trace['trace_id']}")

    print("\n=== 4. And the slowest runs, for the next investigation ===")
    for r in httpx.get(f"{PLATFORM}/traces/search", params={"sort": "slowest", "limit": 3}).json():
        print(f"  {r['duration']:6.1f} s  {r['root']}  {r['run_id']}  {r['action']} ${r['amount'] or 0:g}")
    telemetry.flush()
