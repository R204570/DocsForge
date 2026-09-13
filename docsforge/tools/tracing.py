"""
Execution-trace observability: a small, best-effort event log for what a
DocsForge tool call is actually doing underneath, so the Web UI can show
more than "tool running / tool finished".

This is a REPORTING layer over execution that already happens elsewhere --
forge_tools.py's orchestration, docsforge.py's harvest ladder, resolver.py,
kb_store.py. It never decides what work to do, and a failure in it must
never break the work it is describing: every mutating method here swallows
its own exceptions.

A Trace is one tool call's story: a flat, ordered, thread-safe log of
TraceEvents, each naming its parent so a browser can render
Turn -> tool call -> stage -> event without this module knowing anything
about turns or tool calls. Traces live in this process only -- the same
policy harvest_jobs.Job already uses -- and are pruned once there are more
finished ones than KEEP_FINISHED.

Deliberately named `tracing`, not `trace`: the standard library already
owns that name (python -m trace), and shadowing it is not worth the
confusion the first time someone reaches for it in a debugger.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

try:
    from docsforge.tools import applog
except Exception:                                       # pragma: no cover
    applog = None  # logging is diagnostics, never a hard dependency


#: Lifecycle states a TraceEvent can be in. `queued` is for a stage that has
#: been created but not yet started (rare today; kept for the contract).
QUEUED, RUNNING, COMPLETED, FAILED, SKIPPED, CANCELLED = (
    "queued", "running", "completed", "failed", "skipped", "cancelled")

_TERMINAL = {COMPLETED, FAILED, SKIPPED, CANCELLED}

#: Finished traces kept so a browser tab opened late can still replay one.
KEEP_FINISHED = 40

#: Hard cap on events per trace. Forge_tools throttles anything repetitive
#: (page-fetch ticks) well below this; this is the backstop for a corpus
#: large enough to defeat that throttle, so memory stays bounded regardless.
MAX_EVENTS = 2000

#: Keys never sent to the browser, wherever a value passes through
#: `sanitize()`. Substring match, case-insensitive: today's tools only pass
#: URLs and names, but the boundary has to hold without being told which
#: future tool it is protecting.
_SENSITIVE = ("key", "token", "secret", "password", "authorization",
              "cookie", "credential", "bearer", "auth")

#: Cap on any single string passing through `sanitize()`. A summary field
#: that arrives page-sized is a mistake somewhere upstream, not something
#: to forward to a browser.
MAX_STRING = 4_000

#: Cap on a tool's own returned text, which is the one field that is
#: *legitimately* long -- it is the answer the model was given, and showing
#: it is the entire point of the detail view. Larger than MAX_STRING and
#: still bounded, with whatever is dropped counted rather than silently
#: cut: an undisclosed truncation is the one thing this project refuses.
MAX_OUTPUT = 20_000

_counter = itertools.count(1)


def new_id(label: str = "trace") -> str:
    safe = "".join(c for c in (label or "").lower() if c.isalnum() or c == "-")[:24]
    return f"{safe or 'trace'}-{next(_counter)}-{int(time.time() * 1000) % 100000}"


def sanitize(value: Any) -> Any:
    """Strip anything that looks like a credential from a value bound for
    the browser, and cap anything long enough to be a whole page rather
    than a summary. Best-effort: a safety net, not a parser."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(s in k.lower() for s in _SENSITIVE):
                out[k] = "[redacted]"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_STRING:
        return value[:MAX_STRING] + "…"
    return value


def clip(text: str, limit: int = MAX_OUTPUT) -> tuple[str, int]:
    """A bounded copy of `text`, and how many characters were left out.

    Returns (kept, omitted). `omitted` is what lets the UI say "12,431 more
    characters" instead of ending mid-sentence and hoping nobody notices.
    """
    body = text or ""
    if len(body) <= limit:
        return body, 0
    return body[:limit], len(body) - limit


