"""A small OpenTelemetry collector, written by hand (stage 10).

    make collector        # or: python -m uvicorn services.collector:app --port 4318

The agent's SDK exports OTLP over HTTP (protobuf) to this process, like it would to the
OpenTelemetry Collector. Here, as there, telemetry flows through a pipeline:

    receiver      POST /v1/traces, POST /v1/metrics        (OTLP/HTTP, protobuf or JSON)
    processors    batch → (stage 11: redact) → (stage 13: tail sampling)
    exporters     viewer (the platform), file (JSONL archive), otlp (Phoenix, Langfuse: stage 22)

Every hop is counted (GET /stats), so the data path can be watched and drawn. The real
Collector does far more (retries with persistent queues, dozens of receivers and exporters,
memory limits); `otelcol-contrib` is the production choice. Writing one shows what it is for.
"""
import json
import os
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import Parse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

PLATFORM = os.environ.get("PLATFORM_URL", "http://127.0.0.1:8710").rstrip("/")
ARCHIVE = Path(os.environ.get("COLLECTOR_ARCHIVE", Path(__file__).resolve().parents[1] / "var" / "collector"))
# OTLP backends to forward to (stage 22): COLLECTOR_FORWARD=url,url or, when a backend needs auth,
# COLLECTOR_FORWARD_JSON='[{"name": "langfuse", "url": "...", "headers": {"Authorization": "Basic ..."}}]'
FORWARD = [{"name": u.split("/")[2], "url": u, "headers": {}} for u in os.environ.get("COLLECTOR_FORWARD", "").split(",") if u]
FORWARD += json.loads(os.environ.get("COLLECTOR_FORWARD_JSON", "[]"))
BATCH_SIZE, BATCH_SECONDS = 200, 1.0

app = FastAPI(title="Refund agent collector (demo)")
stats = Counter()                       # received, batched, exported per exporter, dropped…
_queue, _qlock = [], threading.Lock()
processors = []                         # functions: list of spans -> list of spans, in order


def configure(names=None):
    """COLLECTOR_PROCESSORS=redact,tail_sample (stages 11 and 13): which processors run, in order."""
    names = os.environ.get("COLLECTOR_PROCESSORS", "") if names is None else names
    processors.clear()
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        if name == "redact":
            from services.redact import redact
            processors.append(redact)
        elif name == "tail_sample":
            from services.sampling import tail_sample
            processors.append(tail_sample)
        else:
            raise SystemExit(f"unknown processor {name}")


# --- OTLP protobuf → plain dicts ----------------------------------------------------------
KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


