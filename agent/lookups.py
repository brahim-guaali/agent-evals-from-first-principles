"""Calling a service the way production needs it (stage 05).

`fetch()` retries what is worth retrying (a timeout, a dropped connection, a 5xx) with a short
back-off, checks the answer against a schema, and never raises: a lookup that still fails
comes back as a failure record (which service, what kind, how many attempts), so the graph
can route on it and the reply can say what went wrong. Until stage 04 the graph used
LangGraph's RetryPolicy, which retries well but, after the last attempt, raises and ends the
run with a stack trace.
"""
import time
from typing import Optional

import httpx
from pydantic import BaseModel, Field, ValidationError

ATTEMPTS = 3
BACKOFF = 0.5          # seconds, doubled after each failed attempt


class Item(BaseModel):
    name: str
    price: float = Field(ge=0)
    qty: int = 1


class Order(BaseModel):
    order_id: str
    customer: str
    items: list[Item]
    total: float = Field(ge=0)
    minutes_late: int = Field(ge=0)
    status: str


class Fraud(BaseModel):
    customer: str
    score: float = Field(ge=0, le=1)
    signals: list[str]


class History(BaseModel):
    customer: str
    refunds_30d: int = Field(ge=0)
    refunded_orders: list[str]
    refunds: list[dict]
    notes: list[dict]


class Policy(BaseModel):
    version: str
    clauses: dict[str, str]
    faq: dict[str, str] = {}


def transient(e):
    if isinstance(e, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500


def kind_of(e):
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return "not_found" if code == 404 else f"http_{code}"
    if isinstance(e, ValidationError):
        return "bad_data"
    return type(e).__name__


def fetch(service, call, schema: Optional[type] = None, tracer=None):
    """Returns (value, failure, attempts): exactly one of value and failure is set."""
    wait = BACKOFF
    for attempt in range(1, ATTEMPTS + 1):
        try:
            value = call()
            if schema is not None:
                schema.model_validate(value)          # bad data is not retried: it won't fix itself
            return value, None, attempt
        except Exception as e:
            if transient(e) and attempt < ATTEMPTS:
                if tracer:
                    tracer.log(f"{service}: {kind_of(e)}, retry {attempt} in {wait:g} s")
                time.sleep(wait)
                wait *= 2
                continue
            detail = str(e).splitlines()[0][:160]
            return None, {"service": service, "kind": kind_of(e), "attempts": attempt, "detail": detail}, attempt


# What the customer is told when a lookup fails, by service.
CUSTOMER_WORDS = {
    "orders": "we couldn't load your order details",
    "policy": "we couldn't check our refund rules",
    "fraud": "we couldn't finish a routine account check",
    "history": "we couldn't load your account history",
}


def failure_message(failures, order_id=None):
    if any(f["service"] == "orders" and f["kind"] == "not_found" for f in failures):
        return (f"We couldn't find order {order_id}. Could you check the order number in the app "
                "(Orders › the order › details) and send it again?")
    reasons = "; ".join(CUSTOMER_WORDS.get(f["service"], f["service"]) for f in failures)
    return (f"Sorry, {reasons} just now, so we can't decide on your refund yet. "
            "A colleague will pick this up and get back to you today.")
