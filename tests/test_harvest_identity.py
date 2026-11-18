"""
Harvest identity: contract v1, Phase 1 (issue #42).

A job id (`effect-1`) is a handle for people to quote. It is handed out again
once its status record is swept, so it cannot name anything that has to be
found later. Phase 1 gives every tracked run a `run_id` and every stored
revision a `harvest_id`, both unique for good, and makes the run's record say
what it published as it publishes it. That is what lets a status check tell:

* "failed, nothing stored" from "failed after storing the main documentation";
* "stopped, nothing stored" from "stopped after storing something";
* a harvest that ended days ago from one that never ran.

And a harvest that went to the file store because the database was down says
so, with the command that copies it across (Issues B9).

No network: harvests here are stubs that write through the real stores.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from docsforge.store import kb_store
from docsforge.store.kb_store import FileStore, PostgresStore, RunContext
from docsforge.tools import forge_tools as ft
from docsforge.tools import harvest_jobs
from docsforge.tools.harvest_jobs import DONE, FAILED, RUNNING

DSN = os.environ.get("DOCSFORGE_TEST_DB", "")


@pytest.fixture
def kb(tmp_path):
    files = FileStore(tmp_path / "kb")
    ft.reset_store(files)
    harvest_jobs.clear()
    yield files
    harvest_jobs.clear()
    ft.reset_store(None)


def _harvest(files, tech: str, version: str = "v1", pages: int = 2) -> dict:
    """What a harvest does to the store: open a writer, add, settle."""
    with files.writer(tech, version, f"https://{tech}.dev/", "crawl", expected=pages) as w:
        for i in range(pages):
            w.add(f"Page {i}", f"https://{tech}.dev/{i}", f"body {i}")
        return w.settle(complete=True, expected=pages)


def _run(label: str, work) -> harvest_jobs.Job:
    job = harvest_jobs.start(label, work)
    assert harvest_jobs.wait(job, 10), "the stub harvest did not finish"
    return job


def _write_record(**fields) -> dict:
    """A record as another process left it on this machine."""
    data = {"id": "effect-1", "label": "effect", "state": RUNNING,
            "phase": "harvesting", "url": "", "pages": 3, "expected": None,
            "started": time.time() - 100, "updated": time.time(),
            "finished": 0.0, "error": "", "pid": 4242, "result": ""}
    data.update(fields)
    directory = harvest_jobs.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


# ── I1: a run is never confused with another ─────────────────────
def test_a_job_id_can_come_back_but_a_run_id_cannot(kb):
    first = _run("effect", lambda p: _harvest(kb, "effect") and "ok")
    harvest_jobs.clear()                  # the record is swept, the process restarts
    second = _run("effect", lambda p: _harvest(kb, "effect") and "ok")

    assert first.id == second.id == "effect-1", "the handle is reused, as before"
    assert first.run_id and second.run_id and first.run_id != second.run_id
    assert first.published[0]["harvest_id"] != second.published[0]["harvest_id"]


def test_ids_have_the_documented_shape():
    assert kb_store.new_run_id().startswith("run-")
    assert kb_store.new_harvest_id().startswith("hv-")
    assert len({kb_store.new_run_id() for _ in range(50)}) == 50


# ── P1.2: a run records what it published, as it publishes ───────
def test_a_run_groups_the_revisions_it_published(kb):
    def work(progress):
        _harvest(kb, "acme")
        _harvest(kb, "acme--sdk-acme-dev")          # a federated corpus
        return "ok"

    job = _run("acme", work)
    assert job.state == DONE and job.outcome == "published"
    assert [p["technology"] for p in job.published] == ["acme", "acme--sdk-acme-dev"]
    assert all(p["harvest_id"].startswith("hv-") for p in job.published)

    # The store carries the run too, so it can be asked without the record.
    found = kb.revisions_of_run(job.run_id)
    assert [r["harvest_id"] for r in found] == [p["harvest_id"] for p in job.published]
    assert kb.entry("acme", "v1")["run_id"] == job.run_id

    record = json.loads((harvest_jobs.state_dir() / f"{job.id}.json").read_text("utf-8"))
    assert record["run_id"] == job.run_id
    assert record["outcome"] == "published"
    assert len(record["published"]) == 2


def test_a_revision_written_outside_a_run_has_no_run_id(kb):
    entry = _harvest(kb, "solo")
    assert entry["run_id"] is None and entry["harvest_id"].startswith("hv-")


def test_a_run_that_stores_nothing_new_is_finished_not_published(kb):
    job = _run("effect", lambda p: "already stored")
    assert job.outcome == "finished" and job.published == []


# ── I4: what a failed or stopped run had already stored ──────────
def test_a_failure_after_publishing_reports_what_was_published(kb):
    def work(progress):
        _harvest(kb, "effect")
        raise RuntimeError("related corpora went away")

    job = _run("effect", work)
    assert job.state == FAILED and job.outcome == "partial"

    out = ft.tool_harvest_status(job.id)
    assert "Nothing was stored" not in out
    assert "Stored before it failed" in out
    assert job.published[0]["harvest_id"] in out
    assert "revision(s) were stored before that" in job.line()


def test_a_failure_before_publishing_still_says_nothing_was_stored(kb):
    def work(progress):
        raise RuntimeError("no documentation found")

    job = _run("effect", work)
    assert job.outcome == "failed"
    assert "Nothing was stored" in ft.tool_harvest_status(job.id)


def test_a_stalled_run_that_published_says_so(kb):
    """The process died after publishing and before it could write that down:
    the store is asked by run id."""
    token = kb_store.CURRENT_RUN.set(RunContext("run-dead"))
    try:
        entry = _harvest(kb, "effect")
    finally:
        kb_store.CURRENT_RUN.reset(token)
    _write_record(run_id="run-dead", updated=time.time() - harvest_jobs.STALE_AFTER - 5)

    out = ft.tool_harvest_status("effect-1")
    assert "stopped reporting" in out
    assert "after it had stored" in out and entry["harvest_id"] in out
    assert harvest_jobs.get("effect-1").outcome == "stopped", \
        "the record alone does not know; the status check does"


def test_a_stalled_run_that_stored_nothing_says_so_plainly(kb):
    _write_record(run_id="run-dead", updated=time.time() - harvest_jobs.STALE_AFTER - 5)
    out = ft.tool_harvest_status("effect-1")
    assert "nothing from this run is in the store" in out


def test_a_stalled_record_from_before_run_ids_keeps_the_hedged_wording(kb):
    _write_record(updated=time.time() - harvest_jobs.STALE_AFTER - 5)
    out = ft.tool_harvest_status("effect-1")
    assert "list_knowledge_base` shows whether it landed" in out


# ── I3: an ended run can be asked after by id ────────────────────
class ClaimingLedger:
    """A shared ledger, as a database provides one."""

    def __init__(self, taken=()):
        self.rows: dict[str, dict] = {}
        self.refuse = set(taken)        # ids another instance claims first

    def publish_harvest(self, record):
        self.rows[record["id"]] = dict(record)

    def harvests(self):
        return [dict(r) for r in self.rows.values()]

    def forget_harvest(self, job_id):
        self.rows.pop(job_id, None)

    def claim_harvest(self, record):
        if record["id"] in self.refuse or record["id"] in self.rows:
            return False
        self.rows[record["id"]] = dict(record)
        return True


def test_an_ended_run_is_found_by_id_long_after_the_listing_forgets_it(kb, monkeypatch):
    ledger = ClaimingLedger()
    monkeypatch.setattr(harvest_jobs, "SHARED", lambda: ledger)
    gone = time.time() - harvest_jobs.KEEP_RECORDS_FOR - 3600
    ledger.rows["effect-4"] = {
        "id": "effect-4", "run_id": "run-old", "label": "effect", "state": FAILED,
        "phase": "resolving", "pages": 0, "started": gone - 5, "updated": gone,
        "finished": gone, "error": "the site refused every request", "result": ""}

    listing = ft.tool_harvest_status()
    assert "effect-4" not in listing, "not in the listing of the last 15 minutes"
    for wanted in ("effect-4", "run-old"):
        out = ft.tool_harvest_status(wanted)
        assert "**failed**" in out and "the site refused every request" in out


# ── I2: two instances never share a job id ───────────────────────
def test_a_job_id_another_instance_claimed_first_is_skipped(kb, monkeypatch):
    ledger = ClaimingLedger(taken={"effect-1"})
    monkeypatch.setattr(harvest_jobs, "SHARED", lambda: ledger)
    job = _run("effect", lambda p: "ok")
    assert job.id == "effect-2"
    assert ledger.rows["effect-2"]["state"] == DONE


def test_an_adopted_record_keeps_its_run(kb):
    """A detached worker continues the launcher's record: one run, not two."""
    reserved = harvest_jobs.reserve("effect")
    job = harvest_jobs.start("effect", lambda p: "ok", job_id=reserved.id)
    assert harvest_jobs.wait(job, 10)
    assert job.id == reserved.id and job.run_id == reserved.run_id


