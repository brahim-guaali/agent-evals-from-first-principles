"""Short clips of the strongest visuals, for social feeds (stage 23).

    python viz/social.py <frames dir> <out name> "<headline>" "<one-line caption>" [seconds] [stack=TOP:BOTTOM]

`stack=TOP:BOTTOM` is for a frame with two panels side by side (rows TOP to BOTTOM): on the
vertical clip the right panel goes under the left one, so both stay readable on a phone.

Each clip comes in two shapes, square (1080×1080) and vertical (1080×1350): a big headline
readable on a phone with the sound off, the recorded frames in the middle, a caption, and the
author's URL. MP4 (H.264) and a GIF kept under 5 MB. The frames are the same recorded frames
the READMEs use; nothing is redrawn.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG, INK, INK2, ACCENT = (252, 252, 251), (11, 11, 11), (82, 81, 78), (42, 120, 214)
FONT = "/System/Library/Fonts/Helvetica.ttc"
URL = "medium.com/@brahimg"


def wrap(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= width:
            line = test
        else:
            lines.append(line)
            line = w
    return lines + [line] if line else lines


def stacked(frame, top, bottom):
    """Two side-by-side panels, one above the other, with the frame's header and footer kept."""
    w, h = frame.size
    head, left, right, foot = (frame.crop((0, 0, w, top)), frame.crop((0, top, w // 2, bottom)),
                               frame.crop((w // 2, top, w, bottom)), frame.crop((0, bottom, w, h)))
    half = w // 2
    out = Image.new("RGB", (half, top + 2 * (bottom - top) + (h - bottom)), frame.getpixel((1, 1)))
    out.paste(head.resize((half, round(top * half / w))), (0, 0))
    out.paste(left, (0, top))
    out.paste(right, (0, bottom))
    f = foot.resize((half, round((h - bottom) * half / w)))
    out.paste(f, (0, top + 2 * (bottom - top)))
    return out


def compose(frame, size, headline, caption, stack=None):
    W, H = size
    if stack and H > W:
        frame = stacked(frame, *stack)
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    big, small, tiny = ImageFont.truetype(FONT, 58), ImageFont.truetype(FONT, 34), ImageFont.truetype(FONT, 28)
    y = 56
    for line in wrap(d, headline, big, W - 112):
        d.text((56, y), line, font=big, fill=INK)
        y += 70
    y += 18
    foot = 170
    box_h = H - y - foot
    f = frame.copy()
    scale = min((W - 72) / f.width, box_h / f.height)       # fill the box, up or down
    f = f.resize((round(f.width * scale), round(f.height * scale)), Image.LANCZOS)
    img.paste(f, ((W - f.width) // 2, y + (box_h - f.height) // 2))
    cy = H - foot + 14
    for line in wrap(d, caption, small, W - 112)[:2]:
        d.text((56, cy), line, font=small, fill=INK2)
        cy += 42
    d.text((W - 56, H - 26), URL, font=tiny, fill=ACCENT, anchor="rs")
    return img


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def main(frames_dir, out, headline, caption, seconds=10.0, stack=None):
    frames = sorted(Path(frames_dir).glob("f*.png"))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fps = max(2, round(len(frames) / seconds))
    for shape, size in (("square", (1080, 1080)), ("vertical", (1080, 1350))):
        tmp = Path(tempfile.mkdtemp())
        for i, f in enumerate(frames):
            compose(Image.open(f).convert("RGB"), size, headline, caption, stack).save(tmp / f"f{i:03d}.png")
        last = tmp / f"f{len(frames) - 1:03d}.png"
        for k in range(fps * 2):                          # hold the end two seconds
            shutil.copy(last, tmp / f"f{len(frames) + k:03d}.png")
        mp4, gif = out.with_name(f"{out.name}-{shape}.mp4"), out.with_name(f"{out.name}-{shape}.gif")
        run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "f%03d.png"),
             "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "22", "-movflags", "+faststart", str(mp4)])
        for width, colours in ((720, 96), (600, 64), (480, 48)):   # smallest that keeps it under 5 MB
            run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "f%03d.png"), "-vf",
                 f"scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors={colours}:stats_mode=diff[p];"
                 f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0", str(gif)])
            if gif.stat().st_size < 5_000_000:
                break
        secs = (len(frames) + fps * 2) / fps
        print(f"{mp4.name} {mp4.stat().st_size / 1e6:.1f} MB · {gif.name} {gif.stat().st_size / 1e6:.1f} MB · {secs:.0f} s")
        shutil.rmtree(tmp)


if __name__ == "__main__":
    opts = [a for a in sys.argv[5:] if a.startswith("stack=")]
    rest = [a for a in sys.argv[5:] if not a.startswith("stack=")]
    stack = tuple(int(v) for v in opts[0][6:].split(":")) if opts else None
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(rest[0]) if rest else 10.0, stack)
