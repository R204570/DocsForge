"""
The lab's own database: one SQLite file, never the knowledge base.

SQLite rather than the store's Postgres because the lab is local by design --
accounts, verdicts and a record of every chat turn have no business in a
database other DocsForge instances share, and `.env` on the machine this was
built on points the store at a hosted one. A file also needs no setup.

Connections are opened per operation (the lab is called from request threads,
test workers and trace listeners alike) and writes are serialised by one lock;
WAL lets reads carry on beside them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from docsforge import lab

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created REAL NOT NULL,
    last_login REAL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    last_seen REAL,
    client TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    username TEXT,
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    client TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT,
    created_by INTEGER,
    created REAL NOT NULL,
    options TEXT,
    total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    batch_id INTEGER,
    parent_id INTEGER,
    created_by INTEGER,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    state TEXT NOT NULL,
    input TEXT,
    result TEXT,
    auto TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS tests_state ON tests(state);
CREATE INDEX IF NOT EXISTS tests_batch ON tests(batch_id);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT,
    state TEXT NOT NULL,
    started REAL,
    duration_ms REAL,
    output TEXT,
    omitted INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    events TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS steps_test ON steps(test_id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    user_id INTEGER,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    verdict TEXT NOT NULL,
    aspects TEXT,
    issues TEXT,
    rating INTEGER,
    expected TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    UNIQUE (target_kind, target_id, user_id)
);
CREATE INDEX IF NOT EXISTS feedback_status ON feedback(status);
CREATE TABLE IF NOT EXISTS activity (
    trace_id TEXT PRIMARY KEY,
    tool TEXT,
    source TEXT,
    started REAL,
    finished REAL,
    ok INTEGER,
    duration_ms REAL,
    target TEXT,
    args TEXT,
    output TEXT,
    omitted INTEGER,
    events TEXT,
    turn_id TEXT,
    test_id INTEGER,
    provider TEXT
);
CREATE INDEX IF NOT EXISTS activity_turn ON activity(turn_id);
CREATE INDEX IF NOT EXISTS activity_started ON activity(started);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    started REAL NOT NULL,
    finished REAL,
    provider TEXT,
    model TEXT,
    prompt TEXT,
    answer TEXT,
    outcome TEXT,
    tools TEXT,
    duration_ms REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    username TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

_WRITE = threading.Lock()
_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def path() -> Path:
    return lab.data_dir() / "lab.db"


def _open(where: Path) -> sqlite3.Connection:
    cx = sqlite3.connect(str(where), timeout=15, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    return cx


def _ensure(where: Path) -> None:
    key = str(where)
    if key in _READY:
        return
    with _READY_LOCK:
        if key in _READY:
            return
        where.parent.mkdir(parents=True, exist_ok=True)
        cx = _open(where)
        try:
            cx.execute("PRAGMA journal_mode = WAL")
            cx.executescript(SCHEMA)
            cx.commit()
        finally:
            cx.close()
        _READY.add(key)


@contextmanager
def read():
    where = path()
    _ensure(where)
    cx = _open(where)
    try:
        yield cx
    finally:
        cx.close()


@contextmanager
def write():
    """One transaction, committed on success, under the process's write lock."""
    where = path()
    _ensure(where)
    with _WRITE:
        cx = _open(where)
        try:
            yield cx
            cx.commit()
        except BaseException:
            cx.rollback()
            raise
        finally:
            cx.close()


def rows(sql: str, params=()) -> list[dict]:
    with read() as cx:
        return [dict(r) for r in cx.execute(sql, params).fetchall()]


def row(sql: str, params=()) -> dict | None:
    with read() as cx:
        found = cx.execute(sql, params).fetchone()
        return dict(found) if found else None


def scalar(sql: str, params=()):
    with read() as cx:
        found = cx.execute(sql, params).fetchone()
        return found[0] if found else None


def execute(sql: str, params=()) -> int:
    """Run one write; the new row's id (for an insert) or rows changed."""
    with write() as cx:
        cur = cx.execute(sql, params)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(text, default=None):
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def log_event(kind: str, username: str = "", /, **detail) -> None:
    """The lab's audit trail, in its own table and in `logs/docsforge.log`."""
    try:
        execute("INSERT INTO events (ts, username, kind, detail) VALUES (?, ?, ?, ?)",
                (time.time(), username or "", kind, dumps(detail)))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from docsforge.tools import applog
        applog.lab(kind, user=username or "", **detail)
    except Exception:                                   # noqa: BLE001
        pass


def forget_paths() -> None:
    """For tests, which point the lab at a fresh directory each time."""
    with _READY_LOCK:
        _READY.clear()
