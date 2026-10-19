"""Render a real terminal transcript (a stage's output.txt) as an image.

    python viz/terminal.py 03-supersteps/output.txt 03-supersteps/terminal.png --lines 1-14 --title "..."

Nothing is retyped: the lines come from the file as the run printed them.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG, FG, DIM, BAR = "#1a1a19", "#e8e7e1", "#8f8e86", "#2b2b29"
GOOD, BAD, WAIT, ACCENT = "#3fbf3f", "#e66767", "#fab219", "#5a9bea"
FONT = "/System/Library/Fonts/Menlo.ttc"


def colour(line):
    s = line.strip()
    if s.startswith("round") or s.startswith("[") or s.startswith("checkpoint"):
        return DIM
    if s.startswith("$"):
        return ACCENT
    if s.startswith("===") or s.startswith("What") or s.startswith("Where"):
        return FG
    if "!!" in s or "✕" in s or "💥" in s or "paid twice" in s or "Error" in s:
        return BAD
    if "⏸" in s or "paused" in s or "waiting" in s:
        return WAIT
    if s.startswith("✓") or "-> " in s or "once, as it should be" in s:
        return GOOD
    return FG


def render(lines, out, title, width_chars=None, scale=2):
    font = ImageFont.truetype(FONT, 13 * scale)
    char_w = font.getlength("M")
    line_h = int(19 * scale)
    width_chars = width_chars or min(130, max(len(l) for l in lines) + 2)
    lines = [l if len(l) <= width_chars else l[:width_chars - 1] + "…" for l in lines]
    pad, bar = 18 * scale, 30 * scale
    w = int(pad * 2 + char_w * width_chars)
    h = int(bar + pad * 2 + line_h * len(lines))
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, bar], fill=BAR)
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        x = pad + i * 20 * scale
        d.ellipse([x, bar / 2 - 6 * scale, x + 12 * scale, bar / 2 + 6 * scale], fill=c)
    tfont = ImageFont.truetype(FONT, 12 * scale)
    d.text((w / 2, bar / 2), title, fill=DIM, font=tfont, anchor="mm")
    for i, line in enumerate(lines):
        d.text((pad, bar + pad + i * line_h), line.replace("💥", "*").replace("⏸", "||"), fill=colour(line), font=font)
    img.save(out, optimize=True)


def pick(text, spec):
    lines = text.rstrip("\n").splitlines()
    if not spec:
        return lines
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += lines[int(a) - 1:int(b or a)]
        out.append("") if part != spec.split(",")[-1] else None
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--lines", help="1-based ranges, like 1-12,20-25")
    ap.add_argument("--title", default="real run")
    ap.add_argument("--width", type=int, help="characters per line (default: the longest line)")
    a = ap.parse_args()
    render(pick(Path(a.src).read_text(), a.lines), a.out, a.title, width_chars=a.width)
