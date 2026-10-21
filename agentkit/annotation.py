"""Filling the annotation queue (stage 17): what a person needs to judge a run, frozen when
the run ends and linked to its trace by id.

Traces don't carry message bodies (stage 11), so the queue doesn't read them from traces: the
snapshot comes from the run itself, and lives with the platform's own data, where access to
customer messages is already controlled.
"""
from services.data import FAQ, POLICY


def snapshot(state, run_id):
    d, o, reply = state.get("decision") or {}, state.get("order") or {}, state.get("reply") or {}
    return {
        "run_id": run_id,
        "message": (state.get("request") or {}).get("message"),
        "reply": reply.get("text"),
        "correction": reply.get("correction"),
        "decision": {k: d.get(k) for k in ("action", "amount", "clause", "reason")} if d else None,
        "path": "ticket" if reply.get("ticket") and not d else "escalated" if reply.get("queue") else "decided",
        "order": {k: o.get(k) for k in ("order_id", "items", "total", "minutes_late", "status")} if o else None,
        "fraud_score": (state.get("fraud") or {}).get("score"),
        "history": {k: (state.get("history") or {}).get(k) for k in ("refunds_30d", "refunded_orders")},
        "fraud_review": state.get("fraud_review"),
        # §3 and §8 refunds go through only after a manager says yes (stage 19: without this, the
        # judge saw a frequent claimer's refund under §2 and called the decision wrong)
        "manager_approval": state.get("approval"),
        "explanation": reply.get("explanation"),
        "failures": state.get("failures") or [],
        "guardrails": [g for g in state.get("guardrails") or [] if g.get("verdict") == "flagged"],
        "policy": POLICY, "faq": FAQ,
    }


def queue(platform, items):
    """items: (run dict from runner.run_case, reason, trace_id or None)."""
    payload = [{"run_id": r["run_id"], "trace_id": tid, "reason": reason, "snapshot": snapshot(r["state"], r["run_id"])}
               for r, reason, tid in items]
    return platform._post("/annotations/queue", payload)
