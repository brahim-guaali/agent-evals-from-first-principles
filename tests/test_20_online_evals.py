import httpx

from agent.graph import build
from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.telemetry import span_to_dict
from evals import curate
from evals.online import OnlineEvaluator
from fakes import ScriptedStructured
from judges import answers

ARABIC = next(c for c in CASES if c["id"] == "arabic-late")


def run_and_post(platform, spans, tmp_path, monkeypatch, tid="on1"):
    spans.clear()
    model = ScriptedStructured(answers(order_id="A-9880", amount=55))
    monkeypatch.setattr(model, "REPLY", ["تم استرداد ", "55 ريال"])
    g = build(model, Platform(platform), db=tmp_path / f"{tid}.db")
    g.invoke({"request": {**ARABIC, "session_id": tid}}, {"configurable": {"thread_id": tid}})
    root = next(s for s in spans.get_finished_spans() if s.attributes.get("refund_agent.run_id") == tid)
    mine = [span_to_dict(s) for s in spans.get_finished_spans() if s.context.trace_id == root.context.trace_id]
    httpx.post(f"{platform}/v1/spans", json=mine)
    return format(root.context.trace_id, "032x")


def test_the_online_evaluator_scores_each_finished_run_once(platform, spans, tmp_path, monkeypatch):
    httpx.post(f"{platform}/admin/clear-traces")
    httpx.post(f"{platform}/admin/clear-metrics")
    run_and_post(platform, spans, tmp_path, monkeypatch)
    ev = OnlineEvaluator(platform)
    assert ev.poll() == 1 and ev.poll() == 0
    assert ev.counts[("output_check_passed", "fail")] == 1
    points = httpx.get(f"{platform}/metrics/series", params={"name": "refund_agent.evaluations",
                                                              "group_by": "gen_ai.evaluation.score.label"}).json()
    assert any(p["group"] == "fail" and p["value"] >= 1 for p in points)


def test_a_bad_run_becomes_a_case_with_its_message_order_and_expectations(platform, spans, tmp_path, monkeypatch):
    tid = run_and_post(platform, spans, tmp_path, monkeypatch, tid="on2")
    case = curate.promote(platform, tid, ARABIC["expect"], {"currency": "USD"}, note="reply said riyal",
                          path=tmp_path / "curated.jsonl")
    assert case["message"] == ARABIC["message"] and case["order"]["order_id"] == "A-9880"
    assert case["source_trace"] == tid and curate.load(tmp_path / "curated.jsonl")[0]["id"] == case["id"]
    assert curate.reply_problems(case, "تم استرداد 55 ريال") == ["names a currency other than US dollars"]
    assert curate.reply_problems(case, "We refunded $55.00") == []
