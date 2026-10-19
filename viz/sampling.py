"""Head vs tail sampling, animated from stage 13's real decisions.

    python viz/sampling.py 13-sampling/data/sampling.json var/frames/sampling

1,000 replayed runs arrive one after another. Each is a dot: its colour says what the run was
(error, slow, flagged by a guardrail, ordinary), filled if the policy kept its trace, a faint
ring if it was dropped. Left: head sampling (10% at random, decided before the run). Right:
tail sampling (decided after, by what happened). Every dot is one trace from the run's output.
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
COLOUR = {"error": "#d03b3b", "slow": "#d9730d", "flagged": "#8a5cd0", "ok": "#9a9993"}
LABEL = {"error": "error", "slow": "slow (> 60 s)", "flagged": "guardrail flagged", "ok": "ordinary"}
COLS = 40


def panel(ax, order, upto, title):
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-1, COLS)
    ax.set_ylim(-1, len(order) // COLS + 1)
    ax.invert_yaxis()
    ax.axis("off")
    kept = {k: 0 for k in COLOUR}
    seen = {k: 0 for k in COLOUR}
    for i, d in enumerate(order[:upto]):
        x, y = i % COLS, i // COLS
        c = COLOUR[d["kind"]]
        seen[d["kind"]] += 1
        if d["kept"]:
            kept[d["kind"]] += 1
            ax.scatter(x, y, s=34, color=c, linewidths=0)
        else:
            ax.scatter(x, y, s=34, facecolors="none", edgecolors=c, linewidths=0.6, alpha=0.28)
    ax.set_title(title, color=INK, fontsize=11, loc="left", pad=10)
    lines = [f"{LABEL[k]}: kept {kept[k]} of {seen[k]}" for k in ("error", "slow", "flagged", "ok")]
    ax.text(0, len(order) // COLS + 1.4, "   ·   ".join(lines[:2]) + "\n" + "   ·   ".join(lines[2:]),
            color=INK2, fontsize=9, va="top", linespacing=1.6)
    return sum(kept.values())


def frame(data, upto, path):
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 5.4), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    ka = panel(a, data["head"]["order"], upto, "Head sampling · 10% chosen at random when the run starts")
    kb = panel(b, data["tail"]["order"], upto, "Tail sampling · decided after the run, by what happened")
    fig.text(0.5, 0.955, f"{upto:,} of 1,000 replayed real runs arrived · traces kept: head {ka}, tail {kb}",
             ha="center", color=INK, fontsize=12)
    handles = [plt.Line2D([], [], marker="o", linestyle="", color=c, label=LABEL[k], markersize=7) for k, c in COLOUR.items()]
    handles.append(plt.Line2D([], [], marker="o", linestyle="", markerfacecolor="none", markeredgecolor=INK2,
                              label="dropped", markersize=7))
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False, fontsize=9, labelcolor=INK2)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.86, bottom=0.17, wspace=0.08)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("f*.png"):
        f.unlink()
    steps = list(range(25, 1001, 25))
    for i, upto in enumerate(steps):
        frame(data, upto, out / f"f{i:03d}.png")
    print(f"{len(steps)} frames → {out}")
