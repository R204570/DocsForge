"""
The record of what the AI ran: every chat turn, every tool call, and what each
tool call returned.

`tracing` already builds this story for the browser -- the arguments, the
stages underneath, the text the model was handed -- and then forgets it once
forty newer ones exist. The lab keeps it. A trace is recorded when it closes
(`tracing.on_close`), so this module adds no call sites to the tools; the chat
and the test runner then *tag* the trace with where it came from. Record and
tag are both upserts, so whichever lands first does not matter: an in-process
call closes before the chat relays it, a harvest past its deadline long after.

Bounded: the newest `KEEP_CALLS` tool calls and `KEEP_TURNS` turns are kept.
"""

from __future__ import annotations

import itertools
import time
import uuid

from docsforge import lab
from docsforge.lab import db
from docsforge.tools import tracing

KEEP_CALLS = 5000
KEEP_TURNS = 2000
#: A child event's own output, stored. The call's output -- what the model was
#: handed -- keeps tracing's own bound (MAX_OUTPUT) and says what it left out.
EVENT_OUTPUT = 2000
PROMPT_CHARS = 4000
ANSWER_CHARS = 60_000

_writes = itertools.count(1)


def install() -> None:
    tracing.on_close(record_trace)


def _compact(event: dict, keep_output: bool) -> dict:
    out = dict(event)
    text = out.get("output") or ""
    if not keep_output:
        out.pop("output", None)
    elif len(text) > EVENT_OUTPUT:
        out["output"] = text[:EVENT_OUTPUT]
        out["omitted"] = (out.get("omitted") or 0) + len(text) - EVENT_OUTPUT
    return out


def snapshot(trace) -> dict:
    """One trace as an activity row: the call, its arguments, its output and
    every stage beneath it."""
    events = [e.as_dict() for e in trace.events()]
    root = next((e for e in events if e["parent_id"] is None and e["type"] == "stage"), None)
    finished = trace.finished
    state = root["state"] if root else ""
    return {
        "trace_id": trace.id,
        "tool": trace.label or (root["name"] if root else ""),
        "started": trace.started,
        "finished": finished,
        "ok": None if not finished else int(state == tracing.COMPLETED),
        "duration_ms": round(((finished or time.time()) - trace.started) * 1000, 1),
        "target": (root or {}).get("target") or "",
        "args": db.dumps((root or {}).get("metadata") or {}),
        "output": (root or {}).get("output") or "",
        "omitted": (root or {}).get("omitted") or 0,
        "events": db.dumps([_compact(e, e is not root) for e in events]),
    }


_RECORD = ("tool", "started", "finished", "ok", "duration_ms", "target", "args",
           "output", "omitted", "events")


def record_trace(trace) -> None:
    if not lab.recording():
        return
    try:
        row = snapshot(trace)
        cols = ("trace_id",) + _RECORD
        db.execute(
            f"INSERT INTO activity ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(trace_id) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in _RECORD),
            tuple(row[c] for c in cols))
        if next(_writes) % 200 == 0:
            prune()
    except Exception:                                   # noqa: BLE001
        pass


def tag(trace_id: str, *, source: str | None = None, turn_id: str | None = None,
        test_id: int | None = None, provider: str | None = None) -> None:
    """Say where a trace came from. A trace still running (a harvest past its
    deadline) is snapshotted now, so it is visible before it ends."""
    if not (trace_id and lab.recording()):
        return
    db.execute(
        "INSERT INTO activity (trace_id, source, turn_id, test_id, provider) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(trace_id) DO UPDATE SET "
        "source = COALESCE(excluded.source, activity.source), "
        "turn_id = COALESCE(excluded.turn_id, activity.turn_id), "
        "test_id = COALESCE(excluded.test_id, activity.test_id), "
        "provider = COALESCE(excluded.provider, activity.provider)",
        (trace_id, source, turn_id, test_id, provider or None))
    live = tracing.get(trace_id)
    if live is not None and db.scalar(
            "SELECT tool FROM activity WHERE trace_id = ?", (trace_id,)) is None:
        record_trace(live)


def record_call(trace_id: str, tool: str, *, args: dict, output: str, ok: bool,
                started: float, duration_ms: float, events: list[dict] | None = None,
                omitted: int = 0, target: str = "", test_id: int | None = None) -> None:
    """A tool call whose trace never closed in this process -- a lab harvest's,
    which ran in a worker process of its own, or a resolution the lab runs
    with the cache off -- recorded from what the lab saw of it."""
    if not lab.recording():
        return
    try:
        db.execute(
            "INSERT OR REPLACE INTO activity (trace_id, tool, source, started, finished, ok, "
            "duration_ms, target, args, output, omitted, events, test_id) "
            "VALUES (?, ?, 'lab', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, tool, started, started + duration_ms / 1000, int(bool(ok)),
             round(duration_ms, 1), target, db.dumps(args), output or "", omitted,
             db.dumps(events or []), test_id))
    except Exception:                                   # noqa: BLE001
        pass


