"""
Drive DocsForge in a real, visible browser window — a demo you can watch.

Unlike tests/shoot_ui.py this runs headed and maximized, types at human speed,
and leaves the window open when it finishes so you can keep using it.

    python tests/demo_drive.py 8010 "your question here"
"""

import sys
import time

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8010"
QUESTION = sys.argv[2] if len(sys.argv) > 2 else (
    "Fetch https://petstore3.swagger.io/api/v3/openapi.json and give me a "
    "Markdown table of the pet endpoints with what each one does."
)
PROVIDER = sys.argv[3] if len(sys.argv) > 3 else "groq"
HOLD = int(sys.argv[4]) if len(sys.argv) > 4 else 3600  # keep the window open

BASE = f"http://127.0.0.1:{PORT}"


def say(msg: str) -> None:
    print(msg, flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
    )
    # viewport=None keeps the real window size instead of a fixed 1280x720 box.
    page = browser.new_context(viewport=None).new_page()

    say("opening DocsForge…")
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#model-chip:not(:empty)")
    time.sleep(3)  # let the landing card be read

    # 1. show the provider switcher
    say("opening the model switcher")
    page.click("#model-chip")
    time.sleep(3)

    say(f"choosing {PROVIDER}")
    target = page.query_selector(f'#model-menu button[data-arg="{PROVIDER}"]')
    if target and not target.is_disabled():
        target.click()
    else:
        page.keyboard.press("Escape")
        say(f"  {PROVIDER} unavailable — staying on the default")
    time.sleep(2)
    say("model chip now reads: " + page.text_content("#model-chip").strip())

    # 2. type the question at human speed
    say("typing the question…")
    page.click("#input")
    page.type("#input", QUESTION, delay=38)
    time.sleep(1.5)

    say("sending")
    page.click("#send")

    # 3. narrate what the page does
    try:
        page.wait_for_selector(".sources li", timeout=120_000)
        say("  tool call started — fetching the docs")
    except Exception:
        say("  (no tool call yet)")

    try:
        page.wait_for_selector(".card-foot .btn:not(:disabled)", timeout=600_000)
        say("  answer complete")
    except Exception:
        say("  still working when the wait expired")

    time.sleep(2)

    # 4. scroll the answer so the whole card is seen from across the room
    field = page.query_selector(".card-field")
    if field:
        for _ in range(6):
            page.mouse.wheel(0, 320)
            time.sleep(1.1)
        time.sleep(1.5)
        page.mouse.wheel(0, -4000)

    title = page.query_selector(".card-title")
    say("card title: " + (title.inner_text().strip() if title else "?"))
    say(f"done — leaving the window open for {HOLD}s. Close it any time.")
    sys.stdout.flush()

    time.sleep(HOLD)
    browser.close()
