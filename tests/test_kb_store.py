"""
Offline tests for the knowledge-base storage layer.

The file backend is tested everywhere. The Postgres backend is tested only when
DOCSFORGE_DB points at a reachable database, so the suite still passes on a
machine with no Postgres — but when one is available, both backends are held to
exactly the same behaviour.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docsforge as df
from kb_store import FileStore, PostgresStore, StoreError, build_store, parse_page, split_pages

PAGES = [
    ("Error Handling", "https://x.dev/docs/errors", "fail fast and recover"),
    ("Layers", "https://x.dev/docs/layers", "wiring services with npm install"),
    ("Generators", "https://x.dev/docs/gen", "yield* composition"),
]

# Deliberately NOT DOCSFORGE_DB: the suite must never reach for the database a
# developer actually stores harvests in. Opt in with a throwaway one instead.
DSN = os.environ.get("DOCSFORGE_TEST_DB", "")


def _pg_or_skip():
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


def _cleanup_pg():
    import psycopg

    with psycopg.connect(DSN) as cx:
        cx.execute("delete from technology where name like 'pytest-%'")
        cx.commit()


# ── parsing a combined file ──────────────────────────────
def test_parse_page_recovers_title_url_and_body():
    block = "## Error Handling\n\nSource: <https://x.dev/a>\n\nthe body"
    assert parse_page(block) == ("Error Handling", "https://x.dev/a", "the body")


def test_parse_page_survives_a_block_with_no_source_line():
    title, url, body = parse_page("## Orphan\n\nsome text")
    assert title == "Orphan" and url == ""
    assert "some text" in body


def test_round_trip_through_a_combined_file_keeps_every_page():
    # Migrating a file store into Postgres reads the pages back out of the
    # combined Markdown, so this round trip has to be lossless.
    docs = [df.Doc(u, t, b) for t, u, b in PAGES]
    combined = df.combine(docs, "https://x.dev/docs/", "crawl")
    _, blocks = split_pages(combined)
    recovered = [parse_page(b) for b in blocks]

    assert [t for t, _, _ in recovered] == [t for t, _, _ in PAGES]
    assert [u for _, u, _ in recovered] == [u for _, u, _ in PAGES]
    for (_, _, before), (_, _, after) in zip(PAGES, recovered):
        assert before in after


# ── backend selection ────────────────────────────────────
def test_files_are_used_when_no_database_is_configured(tmp_path):
    assert build_store(root=tmp_path, dsn="").kind == "files"


def test_an_unreachable_database_falls_back_to_files(tmp_path):
    # Losing a harvest because a database is down would be much worse than
    # quietly writing it to disk.
    store = build_store(root=tmp_path, dsn="postgresql://nobody@127.0.0.1:1/none")
    assert store.kind == "files"


# ── behaviour both backends must share ───────────────────
@pytest.fixture(params=["files", "postgres"])
def store(request, tmp_path):
    if request.param == "files":
        yield FileStore(tmp_path)
        return
    pg = _pg_or_skip()
    yield pg
    _cleanup_pg()


def _save(store, name="pytest-demo", complete=True):
    return store.save(name, "https://x.dev/docs/", "crawl", PAGES, complete=complete)


def test_save_then_list(store):
    entry = _save(store)
    assert entry["pages"] == 3
    assert "pytest-demo" in [e["name"] for e in store.entries()]


def test_entry_reports_completeness(store):
    _save(store, complete=False)
    assert store.entry("pytest-demo")["complete"] is False


def test_read_everything(store):
    _save(store)
    body, how, _ = store.read("pytest-demo")
    assert how == "all"
    for title, _, text in PAGES:
        assert title in body and text in body


def test_section_matches_a_title_first(store):
    _save(store)
    body, how, count = store.read("pytest-demo", "layers")
    assert how == "title" and count == 1
    assert "wiring services" in body
    assert "fail fast" not in body, "a title hit must not drag in other pages"


def test_section_falls_back_to_the_page_text(store):
    _save(store)
    body, how, _ = store.read("pytest-demo", "npm")
    assert how == "content"
    assert "wiring services with npm" in body


def test_unknown_technology_is_refused(store):
    with pytest.raises(StoreError, match="no stored documentation"):
        store.read("pytest-missing")


def test_unmatched_section_is_refused(store):
    _save(store)
    with pytest.raises(StoreError, match="matches"):
        store.read("pytest-demo", "quantum tunnelling")


def test_re_saving_replaces_rather_than_duplicates(store):
    _save(store)
    _save(store)
    matching = [e for e in store.entries() if e["name"] == "pytest-demo"]
    assert len(matching) == 1, "a re-harvest must replace the old copy"
    assert matching[0]["pages"] == 3


# ── postgres specifics ───────────────────────────────────
def test_postgres_ranks_content_matches():
    store = _pg_or_skip()
    try:
        store.save("pytest-rank", "https://x.dev/docs/", "crawl", [
            ("Unrelated", "https://x.dev/1", "nothing to see here"),
            ("Also Unrelated", "https://x.dev/2", "retry retry retry backoff retry"),
        ], complete=True)
        body, how, count = store.read("pytest-rank", "retry backoff")
        assert how == "content" and count == 1
        assert "retry retry retry" in body
    finally:
        _cleanup_pg()


def test_postgres_schema_is_idempotent():
    store = _pg_or_skip()
    store._ready = False
    store.migrate()  # running the DDL twice must not raise
    store._ready = False
    store.migrate()
