"""
`harvest_status`: a harvest reports back on itself.

`learn_technology` hands back a harvest id when the crawl outlives the
deadline, and until now the only way to ask after it was the block that
`list_knowledge_base` prepends -- one line, no phase, no percentage, and
nothing at all once it finished. A model left with that either polls the
listing or, worse, decides the harvest is its job: it starts fetching the
pages itself, or starts the harvest again. So the status tool says two things
every time a harvest is running: where it is, and that the pages are landing
in DocsForge's own store, not the model's, so the harvest is to be left alone
until it is whole.

No network and no real crawling, as in test_background: the harvest is a
stub, because what is under test is what is *said* about it.
"""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.tools import harvest_jobs
from docsforge.tools.harvest_jobs import DONE, FAILED, RUNNING, Progress
from docsforge.store.kb_store import FileStore


@pytest.fixture
def kb(tmp_path):
    ft.reset_store(FileStore(tmp_path))
    harvest_jobs.clear()
    yield tmp_path
    for job in harvest_jobs.running():
        if job.mine:                # a record another "process" left has no thread to wait on
            job.done.wait(10)
    harvest_jobs.clear()
    ft.reset_store(None)


def _write_record(**fields):
    """A record as some *other* process would have left it."""
    data = {
        "id": "effect-1", "label": "effect", "state": RUNNING,
        "phase": "harvesting", "url": "https://effect.website/docs/",
        "pages": 118, "expected": 703,
        "started": time.time() - 25, "updated": time.time(),
        "finished": 0.0, "error": "", "pid": 4242, "result": "",
    }
    data.update(fields)
    directory = harvest_jobs.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _held(label="effect"):
    """A harvest in this process that runs until released."""
    gate = threading.Event()

    def work(progress: Progress) -> str:
        progress.phase = "harvesting"
        progress.url = "https://effect.website/docs/"
        progress.expected = 703
        progress.pages = 118
        gate.wait(10)
        return "Harvested **effect** v3 - 703 pages"

    return harvest_jobs.start(label, work), gate


# ── the tool exists, and is served ────────────────────────────
def test_the_tool_is_registered_and_takes_an_id_and_a_wait():
    tool = ft.BY_NAME["harvest_status"]
    assert set(tool.schema["properties"]) == {"harvest", "wait"}
    assert tool.schema.get("required", []) == []


def test_the_description_says_whose_store_the_pages_land_in():
    # The sentence a model needs before it decides the harvest is its job.
    text = ft.BY_NAME["harvest_status"].description
    assert "DocsForge's own knowledge base" in text
    assert "not to your context" in text
    assert "must not cut the harvest short" in text


# ── a running harvest, in full ───────────────────────────────
def test_a_running_harvest_reports_phase_pages_and_share(kb):
    job, gate = _held()
    try:
        out = ft.tool_harvest_status("effect-1")
    finally:
        gate.set()

    assert "**running**" in out
    assert "harvesting 118/703 pages" in out
    assert "17%" in out                       # 118 / 703
    assert f"`{job.id}`" in out


def test_a_running_harvest_is_found_by_name_as_well_as_id(kb):
    _, gate = _held()
    try:
        by_name = ft.tool_harvest_status("Effect")
    finally:
        gate.set()
    assert "**running**" in by_name and "effect-1" in by_name


def test_a_running_harvest_says_the_pages_are_docsforges_not_the_models(kb):
    _, gate = _held()
    try:
        out = ft.tool_harvest_status("effect-1")
    finally:
        gate.set()

    assert "DocsForge's own knowledge base" in out
    assert "not to your context" in out
    assert "nothing for you to fetch, collect or save" in out
    assert "every page the site lists" in out
    assert "Do not call learn_technology or harvest_docs for it again" in out


def test_another_processes_harvest_is_reported_the_same_way(kb):
    _write_record()
    assert not harvest_jobs._JOBS

    out = ft.tool_harvest_status("effect-1")

    assert "**running**" in out and "118/703" in out
    assert "DocsForge's own knowledge base" in out


def test_a_crawl_has_no_percentage_to_report(kb):
    _write_record(expected=None, pages=41)
    out = ft.tool_harvest_status("effect-1")
    assert "41 pages so far" in out
    assert "%" not in out
    assert "page count is not known" in out


def test_a_harvest_still_resolving_says_so(kb):
    _write_record(phase="resolving", pages=0, expected=None)
    out = ft.tool_harvest_status("effect-1")
    assert "resolving" in out
    assert "has not started fetching pages yet" in out


# ── once it ends ─────────────────────────────────────────────
def test_a_finished_harvest_returns_its_result_and_how_to_read_it(kb):
    _write_record(state=DONE, finished=time.time(),
                  result="Harvested **effect** v3 - 703 pages, 2,104,338 characters")

    out = ft.tool_harvest_status("effect-1")

    assert "**done**" in out
    assert 'read_knowledge_base(name="effect")' in out
    assert "703 pages" in out
    assert "DocsForge's own knowledge base" not in out   # nothing left to wait for


def test_a_failed_harvest_reports_why_and_that_nothing_was_stored(kb):
    _write_record(state=FAILED, finished=time.time(),
                  error="Found 2 candidate(s) but none could be confirmed to document it.")

    out = ft.tool_harvest_status("effect-1")

    assert "**failed**" in out
    assert "none could be confirmed" in out
    assert "Nothing was stored" in out


