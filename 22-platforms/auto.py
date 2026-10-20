"""Stage 22: our hand-written spans next to auto-instrumentation, on the same run.

    make platform
    python 22-platforms/auto.py              # runs the grey-zone case twice, once each way, in two processes
    python 22-platforms/auto.py --report     # the comparison again, from the traces already in the viewer

Auto-instrumentation here is OpenInference (what Phoenix uses): `openinference-instrumentation-
langchain` covers LangGraph, `openinference-instrumentation-google-genai` covers the Gemini SDK.
No code changes in the agent: they patch the libraries.
"""
import collections
import json
import subprocess
import sys
import time
import uuid

import httpx

PLATFORM = "http://127.0.0.1:8710"


def run(mode, run_id):
    from agent.graph import build
    from agentkit import telemetry
    from agentkit.cases import CASES
    from agentkit.llm import Gemini
    from agentkit.platform import Platform
    from agentkit.trace import NullTracer
    if mode == "hand":
        telemetry.setup(environment="staging")          # our spans, to the viewer
    else:                                               # no hand spans: only the libraries' own
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        from openinference.instrumentation.langchain import LangChainInstrumentor
        provider = TracerProvider(resource=Resource.create({"service.name": "refund-agent (auto)"}))
        provider.add_span_processor(BatchSpanProcessor(telemetry.ViewerExporter(), schedule_delay_millis=500))
        LangChainInstrumentor().instrument(tracer_provider=provider)
        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
    platform = Platform()
    platform.reset()
    case = next(c for c in CASES if c["id"] == "grey-shared-device")
    build(Gemini(), platform, NullTracer()).invoke({"request": case}, {"configurable": {"thread_id": run_id}})
    telemetry.flush()
    if mode == "auto":
        provider.force_flush(5000)
    time.sleep(1)


if __name__ == "__main__":
    if sys.argv[1:2] in (["hand"], ["auto"]):
        run(sys.argv[1], sys.argv[2])
        sys.exit()
    if sys.argv[1:2] != ["--report"]:
        httpx.post(f"{PLATFORM}/admin/clear-traces")
        for mode in ("hand", "auto"):
            subprocess.run([sys.executable, __file__, mode, f"{mode}-" + uuid.uuid4().hex[:6]], check=True, timeout=600)
    traces = httpx.get(f"{PLATFORM}/traces", params={"limit": 50}).json()
    full = {t["trace_id"]: httpx.get(f"{PLATFORM}/traces/{t['trace_id']}").json() for t in traces}
    hand = next(t for t, v in full.items() if any(s["attributes"].get("refund_agent.run_id", "").startswith("hand-")
                                                   for s in v["spans"]))
    auto = [t for t in full if t != hand]          # every trace the auto run produced
    print("=== The same grey-zone refund, instrumented two ways ===")
    for label, tids in (("hand-written (OTel GenAI conventions)", [hand]), ("auto (OpenInference)", auto)):
        spans = [s for t in tids for s in full[t]["spans"]]
        keys = collections.Counter(k for s in spans for k in s["attributes"])
        kinds = collections.Counter(s["attributes"].get("openinference.span.kind") or s["attributes"].get(
            "gen_ai.operation.name") or ("http" if s["name"].split()[0] in ("GET", "POST") else "other") for s in spans)
        print(f"\n  {label}: {len(spans)} spans in {len(tids)} trace(s) · {dict(kinds)}")
        if len(tids) > 1:
            print("    traces: " + ", ".join(f"{full[t]['spans'][0]['name']} ({len(full[t]['spans'])})"
                                         for t in sorted(tids, key=lambda t: -len(full[t]["spans"]))))
        print("    names: " + ", ".join(sorted({s['name'] for s in spans})[:18]) + (" …" if len({s['name'] for s in spans}) > 18 else ""))
        print("    attributes most spans carry: " + ", ".join(k for k, _ in keys.most_common(12)))
    checks = [("model calls with token counts", lambda s: s["attributes"].get("gen_ai.usage.input_tokens") or
               s["attributes"].get("llm.token_count.prompt")),
              ("the decision (action, amount, clause)", lambda s: s["attributes"].get("refund_agent.action")),
              ("sub-agent as an agent span", lambda s: s["attributes"].get("gen_ai.operation.name") == "invoke_agent"
               or s["attributes"].get("openinference.span.kind") == "AGENT"),
              ("platform HTTP calls", lambda s: s["name"].split()[0] in ("GET", "POST")),
              ("guardrail verdicts", lambda s: s["attributes"].get("guardrail.verdict")),
              ("retries, quota hits, fallbacks", lambda s: any(e["name"].startswith("gen_ai.") for e in s["events"])),
              ("cost per call", lambda s: s["attributes"].get("gen_ai.usage.cost")),
              ("prompt and response text", lambda s: s["attributes"].get("input.value") or s["attributes"].get("gen_ai.input.messages")),
              ("the conversation (session id)", lambda s: s["attributes"].get("gen_ai.conversation.id") or s["attributes"].get("session.id"))]
    print(f"\n  {'':<40}{'hand':>8}{'auto':>8}   (spans that carry it)")
    for label, test in checks:
        h = sum(1 for s in full[hand]["spans"] if test(s))
        a = sum(1 for t in auto for s in full[t]["spans"] if test(s))
        print(f"  {label:<40}{h:>8}{a:>8}")
    (__import__("pathlib").Path(__file__).resolve().parent / "auto-vs-hand.json").write_text(
        json.dumps({"hand": hand, "auto": auto}))
    root = max(auto, key=lambda t: len(full[t]["spans"]))
    print(f"\n  the auto run's model calls in its LangGraph trace: "
          f"{sum(1 for s in full[root]['spans'] if s['attributes'].get('openinference.span.kind') == 'LLM')} "
          f"of {sum(1 for t in auto for s in full[t]['spans'] if s['attributes'].get('openinference.span.kind') == 'LLM')}")
