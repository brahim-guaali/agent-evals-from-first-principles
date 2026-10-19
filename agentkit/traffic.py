"""Traffic for the agent (stage 07): many customers, varied wording, arriving at random.

Each request is one of the 18 cases rewritten: a fresh order and customer cloned from the
case (so runs don't see each other's refunds), the order id swapped in, an opener, a closer,
sometimes a typo, sometimes all lower case or shouting. Some customers send the same request
twice; the second should be refused under §7 if the first was refunded. Arrivals follow a
Poisson process, and a few workers serve them in parallel, so a busy minute queues.

Everything about every run is appended to a JSONL log: what arrived when, how long it waited,
how long it ran, what it decided against what the case expects, tokens, cost, model events,
guardrail verdicts and failures. That log is what the dashboards, percentiles, sampling and
alerts of part 2 are computed from.
"""
import json
import queue
import random
import re
import threading
import time
import uuid
from pathlib import Path

from agent.versions import run_meta
from agentkit import customers
from agentkit.cases import CASES
from agentkit.runner import outcome, run_case, summary

OPENERS = ["", "", "Hi, ", "Hello team, ", "Good evening. ", "URGENT: ", "Hey, ", "مرحبا، "]
CLOSERS = ["", "", " Thanks.", " Thank you!", " Please sort this out today.", "!!", " Regards"]


def _typo(text, rng):
    words = text.split()
    candidates = [i for i, w in enumerate(words) if len(w) > 5 and not re.search(r"\d", w)]
    if not candidates:
        return text
    i = rng.choice(candidates)
    w = list(words[i])
    j = rng.randrange(1, len(w) - 2)
    w[j], w[j + 1] = w[j + 1], w[j]
    words[i] = "".join(w)
    return " ".join(words)


FIRST = ["Sara", "Omar", "Priya", "Lina", "James", "Yousef", "Mei", "Karim"]
PII = [
    " You can reach me on +971 50 {a} {b}.", " My email is {first}.{last}@example.com.",
    " I paid with card 4242 4242 4242 4242.", " Call me back on 055 {a} {b} please.",
    " This is {first} {last}, deliver issues to Marina Gate 2, flat 1804.",
]


def add_pii(text, rng):
    """Made-up personal details, the kind customers paste into support chats (stage 11)."""
    first, last = rng.choice(FIRST), rng.choice(["Khan", "Haddad", "Rao", "Smith", "Saleh", "Chen"])
    return text + rng.choice(PII).format(a=rng.randint(100, 999), b=rng.randint(1000, 9999), first=first, last=last)


def variant(case, n, rng, pii_rate=0.0):
    """A new customer, a copy of the case's order, and the message reworded."""
    base_order = re.search(r"A-\d{4}", case["message"]).group(0) if re.search(r"A-\d{4}", case["message"]) else None
    tag = f"{n:04d}"
    order_id = f"A-{base_order[2:]}{tag}" if base_order else None      # A-9310 → A-93100007
    customer = f"{case['customer']}-{tag}"
    text = case["message"].replace(base_order, order_id) if base_order else case["message"]
    style = rng.random()
    if style < 0.15:
        text = text.lower()
    elif style < 0.22:
        text = text.upper()
    if rng.random() < 0.2:
        text = _typo(text, rng)
    text = rng.choice(OPENERS) + text + rng.choice(CLOSERS)
    if pii_rate and rng.random() < pii_rate:
        text = add_pii(text, rng)
    return {"id": f"{case['id']}#{tag}", "base": case["id"], "customer": customer, "message": text,
            "order": base_order, "order_id": order_id, "expect": case["expect"], "guard": case.get("guard", "pass")}


def repeat_of(req, n):
    """The same customer asks again: after a refund, policy says refuse (§7)."""
    first = req["expect"]
    expect = {"action": "refuse", "amount": 0, "approval": False} if first["action"] == "refund" else first
    return {**req, "id": f"{req['base']}#{n:04d}-again", "repeat": True, "expect": expect}


