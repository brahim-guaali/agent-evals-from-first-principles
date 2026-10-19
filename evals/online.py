"""Online evals (stage 20): score live traffic as it arrives.

An evaluator follows the viewer: every trace whose root span has arrived (the run is over)
gets the deterministic checks, its results written into the trace as evaluation spans (the
same as offline, stage 18), and a count per check and outcome recorded as a metric point,
so the dashboard can draw pass rates over time and an alert can fire on them.
"""
import time

import httpx

from evals.checks import evaluate
from evals.emit import emit


class OnlineEvaluator:
    def __init__(self, platform="http://127.0.0.1:8710", window_s=60):
        self.platform, self.window_s, self.done = platform.rstrip("/"), window_s, set()
        self.counts = {}

    def poll(self, since=0):
        """Evaluate every finished trace not seen yet. Returns how many were evaluated."""
        found = httpx.get(f"{self.platform}/traces/search", params={"since": since, "limit": 5000}).json()
        new = [t for t in found if t["trace_id"] not in self.done and t["root"].startswith("invoke_workflow")]
        points = {}
        for t in new:
            spans = httpx.get(f"{self.platform}/traces/{t['trace_id']}").json()["spans"]
            run = [s for s in spans if not s["name"].startswith("evaluate ")]
            root = next(s for s in run if not s["parent_id"])
            results = evaluate(run)
            emit(t["trace_id"], root["span_id"], results, evaluator="online")
            minute = int(root["end"] // self.window_s * self.window_s)
            for name, r in results.items():
                label = "pass" if r["passed"] else "fail"
                key = (minute, name, label)
                points[key] = points.get(key, 0) + 1
                self.counts[(name, label)] = self.counts.get((name, label), 0) + 1
            self.done.add(t["trace_id"])
        if points:
            httpx.post(f"{self.platform}/v1/points", json=[
                {"name": "refund_agent.evaluations", "kind": "sum", "t": m + self.window_s, "start": m,
                 "attributes": {"gen_ai.evaluation.name": n, "gen_ai.evaluation.score.label": lbl}, "value": v,
                 "environment": "online"} for (m, n, lbl), v in points.items()])
        return len(new)

    def follow(self, seconds, every=2.0, since=0):
        t_end, total = time.time() + seconds, 0
        while time.time() < t_end:
            total += self.poll(since)
            time.sleep(every)
        return total
