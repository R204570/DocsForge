"""
The lab's test runner: tests are queued, run on worker threads, and every tool
they call is recorded with what it returned.

Three kinds, each run the way DocsForge itself runs them:

  resolve   `resolver.resolve` with the cache off (as the held-out measure
            does), reported in the words `find_docs` gives a model; then the
            resolved page fetched with `fetch_docs`, so a tester can check the
            content as well as the address
  fetch     `detect_source_type` and `fetch_docs` on one URL, read for what
            extraction left behind
  harvest   `learn_technology` (a name) or `harvest_docs` (a URL), in a
            subprocess with a knowledge base, caches and harvest records of
            its own -- a test harvest must never land in the real store, and
            on the machine this was built on the real store is a hosted
            database. Its trace streams back live; its pages are measured.

Resolution and fetch tests share a small pool (`DOCSFORGE_LAB_WORKERS`, 2);
harvests have their own (`DOCSFORGE_LAB_HARVESTS`, 1), because one harvest
already fetches pages concurrently and two at once mostly means two sites
rate-limiting the same address.

A test that was running when the server stopped is marked failed on the next
start, and one still queued is queued again.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from docsforge import lab
from docsforge.lab import activity, db, quality

RESOLVE, FETCH, HARVEST = "resolve", "fetch", "harvest"
KINDS = (RESOLVE, FETCH, HARVEST)
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = (DONE, FAILED, CANCELLED)

FAST_WORKERS = max(1, int(os.environ.get("DOCSFORGE_LAB_WORKERS", "2") or 2))
HARVEST_WORKERS = max(1, int(os.environ.get("DOCSFORGE_LAB_HARVESTS", "1") or 1))

#: A step's output as stored: the whole of what a tool returned, up to here.
#: `fetch_docs` is itself capped (DOCSFORGE_MAX_CHARS), so this rarely bites.
STEP_OUTPUT = 250_000
#: How often a running harvest's live trace is written back for the browser.
PERSIST_EVERY = 1.0
MAX_BATCH = 300

#: The line prefix the harvest worker marks its own messages with; anything
#: else it prints is the engine talking, kept as the run's log.
MARK = "@@lab "

_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)


class LabInputError(ValueError):
    """A test that cannot be run as asked; the message says why."""


# ── input ───────────────────────────────────────────────────
def _text(raw: dict, key: str, limit: int = 200) -> str:
    value = raw.get(key)
    value = "" if value is None else str(value).strip()
    if len(value) > limit:
        raise LabInputError(f"{key} is longer than {limit} characters.")
    return value


def _flag(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _number(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if value in (None, ""):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise LabInputError(f"{key} must be a whole number.") from None
    if not low <= value <= high:
        raise LabInputError(f"{key} must be between {low} and {high}.")
    return value


def _language(raw: dict) -> str:
    said = _text(raw, "language", 40)
    if not said:
        return ""
    from docsforge.core import languages
    lang = languages.canonical(said)
    if lang is None:
        raise LabInputError(f"Unknown language {said!r}.")
    return lang.name


def _url(raw: dict, key: str = "url") -> str:
    url = _text(raw, key, 2000)
    if url and not _URL.match(url):
        raise LabInputError(f"{url!r} is not an http(s) URL.")
    return url


def validate(kind: str, raw: dict) -> dict:
    """The input a test runs with, normalised; raises LabInputError."""
    raw = raw or {}
    if kind == RESOLVE:
        name = _text(raw, "name", 120)
        if not name:
            raise LabInputError("A resolution test needs a name.")
        ecosystem = _text(raw, "ecosystem", 20).lower()
        if ecosystem and ecosystem not in ("npm", "pypi", "crates"):
            raise LabInputError("Ecosystem is npm, pypi or crates, or empty.")
        return {"name": name, "language": _language(raw), "ecosystem": ecosystem,
                "expected": quality.parse_expected(raw.get("expected")),
                "use_memory": _flag(raw, "use_memory", False),
                "fetch_page": _flag(raw, "fetch_page", True)}
    if kind == FETCH:
        url = _url(raw)
        if not url:
            raise LabInputError("A content test needs a URL.")
        return {"url": url, "name": _text(raw, "name", 120), "js": _flag(raw, "js", False)}
    if kind == HARVEST:
        name, url = _text(raw, "name", 120), _url(raw)
        if not (name or url):
            raise LabInputError("A harvest test needs a name or a URL.")
        return {"name": name, "url": url, "language": _language(raw),
                "topic": _text(raw, "topic", 120), "version": _text(raw, "version", 40),
                "max_pages": _number(raw, "max_pages", 40, 0, 5000),
                "js": _flag(raw, "js", False),
                "expected": quality.parse_expected(raw.get("expected")),
                "timeout_min": _number(raw, "timeout_min", 30, 1, 240)}
    raise LabInputError(f"Unknown test kind {kind!r}.")


def parse_batch(kind: str, text: str, defaults: dict | None = None) -> list[dict]:
    """One test per line.

      resolve   name | language | expected, expected
      fetch     url | name
      harvest   name or url | language | max pages

    `#` starts a comment. `defaults` fills whatever a line leaves out.
    """
    items = []
    for raw_line in (text or "").splitlines():
        # A `#` after a space (or opening the line) is a comment; one inside
        # a URL is its fragment.
        line = re.sub(r"(^|\s)#.*$", "", raw_line).strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        item = dict(defaults or {})
        if kind == RESOLVE:
            item["name"] = parts[0]
            if len(parts) > 1 and parts[1]:
                item["language"] = parts[1]
            if len(parts) > 2 and parts[2]:
                item["expected"] = parts[2]
        elif kind == FETCH:
            item["url"] = parts[0]
            if len(parts) > 1 and parts[1]:
                item["name"] = parts[1]
        elif kind == HARVEST:
            key = "url" if parts[0].lower().startswith(("http://", "https://")) else "name"
            item[key] = parts[0]
            if len(parts) > 1 and parts[1]:
                item["language"] = parts[1]
            if len(parts) > 2 and parts[2]:
                item["max_pages"] = parts[2]
        else:
            raise LabInputError(f"Unknown test kind {kind!r}.")
        items.append(validate(kind, item))
    if not items:
        raise LabInputError("The list is empty.")
    if len(items) > MAX_BATCH:
        raise LabInputError(f"A batch holds at most {MAX_BATCH} tests.")
    return items


# ── steps: the tools a test ran, and what each returned ─────
class Step:
    def __init__(self, test_id: int, tool: str, args: dict):
        self.test_id = test_id
        self.started = time.time()
        seq = (db.scalar("SELECT COALESCE(MAX(seq), 0) FROM steps WHERE test_id = ?",
                         (test_id,)) or 0) + 1
        self.id = db.execute(
            "INSERT INTO steps (test_id, seq, tool, args, state, started) VALUES (?, ?, ?, ?, ?, ?)",
            (test_id, seq, tool, db.dumps(args), RUNNING, self.started))

    def events(self, events: list[dict], trace_id: str = "") -> None:
        db.execute("UPDATE steps SET events = ?, trace_id = COALESCE(NULLIF(?, ''), trace_id), "
                   "duration_ms = ? WHERE id = ?",
                   (db.dumps(events), trace_id, round((time.time() - self.started) * 1000, 1),
                    self.id))

    def finish(self, ok: bool, output: str = "", *, error: str = "", trace_id: str = "",
               events: list[dict] | None = None, omitted: int = 0) -> None:
        text = output or ""
        cut = max(0, len(text) - STEP_OUTPUT)
        db.execute(
            "UPDATE steps SET state = ?, duration_ms = ?, output = ?, omitted = ?, "
            "trace_id = COALESCE(NULLIF(?, ''), trace_id), events = COALESCE(?, events), "
            "error = ? WHERE id = ?",
            (DONE if ok else FAILED, round((time.time() - self.started) * 1000, 1),
             text[:STEP_OUTPUT], omitted + cut, trace_id,
             db.dumps(events) if events is not None else None, error[:2000], self.id))


def _trace_events(trace_id: str | None) -> list[dict]:
    from docsforge.tools import tracing
    trace = tracing.get(trace_id) if trace_id else None
    if trace is None:
        return []
    events = [e.as_dict() for e in trace.events()]
    return [activity._compact(e, e.get("parent_id") is not None) for e in events]


def _tool_step(test_id: int, tool: str, args: dict) -> tuple[str, bool]:
    """Run one DocsForge tool exactly as a model's call runs it, and record it."""
    from docsforge.tools import forge_tools
    step = Step(test_id, tool, args)
    try:
        text, ok = forge_tools.run_tool_checked(tool, args)
    except Exception as e:                              # noqa: BLE001 -- recorded
        step.finish(False, error=f"{type(e).__name__}: {e}")
        raise
    trace_id = forge_tools.last_trace_id() or ""
    if trace_id:
        activity.tag(trace_id, source="lab", test_id=test_id)
    step.finish(ok, text, trace_id=trace_id, events=_trace_events(trace_id),
                error="" if ok else text[:500])
    return text, ok


