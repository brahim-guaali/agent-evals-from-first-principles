"""The release gate (stage 21): what must hold before a change ships.

    python -m evals.gate              # golden traces + the case set through the real graph, scripted model
    python -m evals.gate --live       # also the case set and curated cases on the real model (needs GEMINI_API_KEY)

Three layers, cheapest first:

1. golden traces  The deterministic checks (stage 18) on stored real runs. If an instrumentation
                  change breaks a span (tokens missing, a payment span gone), this fails.
2. scripted cases Every case runs through the real graph, platform and verifier, with a model
                  that gives each case the right judgments. The model is fixed, so a failure here
                  is the *code* getting worse: a routing rule, a threshold, a check removed.
3. live cases     The case set and the curated cases on the real model, against a stored
                  baseline: accuracy may not drop, curated cases must pass, every reply names
                  only the FAQ's timelines, cost and latency must stay in budget. Prompt and
                  model changes are caught here.

Exit code 1 if any layer fails; a markdown summary goes to $GITHUB_STEP_SUMMARY when set.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"


def golden():
    from evals.checks import evaluate
    rows = []
    for f in sorted((HERE / "golden").glob("*.json")):
        res = evaluate(json.loads(f.read_text()))
        failed = {k: r["explanation"] for k, r in res.items() if not r["passed"]}
        rows.append((f.stem, not failed, failed or f"{len(res)} checks passed"))
    return rows


def oracle(case):
    """The judgments a correct model would make for this case."""
    e = case["expect"]
    order = re.search(r"A-\d+", case["message"], re.IGNORECASE)
    clause = {"refund": "§2" if any(w in case["message"].lower() for w in ("missing", "without", "no drink", "burnt"))
              else "§1", "credit": "§5" if "cancel" in case["message"].lower() else "§6"}.get(e["action"], "§4")
    if case["id"] == "already-refunded":
        clause = "§7"
    if case["id"].startswith("grey"):
        clause = "§9" if e["action"] == "refuse" else "§2"
    kind = "other" if e["action"] == "handoff" else "refund_request"

    def answer(schema, prompt):
        if schema == "Classification":
            return {"kind": kind, "order_id": order.group(0).upper() if order else None, "items_claimed": []}
        if schema == "InjectionCheck":
            return {"injection": False, "evidence": ""}
        return {"action": e["action"] if e["action"] != "handoff" else "refuse", "amount": e["amount"], "clause": clause,
                "reason": "per policy", "customer_message": "Done."}
    fraud = [[("finish", {"risk": "high" if e["action"] == "refuse" else "low", "evidence": ["scripted"]})]]
    return answer, fraud


def scripted():
    sys.path.insert(0, str(HERE.parent / "tests"))
    from fakes import ScriptedChat, ScriptedStructured, finish  # noqa: F401
    from agent.graph import build
    from agentkit.cases import CASES
    from agentkit.platform import Platform
    from agentkit.runner import outcome
    from conftest import _free_port
    import threading
    import time
    import httpx
    import uvicorn
    os.environ["PLATFORM_LATENCY"] = "0.01"
    os.environ["PLATFORM_DB"] = str(Path(tempfile.mkdtemp()) / "platform.db")
    from services.app import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    platform, rows, db = Platform(url), [], Path(tempfile.mkdtemp())
    for case in CASES:
        platform.reset()
        answer, fraud = oracle(case)
        model = ScriptedStructured(answer, {"fraud analyst": fraud, "refund policy": [], "support agent": []})
        if case["expect"]["action"] == "handoff":
            model.scripts["support agent"] = [[("create_ticket", {"category": "rider", "priority": "normal",
                                                                   "summary": "rude rider"})], "We filed a ticket."]
        g = build(model, platform, db=db / f"{case['id']}.db")
        config = {"configurable": {"thread_id": case["id"]}}
        state = g.invoke({"request": case}, config)
        asked = bool(g.get_state(config).next)
        if asked:
            from langgraph.types import Command
            platform.decide_approval(state["approval_request"]["id"], "approved")
            state = g.invoke(Command(resume="approved"), config)
        got = outcome({"state": state, "asked": asked})
        rows.append((case["id"], got == case["expect"], got if got != case["expect"] else "as expected"))
    server.should_exit = True
    return rows


def live():
    from agentkit.cases import CASES
    from agentkit.platform import Platform
    from agentkit.runner import outcome, run_case
    from evals import curate
    from evals.checks import timelines_from_faq
    base = json.loads(BASELINE.read_text())
    platform, rows, cost, slow, timed = Platform(), [], 0.0, 0, []
    platform.check()
    for case in CASES:
        platform.reset()
        r = run_case(case, platform, "21 · gate", prefix="gate")
        why = r["state"].get("reply", {}).get("why")              # an escalation says why
        rows.append((case["id"], outcome(r) == case["expect"], outcome(r) | ({"why": why} if why else {})))
        cost += r["usage"].cost
        slow += r["seconds"] > base["max_seconds"]
        timed.append((case["id"], timelines_from_faq(r["state"].get("reply", {}).get("text"))))
    for case in curate.load():
        platform.reset()
        if case.get("order"):
            platform._post("/admin/orders", {"order": case["order"]})
        r = run_case({"customer": case["customer"], "message": case["message"]}, platform, "21 · gate", prefix="gate")
        problems = curate.reply_problems(case, r["state"].get("reply", {}).get("text"))
        rows.append((case["id"], outcome(r) == case["expect"] and not problems, problems or outcome(r)))
        timed.append((case["id"], timelines_from_faq(r["state"].get("reply", {}).get("text"))))
    right, curated_ok = sum(ok for _, ok, _ in rows[:len(CASES)]), all(ok for _, ok, _ in rows[len(CASES):])
    wrong_times = [(name, t[1]) for name, t in timed if t and not t[0]]
    rows += [(name, False, f"reply {detail}") for name, detail in wrong_times]
    gates = [("accuracy", right >= base["min_correct"], f"{right}/{len(CASES)} (baseline {base['min_correct']})"),
             ("curated cases", curated_ok, f"{len(timed) - len(CASES)} curated"),
             ("timelines from the FAQ", not wrong_times,
              f"{len(wrong_times)} of {sum(t is not None for _, t in timed)} replies that name a time name one the FAQ doesn't"),
             ("cost per run", cost / len(CASES) <= base["max_cost_per_run"],
              f"${cost / len(CASES):.5f} (limit ${base['max_cost_per_run']})"),
             ("slow runs", slow <= base["max_slow"], f"{slow} over {base['max_seconds']} s (limit {base['max_slow']})")]
    return rows, gates


def main():
    lines, ok = ["## Release gate", ""], True
    for title, rows in (("Golden traces", golden()), ("Case set, scripted model", scripted())):
        bad = [r for r in rows if not r[1]]
        ok &= not bad
        lines.append(f"**{title}**: {len(rows) - len(bad)}/{len(rows)} {'✅' if not bad else '❌'}")
        lines += [f"- ❌ `{name}`: {detail}" for name, _, detail in bad]
    if "--live" in sys.argv:
        if not os.environ.get("GEMINI_API_KEY"):
            lines.append("**Live cases**: skipped, no GEMINI_API_KEY")
        else:
            rows, gates = live()
            for name, passed, detail in gates:
                ok &= passed
                lines.append(f"- {'✅' if passed else '❌'} {name}: {detail}")
            lines += [f"  - ❌ `{n}`: {d}" for n, good, d in rows if not good]
    lines.append("")
    lines.append("**Gate: PASS**" if ok else "**Gate: FAIL**: this change makes the agent worse")
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
