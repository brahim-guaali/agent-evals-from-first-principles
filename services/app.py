"""The food-delivery platform our agent works against: a real HTTP API.

    make platform        # or: python -m uvicorn services.app:app --port 8710

Read endpoints are slow on purpose (2 s each, like real downstream services), so you can
see what running them one after another costs. Payments, approvals and notifications are
stored in SQLite, so they outlive the agent: if the agent crashes after paying, the money
has still moved.

Faults are injected over HTTP, so a demo can make a service misbehave the next n times it
is called: POST /admin/faults {"fraud": 1} hangs, {"orders:500": 2} answers 500 twice,
{"orders:bad": 1} returns a broken record once (stage 05).
"""
import asyncio
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from services import data

LATENCY = float(os.environ.get("PLATFORM_LATENCY", "2.0"))   # seconds per read call
HANG = 30                                                    # longer than any client timeout
DB_PATH = Path(os.environ.get("PLATFORM_DB", Path(__file__).resolve().parents[1] / "var" / "platform.db"))

app = FastAPI(title="Food-delivery platform (demo)")
_lock = threading.Lock()
_faults: dict[str, int] = {}
_chats: dict[str, dict] = {}       # session -> the reply being streamed to the customer's app


@contextmanager
def db():
    """A connection that commits on success and is always closed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        create table if not exists payments (
            id text primary key, order_id text, amount real, idempotency_key text unique, created_at text);
        create table if not exists approvals (
            id text primary key, run_id text, order_id text, amount real, reason text,
            status text, decided_by text, created_at text, decided_at text);
        create table if not exists notifications (
            id text primary key, customer text, text text, created_at text);
        create table if not exists turns (
            id integer primary key autoincrement, session_id text, customer text, role text,
            text text, run_id text, created_at text);
        create table if not exists points (
            id integer primary key autoincrement, name text, kind text, t real, start real,
            environment text, attributes text, count integer, sum real, bounds text, buckets text, value real);
        create index if not exists points_name_t on points (name, t);
        create table if not exists spans (
            span_id text primary key, trace_id text, parent_id text, name text, start real, end_ real,
            status text, body text);
        create index if not exists spans_trace on spans (trace_id);
        create table if not exists feedback (
            id text primary key, run_id text, session_id text, customer text, rating integer,
            comment text, created_at text);
        create table if not exists notes (
            id text primary key, customer text, run_id text, order_id text, text text, created_at text);
        create table if not exists tickets (
            id text primary key, customer text, category text, priority text, summary text, created_at text);
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _take(key):
    """Use up one injected fault, if any is left."""
    if _faults.get(key, 0) > 0:
        _faults[key] -= 1
        return True
    return False


async def _slow(service):
    """Every read takes LATENCY seconds. Injected faults make it hang or answer 500 instead."""
    if _take(service):
        await asyncio.sleep(HANG)
    await asyncio.sleep(LATENCY)
    if _take(f"{service}:500"):
        raise HTTPException(500, f"{service} service error")


# --- reads ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "latency": LATENCY}


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    await _slow("orders")
    if order_id not in data.ORDERS:
        raise HTTPException(404, f"no order {order_id}")
    if _take("orders:bad"):                       # a half-migrated record: no total, lateness unknown
        return {**data.ORDERS[order_id], "total": None, "minutes_late": "unknown"}
    return data.ORDERS[order_id]


@app.get("/policy")
async def get_policy():
    await _slow("policy")
    return {"version": data.POLICY_VERSION, "clauses": data.POLICY}


def _words(text):
    """Lower-case words of 3+ letters, crudely stemmed ("tips"/"tip", "refunded"/"refund")."""
    out = set()
    for w in text.lower().split():
        w = w.strip("?.,:;!$()'\"")
        if len(w) > 5 and w.endswith("ed"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        if len(w) >= 3:
            out.add(w)
    return out


@app.get("/policy/search")
async def search_policy(q: str):
    """Clauses and FAQ answers that share words with the question, rarer words counting more."""
    await asyncio.sleep(LATENCY / 4)
    docs = [(kind, key, text) for kind, items in (("clause", data.POLICY), ("faq", data.FAQ))
            for key, text in items.items()]
    df = {}
    for _, _, text in docs:
        for w in _words(text):
            df[w] = df.get(w, 0) + 1
    query, hits = _words(q), []
    for kind, key, text in docs:
        score = sum(1 / df[w] for w in query & _words(text))
        if score:
            hits.append({"id": key, "kind": kind, "text": text, "score": round(score, 2)})
    return {"query": q, "results": sorted(hits, key=lambda h: -h["score"])[:5]}


@app.get("/customers/{customer}/devices")
async def get_devices(customer: str):
    await _slow("devices")
    return {"customer": customer, "devices": data.DEVICES.get(customer, [])}


@app.get("/fraud/{customer}")
async def get_fraud(customer: str):
    await _slow("fraud")
    if _take("fraud:bad"):                        # a score on the wrong scale
        return {"customer": customer, "score": 86, "signals": []}
    return {"customer": customer, **data.FRAUD.get(customer, data.DEFAULT_FRAUD)}


@app.get("/customers/{customer}/history")
async def get_history(customer: str):
    """The customer's memory: past refunds (seeded, plus every payment made since) and the
    notes earlier runs wrote. The refund count over 30 days is the fraud signal §8 uses."""
    await _slow("history")
    seed = data.HISTORY.get(customer, data.DEFAULT_HISTORY)
    today = datetime.now(timezone.utc)
    refunds = [{"order_id": r["order_id"], "amount": r["amount"], "reason": r["reason"],
                "at": (today - timedelta(days=r["days_ago"])).date().isoformat()} for r in seed["refunds"]]
    orders = [o for o, v in data.ORDERS.items() if v["customer"] == customer]
    with db() as conn:
        if orders:
            marks = ",".join("?" * len(orders))
            for p in conn.execute(f"select * from payments where order_id in ({marks})", orders):
                refunds.append({"order_id": p["order_id"], "amount": p["amount"], "reason": "refund paid",
                                "at": p["created_at"][:10]})
        notes = [dict(n) for n in conn.execute(
            "select run_id, order_id, text, created_at from notes where customer = ? order by created_at",
            (customer,))]
    cutoff = (today - timedelta(days=30)).date().isoformat()
    return {"customer": customer, "orders_90d": seed["orders_90d"], "refunds": refunds,
            "refunds_30d": sum(1 for r in refunds if r["at"] >= cutoff),
            "refunded_orders": sorted({r["order_id"] for r in refunds}), "notes": notes}


class Feedback(BaseModel):
    run_id: str
    session_id: str | None = None
    customer: str
    rating: int              # 1 to 5 stars
    comment: str = ""


@app.post("/feedback")
def add_feedback(f: Feedback):
    """The customer's rating of a reply (stage 08), kept with the run that wrote it."""
    if not 1 <= f.rating <= 5:
        raise HTTPException(422, "rating must be 1 to 5")
    fid = "fb_" + uuid.uuid4().hex[:8]
    with _lock, db() as conn:
        conn.execute("insert into feedback values (?,?,?,?,?,?,?)",
                     (fid, f.run_id, f.session_id, f.customer, f.rating, f.comment, now()))
    return {"id": fid}


