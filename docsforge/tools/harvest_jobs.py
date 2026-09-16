"""
Background harvests, so the headline tool stops timing out.

`learn_technology` used to block for as long as the harvest took — around twelve
minutes for a 703-page site — while every MCP client gives up long before that.
The tool was not merely slow, it read as broken, which is worse.

What changes here is not the harvest but who waits for it. A call returns
whatever is ready by a deadline (`DOCSFORGE_HARVEST_DEADLINE`, 25 seconds).
A harvest that finishes inside the deadline returns exactly what it always
returned, word for word — that is the common case and it is deliberately
untouched. One that does not keeps running on its own thread, and the caller
gets a harvest id and a progress line instead of a stalled connection.

**This is still not a job queue and should not become one.** A harvest runs on
a daemon thread, dies with the process that started it, and is never resumed:
the durable record of a harvest is the knowledge-base entry it writes, and a
harvest that vanished before writing one simply did not happen.

What *is* published, since it turned out to be the difference between a working
feature and an invisible one, is a **status record**. Jobs used to live only in
this module's dict, and that is fine for one process and useless the moment
there are two. The `claudecode` provider launches the CLI, which launches
`mcp_server.py` itself, so every turn runs its tools in a fresh subprocess:
`learn_technology` started a harvest in one process and the `list_knowledge_base`
that was supposed to report it ran in another, which had never heard of it. The
tool told the user to watch a progress line that could not exist, and the same
harvest id came back twice because the counter had reset.

So each job writes a small JSON record under `~/.docsforge/harvests/`, refreshed
on a heartbeat while it runs, and every process reads all of them. The record is
status, not state: nothing is ever resumed from it, and when its heartbeat stops
it is reported **stalled**, never "running". A record that outlives its process
is a record whose process died, and saying so is the whole point.

A file reaches every process on one machine and no further. A serverless host
gives each instance its own `/tmp`, so on Vercel a harvest that failed on one
instance was listed by `harvest_status` once and, two calls later on another
instance, "none finished, failed or stopped in the last 15 minutes" — the same
lie, one machine wider. So the same record also goes to the **shared ledger**
when there is one: the knowledge-base store, which is the one thing every
DocsForge on a database has in common. The tool layer wires it in (`SHARED`);
this module reads both and believes the fresher copy.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from docsforge.core.engine import ForgeError

# Long enough to swallow the great majority of harvests whole, short enough to
# sit inside the shortest MCP client timeout. Both bounds matter, so this is
# tunable but not per-call: a caller who could pick it would pick "forever".
DEADLINE = float(os.environ.get("DOCSFORGE_HARVEST_DEADLINE", "25"))

# Finished jobs are kept so the next list_knowledge_base can say how a harvest
# ended. Small enough that nobody mistakes it for storage.
KEEP_FINISHED = 10

#: How often a running job rewrites its record. A harvest can spend a minute
#: resolving without fetching anything, so the heartbeat cannot be driven by
#: progress — it has to be its own clock, or a slow start looks like a death.
#: Kept close to the UI's own poll: at five seconds a page count sat visibly
#: frozen between beats, which reads as a stall rather than as progress.
HEARTBEAT = 2.0

#: A running record older than this is not believed. Six missed heartbeats:
#: loose enough to survive a stalled fetch or a loaded machine, tight enough
#: that a killed process stops being reported as working within half a minute.
STALE_AFTER = 30.0

#: How long a finished record stays readable. Long enough to answer "what
#: happened to that harvest?" on the next turn, short enough not to accumulate.
KEEP_RECORDS_FOR = 900.0

RUNNING, DONE, FAILED, STALLED = "running", "done", "failed", "stalled"


class Ledger(Protocol):
    """Where records are shared beyond this machine — see `SHARED`."""

    def publish_harvest(self, record: dict) -> None: ...
    def harvests(self) -> list[dict]: ...
    def forget_harvest(self, job_id: str) -> None: ...


#: Returns the shared ledger, or None when records stay on this machine. Set
#: by the tool layer to hand back the knowledge-base store when that store is
#: a database (`PostgresStore` implements `Ledger`; a directory of files is
#: already what the records under `state_dir()` are, so a file store adds
#: nothing). A callable rather than an object because the store is built
#: lazily and can be swapped — a database that was down at startup and came
#: back must start receiving records without anyone re-wiring this.
SHARED: Callable[[], Ledger | None] = lambda: None


def _shared() -> Ledger | None:
    """The ledger, or None. Never raises: status must not fail a harvest, and
    an unreachable database is the store's to report, not this module's."""
    try:
        return SHARED()
    except Exception:                              # noqa: BLE001
        return None


def state_dir() -> Path:
    """Where status records live. Overridable, mainly so tests do not share."""
    return Path(os.environ.get("DOCSFORGE_HARVEST_STATE")
                or Path.home() / ".docsforge" / "harvests")


@dataclass
class Progress:
    """What a running harvest is willing to say about itself.

    Written by the worker thread, read by everyone else. Every field is a
    plain assignment of an immutable value, which is the only reason this
    needs no lock of its own.
    """

    phase: str = "starting"          # resolving | harvesting | storing
    url: str = ""
    pages: int = 0                   # pages fetched so far
    expected: int | None = None      # the site's own count, when known

    def __setattr__(self, name: str, value) -> None:
        """Publish a phase change at once rather than at the next heartbeat.

        A phase shorter than the beat was otherwise invisible: a two-second
        `resolving` between two beats never appeared anywhere, so a watcher saw
        `starting` and then `harvesting 21/200` and nothing in between. Page
        counts are left to the heartbeat — they tick hundreds of times and the
        beat is what bounds that to one write every couple of seconds.
        """
        moved = name == "phase" and getattr(self, "phase", None) != value
        object.__setattr__(self, name, value)
        if moved:
            hook = getattr(self, "_moved", None)
            if hook is not None:
                try:
                    hook()
                except Exception:              # noqa: BLE001
                    pass                       # never fail a harvest to report it

    def line(self) -> str:
        if self.phase == "harvesting" and self.expected:
            return f"harvesting {self.pages}/{self.expected} pages"
        if self.phase == "harvesting":
            return f"harvesting, {self.pages} pages so far"
        return self.phase

    def fraction(self) -> float | None:
        """How far along, or None when there is no honest denominator.

        A crawl's frontier is not a denominator — it grows as you walk it — so
        a crawl returns None here and the UI shows elapsed time instead of an
        invented percentage.
        """
        if self.phase == "harvesting" and self.expected:
            return max(0.0, min(1.0, self.pages / self.expected))
        return None


@dataclass
class Job:
    """One harvest, running or finished."""

    id: str
    label: str                       # what is being learned, for a human
    started: float
    progress: Progress = field(default_factory=Progress)
    state: str = RUNNING             # running | done | failed | stalled
    result: str = ""
    error: str = ""
    # The exception object itself, not a rendering of it. A caller still
    # inside the deadline re-raises this one, so a failure that would have
    # surfaced as a ForgeError with a candidate list still does.
    exc: BaseException | None = None
    finished: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    #: Which process owns this job, and when it last said so. Both only
    #: matter for jobs read back from another process's record.
    pid: int = 0
    updated: float = 0.0
    mine: bool = True                # False when loaded from someone's record

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    def line(self) -> str:
        """One line describing this job, for list_knowledge_base."""
        mins = f"{self.elapsed:.0f}s" if self.elapsed < 90 else f"{self.elapsed / 60:.0f}m"
        if self.state == RUNNING:
            return f"- **{self.label}** — {self.progress.line()}, {mins} elapsed  ·  `{self.id}`"
        if self.state == STALLED:
            return (f"- **{self.label}** — STOPPED REPORTING after {mins}, last seen "
                    f"{self.progress.line()}. The process running it most likely "
                    f"exited; nothing was stored.  ·  `{self.id}`")
        if self.state == FAILED:
            return f"- **{self.label}** — FAILED after {mins}: {self.error}  ·  `{self.id}`"
        return f"- **{self.label}** — finished in {mins}  ·  `{self.id}`"

    def as_dict(self) -> dict:
        """The wire and on-disk shape. One definition, so they cannot drift."""
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "phase": self.progress.phase,
            "url": self.progress.url,
            "pages": self.progress.pages,
            "expected": self.progress.expected,
            "fraction": self.progress.fraction(),
            "line": self.progress.line(),
            "started": self.started,
            "updated": self.updated,
            "finished": self.finished,
            "elapsed": self.elapsed,
            "error": self.error,
            "pid": self.pid,
            # Carried so a caller in another process can return what the
            # harvest produced, instead of only being able to say it finished.
            # Bounded: this is a status record, not the corpus.
            "result": self.result[:100_000],
        }


_LOCK = threading.Lock()
_JOBS: dict[str, Job] = {}
_COUNTER = 0


# ─────────────────────────────────────────────────────────────
# Status records: how one process sees another's harvest
# ─────────────────────────────────────────────────────────────

def _announce(job: Job) -> None:
    """Say it once, on the console and in the log.

    Only on a transition -- a start, a change of phase, an ending -- never on
    a heartbeat, so watching a 211-page harvest costs four lines rather than
    a scrolling wall. stderr rather than stdout because MCP owns stdout.
    """
    said = f"{job.progress.phase}|{job.state}"
    if getattr(job, "_said", None) == said:
        return
    job._said = said                                    # type: ignore[attr-defined]
    try:
        from docsforge.tools import applog
        applog.harvest(job.id, job.label, job.state, phase=job.progress.phase,
                       pages=job.progress.pages, expected=job.progress.expected,
                       elapsed=job.elapsed, error=job.error)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        if job.state == RUNNING:
            note = f"harvest {job.id}: {job.progress.line()}"
        elif job.state == FAILED:
            note = f"harvest {job.id}: FAILED after {job.elapsed:.0f}s - {job.error}"
        else:
            note = f"harvest {job.id}: {job.state} after {job.elapsed:.0f}s"
        print(f"[docsforge] {note}", file=sys.stderr, flush=True)
    except Exception:                                   # noqa: BLE001
        pass


def _record_path(job_id: str) -> Path:
    return state_dir() / f"{job_id}.json"


#: How often a *running* harvest's heartbeat reaches the shared ledger.
#:
#: The local file is cheap and stays on `HEARTBEAT`. The ledger is a database,
#: possibly a managed one across the network, and it opens a connection per
#: write: at a two-second beat a 695-page harvest spends the whole crawl
#: opening an SSL connection every two seconds — connection churn on the
#: critical path of the fetch loop, against a plan whose backend count is the
#: binding constraint. Nothing needs two-second granularity from another
#: instance; `STALE_AFTER` is 30s, so this has only to stay comfortably
#: inside that for a live harvest to keep reading as live.
LEDGER_BEAT = 8.0


def _publish(job: Job, force: bool = False) -> None:
    """Write this job's record. Never raises: a harvest must not fail because
    a status file could not be written.

    `force` sends it to the shared ledger regardless of the beat. Every state
    transition does — a start, a phase change, an ending — because those are
    what another instance is actually waiting to see, and delaying one by up
    to `LEDGER_BEAT` would make a harvest that finished look like a harvest
    still working. Only the page counter between beats is throttled.
    """
    job.updated = time.time()
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _record_path(job.id)
        # One file per job, written only by the process that owns it, so two
        # harvests never contend. `os.replace` is atomic on both platforms, so
        # a reader never sees half a record.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job.as_dict()), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:                              # noqa: BLE001
        pass

    if not force and job.state == RUNNING:
        since = job.updated - getattr(job, "_ledgered", 0.0)
        if since < LEDGER_BEAT:
            return
    ledger = _shared()
    if ledger is not None:
        try:
            ledger.publish_harvest(job.as_dict())
            job._ledgered = job.updated            # type: ignore[attr-defined]
        except Exception:                          # noqa: BLE001
            pass


def _forget_record(job_id: str) -> None:
    try:
        _record_path(job_id).unlink(missing_ok=True)
    except Exception:                              # noqa: BLE001
        pass
    ledger = _shared()
    if ledger is not None:
        try:
            ledger.forget_harvest(job_id)
        except Exception:                          # noqa: BLE001
            pass


def _job_from_record(data: dict) -> Job | None:
    try:
        progress = Progress(
            phase=data.get("phase", "starting"),
            url=data.get("url", "") or "",
            pages=int(data.get("pages") or 0),
            expected=data.get("expected"),
        )
        job = Job(
            id=data["id"], label=data.get("label", data["id"]),
            started=float(data.get("started") or 0.0),
            progress=progress,
            state=data.get("state", RUNNING),
            error=data.get("error", "") or "",
            result=data.get("result", "") or "",
            finished=float(data.get("finished") or 0.0),
            pid=int(data.get("pid") or 0),
            updated=float(data.get("updated") or 0.0),
            mine=False,
        )
    except (KeyError, TypeError, ValueError):
        return None

    # A record still claiming to run, whose process stopped saying so. We
    # cannot know it died — but we can no longer say it is working, and
    # saying so anyway is the lie this whole mechanism exists to avoid.
    if job.state == RUNNING and time.time() - job.updated > STALE_AFTER:
        job.state = STALLED
        job.finished = job.updated
    return job


def _expired(job: Job, now: float) -> bool:
    """A finished record nobody will ask about any more."""
    settled = job.finished or job.updated
    return bool(job.state != RUNNING and settled and now - settled > KEEP_RECORDS_FOR)


def _disk_records() -> list[Job]:
    """This machine's records, and a sweep of the expired ones."""
    directory = state_dir()
    out: list[Job] = []
    try:
        entries = list(directory.glob("*.json"))
    except Exception:                              # noqa: BLE001
        return out

    now = time.time()
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                          # noqa: BLE001
            continue                               # mid-write or corrupt
        job = _job_from_record(data)
        if job is None:
            continue
        if _expired(job, now):
            try:
                path.unlink(missing_ok=True)
            except Exception:                      # noqa: BLE001
                pass
            continue
        out.append(job)
    return out


