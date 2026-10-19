import httpx

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
