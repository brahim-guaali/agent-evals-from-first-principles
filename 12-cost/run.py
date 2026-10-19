"""Stage 12: cost. A metering proxy in front of the model and the paid tools; budgets per run.

    make platform ; make collector ; make meter          # three terminals
    METER_URL=http://127.0.0.1:8720 python 12-cost/run.py          # all scenes
    METER_URL=... python 12-cost/run.py budget                      # or one: costs | budget | cache
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "07-traffic"))

from agentkit import judgments, telemetry
from agentkit.cases import CASES
from agentkit.llm import Gemini
from agentkit.platform import Platform
from agentkit.runner import run_case, summary
from agentkit.traffic import Replayer, plan
from report import load

sys.stdout.reconfigure(line_buffering=True)
STAGE = "12 · cost"
METER = os.environ.get("METER_URL", "http://127.0.0.1:8720")
DATA = Path(__file__).resolve().parent / "data"
telemetry.setup(otlp="http://127.0.0.1:4318", environment="staging")


def costs(platform):
    print("\n=== 1. Twenty requests through the meter: what they cost, and who ===")
    httpx.post(f"{METER}/reset")
    batch = time.strftime("%Y-%m-%d-%H%M")
    log = DATA / f"{batch}-costs.jsonl"
    Replayer(platform, STAGE, log, workers=3, rate_per_min=5, seed=31, fault_rate=0).run(plan(20, seed=31), batch=batch)
    rows = [r for r in load(log) if r.get("run_id")]       # a crashed run has no id (see output-first-attempt.txt)
    by_case = {}
    for r in rows:
        by_case.setdefault(r["base"], []).append(r)
    meter_runs = {c["k"]: c for c in httpx.get(f"{METER}/costs", params={"group_by": "run_id"}).json()}
    print("  by case (meter):")
    for base, rs in sorted(by_case.items(), key=lambda kv: -sum(meter_runs.get(r["run_id"], {}).get("cost", 0) for r in kv[1])):
        total = sum(meter_runs.get(r["run_id"], {}).get("cost", 0) for r in rs)
        print(f"    {base:<22} {len(rs):2} runs  ${total:.5f}  (${total / len(rs):.5f} a run)")
    for g in ("kind", "what", "customer", "day"):
        out = httpx.get(f"{METER}/costs", params={"group_by": g}).json()
        top = ", ".join(f"{c['k']} ${c['cost']:.5f} ({c['calls']})" for c in out[:5])
        print(f"  by {g}: {top}" + (" …" if len(out) > 5 else ""))
    metered = sum(c["cost"] for c in httpx.get(f"{METER}/costs", params={"group_by": "kind"}).json() if c["k"] == "model")
    counted = sum(r.get("cost", 0) for r in rows)
    print(f"  model spend: meter ${metered:.5f} vs the agent's own count ${counted:.5f} "
          f"({'they match' if abs(metered - counted) < 1e-6 else f'differ by ${metered - counted:+.6f}'})")
    tools = sum(c["cost"] for c in httpx.get(f"{METER}/costs", params={"group_by": "kind"}).json() if c["k"] == "tool")
    print(f"  tool spend (paid fraud, device and search calls): ${tools:.4f}, "
          f"{tools / max(metered + tools, 1e-9):.0%} of the total")
    return rows


def budget(platform):
    print("\n=== 2. A budget per run: $0.003. A plain refund fits; a grey-zone review doesn't ===")
    httpx.post(f"{METER}/reset")
    out = {}
    for cid in ("late-grill", "grey-shared-device"):
        platform.reset()
        case = next(c for c in CASES if c["id"] == cid)
        r = run_case(case, platform, STAGE, prefix="cost", budget=0.003)
        led = httpx.get(f"{METER}/ledger", params={"run_id": r["run_id"]}).json()
        out[cid] = led
        stopped = r["state"].get("budget_stop")
        print(f"  {cid}: {summary(r['state']) if not stopped else 'STOPPED by the meter'} · spent ${led['spent']:.5f} "
              f"of $0.003 in {sum(e['status'] == 'ok' for e in led['entries'])} metered calls · {r['seconds']:.1f} s")
        running = 0.0
        t0 = led["entries"][0]["t"] if led["entries"] else 0
        for e in led["entries"]:
            running += e["cost"] if e["status"] == "ok" else 0
            print(f"    +{e['t'] - t0:5.1f}s {e['kind']:<5} {e['what']:<24} {e['status']:<8} ${e['cost']:.5f}  total ${running:.5f}")
        if stopped:
            print(f"    customer was told: \"{r['state']['reply']['text']}\"")
    (DATA / "budget-ledgers.json").write_text(json.dumps(out, indent=1))


def cache():
    print("\n=== 3. Gemini's implicit cache: the same long prefix three times ===")
    prefix = "Refund policy and worked examples, the same for every request:\n" + json.dumps(
        {"clauses": __import__("services.data", fromlist=["POLICY"]).POLICY,
         "faq": __import__("services.data", fromlist=["FAQ"]).FAQ,
         "examples": [{"case": c["id"], "message": c["message"], "expected": c["expect"]} for c in CASES] * 6},
        ensure_ascii=False)
    llm = Gemini(run_id="cache-probe")
    for i, cid in enumerate(("late-grill", "missing-sides", "spoiled-pizza"), 1):
        before = (llm.usage.input_tokens, llm.usage.cached_tokens, llm.usage.spent)
        msg = next(c for c in CASES if c["id"] == cid)["message"]
        llm.structured(judgments.CLASSIFY, prefix + "\n\nLatest customer message:\n" + msg, judgments.Classification)
        inp, cached, cost = (llm.usage.input_tokens - before[0], llm.usage.cached_tokens - before[1],
                             llm.usage.spent - before[2])
        print(f"  call {i}: {inp:,} input tokens, {cached:,} served from the cache, ${cost:.6f} "
              f"({', '.join(llm.usage.models)})")
    print(f"  cached share: {llm.usage.cached_tokens / max(llm.usage.input_tokens, 1):.0%}")


SCENES = {"costs": costs, "budget": budget, "cache": lambda p: cache()}

if __name__ == "__main__":
    platform = Platform()
    platform.check()
    httpx.get(f"{METER}/health")
    for name in sys.argv[1:] or SCENES:
        SCENES[name](platform)
    telemetry.flush()
