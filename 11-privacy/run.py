"""Stage 11: privacy. What customer data reaches the traces, and how to stop it.

    make platform ; make collector                # two terminals
    python 11-privacy/run.py                      # all scenes
"""
import json
import os
import sys
import time

import httpx

from agentkit import telemetry
from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.runner import run_case, summary

sys.stdout.reconfigure(line_buffering=True)
STAGE = "11 · privacy"
COLLECTOR = "http://127.0.0.1:4318"
telemetry.setup(otlp=COLLECTOR, environment="staging")
SECRETS = ["+971 50", "055 123", "@example.com", "4242 4242", "Omar Saleh", "Marina Gate", "C-7740", "C-1280", "C-2210"]


EXTRA = {  # the made-up details customers paste into chats, one kind per request
    "late-grill": " You can reach me on +971 50 365 9376.",
    "missing-sides": " Email me at sara.haddad@example.com. I paid with card 4242 4242 4242 4242.",
    "missing-dessert": " This is Omar Saleh, deliver to Marina Gate 2, flat 1804. Call 055 123 4567.",
}


def requests():
    return [{**next(c for c in CASES if c["id"] == cid), "message": next(c for c in CASES if c["id"] == cid)["message"] + extra}
            for cid, extra in EXTRA.items()]


def leaks(trace_id, platform):
    spans = httpx.get(f"{platform.url}/traces/{trace_id}").json()["spans"]
    blob = json.dumps(spans, ensure_ascii=False)
    found = {s: blob.count(s) for s in SECRETS if s in blob}
    where = sorted({k for sp in spans for k, v in sp["attributes"].items() if any(x in str(v) for x in SECRETS)})
    return found, where, spans


def scene(title, platform, capture, processors):
    print(f"\n=== {title} ===")
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_ONLY" if capture else "NO_CONTENT"
    httpx.post(f"{COLLECTOR}/config", params={"processors": processors})
    print(f"  message bodies on spans: {'captured' if capture else 'off'} · collector processors: {processors or 'none'}")
    for req in requests():
        platform.reset()
        r = run_case(req, platform, STAGE, prefix="pv")
        telemetry.flush()
        time.sleep(1.5)                                  # one batch interval in the collector
        tid = next(t["trace_id"] for t in httpx.get(f"{platform.url}/traces").json()
                   if t["title"].startswith(r["run_id"]))
        found, where, spans = leaks(tid, platform)
        print(f"  {req['id']:<16} {summary(r['state'])} · message: \"…{req['message'][-58:]}\"")
        print(f"    in the trace: {found or 'none of the customer details'}" + (f"  (attributes: {', '.join(where)})" if where else ""))
        root = next(s for s in spans if not s["parent_id"])
        print(f"    root span refund_agent.customer = {root['attributes'].get('refund_agent.customer')}")
        if capture:
            chat = next((s for s in spans if s["attributes"].get("gen_ai.input.messages")), None)
            if chat:
                text = json.loads(chat["attributes"]["gen_ai.input.messages"])[0]["parts"][0]["content"]
                print(f"    {chat['name']} gen_ai.input.messages: \"…{text[-110:]}\"")
    s = httpx.get(f"{COLLECTOR}/stats").json()
    print("  redactions so far: " + ", ".join(f"{k.split('.')[1]} {v}" for k, v in s.items() if k.startswith("redacted.")) if
          any(k.startswith("redacted.") for k in s) else "  redactions so far: none")


def retention(platform, cutoff):
    print("\n=== 4. Retention: delete everything older than the start of scene 3 ===")
    before = len(httpx.get(f"{platform.url}/traces", params={"limit": 1000}).json())
    r = httpx.post(f"{platform.url}/admin/retention", json={"before": cutoff}).json()
    after = len(httpx.get(f"{platform.url}/traces", params={"limit": 1000}).json())
    print(f"  {before} traces before, {r['spans_deleted']} spans and {r['points_deleted']} metric points deleted, "
          f"{after} traces left (scene 3's, all redacted)")
    print("  by default the platform keeps 30 days (RETENTION_DAYS) and checks every hour; the collector's "
          "archive keeps one file a day (POST /retention?days=30)")


if __name__ == "__main__":
    platform = Platform()
    platform.check()
    httpx.post(f"{platform.url}/admin/clear-traces")
    scene("1. Bodies captured, no redaction: what a trace holds", platform, capture=True, processors="")
    scene("2. Bodies captured, redaction in the collector", platform, capture=True, processors="redact")
    cutoff = time.time()
    scene("3. Bodies off (the default) and redaction on", platform, capture=False, processors="redact")
    retention(platform, cutoff)