def _rows_of(ledger: Ledger) -> list[dict]:
    """Every record in the ledger, expired ones included — or nothing, when
    it cannot be read."""
    try:
        return [row for row in ledger.harvests() if isinstance(row, dict)]
    except Exception:                              # noqa: BLE001
        return []


def _shared_records() -> list[Job]:
    """The ledger's records, and a sweep of the expired ones."""
    ledger = _shared()
    if ledger is None:
        return []
    out: list[Job] = []
    now = time.time()
    for data in _rows_of(ledger):
        job = _job_from_record(data)
        if job is None:
            continue
        if _expired(job, now):
            try:
                ledger.forget_harvest(job.id)
            except Exception:                      # noqa: BLE001
                pass
            continue
        out.append(job)
    return out


def _records() -> list[Job]:
    """Every other process's jobs — this machine's records and the shared
    ledger's — and a sweep of the expired ones.

    A harvest run here is in both, and the two copies can differ by one
    heartbeat; a harvest run on another instance is only in the ledger. The
    fresher copy is believed either way, which is what `updated` is for.
    """
    seen: dict[str, Job] = {}
    for job in _disk_records() + _shared_records():
        held = seen.get(job.id)
        if held is None or job.updated > held.updated:
            seen[job.id] = job
    return list(seen.values())