def plan(total, seed=7, repeat_rate=0.08, weights=None, pii_rate=0.0):
    """The list of requests, in arrival order."""
    rng = random.Random(seed)
    cases = [c for c in CASES if re.search(r"A-\d{4}", c["message"]) or c["id"] == "rude-rider"]
    weights = weights or [3 if c["expect"]["action"] in ("refund", "credit") else 1 for c in cases]
    out = []
    while len(out) < total:
        req = variant(rng.choices(cases, weights)[0], len(out) + 1, rng, pii_rate)
        out.append(req)
        if rng.random() < repeat_rate and len(out) < total:
            out.append(repeat_of(req, len(out) + 1))
    return out


class Replayer:
    def __init__(self, platform, stage, log_path, workers=3, rate_per_min=6.0, seed=7, fault_rate=0.0,
                 llm_factory=None, ratings=False, environment=None):
        self.llm_factory, self.ratings, self.environment = llm_factory, ratings, environment
        self.platform, self.stage, self.log_path = platform, stage, Path(log_path)
        self.workers, self.rate, self.rng, self.fault_rate = workers, rate_per_min, random.Random(seed), fault_rate
        self.q, self.lock, self.done = queue.Queue(), threading.Lock(), []
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _prepare(self, req):
        if req.get("order") and not req.get("repeat"):
            self.platform.clone(req["order"], req["order_id"], req["customer"])

    def _serve(self, batch):
        while True:
            item = self.q.get()
            if item is None:
                return
            req, arrived = item
            started = time.time()
            record = {"batch": batch, "id": req["id"], "base": req["base"], "repeat": bool(req.get("repeat")),
                      "customer": req["customer"], "message": req["message"], "arrived": arrived,
                      "queued_s": round(started - arrived, 2), "expect": req["expect"], "guard_expect": req["guard"]}
            try:
                if self.fault_rate and self.rng.random() < self.fault_rate:
                    fault = self.rng.choice(["orders:500", "fraud", "history:500", "orders:bad"])
                    self.platform.inject_faults(**{fault: 1})
                    record["fault_injected"] = fault
                r = run_case({"customer": req["customer"], "message": req["message"]}, self.platform,
                             self.stage, title=req["id"], prefix="t",
                             llm=self.llm_factory(req) if self.llm_factory else None,
                             meta=run_meta(self.environment) if self.environment else None)
                s, u = r["state"], r["usage"]
                ins = [e for e in s.get("guardrails", []) if e["stage"] == "input"]
                got = outcome(r)
                record.update(
                    run_id=r["run_id"], seconds=round(r["seconds"], 2), outcome=got, summary=summary(s),
                    correct=got == req["expect"], guard=ins[0]["verdict"] if ins else None,
                    output_flags=[e["check"] for e in s.get("guardrails", []) if e["stage"] == "output"
                                  and e["verdict"] == "flagged"],
                    failures=s.get("failures", []), handoffs=len(s.get("handoffs", [])),
                    calls=u.calls, models=u.models, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                    cost=round(u.cost, 6), events=u.events,
                    ttft=(s.get("reply", {}).get("stream") or {}).get("ttft"),
                    meta=s.get("meta"), model_versions=sorted(u.versions))
                if self.ratings:
                    rated = customers.rate(record)
                    if rated:
                        self.platform.rate(r["run_id"], req["customer"], *rated)
                        record["rating"], record["comment"] = rated
            except Exception as e:                       # a crash is a data point too
                record.update(seconds=round(time.time() - started, 2), crashed=f"{type(e).__name__}: {e}"[:300],
                              correct=False)
            record["finished"] = time.time()
            with self.lock:
                self.done.append(record)
                with self.log_path.open("a") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.q.task_done()

    def run(self, requests, batch=None, progress=print):
        batch = batch or "b-" + uuid.uuid4().hex[:6]
        threads = [threading.Thread(target=self._serve, args=(batch,), daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        t0 = time.time()
        for i, req in enumerate(requests, 1):
            self._prepare(req)
            self.q.put((req, time.time()))
            if i % 10 == 0:
                with self.lock:
                    n = len(self.done)
                progress(f"  {time.time() - t0:6.0f} s: {i} arrived, {n} finished, {self.q.qsize()} waiting")
            time.sleep(self.rng.expovariate(self.rate / 60))
        self.q.join()
        for _ in threads:
            self.q.put(None)
        return batch, self.done