@dataclass
class TraceEvent:
    """One row of an execution trace.

    `id` is stable across re-emissions of the same stage (queued -> running
    -> completed), so a browser keys its UI on `id` and updates a row in
    place rather than appending a new one for every tick -- that is what
    keeps a 1,799-page harvest from becoming 1,799 DOM nodes.
    """

    id: str
    trace_id: str
    parent_id: str | None
    type: str                              # "stage" | "event" | "heartbeat"
    name: str
    state: str = RUNNING
    ts: float = field(default_factory=time.time)
    message: str = ""
    metadata: dict = field(default_factory=dict)
    target: str = ""                       # input/target acted on, e.g. a URL
    result: dict | None = None
    error: str = ""
    counters: dict = field(default_factory=dict)
    #: The operation's own returned text, bounded by MAX_OUTPUT. Kept apart
    #: from `result` (a small structured summary) because this is the raw
    #: thing the model was handed, and seeing exactly that is what "what was
    #: the output" means.
    output: str = ""
    #: Characters of `output` not sent. Non-zero means the view must say so.
    omitted: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "trace_id": self.trace_id, "parent_id": self.parent_id,
            "type": self.type, "name": self.name, "state": self.state,
            "ts": self.ts, "message": self.message, "metadata": self.metadata,
            "target": self.target, "result": self.result, "error": self.error,
            "counters": self.counters, "output": self.output,
            "omitted": self.omitted,
        }


class Trace:
    """One tool call's execution story.

    Any thread may append -- the request thread for a fast tool call, or a
    harvest_jobs worker thread for one still running after the response has
    already gone back. Subscribers (an SSE handler on a *different* HTTP
    request) get every event already logged, then block for new ones as
    they arrive -- genuine liveness, not client-side polling, because the
    subscriber's thread is not the one doing the work.
    """

    def __init__(self, trace_id: str, label: str = ""):
        self.id = trace_id
        self.label = label
        self.started = time.time()
        self.finished: float | None = None
        #: True once something has claimed responsibility for closing this
        #: trace itself -- see `TraceContext.detach()`. `run_tool()` checks
        #: this before closing on its own way out, so a tool whose real work
        #: continues on another thread (harvest_jobs, past its deadline)
        #: does not have its trace cut off the moment the *call* returns.
        self.keep_open = False
        self._lock = threading.Lock()
        #: Keyed by event id, not a plain append log: a running stage that
        #: ticks its own id 300 times (a page-fetch counter) must update one
        #: entry, not accumulate 300 -- a dict already preserves the id's
        #: first-insertion position across updates, which is exactly the
        #: "keep the row, refresh its state" behaviour the browser wants,
        #: and it is what keeps MAX_EVENTS a bound on *distinct* operations
        #: rather than on how many times one of them happened to tick.
        self._events: dict[str, TraceEvent] = {}
        self._subscribers: list["queue.Queue[TraceEvent | None]"] = []
        self._seq = itertools.count(1)

    def next_id(self) -> str:
        return f"{self.id}-{next(self._seq)}"

    def append(self, event: TraceEvent) -> None:
        with self._lock:
            if self.finished is not None:
                return  # a closed trace accepts no further events
            self._events[event.id] = event
            if len(self._events) > MAX_EVENTS:
                for stale_id in list(self._events.keys())[:len(self._events) - MAX_EVENTS]:
                    del self._events[stale_id]
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        if applog is not None:
            try:
                applog.trace_event(self.id, event.name, event.state, event.message)
            except Exception:
                pass

    def events(self) -> list[TraceEvent]:
        """Every distinct operation's latest state, oldest-first. A live
        stage appears once, showing wherever its progress currently stands
        -- the transitions it passed through to get there are not kept."""
        with self._lock:
            return list(self._events.values())

    def close(self) -> None:
        with self._lock:
            if self.finished is not None:
                return
            self.finished = time.time()
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(None)  # sentinel: no more events
            except queue.Full:
                pass

    def subscribe(self, idle_heartbeat: float = 15.0,
                 max_idle_cycles: int = 480) -> Iterator[TraceEvent]:
        """Yield every event already logged, then new ones as they arrive,
        until the trace closes. A synthetic `heartbeat` event is yielded
        every `idle_heartbeat` seconds of silence so a caller streaming this
        over SSE has something to keep the connection alive with -- it is
        typed distinctly and must never be mistaken for real progress.

        `max_idle_cycles` bounds total wait time (default 2 hours) so a
        trace nobody ever closes cannot hold a subscriber open forever.
        """
        q: "queue.Queue[TraceEvent | None]" = queue.Queue(maxsize=1000)
        with self._lock:
            backlog = list(self._events.values())
            closed = self.finished is not None
            if not closed:
                self._subscribers.append(q)
        for ev in backlog:
            yield ev
        if closed:
            return
        try:
            cycles = 0
            while cycles < max_idle_cycles:
                try:
                    item = q.get(timeout=idle_heartbeat)
                except queue.Empty:
                    cycles += 1
                    with self._lock:
                        if self.finished is not None:
                            return
                    yield TraceEvent(id=f"{self.id}-hb-{cycles}", trace_id=self.id,
                                     parent_id=None, type="heartbeat", name="",
                                     state=RUNNING)
                    continue
                cycles = 0
                if item is None:
                    return
                yield item
        finally:
            with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)