def _merged(records: list[Job] | None = None) -> list[Job]:
    """This process's jobs plus everyone else's, ours winning on collision.

    `records` lets a caller that needs both the running and the finished jobs
    read the ledger once and split the answer, rather than twice. Measured
    against Postgres: `list_knowledge_base()` opened four connections, two of
    them the same `select record from harvest` run back to back because
    `running()` and `recent()` each fetched their own copy. On a managed plan
    allowing on the order of twenty backends, halving that is not a
    micro-optimisation.
    """
    with _LOCK:
        mine = dict(_JOBS)
    seen = dict(mine)
    for job in _records() if records is None else records:
        seen.setdefault(job.id, job)
    return list(seen.values())


def snapshot() -> tuple[list[Job], list[Job]]:
    """Every harvest, split into running and ended, from one read of the
    ledger. What a caller wanting both should use."""
    jobs = _merged(_records())
    return (sorted((j for j in jobs if j.state == RUNNING), key=lambda j: j.started),
            sorted((j for j in jobs if j.state != RUNNING),
                   key=lambda j: j.finished, reverse=True))


# ─────────────────────────────────────────────────────────────

def _new_id(label: str) -> str:
    """A short id a model can quote back, unique across processes.

    It used to be a per-process counter, which restarted at 1 in every new
    subprocess — so two different langchain harvests were both `langchain-1`
    and their records would have overwritten each other. The counter now
    starts above whatever is already on disk for this label.
    """
    global _COUNTER
    _COUNTER += 1
    safe = "".join(c for c in label.lower() if c.isalnum() or c == "-")[:24] or "harvest"

    taken = set()
    try:
        for path in state_dir().glob(f"{safe}-*.json"):
            suffix = path.stem[len(safe) + 1:]
            if suffix.isdigit():
                taken.add(int(suffix))
    except Exception:                              # noqa: BLE001
        pass
    # And whatever another instance has published to the ledger, expired rows
    # included: two instances that both mint `markdownify-1` overwrite each
    # other's record, which is the collision the counter exists to prevent.
    ledger = _shared()
    known = list(_JOBS) + [str(row.get("id") or "")
                           for row in (_rows_of(ledger) if ledger is not None else [])]
    taken |= {int(k.rsplit("-", 1)[-1]) for k in known
              if k.startswith(f"{safe}-") and k.rsplit("-", 1)[-1].isdigit()}

    number = _COUNTER
    while number in taken:
        number += 1
    _COUNTER = number
    return f"{safe}-{number}"


