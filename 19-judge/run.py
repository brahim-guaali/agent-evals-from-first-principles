"""Stage 19: an LLM judge, calibrated against a person's labels, and tested for position bias.

    make platform
    python 19-judge/run.py                 # needs labels from stage 17 (http://127.0.0.1:8710/ui/annotate)
"""
import json
import sys
from pathlib import Path

import httpx

from agent.experiments import COMPOSE_V2
from agentkit.llm import Gemini
from agentkit.platform import Platform
from evals import judge

sys.stdout.reconfigure(line_buffering=True)
PLATFORM = "http://127.0.0.1:8710"
OUT = Path(__file__).resolve().parent / "data"


def main():
    labelled = httpx.get(f"{PLATFORM}/annotations/export", params={"annotator": "brahim"}).json()
    if not labelled:
        sys.exit("No labels yet: label the queue at http://127.0.0.1:8710/ui/annotate first (stage 17).")
    llm = Gemini()
    print(f"=== The judge reads the {len(labelled)} runs Brahim labelled ===")
    pairs, rows = [], []
    for item in labelled:
        v = judge.judge(llm, item["snapshot"]).model_dump()
        pairs.append((item["labels"], v))
        diff = [q for q in judge.QUESTIONS if bool(item["labels"][q]) != bool(v[q])]
        rows.append({"run_id": item["run_id"], "human": item["labels"], "judge": v, "disagree": diff})
        print(f"  {item['run_id']:<12} {'agree' if not diff else 'DISAGREE on ' + ', '.join(diff)}"
              + (f" · judge: {v['reasoning'][:110]}" if diff else ""))
    cal = judge.calibrate(pairs)
    print("\n=== Calibration: the judge against the person ===")
    for q, c in cal.items():
        if q == "tone":
            print(f"  {q:<24} agreement {c['agreement']:.0%} (n={c['n']})")
            continue
        print(f"  {q:<24} agreement {c['agreement']:.0%} · kappa {c['kappa']:.2f} · {c['confusion']}")
    back = [r for r in rows if r["disagree"]]
    if back:
        items = [{"run_id": r["run_id"] + "-review", "reason": f"judge disagreement: {', '.join(r['disagree'])}",
                  "snapshot": next(i["snapshot"] for i in labelled if i["run_id"] == r["run_id"]) | {
                      "judge": r["judge"]}} for r in back]
        httpx.post(f"{PLATFORM}/annotations/queue", json=items)
        print(f"  {len(back)} disagreements sent back to the queue for a second look")

    print("\n=== Position bias: which reply is better, asked twice with the order swapped ===")
    from agentkit import judgments
    cases = []
    for item in labelled:
        s = item["snapshot"]
        if s.get("path") == "decided" and s.get("reply") and len(cases) < 12:
            other = llm.stream(COMPOSE_V2, json.dumps({"customer_message": s["message"], "decision": s["decision"]},
                                                      ensure_ascii=False), lambda piece: True)["text"]
            cases.append((s, s["reply"], other))
    bias = judge.position_bias(llm, cases)
    print(f"  {bias['pairs']} pairs (the original reply vs one written by compose v2): the judge changed its mind "
          f"in {bias['flip_rate']:.0%} of pairs; the reply shown first won {bias['first_position_wins']:.0%} of the time")
    OUT.mkdir(exist_ok=True)
    (OUT / "judge.json").write_text(json.dumps({"rows": rows, "calibration": cal, "position_bias": bias}, indent=1,
                                               ensure_ascii=False))
    print(f"  usage: {llm.usage}")


if __name__ == "__main__":
    Platform().check()
    main()
