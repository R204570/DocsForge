"""
The store keeps what it publishes, and says what it holds.

Contract v1, Phase 0 (DOC-5). Each test here is a condition that broke the
store before this phase, reproduced against `633ae8e` and listed in the
proposal's break map:

    B1  two harvests of one version mixed their pages in one file
    B2  …and in Postgres deleted the published version, reporting success
    B3  a crash while publishing destroyed the version being replaced
    B4  two processes publishing at once lost one from index.json
    B5  a half-written index.json orphaned every other technology
    B7  a newline in a title merged that page into the one before it
    B8  page bodies did not come back byte for byte
    B10 two labels that slug alike shared one file
    M1  `complete` was decided before the store's refusals were known
    M2  federation judged a recount, not the stored `complete`
    M3  a selected corpus that failed dropped out of the roll-up
    I4  a federation error after publishing reported "Nothing was stored"

The file-store tests need nothing; the Postgres ones are gated on
`DOCSFORGE_TEST_DB` like the rest of the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from docsforge.store import kb_store
from docsforge.store.kb_store import FileStore, PostgresStore, StoreError
from docsforge.tools import forge_tools as ft

REPO = Path(__file__).resolve().parent.parent
DSN = os.environ.get("DOCSFORGE_TEST_DB", "")

OLD = [(f"Old {i}", f"https://x.dev/old/{i}", f"old body {i} " * 40) for i in range(5)]


@pytest.fixture
def files(tmp_path):
    return FileStore(tmp_path / "kb")


def _titles_in_file(path: Path) -> list[str]:
    _, blocks = kb_store.split_pages(path.read_text(encoding="utf-8"))
    return [kb_store.parse_page(b)[0] for b in blocks]


# ── B1: one partial per writer ──────────────────────────────────
def test_two_writers_on_one_version_never_mix_pages(files):
    files.save("effect", "v3", "https://old/", "crawl", OLD, complete=True)
    a = files.writer("effect", "v3", "https://a.example/", "crawl")
    b = files.writer("effect", "v3", "https://b.example/", "crawl")
    for i in range(3):
        a.add(f"A{i}", f"https://a.example/{i}", f"alpha {i}")
    for i in range(2):
        b.add(f"B{i}", f"https://b.example/{i}", f"bravo {i}")

    assert a.partial != b.partial, "each writer streams into its own partial"

    a.settle(complete=True, expected=3)
    md = Path(files.entry("effect", "v3")["file"])
    assert _titles_in_file(md) == ["A0", "A1", "A2"]
    assert files.entry("effect", "v3")["titles"] == ["A0", "A1", "A2"]

    b.settle(complete=True, expected=2)
    # The later settle wins, whole — never a mixture of the two.
    assert _titles_in_file(md) == ["B0", "B1"]
    assert files.entry("effect", "v3")["titles"] == ["B0", "B1"]
    assert files.entry("effect", "v3")["pages"] == 2


def test_a_settle_waits_for_another_settle_of_the_same_version(files):
    w = files.writer("effect", "v3", "https://x.dev/", "crawl")
    w.add("Only", "https://x.dev/only", "body")
    held = threading.Event()
    release = threading.Event()

    def hold():
        with files._version_lock("effect", "v3"):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    started = time.monotonic()
    threading.Timer(0.4, release.set).start()
    w.settle(complete=True, expected=1)
    holder.join(5)

    assert time.monotonic() - started >= 0.35, "settle published while another held the version"


# ── B3: publish by rename ───────────────────────────────────────
def test_a_crash_while_publishing_keeps_the_previous_version(files, monkeypatch):
    files.save("effect", "v3", "https://x.dev/", "crawl", OLD, complete=True)
    md = Path(files.entry("effect", "v3")["file"])
    before = md.read_bytes()

    w = files.writer("effect", "v3", "https://x.dev/", "crawl")
    for i in range(5):
        w.add(f"New {i}", f"https://x.dev/new/{i}", "new body " * 40)

    def killed(*_a, **_k):
        raise KeyboardInterrupt("process killed while publishing")

    monkeypatch.setattr(kb_store.os, "fsync", killed)
    with pytest.raises(KeyboardInterrupt):
        w.settle(complete=True, expected=5)
    monkeypatch.undo()

    assert md.read_bytes() == before, "the published version was touched"
    assert files.entry("effect", "v3")["titles"][0] == "Old 0"
    assert not list(md.parent.glob("*.tmp")), "a half-written file was left behind"


def test_a_failed_rename_keeps_the_previous_version(files, monkeypatch):
    files.save("effect", "v3", "https://x.dev/", "crawl", OLD, complete=True)
    md = Path(files.entry("effect", "v3")["file"])
    before = md.read_bytes()

    w = files.writer("effect", "v3", "https://x.dev/", "crawl")
    w.add("New", "https://x.dev/new", "new body")
    monkeypatch.setattr(kb_store, "_replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    with pytest.raises(OSError):
        w.settle(complete=True, expected=1)

    assert md.read_bytes() == before
    assert files.entry("effect", "v3")["pages"] == 5


# ── B10: one file per label ─────────────────────────────────────
def test_labels_that_slug_alike_do_not_share_a_file(files):
    files.save("lib", "v3 beta", "https://x.dev/", "crawl",
               [("Beta", "https://x.dev/b", "beta body")], complete=True)
    files.save("lib", "v3-beta", "https://x.dev/", "crawl",
               [("Dash", "https://x.dev/d", "dash body")], complete=True)

    spaced, dashed = files.entry("lib", "v3 beta"), files.entry("lib", "v3-beta")
    assert spaced["file"] != dashed["file"]
    assert files.read("lib", version="v3 beta")[0].count("beta body") == 1
    assert "dash body" not in files.read("lib", version="v3 beta")[0]
    assert "dash body" in files.read("lib", version="v3-beta")[0]


# ── B4: index.json under a lock, written whole ──────────────────
_SETTLE_MANY = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from docsforge.store.kb_store import FileStore
store = FileStore(Path(sys.argv[2]))
for i in range(int(sys.argv[4])):
    store.save(f"{sys.argv[3]}{i}", "v1", "https://x.dev/", "crawl",
               [("p", f"https://x.dev/{i}", "body")], True, 1)
"""