def _prune() -> None:
    """Drop the oldest finished jobs. Caller holds the lock."""
    finished = sorted(
        (j for j in _JOBS.values() if j.state != RUNNING),
        key=lambda j: j.finished,
    )
    for job in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
        _JOBS.pop(job.id, None)


def origin_of(e: BaseException, skip: str = "") -> str:
    """` (at file.py:123 in func)`: the deepest frame inside this package —
    not counting `skip`, a dispatcher's own file — or, failing that, wherever
    the exception was raised. A worker's exception keeps the worker's frames,
    so a failure inside a harvest thread still names its real line.
    """
    import traceback as _tb
    frames = _tb.extract_tb(e.__traceback__)
    if not frames:
        return ""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ours = [f for f in frames
            if os.path.abspath(f.filename).startswith(package)
            and os.path.abspath(f.filename) != skip]
    f = (ours or frames)[-1]
    return f" (at {os.path.basename(f.filename)}:{f.lineno} in {f.name})"


def start(label: str, work: Callable[[Progress], str],
          job_id: str = "") -> Job:
    """Run `work` on its own thread. Returns immediately.

    `work` is handed the job's Progress to update as it goes, and whatever
    string it returns becomes the job's result — the same string the caller
    would have received had it waited.

    `job_id` adopts an id somebody else already published, which is how a
    detached worker continues the record its launcher created rather than
    minting a second one for the same harvest. `adopt()` is the same thing for
    a caller that cannot reach this argument — `tool_learn_technology` builds
    its own `work` and calls this itself, so the launcher leaves the id there
    instead of threading a parameter through the tool layer.
    """
    adopted = job_id or adopting()
    adopt("")
    with _LOCK:
        job = Job(id=adopted or _new_id(label), label=label,
                  started=time.time(), pid=os.getpid())
        _JOBS[job.id] = job
        _prune()

    def moved() -> None:
        _publish(job, force=True)          # a transition, not a tick
        _announce(job)

    object.__setattr__(job.progress, "_moved", moved)
    _publish(job, force=True)              # the record must exist immediately
    _announce(job)

    def run() -> None:
        try:
            job.result = work(job.progress)
            job.state = DONE
        except BaseException as e:                      # noqa: BLE001
            # Including SystemExit and KeyboardInterrupt: a worker thread that
            # died silently would leave a job "running" forever, and a harvest
            # that reports nothing is the failure mode this module exists to
            # prevent.
            job.exc = e
            # A ForgeError explains itself. Anything else is a crash, and a
            # record that says only "[Errno 16] Device or resource busy" —
            # what one Vercel harvest left behind — cannot be acted on
            # without the type and the line it came from.
            if isinstance(e, ForgeError):
                job.error = str(e)
            else:
                job.error = (f"{type(e).__name__}: {e}".rstrip(": ")
                             + origin_of(e, skip=os.path.abspath(__file__)))
            job.state = FAILED
        finally:
            job.finished = time.time()
            _publish(job)
            _announce(job)
            job.done.set()

    def beat() -> None:
        # Its own clock, not progress-driven: a harvest can spend a minute
        # resolving without fetching a page, and a silent minute must not
        # read as a dead process.
        while not job.done.wait(HEARTBEAT):
            _publish(job)
            _announce(job)      # no-op unless the phase actually moved

    # Daemon: a harvest must never keep the process alive at shutdown. The
    # cost is that shutdown loses in-flight harvests, which is why the caller
    # is told the harvest runs "while this server is running" rather than
    # promised a result — and why a record whose heartbeat stopped is reported
    # as stalled rather than quietly left claiming to run.
    threading.Thread(target=run, name=f"harvest:{job.id}", daemon=True).start()
    threading.Thread(target=beat, name=f"heartbeat:{job.id}", daemon=True).start()
    return job


