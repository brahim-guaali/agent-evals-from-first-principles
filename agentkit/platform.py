"""HTTP client for the food-delivery platform (services/app.py)."""
import os

import re

import httpx
from opentelemetry.trace import SpanKind

from agentkit import telemetry

TIMEOUT = float(os.environ.get("PLATFORM_TIMEOUT", "5"))   # a hung service fails after this


class Platform:
    def __init__(self, url=None, timeout=TIMEOUT):
        self.url = url or os.environ.get("PLATFORM_URL", "http://127.0.0.1:8710")
        self.http = httpx.Client(base_url=self.url, timeout=timeout)

    def _request(self, method, path, **kw):
        """One HTTP call, as a CLIENT span (stage 09): method, route, status, server."""
        if not telemetry.in_trace():
            return self.http.request(method, path, **kw)
        route = re.sub(r"/(A-\w+|C-[\w-]+|apr_\w+|sess-\w+|[\w-]+-[0-9a-f]{6})(?=/|$)", "/{id}", path)
        attrs = {"http.request.method": method, "url.template": route, "server.address": self.http.base_url.host,
                 "server.port": self.http.base_url.port}
        with telemetry.span(f"{method} {route}", SpanKind.CLIENT, attrs) as s:
            r = self.http.request(method, path, **kw)
            s.set_attribute("http.response.status_code", r.status_code)
            if r.status_code >= 400:
                telemetry.fail(s, str(r.status_code), r.text[:120])
            return r

    def _get(self, path, **params):
        r = self._request("GET", path, params=params or None)
        r.raise_for_status()
        return r.json()

    def _post(self, path, json=None, headers=None):
        r = self._request("POST", path, json=json, headers=headers)
        r.raise_for_status()
        return r.json()

    # reads (slow)
    def get_order(self, order_id):
        return self._get(f"/orders/{order_id}")

    def get_policy(self):
        return self._get("/policy")

    def get_fraud(self, customer):
        return self._get(f"/fraud/{customer}")

    # memory
    def get_history(self, customer):
        return self._get(f"/customers/{customer}/history")

    def add_note(self, customer, run_id, order_id, text):
        return self._post(f"/customers/{customer}/notes", {"run_id": run_id, "order_id": order_id, "text": text})

    # outside signals (stage 08)
    def rate(self, run_id, customer, rating, comment="", session_id=None):
        return self._post("/feedback", {"run_id": run_id, "customer": customer, "rating": rating,
                                        "comment": comment, "session_id": session_id})

    def feedback(self, run_id=None):
        return self._get("/feedback", **({"run_id": run_id} if run_id else {}))

    # what sub-agents look up
    def search_policy(self, q):
        return self._get("/policy/search", q=q)

    def get_devices(self, customer):
        return self._get(f"/customers/{customer}/devices")

    # conversations
    def add_turn(self, session_id, customer, role, text, run_id=None):
        return self._post(f"/sessions/{session_id}/turns", {"customer": customer, "role": role,
                                                             "text": text, "run_id": run_id})

    def push_chunk(self, session_id, run_id, seq, text):
        """False when the customer has closed the chat (409)."""
        r = self.http.post(f"/chat/{session_id}/chunks", json={"run_id": run_id, "seq": seq, "text": text})  # no span per chunk
        if r.status_code == 409:
            return False
        r.raise_for_status()
        return True

    def get_chat(self, session_id):
        return self._get(f"/chat/{session_id}")

    def get_session(self, session_id):
        return self._get(f"/sessions/{session_id}")

    # a person
    def request_approval(self, run_id, order_id, amount, reason):
        return self._post("/approvals", {"run_id": run_id, "order_id": order_id,
                                         "amount": amount, "reason": reason})

    def get_approval(self, approval_id):
        return self._get(f"/approvals/{approval_id}")

    def pending_approvals(self):
        return self._get("/approvals", status="pending")

    def decide_approval(self, approval_id, decision, by="ops-manager"):
        return self._post(f"/approvals/{approval_id}/decision", {"decision": decision, "by": by})

    # money and messages
    def pay(self, order_id, amount, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._post("/payments", {"order_id": order_id, "amount": amount}, headers)

    def payments(self, order_id):
        return self._get("/payments", order_id=order_id)

    def notify(self, customer, text):
        return self._post("/notifications", {"customer": customer, "text": text})

    def create_ticket(self, customer, category, priority, summary):
        return self._post("/tickets", {"customer": customer, "category": category,
                                       "priority": priority, "summary": summary})

    # demo controls
    def inject_faults(self, **faults):     # e.g. fraud=1 (hang), leave_after_chunks=2
        return self._post("/admin/faults", faults)

    def clone(self, order_id, new_order_id, new_customer):
        return self._post("/admin/clone", {"order_id": order_id, "new_order_id": new_order_id,
                                           "new_customer": new_customer})

    def reset(self):
        return self._post("/admin/reset")

    def check(self):
        try:
            self._get("/health")
        except httpx.HTTPError:
            raise SystemExit(f"The platform isn't running at {self.url}. Start it with: make platform")
