"""
Tests for the Web UI's trace surface in app.py:
  - GET /api/trace/{id} replays history, streams live updates, and signals
    end-of-trace so the browser's EventSource can stop cleanly.
  - chat_stream() attaches the trace id run_tool() minted to the SSE
    `tool_end` event, which is how the browser knows what to subscribe to.
"""

import json
import os
import sys
import threading
import time
from typing import Any, Iterator

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.server import app
from docsforge.tools import tracing as tr
from docsforge.providers.base import text, tool_end, tool_start


@pytest.fixture(autouse=True)
def clean_traces():
    tr.clear()
    yield
    tr.clear()


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        kind, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                kind = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if data is not None:
            events.append((kind, data))
    return events


# ── GET /api/trace/{id} ───────────────────────────────────
def test_trace_endpoint_replays_a_finished_trace_then_ends():
    ctx = tr.start("effect")
    with ctx.stage("resolving", target="effect"):
        pass
    ctx.close()

    with TestClient(app.app) as client:
        res = client.get(f"/api/trace/{ctx.trace_id}")
    assert res.status_code == 200
    events = parse_sse(res.text)

    kinds = [k for k, _ in events]
    assert "trace" in kinds
    assert kinds[-1] == "trace_end"
    resolving = next(d for k, d in events if k == "trace" and d["name"] == "resolving")
    assert resolving["state"] == "completed"


def test_trace_endpoint_reports_an_unknown_id_without_using_the_reserved_error_event():
    with TestClient(app.app) as client:
        res = client.get("/api/trace/does-not-exist")
    assert res.status_code == 200
    events = parse_sse(res.text)
    assert events, "expected at least the trace_error event"
    kind, data = events[0]
    # Not "error" -- EventSource treats that name specially in the browser,
    # dispatching it even for transport-level failures with no JSON `data`.
    assert kind == "trace_error"
    assert "does-not-exist" in data["message"]


def test_trace_endpoint_streams_live_updates_from_another_thread():
    """The point of the whole design: a second connection sees events a
    *different* thread is still appending, without polling."""
    ctx = tr.start("effect")

    def append_soon():
        time.sleep(0.15)
        with ctx.stage("harvesting", target="https://x.dev"):
            pass
        ctx.close()

    threading.Thread(target=append_soon, daemon=True).start()

    with TestClient(app.app) as client:
        with client.stream("GET", f"/api/trace/{ctx.trace_id}") as res:
            assert res.status_code == 200
            body = ""
            for chunk in res.iter_text():
                body += chunk
                if "trace_end" in body:
                    break

    events = parse_sse(body)
    names = [d.get("name") for k, d in events if k == "trace"]
    assert "harvesting" in names


# ── chat_stream(): trace id attached to tool_end ──────────
class _FakeProvider:
    name = "fake"

    def model(self):
        return "fake-model"

    def stream(self, *, system, history, tools, run_tool) -> Iterator[dict[str, Any]]:
        yield tool_start("read_knowledge_base", {"name": "nothing-stored"})
        result = run_tool("read_knowledge_base", {"name": "nothing-stored"})
        yield tool_end("read_knowledge_base", result, kind="")
        yield text("done")


def test_chat_stream_attaches_trace_id_to_tool_end(monkeypatch):
    monkeypatch.setattr(app.providers, "get", lambda name: _FakeProvider())

    raw = list(app.chat_stream([{"role": "user", "content": "hi"}], "fake"))
    body = "".join(raw)
    parsed = parse_sse(body)

    tool_end_events = [d for k, d in parsed if k == "tool" and d.get("phase") == "end"]
    assert tool_end_events, "expected a tool_end SSE event"
    trace_id = tool_end_events[0]["trace_id"]
    assert trace_id, "chat_stream must attach the trace id run_tool() minted"

    trace = tr.get(trace_id)
    assert trace is not None
    assert trace.finished is not None  # a fast tool call's trace closes promptly


