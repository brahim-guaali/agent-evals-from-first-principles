"""A metering proxy (stage 12): every model call and every paid tool call passes a meter.

    make meter            # or: python -m uvicorn services.meter:app --port 8720

The agent sends Gemini calls to /gemini/... and platform calls to /platform/... instead of
going direct (METER_URL). For each call, with the run's id, customer and budget in headers:

    before   reserve the most the call could cost (input estimated from the request,
             output from its token limit); if the run's spend plus that reserve passes the
             budget, answer 402 and forward nothing: the budget stop
    after    replace the reserve with the real cost, from the response's usageMetadata (or
             the tool's price), and write it to the ledger

Like a utility meter: the agent doesn't have to remember to count. LiteLLM's proxy and
Portkey's gateway do this for real, with budgets per key, user and team.
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agentkit.llm import PRICES, DEFAULT_PRICE, price_of

GEMINI = os.environ.get("GEMINI_UPSTREAM", "https://generativelanguage.googleapis.com")
PLATFORM = os.environ.get("PLATFORM_URL", "http://127.0.0.1:8710").rstrip("/")
DB = Path(os.environ.get("METER_DB", Path(__file__).resolve().parents[1] / "var" / "meter.db"))
DEFAULT_MAX_OUTPUT = 2048          # if a request sets no limit, assume this many output tokens at most

# What the paid platform services charge per call: made up, like the platform itself.
TOOL_PRICES = [("/fraud/", 0.002), ("/devices", 0.001), ("/policy/search", 0.0002)]

app = FastAPI(title="Metering proxy (demo)")
_lock = threading.Lock()
_reserved: dict[str, float] = {}
_client = httpx.AsyncClient(timeout=120)


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""create table if not exists ledger (
        id integer primary key autoincrement, t real, run_id text, customer text, kind text, what text,
        input_tokens integer, output_tokens integer, cached_tokens integer, cost real, budget real, status text)""")
    return conn


def spent(run_id):
    with db() as conn:
        return conn.execute("select coalesce(sum(cost), 0) from ledger where run_id = ? and status = 'ok'",
                            (run_id,)).fetchone()[0]


def write(**row):
    with _lock, db() as conn:
        conn.execute("insert into ledger (t, run_id, customer, kind, what, input_tokens, output_tokens, cached_tokens, "
                     "cost, budget, status) values (?,?,?,?,?,?,?,?,?,?,?)",
                     (time.time(), row["run_id"], row.get("customer"), row["kind"], row["what"], row.get("input_tokens", 0),
                      row.get("output_tokens", 0), row.get("cached_tokens", 0), row.get("cost", 0.0), row.get("budget"),
                      row["status"]))


def reserve(run_id, budget, amount):
    """Check and hold. Returns None if allowed, or the refusal body."""
    with _lock:
        held = _reserved.get(run_id, 0.0)
        already = spent(run_id)
        if budget is not None and already + held + amount > budget:
            return {"error": {"code": 402, "status": "BUDGET_EXCEEDED",
                              "message": f"run {run_id} budget ${budget:.4f}: spent ${already:.5f}, "
                                         f"held ${held:.5f}, this call could cost ${amount:.5f}"}}
        _reserved[run_id] = held + amount
    return None


def release(run_id, amount):
    with _lock:
        _reserved[run_id] = max(0.0, _reserved.get(run_id, 0.0) - amount)


def headers_of(request):
    h = request.headers
    budget = h.get("x-run-budget")
    return h.get("x-run-id", "unknown"), h.get("x-customer"), float(budget) if budget else None


