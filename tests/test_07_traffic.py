import json
import random

from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.traffic import Replayer, plan, variant
from fakes import ScriptedStructured
from judges import answers


def test_a_plan_is_reproducible_and_varied():
    a, b = plan(40, seed=3), plan(40, seed=3)
    assert [r["message"] for r in a] == [r["message"] for r in b]
    assert len({r["base"] for r in a}) > 8 and len({r["message"] for r in a}) == len(a) - sum(r.get("repeat", False) for r in a)
    again = [r for r in a if r.get("repeat")]
    assert again and all(r["expect"]["action"] != "refund" for r in again)


def test_a_variant_points_at_its_own_order_and_customer():
    grill = next(c for c in CASES if c["id"] == "late-grill")
    v = variant(grill, 7, random.Random(1))
    assert v["order_id"] == "A-93100007" and v["order_id"].lower() in v["message"].lower() and v["customer"] == "C-7740-0007"


def test_the_replayer_runs_clones_and_logs_every_run(platform, tmp_path):
    reqs = [r for r in plan(30, seed=5) if r["base"] in ("late-grill", "missing-sides")][:3]
    p = Platform(platform)
    amounts = {"late-grill": (72, "§1"), "missing-sides": (10, "§2")}

    def model(req):
        amount, clause = amounts[req["base"]]
        return ScriptedStructured(answers(order_id=req["order_id"], amount=amount, clause=clause))
    log = tmp_path / "t.jsonl"
    rp = Replayer(p, "test", log, workers=2, rate_per_min=6000, llm_factory=model)
    _, done = rp.run(reqs, progress=lambda *_: None)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == len(done) == 3 and all(r["correct"] for r in rows), rows
    assert all(p.payments(q["order_id"])["count"] == 1 for q in reqs)


def test_two_runs_for_one_order_at_the_same_moment_pay_once(platform, tmp_path):
    """Found by the first traffic batch: a repeat sent seconds after the first request."""
    import threading
    from agent.graph import build
    p = Platform(platform, timeout=15)          # CI runners are slower; a lookup timeout isn't what this tests
    grill = next(c for c in CASES if c["id"] == "late-grill")
    states = {}

    def go(n):
        g = build(ScriptedStructured(answers(order_id="A-9310", amount=72)), p, db=tmp_path / f"{n}.db")
        states[n] = g.invoke({"request": grill}, {"configurable": {"thread_id": f"race{n}"}})
    threads = [threading.Thread(target=go, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # Exactly one payment, whatever the timing. The second run either reached the payment and got
    # `replayed` back, or (on a slower machine) read a history that already had the refund and
    # refused under §7: both are right; paying twice is the only wrong outcome.
    assert p.payments("A-9310")["count"] == 1
    assert not any(s.get("failures") for s in states.values()), [s.get("failures") for s in states.values()]
    paid = [s for s in states.values() if s.get("payment") and not s["payment"]["replayed"]]
    others = [s for s in states.values() if s not in paid]
    assert len(paid) == 1 and len(others) == 1
    other = others[0]
    assert (other.get("payment") or {}).get("replayed") or "already refunded" in " ".join(other.get("problems") or [])
