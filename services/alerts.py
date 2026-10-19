"""SLOs, burn-rate alerts and drift detection over the agent's metrics (stage 14).

Rules read the same metric points the dashboard draws (platform /v1/points). Each rule is
evaluated at a moment `now` over trailing windows, and an alert fires when its condition
holds and resolves when it stops holding. Three kinds of rule:

SLO burn rate  An SLO says "99% of runs end without a failure" over 30 days. The error
               budget is the 1% allowed to fail. The *burn rate* is how fast it's being
               spent: the window's failure ratio divided by 1%. Burning at 14.4× for an hour
               spends 2% of a 30-day budget; the classic fast-burn alert fires when both a
               short and a long window exceed it (Google SRE workbook, multiwindow alerts),
               so one bad minute doesn't page and a real incident pages in minutes.
Drift          The population stability index between a reference window and the latest
               one, on a histogram's buckets. PSI > 0.25 is the usual "the distribution
               moved" line. Drift isn't failure: nothing errors, the numbers just change.
Threshold      Plain limits: spend per hour.
"""
import json
import math
from dataclasses import dataclass, field


@dataclass
class Window:
    """Merged histogram and counter values over [start, end)."""
    count: int = 0
    failed: int = 0
    slow: int = 0
    buckets: list = field(default_factory=list)
    bounds: list = field(default_factory=list)
    value: float = 0.0


def window(points, name, start, end, slow_s=None, where=None):
    w = Window()
    for p in points:
        if p["name"] != name or not (start <= p["t"] < end):
            continue
        a = p["attributes"]
        if where and any(a.get(k) != v for k, v in where.items()):
            continue
        if p["kind"] == "histogram":
            w.count += p["count"]
            if a.get("error.type"):
                w.failed += p["count"]
            if not w.buckets:
                w.buckets, w.bounds = [0] * len(p["buckets"]), p["bounds"]
            w.buckets = [x + y for x, y in zip(w.buckets, p["buckets"])]
            if slow_s is not None:                     # runs above the SLO threshold, from the buckets
                w.slow += sum(n for b, n in zip(p["bounds"] + [math.inf], p["buckets"]) if b > slow_s)
        else:
            w.value += p["value"]
    return w


def psi(ref, cur, eps=1e-4):
    """Population stability index between two bucket-count lists."""
    if not ref or not cur or not sum(ref) or not sum(cur):
        return None
    r = [max(x / sum(ref), eps) for x in ref]
    c = [max(x / sum(cur), eps) for x in cur]
    return sum((ci - ri) * math.log(ci / ri) for ri, ci in zip(r, c))


@dataclass
class Rule:
    name: str
    kind: str                 # burn | drift | threshold
    description: str
    params: dict

    def evaluate(self, points, now):
        p = self.params
        if self.kind == "burn":
            budget = 1 - p["objective"]
            rates = []
            for span_s in (p["short_s"], p["long_s"]):
                w = window(points, p["metric"], now - span_s, now, slow_s=p.get("slow_s"))
                bad = w.slow if p.get("slow_s") else w.failed
                rates.append((bad / w.count / budget) if w.count >= p.get("min_count", 3) else 0.0)
            firing = all(r >= p["burn"] for r in rates)
            return firing, {"burn_short": round(rates[0], 1), "burn_long": round(rates[1], 1)}
        if self.kind == "drift":
            ref = window(points, p["metric"], p["ref_start"], p["ref_end"])
            cur = window(points, p["metric"], now - p["window_s"], now)
            if cur.count < p.get("min_count", 5):
                return False, {"psi": None, "n": cur.count}
            value = psi(ref.buckets, cur.buckets)
            return value is not None and value > p["psi"], {"psi": round(value, 2) if value is not None else None,
                                                            "n": cur.count}
        if self.kind == "threshold":
            w = window(points, p["metric"], now - p["window_s"], now)
            per_hour = w.value * 3600 / p["window_s"]
            return per_hour > p["limit"], {"per_hour": round(per_hour, 4)}
        raise ValueError(self.kind)


def default_rules(ref_start, ref_end):
    return [
        Rule("availability fast burn", "burn",
             "99% of runs end without a failed lookup, a crash or a safe stop; page at 14.4x burn over 5 min and 1 h",
             {"metric": "gen_ai.invoke_workflow.duration", "objective": 0.99, "burn": 14.4, "short_s": 300,
              "long_s": 3600}),
        Rule("latency fast burn", "burn", "90% of runs finish within 60 s; page at 6x burn over 5 min and 30 min",
             {"metric": "gen_ai.invoke_workflow.duration", "objective": 0.90, "slow_s": 60, "burn": 6, "short_s": 300,
              "long_s": 1800}),
        Rule("first-chunk drift", "drift", "time to first chunk: PSI > 0.25 against the first 15 minutes",
             {"metric": "gen_ai.client.inference.time_to_first_chunk", "psi": 0.25, "window_s": 900,
              "ref_start": ref_start, "ref_end": ref_end}),
        Rule("spend rate", "threshold", "model spend above $0.30 an hour at list price",
             {"metric": "refund_agent.cost_usd", "window_s": 600, "limit": 0.30}),
    ]


class Engine:
    """Evaluates every rule at each tick; reports when an alert starts and ends firing."""

    def __init__(self, rules):
        self.rules, self.firing, self.events = rules, {}, []

    def tick(self, points, now):
        for rule in self.rules:
            on, detail = rule.evaluate(points, now)
            if on and rule.name not in self.firing:
                self.firing[rule.name] = now
                self.events.append({"t": now, "rule": rule.name, "state": "firing", "detail": detail})
            elif not on and rule.name in self.firing:
                started = self.firing.pop(rule.name)
                self.events.append({"t": now, "rule": rule.name, "state": "resolved", "detail": detail,
                                    "lasted_s": now - started})
        return self.events


def load_points(rows):
    """Platform rows (attributes as JSON text) into plain dicts."""
    out = []
    for r in rows:
        r = dict(r)
        r["attributes"] = json.loads(r["attributes"]) if isinstance(r["attributes"], str) else r["attributes"]
        for k in ("bounds", "buckets"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])
        out.append(r)
    return out
