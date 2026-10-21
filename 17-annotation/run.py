"""Stage 17: an annotation queue. Thirty real runs, queued with why, labelled by a person.

    make platform ; make collector
    python 17-annotation/run.py              # runs and queues them; then label at http://127.0.0.1:8710/ui/annotate
    python 17-annotation/run.py --report     # once labelled: what the labels say (reference labels by default)
"""
import collections
import sys

import httpx

from agentkit import telemetry
from agentkit.annotation import queue
from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.runner import outcome, run_case, summary
from agentkit.traffic import plan

sys.stdout.reconfigure(line_buffering=True)
STAGE = "17 · annotation"
PLATFORM = "http://127.0.0.1:8710"


def fill(platform):
    telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")
    variants = [r for r in plan(40, seed=51) if not r.get("repeat")][:12]
    items = []
    for c in CASES + [{**v, "customer": v["customer"]} for v in variants]:
        platform.reset()
        if c.get("order") and c.get("order_id"):
            platform.clone(c["order"], c["order_id"], c["customer"])
        r = run_case({"customer": c["customer"], "message": c["message"]}, platform, STAGE, prefix="lab")
        flagged = any(g.get("verdict") == "flagged" for g in r["state"].get("guardrails", []))
        reason = "guardrail flagged" if flagged else ("hand-written case" if c in CASES else "traffic sample")
        right = outcome(r) == c["expect"]
        print(f"  {c['id']:<28} {summary(r['state']):<36} {'✓' if right else '✗'} queued: {reason}")
        items.append((r, reason, None))
    queue(platform, items)
    print(f"\n  {len(items)} runs in the queue: label them at {PLATFORM}/ui/annotate")


def report(annotator):
    rows = httpx.get(f"{PLATFORM}/annotations/export", params={"annotator": annotator}).json()
    if not rows:
        sys.exit("  no labels yet")
    print(f"=== {len(rows)} runs labelled by {annotator!r} ===")
    for k in ("decision_correct", "reply_grounded", "amount_currency_right"):
        yes = sum(r["labels"][k] for r in rows)
        print(f"  {k:<24} yes {yes:>3} · no {len(rows) - yes:>3}")
    print(f"  tone: {dict(collections.Counter(r['labels']['tone'] for r in rows))}")
    for r in rows:
        bad = [k for k, v in r["labels"].items() if v is False or v == "bad"]
        if bad or r["notes"]:
            print(f"  {r['run_id']}: {', '.join(bad)} · {r['notes']}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report(sys.argv[sys.argv.index("--annotator") + 1] if "--annotator" in sys.argv else "reference")
    else:
        p = Platform()
        p.check()
        fill(p)