# ─────────────────────────────────────────────────────────────
# Registry -- same shape as harvest_jobs._JOBS, deliberately: an in-process,
# never-persisted table of live and recently-finished traces.
# ─────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_TRACES: dict[str, Trace] = {}


def _prune() -> None:
    """Drop the oldest finished traces. Caller holds the lock."""
    finished = sorted((t for t in _TRACES.values() if t.finished is not None),
                      key=lambda t: t.finished)
    if len(finished) > KEEP_FINISHED:
        for t in finished[: len(finished) - KEEP_FINISHED]:
            _TRACES.pop(t.id, None)


def create(label: str = "", trace_id: str | None = None) -> Trace:
    tid = trace_id or new_id(label)
    t = Trace(tid, label)
    with _LOCK:
        _TRACES[tid] = t
        _prune()
    return t


def get(trace_id: str) -> Trace | None:
    with _LOCK:
        return _TRACES.get(trace_id)


def clear() -> None:
    """Forget every trace. For tests; does not touch subscribers in flight."""
    with _LOCK:
        _TRACES.clear()


# ─────────────────────────────────────────────────────────────
# TraceContext -- what gets threaded through forge_tools.py
# ─────────────────────────────────────────────────────────────
class Stage:
    """A named span of work, created by `TraceContext.stage()`.

    Used either as a context manager (`with ctx.stage("resolving") as sub:`,
    which marks running on entry and completed/failed on exit) or manually
    via `.start()` / `.tick()` / `.finish()` when the span does not map onto
    one Python `with` block -- `tool_harvest_docs` reports its "harvesting"
    stage this way, since paging happens across several nested calls.
    """

    def __init__(self, ctx: "TraceContext", name: str, *, message: str = "",
                target: str = "", metadata: dict | None = None):
        self._ctx = ctx
        self.name = name
        self.id = ctx._trace.next_id()
        self._target = target
        self._metadata = metadata or {}
        self.counters: dict = {}
        #: A stage finishes once. `with ctx.stage(...) as sub:` calls
        #: `finish()` automatically on exit; a call site that already gave a
        #: richer result manually (a page count, a strategy name) is not
        #: overwritten by that generic follow-up, since both share one event
        #: id and the browser would otherwise see the detail vanish.
        self._done = False

    def _emit(self, state: str, *, message: str = "", result=None,
             error: str = "", counters: dict | None = None,
             output: str = "") -> None:
        if self._done:
            return
        if state in _TERMINAL:
            self._done = True
        if counters:
            self.counters.update(counters)
        body, omitted = clip(output) if output else ("", 0)
        try:
            self._ctx._trace.append(TraceEvent(
                id=self.id, trace_id=self._ctx.trace_id, parent_id=self._ctx.parent_id,
                type="stage", name=self.name, state=state, message=message,
                target=sanitize(self._target), metadata=sanitize(self._metadata),
                result=sanitize(result), error=error, counters=dict(self.counters),
                output=body, omitted=omitted))
        except Exception:
            pass

    def start(self, message: str = "") -> "TraceContext":
        self._emit(RUNNING, message=message or f"{self.name} starting")
        return TraceContext(self._ctx._trace, parent_id=self.id)

    def tick(self, message: str = "", counters: dict | None = None) -> None:
        """A progress update while still running. Re-emits under the same
        event id, so the browser updates one row instead of adding rows."""
        self._emit(RUNNING, message=message, counters=counters)

    def skip(self, message: str = "") -> None:
        self._emit(SKIPPED, message=message)

    def cancel(self, message: str = "cancelled") -> None:
        self._emit(CANCELLED, message=message)

    def finish(self, state: str = COMPLETED, *, message: str = "",
              result=None, error: str = "", counters: dict | None = None,
              output: str = "") -> None:
        self._emit(state, message=message, result=result, error=error,
                  counters=counters, output=output)

    def __enter__(self) -> "TraceContext":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.finish(COMPLETED)
        else:
            self.finish(FAILED, error=f"{exc_type.__name__}: {exc}")
        return False  # never swallow the real exception


