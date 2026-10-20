from langgraph.types import Command

from agent.graph import build
from agentkit.cases import CASES, REFUND
from agentkit.platform import Platform
from fakes import ScriptedStructured, finish
from judges import answers


def case(cid):
    return next(c for c in CASES if c["id"] == cid)


def trace_of(spans, run_id):
    root = next(s for s in spans.get_finished_spans() if s.attributes.get("refund_agent.run_id") == run_id)
    tid = root.context.trace_id
    return root, [s for s in spans.get_finished_spans() if s.context.trace_id == tid]


def parent_name(span, all_spans):
    by_id = {s.context.span_id: s for s in all_spans}
    return by_id[span.parent.span_id].name if span.parent else None


def test_one_run_is_one_trace_with_the_genai_shape(platform, tmp_path, spans):
    spans.clear()
    model = ScriptedStructured(answers(order_id="A-9940", amount=12, clause="§2"),
                               {"fraud analyst": [[("get_devices", {"customer": "C-4321"})], finish(risk="low", evidence=[])],
                                "refund policy": []})
    g = build(model, Platform(platform), db=tmp_path / "lg.db")
    g.invoke({"request": case("grey-shared-device")}, {"configurable": {"thread_id": "sp1"}})
    root, tr = trace_of(spans, "sp1")
    names = [s.name for s in tr]
    assert root.name == "invoke_workflow refund_agent" and root.parent is None
    assert root.attributes["gen_ai.operation.name"] == "invoke_workflow"
    assert {"node classify", "node history", "retrieval customer_history", "execute_tool get_order",
            "invoke_agent fraud_review", "execute_tool get_devices", "guardrail prompt_injection"} <= set(names)
    review = next(s for s in tr if s.name == "invoke_agent fraud_review")
    assert parent_name(review, tr) == "node fraud_review"
    tool = next(s for s in tr if s.name == "execute_tool get_devices")
    assert parent_name(tool, tr) == "invoke_agent fraud_review"
    http = next(s for s in tr if s.name == "GET /customers/{id}/devices")
    assert parent_name(http, tr) == "execute_tool get_devices" and http.attributes["http.response.status_code"] == 200
    assert all(s.attributes.get("gen_ai.conversation.id") == "sp1" for s in tr
               if s.attributes.get("gen_ai.operation.name") in ("retrieval", "invoke_agent"))


def test_a_failed_lookup_is_an_error_span_with_its_retries_underneath(platform, tmp_path, spans):
    spans.clear()
    Platform(platform).inject_faults(**{"orders:500": 5})
    g = build(ScriptedStructured(answers(order_id="A-9310", amount=72)), Platform(platform), db=tmp_path / "lg.db")
    g.invoke({"request": case("late-grill")}, {"configurable": {"thread_id": "sp2"}})
    root, tr = trace_of(spans, "sp2")
    tool = next(s for s in tr if s.name == "execute_tool get_order")
    attempts = [s for s in tr if s.name == "GET /orders/{id}"]
    assert tool.status.status_code.name == "ERROR" and tool.attributes["error.type"] == "http_500"
    assert len(attempts) == 3 and all(parent_name(a, tr) == "execute_tool get_order" for a in attempts)
    assert root.status.status_code.name == "ERROR"


def test_the_resumed_run_is_a_new_trace_linked_to_the_one_that_paused(platform, tmp_path, spans):
    spans.clear()
    g = build(ScriptedStructured(answers()), Platform(platform), db=tmp_path / "lg.db")
    config = {"configurable": {"thread_id": "sp3"}}
    g.invoke({"request": REFUND}, config)
    first = next(s for s in spans.get_finished_spans() if s.name == "invoke_workflow refund_agent")
    assert first.attributes["refund_agent.paused"] is True
    g2 = build(ScriptedStructured(answers()), Platform(platform), db=tmp_path / "lg.db")   # "another process"
    g2.invoke(Command(resume="approved"), config)
    resumed = next(s for s in spans.get_finished_spans() if s.name == "invoke_workflow refund_agent (resumed)")
    assert resumed.context.trace_id != first.context.trace_id
    assert resumed.links[0].context.trace_id == first.context.trace_id


def test_tracing_off_never_breaks_a_model_call(monkeypatch):
    """Found in stage 22: with no tracer recording, the rate limiter's bookkeeping crashed the run."""
    from opentelemetry.trace import INVALID_SPAN
    from agentkit import llm, telemetry
    monkeypatch.setattr(telemetry, "current", lambda: INVALID_SPAN)
    monkeypatch.setattr(llm._limiter, "wait", lambda: 1.0)
    g = llm.Gemini.__new__(llm.Gemini)
    g.models, g.usage = ["m"], llm.Usage()
    assert g._with_retries(lambda model: "answer") == "answer"