@app.get("/feedback")
def list_feedback(run_id: str | None = None):
    with db() as conn:
        q, args = ("select * from feedback where run_id = ?", (run_id,)) if run_id else ("select * from feedback", ())
        return [dict(r) for r in conn.execute(q, args)]


class Note(BaseModel):
    run_id: str
    order_id: str | None = None
    text: str


@app.post("/customers/{customer}/notes")
def add_note(customer: str, n: Note):
    """What a run leaves behind for the next one: one line on what was decided and why."""
    nid = "note_" + uuid.uuid4().hex[:8]
    with _lock, db() as conn:
        conn.execute("insert into notes values (?,?,?,?,?,?)", (nid, customer, n.run_id, n.order_id, n.text, now()))
    return {"id": nid}


class Turn(BaseModel):
    customer: str
    role: str                # customer | agent
    text: str
    run_id: str | None = None


@app.post("/sessions/{session_id}/turns")
def add_turn(session_id: str, t: Turn):
    """A conversation is a session: every message in and every reply out, in order."""
    with _lock, db() as conn:
        conn.execute("insert into turns (session_id, customer, role, text, run_id, created_at) values (?,?,?,?,?,?)",
                     (session_id, t.customer, t.role, t.text, t.run_id, now()))
    return get_session(session_id)


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    with db() as conn:
        turns = [dict(r) for r in conn.execute(
            "select role, text, run_id, created_at from turns where session_id = ? order by id", (session_id,))]
    return {"session_id": session_id, "turns": turns}


