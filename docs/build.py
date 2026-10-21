"""Build docs/index.html (the GitHub Pages write-up) from docs/article.md.

    python docs/build.py
"""
import html
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
text = (HERE / "article.md").read_text()
title = text.splitlines()[0].lstrip("# ").strip()
body = markdown.markdown(text.split("\n", 1)[1], extensions=["fenced_code"]).replace("<hr />", "")
desc = ("Part 4 of Building an AI agent from first principles: a refund agent gets a referee: checks from traces, "
        "people's labels, a calibrated LLM judge, online evals, curated cases and a release gate, all from real runs.")
og = "https://raw.githubusercontent.com/brahim-guaali/agent-evals-from-first-principles/main/21-release-gate/gate.gif"
page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="article">
<meta property="og:image" content="{og}">
<meta name="author" content="Brahim Guaali">
<style>
  :root {{ --text:#1a1a19; --muted:#52514e; --bg:#fcfcfb; --code:#f0efec; --link:#2a78d6; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --text:#f2f2f0; --muted:#c3c2b7; --bg:#151514; --code:#232321; --link:#5a9bea; }} }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:19px/1.65 Georgia, "Times New Roman", serif; }}
  article {{ max-width: 760px; margin: 0 auto; padding: 48px 16px 96px; }}
  h1 {{ font: 700 40px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0 0 12px; }}
  h2 {{ font: 700 26px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 44px 0 8px; }}
  a {{ color: var(--link); }}
  img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 12px 0 0; display: block; }}
  img + em, p > em:only-child {{ display: block; color: var(--muted); font-size: 15px; line-height: 1.5; }}
  pre {{ background: var(--code); padding: 14px 16px; border-radius: 6px; overflow-x: auto; font-size: 15px; line-height: 1.5; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; background: var(--code); padding: 1px 4px; border-radius: 4px; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ margin: 16px 0; padding-left: 18px; border-left: 3px solid var(--muted); font-style: italic; }}
  .by {{ color: var(--muted); font: 15px -apple-system, BlinkMacSystemFont, sans-serif; margin-bottom: 32px; }}
</style>
</head>
<body>
<article>
<h1>{html.escape(title)}</h1>
<div class="by">Brahim Guaali · <a href="https://github.com/brahim-guaali/agent-evals-from-first-principles">code on GitHub</a></div>
{body}
</article>
</body>
</html>
"""
(HERE / "index.html").write_text(page)
print("wrote docs/index.html")
