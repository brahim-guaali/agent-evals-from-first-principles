"""Alerts over recorded runs, animated from stage 14's evaluation (14-alerts/data/alerts.json).

    python viz/alerts.py 14-alerts/data/alerts.json var/frames/alerts

Three panels over the same 90 minutes, revealed minute by minute: failed runs, runs over the
60 s latency objective, and the median time to first chunk. Shaded bands are the minutes an
alert was firing; labels on top are what really happened (from the run logs).
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e9e8e4"
BLUE, ORANGE, CRITICAL, WARN = "#2a78d6", "#d9730d", "#d03b3b", "#fab219"


def median_from(bounds, buckets):
    total = sum(buckets)
    if not total:
        return None
    rank, seen = total / 2, 0
    for i, n in enumerate(buckets):
        if seen + n >= rank and n:
            lo = bounds[i - 1] if i else 0.0
            hi = bounds[i] if i < len(bounds) else bounds[-1] * 2
            return lo + (hi - lo) * (rank - seen) / n
        seen += n


def bands(events, rule, t0, end):
    out, start = [], None
    for e in events:
        if e["rule"] != rule:
            continue
        if e["state"] == "firing":
            start = e["t"]
        elif start is not None:
            out.append(((start - t0) / 60, (e["t"] - t0) / 60))
            start = None
    if start is not None:
        out.append(((start - t0) / 60, (end - t0) / 60))
    return out


def frame(data, upto, path):
    t0, series = data["t0"], data["series"]
    shown = [s for s in series if s["t"] <= upto]
    xs = [(s["t"] - t0) / 60 for s in shown]
    end_min = (series[-1]["t"] - t0) / 60
    fig, axes = plt.subplots(3, 1, figsize=(10, 8.2), dpi=130, sharex=True)
    fig.patch.set_facecolor(SURFACE)
    specs = [
        ("failed runs, last 5 min", [s["failed"] for s in shown], CRITICAL, "availability fast burn", "availability alert"),
        ("runs over 60 s, last 5 min", [s["slow"] for s in shown], ORANGE, "latency fast burn", "latency alert"),
        ("median time to first chunk (s)", [median_from(s["ttft_bounds"], s["ttft_buckets"]) if s["ttft_buckets"] else None
                                            for s in shown], BLUE, "first-chunk drift", "drift alert"),
    ]
    for ax, (label, ys, colour, rule, alert_label) in zip(axes, specs):
        ax.set_facecolor(SURFACE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=8)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for a, b in bands([e for e in data["events"] if e["t"] <= upto], rule, t0, upto):
            ax.axvspan(a, b, color=WARN, alpha=0.22, linewidth=0)
            ax.text(a, 1.0, f" {alert_label}", transform=ax.get_xaxis_transform(), color=INK, fontsize=8, va="top")
        pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=colour, linewidth=2)
        ax.set_ylabel(label, color=INK2, fontsize=8.5)
        ax.set_xlim(0, end_min)
    axes[0].set_ylim(0, max(max((s["failed"] for s in series), default=1) * 1.25, 1))
    axes[1].set_ylim(0, max(max((s["slow"] for s in series), default=1) * 1.25, 1))
    axes[2].set_ylim(0, 6)
    key = []
    for letter, (t, text) in zip("ABCDE", data["happened"]):
        x = (t - t0) / 60
        if t <= upto:
            for ax in axes:
                ax.axvline(x, color=INK2, linewidth=0.8, linestyle=(0, (2, 3)))
            axes[0].text(x, 1.04, letter, transform=axes[0].get_xaxis_transform(), color=INK, fontsize=9,
                         ha="center", weight="bold")
            key.append(f"{letter}  {text}")
    axes[2].set_xlabel("minutes", color=INK2, fontsize=8.5)
    fig.suptitle("Alerts evaluated every minute over 90 minutes of recorded runs (real model, generated customers)", color=INK, fontsize=11, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0.09, 1, 0.97))
    fig.text(0.02, 0.012, "\n".join(key), color=INK2, fontsize=8.5, va="bottom", linespacing=1.5)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    data = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("f*.png"):
        f.unlink()
    ts = [s["t"] for s in data["series"]]
    n = 45
    for i in range(n):
        frame(data, ts[0] + (ts[-1] - ts[0]) * i / (n - 1), out / f"f{i:03d}.png")
    print(f"{n} frames → {out}")