def estimate(body, model):
    """The most this request could cost: its text as input tokens (4 characters a token), and
    its output limit (or DEFAULT_MAX_OUTPUT) as output tokens."""
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        req = {}
    chars = len(json.dumps(req.get("contents", ""))) + len(json.dumps(req.get("systemInstruction", "")))
    max_out = (req.get("generationConfig") or {}).get("maxOutputTokens") or DEFAULT_MAX_OUTPUT
    return price_of(model, chars // 4, max_out)


def usage_cost(usage, model):
    inp, cached = usage.get("promptTokenCount", 0), usage.get("cachedContentTokenCount", 0)
    out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return inp, out, cached, price_of(model, inp, out, cached)


def forward_headers(request):
    return {k: v for k, v in request.headers.items()
            if k.lower() in ("x-goog-api-key", "content-type", "x-goog-api-client", "user-agent")}


@app.api_route("/gemini/{path:path}", methods=["POST", "GET"])
async def gemini(path: str, request: Request):
    run_id, customer, budget = headers_of(request)
    model = path.split("models/")[-1].split(":")[0] if "models/" in path else "unknown"
    body = await request.body()
    hold = estimate(body, model) if request.method == "POST" else 0.0
    refused = reserve(run_id, budget, hold)
    if refused:
        write(run_id=run_id, customer=customer, kind="model", what=model, budget=budget, status="refused")
        return JSONResponse(refused, status_code=402)
    url = f"{GEMINI}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    if "streamGenerateContent" in path:
        upstream = await _client.send(_client.build_request(request.method, url, content=body,
                                                            headers=forward_headers(request)), stream=True)

        async def relay():
            usage, buf = {}, b""
            try:
                async for chunk in upstream.aiter_raw():
                    buf += chunk
                    while b"\n" in buf:                    # SSE: "data: {...}" lines; usage rides on the last ones
                        line, buf = buf.split(b"\n", 1)
                        if line.startswith(b"data:"):
                            try:
                                usage = json.loads(line[5:]).get("usageMetadata") or usage
                            except ValueError:
                                pass
                    yield chunk
            finally:
                await upstream.aclose()
                release(run_id, hold)
                inp, out, cached, cost = usage_cost(usage, model)
                write(run_id=run_id, customer=customer, kind="model", what=model, input_tokens=inp, output_tokens=out,
                      cached_tokens=cached, cost=cost, budget=budget, status="ok" if usage else "no_usage")
        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "text/event-stream"))
    r = await _client.request(request.method, url, content=body, headers=forward_headers(request))
    release(run_id, hold)
    if r.status_code == 200:
        inp, out, cached, cost = usage_cost(r.json().get("usageMetadata", {}), model)
        write(run_id=run_id, customer=customer, kind="model", what=model, input_tokens=inp, output_tokens=out,
              cached_tokens=cached, cost=cost, budget=budget, status="ok")
    else:
        write(run_id=run_id, customer=customer, kind="model", what=model, budget=budget, status=f"upstream_{r.status_code}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.api_route("/platform/{path:path}", methods=["GET", "POST"])
async def platform(path: str, request: Request):
    run_id, customer, budget = headers_of(request)
    price = next((p for frag, p in TOOL_PRICES if frag in "/" + path), 0.0)
    if price:
        refused = reserve(run_id, budget, price)
        if refused:
            write(run_id=run_id, customer=customer, kind="tool", what="/" + path.split("/")[0], cost=0, budget=budget,
                  status="refused")
            return JSONResponse(refused, status_code=402)
    url = f"{PLATFORM}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    fwd = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "idempotency-key")}
    r = await _client.request(request.method, url, content=await request.body(), headers=fwd)
    if price:
        release(run_id, price)
        write(run_id=run_id, customer=customer, kind="tool", what="/" + path.split("/")[0], cost=price if r.status_code < 500 else 0,
              budget=budget, status="ok" if r.status_code < 500 else f"upstream_{r.status_code}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.get("/ledger")
def ledger(run_id: str):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("select * from ledger where run_id = ? order by t", (run_id,))]
    return {"run_id": run_id, "spent": sum(r["cost"] for r in rows if r["status"] == "ok"), "entries": rows}


@app.get("/costs")
def costs(group_by: str = "run_id", since: float = 0):
    """Spend grouped by run_id, customer, kind, what (model or tool), or day."""
    col = {"run_id": "run_id", "customer": "customer", "kind": "kind", "what": "what",
           "day": "date(t, 'unixepoch')"}[group_by]
    with db() as conn:
        rows = conn.execute(f"select {col} as k, count(*) calls, sum(cost) cost, sum(input_tokens) input_tokens, "
                            f"sum(output_tokens) output_tokens, sum(cached_tokens) cached_tokens, "
                            f"sum(status = 'refused') refused from ledger where t >= ? group by k order by cost desc",
                            (since,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/reset")
def reset():
    with _lock, db() as conn:
        conn.execute("delete from ledger")
    _reserved.clear()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "models_priced": sorted(PRICES), "default_price": DEFAULT_PRICE}