# ── B9: a harvest that went to the fallback store says so ────────
def test_a_harvest_into_the_fallback_store_says_so(kb):
    kb.wanted_dsn = "postgresql://unreachable/db"
    job = _run("effect", lambda p: _harvest(kb, "effect") and "ok")

    assert job.published[0]["fallback"] is True
    out = ft.tool_harvest_status(job.id)
    assert "Stored in the local file store, not the database" in out
    assert "--sync-store" in out


def test_a_harvest_into_the_configured_store_says_nothing_about_it(kb):
    entry = _harvest(kb, "effect")
    assert entry["fallback"] is False and ft._fallback_note(entry) == ""


def test_the_sync_command_needs_a_database(monkeypatch, capsys):
    from docsforge.core import engine
    monkeypatch.delenv("DOCSFORGE_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert engine.main(["--sync-store"]) == 1
    assert "DOCSFORGE_DB" in capsys.readouterr().err


# ── Postgres ─────────────────────────────────────────────────────
def _pg_or_skip() -> PostgresStore:
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


def test_a_postgres_revision_carries_its_run_and_its_id():
    store = _pg_or_skip()
    token = kb_store.CURRENT_RUN.set(RunContext("run-pg-ids"))
    try:
        entry = _harvest(store, "identity-pg")
    finally:
        kb_store.CURRENT_RUN.reset(token)
        try:
            found = store.revisions_of_run("run-pg-ids")
        finally:
            store.delete("identity-pg")
    assert entry["run_id"] == "run-pg-ids" and entry["harvest_id"].startswith("hv-")
    assert [r["harvest_id"] for r in found] == [entry["harvest_id"]]
    assert found[0]["stored_pages"] == 2


def test_only_one_postgres_claim_on_a_job_id_wins():
    store = _pg_or_skip()
    record = {"id": "identity-claim-1", "updated": time.time(), "state": RUNNING}
    store.forget_harvest(record["id"])
    try:
        results = []
        barrier = threading.Barrier(4)

        def claim():
            barrier.wait()
            results.append(store.claim_harvest(record))

        threads = [threading.Thread(target=claim) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, False, False, True]
    finally:
        store.forget_harvest(record["id"])


def test_sync_store_copies_a_fallback_revision_once(tmp_path):
    db = _pg_or_skip()
    files = FileStore(tmp_path / "kb")
    entry = _harvest(files, "identity-sync", pages=3)
    try:
        first = kb_store.sync_store(files, db)
        assert first == [("identity-sync", "v1", "copied, 3 pages")]
        assert db.entry("identity-sync", "v1")["pages"] == 3
        assert [db.page("identity-sync", "v1", i) for i in (1, 2, 3)] == \
               [files.page("identity-sync", "v1", i) for i in (1, 2, 3)]
        with db._borrow() as cx:
            held = cx.execute("select harvest_id from doc_version v join technology t "
                              "on t.id = v.technology_id where t.name = %s and "
                              "v.state = 'ready'", ("identity-sync",)).fetchone()
        assert held[0] == entry["harvest_id"], "copied under the same id"
        assert kb_store.sync_store(files, db) == [], "a second sync copies nothing"
    finally:
        db.delete("identity-sync")


def test_sync_store_leaves_a_newer_database_harvest_alone(tmp_path):
    db = _pg_or_skip()
    files = FileStore(tmp_path / "kb")
    _harvest(files, "identity-newer", pages=2)
    try:
        _harvest(db, "identity-newer", pages=5)          # harvested after the fallback
        done = kb_store.sync_store(files, db)
        assert done == [("identity-newer", "v1",
                         "skipped: the database has a newer harvest")]
        assert db.entry("identity-newer", "v1")["pages"] == 5
    finally:
        db.delete("identity-newer")