class Chunk(BaseModel):
    run_id: str
    seq: int
    text: str


@app.post("/chat/{session_id}/chunks")
def push_chunk(session_id: str, c: Chunk):
    """The agent streams its reply here, piece by piece. A customer who left gets 409 back,
    which tells the agent to stop. Fault `leave_after_chunks: n`: the next reply's customer
    closes the app after n pieces."""
    with _lock:
        chat = _chats.get(session_id)
        if chat is None or chat["run_id"] != c.run_id:
            chat = _chats[session_id] = {"run_id": c.run_id, "chunks": [], "closed": False,
                                         "leave_after": _faults.pop("leave_after_chunks", None)}
        if chat["closed"]:
            raise HTTPException(409, "the customer closed the chat")
        chat["chunks"].append(c.text)
        if chat["leave_after"] is not None and len(chat["chunks"]) >= chat["leave_after"]:
            chat["closed"] = True
    return {"received": len(chat["chunks"])}


@app.post("/chat/{session_id}/close")
def close_chat(session_id: str):
    with _lock:
        _chats.setdefault(session_id, {"run_id": None, "chunks": [], "closed": True})["closed"] = True
    return {"ok": True}


@app.get("/chat/{session_id}")
def get_chat(session_id: str):
    chat = _chats.get(session_id, {"chunks": [], "closed": False})
    return {"text": "".join(chat["chunks"]), "chunks": len(chat["chunks"]), "closed": chat["closed"]}


# --- writes: approvals (a person), payments (money), notifications ---------------------
class ApprovalRequest(BaseModel):
    run_id: str
    order_id: str
    amount: float
    reason: str


class Decision(BaseModel):
    decision: str            # approved | rejected
    by: str = "ops-manager"


@app.post("/approvals")
def request_approval(req: ApprovalRequest):
    aid = "apr_" + uuid.uuid4().hex[:8]
    with _lock, db() as conn:
        conn.execute("insert into approvals values (?,?,?,?,?,?,?,?,?)",
                     (aid, req.run_id, req.order_id, req.amount, req.reason, "pending", None, now(), None))
    return {"id": aid, "status": "pending"}


@app.get("/approvals")
def list_approvals(status: str = "pending"):
    with db() as conn:
        return [dict(r) for r in conn.execute("select * from approvals where status = ?", (status,))]


@app.get("/approvals/{aid}")
def get_approval(aid: str):
    with db() as conn:
        row = conn.execute("select * from approvals where id = ?", (aid,)).fetchone()
    if not row:
        raise HTTPException(404, f"no approval {aid}")
    return dict(row)


@app.post("/approvals/{aid}/decision")
def decide_approval(aid: str, d: Decision):
    if d.decision not in ("approved", "rejected"):
        raise HTTPException(422, "decision must be approved or rejected")
    with _lock, db() as conn:
        conn.execute("update approvals set status = ?, decided_by = ?, decided_at = ? where id = ?",
                     (d.decision, d.by, now(), aid))
    return get_approval(aid)


class PaymentRequest(BaseModel):
    order_id: str
    amount: float


@app.post("/payments")
def create_payment(p: PaymentRequest, idempotency_key: str | None = Header(default=None)):
    """Moves money. With an Idempotency-Key header, the same key never moves it twice."""
    with _lock, db() as conn:
        if idempotency_key:
            row = conn.execute("select * from payments where idempotency_key = ?",
                               (idempotency_key,)).fetchone()
            if row:
                return {**dict(row), "replayed": True}
        pid = "pay_" + uuid.uuid4().hex[:8]
        conn.execute("insert into payments values (?,?,?,?,?)",
                     (pid, p.order_id, p.amount, idempotency_key, now()))
        row = conn.execute("select * from payments where id = ?", (pid,)).fetchone()
    return {**dict(row), "replayed": False}