#: This host will be torn down with the turn, so a harvest started here has to
#: be run somewhere else. Off by default — `app.py` outlives any turn, and a
#: thread there is simpler and feeds the live trace. The stdio MCP server turns
#: it on, because the `claude` CLI launches it per turn and tears it down.
#:
#: Three attempts, each measured, because each of the first two looked right:
#:
#:   1. **Wait for the harvest before exiting.** Works when the client closes
#:      stdin; useless when it kills.
#:
#:          close-stdin   lived a further 232s   stored: 1.0.0.md
#:          kill          lived a further   2s   stored: NOTHING
#:
#:   2. **Spawn it `DETACHED_PROCESS`.** Survives the parent being killed —
#:      and *not* the parent's **job object**, which is how a client actually
#:      tears its children down. Windows kills every process in a job with
#:      `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, detached or not:
#:
#:          detached only          tick 4 -> 4      DIED
#:          detached + breakaway   never started    DIED
#:
#:      `CREATE_BREAKAWAY_FROM_JOB` is not the escape either: it is refused
#:      unless the job grants it, and the child then fails to start at all.
#:
#:   3. **Hand it to a process that was never in that job.** There is one:
#:      the DocsForge server. It is long-lived, outside the CLI entirely, and
#:      already the host where harvests have always worked.
#:
#: No spawn flag can win this. Whoever owns the job decides, so the work has to
#: begin somewhere the job never reached.
DETACHED = False

