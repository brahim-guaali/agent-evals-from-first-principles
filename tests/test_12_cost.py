import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request

from agentkit.llm import price_of
from conftest import _free_port


def serve(app):
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            return url
        except httpx.HTTPError:
            time.sleep(0.05)


@pytest.fixture(scope="module")
def meter(platform_url, tmp_path_factory):
    fake = FastAPI()                                   # stands in for generativelanguage.googleapis.com

    @fake.get("/health")
    def health():
        return {"ok": True}

    @fake.post("/v1beta/models/{model_call}")
    async def generate(model_call: str, request: Request):
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "cachedContentTokenCount": 600}}
    upstream = serve(fake)
    from services import meter as m
    m.GEMINI, m.PLATFORM, m.DB = upstream, platform_url, tmp_path_factory.mktemp("meter") / "meter.db"
    url = serve(m.app)
    yield url


def test_cached_tokens_are_billed_at_the_cached_rate():
    full = price_of("gemini-3.1-flash-lite", 1000, 200)
    cached = price_of("gemini-3.1-flash-lite", 1000, 200, cached_tokens=600)
    assert full == pytest.approx((1000 * 0.25 + 200 * 1.5) / 1e6)
    assert cached == pytest.approx((400 * 0.25 + 600 * 0.025 + 200 * 1.5) / 1e6)


def test_the_meter_records_the_real_cost_of_each_model_call(meter):
    h = {"x-run-id": "m1", "x-customer": "C-1", "x-run-budget": "1.0"}
    r = httpx.post(f"{meter}/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent", json={"contents": "hi"}, headers=h)
    assert r.status_code == 200
    led = httpx.get(f"{meter}/ledger", params={"run_id": "m1"}).json()
    assert led["spent"] == pytest.approx(price_of("gemini-3.1-flash-lite", 1000, 200, 600))
    assert led["entries"][0]["cached_tokens"] == 600


def test_a_call_that_could_pass_the_budget_is_refused_before_it_is_made(meter):
    h = {"x-run-id": "m2", "x-run-budget": "0.0005"}
    first = httpx.post(f"{meter}/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent",
                       json={"contents": "hi", "generationConfig": {"maxOutputTokens": 200}}, headers=h)
    second = httpx.post(f"{meter}/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent",
                        json={"contents": "hi", "generationConfig": {"maxOutputTokens": 200}}, headers=h)
    assert first.status_code == 200 and second.status_code == 402
    assert second.json()["error"]["status"] == "BUDGET_EXCEEDED"
    statuses = [e["status"] for e in httpx.get(f"{meter}/ledger", params={"run_id": "m2"}).json()["entries"]]
    assert statuses == ["ok", "refused"]


def test_paid_tools_are_billed_and_free_ones_pass_through(platform, meter):
    h = {"x-run-id": "m3", "x-customer": "C-1"}
    assert httpx.get(f"{meter}/platform/fraud/C-1042", headers=h).json()["score"] < 0.7
    httpx.get(f"{meter}/platform/orders/A-7731", headers=h)
    entries = httpx.get(f"{meter}/ledger", params={"run_id": "m3"}).json()["entries"]
    assert [(e["what"], e["cost"]) for e in entries] == [("/fraud", 0.002)]


def test_a_run_stopped_by_the_budget_ends_safely(platform, tmp_path):
    from agent.graph import build
    from agentkit.cases import CASES
    from agentkit.llm import BudgetExceeded
    from agentkit.platform import Platform
    from fakes import ScriptedStructured
    from judges import answers

    model = ScriptedStructured(answers(order_id="A-9310", amount=72))
    real = model.structured

    def budgeted(system, prompt, schema):
        if schema.__name__ == "Decision":
            raise BudgetExceeded("run t budget $0.001: spent $0.0011")
        return real(system, prompt, schema)
    model.structured = budgeted
    p = Platform(platform)
    g = build(model, p, db=tmp_path / "lg.db")
    grill = next(c for c in CASES if c["id"] == "late-grill")
    state = g.invoke({"request": grill}, {"configurable": {"thread_id": "b1"}})
    assert state["budget_stop"] and "payment" not in state and p.payments("A-9310")["count"] == 0
    assert "colleague" in state["reply"]["text"]


def test_no_model_available_stops_the_run_instead_of_crashing(platform, tmp_path):
    """Found in the first stage 12 run: both free models hit their daily quota and 14 runs crashed."""
    from agent.graph import build
    from agentkit.cases import CASES
    from agentkit.llm import ModelUnavailable
    from agentkit.platform import Platform
    from fakes import ScriptedStructured
    from judges import answers

    model = ScriptedStructured(answers(order_id="A-9310", amount=72))

    def out_of_quota(system, prompt, schema):
        raise ModelUnavailable("429 GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    model.structured = out_of_quota
    g = build(model, Platform(platform), db=tmp_path / "lg.db")
    grill = next(c for c in CASES if c["id"] == "late-grill")
    state = g.invoke({"request": grill}, {"configurable": {"thread_id": "q1"}})
    assert state["budget_stop"] and state["reply"]["why"] == "no model available"
