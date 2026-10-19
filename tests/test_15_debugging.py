import json

import httpx

from agent.graph import build
from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.telemetry import span_to_dict
from fakes import ScriptedStructured
from judges import answers

GRILL = next(c for c in CASES if c["id"] == "late-grill")


def post_trace(platform, spans, run_id):
    tid = next(s for s in spans.get_finished_spans() if s.attributes.get("refund_agent.run_id") == run_id).context.trace_id
    mine = [span_to_dict(s) for s in spans.get_finished_spans() if s.context.trace_id == tid]
    httpx.post(f"{platform}/v1/spans", json=mine)
    return format(tid, "032x"), mine


def test_replay_from_a_checkpoint_is_a_new_linked_trace_and_never_pays_twice(platform, tmp_path, spans, monkeypatch):
    spans.clear()
    p = Platform(platform)
    model = ScriptedStructured(answers(order_id="A-9310", amount=72))
    monkeypatch.setattr(model, "REPLY", ["We refunded ", "72 riyals."])          # the bug: wrong currency
    g = build(model, p, db=tmp_path / "lg.db")
    config = {"configurable": {"thread_id": "dbg1"}}
    first = g.invoke({"request": GRILL}, config)
    assert "correction" in first["reply"]
    monkeypatch.setattr(model, "REPLY", ["We refunded ", "$72.00."])              # the fix (a new compose prompt)
    again = g.replay(config, before="reply")
    assert "correction" not in again["reply"] and again["reply"]["text"].endswith("$72.00.")
    assert p.payments("A-9310")["count"] == 1                                     # the refund wasn't repeated
    roots = [s for s in spans.get_finished_spans() if s.name.startswith("invoke_workflow")]
    original = next(s for s in roots if not s.attributes.get("refund_agent.replay"))
    replayed = next(s for s in roots if s.attributes.get("refund_agent.replay"))
    assert replayed.name.endswith("(replay)") and replayed.context.trace_id != original.context.trace_id
    assert replayed.links[0].context.trace_id == original.context.trace_id
    assert "before reply" in replayed.links[0].attributes["link.reason"]
    replayed_nodes = {s.name for s in spans.get_finished_spans() if s.context.trace_id == replayed.context.trace_id}
    assert "node reply" in replayed_nodes and "node classify" not in replayed_nodes    # only what came after ran again


def test_search_finds_runs_by_an_attribute_on_any_span(platform, tmp_path, spans, monkeypatch):
    spans.clear()
    httpx.post(f"{platform}/admin/clear-traces")
    model = ScriptedStructured(answers(order_id="A-9310", amount=72))
    monkeypatch.setattr(model, "REPLY", ["We refunded ", "72 riyals."])
    g = build(model, Platform(platform), db=tmp_path / "lg.db")
    g.invoke({"request": GRILL}, {"configurable": {"thread_id": "dbg2"}})
    tid, _ = post_trace(platform, spans, "dbg2")
    found = httpx.get(f"{platform}/traces/search", params={"attr": "guardrail.verdict=flagged"}).json()
    assert [r["trace_id"] for r in found] == [tid]
    # Message bodies are off, yet the word is findable: the output guardrail's evidence quotes it.
    hits = httpx.get(f"{platform}/traces/search", params={"q": "riyal"}).json()
    assert [r["trace_id"] for r in hits] == [tid]


def test_compare_lines_up_spans_and_shows_what_changed(platform):
    def trace(tid, amount, tokens):
        root_id, child_id = tid[:15] + "1", tid[:15] + "2"
        return [{"trace_id": tid, "span_id": root_id, "parent_id": None, "name": "invoke_workflow refund_agent",
                 "kind": "INTERNAL", "start": 0, "end": 10, "status": "UNSET", "status_message": "", "events": [], "links": [],
                 "resource": {}, "attributes": {"refund_agent.amount": amount}},
                {"trace_id": tid, "span_id": child_id, "parent_id": root_id, "name": "chat m", "kind": "CLIENT", "start": 1,
                 "end": 4, "status": "UNSET", "status_message": "", "events": [], "links": [], "resource": {},
                 "attributes": {"gen_ai.usage.output_tokens": tokens, "gen_ai.usage.cost": 0.001}}]
    a, b = "a" * 32, "b" * 32
    httpx.post(f"{platform}/v1/spans", json=trace(a, 72, 100) + trace(b, 72, 40))
    c = httpx.get(f"{platform}/traces/compare", params={"a": a, "b": b}).json()
    chat = next(r for r in c["rows"] if r["path"].endswith("chat m"))
    assert chat["diffs"] == {"gen_ai.usage.output_tokens": [100, 40]}
    root = next(r for r in c["rows"] if r["path"] == "invoke_workflow refund_agent")
    assert root["diffs"] == {}


def test_restore_brings_archived_spans_back_into_the_viewer(platform, tmp_path):
    s = {"trace_id": "c" * 32, "span_id": "1" * 16, "parent_id": None, "name": "archived", "kind": "INTERNAL",
         "start": 1.0, "end": 2.0, "status": "UNSET", "status_message": "", "attributes": {}, "events": [], "links": [],
         "resource": {}}
    f = tmp_path / "spans.jsonl"
    f.write_text(json.dumps(s) + "\n")
    assert httpx.post(f"{platform}/admin/restore", params={"path": str(f)}).json() == {"restored": 1, "traces": 1}
    assert httpx.get(f"{platform}/traces/{'c' * 32}").json()["root"] == "archived"
