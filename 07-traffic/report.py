"""Summarise a traffic log: what a dashboard would show, computed by hand from the JSONL.

    python 07-traffic/report.py 07-traffic/data/<batch>.jsonl
"""
import json
import sys
from collections import Counter, defaultdict


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else 0


def load(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def report(rows, show=print):
    ok = [r for r in rows if not r.get("crashed")]
    secs, queued = [r["seconds"] for r in ok], [r["queued_s"] for r in rows]
    total = [r["seconds"] + r["queued_s"] for r in ok]
    span = (max(r["finished"] for r in rows) - min(r["arrived"] for r in rows)) / 60
    show(f"  {len(rows)} runs in {span:.1f} min ({len(rows) / span:.1f} per min), "
         f"{sum(r['correct'] for r in rows)}/{len(rows)} correct, {len(rows) - len(ok)} crashed")
    show(f"  run time    p50 {pct(secs, 50):5.1f} s   p95 {pct(secs, 95):5.1f} s   p99 {pct(secs, 99):5.1f} s   max {max(secs):5.1f} s")
    show(f"  queue wait  p50 {pct(queued, 50):5.1f} s   p95 {pct(queued, 95):5.1f} s   max {max(queued):5.1f} s")
    show(f"  customer    p50 {pct(total, 50):5.1f} s   p95 {pct(total, 95):5.1f} s   (wait + run)")
    calls = sum(r.get("calls", 0) for r in ok)
    cost = sum(r.get("cost", 0) for r in ok)
    tokens = sum(r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in ok)
    show(f"  {calls} model calls, {tokens:,} tokens, ${cost:.4f} at list price (${cost / len(ok) * 1000:.2f} per 1,000 runs)")
    models = Counter()
    for r in ok:
        models.update(r.get("models", {}))
    events = Counter(e["kind"] for r in ok for e in r.get("events", []))
    show(f"  models: {dict(models)}   model events: {dict(events) or 'none'}")
    failures = Counter(f"{f['service']}:{f['kind']}" for r in ok for f in r.get("failures", []))
    faults = Counter(r["fault_injected"] for r in rows if r.get("fault_injected"))
    show(f"  faults injected: {dict(faults) or 'none'}   failures recorded: {dict(failures) or 'none'}")
    flagged = [r for r in ok if r.get("guard") == "flagged"]
    false_pos = [r for r in flagged if r["guard_expect"] == "pass"]
    missed = [r for r in ok if r.get("guard") == "pass" and r["guard_expect"] == "flagged"]
    show(f"  input guardrail: {len(flagged)} flagged ({len(false_pos)} false positives, {len(missed)} missed); "
         f"output flags: {dict(Counter(f for r in ok for f in r.get('output_flags', [])))}")
    by = defaultdict(list)
    for r in rows:
        by[r["base"] + (" (repeat)" if r["repeat"] else "")].append(r)
    stopped = Counter((r.get("summary") or "").replace("escalated: ", "") for r in ok
                      if "budget" in str(r.get("summary")) or "no model" in str(r.get("summary")))
    if stopped:
        show(f"  stopped safely: {dict(stopped)}")
    show("  by case:")
    for base, rs in sorted(by.items()):
        wrong = [r for r in rs if not r["correct"]]
        show(f"    {base:<34} {len(rs):3} runs  {len(rs) - len(wrong):3} right  p50 {pct([r['seconds'] for r in rs], 50):5.1f} s"
             + (f"   e.g. got {wrong[0].get('outcome') or wrong[0].get('crashed', '')[:60]} for \"{wrong[0]['message'][:60]}\""
                if wrong else ""))


if __name__ == "__main__":
    report(load(sys.argv[1]))
