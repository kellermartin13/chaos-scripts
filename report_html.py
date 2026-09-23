#!/usr/bin/env python3

"""
Shared HTML shell for the report pages published to GitHub Pages.

`document(title, body_html, generated)` wraps caller-provided body HTML in a
self-contained page (embedded CSS, no external assets) styled to match the
trade-review report: dark theme, cards, tables, badges, responsive, and
WCAG-minded (lang, semantic headings/tables, text-not-color badges, AA
contrast). Callers are responsible for html-escaping their content.
"""

import html
from datetime import datetime, timezone


CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0d1117; color:#e6edf3; line-height:1.5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }
  header.page { border-bottom:1px solid #30363d; padding-bottom:1rem; margin-bottom:1.5rem; }
  h1 { font-size:1.6rem; margin:0 0 .35rem; }
  h2 { font-size:1.15rem; margin:2rem 0 .75rem; padding-bottom:.35rem; border-bottom:1px solid #21262d; }
  h3 { font-size:1rem; margin:0; }
  .meta { color:#8b949e; font-size:.9rem; }
  a { color:#58a6ff; }
  .muted { color:#8b949e; }
  .num { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

  .card { border:1px solid #30363d; border-radius:10px; padding:1rem 1.1rem; margin:0 0 1rem; background:#0f141a; }
  .card.flip { border-left:3px solid #d29922; }
  .card .thead { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin-bottom:.6rem; }

  .badge { display:inline-block; padding:.08rem .5rem; border-radius:999px; font-size:.72rem; font-weight:700; letter-spacing:.03em; }
  .badge.flip { background:#9e6a03; color:#fff; }
  .badge.up { background:#238636; color:#fff; }
  .badge.down { background:#da3633; color:#fff; }
  .tag.win { background:#238636; color:#fff; padding:.05rem .45rem; border-radius:6px; font-size:.72rem; font-weight:700; }

  .side { border:1px solid #21262d; border-radius:8px; padding:.5rem .75rem; margin:.4rem 0; }
  .side.win { border-left:3px solid #238636; }
  .side-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem; }
  .team { font-weight:600; }

  .callout { border:1px solid #30363d; border-radius:10px; padding:.9rem 1.1rem; background:#0f141a; }
  .callout.changed { border-left:3px solid #d29922; }

  table { border-collapse:collapse; width:100%; margin-top:.5rem; font-size:.9rem; }
  caption { text-align:left; color:#8b949e; font-size:.82rem; margin-bottom:.4rem; }
  th, td { text-align:left; padding:.4rem .6rem; border-bottom:1px solid #21262d; }
  th[scope="col"] { color:#adbac7; border-bottom:1px solid #30363d; }
  td.num, th.num { text-align:right; }
  tbody tr:hover { background:#11161d; }

  pre { background:#0f141a; border:1px solid #21262d; border-radius:8px; padding:1rem;
    overflow-x:auto; font-size:.8rem; line-height:1.4; white-space:pre; }
  details > summary { cursor:pointer; color:#adbac7; margin:.5rem 0; }

  @media (max-width:600px) { h1 { font-size:1.35rem; } }
"""


def document(title, body_html, generated=None):
    """Full HTML document: <head> with embedded CSS + the given body HTML."""

    if generated is None:
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    safe_title = html.escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{CSS}</style>
</head>
<body>
<main>
{body_html}
<footer class="meta" style="margin-top:2rem;border-top:1px solid #30363d;padding-top:1rem;">
Generated {html.escape(generated)} · League of Chaos
</footer>
</main>
</body>
</html>
"""
