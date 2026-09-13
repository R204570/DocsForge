"""A background harvest has to be visible from outside the process running it.

The defect these were written from: `learn_technology` started a harvest in the
MCP subprocess the `claudecode` provider launches, told the user that
`list_knowledge_base()` would report it, and that call ran in a different
process which had never heard of the job. Nothing anywhere showed progress —
not the panel, not the log, not the tool — and then the documentation appeared
in DocsStore some minutes later as though from nowhere.
"""
import json
import time

import pytest

from docsforge.tools import harvest_jobs
from docsforge.tools.harvest_jobs import DONE, FAILED, RUNNING, STALLED, Job, Progress


@pytest.fixture(autouse=True)
def _clean():
    harvest_jobs.clear()
    yield
    harvest_jobs.clear()


def _write_record(**fields):
    """A record as some *other* process would have left it."""
    data = {
        "id": "other-1", "label": "other", "state": RUNNING,
        "phase": "harvesting", "url": "", "pages": 7, "expected": 20,
        "started": time.time() - 10, "updated": time.time(),
        "finished": 0.0, "error": "", "pid": 4242,
    }
    data.update(fields)
    directory = harvest_jobs.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{data['id']}.json").write_text(json.dumps(data),
                                                  encoding="utf-8")
    return data


# ── a job is visible from a process that did not start it ──────

def test_another_processes_running_harvest_is_reported():
    _write_record()
    assert not harvest_jobs._JOBS, "this process started nothing"

    live = harvest_jobs.running()
    assert [j.id for j in live] == ["other-1"]
    assert live[0].mine is False
    assert "7/20" in live[0].line()


def test_another_processes_finished_harvest_is_reported_as_finished():
    _write_record(state=DONE, finished=time.time())
    assert [j.id for j in harvest_jobs.recent()] == ["other-1"]
    assert not harvest_jobs.running()


def test_our_own_job_wins_over_a_record_with_the_same_id():
    """Ours is live; the record is at best one heartbeat behind it."""
    job = Job(id="mine-1", label="mine", started=time.time())
    job.progress.phase = "harvesting"
    job.progress.pages = 99
    harvest_jobs._JOBS[job.id] = job
    _write_record(id="mine-1", pages=1)

    live = harvest_jobs.running()
    assert len(live) == 1
    assert live[0].progress.pages == 99
    assert live[0].mine is True


# ── a record that outlives its process must say so ─────────────

def test_a_record_whose_heartbeat_stopped_is_stalled_not_running():
    """The honesty guarantee. A daemon thread dies with its process, so a
    record still claiming to run is a record whose process is gone."""
    _write_record(updated=time.time() - (harvest_jobs.STALE_AFTER + 5))

    assert not harvest_jobs.running(), "must not still be called running"
    stalled = harvest_jobs.recent()
    assert [j.state for j in stalled] == [STALLED]
    assert "STOPPED REPORTING" in stalled[0].line()


def test_a_recent_heartbeat_is_still_believed():
    _write_record(updated=time.time() - 1)
    assert [j.state for j in harvest_jobs.running()] == [RUNNING]


def test_an_expired_finished_record_is_swept():
    old = time.time() - (harvest_jobs.KEEP_RECORDS_FOR + 60)
    _write_record(state=DONE, finished=old, updated=old)
    assert harvest_jobs.recent() == []
    assert list(harvest_jobs.state_dir().glob("*.json")) == []


# ── ids have to survive a process restart ──────────────────────

def test_ids_do_not_collide_with_another_processes_job():
    """`langchain-1` came back twice because the counter restarts per process,
    and the second record would have overwritten the first."""
    _write_record(id="langchain-1", label="langchain")
    harvest_jobs._COUNTER = 0            # a fresh process starts here

    job = harvest_jobs.start("langchain", lambda p: "done")
    harvest_jobs.wait(job, 5)
    assert job.id != "langchain-1"
    assert {j.id for j in harvest_jobs._merged()} >= {"langchain-1", job.id}


# ── no invented progress ───────────────────────────────────────