#: Set by a host that cannot keep a thread alive past the request that started
#: it — a serverless function. There is nobody to hand the harvest to and no
#: background to continue it in, so a harvest that outlives the deadline is
#: simply lost, and the result must say that instead of promising it will
#: finish. Distinct from DETACHED: that is "someone else will run it", this is
#: "nobody can".
EPHEMERAL = False

#: Where the long-lived server listens. A harvest is handed to it rather than
#: started here; `app.py`'s own default port, overridable for a server on
#: another port or host.
SERVER = os.environ.get("DOCSFORGE_SERVER", "http://127.0.0.1:8000")

#: Long enough to cross a loopback request to a busy server, short enough that
#: a caller waiting on a tool call does not notice when nothing is listening.
HANDOFF_TIMEOUT = 5.0


def reserve(label: str) -> Job:
    """Publish a record for work that is about to begin, and return it.

    Before the thread, deliberately: a caller handed an id must be able to
    find it immediately, and a gap where the harvest it just asked for cannot
    be found is exactly what this whole mechanism exists to close.
    """
    with _LOCK:
        job = Job(id=_new_id(label), label=label, started=time.time(),
                  pid=os.getpid())
    _publish(job, force=True)
    _announce(job)
    return job


def hand_off(label: str, kwargs: dict, tool: str = "learn_technology") -> Job | None:
    """Ask the long-lived server to run this harvest, or None if none answers.

    The server publishes its own status record, so the id comes back and every
    other process — this one included — can watch it exactly as before.

    `tool` names which one to run there. It used to be implicit, because
    `learn_technology` was the only tool that handed anything off; sending
    `harvest_docs`'s own arguments to `learn_technology` would have resolved
    a name the caller never gave and harvested whatever that found. A server
    too old to know the field runs `learn_technology`, which is what it did
    before — so a new client against an old server is no worse off than it
    was, and the `url` it sends is simply ignored.
    """
    import urllib.error
    import urllib.request

    payload = json.dumps({"label": label, "kwargs": kwargs, "tool": tool}).encode()
    request = urllib.request.Request(
        SERVER.rstrip("/") + "/api/harvests", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HANDOFF_TIMEOUT) as answer:
            body = json.load(answer)
    except Exception:                                   # noqa: BLE001
        return None                                     # nobody is listening

    job_id = str(body.get("job") or "")
    if not job_id:
        return None
    found = next((j for j in _records() if j.id == job_id), None)
    if found is not None:
        return found
    job = Job(id=job_id, label=label, started=time.time(), mine=False)
    job.updated = time.time()
    return job

#: An id the next `start()` on *this thread* should adopt instead of minting
#: one. Thread-local, not module state: the server runs one harvest per thread
#: and two starting at once would otherwise swap ids with each other. Set by
#: whoever reserved the record, consumed once.
_adopt = threading.local()


def adopt(job_id: str) -> None:
    """The next `start()` on this thread continues `job_id`."""
    _adopt.value = job_id


def adopting() -> str:
    return getattr(_adopt, "value", "") or ""


