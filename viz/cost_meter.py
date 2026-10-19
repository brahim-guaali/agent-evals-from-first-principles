"""The cost meter, animated from the meter's real ledger (stage 12).

    python viz/cost_meter.py 12-cost/data/budget-ledgers.json var/frames/cost-meter [budget]

One frame per step of time: each run's cumulative spend climbs call by call toward the budget
line; a refused call is marked where the meter stopped the run. Every value comes from the
ledger the metering proxy wrote during the run.
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e9e8e4"
SERIES = ["#2a78d6", "#d9730d"]            # validated pair (dataviz validator: all checks pass)
CRITICAL = "#d03b3b"
LABELS = {"late-grill": "plain refund ($72 late)", "grey-shared-device": "grey-zone review (fraud 0.58)"}


def steps(ledger):
    """(seconds since the run's first call, cumulative $, status, what) per ledger entry."""
    entries = ledger["entries"]
    if not entries:
        return []
    t0, total, out = entries[0]["t"], 0.0, [(0.0, 0.0, "start", "")]
    for e in entries:
        if e["status"] == "ok":
            total += e["cost"]
        out.append((e["t"] - t0, total, e["status"], e["what"]))
    return out


def frame(series, budget, at, path, end):
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.axhline(budget, color=INK2, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.text(end * 0.995, budget, f"budget ${budget:.4f} per run", color=INK2, fontsize=9, ha="right", va="bottom")
    for i, (name, pts) in enumerate(series.items()):
        shown = [p for p in pts if p[0] <= at]
        if not shown:
            continue
        xs, ys = [], []
        for t, total, status, what in shown:           # a staircase: spend jumps when a call is billed
            if xs:
                xs.append(t)
                ys.append(ys[-1])
            xs.append(t)
            ys.append(total)
        xs.append(min(at, pts[-1][0]))
        ys.append(ys[-1])
        ax.plot(xs, ys, color=SERIES[i], linewidth=2, solid_capstyle="round")
        ax.scatter([xs[-1]], [ys[-1]], s=40, color=SERIES[i], zorder=3, edgecolors=SURFACE, linewidths=2)
        ax.text(xs[-1] + end * 0.01, ys[-1], f"{LABELS.get(name, name)}  ${ys[-1]:.5f}", color=INK, fontsize=9, va="center")
        for t, total, status, what in shown:
            if status == "refused":
                ax.scatter([t], [total], s=90, marker="X", color=CRITICAL, zorder=4)
                ax.annotate(f"refused: {what}\nrun stopped", (t, total), xytext=(t - end * 0.18, budget * 1.12),
                            color=CRITICAL, fontsize=9, arrowprops={"arrowstyle": "-", "color": CRITICAL})
    ax.set_xlim(0, end * 1.25)
    ax.set_ylim(0, budget * 1.35)
    ax.set_xlabel("seconds into the run", color=INK2, fontsize=9)
    ax.set_ylabel("spend at list price (USD)", color=INK2, fontsize=9)
    ax.set_title("The meter: every model and tool call billed as it happens", color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    budget = float(sys.argv[3]) if len(sys.argv) > 3 else 0.003
    series = {k: steps(v) for k, v in json.loads(src.read_text()).items()}
    end = max(p[-1][0] for p in series.values() if p)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("f*.png"):
        f.unlink()
    n = 60
    for i in range(n):
        frame(series, budget, end * i / (n - 1), out / f"f{i:03d}.png", end)
    print(f"{n} frames → {out}")