@app.get("/payments")
def list_payments(order_id: str):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("select * from payments where order_id = ?", (order_id,))]
    return {"order_id": order_id, "count": len(rows), "total": sum(r["amount"] for r in rows), "payments": rows}


class Notification(BaseModel):
    customer: str
    text: str


@app.post("/notifications")
def notify(n: Notification):
    nid = "msg_" + uuid.uuid4().hex[:8]
    with _lock, db() as conn:
        conn.execute("insert into notifications values (?,?,?,?)", (nid, n.customer, n.text, now()))
    return {"id": nid}


class Ticket(BaseModel):
    customer: str
    category: str            # rider | restaurant | app | other
    priority: str            # low | normal | high
    summary: str


@app.post("/tickets")
def create_ticket(t: Ticket):
    tid = "tkt_" + uuid.uuid4().hex[:8]
    with _lock, db() as conn:
        conn.execute("insert into tickets values (?,?,?,?,?,?)",
                     (tid, t.customer, t.category, t.priority, t.summary, now()))
    return {"id": tid, **t.model_dump()}


# --- admin: fault injection and reset, for the demos --------------------------------------
@app.post("/admin/faults")
def set_faults(faults: dict[str, int]):
    """{"fraud": 1} hang once · {"orders:500": 2} two 500s · {"orders:bad": 1} a broken record ·
    {"leave_after_chunks": 2} the customer leaves mid-reply."""
    _faults.update(faults)
    return _faults


class Clone(BaseModel):
    order_id: str
    new_order_id: str
    new_customer: str


@app.post("/admin/clone")
def clone(c: Clone):
    """Traffic (stage 07): a copy of a seeded order for a new customer, with the original
    customer's fraud score, devices and past refunds, so every replayed run starts clean."""
    base = data.ORDERS[c.order_id]
    old = base["customer"]
    data.ORDERS[c.new_order_id] = {**base, "order_id": c.new_order_id, "customer": c.new_customer}
    if old in data.FRAUD:
        data.FRAUD[c.new_customer] = data.FRAUD[old]
    if old in data.DEVICES:
        data.DEVICES[c.new_customer] = data.DEVICES[old]
    if old in data.HISTORY:                      # a past refund on this order follows it to the copy
        h = data.HISTORY[old]
        data.HISTORY[c.new_customer] = {**h, "refunds": [
            {**r, "order_id": c.new_order_id if r["order_id"] == c.order_id else r["order_id"]}
            for r in h["refunds"]]}
    return {"order_id": c.new_order_id, "customer": c.new_customer}


@app.post("/admin/reset")
def reset():
    _faults.clear()
    _chats.clear()
    with _lock, db() as conn:
        conn.executescript("delete from payments; delete from approvals; delete from notifications; "
                           "delete from tickets; delete from notes; delete from turns; delete from feedback;")
    return {"ok": True}


# --- live run viewer: runs post events here, the page at /ui streams them ------------------
_runs: dict[str, dict] = {}
_listeners: set[asyncio.Queue] = set()
UI = Path(__file__).resolve().parent / "ui" / "index.html"


@app.post("/events")
async def post_events(events: list[dict]):
    for e in events:
        run = _runs.setdefault(e["run"], {"id": e["run"], "events": []})
        if e["type"] == "run_started":
            run.update(stage=e["stage"], title=e["title"], status="running", started=now())
        elif e["type"] == "run_finished":
            run["status"] = e["status"]
        run["events"].append(e)
        for q in list(_listeners):
            q.put_nowait(e)
    return {"ok": True}


@app.get("/runs")
def list_runs():
    return [{k: v for k, v in r.items() if k != "events"} for r in reversed(_runs.values())]


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, f"no run {run_id}")
    return _runs[run_id]


