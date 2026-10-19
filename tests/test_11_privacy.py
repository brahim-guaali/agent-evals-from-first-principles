import json
import time

import httpx

from services.redact import pseudonym, redact_span, scrub


def test_contact_details_and_cards_are_masked_and_ordinary_numbers_kept():
    counts = {}
    out = scrub("Call +971 50 123 4567 or 055 123 4567, mail sara.k@example.com, card 4242 4242 4242 4242, "
                "order A-93100007, ref 1234567890123", counts)
    assert out == ("Call [phone] or [phone], mail [email], card [card ****4242], order A-93100007, ref 1234567890123")
    assert counts == {"email": 1, "phone": 2, "card": 1}


def test_customer_ids_become_a_stable_pseudonym():
    a, b = scrub("C-7740-0012 wrote"), scrub("again from C-7740-0012")
    assert pseudonym("C-7740-0012") in a and pseudonym("C-7740-0012") in b and "C-7740" not in a + b


def test_json_inside_attributes_is_redacted_too():
    msgs = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "email me at a@b.co, I'm C-1042"}]}])
    span = {"name": "chat m", "attributes": {"gen_ai.input.messages": msgs, "refund_agent.customer": "C-1042"},
            "events": [{"name": "x", "attributes": {"note": "+971 50 123 4567"}}], "links": [], "status_message": ""}
    out = redact_span(span)
    assert "a@b.co" not in out["attributes"]["gen_ai.input.messages"]
    assert json.loads(out["attributes"]["gen_ai.input.messages"])[0]["parts"][0]["content"].startswith("email me at [email]")
    assert out["attributes"]["refund_agent.customer"].startswith("cust_") and out["events"][0]["attributes"]["note"] == "[phone]"


def test_names_and_addresses_in_free_text_get_through():
    """The honest limit of regexes: why message bodies are opt-in."""
    assert scrub("This is Sara Khan, Marina Gate 2, flat 1804") == "This is Sara Khan, Marina Gate 2, flat 1804"


def test_retention_deletes_spans_older_than_the_cutoff(platform):
    now = time.time()
    old = {"trace_id": "a" * 32, "span_id": "1" * 16, "parent_id": None, "name": "old", "kind": "INTERNAL",
           "start": now - 40 * 86400, "end": now - 40 * 86400 + 1, "status": "OK", "status_message": "",
           "attributes": {}, "events": [], "links": [], "resource": {}}
    new = {**old, "trace_id": "b" * 32, "span_id": "2" * 16, "name": "new", "start": now - 10, "end": now - 9}
    httpx.post(f"{platform}/v1/spans", json=[old, new])
    r = httpx.post(f"{platform}/admin/retention", json={"days": 30}).json()
    assert r["spans_deleted"] == 1
    roots = [t["root"] for t in httpx.get(f"{platform}/traces").json()]
    assert "new" in roots and "old" not in roots


def test_message_bodies_are_captured_only_when_asked(monkeypatch):
    from agentkit import telemetry
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    assert telemetry.capture_content() is False
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")
    assert telemetry.capture_content() is True


def test_infrastructure_fields_are_left_alone():
    span = {"name": "GET /orders/{id}", "attributes": {"server.address": "127.0.0.1", "note": "from 10.0.0.7"},
            "events": [], "links": [], "status_message": ""}
    out = redact_span(span)
    assert out["attributes"] == {"server.address": "127.0.0.1", "note": "from [ip]"}