def test_chat_stream_logs_the_tool_call_sequence_for_the_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(app.providers, "get", lambda name: _FakeProvider())
    monkeypatch.setattr("docsforge.tools.applog.LOG_DIR", str(tmp_path))
    monkeypatch.setattr("docsforge.tools.applog.LOG_FILE", str(tmp_path / "docsforge.log"))
    monkeypatch.setattr("docsforge.tools.applog._configured", False)
    monkeypatch.setattr("docsforge.tools.applog._disabled", False)

    list(app.chat_stream([{"role": "user", "content": "hi"}], "fake"))

    log_path = tmp_path / "docsforge.log"
    assert log_path.exists()
    lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    turns = [l for l in lines if l["kind"] == "turn"]
    assert turns, "expected a 'turn' behaviour-pattern log line"
    assert any("read_knowledge_base" in t for t in turns[-1]["tools"])


# ── providers that run tools somewhere this process cannot see ──────
class _OutOfProcessProvider:
    """Shaped like providers/claudecode.py, which is the whole point.

    It hands DocsForge's tools to the Claude Code CLI, which calls them
    over MCP from a process of its own -- so `run_tool` never executes
    here, and until this was fixed there was no trace at all and the UI
    row had nothing to open."""

    name = "cli"

    def model(self):
        return "cli-model"

    def stream(self, *, system, history, tools, run_tool) -> Iterator[dict[str, Any]]:
        args = {"name": "mojo", "api_key": "sk-live-not-for-the-browser"}
        yield tool_start("learn_technology", args)
        # Note: run_tool is deliberately NOT called.
        yield tool_end("learn_technology", "Harvested **mojo** 1.0.0 — 306 pages.", kind="")
        yield text("done")


def test_a_tool_run_out_of_process_is_still_traceable(monkeypatch):
    monkeypatch.setattr(app.providers, "get", lambda name: _OutOfProcessProvider())

    body = "".join(app.chat_stream([{"role": "user", "content": "mojo?"}], "cli"))
    ends = [d for k, d in parse_sse(body) if k == "tool" and d.get("phase") == "end"]
    assert ends, "expected a tool_end event"
    trace_id = ends[0]["trace_id"]
    assert trace_id, "a tool this process never ran must still be traceable"

    events = tr.get(trace_id).events()
    call = next(e for e in events if e.name == "learn_technology")
    assert call.state == "completed"
    assert call.output == "Harvested **mojo** 1.0.0 — 306 pages.", "what came back"
    assert call.target == "mojo"
    assert call.metadata["name"] == "mojo", "what was executed"
    # The sanitisation boundary holds on this path too.
    assert call.metadata["api_key"] == "[redacted]"


def test_an_out_of_process_failure_keeps_the_text_the_model_was_given(monkeypatch):
    class Failing(_OutOfProcessProvider):
        def stream(self, *, system, history, tools, run_tool):
            yield tool_start("learn_technology", {"name": "ghost"})
            yield tool_end("learn_technology", "Error: could not resolve 'ghost'", kind="")
            yield text("done")

    monkeypatch.setattr(app.providers, "get", lambda name: Failing())
    body = "".join(app.chat_stream([{"role": "user", "content": "ghost?"}], "cli"))
    ends = [d for k, d in parse_sse(body) if k == "tool" and d.get("phase") == "end"]

    call = next(e for e in tr.get(ends[0]["trace_id"]).events()
                if e.name == "learn_technology")
    assert call.state == "failed"
    assert "could not resolve" in call.error
    assert "could not resolve" in call.output


def test_a_stale_thread_local_id_is_never_reused_for_another_turn(monkeypatch):
    """Server threads are reused. A turn whose tools run out of process must
    not inherit the trace id an earlier in-process turn left behind."""
    from docsforge.tools import forge_tools as ft

    monkeypatch.setattr(app.providers, "get", lambda name: _FakeProvider())
    first = "".join(app.chat_stream([{"role": "user", "content": "a"}], "fake"))
    first_id = [d for k, d in parse_sse(first)
                if k == "tool" and d.get("phase") == "end"][0]["trace_id"]
    assert ft.last_trace_id() == first_id, "the thread-local is genuinely left set"

    monkeypatch.setattr(app.providers, "get", lambda name: _OutOfProcessProvider())
    second = "".join(app.chat_stream([{"role": "user", "content": "b"}], "cli"))
    second_id = [d for k, d in parse_sse(second)
                 if k == "tool" and d.get("phase") == "end"][0]["trace_id"]

    assert second_id != first_id, "the second turn must not borrow the first's trace"
    call = next(e for e in tr.get(second_id).events()
                if e.name == "learn_technology")
    assert call.target == "mojo"
