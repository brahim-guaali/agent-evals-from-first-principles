import json

import httpx
from langgraph.types import Command

from agent.graph import build
from agentkit.annotation import queue
from agentkit.cases import CASES
from agentkit.platform import Platform
from fakes import ScriptedStructured
from judges import answers


def test_a_run_goes_into_the_queue_gets_labelled_and_comes_out_in_the_export(platform, tmp_path):
    p = Platform(platform)
    grill = next(c for c in CASES if c["id"] == "late-grill")
    g = build(ScriptedStructured(answers(order_id="A-9310", amount=72)), p, db=tmp_path / "lg.db")
    state = g.invoke({"request": grill}, {"configurable": {"thread_id": "lab1"}})
    queue(p, [({"run_id": "lab1", "state": state}, "sample", None)])
    nxt = httpx.get(f"{platform}/annotations/next", params={"annotator": "t"}).json()
    snap = nxt["item"]["snapshot"]
    assert snap["message"] == grill["message"] and snap["decision"]["amount"] == 72 and snap["order"]["total"] == 72
    bad = httpx.post(f"{platform}/annotations/labels", json={"item_id": nxt["item"]["item_id"], "annotator": "t",
                                                             "labels": {"tone": "great"}})
    assert bad.status_code == 422
    httpx.post(f"{platform}/annotations/labels", json={"item_id": nxt["item"]["item_id"], "annotator": "t", "labels": {
        "decision_correct": True, "reply_grounded": True, "amount_currency_right": True, "tone": "good"}})
    assert httpx.get(f"{platform}/annotations/next", params={"annotator": "t"}).json()["item"] is None
    out = httpx.get(f"{platform}/annotations/export", params={"annotator": "t"}).json()
    assert out[0]["labels"]["decision_correct"] is True and out[0]["run_id"] == "lab1"


def test_the_page_is_served(platform):
    assert "Label runs" in httpx.get(f"{platform}/ui/annotate").text


def test_the_snapshot_says_a_manager_approved(platform, tmp_path):
    """Stage 19: shown only the decision (§2), the judge called the frequent claimer's approved
    refund wrong. §8 needs a manager's yes, so the snapshot carries it."""
    p = Platform(platform)
    case = next(c for c in CASES if c["id"] == "frequent-claimer")
    g = build(ScriptedStructured(answers(order_id="A-9990", amount=15, clause="§2")), p, db=tmp_path / "lg.db")
    config = {"configurable": {"thread_id": "lab2"}}
    state = g.invoke({"request": case}, config)
    p.decide_approval(state["approval_request"]["id"], "approved")
    state = g.invoke(Command(resume="approved"), config)
    from agentkit.annotation import snapshot
    snap = snapshot(state, "lab2")
    assert snap["manager_approval"] == "approved"
    from evals.judge import facts
    assert json.loads(facts(snap))["manager_approval"] == "approved"


def test_the_reply_prompt_gets_the_faq_entry_for_its_decision(platform, tmp_path):
    """Stage 17's labels: 26 of 30 replies made up when the money arrives. Now a refund reply
    sees F5 (5 to 10 working days), a credit reply F4 (within an hour)."""
    p = Platform(platform)
    for case, ans, faq in ((next(c for c in CASES if c["id"] == "late-grill"), answers(order_id="A-9310", amount=72), "F5"),
                           (next(c for c in CASES if c["id"] == "slightly-late"),
                            answers(order_id="A-9002", action="credit", amount=0, clause="§6"), "F4")):
        model = ScriptedStructured(ans)
        build(model, p, db=tmp_path / f"{case['id']}.db").invoke({"request": case},
                                                                 {"configurable": {"thread_id": case["id"]}})
        assert list(json.loads(model.stream_prompts[-1])["faq"]) == [faq]