def test_a_harvest_whose_process_died_is_reported_as_stopped(kb):
    _write_record(updated=time.time() - harvest_jobs.STALE_AFTER - 5)

    out = ft.tool_harvest_status("effect-1")

    assert "**stopped reporting**" in out
    assert "nothing partial was stored" in out
    assert "**running**" not in out


def test_a_harvest_that_failed_on_another_instance_is_reported(kb, monkeypatch):
    """Live on Vercel, 2026-09-15: `harvest_status()` listed `markdownify-1`
    as failed, and two calls later -- on another instance -- said nothing had
    finished, failed or stopped in fifteen minutes. The record is now in the
    store every instance shares, and the tool reads it from there."""
    class Ledger:
        rows = {"markdownify-1": {
            "id": "markdownify-1", "label": "markdownify", "state": FAILED,
            "phase": "resolving", "url": "", "pages": 0, "expected": None,
            "started": time.time() - 3, "updated": time.time(),
            "finished": time.time(), "pid": 7, "result": "",
            "error": "OSError: [Errno 16] Device or resource busy "
                     "(at engine.py:396 in _resolves_private)"}}

        def publish_harvest(self, record): self.rows[record["id"]] = record
        def harvests(self): return list(self.rows.values())
        def forget_harvest(self, job_id): self.rows.pop(job_id, None)

    monkeypatch.setattr(harvest_jobs, "SHARED", lambda: Ledger())
    assert not list(harvest_jobs.state_dir().glob("*.json")), "nothing on this machine"

    out = ft.tool_harvest_status()
    assert "markdownify-1" in out and "FAILED" in out
    assert "none finished, failed or stopped" not in out

    out = ft.tool_harvest_status("markdownify-1")
    assert "**failed**" in out and "Errno 16" in out


# ── waiting ──────────────────────────────────────────────────
def test_wait_returns_as_soon_as_the_harvest_settles(kb):
    job, gate = _held()
    threading.Timer(0.4, gate.set).start()

    began = time.time()
    out = ft.tool_harvest_status("effect-1", wait=10)
    took = time.time() - began

    assert "**done**" in out
    assert took < 5, f"kept waiting {took:.1f}s after the harvest finished"


def test_wait_is_capped_at_the_deadline(kb, monkeypatch):
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 0.5)
    _, gate = _held()
    try:
        began = time.time()
        out = ft.tool_harvest_status("effect-1", wait=60)
        took = time.time() - began
    finally:
        gate.set()

    assert "**running**" in out
    assert took < 2, f"waited {took:.1f}s past a 0.5s deadline"


def test_wait_does_not_wait_when_nothing_is_running(kb):
    began = time.time()
    ft.tool_harvest_status("effect-1", wait=10)
    assert time.time() - began < 1


# ── nothing to report ────────────────────────────────────────
def test_an_unknown_harvest_that_is_already_stored_points_at_the_store(kb):
    ft.store().save("effect", "v3", "https://effect.website/docs/", "sitemap",
                    [("Intro", "https://effect.website/docs/", "hello")], complete=True)

    out = ft.tool_harvest_status("effect")

    assert "already stored" in out
    assert 'read_knowledge_base(name="effect")' in out
    assert "learn_technology" not in out


def test_an_unknown_harvest_id_suggests_the_bare_name(kb):
    out = ft.tool_harvest_status("effect-1")
    assert "No harvest called `effect-1`" in out
    assert 'learn_technology(name="effect")' in out


def test_no_argument_lists_running_and_recent(kb):
    _write_record(id="effect-1", label="effect")
    _write_record(id="astro-1", label="astro", state=DONE, finished=time.time(),
                  pages=385, expected=385)

    out = ft.tool_harvest_status()

    assert "1 harvest running:" in out
    assert "**effect**" in out and "118/703" in out
    assert "Ended in the last 15 minutes:" in out
    assert "**astro**" in out and "finished" in out
    assert "DocsForge's own knowledge base" in out


def test_an_idle_status_says_so(kb):
    out = ft.tool_harvest_status()
    assert "No harvest is running" in out
    assert "list_knowledge_base" in out


def test_a_serverless_host_says_no_harvest_can_run_here(kb, monkeypatch):
    monkeypatch.setattr(harvest_jobs, "EPHEMERAL", True)
    out = ft.tool_harvest_status()
    assert "cannot keep working after a request ends" in out
    assert "python main.py" in out


# ── the still-running result now names the tool ─────────────
def test_learn_technology_points_a_slow_harvest_at_the_status_tool(kb, monkeypatch):
    from docsforge.core import resolver

    got = resolver.Resolution(name="effect", ecosystem="npm", release="")
    cand = resolver.Candidate("https://x.dev/docs/", "npm:homepage", 0.8, "stubbed",
                              True, "names it 9 times")
    got.candidates = [cand]; got.best = cand
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 0.3)
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: got)
    monkeypatch.setattr(ft, "tool_harvest_docs", lambda **kw: time.sleep(2) or "done")

    out = ft.tool_learn_technology("effect")

    assert "harvest_status(harvest=" in out
    assert "not to your context" in out
