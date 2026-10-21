"""From a bad production run to a new eval case (stage 20).

A trace says *which* run went wrong; the platform's own records say what the customer wrote
(traces don't carry message bodies) and what the order was. A curated case keeps all of it,
the order included, so it can be run again exactly, long after the traffic that produced it,
and an expectation a reviewer sets: the decision the policy requires, and checks on the reply.
"""
import json
from pathlib import Path

import httpx

CURATED = Path(__file__).resolve().parent / "curated.jsonl"


def promote(platform, trace_id, expect, expect_reply=None, note="", case_id=None, path=CURATED):
    trace = httpx.get(f"{platform}/traces/{trace_id}").json()
    root = next(s for s in trace["spans"] if not s["parent_id"])
    session = root["attributes"]["gen_ai.conversation.id"]
    turns = httpx.get(f"{platform}/sessions/{session}").json()["turns"]
    message = next(t["text"] for t in turns if t["role"] == "customer")
    customer = root["attributes"].get("refund_agent.customer")
    order_id = next((w.strip(".,:") for w in message.split() if w.upper().startswith("A-")), None)
    order = httpx.get(f"{platform}/orders/{order_id.upper()}").json() if order_id else None
    case = {"id": case_id or f"curated-{trace_id[:8]}", "customer": customer, "message": message, "expect": expect,
            "expect_reply": expect_reply or {}, "order": order, "source_trace": trace_id, "note": note}
    with Path(path).open("a") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return case


def from_annotation(platform, item, expect, expect_reply=None, note="", case_id=None, path=CURATED):
    """A labelled run (stage 17) as a case: the message from its snapshot, the order and the
    customer from the platform's records."""
    s = item["snapshot"]
    order = httpx.get(f"{platform}/orders/{s['order']['order_id']}").json()
    case = {"id": case_id or f"curated-{item['run_id']}", "customer": order["customer"], "message": s["message"],
            "expect": expect, "expect_reply": expect_reply or {}, "order": order,
            "source_annotation": item["run_id"], "note": note}
    with Path(path).open("a") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return case


def load(path=CURATED):
    p = Path(path)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def reply_problems(case, reply_text):
    """Checks a curated case can set on the reply: currency, timelines only from the FAQ, words
    that must appear."""
    out, exp, text = [], case.get("expect_reply") or {}, reply_text or ""
    from agent.guardrails import OTHER_CURRENCIES
    import re
    if exp.get("currency") == "USD" and re.search(OTHER_CURRENCIES, text, re.IGNORECASE):
        out.append("names a currency other than US dollars")
    if exp.get("timelines") == "faq":
        from evals.checks import timelines_from_faq
        found = timelines_from_faq(text)
        if found and not found[0]:
            out.append(f"names a time {found[1]}")
    for word in exp.get("must_include", []):
        if word not in text:
            out.append(f"doesn't include {word!r}")
    return out
