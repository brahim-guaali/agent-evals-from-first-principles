from evals import checks
from evals.checks import evaluate


def sp(sid, name, parent=None, status="UNSET", start=0.0, end=1.0, **attrs):
    return {"span_id": sid, "parent_id": parent, "name": name, "status": status, "start": start, "end": end,
            "attributes": attrs, "events": []}


def run(*extra, end=20.0, **root_attrs):
    return [sp("r", "invoke_workflow refund_agent", end=end, **root_attrs), *extra]


def test_a_clean_run_passes_everything_that_applies():
    spans = run(sp("d", "node decide", "r"),
                sp("c", "chat m", "d", **{"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10,
                                          "gen_ai.usage.cost": 0.0004}),
                sp("p", "POST /payments", "r"))
    res = evaluate(spans, {"cost": 0.0004})
    assert all(r["passed"] for r in res.values()) and "no_payment_after_a_failed_lookup" not in res


def test_money_after_a_failed_lookup_and_a_second_payment_both_fail():
    spans = run(sp("t", "execute_tool get_order", "r", status="ERROR", **{"gen_ai.operation.name": "execute_tool"}),
                sp("p1", "POST /payments", "r"), sp("p2", "POST /payments", "r"))
    res = evaluate(spans)
    assert res["no_payment_after_a_failed_lookup"]["passed"] is False and res["at_most_one_payment"]["passed"] is False


def test_a_spend_the_trace_cant_account_for_fails():
    spans = run(sp("c", "chat m", "r", **{"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 1}))
    assert evaluate(spans, {"cost": 0.002})["spend_matches_record"]["passed"] is False


def test_a_paused_run_is_not_judged_on_latency():
    assert "within_latency_slo" not in evaluate(run(end=500.0, **{"refund_agent.paused": True}))
    assert evaluate(run(end=500.0))["within_latency_slo"]["passed"] is False


def test_results_land_in_the_judged_trace_as_genai_evaluation_events(spans):
    from evals.emit import emit
    spans.clear()
    emit("ab" * 16, "cd" * 8, {"verify_loop_bounded": {"passed": False, "explanation": "decide ran 4 times"}})
    s = spans.get_finished_spans()[0]
    assert format(s.context.trace_id, "032x") == "ab" * 16 and format(s.parent.span_id, "016x") == "cd" * 8
    ev = s.events[0]
    assert ev.name == "gen_ai.evaluation.result" and ev.attributes["gen_ai.evaluation.score.label"] == "fail"
    assert s.status.status_code.name == "ERROR"


def test_every_check_is_listed():
    assert len(checks.CHECKS) == 9


def test_the_gates_oracle_has_an_answer_for_every_case():
    from agentkit.cases import CASES
    from evals.gate import oracle
    for c in CASES:
        answer, fraud = oracle(c)
        d = answer("Decision", "")
        assert d["action"] in ("refund", "credit", "refuse") and fraud
