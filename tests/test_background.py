"""
Offline tests for background harvesting (F7).

The defect: `learn_technology` blocked for as long as the harvest took, so a
703-page site timed out every MCP client and the headline tool read as broken.
The fix must hold two things at once, and each test below pins one of them:

  * a harvest that finishes quickly behaves exactly as it always did, and
  * a harvest that does not still returns promptly, keeps running, and is
    reported afterwards rather than lost.

No network and no real crawling: `_resolve` and `tool_harvest_docs` are stubbed,
because what is under test is who waits for the harvest, not the harvest.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.tools import harvest_jobs
from docsforge.core import resolver
from docsforge.store.kb_store import FileStore


@pytest.fixture
def kb(tmp_path):
    ft.reset_store(FileStore(tmp_path))
    harvest_jobs.clear()
    yield tmp_path
    for job in harvest_jobs.running():
        job.done.wait(10)          # never tear the store down under a live thread
    harvest_jobs.clear()
    ft.reset_store(None)


def resolution(url="https://x.dev/docs/", verified=True, name="effect", release=""):
    got = resolver.Resolution(name=name, ecosystem="npm", release=release)
    cand = resolver.Candidate(url, "npm:homepage", 0.8, "stubbed", verified,
                              "names it 9 times" if verified else "never mentions it")
    got.candidates = [cand]
    got.best = cand if verified else None
    if not verified:
        got.note = "Found 1 candidate(s) but none could be confirmed to document it."
    return got


def _stub(monkeypatch, harvest, deadline=0.3):
    monkeypatch.setattr(harvest_jobs, "DEADLINE", deadline)
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    monkeypatch.setattr(ft, "_harvest_now", harvest)


# ── the fast path must not have moved ────────────────────
def test_a_harvest_that_beats_the_deadline_is_returned_whole(kb, monkeypatch):
    _stub(monkeypatch, lambda **kw: "Harvested **effect** v3 - 2 pages")

    out = ft.tool_learn_technology("effect")

    assert "Harvested **effect**" in out
    assert "Resolved **effect**" in out
    # No trace of the machinery leaks into the ordinary answer.
    assert "still running" not in out and "Harvest id" not in out


# ── the slow path is why this exists ─────────────────────
def test_a_slow_harvest_returns_promptly_instead_of_blocking(kb, monkeypatch):
    def slow(**kw):
        time.sleep(3)
        return "Harvested **effect** v3 - 703 pages"

    _stub(monkeypatch, slow, deadline=0.3)

    began = time.time()
    out = ft.tool_learn_technology("effect")
    waited = time.time() - began

    # The whole point: the caller is released long before the harvest ends.
    assert waited < 2, f"blocked {waited:.1f}s despite a 0.3s deadline"
    assert "still running" in out and "Harvest id" in out


def test_a_slow_harvest_still_finishes_and_stores_its_result(kb, monkeypatch):
    def slow(**kw):
        time.sleep(0.6)
        return "Harvested **effect** v3 - 703 pages"

    _stub(monkeypatch, slow, deadline=0.1)
    ft.tool_learn_technology("effect")

    job = harvest_jobs.running()[0]
    assert job.done.wait(10), "background harvest never finished"
    # Returning early must not mean returning instead of harvesting.
    assert job.state == "done"
    assert "703 pages" in job.result


def test_the_caller_is_told_not_to_start_it_again(kb, monkeypatch):
    # A model reading "still running" as failure would re-issue the call and
    # crawl the same site twice, which is the cost this change exists to avoid.
    _stub(monkeypatch, lambda **kw: time.sleep(3) or "done", deadline=0.3)

    out = ft.tool_learn_technology("effect")

    assert "Do not call learn_technology" in out
    assert "again" in out


# ── failures must survive the round trip ─────────────────
def test_a_resolution_failure_raises_the_original_error(kb, monkeypatch):
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution(verified=False))

    with pytest.raises(ft.ForgeError) as caught:
        ft.tool_learn_technology("effect")

    # Not a stringified copy: the candidate list a caller acts on is still there.
    message = str(caught.value)
    assert "Candidates considered" in message
    assert "https://x.dev/docs/" in message
    assert "ForgeError:" not in message


def test_a_background_failure_is_reported_not_swallowed(kb, monkeypatch):
    def explodes(**kw):
        time.sleep(0.4)
        raise ft.ForgeError("the site went away")

    _stub(monkeypatch, explodes, deadline=0.1)
    ft.tool_learn_technology("effect")

    job = harvest_jobs.recent()[0] if harvest_jobs.recent() else harvest_jobs.running()[0]
    assert job.done.wait(10)
    assert job.state == "failed"
    # A harvest that died in the background leaves no other trace, so the
    # listing is the only place a caller can learn it went wrong.
    assert "the site went away" in ft.tool_list_knowledge_base()


# ── progress is visible where a model already looks ──────
def test_a_running_harvest_shows_up_in_the_listing(kb, monkeypatch):
    _stub(monkeypatch, lambda **kw: time.sleep(2) or "done", deadline=0.3)
    ft.tool_learn_technology("effect")

    listing = ft.tool_list_knowledge_base()

    assert "still running" in listing
    assert "effect" in listing


def test_an_idle_listing_says_nothing_about_jobs(kb):
    # The block must be invisible when there is nothing in flight, or every
    # ordinary listing grows noise.
    listing = ft.tool_list_knowledge_base()
    assert "still running" not in listing


# ── the progress counter itself ──────────────────────────
def test_the_counting_fetcher_reports_pages_and_the_expected_total(monkeypatch):
    # Progress is counted at the fetcher rather than threaded through every
    # strategy in harvest(), so the counting itself is what needs proving.
    monkeypatch.setattr(ft.Fetcher, "html", lambda self, url: "<html><body>x</body></html>")

    progress = harvest_jobs.Progress(phase="harvesting")   # set by work(), as in the real call
    stats = {"discovered": 703}
    fetcher = ft._CountingFetcher(ft._options(crawl=True), progress, stats)

    fetcher.html("https://x.dev/docs/a")
    fetcher.html("https://x.dev/docs/b")

    assert progress.pages == 2
    assert progress.expected == 703
    assert progress.line() == "harvesting 2/703 pages"


def test_a_background_harvest_counts_the_pages_it_fetches(kb, monkeypatch):
    seen = {}

    def fake_harvest(url, opts, fetcher=None, stats=None, sink=None):
        seen["fetcher"] = fetcher
        stats["discovered"] = 3
        stats["whole"] = True
        for path in ("a", "b", "c"):
            fetcher.html(f"https://x.dev/docs/{path}")
        return [ft.Doc(f"https://x.dev/docs/{p}", p.upper(), "body") for p in "abc"], "sitemap"

    monkeypatch.setattr(ft.Fetcher, "html", lambda self, url: "<html><body>x</body></html>")
    monkeypatch.setattr(ft, "harvest", fake_harvest)
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())

    progress = harvest_jobs.Progress()
    out = ft._harvest_now(url="https://x.dev/docs/", name="effect",
                          progress=progress)

    assert isinstance(seen["fetcher"], ft._CountingFetcher)
    assert progress.pages == 3
    assert progress.phase == "storing"
    assert "3 pages" in out


# ── the version label when the site names none ───────────
def _unversioned_harvest(declared: str = ""):
    def fake_harvest(url, opts, fetcher=None, stats=None, sink=None):
        stats["discovered"] = 1
        stats["whole"] = True
        if declared:
            stats["declared_version"] = declared
        return [ft.Doc(f"{url}overview", "Overview", "body")], "llms_txt"
    return fake_harvest


def test_the_registrys_release_labels_an_unversioned_site_and_says_so(kb, monkeypatch):
    monkeypatch.setattr(ft, "harvest", _unversioned_harvest())
    out = ft._harvest_now(url="https://angular.dev/", name="angular", release_hint="20.2.1")
    assert "20.2.1" in out
    assert "registry's current release" in out and "URLs name no version" in out
    assert any(v.get("version") == "20.2.1" for v in ft.store().versions("angular"))


def test_the_sites_own_declaration_outranks_the_registry(kb, monkeypatch):
    monkeypatch.setattr(ft, "harvest", _unversioned_harvest(declared="19.0.0"))
    out = ft._harvest_now(url="https://angular.dev/", name="angular", release_hint="20.2.1")
    assert "19.0.0" in out and "20.2.1" not in out
    assert "registry's current release" not in out


def test_a_version_in_the_url_outranks_the_registry(kb, monkeypatch):
    def fake_harvest(url, opts, fetcher=None, stats=None, sink=None):
        stats["discovered"] = 1; stats["whole"] = True
        return [ft.Doc("https://docs.pydantic.dev/2.11/overview", "Overview", "body")], "sitemap"
    monkeypatch.setattr(ft, "harvest", fake_harvest)
    out = ft._harvest_now(url="https://docs.pydantic.dev/2.11/", name="pydantic", release_hint="2.12.0")
    assert "2.11" in out and "2.12.0" not in out


def test_without_a_hint_the_date_still_stands_in(kb, monkeypatch):
    import time
    monkeypatch.setattr(ft, "harvest", _unversioned_harvest())
    out = ft.tool_harvest_docs(url="https://angular.dev/", name="angular")
    assert time.strftime("%Y-%m-%d") in out and "registry's current release" not in out


def test_learn_technology_hands_the_release_to_the_harvest(kb, monkeypatch):
    seen = {}
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution(release="20.2.1"))
    def fake_tool_harvest(url, **kw):
        seen.update(kw); return "harvested"
    monkeypatch.setattr(ft, "_harvest_now", fake_tool_harvest)
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)
    ft.tool_learn_technology(name="angular")
    assert seen.get("release_hint") == "20.2.1"


# ── harvest_docs is watchable too ─────────────────────────
#
# Live against Vercel, 16 September 2026:
#
#   harvest_docs(url="https://docs.pydantic.dev/latest/")
#       -> the connector stopped responding. Twice.
#   harvest_status()
#       -> no running job, no failed job, nothing.
#
# `learn_technology` had the record, the duplicate check and the deadline;
# `harvest_docs` ran its crawl inline and registered nothing, so a harvest
# larger than one request left no trace of ever having existed. A caller who
# cannot tell "still crawling" from "died four minutes ago" can only retry,
# which starts the whole crawl again.

def _slow_crawl(gate, seen=None):
    def crawl(url, name=None, progress=None, **kw):
        if seen is not None:
            seen["url"] = url
        if progress is not None:
            progress.pages = 7
        gate.wait(timeout=30)
        return f"Harvested **{name}** - 7 pages"
    return crawl


def test_a_harvest_docs_that_beats_the_deadline_is_returned_whole(kb, monkeypatch):
    """The common case, and it must read exactly as it always did."""
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)
    monkeypatch.setattr(ft, "_harvest_now",
                        lambda **kw: "Harvested **effect** v3 - 2 pages")
    out = ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
    assert out == "Harvested **effect** v3 - 2 pages"


def test_a_slow_harvest_docs_returns_an_id_instead_of_hanging(kb, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 0.3)
    monkeypatch.setattr(ft, "_harvest_now", _slow_crawl(gate))
    try:
        out = ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
        assert "still running" in out
        assert "harvest_status" in out
    finally:
        gate.set()


def test_a_slow_harvest_docs_is_visible_to_harvest_status(kb, monkeypatch):
    """The whole point. `harvest_status()` called straight afterwards used to
    answer that nothing was running and nothing had failed."""
    gate = threading.Event()
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 0.3)
    monkeypatch.setattr(ft, "_harvest_now", _slow_crawl(gate))
    try:
        ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
        assert any(j.label == "effect" for j in harvest_jobs.running())
        assert "effect" in ft.tool_harvest_status()
    finally:
        gate.set()


def test_a_harvest_docs_failure_is_recorded_rather_than_lost(kb, monkeypatch):
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)

    def boom(**kw):
        raise ft.ForgeError("Harvested nothing from https://x.dev/docs/")
    monkeypatch.setattr(ft, "_harvest_now", boom)

    with pytest.raises(ft.ForgeError):
        ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
    assert any(j.label == "effect" and j.state == harvest_jobs.FAILED
               for j in harvest_jobs.recent())


def test_harvest_docs_refuses_to_crawl_what_is_already_being_crawled(kb, monkeypatch):
    """Two calls for one site is the exact cost the record exists to avoid,
    and retrying is the only thing a caller who was told nothing could do."""
    gate = threading.Event()
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 0.3)
    monkeypatch.setattr(ft, "_harvest_now", _slow_crawl(gate))
    try:
        ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
        again = ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
        assert "already" in again or "still running" in again
        assert "Do not call learn_technology" in again or "still running" in again
    finally:
        gate.set()


def test_harvest_docs_names_itself_when_it_hands_work_to_the_server(kb, monkeypatch):
    """A stdio server hands the harvest to the long-lived one. Sending
    harvest_docs's own arguments to learn_technology would resolve a name the
    caller never gave and harvest whatever that found."""
    sent = {}

    def fake_hand_off(label, kwargs, tool="learn_technology"):
        sent.update(label=label, kwargs=kwargs, tool=tool)
        return None                      # fall through to running it here
    monkeypatch.setattr(harvest_jobs, "DETACHED", True)
    monkeypatch.setattr(harvest_jobs, "hand_off", fake_hand_off)
    monkeypatch.setattr(harvest_jobs, "DEADLINE", 5)
    monkeypatch.setattr(ft, "_harvest_now", lambda **kw: "Harvested - 1 page")

    ft.tool_harvest_docs(url="https://x.dev/docs/", name="effect")
    assert sent["tool"] == "harvest_docs"
    assert sent["kwargs"]["url"] == "https://x.dev/docs/"