@app.get("/events/stream")
async def stream_events():
    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        _listeners.add(q)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _listeners.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/ui", response_class=HTMLResponse)
def ui():
    return UI.read_text()


@app.post("/admin/clear-runs")
def clear_runs():
    _runs.clear()
    return {"ok": True}


# --- traces: spans from the agent's OpenTelemetry exporter (stage 09) -----------------------
TRACES_UI = Path(__file__).resolve().parent / "ui" / "traces.html"
_span_listeners: set[asyncio.Queue] = set()


@app.post("/v1/spans")
async def post_spans(spans: list[dict]):
    """Finished spans as JSON (agentkit.telemetry.ViewerExporter). Stage 10 adds real OTLP."""
    with _lock, db() as conn:
        conn.executemany("insert or replace into spans values (?,?,?,?,?,?,?,?)",
                         [(x["span_id"], x["trace_id"], x["parent_id"], x["name"], x["start"], x["end"],
                           x["status"], json.dumps(x, ensure_ascii=False)) for x in spans])
    for x in spans:
        for q in list(_span_listeners):
            q.put_nowait({"trace_id": x["trace_id"], "name": x["name"]})
    return {"received": len(spans)}


def _summary(trace_id, rows):
    spans = [json.loads(r["body"]) for r in rows]
    root = next((x for x in spans if not x["parent_id"]), None) or min(spans, key=lambda x: x["start"])
    a = root["attributes"]
    title = a.get("refund_agent.run_id", root["name"])
    if a.get("refund_agent.action"):
        title += f" · {a['refund_agent.action']} ${a.get('refund_agent.amount', 0):g}"
    return {"trace_id": trace_id, "title": title + (" (resumed)" if a.get("refund_agent.resumed") else ""),
            "root": root["name"], "start": min(x["start"] for x in spans),
            "duration": max(x["end"] for x in spans) - min(x["start"] for x in spans), "spans": len(spans),
            "model_calls": sum(x["attributes"].get("gen_ai.operation.name") == "chat" for x in spans),
            "errors": sum(x["status"] == "ERROR" for x in spans), "complete": bool(root and not root["parent_id"])}


@app.get("/traces")
def list_traces(limit: int = 200):
    with db() as conn:
        ids = [r["trace_id"] for r in conn.execute(
            "select trace_id, max(end_) e from spans group by trace_id order by e desc limit ?", (limit,))]
        out = []
        for t in ids:
            out.append(_summary(t, conn.execute("select body from spans where trace_id = ?", (t,)).fetchall()))
    return out


@app.get("/traces/{trace_id}")
def get_trace(trace_id: str):
    with db() as conn:
        rows = conn.execute("select body from spans where trace_id = ? order by start", (trace_id,)).fetchall()
    if not rows:
        raise HTTPException(404, f"no trace {trace_id}")
    return {**_summary(trace_id, rows), "spans": [json.loads(r["body"]) for r in rows]}


@app.get("/spans/stream")
async def stream_spans():
    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        _span_listeners.add(q)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(e)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _span_listeners.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/ui/traces", response_class=HTMLResponse)
def traces_ui():
    return TRACES_UI.read_text()


@app.post("/admin/clear-traces")
def clear_traces():
    with _lock, db() as conn:
        conn.execute("delete from spans")
    return {"ok": True}


# --- metrics from the collector (stage 10) ----------------------------------------------------
COLLECTOR = os.environ.get("COLLECTOR_URL", "http://127.0.0.1:4318")
DASHBOARD_UI = Path(__file__).resolve().parent / "ui" / "dashboard.html"


@app.post("/v1/points")
def post_points(points: list[dict]):
    """Metric points (delta temporality: each one covers its own interval)."""
    with _lock, db() as conn:
        conn.executemany(
            "insert into points (name, kind, t, start, environment, attributes, count, sum, bounds, buckets, value) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            [(p["name"], p["kind"], p["t"], p["start"], p.get("environment"), json.dumps(p["attributes"]),
              p.get("count"), p.get("sum"), json.dumps(p.get("bounds")), json.dumps(p.get("buckets")), p.get("value"))
             for p in points])
    return {"received": len(points)}


