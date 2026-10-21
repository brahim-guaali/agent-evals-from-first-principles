from agent.experiments import COMPOSE_TEST, COMPOSE_V2, Experiment
from agent.graph import build
from agentkit import abtest
from agentkit.cases import CASES
from agentkit.platform import Platform
from fakes import ScriptedStructured
from judges import answers


def test_assignment_is_sticky_and_roughly_even():
    arms = [COMPOSE_TEST.arm_for(f"C-{i}") for i in range(2000)]
    assert 0.45 < arms.count("A") / len(arms) < 0.55
    assert all(COMPOSE_TEST.arm_for("C-42") == COMPOSE_TEST.arm_for("C-42") for _ in range(5))
    uneven = Experiment("x", {"A": {}, "B": {}}, split={"A": 0.9, "B": 0.1})
    assert sum(uneven.arm_for(f"C-{i}") == "B" for i in range(2000)) < 300


def test_the_run_uses_its_arms_prompt_and_says_so(platform, tmp_path, spans):
    spans.clear()
    model = ScriptedStructured(answers(order_id="A-9310", amount=72))
    g = build(model, Platform(platform), db=tmp_path / "lg.db")
    grill = next(c for c in CASES if c["id"] == "late-grill")
    g.invoke({"request": grill, "prompt_versions": {"compose": "v2"}, "experiment": {"name": "compose-v2", "arm": "B"}},
             {"configurable": {"thread_id": "ab1"}})
    assert model.stream_systems[-1] == COMPOSE_V2
    root = next(s for s in spans.get_finished_spans() if s.attributes.get("refund_agent.run_id") == "ab1")
    assert root.attributes["refund_agent.arm"] == "B" and root.attributes["gen_ai.prompt.version"] == "v2"


def test_a_real_difference_and_noise_are_told_apart():
    assert abtest.z_test(30, 100, 5, 100) < 0.001          # 30% vs 5%: real
    assert abtest.z_test(10, 100, 12, 100) > 0.5           # 10% vs 12%: noise
    lo, hi = abtest.bootstrap_diff([1.0] * 50, [2.0] * 50)
    assert lo > 0.9 and hi < 1.1


def test_a_pinned_v1_arm_still_gets_v1_now_that_v3_is_the_default(platform, tmp_path):
    from agentkit import judgments
    model = ScriptedStructured(answers(order_id="A-9310", amount=72))
    g = build(model, Platform(platform), db=tmp_path / "lg.db")
    grill = next(c for c in CASES if c["id"] == "late-grill")
    g.invoke({"request": grill, "prompt_versions": {"compose": "v1"}}, {"configurable": {"thread_id": "ab2"}})
    assert model.stream_systems == [judgments.COMPOSE_V1]
    from agent.experiments import text
    assert text("compose", {}) == judgments.COMPOSE == text("compose", {"compose": "v3"})
