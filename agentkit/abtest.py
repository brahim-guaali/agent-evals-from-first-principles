"""Reading an A/B test from the traffic log (stage 16): per arm, the numbers that decide it,
and whether the difference is more than noise.

Proportions (accuracy, guardrail flags) use a two-proportion z-test; means (cost, tokens,
ratings, time) a bootstrap 95% interval of the difference B - A. Standard library only.
"""
import math
import random
from statistics import mean, median


def z_test(k1, n1, k2, n2):
    """p-value (two-sided) for the difference of two proportions."""
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0
    if not se:
        return 1.0
    z = (k2 / n2 - k1 / n1) / se
    return math.erfc(abs(z) / math.sqrt(2))


def bootstrap_diff(a, b, n=2000, seed=1):
    """95% interval of mean(b) - mean(a)."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    diffs = sorted(mean(rng.choices(b, k=len(b))) - mean(rng.choices(a, k=len(a))) for _ in range(n))
    return diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def summarise(rows):
    arms = {}
    for arm in sorted({r.get("arm") for r in rows if r.get("arm")}):
        rs = [r for r in rows if r.get("arm") == arm and not r.get("crashed")]
        arms[arm] = {
            "n": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "flagged": sum(bool(r.get("output_flags")) for r in rs),
            "cost": [r.get("cost", 0) for r in rs],
            "output_tokens": [r.get("output_tokens", 0) / max(r.get("calls", 1), 1) for r in rs],
            "ttft": [r["ttft"] for r in rs if r.get("ttft")],
            "seconds": [r["seconds"] for r in rs],
            "rating": [r["rating"] for r in rs if r.get("rating")],
        }
    return arms


def report(rows, show=print):
    arms = summarise(rows)
    if set(arms) != {"A", "B"}:
        show(f"  arms found: {sorted(arms)}")
        return arms
    a, b = arms["A"], arms["B"]
    show(f"  {'':<30}{'A (compose v1)':>18}{'B (compose v2)':>18}   difference")
    show(f"  {'runs':<30}{a['n']:>18}{b['n']:>18}")
    for label, key in (("correct decisions", "correct"), ("replies the output check flagged", "flagged")):
        p = z_test(a[key], a["n"], b[key], b["n"])
        show(f"  {label:<30}{a[key]:>13} ({a[key] / max(a['n'], 1):.0%}){b[key]:>13} ({b[key] / max(b['n'], 1):.0%})"
             f"   p = {p:.2f}" if p is not None else "")
    for label, key, fmt in (("cost per run ($)", "cost", "{:.5f}"), ("output tokens per call", "output_tokens", "{:.0f}"),
                            ("rating (★)", "rating", "{:.2f}")):
        ci = bootstrap_diff(a[key], b[key])
        if not a[key] or not b[key]:
            continue
        show(f"  {label:<30}{fmt.format(mean(a[key])):>18}{fmt.format(mean(b[key])):>18}"
             f"   B-A 95% CI [{fmt.format(ci[0])}, {fmt.format(ci[1])}]"
             + ("  ← real" if ci[0] > 0 or ci[1] < 0 else "  (could be noise)"))
    for label, key in (("time to first chunk p50 (s)", "ttft"), ("run time p50 (s)", "seconds")):
        if a[key] and b[key]:
            show(f"  {label:<30}{median(a[key]):>18.2f}{median(b[key]):>18.2f}")
    return arms