def test_two_processes_settling_keep_both_index_entries(tmp_path):
    root = tmp_path / "kb"
    n = 20
    procs = [subprocess.Popen([sys.executable, "-c", _SETTLE_MANY, str(REPO), str(root),
                               prefix, str(n)])
             for prefix in ("alpha", "bravo")]
    for p in procs:
        assert p.wait(timeout=120) == 0

    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 2 * n, f"lost {2 * n - len(index)} entries"


# ── B5: a damaged index is rebuilt, never emptied ───────────────
def test_a_damaged_index_is_never_written_back_smaller(files):
    files.save("alpha", "v1", "https://a.dev/", "sitemap",
               [("A", "https://a.dev/a", "body a")], complete=True, expected=1)
    files.save("bravo", "v2", "https://b.dev/", "crawl",
               [("B1", "https://b.dev/1", "b1"), ("B2", "https://b.dev/2", "b2")],
               complete=False, expected=5)
    index = files.index_path.read_text(encoding="utf-8")
    files.index_path.write_text(index[: len(index) // 2], encoding="utf-8")

    seen = sorted(t["name"] for t in files.technologies()[0])
    assert seen == ["alpha", "bravo"], "a damaged index read as an empty store"

    files.save("charlie", "v1", "https://c.dev/", "crawl",
               [("C", "https://c.dev/c", "c")], complete=True, expected=1)
    after = json.loads(files.index_path.read_text(encoding="utf-8"))
    assert sorted(after) == ["alpha@v1", "bravo@v2", "charlie@v1"]

    recovered = files.entry("bravo", "v2")
    assert recovered["pages"] == 2
    assert recovered["source"] == "https://b.dev/"
    # The file never said how complete it was. Unknown, not a guess.
    assert recovered["complete"] is None
    assert list(files.root.glob("index.json.damaged-*")), "the damaged index was not kept"


# ── B7, B8: the page read back is the page stored ───────────────
def test_a_title_with_a_newline_round_trips(files):
    files.save("t", "v1", "https://t.dev/", "crawl",
               [("Intro", "https://t.dev/1", "first"),
                ("Getting\n   Started", "https://t.dev/2", "second"),
                ("API", "https://t.dev/3", "third")], complete=True, expected=3)
    pages = files.pages("t", "v1")
    assert [p["title"] for p in pages] == ["Intro", "Getting Started", "API"]
    assert files.page("t", "v1", 2)["content"] == "second"


def test_line_endings_round_trip_identically_on_every_os(files):
    body = "line one\r\nline two\r\n\r\nline three\rline four"
    files.save("crlf", "v1", "https://c.dev/", "crawl",
               [("Page", "https://c.dev/p", body)], complete=True, expected=1)

    assert files.page("crlf", "v1", 1)["content"] == \
        "line one\nline two\n\nline three\nline four"
    raw = Path(files.entry("crlf", "v1")["file"]).read_bytes()
    assert b"\r" not in raw, "the file on disk differs by operating system"


def test_every_page_reads_back_without_the_next_pages_separator(files):
    """B11, found writing these tests: every page but the last came back as
    its body plus `\\n\\n---`, the separator written before the page after it."""
    pages = [("A", "https://t.dev/a", "first"),
             ("B", "https://t.dev/b", "second ends in its own rule\n\n---"),
             ("C", "https://t.dev/c", "third")]
    files.save("sep", "v1", "https://t.dev/", "crawl", pages, complete=True, expected=3)

    assert [files.page("sep", "v1", i)["content"] for i in (1, 2, 3)] == \
        ["first", "second ends in its own rule\n\n---", "third"]


def test_clean_page_is_the_one_normal_form():
    assert kb_store.clean_page(" A\n  B ", " https://x.dev/a b> ", "\r\nx\r\n") == \
        ("A B", "https://x.dev/a%20b%3E", "x")


@pytest.mark.xfail(strict=True, reason="contract v1 Phase 2 (P2.3): length-delimited pages")
def test_a_body_that_looks_like_a_page_boundary_stays_one_page(files):
    body = "Intro.\n\n## Changelog\n\nSource: <https://up.example/CHANGELOG>\n\nQuoted."
    files.save("md", "v1", "https://m.dev/", "crawl",
               [("Guide", "https://m.dev/g", body), ("API", "https://m.dev/a", "api")],
               complete=True, expected=2)
    assert [p["title"] for p in files.pages("md", "v1")] == ["Guide", "API"]


# ── M1: settle enforces `complete` against what was stored ──────
def test_fewer_pages_than_counted_cannot_be_stored_as_complete(files):
    w = files.writer("short", "v1", "https://s.dev/", "sitemap", expected=3)
    w.add("One", "https://s.dev/1", "one")
    w.add("Two", "https://s.dev/2", "two")
    entry = w.settle(complete=True)

    assert entry["complete"] is False
    assert "2 of the 3" in entry["complete_reason"]
    assert files.entry("short", "v1")["complete"] is False


def test_a_refused_page_lowers_complete(files):
    w = files.writer("refused", "v1", "https://r.dev/", "sitemap", expected=1)
    w.add("One", "https://r.dev/1", "one")
    w.rejected.append(("https://r.dev/2", "too large"))
    entry = w.settle(complete=True)

    assert entry["complete"] is False
    assert "refused 1 page" in entry["complete_reason"]


@pytest.mark.parametrize("claimed", [False, None])
def test_only_true_is_ever_lowered(files, claimed):
    w = files.writer("weak", "v1", "https://w.dev/", "crawl", expected=9)
    w.add("One", "https://w.dev/1", "one")
    assert w.settle(complete=claimed)["complete"] is claimed


def test_a_page_the_store_refuses_makes_a_complete_harvest_partial(tmp_path, monkeypatch):
    ft.reset_store(FileStore(tmp_path / "kb"))

    def harvest(url, opts, fetcher=None, stats=None, sink=None):
        stats.update(discovered=2, whole=True)
        docs = [ft.Doc(url + "a", "Kept", "kept"), ft.Doc(url + "b", "Refused", "refused")]
        for d in docs:
            sink.add(d.title, d.url, d.markdown)
        return docs, "sitemap"

    real_add = kb_store._FileWriter.add

    def refusing_add(self, title, url, body):
        if title == "Refused":
            self.rejected.append((url, "too large for the full-text index"))
            return False
        return real_add(self, title, url, body)

    monkeypatch.setattr(ft, "harvest", harvest)
    monkeypatch.setattr(kb_store._FileWriter, "add", refusing_add)

    out = ft._harvest_now(url="https://r.dev/docs/", name="refusal", version="v1")

    assert ft.store().entry("refusal", "v1")["complete"] is False
    assert "INCOMPLETE" in out


# ── M2, M3: federation judges what is stored ────────────────────
def test_federation_judges_the_stored_completeness(tmp_path):
    ft.reset_store(FileStore(tmp_path / "kb"))
    stats = {"fetched": 40, "discovered": 40, "whole": None}

    ft._federate("x", "https://x.dev/api/", stats,
                 settled={"complete": None, "pages": 40, "stored_pages": 40,
                          "expected": 40})

    # A drained crawl is unknown in the store; the roll-up must say the same,
    # not recount it as complete.
    assert stats["federation"]["complete"] is None


def test_a_failed_selected_corpus_makes_the_rollup_incomplete(tmp_path, monkeypatch):
    from docsforge.core.engine import ForgeError

    ft.reset_store(FileStore(tmp_path / "kb"))
    monkeypatch.setattr(ft, "_identify_host", lambda name, url: (True, "names it"))

    def explodes(url, opts, fetcher=None, stats=None, sink=None):
        raise ForgeError("the site went away")

    monkeypatch.setattr(ft, "harvest", explodes)
    stats = {"corpora": [{"url": "https://api.x.dev/reference/",
                          "host": "api.x.dev", "votes": 42.0}],
             "fetched": 5, "discovered": 5, "whole": True}

    note = ft._federate("x", "https://x.dev/docs/", stats, intent="reference",
                        settled={"complete": True, "pages": 5, "stored_pages": 5,
                                 "expected": 5})

    assert "could not be harvested" in note
    if stats["federation"]["selected"] > 1:
        assert stats["federation"]["complete"] is False, \
            "a corpus that was wanted and not obtained read as covered"


def test_usable_for_planning_names_a_failed_corpus_plainly():
    from docsforge.core import federation, selection

    api = federation.Corpus(url="https://x.dev/api/")
    api.kind, api.selected, api.complete = "api", True, False
    api.status = "selected, but could not be harvested: timeout"
    sdk = federation.Corpus(url="https://x.dev/sdk/")
    sdk.kind, sdk.selected = "sdk", True
    sdk.settle(4, 4)
    ok, why = selection.usable_for_planning(
        selection.Selection(intent="resolve-import", selected=[api, sdk]), [api, sdk])

    assert ok is False
    assert "None" not in why and "could not be harvested" in why


# ── I4: an error after publishing does not deny the publish ─────
def test_a_federation_error_after_publishing_still_reports_the_stored_harvest(
        tmp_path, monkeypatch):
    ft.reset_store(FileStore(tmp_path / "kb"))

    def harvest(url, opts, fetcher=None, stats=None, sink=None):
        stats.update(discovered=1, whole=True)
        sink.add("Only", url + "only", "the only page")
        return [ft.Doc(url + "only", "Only", "")], "sitemap"

    def federate(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ft, "harvest", harvest)
    monkeypatch.setattr(ft, "_federate", federate)

    out = ft._harvest_now(url="https://f.dev/docs/", name="fed", version="v1", strict=True)

    assert ft.store().entry("fed", "v1")["pages"] == 1, "the entry corpus is stored"
    assert "Related corpora could not be checked" in out
    assert "usable_for_planning: false" in out
    assert "COVERAGE UNKNOWN" in out, "beyond the entry corpus, nothing is known"


# ── B2: Postgres ────────────────────────────────────────────────
def _pg_or_skip():
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


def test_a_second_postgres_writer_does_not_delete_the_first():
    store = _pg_or_skip()
    a = store.writer("integrity-a", "v1", "https://a.dev/", "crawl", expected=2)
    try:
        assert a.add("A1", "https://a.dev/1", "one")
        b = store.writer("integrity-a", "v1", "https://b.dev/", "crawl")
        b.close()
        assert a.add("A2", "https://a.dev/2", "two"), "the first writer's row was deleted"
        entry = a.settle(complete=True)
        assert entry["pages"] == 2 and entry["complete"] is True
        assert store.entry("integrity-a", "v1")["pages"] == 2
    finally:
        a.close()
        store.delete("integrity-a")


def test_a_postgres_settle_publishes_nothing_if_its_row_is_gone():
    store = _pg_or_skip()
    store.save("integrity-b", "v1", "https://x.dev/", "crawl", OLD[:2], complete=True)
    w = store.writer("integrity-b", "v1", "https://x.dev/", "crawl")
    try:
        w.add("New", "https://x.dev/new", "new")
        with store._connect() as cx:
            cx.execute("delete from doc_version where id = %s", (w.version_id,))
            cx.commit()
        with pytest.raises(StoreError):
            w.settle(complete=True)
        assert store.entry("integrity-b", "v1")["pages"] == 2, \
            "the published version was deleted by a writer that published nothing"
    finally:
        w.close()
        store.delete("integrity-b")


def test_two_postgres_settles_of_one_version_leave_exactly_one_ready():
    store = _pg_or_skip()
    a = store.writer("integrity-c", "v1", "https://a.dev/", "crawl")
    b = store.writer("integrity-c", "v1", "https://b.dev/", "crawl")
    try:
        a.add("A", "https://a.dev/a", "alpha")
        b.add("B1", "https://b.dev/1", "bravo")
        b.add("B2", "https://b.dev/2", "bravo")
        a.settle(complete=None)
        b.settle(complete=None)
        assert len(store.versions("integrity-c")) == 1
        assert [p["title"] for p in store.pages("integrity-c", "v1")] == ["B1", "B2"]
    finally:
        a.close()
        b.close()
        store.delete("integrity-c")


def test_both_stores_return_the_same_body(tmp_path):
    store = _pg_or_skip()
    files = FileStore(tmp_path / "kb")
    body = "one\r\ntwo\r\n\r\nthree"
    for s in (store, files):
        s.save("integrity-d", "v1", "https://x.dev/", "crawl",
               [("Ti\ntle", "https://x.dev/p", body)], complete=True, expected=1)
    try:
        assert store.page("integrity-d", "v1", 1) == files.page("integrity-d", "v1", 1)
    finally:
        store.delete("integrity-d")
