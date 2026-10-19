"""Stage 13: sampling at volume. 1,000 replayed real runs, head sampling vs tail sampling.

    make platform ; make collector
    python 13-sampling/run.py                      # needs the collector's archive of real runs (stages 10-12)
"""
import collections
import json
import sys
import time
from pathlib import Path

import httpx
from opentelemetry.sdk.trace.sampling import Decision, TraceIdRatioBased

from agentkit.span_replay import categorise, copy_trace, load_traces, plan, send

sys.stdout.reconfigure(line_buffering=True)
COLLECTOR, PLATFORM = "http://127.0.0.1:4318", "http://127.0.0.1:8710"
ARCHIVE = sorted((Path(__file__).resolve().parents[1] / "var" / "collector").glob("spans-*.jsonl"))
OUT = Path(__file__).resolve().parent / "data"
N, HEAD_RATE = 1000, 0.10


def replays():
    traces = [t for f in ARCHIVE for t in load_traces(f)]
    chosen, rng = plan(traces, N, seed=13)
    print(f"  {len(traces)} recorded runs in the archive → {N} replays "
          f"({dict(collections.Counter(categorise(t) for t, _ in chosen))})")
    return chosen, rng


def viewer_traces():
    rows = httpx.get(f"{PLATFORM}/traces", params={"limit": 5000}).json()
    return [httpx.get(f"{PLATFORM}/traces/{t['trace_id']}").json() for t in rows]


def kept_by_kind(traces, kind_of):
    c = collections.Counter()
    for t in traces:
        root = next((s for s in t["spans"] if not s["parent_id"]), t["spans"][0])
        c[kind_of[root["attributes"].get("replay.of")]] += 1
    return c


def scenario(name, processors, decide_at_source, chosen, rng):
    httpx.post(f"{PLATFORM}/admin/clear-traces")
    httpx.post(f"{COLLECTOR}/config", params={"processors": processors})
    before = httpx.get(f"{COLLECTOR}/stats").json()
    kind_of, sent, sent_spans, order = {}, 0, 0, []
    sampler = TraceIdRatioBased(HEAD_RATE)
    for spans, at in chosen:
        kind_of[spans[0]["trace_id"]] = categorise(spans)
        tid, copy = copy_trace(spans, at, rng)
        keep = True
        if decide_at_source:                         # the SDK decides before the trace exists
            keep = sampler.should_sample(None, int(tid, 16), "invoke_workflow refund_agent").decision \
                == Decision.RECORD_AND_SAMPLE
        order.append((categorise(spans), keep, tid))
        if keep:
            send(f"{COLLECTOR}/v1/traces", copy, {"service.name": "refund-agent", "replay": True})
            sent += 1
            sent_spans += len(copy)
    time.sleep(3 if not processors else 5)
    stats = httpx.get(f"{COLLECTOR}/stats").json()
    kept = viewer_traces()
    stored = sum(len(t["spans"]) for t in kept)
    by = kept_by_kind(kept, kind_of)
    truth = collections.Counter(k for k, _, _ in order)
    print(f"\n=== {name} ===")
    print(f"  sent by the agent: {sent} traces ({sent_spans:,} spans) · stored: {len(kept)} traces ({stored:,} spans)")
    for k in ("error", "slow", "flagged", "ok"):
        print(f"    {k:<8} kept {by[k]:>4} of {truth[k]:>4}  ({by[k] / max(truth[k], 1):.0%})")
    if processors:
        dec = {k.split(".")[-1]: stats.get(k, 0) - before.get(k, 0) for k in stats if k.startswith("tail.")}
        print(f"  tail sampler decisions: {dec}")
    rate = by["error"] / max(len(kept), 1)
    print(f"  error rate measured from the stored traces: {rate:.1%} (true: {truth['error'] / N:.1%})")
    # which replays survived, for the visual
    stored_ids = {t["trace_id"] for t in kept}
    return {"order": [{"kind": k, "kept": tid in stored_ids} for k, _, tid in order], "stored_spans": stored,
            "kept": len(kept), "by": dict(by), "truth": dict(truth)}


if __name__ == "__main__":
    for url in (PLATFORM, COLLECTOR):
        httpx.get(f"{url}/health")
    print("=== Replaying real runs at volume ===")
    chosen, rng = replays()
    head = scenario(f"Head sampling: the SDK keeps {HEAD_RATE:.0%} of traces at random", "", True, chosen, rng)
    tail = scenario("Tail sampling in the collector: errors, slow, flagged, plus 5% of the rest", "tail_sample", False,
                    chosen, rng)
    httpx.post(f"{COLLECTOR}/config", params={"processors": ""})
    OUT.mkdir(exist_ok=True)
    (OUT / "sampling.json").write_text(json.dumps({"head": head, "tail": tail}, indent=1))
    print(f"\n  stored spans: head {head['stored_spans']:,} vs tail {tail['stored_spans']:,} of "
          f"{sum(len(t) for t, _ in chosen):,} produced")
