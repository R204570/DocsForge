"""Live web smoke test — hits a running app.py and drives one real Groq turn.

Not collected by pytest (needs the server up, a GROQ_API_KEY, and the network).
Run:  python app.py --port 8123
      python tests/smoke_web.py [port] ["your prompt"]
"""

import json
import sys

import requests

PORT = sys.argv[1] if len(sys.argv) > 1 else "8123"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else (
    "Fetch https://petstore3.swagger.io/api/v3/openapi.json and give me a short "
    "Markdown table of the first 5 endpoints with their summaries."
)
BASE = f"http://127.0.0.1:{PORT}"


def check_config():
    cfg = requests.get(f"{BASE}/api/config", timeout=10).json()
    print(f"model      : {cfg['model']}")
    print(f"groq_ready : {cfg['groq_ready']}")
    print(f"tools      : {', '.join(t['name'] for t in cfg['tools'])}")
    return cfg


def check_index():
    r = requests.get(BASE + "/", timeout=10)
    assert r.status_code == 200, r.status_code
    assert "DocsForge" in r.text
    for asset in ("/static/style.css", "/static/app.js"):
        a = requests.get(BASE + asset, timeout=10)
        assert a.status_code == 200, f"{asset} -> {a.status_code}"
    print("index + assets: ok")


def check_render():
    r = requests.post(f"{BASE}/api/render",
                      json={"markdown": "# Hi\n\n<script>alert(1)</script>\n\n| a |\n|---|\n| 1 |"},
                      timeout=10).json()
    assert "<h1>" in r["html"], r["html"]
    assert "<table>" in r["html"], r["html"]
    assert "<script>" not in r["html"], "sanitiser let a script through!"
    print("render + sanitise: ok")


def drive_chat(prompt):
    print(f"\n> {prompt}\n")
    body = {"messages": [{"role": "user", "content": prompt}]}
    tokens = 0
    final = None

    with requests.post(f"{BASE}/api/chat", json=body, stream=True, timeout=300) as res:
        res.raise_for_status()
        buffer = ""
        for raw in res.iter_content(chunk_size=None):
            buffer += raw.decode("utf-8", "replace")
            blocks = buffer.split("\n\n")
            buffer = blocks.pop()
            for block in blocks:
                event, data = "message", []
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                if not data:
                    continue
                payload = json.loads("\n".join(data))

                if event == "token":
                    tokens += 1
                    sys.stdout.write(payload["text"])
                    sys.stdout.flush()
                elif event == "tool":
                    if payload["phase"] == "start":
                        print(f"\n  [tool] {payload['name']} {payload.get('args')}")
                    else:
                        status = "ok" if payload["ok"] else "FAILED"
                        kind = payload.get("kind") or "-"
                        print(f"  [tool] {payload['name']} -> {status}, "
                              f"{payload['chars']:,} chars, kind={kind}")
                        if not payload["ok"]:
                            print(f"         {payload['preview']}")
                elif event == "done":
                    final = payload
                elif event == "error":
                    print(f"\n!! error: {payload['message']}")
                    return None

    print("\n" + "-" * 60)
    if final is None:
        print("no done event received")
        return None
    print(f"token events : {tokens}")
    print(f"markdown     : {len(final['markdown']):,} chars")
    print(f"html         : {len(final['html']):,} chars")
    assert "<script>" not in final["html"]
    return final


if __name__ == "__main__":
    cfg = check_config()
    check_index()
    check_render()
    if not cfg["groq_ready"]:
        print("\nGROQ_API_KEY missing — skipping the live chat turn.")
        sys.exit(1)
    result = drive_chat(PROMPT)
    sys.exit(0 if result else 1)
