"""
Render a stored knowledge-base file into a browsable HTML page and open it.

The harvested .md is one very large file; a plain text view of it is unusable.
This renders it with a sticky contents sidebar so you can jump around, in the
same one-bit style as the app.

    python tests/preview_kb.py effect            # render and open
    python tests/preview_kb.py effect --pages 80 # cap it for a faster page
"""

import html as html_mod
import os
import re
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nh3
from markdown_it import MarkdownIt

import forge_tools as ft

NAME = sys.argv[1] if len(sys.argv) > 1 else "effect"
LIMIT = None
if "--pages" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--pages") + 1])

CSS = """
:root { --ink:#000; --paper:#fff; --rule:2px;
  --mono:"Cascadia Mono",Consolas,monospace; --body:-apple-system,"Segoe UI",sans-serif; }
@media (prefers-color-scheme: dark){ :root{ --ink:#fff; --paper:#101010; } }
*{box-sizing:border-box}
body{margin:0;display:flex;background:var(--paper);color:var(--ink);font-family:var(--body);}
nav{position:sticky;top:0;height:100vh;overflow-y:auto;flex:0 0 320px;
  border-right:var(--rule) solid var(--ink);padding:14px 12px;font-size:13px;}
nav b{display:block;font-size:15px;margin-bottom:4px}
nav .meta{font-family:var(--mono);font-size:11px;margin-bottom:12px;line-height:1.6}
nav ol{margin:0;padding-left:22px}
nav li{margin:3px 0;line-height:1.35}
nav a{color:var(--ink);text-decoration:none}
nav a:hover{background:var(--ink);color:var(--paper)}
main{flex:1 1 auto;padding:26px 34px 80px;max-width:74ch;line-height:1.62;font-size:15px}
h1{font-size:26px} h2{font-size:20px;border-bottom:var(--rule) solid var(--ink);padding-bottom:6px;margin-top:40px}
h3{font-size:16px} p{margin:0 0 13px}
a{color:var(--ink)}
code{font-family:var(--mono);font-size:13px;border:1px solid var(--ink);padding:0 4px}
pre{border:var(--rule) solid var(--ink);padding:12px;overflow-x:auto}
pre code{border:0;padding:0}
table{border-collapse:collapse;margin:0 0 14px;display:block;overflow-x:auto}
th,td{border:1px solid var(--ink);padding:5px 9px;text-align:left;vertical-align:top}
th{font-weight:700}
blockquote{margin:0 0 12px;padding-left:13px;border-left:var(--rule) solid var(--ink)}
hr{display:none}
.src{font-family:var(--mono);font-size:11px;margin:-6px 0 14px}
.warn{border:var(--rule) solid var(--ink);padding:10px 14px;margin:0 0 20px;font-weight:700}
"""


def main() -> int:
    index = ft._kb_load()
    entry = index.get(ft._kb_slug(NAME))
    if entry is None:
        print(f"nothing stored as {NAME!r}. have: {', '.join(index) or '(empty)'}")
        return 1

    body = open(entry["file"], encoding="utf-8").read()

    # One chunk per harvested page — not per "##", which also matches headings
    # inside a page's own content.
    head, pages = ft.split_pages(body)
    if LIMIT:
        pages = pages[:LIMIT]

    md = MarkdownIt("gfm-like")
    allowed = set(nh3.ALLOWED_TAGS) | {"del", "s"}
    attrs = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
    for tag in ("code", "pre", "span", "div", "table", "th", "td", "h2", "a"):
        attrs.setdefault(tag, set()).update({"class", "id"})

    nav, out = [], []
    for i, chunk in enumerate(pages):
        title = chunk.split("\n", 1)[0].lstrip("# ").strip()
        anchor = f"p{i}"
        nav.append(f'<li><a href="#{anchor}">{html_mod.escape(title)}</a></li>')
        rendered = nh3.clean(md.render(chunk), tags=allowed, attributes=attrs)
        # Anchor the page's own heading so the sidebar can reach it.
        rendered = rendered.replace("<h2>", f'<h2 id="{anchor}">', 1)
        out.append(rendered)

    warn = ""
    if not entry.get("complete", True):
        warn = ('<div class="warn">INCOMPLETE — this harvest hit its page limit. '
                'Re-run harvest_docs with a higher max_pages for the rest.</div>')

    meta = (f"{entry['pages']} pages · {entry['characters']:,} chars<br>"
            f"via {entry['strategy']} · {entry['harvested']}<br>"
            f"{html_mod.escape(entry['source'])}")
    shown = f"showing {len(pages)} of {entry['pages']}" if LIMIT else f"all {len(pages)} pages"

    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html_mod.escape(entry['name'])} — DocsForge knowledge base</title>"
        f"<style>{CSS}</style>"
        f"<nav><b>{html_mod.escape(entry['name'])}</b>"
        f"<div class='meta'>{meta}<br>{shown}</div><ol>{''.join(nav)}</ol></nav>"
        f"<main>{warn}{nh3.clean(md.render(head), tags=allowed, attributes=attrs)}"
        f"{''.join(out)}</main>"
    )

    target = os.path.join(os.path.dirname(entry["file"]), f"{entry['name']}-preview.html")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(page)

    print(f"{entry['pages']} pages, {entry['characters']:,} chars of Markdown")
    print(f"rendered {len(pages)} pages -> {target} ({len(page):,} bytes of HTML)")
    webbrowser.open(f"file:///{target.replace(os.sep, '/')}")
    print("opened in your browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