def test_a_crawl_reports_no_fraction_because_it_has_no_denominator():
    """A frontier grows as it is walked; dividing by it would be a fiction."""
    p = Progress(phase="harvesting", pages=40, expected=None)
    assert p.fraction() is None
    assert "40 pages so far" in p.line()


def test_a_manifest_reports_a_real_fraction():
    p = Progress(phase="harvesting", pages=40, expected=200)
    assert p.fraction() == pytest.approx(0.2)


def test_resolving_reports_no_fraction():
    assert Progress(phase="resolving").fraction() is None


# ── the live path ──────────────────────────────────────────────

def test_a_real_job_publishes_a_record_and_removes_nothing_on_success():
    started = {}

    def work(progress):
        progress.phase = "harvesting"
        progress.expected = 4
        progress.pages = 4
        started["record"] = (harvest_jobs.state_dir() / f"{job.id}.json").exists()
        return "stored 4 pages"

    job = harvest_jobs.start("thing", work)
    assert harvest_jobs.wait(job, 10)
    assert started["record"] is True, "the record exists while the work runs"
    assert job.state == DONE

    data = json.loads((harvest_jobs.state_dir() / f"{job.id}.json")
                      .read_text(encoding="utf-8"))
    assert data["state"] == DONE
    assert data["pages"] == 4 and data["expected"] == 4


def test_a_phase_change_is_published_without_waiting_for_the_heartbeat():
    """A phase shorter than the beat used to be invisible entirely."""
    job = Job(id="beat-1", label="beat", started=time.time())
    harvest_jobs._JOBS[job.id] = job
    seen = []
    object.__setattr__(job.progress, "_moved", lambda: seen.append(job.progress.phase))

    job.progress.phase = "resolving"
    job.progress.pages = 3           # not a phase change
    job.progress.phase = "harvesting"

    assert seen == ["resolving", "harvesting"], seen


def test_a_failed_job_records_why():
    def work(progress):
        raise RuntimeError("the site refused us")

    job = harvest_jobs.start("bad", work)
    assert harvest_jobs.wait(job, 10)
    assert job.state == FAILED
    data = json.loads((harvest_jobs.state_dir() / f"{job.id}.json")
                      .read_text(encoding="utf-8"))
    assert data["state"] == FAILED
    assert "refused us" in data["error"]
    assert "FAILED" in job.line()


# ── the endpoint the browser polls ─────────────────────────────

def test_api_harvests_reports_running_and_recent():
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from starlette.testclient import TestClient

    from docsforge.server import app

    _write_record(id="live-1", label="langchain", pages=7, expected=20)
    _write_record(id="over-1", label="astro", state=DONE,
                  finished=time.time(), pages=385, expected=385)

    with TestClient(app.app) as client:
        body = client.get("/api/harvests").json()

    assert body["count"] == 1
    live = body["running"][0]
    assert live["label"] == "langchain"
    assert live["fraction"] == pytest.approx(7 / 20)
    assert live["line"] == "harvesting 7/20 pages"
    assert [j["id"] for j in body["recent"]] == ["over-1"]