def _mentions(text: str, name: str) -> int:
    """How often a page names the technology -- `three.js` also as `threejs`.
    A hint for the tester, not the identity gate."""
    if not name:
        return 0
    body = (text or "").lower()
    found = body.count(name.lower())
    bare = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not found and bare and bare != name.lower():
        found = body.count(bare)
    return found


def _page_result(text: str, ok: bool, url: str, name: str = "") -> dict:
    from docsforge.tools import forge_tools
    metrics = quality.assess(text)
    return {"url": url, "ok": ok, "chars": len(text or ""), "kind": forge_tools.kind_of(text),
            "metrics": metrics, "flags": quality.page_flags(metrics) if ok else ["error"],
            "mentions": _mentions(text, name)}


# ── the three kinds ─────────────────────────────────────────
def _run_resolve(test_id: int, inp: dict) -> tuple[dict, dict | None]:
    from docsforge.core import languages, resolver
    from docsforge.tools import forge_tools

    lang = languages.canonical(inp["language"]) if inp["language"] else None
    args = {"name": inp["name"], "language": inp["language"], "ecosystem": inp["ecosystem"],
            "use_memory": inp["use_memory"]}
    step = Step(test_id, "find_docs", args)
    started = time.perf_counter()
    try:
        found = resolver.resolve(inp["name"], inp["ecosystem"], use_memory=inp["use_memory"],
                                 language=lang.name if lang else "")
    except Exception as e:                              # noqa: BLE001 -- recorded
        step.finish(False, f"Error: {type(e).__name__}: {e}", error=f"{type(e).__name__}: {e}")
        raise
    seconds = round(time.perf_counter() - started, 2)
    report = forge_tools.resolution_report(inp["name"], found, lang)
    events = [{"id": "resolution", "type": "event", "name": "resolution",
               "state": "completed", "parent_id": None,
               "message": f"{len(found.candidates)} candidate(s), "
                          f"via {found.resolved_via or 'nothing'}",
               "result": found.as_dict()}]
    step.finish(True, report, events=events)
    activity.record_call(f"lab-{test_id}-{step.id}", "find_docs", args=args, output=report,
                         ok=True, started=step.started, duration_ms=seconds * 1000,
                         events=events, target=inp["name"], test_id=test_id)
    best = found.best.url if found.best else ""
    result = {"resolution": found.as_dict(), "best_url": best, "via": found.resolved_via,
              "seconds": seconds, "cached": inp["use_memory"],
              "headline": best or "refused — nothing verified"}

    auto = None
    if inp["expected"]:
        hit = quality.matches(best, inp["expected"], follow=quality.lands_on)
        auto = {"pass": hit,
                "reason": (f"on {', '.join(inp['expected'])}" if hit else
                           f"expected {', '.join(inp['expected'])}; got {best or 'a refusal'}")}
    if inp["fetch_page"] and best:
        text, ok = _tool_step(test_id, "fetch_docs", {"url": best})
        result["page"] = _page_result(text, ok, best, inp["name"])
    return result, auto


