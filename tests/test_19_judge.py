import pytest

from evals import judge
from fakes import ScriptedStructured


def test_kappa_is_one_for_perfect_agreement_and_zero_for_chance():
    assert judge.kappa([(True, True), (False, False)] * 5) == 1.0
    assert judge.kappa([(True, True), (True, False), (False, True), (False, False)]) == pytest.approx(0.0)


def test_calibration_counts_agreement_and_the_kinds_of_disagreement():
    h = {"decision_correct": True, "reply_grounded": False, "amount_currency_right": True, "tone": "ok"}
    j = {"decision_correct": True, "reply_grounded": True, "amount_currency_right": True, "tone": "ok"}
    c = judge.calibrate([(h, j), (h, h)])
    assert c["decision_correct"]["agreement"] == 1.0
    assert c["reply_grounded"]["confusion"]["label no, judge yes"] == 1 and c["reply_grounded"]["agreement"] == 0.5
    assert c["tone"]["agreement"] == 1.0


def test_a_judge_that_always_picks_the_first_reply_flips_every_time():
    biased = ScriptedStructured(lambda schema, prompt: {"reasoning": "first looks fine", "better": "first"})
    r = judge.position_bias(biased, [({"run_id": "x", "message": "m", "decision": {}}, "reply a", "reply b")] * 4)
    assert r["flip_rate"] == 1.0 and r["first_position_wins"] == 1.0


def test_the_judge_answers_the_label_questions_from_a_snapshot():
    scripted = ScriptedStructured(lambda schema, prompt: {"reasoning": "checked §1", "decision_correct": True,
                                                         "reply_grounded": "riyal" not in prompt,
                                                         "amount_currency_right": "riyal" not in prompt, "tone": "good"})
    v = judge.judge(scripted, {"message": "late", "reply": "We refunded 55 riyal", "decision": {"amount": 55}})
    assert v.decision_correct and not v.amount_currency_right
