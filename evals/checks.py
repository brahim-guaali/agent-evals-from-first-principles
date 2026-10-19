"""Deterministic checks computed from a run's trace (stage 18).

Each check is a plain function: it gets the run's spans (and, where it needs one, the run's
own record from the traffic log) and returns (passed, explanation), or None when it doesn't
apply. No model involved: these are the referee's rules that never need judgement.
"""

SLO_S, BUDGET_USD, MAX_DECIDE = 60.0, 0.003, 3


def root_of(spans):
    return next(s for s in spans if not s["parent_id"])


def children(spans, parent):
    return [s for s in spans if s["parent_id"] == parent["span_id"]]


def node(spans, name):
    return [s for s in spans if s["name"] == f"node {name}"]


def model_calls(spans):
    return [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "chat"]


def spend_matches_record(spans, record=None):
    """The cost the spans add up to is the cost the run reported (the bill matches the trace)."""
    if not record or "cost" not in record:
        return None
    traced = sum(s["attributes"].get("gen_ai.usage.cost", 0) for s in model_calls(spans))
    ok = abs(traced - record["cost"]) < 1e-6
    return ok, f"spans ${traced:.6f} vs reported ${record['cost']:.6f}"


def every_call_has_usage(spans, record=None):
    """Instrumentation complete: every model call that got an answer recorded its tokens.
    (A call that failed or was refused by the meter has no answer, so no tokens: not a gap.)"""
    calls = [s for s in model_calls(spans) if s["status"] != "ERROR"]
    if not calls:
        return None
    missing = [s["name"] for s in calls if s["attributes"].get("gen_ai.usage.input_tokens") is None]
    return not missing, f"{len(calls) - len(missing)}/{len(calls)} model calls with token usage"


def no_payment_after_a_failed_lookup(spans, record=None):
    """If a lookup failed, the run must not move money."""
    failed = [s for s in spans if s["status"] == "ERROR" and s["attributes"].get("gen_ai.operation.name")
              in ("execute_tool", "retrieval")]
    if not failed:
        return None
    paid = [s for s in spans if s["name"] == "POST /payments"]
    return not paid, f"{len(failed)} failed lookups, {len(paid)} payments"


def at_most_one_payment(spans, record=None):
    paid = [s for s in spans if s["name"] == "POST /payments"]
    if not paid:
        return None
    return len(paid) == 1, f"{len(paid)} payment calls"


def verify_loop_bounded(spans, record=None):
    tries = len(node(spans, "decide"))
    if not tries:
        return None
    return tries <= MAX_DECIDE, f"decide ran {tries} times"


def within_latency_slo(spans, record=None):
    r = root_of(spans)
    if r["attributes"].get("refund_agent.paused"):
        return None                           # waiting for a person isn't latency
    took = r["end"] - r["start"]
    return took <= SLO_S, f"{took:.1f} s (objective {SLO_S:.0f} s)"


def under_budget(spans, record=None):
    spent = sum(s["attributes"].get("gen_ai.usage.cost", 0) for s in model_calls(spans))
    if not spent:
        return None
    return spent <= BUDGET_USD, f"${spent:.5f} of ${BUDGET_USD}"


def sub_agents_finished(spans, record=None):
    agents = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "invoke_agent"]
    if not agents:
        return None
    stuck = [s["attributes"].get("gen_ai.agent.name") for s in agents if s["status"] == "ERROR"]
    return not stuck, f"{len(agents) - len(stuck)}/{len(agents)} sub-agents returned a result"


def output_check_passed(spans, record=None):
    g = [s for s in spans if s["name"] == "guardrail output_facts"]
    if not g:
        return None
    flagged = [s for s in g if s["attributes"].get("guardrail.verdict") == "flagged"]
    return not flagged, ", ".join(str(e) for s in flagged for e in s["attributes"].get("guardrail.evidence", [])) or "pass"


CHECKS = [spend_matches_record, every_call_has_usage, no_payment_after_a_failed_lookup, at_most_one_payment,
          verify_loop_bounded, within_latency_slo, under_budget, sub_agents_finished, output_check_passed]


def evaluate(spans, record=None):
    out = {}
    for check in CHECKS:
        result = check(spans, record)
        if result is not None:
            out[check.__name__] = {"passed": result[0], "explanation": result[1]}
    return out