def _run_fetch(test_id: int, inp: dict) -> tuple[dict, dict | None]:
    kind, kind_ok = _tool_step(test_id, "detect_source_type", {"url": inp["url"]})
    args = {"url": inp["url"]} | ({"js": True} if inp["js"] else {})
    text, ok = _tool_step(test_id, "fetch_docs", args)
    page = _page_result(text, ok, inp["url"], inp["name"])
    page["source_type"] = kind.strip() if kind_ok else ""
    reasons = []
    if not ok:
        reasons.append("the fetch failed")
    elif page["chars"] < quality.THIN:
        reasons.append(f"only {page['chars']} characters")
    if ok and not quality.clean(page["metrics"]):
        reasons.append(", ".join(f for f in page["flags"] if f != "thin"))
    if inp["name"] and ok and page["mentions"] == 0:
        reasons.append(f"never mentions {inp['name']}")
    page["headline"] = (f"{page['chars']:,} characters · {page['kind'] or page['source_type'] or 'page'}"
                        if ok else "fetch failed")
    return page, {"pass": not reasons, "reason": "; ".join(reasons) or "substantial and clean"}


def harvest_command(run_dir: Path) -> list[str]:
    """The worker's command line. A function so a test can substitute one."""
    return [sys.executable, "-m", "docsforge.lab.harvest_worker", str(run_dir)]