def percentile(bounds, buckets, q):
    """A quantile from histogram buckets, interpolating inside the bucket it falls in.
    Approximate by design: all a histogram keeps is counts per bucket."""
    total = sum(buckets)
    if not total:
        return None
    rank, seen = q * total, 0
    for i, n in enumerate(buckets):
        if seen + n >= rank and n:
            lo = bounds[i - 1] if i > 0 else 0.0
            hi = bounds[i] if i < len(bounds) else bounds[-1] * 2
            return lo + (hi - lo) * (rank - seen) / n
        seen += n
    return bounds[-1]


@app.get("/metrics/series")
def metric_series(name: str, window: int = 60, since: float = 0, where: str = "", group_by: str = ""):
    """Points of one metric, merged per time window (and per attribute value if group_by):
    histograms give count, mean, p50/p95/p99; counters give the sum."""
    with db() as conn:
        rows = conn.execute("select * from points where name = ? and t >= ? order by t", (name, since)).fetchall()
    filt = dict(kv.split("=", 1) for kv in where.split(",") if "=" in kv)
    series = {}
    for r in rows:
        a = json.loads(r["attributes"])
        if any(str(a.get(k)) != v for k, v in filt.items()):
            continue
        key = (str(a.get(group_by)) if group_by else "all", int(r["t"] // window * window))
        b = series.setdefault(key, {"count": 0, "sum": 0.0, "value": 0.0, "bounds": None, "buckets": None})
        if r["kind"] == "histogram":
            bk = json.loads(r["buckets"])
            b["bounds"] = json.loads(r["bounds"])
            b["buckets"] = [x + y for x, y in zip(b["buckets"], bk)] if b["buckets"] else bk
            b["count"] += r["count"]
            b["sum"] += r["sum"]
        else:
            b["value"] += r["value"] or 0
    out = []
    for (group, t), b in sorted(series.items(), key=lambda kv: kv[0][1]):
        row = {"group": group, "t": t}
        if b["buckets"]:
            row.update(count=b["count"], mean=b["sum"] / b["count"] if b["count"] else None,
                       p50=percentile(b["bounds"], b["buckets"], .5), p95=percentile(b["bounds"], b["buckets"], .95),
                       p99=percentile(b["bounds"], b["buckets"], .99))
        else:
            row["value"] = b["value"]
        out.append(row)
    return out


@app.get("/metrics/names")
def metric_names():
    with db() as conn:
        return [dict(r) for r in conn.execute("select name, kind, count(*) points, min(t) first, max(t) last "
                                              "from points group by name, kind order by name")]


@app.get("/collector/stats")
def collector_stats():
    try:
        return httpx.get(f"{COLLECTOR}/stats", timeout=2).json()
    except httpx.HTTPError:
        return {"error": "collector not running (make collector)"}


@app.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard_ui():
    return DASHBOARD_UI.read_text()


@app.post("/admin/clear-metrics")
def clear_metrics():
    with _lock, db() as conn:
        conn.execute("delete from points")
    return {"ok": True}


# --- retention (stage 11) ---------------------------------------------------------------------
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))


class Retention(BaseModel):
    days: float | None = None
    before: float | None = None         # or an exact cut-off (unix time)


@app.post("/admin/retention")
def apply_retention(r: Retention):
    """Delete spans and metric points older than the cut-off. The archive files are the
    collector's job (POST /retention there)."""
    cutoff = r.before if r.before is not None else datetime.now(timezone.utc).timestamp() - 86400 * (
        r.days if r.days is not None else RETENTION_DAYS)
    with _lock, db() as conn:
        spans = conn.execute("delete from spans where end_ < ?", (cutoff,)).rowcount
        points = conn.execute("delete from points where t < ?", (cutoff,)).rowcount
    return {"cutoff": cutoff, "spans_deleted": spans, "points_deleted": points}


def _retention_loop():
    import time as _t
    while True:
        _t.sleep(3600)
        try:
            apply_retention(Retention())
        except Exception:
            pass


threading.Thread(target=_retention_loop, daemon=True).start()
