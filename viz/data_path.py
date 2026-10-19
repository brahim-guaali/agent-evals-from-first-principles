"""The data path, animated from stage 10's real batch (the collector's archive).

    python viz/data_path.py var/collector/spans-2026-10-20.jsonl var/frames/data-path

Every span of stage 10's 30 runs (1,045 of them) ends at a real moment, recorded in the
archive. A span leaves the agent's SDK when it ends, crosses to the collector in the SDK's next
batch (every 0.5 s, the setting used) and reaches the viewer and the archive in the collector's
next batch (every 1 s). The counters show those cumulative counts as the ten minutes play;
they end at the numbers the collector reported: 1,045 received, 1,045 to the viewer, 1,045 to
the archive. Phoenix and Langfuse are drawn greyed out: they join in stage 22.
"""
import datetime
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

SURFACE, INK, INK2, BOX, LINE = "#fcfcfb", "#0b0b0b", "#52514e", "#f0efec", "#c9c8c2"
COLOURS = {"model": "#2a78d6", "tool": "#0f9d8a", "memory": "#8a5cd0", "agent": "#d9730d", "other": "#9a9993"}


def kind(s):
    op = s["attributes"].get("gen_ai.operation.name")
    return {"chat": "model", "execute_tool": "tool", "retrieval": "memory", "invoke_agent": "agent"}.get(op, "other")


def load(path):
    lo = datetime.datetime(2026, 10, 19, 22, 37, tzinfo=datetime.timezone.utc).timestamp()
    spans = [json.loads(line) for line in open(path)]
    return sorted([s for s in spans if lo <= s["end"] <= lo + 660 and "replay.of" not in s["attributes"]
                   and not s["name"].startswith("evaluate ")], key=lambda s: s["end"])


def box(ax, x, y, w, h, title, sub, n=None, faded=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.04",
                                fc=BOX if not faded else SURFACE, ec=LINE, lw=1.2, ls="-" if not faded else (0, (3, 3))))
    ax.text(x + w / 2, y + h - 0.13, title, ha="center", va="top", fontsize=10.5, color=INK if not faded else INK2,
            weight="bold")
    ax.text(x + w / 2, y + h - 0.36, sub, ha="center", va="top", fontsize=8, color=INK2)
    if n is not None:
        ax.text(x + w / 2, y + 0.16, f"{n:,}", ha="center", va="bottom", fontsize=17, color=INK, weight="bold")


def arrow(ax, x0, y0, x1, y1, dots, window):
    """dots: (seconds since the span crossed, kind) for spans that crossed since the last frame,
    spread along the arrow by that age: the newest near the start."""
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops={"arrowstyle": "-|>", "color": LINE, "lw": 1.4})
    for i, (age, k) in enumerate(dots[-60:]):
        f = 0.08 + 0.84 * min(1.0, age / max(window, 1e-9))
        ax.scatter(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f + ((i % 7) - 3) * 0.035, s=16, color=COLOURS[k], zorder=3,
                   linewidths=0)


def frame(spans, t0, at, path, window, done=False):
    sent = [s for s in spans if s["end"] <= at]
    at_collector = [s for s in spans if math.ceil((s["end"] - t0) / 0.5) * 0.5 + t0 + 0.05 <= at]
    exported = [s for s in spans if math.ceil((s["end"] - t0) / 0.5) * 0.5 + t0 + 1.05 <= at]
    fig, ax = plt.subplots(figsize=(12, 4.8), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 4.6)
    ax.axis("off")
    box(ax, 0.1, 1.6, 2.4, 1.5, "refund agent", "OTel SDK, batch every 0.5 s", len(sent))
    box(ax, 3.3, 1.6, 2.8, 1.5, "collector", "receive · batch 1 s · process", len(at_collector))
    box(ax, 7.3, 3.0, 2.0, 1.2, "viewer", "/ui/traces", len(exported))
    box(ax, 7.3, 1.7, 2.0, 1.2, "archive", "JSONL, a file a day", len(exported))
    box(ax, 7.3, 0.4, 2.0, 1.2, "dashboard", "metric points", 472 if done else None)
    box(ax, 9.8, 3.0, 2.0, 1.2, "Phoenix", "stage 22", faded=True)
    box(ax, 9.8, 1.7, 2.0, 1.2, "Langfuse", "stage 22", faded=True)
    recent = [(at - s["end"], kind(s)) for s in sent if at - s["end"] < window]
    arrow(ax, 2.55, 2.35, 3.25, 2.35, recent, window)
    reach = lambda s: math.ceil((s["end"] - t0) / 0.5) * 0.5 + t0 + 0.05
    leaving = [(at - reach(s), kind(s)) for s in at_collector if at - reach(s) < window]
    for y in (3.6, 2.3):
        arrow(ax, 6.15, 2.35, 7.25, y, leaving, window)
    ax.annotate("", xy=(7.25, 1.0), xytext=(6.15, 2.2), arrowprops={"arrowstyle": "-|>", "color": LINE, "lw": 1.4})
    for y in (3.6, 2.3):
        ax.annotate("", xy=(9.75, y), xytext=(9.35, y), arrowprops={"arrowstyle": "-|>", "color": LINE, "lw": 1, "ls": ":"})
    minutes = (at - t0) / 60
    ax.text(0.2, 4.45, f"Stage 10's real batch: 30 runs, {len(spans):,} spans · minute {minutes:4.1f} of 11",
            fontsize=11.5, color=INK, va="top")
    for i, (k, c) in enumerate(COLOURS.items()):
        ax.scatter(0.3 + i * 1.15, 0.2, s=24, color=c)
        ax.text(0.42 + i * 1.15, 0.2, {"other": "step / http"}.get(k, k), fontsize=8.5, color=INK2, va="center")
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


if __name__ == "__main__":
    spans = load(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("f*.png"):
        f.unlink()
    t0, t1 = spans[0]["start"], spans[-1]["end"] + 2
    n = 60
    window = (t1 - t0) / (n - 1)
    for i in range(n):
        frame(spans, t0, t0 + (t1 - t0) * i / (n - 1), out / f"f{i:03d}.png", window, done=i == n - 1)
    print(f"{n} frames, {len(spans)} spans → {out}")