def _child_env(run_dir: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        # Never the real store, whatever .env says: set empty, since dotenv
        # does not override a variable that is set.
        "DOCSFORGE_DB": "", "DATABASE_URL": "",
        "DOCSFORGE_KB_ROOT": str(run_dir / "kb"),
        "DOCSFORGE_RESOLVE_CACHE": str(run_dir / "resolutions.json"),
        "DOCSFORGE_SELECTION_POLICY": str(run_dir / "selection.json"),
        "DOCSFORGE_HARVEST_STATE": str(run_dir / "state"),
        "DOCSFORGE_LOG_DIR": str(run_dir / "logs"),
        # Finish inside the call: the worker *is* the background.
        "DOCSFORGE_HARVEST_DEADLINE": "864000",
        "DOCSFORGE_LAB": "0",
    })
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """The worker and whatever it started -- a Chromium for rendering, most often."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:                                   # noqa: BLE001
        pass


def run_dir(test_id: int) -> Path:
    return lab.data_dir() / "runs" / f"test-{test_id}"


def _run_harvest(test_id: int, inp: dict) -> tuple[dict, dict | None]:
    where = run_dir(test_id)
    shutil.rmtree(where, ignore_errors=True)
    where.mkdir(parents=True, exist_ok=True)
    if inp["url"]:
        tool = "harvest_docs"
        args = {"url": inp["url"]}
        if inp["name"]:
            args["name"] = inp["name"]
    else:
        tool = "learn_technology"
        args = {"name": inp["name"]}
        if inp["language"]:
            args["language"] = inp["language"]
    for key in ("topic", "version"):
        if inp[key]:
            args[key] = inp[key]
    args["max_pages"] = inp["max_pages"]
    if inp["js"]:
        args["js"] = True
    (where / "spec.json").write_text(json.dumps({"tool": tool, "args": args}), encoding="utf-8")

    step = Step(test_id, tool, args)
    started = time.time()
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" \
        else {"start_new_session": True}
    from docsforge import ROOT
    proc = subprocess.Popen(harvest_command(where), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=_child_env(where), cwd=str(ROOT), text=True,
                            encoding="utf-8", errors="replace", bufsize=1, **kwargs)
    RUNNER.attach(test_id, proc)
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(inp["timeout_min"] * 60, expire)
    timer.daemon = True
    timer.start()

    events: dict[str, dict] = {}
    output = {"text": "", "ok": False, "trace_id": "", "omitted": 0}
    tail: list[str] = []
    last_write = 0.0
    try:
        with open(where / "log.txt", "w", encoding="utf-8") as log:
            for line in proc.stdout:
                if line.startswith(MARK):
                    try:
                        msg = json.loads(line[len(MARK):])
                    except ValueError:
                        continue
                    if msg.get("type") == "event":
                        ev = msg.get("event") or {}
                        events[ev.get("id") or str(len(events))] = ev
                        output["trace_id"] = msg.get("trace_id") or output["trace_id"]
                    elif msg.get("type") == "output":
                        output.update(text=msg.get("text") or "", ok=bool(msg.get("ok")),
                                      trace_id=msg.get("trace_id") or output["trace_id"],
                                      omitted=msg.get("omitted") or 0)
                    if time.time() - last_write >= PERSIST_EVERY:
                        step.events(list(events.values()), output["trace_id"])
                        last_write = time.time()
                else:
                    log.write(line)
                    tail.append(line.rstrip())
                    del tail[:-60]
        code = proc.wait()
    finally:
        timer.cancel()
        RUNNER.detach(test_id)

    summary = {}
    path = where / "result.json"
    if path.exists():
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            summary = {}
    cancelled = RUNNER.was_cancelled(test_id)
    error = ""
    if cancelled:
        error = "cancelled by a tester"
    elif timed_out.is_set():
        error = f"stopped after {inp['timeout_min']} minutes"
    elif not output["text"]:
        error = f"the harvest process ended ({code}) without a result"
    elif not output["ok"]:
        error = output["text"].strip().splitlines()[0][:300]
    ok = output["ok"] and not error
    text = output["text"] or ("Error: " + error)
    step.finish(ok, text, error=error if not ok else "", trace_id=output["trace_id"],
                events=list(events.values()), omitted=output["omitted"])
    # The worker's trace closed in the worker; the record of what ran is ours.
    activity.record_call(f"worker:{test_id}:{output['trace_id'] or step.id}", tool, args=args,
                         output=text, ok=ok, started=started,
                         duration_ms=(time.time() - started) * 1000,
                         events=list(events.values()), omitted=output["omitted"],
                         target=inp["url"] or inp["name"], test_id=test_id)

    entries = summary.get("entries") or []
    pages = summary.get("pages") or []
    main = max(entries, key=lambda e: e.get("pages") or 0) if entries else {}
    judged = quality.harvest_verdict({
        "pages": pages, "expected": main.get("expected"),
        "complete": main.get("complete"), "error": error})
    if inp["expected"] and main.get("source"):
        hit = quality.matches(main["source"], inp["expected"])
        if not hit:
            judged["pass"] = False
            judged["reason"] += f"; harvested from {main['source']}, expected " \
                                f"{', '.join(inp['expected'])}"
    result = {
        "tool": tool, "args": args, "seconds": round(time.time() - started, 1),
        "returncode": code, "entries": entries, "pages": pages,
        "totals": summary.get("totals") or {}, "log_tail": tail[-40:],
        "error": error or summary.get("error") or "",
        "headline": _harvest_headline(judged, entries, main) if pages else (error or "nothing stored"),
    }
    if cancelled:
        raise _Cancelled(result)
    return result, judged


def _harvest_headline(judged: dict, entries: list[dict], main: dict) -> str:
    """"27 pages in 4 sets · the main one 7 of 67 listed, by crawl"."""
    head = f"{judged['pages']} page{'s' if judged['pages'] != 1 else ''}"
    if len(entries) > 1:
        head += f" in {len(entries)} sets · the main one {main.get('pages', 0)}"
    if main.get("expected"):
        head += f" of {main['expected']} listed"
    if main.get("strategy"):
        head += f", by {main['strategy']}"
    return head


class _Cancelled(Exception):
    def __init__(self, result: dict):
        super().__init__("cancelled")
        self.result = result


_EXECUTORS = {RESOLVE: _run_resolve, FETCH: _run_fetch, HARVEST: _run_harvest}


# ── the queue ───────────────────────────────────────────────
class Runner:
    def __init__(self):
        self._fast: queue.Queue = queue.Queue()
        self._slow: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._procs: dict[int, subprocess.Popen] = {}
        self._cancelled: set[int] = set()

    def _ensure_workers(self) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            if self._threads:
                return
            for i in range(FAST_WORKERS):
                self._spawn(self._fast, f"lab-fast-{i}")
            for i in range(HARVEST_WORKERS):
                self._spawn(self._slow, f"lab-harvest-{i}")

    def _spawn(self, q: queue.Queue, name: str) -> None:
        t = threading.Thread(target=self._work, args=(q,), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def enqueue(self, test_id: int, kind: str) -> None:
        self._ensure_workers()
        (self._slow if kind == HARVEST else self._fast).put(test_id)

    def attach(self, test_id: int, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[test_id] = proc

    def detach(self, test_id: int) -> None:
        with self._lock:
            self._procs.pop(test_id, None)

    def was_cancelled(self, test_id: int) -> bool:
        with self._lock:
            return test_id in self._cancelled

    def cancel(self, test_id: int) -> str:
        """"cancelled", "stopping", or why it cannot be."""
        found = db.row("SELECT state, kind FROM tests WHERE id = ?", (test_id,))
        if not found:
            return "no such test"
        if found["state"] == QUEUED:
            db.execute("UPDATE tests SET state = ?, finished = ?, error = ? WHERE id = ? "
                       "AND state = ?", (CANCELLED, time.time(), "cancelled before it ran",
                                         test_id, QUEUED))
            return CANCELLED
        if found["state"] != RUNNING:
            return f"already {found['state']}"
        with self._lock:
            proc = self._procs.get(test_id)
            if proc is None:
                return "a resolution or fetch cannot be stopped mid-request; it ends on its own"
            self._cancelled.add(test_id)
        _kill_tree(proc)
        return "stopping"

    def _work(self, q: queue.Queue) -> None:
        while True:
            test_id = q.get()
            try:
                self.run(test_id)
            except Exception as e:                      # noqa: BLE001 -- never kill a worker
                try:
                    from docsforge.tools import applog
                    applog.error("lab_runner", f"test {test_id}: {type(e).__name__}: {e}")
                except Exception:                       # noqa: BLE001
                    pass
            finally:
                q.task_done()

    def run(self, test_id: int) -> None:
        """Run one queued test to its end, on this thread."""
        with db.write() as cx:
            claimed = cx.execute("UPDATE tests SET state = ?, started = ? WHERE id = ? AND state = ?",
                                 (RUNNING, time.time(), test_id, QUEUED)).rowcount
        if not claimed:
            return                                      # cancelled while queued
        found = db.row("SELECT kind, input, created_by FROM tests WHERE id = ?", (test_id,))
        inp = db.loads(found["input"], {})
        who = db.scalar("SELECT username FROM users WHERE id = ?", (found["created_by"],)) or ""
        state, result, auto, error = DONE, {}, None, ""
        try:
            result, auto = _EXECUTORS[found["kind"]](test_id, inp)
            if found["kind"] == HARVEST and result.get("error"):
                state, error = FAILED, result["error"]
        except _Cancelled as c:
            state, result, error = CANCELLED, c.result, "cancelled by a tester"
        except Exception as e:                          # noqa: BLE001 -- the test's outcome
            state, error = FAILED, f"{type(e).__name__}: {e}"
        with self._lock:
            self._cancelled.discard(test_id)
        db.execute("UPDATE tests SET state = ?, finished = ?, result = ?, auto = ?, error = ? "
                   "WHERE id = ?", (state, time.time(), db.dumps(result),
                                    db.dumps(auto) if auto is not None else None, error, test_id))
        db.log_event("test_finished", who, test=test_id, test_kind=found["kind"], state=state,
                     auto=(auto or {}).get("pass"))


RUNNER = Runner()


# ── creating tests ──────────────────────────────────────────
def submit(kind: str, raw: dict, user: dict, *, batch_id: int | None = None,
           parent_id: int | None = None, validated: bool = False) -> int:
    inp = raw if validated else validate(kind, raw)
    test_id = db.execute(
        "INSERT INTO tests (kind, batch_id, parent_id, created_by, created, state, input) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, batch_id, parent_id, user["id"], time.time(), QUEUED, db.dumps(inp)))
    if batch_id is None:
        db.log_event("test_started", user["username"], test=test_id, test_kind=kind,
                     target=inp.get("name") or inp.get("url"))
    RUNNER.enqueue(test_id, kind)
    return test_id


def create_batch(kind: str, items: list[dict], user: dict, title: str = "",
                 options: dict | None = None) -> tuple[int, list[int]]:
    if kind not in KINDS:
        raise LabInputError(f"Unknown test kind {kind!r}.")
    batch_id = db.execute(
        "INSERT INTO batches (kind, title, created_by, created, options, total) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, (title or "").strip()[:120] or f"{len(items)} {kind} tests", user["id"],
         time.time(), db.dumps(options or {}), len(items)))
    ids = [submit(kind, item, user, batch_id=batch_id, validated=True) for item in items]
    db.log_event("batch_started", user["username"], batch=batch_id, test_kind=kind, tests=len(ids))
    return batch_id, ids


def rerun(test_id: int, user: dict) -> int:
    found = db.row("SELECT kind, input FROM tests WHERE id = ?", (test_id,))
    if not found:
        raise LabInputError("No such test.")
    return submit(found["kind"], db.loads(found["input"], {}), user, parent_id=test_id,
                  validated=True)


def recover() -> None:
    """Called once at startup: what was running died with the last process."""
    now = time.time()
    db.execute("UPDATE tests SET state = ?, finished = ?, error = ? WHERE state = ?",
               (FAILED, now, "the server stopped while this test was running", RUNNING))
    db.execute("UPDATE steps SET state = ? WHERE state = ?", (FAILED, RUNNING))
    for r in db.rows("SELECT id, kind FROM tests WHERE state = ? ORDER BY id", (QUEUED,)):
        RUNNER.enqueue(r["id"], r["kind"])


# ── presets for batches ─────────────────────────────────────
def presets() -> list[dict]:
    """Ready-made lists: the held-out rounds and the field-test sites, when
    this is a checkout that has them (they live in `scripts/`, not the package)."""
    from docsforge import ROOT
    out = []
    held = _load_script(ROOT / "scripts" / "heldout.py", "_lab_heldout")
    for name, cases in (getattr(held, "SETS", None) or {}).items():
        lines = [f"{n} | {lang} | {', '.join(exp)}" for n, lang, exp in cases]
        out.append({"id": f"heldout-{name}", "kind": RESOLVE,
                    "title": f"Held-out {name} ({len(cases)} names, answers written down)",
                    "text": "\n".join(lines)})
    field = _load_script(ROOT / "scripts" / "fieldtest.py", "_lab_fieldtest")
    sites = getattr(field, "SITES", None) or []
    if sites:
        out.append({"id": "fieldtest-fetch", "kind": FETCH,
                    "title": f"Field-test sites, one page each ({len(sites)})",
                    "text": "\n".join(url for _label, _gen, url in sites)})
        out.append({"id": "fieldtest-harvest", "kind": HARVEST,
                    "title": f"Field-test sites, harvested ({len(sites)})",
                    "text": "\n".join(f"{url} | | 15" for _label, _gen, url in sites)})
    return out


def _load_script(path: Path, alias: str):
    if not path.exists():
        return None
    if alias in sys.modules:
        return sys.modules[alias]
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(path.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(path.parent))
        sys.modules[alias] = module
        return module
    except Exception:                                   # noqa: BLE001 -- a preset is optional
        return None