def prune() -> None:
    with db.write() as cx:
        cx.execute("DELETE FROM activity WHERE trace_id NOT IN (SELECT trace_id FROM activity "
                   "ORDER BY COALESCE(started, 0) DESC LIMIT ?)", (KEEP_CALLS,))
        cx.execute("DELETE FROM turns WHERE id NOT IN (SELECT id FROM turns "
                   "ORDER BY started DESC LIMIT ?)", (KEEP_TURNS,))


# ── chat turns ──────────────────────────────────────────────
def turn_started(provider: str, history: list[dict]) -> str:
    asked = next((m.get("content") or "" for m in reversed(history or [])
                  if m.get("role") == "user"), "")
    turn_id = uuid.uuid4().hex[:16]
    db.execute("INSERT INTO turns (id, started, provider, prompt, outcome) VALUES (?, ?, ?, ?, ?)",
               (turn_id, time.time(), provider or "", asked[:PROMPT_CHARS], "running"))
    return turn_id


def turn_finished(turn_id: str, outcome: str, answer: str, model: str,
                  tools: list[str], duration_ms: float) -> None:
    db.execute("UPDATE turns SET finished = ?, outcome = ?, answer = ?, model = ?, tools = ?, "
               "duration_ms = ? WHERE id = ?",
               (time.time(), outcome, (answer or "")[:ANSWER_CHARS], model or "",
                db.dumps(tools), round(duration_ms, 1), turn_id))


# ── reading it back ─────────────────────────────────────────
def _call_row(r: dict, full: bool = False) -> dict:
    out = {
        "trace_id": r["trace_id"], "tool": r["tool"] or "", "source": r["source"] or "other",
        "started": r["started"], "finished": r["finished"],
        "ok": None if r["ok"] is None else bool(r["ok"]),
        "running": r["finished"] is None, "duration_ms": r["duration_ms"],
        "target": r["target"] or "", "args": db.loads(r["args"], {}),
        "turn_id": r["turn_id"], "test_id": r["test_id"], "provider": r["provider"] or "",
        "chars": len(r["output"] or "") + (r["omitted"] or 0),
    }
    if full:
        out["output"] = r["output"] or ""
        out["omitted"] = r["omitted"] or 0
        out["events"] = db.loads(r["events"], [])
    else:
        out["preview"] = (r["output"] or "")[:280]
    return out


def calls(limit: int = 50, offset: int = 0, tool: str = "", ok: str = "",
          source: str = "", q: str = "") -> tuple[list[dict], int]:
    where, params = ["tool IS NOT NULL"], []
    if tool:
        where.append("tool = ?")
        params.append(tool)
    if ok in ("1", "0"):
        where.append("ok = ?")
        params.append(int(ok))
    if source:
        where.append("COALESCE(source, 'other') = ?")
        params.append(source)
    if q:
        where.append("(target LIKE ? OR args LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM activity WHERE {clause}", params)
    found = db.rows(f"SELECT * FROM activity WHERE {clause} ORDER BY started DESC "
                    f"LIMIT ? OFFSET ?", params + [limit, offset])
    return [_call_row(r) for r in found], total or 0


def call(trace_id: str) -> dict | None:
    live = tracing.get(trace_id)
    if live is not None and live.finished is None:
        record_trace(live)          # still running: show where it is now
    found = db.row("SELECT * FROM activity WHERE trace_id = ?", (trace_id,))
    return _call_row(found, full=True) if found and found["tool"] else None


def turns(limit: int = 50, offset: int = 0, q: str = "") -> tuple[list[dict], int]:
    where, params = "", []
    if q:
        where = "WHERE prompt LIKE ? OR answer LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    total = db.scalar(f"SELECT COUNT(*) FROM turns {where}", params)
    found = db.rows(f"SELECT id, started, finished, provider, model, prompt, outcome, tools, "
                    f"duration_ms, substr(answer, 1, 280) AS preview FROM turns {where} "
                    f"ORDER BY started DESC LIMIT ? OFFSET ?", params + [limit, offset])
    for r in found:
        r["tools"] = db.loads(r["tools"], [])
    return found, total or 0


def turn(turn_id: str) -> dict | None:
    found = db.row("SELECT * FROM turns WHERE id = ?", (turn_id,))
    if not found:
        return None
    found["tools"] = db.loads(found["tools"], [])
    found["calls"] = [_call_row(r, full=True) for r in db.rows(
        "SELECT * FROM activity WHERE turn_id = ? AND tool IS NOT NULL ORDER BY started",
        (turn_id,))]
    return found
