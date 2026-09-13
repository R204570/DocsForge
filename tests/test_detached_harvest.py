"""A harvest must outlive the turn that asked for it, and three tries were wrong.

The stdio MCP server is launched per turn by the `claude` CLI and torn down
with it. Measured, in order:

    1. wait before exiting     close-stdin  lived 232s   stored: 1.0.0.md
                               kill         lived   2s   stored: NOTHING
    2. spawn DETACHED_PROCESS  detached only          tick 4 -> 4   DIED
                               detached + breakaway   never started  DIED
    3. hand it to the server                                        works

A client tears its children down by closing their **job object**, and Windows
kills every process in a job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` however
detached. `CREATE_BREAKAWAY_FROM_JOB` is refused unless the job grants it, and
the child then fails to start at all. No spawn flag wins that argument, so the
work has to *begin* somewhere the job never reached — the long-lived DocsForge
server, which is also where background harvests have always worked.
"""
import json
import os
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.server import app
from docsforge.tools import harvest_jobs
from docsforge.tools.harvest_jobs import DONE, FAILED, RUNNING


@pytest.fixture(autouse=True)
def _clean():
    harvest_jobs.clear()
    harvest_jobs.adopt("")
    harvest_jobs.DETACHED = False
    yield
    harvest_jobs.clear()
    harvest_jobs.adopt("")
    harvest_jobs.DETACHED = False


# ── waiting forever must not overflow ───────────────────────────

def test_an_infinite_deadline_waits_rather_than_raising():
    """`Event.wait(float("inf"))` raises OverflowError on Windows: "timestamp
    out of range for platform time_t"."""
    job = harvest_jobs.start("quick", lambda p: "stored")
    assert harvest_jobs.wait(job, float("inf")) is True
    assert job.state == DONE


def test_a_finite_deadline_still_bounds_the_wait():
    gate = threading.Event()
    job = harvest_jobs.start("slow", lambda p: gate.wait(30) or "never")
    try:
        assert harvest_jobs.wait(job, 0.3) is False
        assert job.state == RUNNING
    finally:
        gate.set()


# ── the id is reserved before the work starts ───────────────────

def test_reserve_publishes_a_record_before_anything_runs():
    """A caller handed an id must be able to find it at once. A gap where the
    harvest just asked for cannot be found is what this exists to close."""
    job = harvest_jobs.reserve("mojo")
    assert (harvest_jobs.state_dir() / f"{job.id}.json").exists()
    assert [j.id for j in harvest_jobs.running()] == [job.id]


def test_start_adopts_the_reserved_id():
    harvest_jobs.adopt("mojo-7")
    job = harvest_jobs.start("mojo", lambda p: "stored")
    harvest_jobs.wait(job, 5)
    assert job.id == "mojo-7"
    assert harvest_jobs.adopting() == "", "consumed once, not left for the next"


def test_the_adopted_id_does_not_leak_between_threads():
    """Two harvests starting at once would otherwise swap ids. The server runs
    one per thread, so this is not hypothetical."""
    harvest_jobs.adopt("mine-1")
    seen = []

    def other() -> None:
        seen.append(harvest_jobs.adopting())

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()

    assert seen == [""], "another thread must not see this thread's id"
    assert harvest_jobs.adopting() == "mine-1"


# ── handing the work to the long-lived server ───────────────────

def test_hand_off_returns_none_when_nothing_is_listening(monkeypatch):
    """Then the caller runs it here and says so, rather than promising
    background work this host cannot keep."""
    monkeypatch.setattr(harvest_jobs, "SERVER", "http://127.0.0.1:9")
    monkeypatch.setattr(harvest_jobs, "HANDOFF_TIMEOUT", 0.5)
    assert harvest_jobs.hand_off("mojo", {"name": "mojo"}) is None


def test_the_tool_warns_when_a_harvest_cannot_outlive_the_turn():
    from docsforge.tools import forge_tools

    forge_tools._log_no_server()
    job = harvest_jobs.Job(id="mojo-1", label="mojo", started=time.time())
    message = forge_tools._still_harvesting(job)
    assert "will stop when this turn ends" in message
    assert "python -m docsforge.server.app" in message