def test_api_harvests_sends_a_null_fraction_when_there_is_no_denominator():
    """The browser draws no bar for null. It must never receive a made-up one."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from starlette.testclient import TestClient

    from docsforge.server import app

    _write_record(id="crawl-1", label="effect", pages=120, expected=None)
    with TestClient(app.app) as client:
        body = client.get("/api/harvests").json()

    assert body["running"][0]["fraction"] is None
    assert "120 pages so far" in body["running"][0]["line"]


def test_a_second_learn_technology_does_not_crawl_the_same_site_twice():
    """The transcript that prompted this: langchain harvested twice.

    `_still_harvesting` asked the model not to call again, which held for
    exactly as long as one process — and the provider runs each turn's tools
    in a fresh one.
    """
    from docsforge.tools import forge_tools

    _write_record(id="langchain-1", label="langchain", pages=3, expected=99)
    out = forge_tools.tool_learn_technology(name="langchain")

    assert "still running" in out
    assert "`langchain-1`" in out
    assert "3/99" in out
    # Nothing new was started: the only job is the one already on record.
    assert [j.id for j in harvest_jobs.running()] == ["langchain-1"]


def test_a_different_technology_is_not_blocked_by_a_running_one():
    """The guard matches on the name, so an unrelated harvest passes it.

    Asserted against the guard rather than by calling the tool. Calling it
    started a *live* resolution: the test returned in milliseconds, monkeypatch
    put `DOCSFORGE_HARVEST_STATE` back, and the harvest thread — still running
    — published its next heartbeat into the developer's real state directory,
    where it sat as a stalled harvest for a name nobody had asked for.
    """
    from docsforge.tools import forge_tools

    _write_record(id="langchain-1", label="langchain")
    running = harvest_jobs.running()
    assert [j.label for j in running] == ["langchain"]

    def blocked_by(name: str) -> bool:
        wanted = forge_tools._kb_slug(forge_tools._normalise(name) or name)
        return any(
            forge_tools._kb_slug(
                forge_tools._normalise(j.label) or j.label) == wanted
            for j in running)

    assert blocked_by("langchain"), "the same name must be recognised"
    assert blocked_by("LangChain"), "and recognised however it is spelled"
    assert not blocked_by("mojo")
    assert not blocked_by("langsmith")


# ── a short-lived host has to wait for its own harvests ────────
#
# The langchain failure: `learn_technology` handed back at 25s promising the
# harvest would keep going, the `claude` CLI ended the turn, the MCP subprocess
# it had launched exited, and the daemon thread died with it at 59/561 pages.
# Nothing was stored and nothing said why.

def test_wait_for_all_returns_once_our_harvests_finish():
    import threading

    gate = threading.Event()

    def work(progress):
        progress.phase = "harvesting"
        gate.wait(5)
        return "stored"

    job = harvest_jobs.start("slow", work)
    threading.Timer(0.3, gate.set).start()

    began = time.time()
    abandoned = harvest_jobs.wait_for_all(timeout=10)
    assert abandoned == 0
    assert job.state == DONE
    assert time.time() - began < 5, "should return when the work does"


def test_wait_for_all_is_bounded_and_says_what_it_abandoned():
    """An MCP server outliving its client is an orphan; one that never gives
    up is worse than a lost harvest."""
    import threading

    gate = threading.Event()
    job = harvest_jobs.start("endless", lambda p: gate.wait(30) or "never")
    try:
        began = time.time()
        abandoned = harvest_jobs.wait_for_all(timeout=0.4)
        assert abandoned == 1
        assert time.time() - began < 3
        assert job.state == RUNNING
    finally:
        gate.set()


def test_wait_for_all_ignores_another_processes_harvest():
    """Waiting on one we cannot observe finishing would simply hang."""
    _write_record(id="theirs-1", label="theirs")
    began = time.time()
    assert harvest_jobs.wait_for_all(timeout=5) == 0
    assert time.time() - began < 2, "must not have waited on it"


def test_the_stdio_server_waits_before_it_exits():
    """`server.run(transport=\"stdio\")` returns when the client hangs up, and
    returning from main is what killed the harvest."""
    import inspect

    from docsforge.server import mcp_server

    source = inspect.getsource(mcp_server.main)
    assert "wait_for_all" in source
    # ...and only for our own, or it would hang on someone else's.
    assert "j.mine" in source


def test_the_linger_is_unbounded_by_default():
    """A documentation set is of unknown size until it has been read, so any
    wall-clock bound is a guess about someone else's site."""
    assert harvest_jobs.LINGER == 0


def test_an_unbounded_wait_returns_when_the_work_does_not_on_a_timer():
    import threading

    gate = threading.Event()
    job = harvest_jobs.start("big", lambda p: gate.wait(30) or "stored")
    threading.Timer(0.4, gate.set).start()

    began = time.time()
    assert harvest_jobs.wait_for_all(timeout=0) == 0   # 0 == no bound
    assert job.state == DONE
    assert time.time() - began < 10


def test_a_positive_bound_still_caps_the_wait():
    """Removing the default ceiling must not remove the ability to set one."""
    import threading

    gate = threading.Event()
    job = harvest_jobs.start("endless", lambda p: gate.wait(30) or "never")
    try:
        assert harvest_jobs.wait_for_all(timeout=0.4) == 1
        assert job.state == RUNNING
    finally:
        gate.set()
