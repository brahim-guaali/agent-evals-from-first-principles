"""Replay recorded traces at volume (stage 13): the same spans, sent again as new traces.

The free tier allows about 500 model calls a day, so a thousand live runs aren't possible.
These are real runs, recorded by the collector's archive, re-sent with fresh trace and span
ids and shifted timestamps, as OTLP protobuf, to any OTLP endpoint. Every replayed span keeps
its original attributes and timings and gets `replay.of` (the recorded trace it copies), so
nothing pretends to be a new model answer.
"""
import json
import os
import random
from collections import defaultdict

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

KINDS = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
STATUS = {"UNSET": 0, "OK": 1, "ERROR": 2}


def load_traces(archive):
    """Complete recorded runs: traces whose root is an invoke_workflow span."""
    by = defaultdict(list)
    for line in open(archive):
        s = json.loads(line)
        by[s["trace_id"]].append(s)
    return [spans for spans in by.values()
            if any(not s["parent_id"] and s["name"].startswith("invoke_workflow") for s in spans)]


def _any(v):
    if isinstance(v, bool):
        return AnyValue(bool_value=v)
    if isinstance(v, int):
        return AnyValue(int_value=v)
    if isinstance(v, float):
        return AnyValue(double_value=v)
    if isinstance(v, list):
        return AnyValue(array_value=ArrayValue(values=[_any(x) for x in v]))
    return AnyValue(string_value=str(v))


def _kv(d):
    return [KeyValue(key=k, value=_any(v)) for k, v in d.items() if v is not None]


def copy_trace(spans, start_at, rng):
    """New ids, the same shape and timings, moved to start at `start_at`."""
    trace_id = rng.getrandbits(128).to_bytes(16, "big")
    ids = {s["span_id"]: rng.getrandbits(64).to_bytes(8, "big") for s in spans}
    t0 = min(s["start"] for s in spans)
    of = spans[0]["trace_id"]
    out = []
    for s in spans:
        out.append(Span(
            trace_id=trace_id, span_id=ids[s["span_id"]], parent_span_id=ids.get(s["parent_id"], b""),
            name=s["name"], kind=KINDS.get(s["kind"], 1),
            start_time_unix_nano=int((start_at + s["start"] - t0) * 1e9),
            end_time_unix_nano=int((start_at + s["end"] - t0) * 1e9),
            attributes=_kv({**s["attributes"], "replay.of": of}),
            events=[Span.Event(name=e["name"], time_unix_nano=int((start_at + e["t"] - t0) * 1e9),
                               attributes=_kv(e["attributes"])) for e in s.get("events", [])],
            status=Status(code=STATUS.get(s["status"], 0), message=s.get("status_message") or "")))
    return trace_id.hex(), out


def send(endpoint, spans, resource):
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.extend(_kv(resource))
    ss = rs.scope_spans.add()
    ss.scope.name = "refund-agent (replay)"
    ss.spans.extend(spans)
    r = httpx.post(endpoint, content=req.SerializeToString(), headers={"content-type": "application/x-protobuf"},
                   timeout=10)
    r.raise_for_status()


MIX = {"ok": 0.85, "flagged": 0.06, "slow": 0.04, "error": 0.05}    # a production-like mix, stated


def plan(traces, n, seed=13, spread_s=3600, mix=MIX):
    """n replays drawn from the recorded traces in the given mix of kinds, arrivals spread
    over spread_s seconds (ending now)."""
    rng = random.Random(seed)
    pools = defaultdict(list)
    for t in traces:
        pools[categorise(t)].append(t)
    kinds = [k for k in mix if pools[k]]
    import time as _t
    base = _t.time() - spread_s
    out = []
    for i in range(n):
        kind = rng.choices(kinds, [mix[k] for k in kinds])[0]
        out.append((rng.choice(pools[kind]), base + spread_s * i / n))
    return out, rng


def categorise(spans):
    """What a trace *is*, from the recording, before any sampling decides."""
    root = next(s for s in spans if not s["parent_id"])
    a = root["attributes"]
    if any(s["status"] == "ERROR" for s in spans):
        return "error"
    if root["end"] - root["start"] > float(os.environ.get("TAIL_SLOW_S", "60")):
        return "slow"
    if a.get("refund_agent.guardrail_flags"):
        return "flagged"
    return "ok"
