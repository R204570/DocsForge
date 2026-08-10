"""Capture the UI inspection round in Chromium.

By default /api/chat is stubbed with a canned SSE stream: deterministic, and it
spends no Groq tokens while still driving the real client code end to end.
Pass --live to run a genuine model turn instead.

Run:  python app.py --port 8123
      python tests/shoot_ui.py [port] [--live]
"""

import json
import sys

import requests
from playwright.sync_api import sync_playwright

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LIVE = "--live" in sys.argv
PORT = args[0] if args else "8123"
BASE = f"http://127.0.0.1:{PORT}"

PROMPT = ("Fetch https://petstore3.swagger.io/api/v3/openapi.json and give me a table "
          "of 4 pet endpoints, what the API does, and a python example.")
PROMPT2 = "Now list just the HTTP methods you saw, one per line."

CANNED = """## Swagger Petstore

The Petstore API is a sample service for managing pets, store orders, and user
accounts. It is published as an OpenAPI 3.0 document, so every endpoint below
came out of the spec itself rather than out of the model's memory.

### Pet endpoints

| Endpoint | Method | Summary |
|---|---|---|
| `/pet` | POST | Add a new pet to the store |
| `/pet` | PUT | Update an existing pet |
| `/pet/findByStatus` | GET | Find pets by status |
| `/pet/{petId}` | GET | Find a pet by its id |

### What it does

- Manage pets: create, read, update and delete.
- Upload an image against a pet record.
- Place and inspect store orders.
- Register users and log them in and out.

### Example request

```python
import requests

r = requests.get(
    "https://petstore3.swagger.io/api/v3/pet/findByStatus",
    params={"status": "available"},
    timeout=10,
)
r.raise_for_status()
print(r.json()[:3])
```

> The spec declares 19 paths in total; only the pet group is shown here.
"""

CANNED2 = """### Methods in the spec

- `GET`
- `POST`
- `PUT`
- `DELETE`
"""


def sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def canned_stream(markdown, html, url, cut=None):
    """cut=N stops after N characters of markdown and never sends `done`, so
    the mid-flight painting state can actually be photographed."""
    parts = [
        sse("tool", {"phase": "start", "name": "fetch_docs", "args": {"url": url}}),
    ]
    if cut is not None:
        # Leave the tool running and the field half-painted.
        for i in range(0, cut, 90):
            parts.append(sse("token", {"text": markdown[i:i + 90]}))
        return "".join(parts)

    parts.append(sse("tool", {"phase": "end", "name": "fetch_docs", "ok": True,
                              "chars": 8761, "kind": "openapi", "preview": ""}))
    step = 90
    for i in range(0, len(markdown), step):
        parts.append(sse("token", {"text": markdown[i:i + step]}))
    parts.append(sse("done", {"markdown": markdown, "html": html}))
    return "".join(parts)


def render(markdown):
    return requests.post(f"{BASE}/api/render", json={"markdown": markdown}, timeout=20).json()["html"]


errors = []


def watch(page):
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))


def stub(page, bodies):
    """Serve each canned stream in turn; the last one repeats."""
    turn = {"n": 0}

    def handler(route):
        i = min(turn["n"], len(bodies) - 1)
        turn["n"] += 1
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                      body=bodies[i])
    page.route("**/api/chat", handler)