class TraceContext:
    """A handle bound to one Trace and one 'current parent' id.

    Passed as an explicit optional argument through the existing
    synchronous call chain (the same pattern `stats: dict | None` and
    `progress: Progress` already use in this codebase), rather than global
    or thread-local state -- correct by construction across the worker
    threads `harvest_jobs` and the crawler's thread pool already use,
    without needing those threads to inherit anything implicitly.
    """

    def __init__(self, trace: Trace, parent_id: str | None = None):
        self._trace = trace
        self.parent_id = parent_id

    @property
    def trace_id(self) -> str:
        return self._trace.id

    def event(self, name: str, *, state: str = COMPLETED, message: str = "",
             target: str = "", metadata: dict | None = None, result=None,
             error: str = "", counters: dict | None = None,
             output: str = "") -> None:
        """One fire-and-forget fact -- no lifecycle of its own to track."""
        body, omitted = clip(output) if output else ("", 0)
        try:
            self._trace.append(TraceEvent(
                id=self._trace.next_id(), trace_id=self._trace.id,
                parent_id=self.parent_id, type="event", name=name, state=state,
                message=message, target=sanitize(target),
                metadata=sanitize(metadata or {}), result=sanitize(result),
                error=error, counters=counters or {}, output=body,
                omitted=omitted))
        except Exception:
            pass

    def stage(self, name: str, *, message: str = "", target: str = "",
             metadata: dict | None = None) -> Stage:
        return Stage(self, name, message=message, target=target, metadata=metadata)

    def child(self, parent_id: str) -> "TraceContext":
        """A context scoped to a specific stage id, for callers that already
        hold a Stage from elsewhere (crossing a function boundary)."""
        return TraceContext(self._trace, parent_id=parent_id)

    def detach(self) -> None:
        """Declare that the real work under this trace continues after the
        current call returns -- a harvest still running on a background
        thread past its deadline, most often.

        `run_tool()` will not close a detached trace when the tool call it
        wraps returns; whoever calls `detach()` is responsible for calling
        `close()` itself once the work truly ends, however long that takes.
        """
        self._trace.keep_open = True

    def is_detached(self) -> bool:
        """True once `detach()` has been called -- run_tool() checks this
        (indirectly) to decide whether it still owns closing this trace;
        code on the far side of a background thread checks it to decide
        whether *it* now does."""
        return self._trace.keep_open

    def close(self) -> None:
        try:
            self._trace.close()
        except Exception:
            pass


class _NullStage:
    def start(self, message: str = "") -> "TraceContext":
        return NULL_CONTEXT

    def tick(self, *a, **k) -> None:
        pass

    def skip(self, *a, **k) -> None:
        pass

    def cancel(self, *a, **k) -> None:
        pass

    def finish(self, *a, **k) -> None:
        pass

    def __enter__(self) -> "TraceContext":
        return NULL_CONTEXT

    def __exit__(self, *a) -> bool:
        return False


class _NullContext:
    """What every instrumented function gets when nobody asked for a trace
    -- direct calls from tests, from mcp_server.py, or from any tool this
    hardening pass did not reach. Same interface as TraceContext, every
    method a no-op, so call sites never need an `if trace:` guard."""

    trace_id = None
    parent_id = None

    def event(self, *a, **k) -> None:
        pass

    def stage(self, *a, **k) -> _NullStage:
        return _NullStage()

    def child(self, *a, **k) -> "_NullContext":
        return self

    def detach(self) -> None:
        pass

    def is_detached(self) -> bool:
        return False

    def close(self) -> None:
        pass


NULL_CONTEXT = _NullContext()


def start(label: str = "", trace_id: str | None = None) -> TraceContext:
    """Begin a new top-level trace and return its root context."""
    return TraceContext(create(label, trace_id=trace_id), parent_id=None)
