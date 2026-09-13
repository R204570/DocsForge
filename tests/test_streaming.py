"""
Tests for PROPOSAL-3 Phase 1 — storage that streams.

Two invariants carry this phase, and both were paid for in a real failure:
`go.dev` crawled for sixteen minutes, met one 1.19 MB page, and stored none of
the ~1,200 that had extracted cleanly.

  * **Invariant 16** — a page is durable before the next is fetched.
  * **Invariant 17** — a failed page costs one page. Never the batch, never the
    harvest, and never the version already stored.

The Postgres tests are the ones that matter for Invariant 17, because the file
store refuses nothing. They are gated on `DOCSFORGE_TEST_DB` like the rest.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.store import kb_store
from docsforge.store.kb_store import FileStore, PostgresStore

PAGES = [
    ("Error Handling", "https://x.dev/docs/errors", "fail fast and recover"),
    ("Layers", "https://x.dev/docs/layers", "wiring services together"),
]

DSN = os.environ.get("DOCSFORGE_TEST_DB", "")


def _pg_or_skip():
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


@pytest.fixture
def files(tmp_path):
    return FileStore(tmp_path)


# ── one write path ───────────────────────────────────────
def test_save_goes_through_the_writer():
    # Wiring, not behaviour: a batch save and a streamed one must be the same
    # code, or the one nobody runs in tests quietly rots.
    source = open(kb_store.__file__, encoding="utf-8").read()
    assert source.count("with self.writer(") >= 2, \
        "save() should delegate to writer() in both stores"


def test_both_stores_offer_a_writer():
    assert hasattr(FileStore, "writer") and hasattr(PostgresStore, "writer")


# ── Invariant 16: durable before the next page ───────────
def test_a_page_is_on_disk_before_the_harvest_settles(files):
    w = files.writer("effect", "v3", "https://x.dev/docs/", "crawl", expected=2)
    w.add(*PAGES[0])

    # Written and flushed, not buffered until the end.
    assert w.partial.exists()
    assert "fail fast and recover" in w.partial.read_text(encoding="utf-8")

    w.add(*PAGES[1])
    w.settle(complete=True)
    assert not w.partial.exists(), "the partial should be cleaned up on settle"


def test_a_streamed_harvest_reads_back_identically(files):
    streamed = files.writer("effect", "v3", "https://x.dev/docs/", "crawl", expected=2)
    for page in PAGES:
        streamed.add(*page)
    streamed.settle(complete=True)
    one = files.read("effect")[0]

    other = FileStore(files.root.parent / "batch")
    other.save("effect", "v3", "https://x.dev/docs/", "crawl", PAGES, complete=True)
    two = other.read("effect")[0]

    # The header carries a timestamp to the minute; everything else must match.
    assert one.replace("\r\n", "\n") == two.replace("\r\n", "\n")


# ── Invariant 17: the stored version survives a failure ──
def test_abandoning_a_harvest_leaves_the_stored_one_alone(files):
    files.save("effect", "v3", "https://x.dev/docs/", "crawl", PAGES, complete=True)
    before = files.read("effect")[0]

    # A second harvest of the same version that never settles.
    with files.writer("effect", "v3", "https://x.dev/docs/", "crawl") as w:
        w.add("Half a page", "https://x.dev/docs/half", "interrupted")
        # falling out of the block without settle() abandons it

    assert files.read("effect")[0] == before
    assert files.entry("effect", "v3")["pages"] == 2


def test_an_abandoned_harvest_leaves_no_partial_behind(files):
    with files.writer("effect", "v3", "https://x.dev/docs/", "crawl") as w:
        w.add(*PAGES[0])
        partial = w.partial
        assert partial.exists()
    assert not partial.exists()


# ── the failure that started all this ────────────────────
#: The ceiling is on the *tsvector*, not on the text. A megabyte of one
#: repeated word compresses to a single lexeme and sails through; it takes a
#: large vocabulary to build a tsvector over 1 MB. Worth knowing, because "the
#: page was 1.19 MB" is necessary but not sufficient to explain the original
#: failure — it was 1.19 MB of *distinct* words.
def _oversized() -> str:
    return " ".join(f"lexeme{i}" for i in range(150_000))


def test_the_page_that_broke_go_dev_is_now_stored():
    # Phase 1 made this page cost one page instead of the whole harvest. Phase
    # 2 makes it cost nothing: `page.search` is generated over a bounded prefix,
    # so the 1 MB tsvector ceiling stops being a storage limit.
    store = _pg_or_skip()
    huge = _oversized()

    with store.writer("bigdocs", "v1", "https://x.dev/", "crawl", expected=3) as w:
        assert w.add("Fine", "https://x.dev/a", "a small page") is True
        assert w.add("Enormous", "https://x.dev/big", huge) is True
        assert w.add("Also fine", "https://x.dev/b", "another small page") is True
        entry = w.settle(complete=True)

    assert entry["pages"] == 3, "every page should be stored"
    assert entry["rejected"] == []
    # And stored whole, not truncated: the bound is on the index, not the page.
    stored = store.read("bigdocs")[0]
    assert "lexeme149999" in stored
    store.delete("bigdocs")


def test_the_tail_of_an_oversized_page_is_still_searchable():
    # Bounding the index without this would store the page and quietly drop
    # most of it out of search — an undisclosed subset of the index, which is
    # the same class of dishonesty as an undisclosed subset of the corpus.
    store = _pg_or_skip()
    head = " ".join(f"lexeme{i}" for i in range(120_000))
    body = f"# Spec\n\n{head}\n\n## Deprecations\n\nthe zzzunique marker\n"

    with store.writer("tail", "v1", "https://x.dev/", "crawl", expected=1) as w:
        assert w.add("Spec", "https://x.dev/spec", body) is True
        w.settle(complete=True)

    assert len(body) > kb_store.INDEX_CHARS, "fixture must exceed the bound"
    with store._connect() as cx:
        found = cx.execute(
            "select count(*) from section s join page p on p.id = s.page_id "
            "join doc_version v on v.id = p.version_id "
            "join technology t on t.id = v.technology_id "
            "where t.name = %s and s.search @@ "
            "      websearch_to_tsquery('english', 'zzzunique')",
            ("tail",)).fetchone()[0]
    assert found >= 1, "the tail past the index bound should still be reachable"
    store.delete("tail")


def test_an_ordinary_page_is_not_split():
    # Splitting every page would double the store to buy nothing.
    store = _pg_or_skip()
    with store.writer("ordinary", "v1", "https://x.dev/", "crawl", expected=1) as w:
        w.add("Small", "https://x.dev/s", "## A\n\nshort\n\n## B\n\nalso short")
        w.settle(complete=True)

    with store._connect() as cx:
        rows = cx.execute(
            "select count(*) from section s join page p on p.id = s.page_id "
            "join doc_version v on v.id = p.version_id "
            "join technology t on t.id = v.technology_id where t.name = %s",
            ("ordinary",)).fetchone()[0]
    assert rows == 0
    store.delete("ordinary")


def test_the_index_is_bounded_in_the_schema_itself():
    # Not a behaviour test: a claim about the column definition, because the
    # column is GENERATED and so the bound is what makes the page storable at
    # all. If someone re-widens it, every one of the tests above still passes
    # until a 1.19 MB page turns up in production again.
    store = _pg_or_skip()
    store.migrate()
    with store._connect() as cx:
        expr = cx.execute(
            "select generation_expression from information_schema.columns "
            "where table_name = 'page' and column_name = 'search'").fetchone()[0]
    # Postgres renders the reserved word `left` quoted, so this matches on the
    # bound rather than the function name.
    assert str(kb_store.INDEX_CHARS) in expr, expr


def test_the_rejection_reason_does_not_blame_the_database():
    # The raw driver message is "string is too long for tsvector", which a
    # reader hears as "the documentation is too big for the database" and then
    # goes looking for a smaller subset to harvest. It is neither.
    class Boom(Exception):
        pass

    why = kb_store._PgWriter._why(Boom("string is too long for tsvector"))
    assert "1 MB" in why and "page" in why
    assert "too large for the current database" not in why.lower()


def test_an_interrupted_postgres_harvest_keeps_the_published_version():
    store = _pg_or_skip()
    store.save("interrupted", "v1", "https://x.dev/", "crawl", PAGES, complete=True)
    assert store.entry("interrupted", "v1")["pages"] == 2

    with store.writer("interrupted", "v1", "https://x.dev/", "crawl") as w:
        w.add("Partial", "https://x.dev/partial", "half a harvest")
        # abandoned without settle

    # The published version is untouched, and the abandoned one is invisible.
    assert store.entry("interrupted", "v1")["pages"] == 2
    assert len(store.versions("interrupted")) == 1
    store.delete("interrupted")


def test_a_harvest_in_progress_is_not_visible_to_readers():
    store = _pg_or_skip()
    w = store.writer("inflight", "v1", "https://x.dev/", "crawl", expected=2)
    try:
        w.add(*PAGES[0])
        # Streamed and durable, but not yet published: nothing can read it, and
        # it does not appear in a listing as though it were stored.
        assert store.entry("inflight", "v1") is None
        names = [t["name"] for t in store.technologies()[0]]
        assert "inflight" not in names
    finally:
        w.close()
        store.delete("inflight")


def test_settling_replaces_the_previous_version():
    store = _pg_or_skip()
    store.save("replaced", "v1", "https://x.dev/", "crawl", PAGES, complete=True)

    with store.writer("replaced", "v1", "https://x.dev/", "crawl", expected=1) as w:
        w.add("Only page", "https://x.dev/only", "the new harvest")
        w.settle(complete=True)

    assert store.entry("replaced", "v1")["pages"] == 1
    assert len(store.versions("replaced")) == 1, "no duplicate version rows"
    store.delete("replaced")


# ── wiring: the pipeline must actually use the writer ────
def _forge_tools_source() -> str:
    return open(ft.__file__, encoding="utf-8").read()


def test_the_harvest_pipeline_streams_into_a_writer():
    # The lesson from PROPOSAL-II: a passing test proves a function behaves,
    # not that anything calls it. Four features shipped built, tested and
    # unreachable. This asserts reachability, mechanically.
    source = _forge_tools_source()
    assert "store().writer(" in source, "the harvest never opens a writer"
    assert "sink=" in source, "pages are not streamed into the store"
    assert "store().save(" not in source, \
        "a harvest still batches into save() instead of streaming"


def test_both_harvest_paths_stream():
    # tool_harvest_docs and _harvest_corpus, so a federated corpus gets the
    # same durability as the entry one.
    assert _forge_tools_source().count("store().writer(") >= 2


def test_pages_are_released_rather_than_carried_to_the_end():
    # Peak memory should be the number of pages, not their total size: the
    # crawl hands each body to the sink and keeps only its shape.
    from docsforge.core import engine
    crawler = open(engine.__file__, encoding="utf-8").read()
    assert 'out.append(Doc(url, title, ""))' in crawler, \
        "the crawl still carries page bodies to the end of the harvest"


def test_bounding_the_index_happens_once_not_every_startup():
    # Found while building this: the guard looked for `left(`, but Postgres
    # renders the reserved word quoted as `"left"(`, so the guard never matched
    # and every migrate() dropped and rebuilt a generated column — a full table
    # rewrite plus a GIN index rebuild, on every process start, silently.
    #
    # A dropped-and-re-added column gets a fresh attnum, so comparing it across
    # two migrations detects a rebuild that no behavioural test would notice.
    store = _pg_or_skip()
    store.migrate()

    def attnum():
        with store._connect() as cx:
            return cx.execute(
                "select attnum from pg_attribute "
                "where attrelid = 'page'::regclass and attname = 'search'"
            ).fetchone()[0]

    before = attnum()
    store._ready = False            # force migrate() past its in-process guard
    store.migrate()
    assert attnum() == before, "the search column was rebuilt a second time"


def test_search_reaches_the_tail_of_an_oversized_page():
    # The section table existed and was populated for a whole phase before
    # anything read it — which is precisely the defect the whole W-series of
    # ISSUES.md is about, committed while closing it. Writing an index nobody
    # queries is the same as not writing it: bounding `page.search` would have
    # traded a page that could not be stored for a page stored whole and
    # findable only by its opening.
    store = _pg_or_skip()
    head = " ".join(f"lexeme{i}" for i in range(120_000))
    body = f"# Spec\n\n{head}\n\n## Deprecations\n\nthe zzzmarker clause\n"
    assert len(body) > kb_store.INDEX_CHARS

    with store.writer("searchtail", "v1", "https://x.dev/", "crawl", expected=1) as w:
        w.add("Spec", "https://x.dev/spec", body)
        w.settle(complete=True)

    hits = store.search("zzzmarker", tech="searchtail")
    assert hits, "a term past the index bound should still be findable"
    assert hits[0]["url"] == "https://x.dev/spec"
    assert "zzzmarker" in hits[0]["snippet"].lower()
    store.delete("searchtail")


def test_a_page_matching_twice_is_returned_once():
    # A long page can match through its own index and through a section of it.
    store = _pg_or_skip()
    head = " ".join(f"lexeme{i}" for i in range(120_000))
    body = f"# Spec\n\nzzzboth {head}\n\n## More\n\nzzzboth again here\n"

    with store.writer("twice", "v1", "https://x.dev/", "crawl", expected=1) as w:
        w.add("Spec", "https://x.dev/spec", body)
        w.settle(complete=True)

    hits = store.search("zzzboth", tech="twice")
    assert len(hits) == 1, "one page, one result"
    store.delete("twice")


def test_ordinary_search_still_works():
    store = _pg_or_skip()
    store.save("plain", "v1", "https://x.dev/", "crawl", PAGES, complete=True)
    hits = store.search("recover", tech="plain")
    assert hits and hits[0]["title"] == "Error Handling"
    store.delete("plain")
