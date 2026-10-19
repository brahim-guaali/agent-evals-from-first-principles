from services import sampling


def span(tid, sid, parent=None, status="UNSET", start=0.0, end=1.0, **attrs):
    return {"trace_id": tid, "span_id": sid, "parent_id": parent, "status": status, "start": start, "end": end,
            "attributes": attrs, "name": "x"}


def test_policies_keep_errors_slow_and_flagged_runs():
    t = "0" * 31 + "1"
    assert sampling.decide([span(t, "a"), span(t, "b", "a", status="ERROR")]) == "errors"
    assert sampling.decide([span(t, "a", end=90.0)]) == "slow"
    assert sampling.decide([span(t, "a", **{"refund_agent.guardrail_flags": 1})]) == "flagged"
    assert sampling.decide([span(t, "a", **{"refund_agent.budget_stop": True})]) == "flagged"


def test_the_baseline_is_a_stable_share_of_trace_ids():
    import random
    rng = random.Random(1)
    ids = [f"{rng.getrandbits(128):032x}" for _ in range(4000)]
    share = sum(sampling.baseline_keep(t) for t in ids) / len(ids)
    assert 0.04 < share < 0.06 and sampling.baseline_keep(ids[0]) == sampling.baseline_keep(ids[0])


def test_spans_wait_in_the_buffer_until_their_root_arrives():
    t = "f" * 32
    assert sampling.tail_sample([span(t, "b", "a", status="ERROR")]) == []      # child first: held back
    assert sampling.pending() == 1
    out = sampling.tail_sample([span(t, "a")])                                   # root arrives: decided, kept
    assert {s["span_id"] for s in out} == {"a", "b"} and sampling.pending() == 0
    assert next(s for s in out if s["span_id"] == "a")["attributes"]["sampling.policy"] == "errors"


def test_head_sampling_drops_errors_as_readily_as_successes():
    """Decided by the trace id alone, before anything has happened."""
    from opentelemetry.sdk.trace.sampling import Decision, TraceIdRatioBased
    import random
    s, rng = TraceIdRatioBased(0.1), random.Random(2)
    kept = sum(s.should_sample(None, rng.getrandbits(128), "run").decision == Decision.RECORD_AND_SAMPLE
               for _ in range(2000))
    assert 150 < kept < 250
