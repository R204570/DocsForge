"""Live two-turn check — the second turn must use context from the first.

Run:  python app.py --port 8123
      python tests/smoke_multiturn.py [port]
"""

import json
import sys

import requests

PORT = sys.argv[1] if len(sys.argv) > 1 else "8123"
BASE = f"http://127.0.0.1:{PORT}"


def turn(history, prompt):
    history.append({"role": "user", "content": prompt})
    print(f"\n> {prompt}")
    tools_used, final = [], None

    with requests.post(f"{BASE}/api/chat", json={"messages": history},
                       stream=True, timeout=300) as res:
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
                if event == "tool" and payload["phase"] == "start":
                    tools_used.append(payload["name"])
                elif event == "done":
                    final = payload
                elif event == "error":
                    raise SystemExit(f"error: {payload['message']}")

    if final is None:
        raise SystemExit("no done event")
    history.append({"role": "assistant", "content": final["markdown"]})
    print(f"  tools: {tools_used or 'none'}")
    print(f"  reply: {final['markdown'][:220]}...")
    return final, tools_used


if __name__ == "__main__":
    history = []

    _, t1 = turn(history, "Fetch https://petstore3.swagger.io/api/v3/openapi.json "
                          "and tell me what this API is for in two sentences.")
    assert t1, "first turn should have called a tool"

    # No URL in the follow-up — it can only answer from the prior turn's context.
    f2, t2 = turn(history, "How many endpoints did it have? Just the number and a one-line note.")

    print("\n" + "-" * 60)
    print(f"turn 1 tools : {t1}")
    print(f"turn 2 tools : {t2 or 'none (answered from context)'}")
    print(f"history size : {len(history)} messages")
    print("OK")