def value(v):
    which = v.WhichOneof("value")
    if which == "array_value":
        return [value(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: value(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, which) if which else None


def attrs(kvs):
    return {kv.key: value(kv.value) for kv in kvs}


def spans_from(req):
    out = []
    for rs in req.resource_spans:
        resource = attrs(rs.resource.attributes)
        for ss in rs.scope_spans:
            for s in ss.spans:
                out.append({
                    "trace_id": s.trace_id.hex(), "span_id": s.span_id.hex(),
                    "parent_id": s.parent_span_id.hex() or None, "name": s.name, "kind": KINDS.get(s.kind, "INTERNAL"),
                    "start": s.start_time_unix_nano / 1e9, "end": s.end_time_unix_nano / 1e9,
                    "status": STATUS.get(s.status.code, "UNSET"), "status_message": s.status.message,
                    "attributes": attrs(s.attributes),
                    "events": [{"name": e.name, "t": e.time_unix_nano / 1e9, "attributes": attrs(e.attributes)} for e in s.events],
                    "links": [{"trace_id": link.trace_id.hex(), "span_id": link.span_id.hex(),
                               "attributes": attrs(link.attributes)} for link in s.links],
                    "resource": resource, "scope": ss.scope.name})
    return out


def points_from(req):
    """Histogram, sum (counter) and gauge points, one dict each."""
    out = []
    for rm in req.resource_metrics:
        resource = attrs(rm.resource.attributes)
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                kind = m.WhichOneof("data")
                data = getattr(m, kind)
                for p in data.data_points:
                    point = {"name": m.name, "unit": m.unit, "kind": kind, "attributes": attrs(p.attributes),
                             "start": p.start_time_unix_nano / 1e9, "t": p.time_unix_nano / 1e9,
                             "environment": resource.get("deployment.environment.name")}
                    if kind == "histogram":
                        point.update(count=p.count, sum=p.sum, bounds=list(p.explicit_bounds),
                                     buckets=list(p.bucket_counts))
                    else:
                        point["value"] = p.as_double if p.WhichOneof("value") == "as_double" else p.as_int
                    out.append(point)
    return out


async def decode(request, message):
    body = await request.body()
    if "json" in request.headers.get("content-type", ""):
        return Parse(body, message())
    msg = message()
    msg.ParseFromString(body)
    return msg


# --- exporters -------------------------------------------------------------------------------
_http = httpx.Client(timeout=5)


def export_viewer(spans):
    _http.post(f"{PLATFORM}/v1/spans", json=spans).raise_for_status()


def export_file(spans):
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    with (ARCHIVE / f"spans-{time.strftime('%Y-%m-%d')}.jsonl").open("a") as f:
        for s in spans:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


EXPORTERS = {"viewer": export_viewer, "file": export_file}


def run_pipeline(spans):
    for p in processors:
        if p.__name__ == "tail_sample":            # holds spans back: count what it returns, not drops
            spans = p(spans)
            continue
        before = len(spans)
        spans = p(spans)
        stats[f"processor.{p.__name__}.dropped"] += before - len(spans)
    for name, fn in EXPORTERS.items():
        if not spans:
            break
        try:
            fn(spans)
            stats[f"exported.{name}"] += len(spans)
        except Exception as e:                       # one exporter down doesn't stop the others
            stats[f"failed.{name}"] += len(spans)
            app.state.last_error = f"{name}: {type(e).__name__}: {e}"[:200]


def _batcher():
    """The batch processor: flush every BATCH_SECONDS or BATCH_SIZE spans, whichever first.
    A tick with nothing new still runs the processors if one of them holds spans back (tail
    sampling waits for whole traces)."""
    while True:
        time.sleep(BATCH_SECONDS)
        with _qlock:
            batch, _queue[:] = _queue[:], []
        if not batch and any(getattr(p, "__name__", "") == "tail_sample" for p in processors):
            run_pipeline([])
        while batch:
            chunk, batch = batch[:BATCH_SIZE], batch[BATCH_SIZE:]
            stats["batches"] += 1
            run_pipeline(chunk)


threading.Thread(target=_batcher, daemon=True).start()
configure()


# --- receivers -------------------------------------------------------------------------------
@app.post("/v1/traces")
async def receive_traces(request: Request):
    req = await decode(request, ExportTraceServiceRequest)
    spans = spans_from(req)
    stats["received.spans"] += len(spans)
    stats["received.requests"] += 1
    with _qlock:
        _queue.extend(spans)
    for fwd in FORWARD:                              # raw OTLP, as received, to another backend
        try:
            r = _http.post(fwd["url"], content=await request.body(),
                           headers={"content-type": request.headers.get("content-type", ""), **fwd.get("headers", {})})
            r.raise_for_status()
            stats[f"forwarded.{fwd['name']}"] += len(spans)
        except httpx.HTTPError as e:
            stats[f"forward_failed.{fwd['name']}"] += len(spans)
            app.state.last_error = f"forward {fwd['name']}: {e}"[:200]
    return Response(content=b"", media_type="application/x-protobuf")


@app.post("/v1/metrics")
async def receive_metrics(request: Request):
    req = await decode(request, ExportMetricsServiceRequest)
    points = points_from(req)
    stats["received.points"] += len(points)
    try:
        _http.post(f"{PLATFORM}/v1/points", json=points).raise_for_status()
        stats["exported.points"] += len(points)
    except httpx.HTTPError:
        stats["failed.points"] += len(points)
    return Response(content=b"", media_type="application/x-protobuf")


@app.get("/stats")
def get_stats():
    with _qlock:
        queued = len(_queue)
    return {**stats, "queued": queued, "processors": [p.__name__ for p in processors],
            "exporters": list(EXPORTERS), "forward": [f["name"] for f in FORWARD], "last_error": getattr(app.state, "last_error", None)}


@app.post("/config")
def set_config(processors: str = ""):
    """Change the processor list while running, e.g. POST /config?processors=redact."""
    configure(processors)
    return {"processors": [p.__name__ for p in globals()["processors"]]}


@app.post("/retention")
def retention(days: float = 30):
    """Delete archive files older than `days` (one file per day)."""
    cutoff, removed = time.time() - 86400 * days, []
    for f in ARCHIVE.glob("spans-*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed.append(f.name)
    return {"removed": removed}


@app.get("/health")
def health():
    return {"ok": True}
