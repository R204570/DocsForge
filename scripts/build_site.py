"""
Build the public site from its sources.

The three pages under docsforge/server/site/ are hand-authored in
scripts/site_src/: one HTML file per page, one stylesheet (site.css), one
behaviour script (site.js), and the client picker for /connect
(picker.html). This assembles them into what the server serves: the shared
nav and footer stamped into each page, the stylesheet and script inlined so
every page is self-contained (fonts still come from Google Fonts, with a
system fallback), and two placeholders left for the server to fill at
request time — `{{VERSION}}` from the package and `{{BASE_URL}}` from the
request.

    python scripts/build_site.py

No toolchain: plain files in, plain files out. Re-run after any edit.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "site_src"))
from diagrams import DIAGRAMS  # noqa: E402

REPO = "https://github.com/R204570/DocsForge"
ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "site_src"
SITE = ROOT / "docsforge" / "server" / "site"

PAGES = ("index", "tools", "connect")


def nav(active: str) -> str:
    """One nav for every page. `active` is the page's file name."""
    items = [("How it works", "/#how-it-works", "wide", ""),
             ("Tools", "/tools", "", "tools"),
             ("Connect", "/connect", "", "connect"),
             ("Storage", "/connect#storage", "wide", "")]
    links = []
    for label, href, cls, page in items:
        on = page == active
        attrs = f' class="{cls}"' if cls else ""
        attrs += ' aria-current="page"' if on else ""
        links.append(f'      <a href="{href}"{attrs}>{label}</a>')
    links.append(f'      <a href="{REPO}" class="ext" target="_blank" rel="noopener noreferrer">GitHub '
                 '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 8 L8 2 M3.5 2 H8 V6.5"/></svg></a>')
    return ('<header class="top">\n  <div class="wrap">\n'
            '    <a href="/" class="mark"><i></i>DocsForge</a>\n'
            '    <nav class="nav" aria-label="Site">\n' + "\n".join(links) + '\n    </nav>\n'
            '    <span class="chip">v{{VERSION}}</span>\n'
            '  </div>\n</header>')


FOOT = f'''<footer class="foot">
  <div class="wrap">
    <div class="l"><b>DocsForge</b><span>MIT License</span><span>Built by Raj Patel</span><span class="note">Bugs, bad extractions, ideas — open an issue.</span></div>
    <nav aria-label="Project">
      <a href="{REPO}" target="_blank" rel="noopener noreferrer">GitHub</a>
      <a href="{REPO}/issues" target="_blank" rel="noopener noreferrer">Issues</a>
      <a href="{REPO}#readme" target="_blank" rel="noopener noreferrer">Contribute</a>
    </nav>
  </div>
</footer>'''


def build(page: str, css: str, js: str, picker: str) -> str:
    html = (SRC / f"{page}.html").read_text(encoding="utf-8")
    for name, draw in DIAGRAMS.items():
        html = html.replace(f"<!--SVG:{name}-->", draw())
    assert "<!--SVG:" not in html, (page, "an unknown diagram marker")
    for marker, value in (("<!--STYLE-->", f"<style>\n{css}\n  </style>"),
                          ("<!--NAV-->", nav(page)),
                          ("<!--FOOT-->", FOOT),
                          ("<!--PICKER-->", picker if page == "connect" else ""),
                          ("<!--SCRIPT-->", f"<script>\n{js}\n</script>")):
        assert html.count(marker) == (1 if marker != "<!--PICKER-->" or page == "connect" else 0) \
            or marker == "<!--PICKER-->", (page, marker)
        html = html.replace(marker, value)
    # what must hold for every served page
    assert "<script src=" not in html, (page, "an external script")
    assert "{{VERSION}}" in html, (page, "version chip placeholder")
    assert ("{{BASE_URL}}" in html) == (page == "connect"), (page, "base url placeholder")
    assert 'href="#"' not in html, (page, "unresolved href")
    assert "YOUR-HOST" not in html, (page, "host placeholder")
    return html


def main() -> int:
    css = (SRC / "site.css").read_text(encoding="utf-8")
    js = (SRC / "site.js").read_text(encoding="utf-8")
    picker = (SRC / "picker.html").read_text(encoding="utf-8")
    SITE.mkdir(exist_ok=True)
    for page in PAGES:
        html = build(page, css, js, picker)
        (SITE / f"{page}.html").write_text(html, encoding="utf-8")
        print(f"{page}.html  {len(html):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