def test_the_warning_is_not_repeated_once_it_has_been_said():
    from docsforge.tools import forge_tools

    forge_tools._log_no_server()
    job = harvest_jobs.Job(id="mojo-1", label="mojo", started=time.time())
    assert "will stop when this turn ends" in forge_tools._still_harvesting(job)
    assert "will stop when this turn ends" not in forge_tools._still_harvesting(job)


# ── the endpoint the handoff calls ──────────────────────────────

def test_the_server_starts_a_harvest_and_returns_its_id(monkeypatch):
    started = threading.Event()

    def fake(**kwargs):
        harvest_jobs.start("mojo", lambda p: "stored")
        started.set()
        return "Harvested **mojo**"

    from docsforge.tools import forge_tools
    monkeypatch.setattr(forge_tools, "tool_learn_technology", fake)

    with TestClient(app.app) as client:
        body = client.post("/api/harvests",
                           json={"label": "mojo", "kwargs": {"name": "mojo"}}).json()

    assert body.get("job", "").startswith("mojo-")
    assert started.wait(5), "the server must actually run it"
    # The record exists from the moment the id is handed back.
    assert (harvest_jobs.state_dir() / f"{body['job']}.json").exists()


def test_the_server_refuses_a_request_with_no_label():
    with TestClient(app.app) as client:
        answer = client.post("/api/harvests", json={"kwargs": {}})
    assert answer.status_code == 400


def test_the_server_will_not_crawl_the_same_site_twice():
    """The guard that already protects the local path protects this one too."""
    directory = harvest_jobs.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mojo-1.json").write_text(json.dumps({
        "id": "mojo-1", "label": "mojo", "state": RUNNING, "phase": "harvesting",
        "url": "", "pages": 3, "expected": 10,
        "started": time.time(), "updated": time.time(),
        "finished": 0.0, "error": "", "pid": 999, "result": "",
    }), encoding="utf-8")

    with TestClient(app.app) as client:
        body = client.post("/api/harvests",
                           json={"label": "Mojo", "kwargs": {"name": "Mojo"}}).json()

    assert body == {"job": "mojo-1", "already": True}


# ── watching another process's record settle ────────────────────

def _write(job_id: str, **over) -> None:
    directory = harvest_jobs.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": job_id, "label": job_id, "state": RUNNING, "phase": "harvesting",
        "url": "", "pages": 1, "expected": 10,
        "started": time.time(), "updated": time.time(),
        "finished": 0.0, "error": "", "pid": 4242, "result": "",
    }
    data.update(over)
    (directory / f"{job_id}.json").write_text(json.dumps(data), encoding="utf-8")


def test_await_record_returns_the_result_the_server_wrote():
    """What keeps a small harvest feeling as it always did: one that finishes
    inside the deadline still returns its own summary inline."""
    _write("mojo-1", state=DONE, finished=time.time(),
           result="Harvested **mojo** 1.0.0 - 213 pages")
    settled = harvest_jobs.await_record("mojo-1", 3)
    assert settled is not None and settled.state == DONE
    assert "213 pages" in settled.result


def test_await_record_gives_up_while_the_harvest_is_still_running():
    _write("mojo-1")
    began = time.time()
    assert harvest_jobs.await_record("mojo-1", 0.5) is None
    assert time.time() - began < 3


def test_await_record_reports_a_failure_rather_than_waiting_it_out():
    _write("mojo-1", state=FAILED, finished=time.time(), error="the site refused us")
    settled = harvest_jobs.await_record("mojo-1", 3)
    assert settled is not None and settled.state == FAILED
    assert "refused us" in settled.error


# ── handing off is off unless a short-lived host asks for it ────

def test_handing_off_is_off_by_default():
    assert harvest_jobs.DETACHED is False


def test_the_stdio_server_turns_it_on():
    import inspect

    from docsforge.server import mcp_server

    assert "DETACHED = True" in inspect.getsource(mcp_server.main)