with sync_playwright() as p:
    bodies = []
    if not LIVE:
        bodies = [
            canned_stream(CANNED, render(CANNED), "https://petstore3.swagger.io/api/v3/openapi.json"),
            canned_stream(CANNED2, render(CANNED2), "https://petstore3.swagger.io/api/v3/openapi.json"),
        ]

    browser = p.chromium.launch()

    # ── desktop ───────────────────────────────────────────
    page = browser.new_page(viewport={"width": 1280, "height": 880})
    watch(page)
    if not LIVE:
        stub(page, bodies)
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#model-chip:not(:empty)")
    page.wait_for_timeout(400)
    page.screenshot(path="shot-1-landing.png")
    print("shot-1-landing.png     — intro card, empty shoebox")

    page.click('.menu[data-menu="file"] .menu-title')
    page.wait_for_timeout(250)
    page.screenshot(path="shot-2-menu.png")
    print("shot-2-menu.png        — File menu open")
    page.keyboard.press("Escape")

    # The painting state needs its own page: a completed stream races past it.
    if not LIVE:
        paint = browser.new_page(viewport={"width": 1280, "height": 880})
        watch(paint)
        stub(paint, [canned_stream(CANNED, "", "https://petstore3.swagger.io/api/v3/openapi.json", cut=430)])
        paint.goto(BASE, wait_until="networkidle")
        paint.fill("#input", PROMPT)
        paint.click("#send")
        paint.wait_for_selector(".sources li.running", timeout=90000)
        paint.wait_for_timeout(900)
        paint.screenshot(path="shot-3-painting.png")
        print("shot-3-painting.png    — tool running, raw markdown mid-stream")
        paint.close()

    page.fill("#input", PROMPT)
    page.click("#send")
    if LIVE:
        page.wait_for_selector(".sources li", timeout=90000)
        page.wait_for_timeout(700)
        page.screenshot(path="shot-3-painting.png")
        print("shot-3-painting.png    — tool row, raw markdown streaming")

    page.wait_for_selector(".card-foot .btn:not(:disabled)", timeout=180000)
    page.wait_for_timeout(900)
    page.screenshot(path="shot-4-card.png")
    print("shot-4-card.png        — rendered card, sources, foot")

    page.click('.card-foot button:has-text("Edit This Card")')
    page.wait_for_timeout(450)
    page.screenshot(path="shot-5-author.png")
    print("shot-5-author.png      — editing the markdown in place")
    authoring = page.is_visible("textarea.author")
    page.click('.card-foot button:has-text("Done Editing")')
    page.wait_for_timeout(600)

    page.fill("#input", PROMPT2)
    page.click("#send")
    page.wait_for_timeout(600)
    try:
        page.wait_for_function(
            "document.querySelectorAll('.index li').length >= 2 && "
            "!document.querySelector('.index-card.painting')", timeout=180000)
    except Exception as exc:
        print(f"  (second card wait: {exc})")
    page.wait_for_timeout(700)
    page.screenshot(path="shot-6-stack.png")
    print("shot-6-stack.png       — two cards in the shoebox")

    cards = page.eval_on_selector_all(".index li", "n => n.length")
    deep = page.evaluate("location.hash")
    dup = page.evaluate(
        "(() => {const t=document.querySelector('.card-title');"
        "const h=document.querySelector('.card-field .md :is(h1,h2,h3)');"
        "return !!(t&&h&&t.textContent.trim()===h.textContent.trim());})()")

    # ── mobile, same round ────────────────────────────────
    m = browser.new_page(viewport={"width": 420, "height": 880})
    watch(m)
    if not LIVE:
        stub(m, bodies)
    m.goto(BASE, wait_until="networkidle")
    m.wait_for_selector(".wordmark")
    m.wait_for_timeout(400)
    m.screenshot(path="shot-7-mobile.png")
    print("shot-7-mobile.png      — 420px docked width")

    m.fill("#input", PROMPT)
    m.click("#send")
    try:
        m.wait_for_selector(".card-foot .btn:not(:disabled)", timeout=180000)
        m.wait_for_timeout(800)
    except Exception as exc:
        print(f"  (mobile answer wait: {exc})")
    m.screenshot(path="shot-8-mobile-card.png")
    print("shot-8-mobile-card.png — answered at 420px")

    overflow = {
        "desktop": page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
        "mobile": m.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
    }

    # ── dark host: the world must inverse, not recolour ───
    d = browser.new_page(viewport={"width": 1280, "height": 880}, color_scheme="dark")
    watch(d)
    if not LIVE:
        stub(d, bodies)
    d.goto(BASE, wait_until="networkidle")
    d.fill("#input", PROMPT)
    d.click("#send")
    try:
        d.wait_for_selector(".card-foot .btn:not(:disabled)", timeout=180000)
        d.wait_for_timeout(900)
    except Exception as exc:
        print(f"  (dark answer wait: {exc})")
    d.screenshot(path="shot-9-dark.png")
    print("shot-9-dark.png        — inverted for a dark host")

    browser.close()

print(f"\nmode                : {'live groq' if LIVE else 'stubbed stream'}")
print(f"author mode engaged : {authoring}")
print(f"cards in shoebox    : {cards}")
print(f"deep link           : {deep}")
print(f"title duplicated    : {dup}")
print(f"horizontal overflow : {overflow}")
print(f"console/page errors : {errors or 'none'}")
sys.exit(1 if errors else 0)