def await_record(job_id: str, seconds: float) -> Job | None:
    """Watch another process's record until it settles, or the time is up.

    This is what keeps a small harvest feeling the way it always did. A
    detached harvest that finishes inside the deadline still returns its own
    summary inline, because the worker writes the result into the record and
    the launcher reads it back — rather than every harvest, however tiny,
    coming back as "still running".
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = next((j for j in _records() if j.id == job_id), None)
        if found is not None and found.state != RUNNING:
            return found
        time.sleep(0.25)
    return None


def wait(job: Job, seconds: float | None = None) -> bool:
    """Block up to `seconds`. True if the job finished within them.

    An infinite limit means "no limit", and has to be passed as *no argument*
    rather than as `inf`: `Event.wait(float("inf"))` raises `OverflowError:
    timestamp out of range for platform time_t` on Windows. The detached
    worker, which exists for exactly one harvest and has nobody to hand back
    to early, sets the deadline that way — and every harvest it ran died
    instantly on that overflow before this handled it.
    """
    limit = DEADLINE if seconds is None else seconds
    if limit is None or not math.isfinite(limit):
        return job.done.wait()
    return job.done.wait(limit)


def get(job_id: str) -> Job | None:
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is not None:
        return job
    return next((j for j in _records() if j.id == job_id), None)


def running() -> list[Job]:
    """Harvests believed to be working, in this process or any other.

    A caller that also wants the finished ones should call `snapshot()`
    instead and pay for one read of the ledger rather than two.
    """
    return snapshot()[0]


def recent() -> list[Job]:
    """Finished, failed and stalled jobs, newest first."""
    return snapshot()[1]


#: How long a short-lived host may linger for its own harvests. **Zero, the
#: default, means no bound: wait for the harvest however long it takes.**
#:
#: It was 900s, and that was wrong. A documentation set is of unknown size
#: until it has been read — the whole point of measuring coverage is that
#: nobody knows the page count in advance — so any wall-clock bound is a
#: guess about someone else's site, and losing a harvest to it costs
#: everything: measured at a deliberately short bound, ~180 of 213 pages were
#: fetched and **zero bytes stored**. A bound that discards all the work it
#: was protecting is not a safety valve.
#:
#: The orphan it was guarding against does not need a timer either. This waits
#: only on harvests *this process started*, every fetch already carries its own
#: timeout, and the loop is bounded by the page cap — so the wait ends when the
#: work ends. Set a positive value to reimpose a ceiling.
LINGER = float(os.environ.get("DOCSFORGE_HARVEST_LINGER", "0"))


def wait_for_all(timeout: float | None = None) -> int:
    """Block until this process's harvests finish. Returns how many did not.

    For a host that exits as soon as its client disconnects. `app.py` never
    needs this — it outlives any single turn, and Ctrl-C there should not hang
    for twelve minutes — but the stdio MCP server is launched per turn by the
    `claude` CLI and torn down with it. Its harvest threads are daemons, so
    the process exited and took a 561-page langchain harvest with it at page
    59. Backgrounding cannot work on that path unless somebody waits.

    Still bounded, and still daemon threads underneath: whatever has not
    finished when the bound expires is abandoned, and its record goes stale
    and reports itself stopped rather than pretending to still be working.
    """
    bound = LINGER if timeout is None else timeout
    with _LOCK:
        mine = [j for j in _JOBS.values() if j.state == RUNNING]

    if bound <= 0:                      # no bound: the wait ends when the work does
        for job in mine:
            job.done.wait()
        return 0

    deadline = time.time() + bound
    for job in mine:
        left = deadline - time.time()
        if left <= 0:
            break
        job.done.wait(left)
    return sum(1 for j in mine if j.state == RUNNING)


def clear() -> None:
    """Forget every job. For tests; does not stop running threads."""
    global _COUNTER
    with _LOCK:
        ids = list(_JOBS)
        _JOBS.clear()
        _COUNTER = 0
    for job_id in ids:
        _forget_record(job_id)
    try:
        for path in state_dir().glob("*.json"):
            path.unlink(missing_ok=True)
    except Exception:                              # noqa: BLE001
        pass
    ledger = _shared()
    if ledger is not None:
        for row in _rows_of(ledger):
            try:
                ledger.forget_harvest(str(row.get("id") or ""))
            except Exception:                      # noqa: BLE001
                pass
