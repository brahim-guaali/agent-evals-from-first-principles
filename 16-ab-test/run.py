"""Stage 16: an A/B test of two prompt versions on live traffic.

    make platform ; make collector
    python 16-ab-test/run.py 60
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "07-traffic"))

from agent.experiments import COMPOSE_TEST
from agentkit import abtest, telemetry
from agentkit.cases import CASES
from agentkit.platform import Platform
from agentkit.traffic import Replayer, plan
from report import load

sys.stdout.reconfigure(line_buffering=True)
STAGE = "16 · A/B test"
telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")

if __name__ == "__main__":
    n = int(sys.argv[1]) if sys.argv[1:] else 60
    platform = Platform()
    platform.check()
    platform.reset()
    # Arabic messages are where the currency slip happens, so they're weighted up; stated, not hidden.
    cases = [c for c in CASES if "A-" in c["message"] or c["id"] == "rude-rider"]
    weights = [6 if c["id"] == "arabic-late" else 3 if c["expect"]["action"] in ("refund", "credit") else 1 for c in cases]
    requests = plan(n, seed=41, weights=weights)
    batch = time.strftime("%Y-%m-%d-%H%M")
    log = Path(__file__).resolve().parent / "data" / f"{batch}.jsonl"
    print(f"=== {n} requests, experiment {COMPOSE_TEST.name}: A = compose v1, B = compose v2 (dollars only, amount first, "
          f"≤60 words), split by customer ===")
    Replayer(platform, STAGE, log, workers=3, rate_per_min=5, seed=41, ratings=True, experiment=COMPOSE_TEST).run(
        requests, batch=batch)
    print("\n=== Result ===")
    abtest.report(load(log))
    telemetry.flush()
