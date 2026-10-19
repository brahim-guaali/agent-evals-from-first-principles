from agentkit.telemetry import SECONDS, WORKFLOW
from services.alerts import Engine, Rule, psi, window


def run_point(t, n, failed=False, slow=False):
    buckets = [0] * (len(WORKFLOW) + 1)
    buckets[5 if slow else 2] = n                          # 120 s bucket vs 10 s bucket
    return {"name": "gen_ai.invoke_workflow.duration", "kind": "histogram", "t": t, "count": n, "sum": 0,
            "attributes": {"error.type": "x"} if failed else {}, "bounds": WORKFLOW, "buckets": buckets}


def ttft_point(t, n, bucket):
    buckets = [0] * (len(SECONDS) + 1)
    buckets[bucket] = n
    return {"name": "gen_ai.client.inference.time_to_first_chunk", "kind": "histogram", "t": t, "count": n, "sum": 0,
            "attributes": {}, "bounds": SECONDS, "buckets": buckets}


def test_psi_is_zero_for_the_same_distribution_and_large_when_it_moves():
    assert psi([10, 20, 30], [20, 40, 60]) < 1e-9
    assert psi([90, 10, 0], [10, 10, 80]) > 1


def test_burn_rate_needs_both_windows():
    rule = Rule("avail", "burn", "", {"metric": "gen_ai.invoke_workflow.duration", "objective": 0.99, "burn": 14.4,
                                       "short_s": 300, "long_s": 3600})
    healthy = [run_point(t, 10) for t in range(0, 3600, 60)]
    spike = healthy + [run_point(3590, 20, failed=True)]      # 20 failures in the last minute
    assert rule.evaluate(healthy, 3600)[0] is False
    on, detail = rule.evaluate(spike, 3600)
    assert detail["burn_short"] > 14.4 and on is (detail["burn_long"] >= 14.4)


def test_slow_runs_are_counted_from_the_buckets():
    w = window([run_point(10, 4, slow=True), run_point(20, 6)], "gen_ai.invoke_workflow.duration", 0, 60, slow_s=60)
    assert (w.count, w.slow) == (10, 4)


def test_the_engine_reports_firing_and_resolved():
    rule = Rule("drift", "drift", "", {"metric": "gen_ai.client.inference.time_to_first_chunk", "psi": 0.25,
                                        "window_s": 300, "ref_start": 0, "ref_end": 600, "min_count": 5})
    points = [ttft_point(t, 5, 5) for t in range(0, 600, 60)]          # ~0.3 s
    points += [ttft_point(t, 5, 8) for t in range(600, 1200, 60)]      # ~2.5 s: moved
    points += [ttft_point(t, 5, 5) for t in range(1200, 1800, 60)]     # back
    e = Engine([rule])
    for now in range(300, 1800, 60):
        e.tick(points, now)
    assert [ev["state"] for ev in e.events] == ["firing", "resolved"]
