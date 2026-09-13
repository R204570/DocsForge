"""
Structured logs for debugging: one line per HTTP request, one line per tool
call, and one line summarising each turn's tool-call sequence -- the
"behaviour pattern" a developer actually wants when a run went wrong.

Separate from tracing.py's Trace/TraceEvent, which exists for the *browser*.
This exists for whoever is watching the server: `tail -f logs/docsforge.log`
during a debugging session, or `grep` it after the fact. Both read from the
same underlying facts (forge_tools.run_tool, app.py's request handling) so
neither has to re-derive what happened -- tracing.py feeds this module one
line per trace event, rather than each keeping its own copy of the truth.

One JSON object per line (not a table, not a database) because the whole
point is that `grep '"kind": "turn"'` and a text editor are enough to read
it back. If DocsForge ever needs more than that, this is the place to
replace, not extend.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time

#: Overridable so a container or a test can point this somewhere writable
#: without patching the module.
LOG_DIR = os.environ.get("DOCSFORGE_LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "docsforge.log")

_logger = logging.getLogger("docsforge.applog")
_logger.setLevel(logging.INFO)
_configured = False
_disabled = False


def _configure() -> None:
    global _configured, _disabled
    if _configured or _disabled:
        return
    _configured = True
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)
        _logger.propagate = False
    except OSError:
        # A machine where the log directory cannot be created must not stop
        # DocsForge from working -- logging is diagnostics, not a dependency.
        _disabled = True


def _write(kind: str, **fields) -> None:
    _configure()
    if _disabled:
        return
    try:
        record = {"ts": round(time.time(), 3), "kind": kind, **fields}
        _logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        pass  # a logging failure must never surface to the caller


def request(method: str, path: str, status: int, duration_ms: float,
           client: str = "") -> None:
    """One HTTP request in, one line out."""
    _write("request", method=method, path=path, status=status,
           duration_ms=round(duration_ms, 1), client=client)


def tool_call(name: str, ok: bool, duration_ms: float, trace_id: str = "",
             chars: int = 0, error: str = "") -> None:
    """One tool call, whatever invoked it (Web UI or MCP)."""
    _write("tool_call", name=name, ok=ok, duration_ms=round(duration_ms, 1),
           trace_id=trace_id, chars=chars, error=(error or "")[:300])


def turn(tools: list[str], outcome: str, duration_ms: float,
        provider: str = "") -> None:
    """The 'behaviour pattern' line: the ordered sequence of tools one
    conversation turn called, and how it ended. This is the line to grep
    when a model looped, picked the wrong tool, or never called the one it
    needed."""
    _write("turn", tools=tools, outcome=outcome,
           duration_ms=round(duration_ms, 1), provider=provider)


def trace_event(trace_id: str, stage: str, state: str, message: str = "") -> None:
    _write("trace_event", trace_id=trace_id, stage=stage, state=state,
           message=(message or "")[:300])


def harvest(job: str, label: str, state: str, phase: str = "",
            pages: int = 0, expected=None, elapsed: float = 0.0,
            error: str = "") -> None:
    """A background harvest, at its start, at each phase change, and at its end.

    Written because a harvest that outlives the request deadline left no line
    anywhere: not here, not on the console, not in `list_knowledge_base`. Grep
    `"kind": "harvest"` to see every one a process ever started and how each
    ended -- including the ones that stopped reporting, which is what a killed
    process looks like from outside.
    """
    _write("harvest", job=job, label=label, state=state, phase=phase,
           pages=pages, expected=expected, elapsed=round(elapsed, 1),
           error=(error or "")[:300])


def error(where: str, message: str) -> None:
    _write("error", where=where, message=(message or "")[:500])
