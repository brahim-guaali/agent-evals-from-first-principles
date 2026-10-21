"""Stamp the author's Medium URL under the images used in the write-up.

    python viz/watermark.py            # every file in FILES not yet listed in viz/watermarked.txt

Small, semi-transparent text in a thin strip added under each image, in the image's own
background colour, so it never covers content: dark text on light images, light on dark ones.
GIFs are stamped frame by frame (ImageMagick); MP4s get the same strip and text (ffmpeg pad
and overlay). Each file is stamped once: the manifest viz/watermarked.txt records what is done.
"""
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "viz" / "watermarked.txt"
TEXT = "medium.com/@brahimg"
FONT = "/System/Library/Fonts/Helvetica.ttc"

FILES = [
    # every image and video used in the write-up and the posts (social/ clips carry the URL already)
    "18-evals/evaluations-in-trace.png", "21-release-gate/gate.gif",
]


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def size(path):
    w, h = run("magick", "identify", "-format", "%w %h\n", f"{path}[0]").split()[:2]
    return int(w), int(h)


def style(path, w):
    """Point size, strip height, right margin, the strip colour (the image's own corner) and text colour."""
    points = max(13, round(w / 62))
    strip = 2 * round(points * 0.95)                       # even, so videos stay yuv420p-friendly
    bg = "#" + run("magick", f"{path}[0]", "-format", "%[hex:p{w-2,h-2}]", "info:")[:6]
    light = float(run("magick", f"{path}[0]", "-format", "%[fx:p{w-2,h-2}.intensity]", "info:")) > 0.5
    fill = "rgba(0,0,0,0.45)" if light else "rgba(255,255,255,0.6)"
    return points, strip, round(points * 0.8), bg, fill


def text_args(points, strip, margin, fill):
    return ["-gravity", "southeast", "-font", FONT, "-pointsize", str(points), "-fill", fill,
            "-annotate", f"+{margin}+{(strip - points) // 2}", TEXT]


def stamp_image(path):
    w, _ = size(path)
    points, strip, margin, bg, fill = style(path, w)
    grow = ["-background", bg, "-gravity", "south", "-splice", f"0x{strip}"]
    if path.suffix == ".gif":
        run("magick", str(path), "-coalesce", *grow, *text_args(points, strip, margin, fill),
            "-layers", "Optimize", str(path))
    else:
        run("magick", str(path), *grow, *text_args(points, strip, margin, fill), str(path))


def stamp_video(path):
    first = Path(tempfile.mkdtemp()) / "first.png"
    run("ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", str(first))
    w, h = size(first)
    points, strip, margin, bg, fill = style(first, w)
    overlay = first.with_name("overlay.png")
    run("magick", "-size", f"{w}x{h + strip}", "xc:none", *text_args(points, strip, margin, fill), str(overlay))
    out = path.with_name(path.stem + ".tmp.mp4")
    run("ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-i", str(overlay), "-filter_complex",
        f"[0:v]pad={w}:{h + strip}:0:0:color={bg}[v];[v][1:v]overlay=0:0,format=yuv420p",
        "-c:v", "libx264", "-crf", "24", "-movflags", "+faststart", str(out))
    out.replace(path)


if __name__ == "__main__":
    done = set(MANIFEST.read_text().split()) if MANIFEST.exists() else set()
    for rel in FILES:
        if rel in done:
            continue
        path = ROOT / rel
        (stamp_video if path.suffix == ".mp4" else stamp_image)(path)
        done.add(rel)
        MANIFEST.write_text("\n".join(sorted(done)) + "\n")
        print(f"{rel:<45} {path.stat().st_size / 1024:7.0f} KB")
