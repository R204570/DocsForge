"""
The test lab: a signed-in panel inside the local web app for testing DocsForge
by hand while the machine does the running.

Three things are tested, each the way a person would check it:

  resolve   a name -> the documentation URL it resolves to, and that page
            fetched, so a tester can say whether both are right
  fetch     one URL -> the Markdown extracted from it, beside the live page
  harvest   a name or URL -> the whole harvest, in a store of its own, every
            page listed with what extraction left behind

Every test records the tools it ran and what each returned, every tool call a
model makes in the chat is recorded with its output, and every finished test
asks for a verdict. The verdicts are what someone fixing DocsForge works from.

It lives only in `docsforge.server.app` (the local chat), at `/lab`, linked
from nowhere, and answers loopback clients only. The public server
(`main.py`) never mounts it. Its data -- accounts, tests, verdicts, the
activity record -- is one SQLite file under `lab_data/` beside the code
(`DOCSFORGE_LAB_DIR` moves it), and none of it leaves the machine.

`DOCSFORGE_LAB=0` turns the whole thing off; `DOCSFORGE_LAB_RECORD=0` keeps the
panel but stops recording chat turns and tool calls.

Nothing heavy is imported here: `app.py` calls the three chat hooks below on
every turn, and they must cost nothing when the lab is off.
"""

from __future__ import annotations

import os
from pathlib import Path

_OFF = ("0", "off", "false", "no")

#: Set by `routes.install()` when the web app actually starts serving. Until
#: then nothing is recorded: importing the app (the test suite does, a lot)
#: must not create a lab database beside the code.
ACTIVE = False


def enabled() -> bool:
    return os.environ.get("DOCSFORGE_LAB", "1").strip().lower() not in _OFF


def recording() -> bool:
    return ACTIVE and enabled() and os.environ.get(
        "DOCSFORGE_LAB_RECORD", "1").strip().lower() not in _OFF


def data_dir() -> Path:
    """Where the lab keeps its database and harvest runs."""
    from docsforge import ROOT
    return Path(os.environ.get("DOCSFORGE_LAB_DIR") or (ROOT / "lab_data"))


# ── chat hooks ──────────────────────────────────────────────
# Called by app.py's chat stream. Each is a no-op when recording is off and
# swallows its own failures: a turn must never fail because its record did.
def turn_started(provider: str, history: list[dict]) -> str | None:
    if not recording():
        return None
    try:
        from docsforge.lab import activity
        return activity.turn_started(provider, history)
    except Exception:                                   # noqa: BLE001
        return None


def tool_reported(turn_id: str | None, trace_id: str | None, provider: str = "") -> None:
    if not (turn_id and trace_id and recording()):
        return
    try:
        from docsforge.lab import activity
        activity.tag(trace_id, source="chat", turn_id=turn_id, provider=provider)
    except Exception:                                   # noqa: BLE001
        pass


def turn_finished(turn_id: str | None, outcome: str, answer: str = "",
                  model: str = "", tools: list[str] | None = None,
                  duration_ms: float = 0.0) -> None:
    if not turn_id:
        return
    try:
        from docsforge.lab import activity
        activity.turn_finished(turn_id, outcome, answer, model, tools or [], duration_ms)
    except Exception:                                   # noqa: BLE001
        pass
