"""Capture the DocsStore inspection round in Chromium.

Drives the real /library page against the real store: the box, an opened
divider showing every crawled version, a version being read, and a search
inside one. Desktop and narrow widths in one round, both themes probed by
computed style rather than by eye.

Run:  python app.py --port 8011
      python tests/shoot_store.py [port] [--out DIR]
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

args = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT = args[0] if args else "8011"
BASE = f"http://127.0.0.1:{PORT}/library"
OUT = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else Path("shots")
OUT.mkdir(parents=True, exist_ok=True)

# The technology to drill into. Picked for having more than one crawled
# version, which is the whole point of the screen.
TECH = "pydantic"
BIG = "effect"          # 703 pages: the index and the reader under real load


def shoot(page, name):
    page.wait_for_timeout(420)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print("  ", name)


def probe(page):
    """Read the built result, not the intention."""
    return page.evaluate("""() => {
      const cs = (el) => el ? getComputedStyle(el) : null;
      const body = cs(document.body);
      const chip = cs(document.querySelector('.chip'));
      const card = cs(document.querySelector('.card'));
      return {
        overflowX: document.documentElement.scrollWidth > window.innerWidth + 1,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        bodyBg: body.backgroundColor, bodyFg: body.color,
        chipBg: chip && chip.backgroundColor, chipFg: chip && chip.color,
        cardBg: card && card.backgroundColor,
        dividers: document.querySelectorAll('#dividers .index-card').length,
        pager: (document.querySelector('#pg-count') || {}).textContent,
        prevDisabled: (document.querySelector('[data-page="prev"]') || {}).disabled,
        nextDisabled: (document.querySelector('[data-page="next"]') || {}).disabled,
        backend: (document.querySelector('#backend') || {}).textContent,
        title: (document.querySelector('.card-title') || {}).textContent,
        versions: document.querySelectorAll('.versions .version').length,
        tabs: document.querySelectorAll('.tab').length,
        toc: document.querySelectorAll('#toc button').length,
        marks: document.querySelectorAll('#toc mark').length,
        reading: (document.querySelector('.page-head h2') || {}).textContent,
      };
    }""")


def run(pw, width, height, theme, tag):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": width, "height": height},
                            color_scheme=theme)
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))

    print(f"\n{tag}  {width}x{height}  {theme}")

    # 1 — the box
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#dividers .index-card")
    shoot(page, f"{tag}-1-box")
    seen = probe(page)

    # 2 — page two of the box, so the pager is exercised, not just drawn
    page.click('[data-page="next"]')
    page.wait_for_timeout(500)
    shoot(page, f"{tag}-2-box-page2")
    paged = probe(page)
    page.click('[data-page="prev"]')
    page.wait_for_timeout(400)

    # 3 — a divider opened: every crawled version of one technology
    page.goto(f"{BASE}#/{TECH}", wait_until="networkidle")
    page.wait_for_selector(".versions .version")
    shoot(page, f"{tag}-3-versions")
    versions = probe(page)

    # 4 — reading a version
    page.click(".versions .version")
    page.wait_for_selector("#toc button")
    page.wait_for_timeout(300)
    page.click("#toc button")
    page.wait_for_selector(".page-head h2")
    shoot(page, f"{tag}-4-reading")
    reading = probe(page)

    # 5 — a big one, searched
    page.goto(f"{BASE}#/{BIG}", wait_until="networkidle")
    page.wait_for_selector(".versions .version")
    page.click(".versions .version")
    page.wait_for_selector("#toc button")
    page.fill("#find-text", "exponential backoff")
    page.wait_for_timeout(900)
    page.wait_for_selector("#toc mark")
    page.click("#toc button:has(mark)")
    page.wait_for_selector(".page-head h2")
    shoot(page, f"{tag}-5-search")
    searched = probe(page)

    browser.close()
    return {"box": seen, "paged": paged, "versions": versions,
            "reading": reading, "searched": searched, "errors": errors}


with sync_playwright() as pw:
    results = {
        "desktop": run(pw, 1280, 860, "light", "desktop"),
        "dark": run(pw, 1280, 860, "dark", "dark"),
        "narrow": run(pw, 900, 800, "light", "narrow"),
        "mobile": run(pw, 420, 760, "light", "mobile"),
    }

print("\n" + json.dumps(results, indent=1))

bad = []
for name, r in results.items():
    for state, p in r.items():
        if state == "errors":
            continue
        if p["overflowX"]:
            bad.append(f"{name}/{state}: horizontal overflow "
                       f"({p['scrollWidth']} > {p['innerWidth']})")
    if r["errors"]:
        bad.append(f"{name}: console errors {r['errors']}")

print("\n" + ("FAIL\n  " + "\n  ".join(bad) if bad else "clean: no overflow, no console errors"))
sys.exit(1 if bad else 0)
