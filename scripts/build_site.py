"""
Build the public site from Stitch's exports.

The three pages under docsforge/server/site/ were designed in Stitch (project
"DocsForge Landing", design system "DocsForge Dark" — the web chat's palette).
Stitch exports HTML that leans on the Tailwind play-CDN and leaves nav and
footer links as placeholders. This turns those exports into what the server
serves: one nav across the pages, real links, the version chip as a
placeholder the server fills, and Tailwind compiled to a static stylesheet so
each page is self-contained (fonts still come from Google Fonts, with a system
fallback).

    python scripts/build_site.py scripts/site_exports

`scripts/site_exports/` holds the raw exports (home.html, tools.html,
connect.html); replace one with a fresh download to change a page.

Needs Node (npx) for the Tailwind compiler; nothing else. Re-run after editing
a page in Stitch and downloading its HTML again.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = "https://github.com/R204570/DocsForge"
ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "docsforge" / "server" / "site"
TAILWIND = "tailwindcss@3.4.17"

#: Stitch export name -> served file name.
PAGES = {"home": "index", "tools": "tools", "connect": "connect"}


def nav(active: str) -> str:
    """One nav for every page. `active` is the page's own label, lower-case."""
    items = [("How it works", "/#how-it-works", "hidden md:inline"),
             ("Tools", "/tools", ""),
             ("Connect", "/connect", ""),
             ("Storage", "/connect#storage", "hidden md:inline")]
    out = ['<nav class="flex items-center gap-4 md:gap-7 text-sm font-medium text-[#a9a9b3]">']
    for label, href, extra in items:
        on = label.lower() == active
        cls = ("text-[#ececee] border-b border-[#cf9fff] pb-px" if on
               else "hover:text-[#ececee] transition-colors")
        current = ' aria-current="page"' if on else ""
        out.append(f'        <a href="{href}" class="{extra + " " if extra else ""}{cls}"{current}>{label}</a>')
    out.append(f'        <a href="{REPO}" target="_blank" rel="noopener noreferrer" '
               f'class="hover:text-[#ececee] transition-colors flex items-center gap-1">GitHub '
               f'<span class="font-mono text-[10px]">↗</span></a>')
    out.append("      </nav>")
    return "\n".join(out)


def normalise(page: str, html: str) -> str:
    """Real links, one nav, the version chip as a placeholder."""
    html, n = re.subn(r"<nav\b[^>]*>.*?</nav>", nav("" if page == "home" else page),
                      html, count=1, flags=re.S)
    assert n == 1, (page, "nav")
    # wordmark -> home
    html, n = re.subn(r'<a href="#"(\s+class="[^"]*flex items-center gap-2[^"]*")', r'<a href="/"\1', html, count=1)
    assert n == 1, (page, "wordmark")
    # the hero CTA on the home page
    html = html.replace('href="#connect"', 'href="/connect"')
    # GitHub placeholders
    html = html.replace('href="https://github.com/issues"', f'href="{REPO}/issues"')
    html = html.replace('href="https://github.com"', f'href="{REPO}"')
    for label, href in (("GitHub", REPO), ("Issues", f"{REPO}/issues"), ("Contribute", f"{REPO}#readme")):
        html = re.sub(rf'<a href="#"(\s+class="[^"]*")>{label}</a>',
                      rf'<a href="{href}" target="_blank" rel="noopener noreferrer"\1>{label}</a>', html)
    # the chip crowds a 400px header: below `sm` the nav matters more
    chip = re.compile(r'<div class="flex items-center">(\s*<span class="font-mono[^>]*>\s*v\d+\.\d+\.\d+)')
    assert len(chip.findall(html)) == 1, (page, "chip wrapper")
    html = chip.sub(r'<div class="hidden sm:flex items-center">\1', html)
    # the server fills the version in from the package
    html, n = re.subn(r"(?<![\w.])v\d+\.\d+\.\d+(?![\w.])", "v{{VERSION}}", html)
    assert n == 1, (page, "version chip", n)
    assert 'href="#"' not in html, (page, "unresolved href")
    return html


def compile_tailwind(page: str, html: str, work: pathlib.Path) -> str:
    """Replace the play-CDN with a compiled, inlined stylesheet.

    Each page carries its own `tailwind.config`, and they do not agree on
    names (`brand.surface` differs between two of them), so each is compiled
    against its own config rather than a merge.
    """
    m = re.search(r"tailwind\.config\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    assert m, (page, "no tailwind.config")
    src = work / f"{page}.src.html"
    src.write_text(html, encoding="utf-8")
    cfg = work / f"{page}.config.cjs"
    cfg.write_text(f"module.exports = Object.assign({m.group(1)}, "
                   f"{{ content: [{src.as_posix()!r}] }});\n", encoding="utf-8")
    (work / "in.css").write_text("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n",
                                 encoding="utf-8")
    out = work / f"{page}.css"
    r = subprocess.run(["npx", "-y", TAILWIND, "-c", str(cfg), "-i", str(work / "in.css"),
                        "-o", str(out), "--minify"], capture_output=True, text=True,
                       shell=sys.platform == "win32")
    if r.returncode:
        sys.exit(f"tailwind failed for {page}:\n{r.stdout}\n{r.stderr}")
    css = out.read_text(encoding="utf-8")
    html, n1 = re.subn(r'\s*<script src="https://cdn\.tailwindcss\.com"></script>', "", html)
    html, n2 = re.subn(r"\s*<script>\s*tailwind\.config\s*=.*?</script>", "", html, flags=re.S)
    assert n1 == 1 and n2 == 1, (page, "cdn/config removal", n1, n2)
    html = html.replace("</head>", f"  <style>{css}</style>\n</head>", 1)
    assert "<script" not in html, (page, "a script survived")
    return html


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    exports = pathlib.Path(sys.argv[1]).resolve()
    work = exports / "_build"
    work.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)
    for page, served in PAGES.items():
        html = (exports / f"{page}.html").read_text(encoding="utf-8")
        html = compile_tailwind(page, normalise(page, html), work)
        (SITE / f"{served}.html").write_text(html, encoding="utf-8")
        print(f"{served}.html  {len(html):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
