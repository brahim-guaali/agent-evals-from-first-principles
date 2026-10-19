"""Tail sampling (stage 13): a collector processor that decides per trace, after it has ended.

Head sampling decides when a trace *starts* (the SDK's TraceIdRatioBased): cheap, but blind,
so it drops an error as readily as a success. Tail sampling waits until the trace is
complete, then keeps it if any policy matches:

    errors       any span with status ERROR
    slow         the root span lasted longer than SLOW_S
    flagged      a guardrail flagged the run, or it was stopped (budget, no model)
    baseline     a fixed share of everything else, by trace id (so the decision is stable)

Spans are buffered per trace until the root span arrives (it ends last, so it's exported last)
or until DECISION_WAIT_S passes without it. Like the OpenTelemetry Collector's
tail_sampling processor (contrib only: no SDK can do this, a process can't see a trace whose
other half ran somewhere else), with its policy names.

Every decision is counted, so stage 13 can compare what each policy kept.
"""
import os
import threading
import time
from collections import defaultdict

SLOW_S = float(os.environ.get("TAIL_SLOW_S", "60"))
BASELINE = float(os.environ.get("TAIL_BASELINE", "0.05"))
DECISION_WAIT_S = float(os.environ.get("TAIL_DECISION_WAIT_S", "30"))

_buffer: dict[str, list] = defaultdict(list)
_first_seen: dict[str, float] = {}
_lock = threading.Lock()


def baseline_keep(trace_id):
    """The same rule as TraceIdRatioBased: the trace id's low 64 bits against a threshold."""
    return int(trace_id[-16:], 16) < BASELINE * (1 << 64)


def decide(spans):
    """Which policy keeps this trace, or None to drop it."""
    root = next((s for s in spans if not s.get("parent_id")), None)
    if any(s.get("status") == "ERROR" for s in spans):
        return "errors"
    if root is not None and root["end"] - root["start"] > SLOW_S:
        return "slow"
    if root is not None:
        a = root.get("attributes", {})
        if a.get("refund_agent.guardrail_flags") or a.get("refund_agent.budget_stop"):
            return "flagged"
    if baseline_keep(spans[0]["trace_id"]):
        return "baseline"
    return None


def tail_sample(spans):
    """The collector processor: takes the newest batch, returns spans of decided traces."""
    from services import collector
    now, ready = time.time(), []
    with _lock:
        for s in spans:
            _buffer[s["trace_id"]].append(s)
            _first_seen.setdefault(s["trace_id"], now)
        for tid in list(_buffer):
            has_root = any(not s.get("parent_id") for s in _buffer[tid])
            if has_root or now - _first_seen[tid] > DECISION_WAIT_S:
                ready.append((tid, _buffer.pop(tid), has_root))
                _first_seen.pop(tid, None)
    out = []
    for tid, trace_spans, has_root in ready:
        policy = decide(trace_spans)
        collector.stats["tail.traces"] += 1
        if not has_root:
            collector.stats["tail.incomplete"] += 1
        if policy:
            collector.stats[f"tail.kept.{policy}"] += 1
            root = next((s for s in trace_spans if not s.get("parent_id")), None)
            if root is not None:
                root.setdefault("attributes", {})["sampling.policy"] = policy
            out.extend(trace_spans)
        else:
            collector.stats["tail.dropped"] += 1
    return out


def pending():
    with _lock:
        return len(_buffer)
