"""
Offline tests for the knowledge-base storage layer.

The file backend is tested everywhere. The Postgres backend is tested only when
DOCSFORGE_TEST_DB points at a reachable database, so the suite still passes on a
machine with no Postgres — but when one is available, both backends are held to
exactly the same behaviour.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docsforge as df
from kb_store import (
    FileStore, PostgresStore, StoreError, build_store, parse_page, split_pages,
    version_from_url,
)

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


# ── naming the version ───────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.effect.website/docs/v3/getting-started/", "v3"),
    ("https://pydantic.dev/docs/validation/2.11/get-started/", "2.11"),
    ("https://docs.python.org/3.12/library/json.html", "3.12"),
    ("https://x.dev/docs/latest/intro", "latest"),
])
def test_version_is_read_out_of_the_url(url, expected):
    assert version_from_url(url) == expected


def test_an_unversioned_site_is_labelled_with_the_harvest_date():
    # A site that publishes one version at a time has no version to read, and
    # the date is the only honest thing to call the snapshot.
    label = version_from_url("https://hono.dev/docs/")
    assert len(label) == 10 and label.count("-") == 2


def test_a_version_looking_segment_does_not_match_an_ordinary_word():
    assert version_from_url("https://x.dev/docs/guide/install") != "guide"


# ── reading a knowledge base written before versions existed ──
V1_INDEX = {
    "effect": {
        "name": "effect",
        "source": "https://www.effect.website/docs/v3/getting-started/introduction/",
        "strategy": "crawl", "complete": True, "pages": 2, "characters": 120,
        "harvested": "2026-08-13 22:10", "titles": ["Introduction", "Layers"],
    },
}


def _write_v1(root, body="## Introduction\n\nSource: <https://x.dev/a>\n\nintro text\n"):
    import json as _json

    entry = dict(V1_INDEX["effect"])
    md = root / "effect.md"
    md.write_text(f"# effect documentation\n\n{body}", encoding="utf-8")
    entry["file"] = str(md)
    (root / "index.json").write_text(_json.dumps({"effect": entry}), encoding="utf-8")


def test_a_pre_versioning_index_is_read_rather_than_crashing(tmp_path):
    # v1 keyed entries by technology and stored `name`. Postgres got a
    # migration for this and the file store did not, so an older
    # knowledge_base raised KeyError: 'technology' on the very first read.
    _write_v1(tmp_path)
    store = FileStore(tmp_path)

    techs, total = store.technologies()
    assert total == 1
    assert techs[0]["name"] == "effect"
    assert techs[0]["versions"] == 1

    # The version is recovered from the URL the harvest came from.
    assert [v["version"] for v in store.versions("effect")] == ["v3"]


def test_upgrading_an_old_index_keeps_the_markdown_where_it_is(tmp_path):
    _write_v1(tmp_path)
    store = FileStore(tmp_path)
    body, how, _ = store.read("effect")
    assert how == "all" and "intro text" in body
    assert (tmp_path / "effect.md").exists(), "the old file must not be orphaned"


def test_the_upgrade_is_written_back_once(tmp_path):
    import json as _json

    _write_v1(tmp_path)
    FileStore(tmp_path).technologies()
    on_disk = _json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["effect@v3"]
    assert on_disk["effect@v3"]["technology"] == "effect"
    assert "name" not in on_disk["effect@v3"]


# ── backend selection ────────────────────────────────────
def test_files_are_used_when_no_database_is_configured(tmp_path):
    store = build_store(root=tmp_path, dsn="")
    assert store.kind == "files"
    assert not store.degraded, "no database was asked for, so nothing is degraded"


def test_an_unreachable_database_falls_back_to_files(tmp_path):
    # Losing a harvest because a database is down would be much worse than
    # quietly writing it to disk.
    store = build_store(root=tmp_path, dsn="postgresql://nobody@127.0.0.1:1/none")
    assert store.kind == "files"


def test_a_fallback_says_it_is_a_fallback(tmp_path):
    # Falling back silently means everything ever harvested appears to have
    # vanished, with the interface calmly reporting an empty store.
    store = build_store(root=tmp_path, dsn="postgresql://nobody@127.0.0.1:1/none")
    assert store.degraded, "the fallback must carry a reason"
    assert store.wanted_dsn, "and the DSN worth retrying"


# ── behaviour both backends must share ───────────────────
@pytest.fixture(params=["files", "postgres"])
def store(request, tmp_path):
    if request.param == "files":
        yield FileStore(tmp_path)
        return
    pg = _pg_or_skip()
    _cleanup_pg()
    yield pg
    _cleanup_pg()


def _save(store, name="pytest-demo", version="v1", complete=True, pages=None):
    return store.save(name, version, "https://x.dev/docs/", "crawl",
                      pages if pages is not None else PAGES, complete=complete)


def test_save_then_list(store):
    entry = _save(store)
    assert entry["pages"] == 3
    techs, total = store.technologies()
    assert total == 1
    assert techs[0]["name"] == "pytest-demo"
    assert techs[0]["versions"] == 1


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


# ── version tracking ─────────────────────────────────────
def test_two_versions_of_one_technology_live_side_by_side(store):
    _save(store, version="v2", pages=PAGES[:1])
    _save(store, version="v3", pages=PAGES)

    techs, total = store.technologies()
    assert total == 1, "two versions are still one technology"
    assert techs[0]["versions"] == 2
    assert techs[0]["pages"] == 4

    labels = {v["version"] for v in store.versions("pytest-demo")}
    assert labels == {"v2", "v3"}


def test_each_version_keeps_its_own_pages(store):
    _save(store, version="v2", pages=[("Old Way", "https://x.dev/old", "deprecated")])
    _save(store, version="v3", pages=PAGES)

    v2, _, _ = store.read("pytest-demo", version="v2")
    assert "deprecated" in v2
    assert "fail fast" not in v2, "v3 content must not leak into a v2 read"

    v3, _, _ = store.read("pytest-demo", version="v3")
    assert "fail fast" in v3


def test_re_harvesting_a_version_replaces_only_that_version(store):
    _save(store, version="v2", pages=[("Old Way", "https://x.dev/old", "deprecated")])
    _save(store, version="v3", pages=PAGES)
    _save(store, version="v3", pages=PAGES[:2])   # crawl it again

    assert len(store.versions("pytest-demo")) == 2
    assert store.entry("pytest-demo", "v3")["pages"] == 2
    assert store.entry("pytest-demo", "v2")["pages"] == 1


def test_reading_without_a_version_gets_the_newest(store):
    _save(store, version="old", pages=[("Old Way", "https://x.dev/old", "deprecated")])
    _save(store, version="new", pages=PAGES)
    assert store.entry("pytest-demo")["version"] == "new"


def test_the_newest_version_is_not_the_newest_harvest(store):
    """The measured F5 failure.

    Pydantic 2.11 was crawled first and 1.10 second, so every unqualified read
    returned the older major — 24 pages of 1.10 instead of 85 of 2.11 — while
    this repository's own requirements.txt pins pydantic>=2.0. Both versions
    were stored correctly; the last step picked the wrong one.
    """
    _save(store, version="2.11", pages=PAGES)                       # harvested first
    _save(store, version="1.10", pages=[("Old", "https://x.dev/o", "old")])

    assert store.entry("pytest-demo")["version"] == "2.11"
    assert [v["version"] for v in store.versions("pytest-demo")] == ["2.11", "1.10"]

    techs, _ = store.technologies()
    assert [t for t in techs if t["name"] == "pytest-demo"][0]["latest"] == "2.11"

    # The read path has its own version lookup, and it was the one that
    # actually served read_knowledge_base.
    _, _, blocks = store.read("pytest-demo")
    assert blocks == len(PAGES)


def test_a_release_number_outranks_a_harvest_date(store):
    """A date label only appears when we failed to find a version, so it must
    never outrank a version we did find."""
    _save(store, version="2.11", pages=PAGES)
    _save(store, version="2026-08-20", pages=[("Dated", "https://x.dev/d", "x")])
    assert store.entry("pytest-demo")["version"] == "2.11"


def test_completeness_can_be_unknown(store):
    """`None` is not `True`. A copy nobody measured must not report itself
    whole — that is the defect the flag existed to warn about."""
    _save(store, version="v1", complete=None)
    assert store.entry("pytest-demo")["complete"] is None
    techs, _ = store.technologies()
    assert [t for t in techs if t["name"] == "pytest-demo"][0]["complete"] is None


def test_one_partial_version_makes_the_technology_partial(store):
    _save(store, version="v1", complete=True)
    _save(store, version="v2", complete=False)
    techs, _ = store.technologies()
    assert [t for t in techs if t["name"] == "pytest-demo"][0]["complete"] is False


def test_an_unknown_version_is_refused(store):
    _save(store, version="v3")
    assert store.entry("pytest-demo", "v9") is None
    with pytest.raises(StoreError, match="v9"):
        store.read("pytest-demo", version="v9")


def test_pages_are_listed_in_order_and_readable_one_at_a_time(store):
    _save(store, version="v3")
    listing = store.pages("pytest-demo", "v3")
    assert [p["ordinal"] for p in listing] == [1, 2, 3]
    assert [p["title"] for p in listing] == [t for t, _, _ in PAGES]

    page = store.page("pytest-demo", "v3", 2)
    assert page["title"] == "Layers"
    assert page["url"] == "https://x.dev/docs/layers"
    assert "wiring services" in page["content"]


def test_asking_for_a_page_that_does_not_exist_is_refused(store):
    _save(store, version="v3")
    with pytest.raises(StoreError, match="page 99"):
        store.page("pytest-demo", "v3", 99)


# ── paging the box ───────────────────────────────────────
def test_technologies_are_paged(store):
    for i in range(5):
        _save(store, name=f"pytest-t{i}", pages=PAGES[:1])

    first, total = store.technologies(offset=0, limit=2)
    assert total == 5, "the total counts everything, not just this page"
    assert [t["name"] for t in first] == ["pytest-t0", "pytest-t1"]

    last, _ = store.technologies(offset=4, limit=2)
    assert [t["name"] for t in last] == ["pytest-t4"]

    everything, _ = store.technologies()
    assert len(everything) == 5, "no limit means the whole box"


def test_technologies_can_be_filtered_by_name(store):
    _save(store, name="pytest-effect", pages=PAGES[:1])
    _save(store, name="pytest-zod", pages=PAGES[:1])

    hits, total = store.technologies(query="zod")
    assert total == 1 and hits[0]["name"] == "pytest-zod"


def test_search_finds_pages_and_says_where_they_live(store):
    _save(store, name="pytest-demo", version="v3")
    hits = store.search("npm")
    assert hits, "the page mentioning npm should be found"
    assert hits[0]["technology"] == "pytest-demo"
    assert hits[0]["version"] == "v3"
    assert hits[0]["ordinal"] == 2


def test_search_can_be_scoped_to_one_version(store):
    _save(store, version="v2", pages=[("Old", "https://x.dev/old", "npm install old")])
    _save(store, version="v3", pages=PAGES)

    scoped = store.search("npm", tech="pytest-demo", version="v2")
    assert scoped and all(h["version"] == "v2" for h in scoped)


def test_deleting_one_version_leaves_the_others(store):
    _save(store, version="v2", pages=PAGES[:1])
    _save(store, version="v3", pages=PAGES)

    store.delete("pytest-demo", "v2")
    remaining = store.versions("pytest-demo")
    assert [v["version"] for v in remaining] == ["v3"]

    store.delete("pytest-demo")
    assert store.technologies()[1] == 0


# ── postgres specifics ───────────────────────────────────
def test_postgres_ranks_content_matches():
    store = _pg_or_skip()
    try:
        store.save("pytest-rank", "v1", "https://x.dev/docs/", "crawl", [
            ("Unrelated", "https://x.dev/1", "nothing to see here"),
            ("Also Unrelated", "https://x.dev/2", "retry retry retry backoff retry"),
        ], complete=True)
        body, how, count = store.read("pytest-rank", "retry backoff")
        assert how == "content" and count == 1
        assert "retry retry retry" in body
    finally:
        _cleanup_pg()


def test_postgres_search_marks_the_matching_words():
    store = _pg_or_skip()
    try:
        store.save("pytest-mark", "v1", "https://x.dev/docs/", "crawl", [
            ("Retrying", "https://x.dev/1",
             "use a schedule to retry the effect with exponential backoff"),
        ], complete=True)
        hits = store.search("exponential backoff", tech="pytest-mark")
        assert hits
        # The snippet marks matches with guillemets rather than markup, so the
        # page's own angle brackets can never become HTML in the browser.
        assert "«" in hits[0]["snippet"] and "»" in hits[0]["snippet"]
        assert "<" not in hits[0]["snippet"]
    finally:
        _cleanup_pg()


def test_postgres_schema_is_idempotent():
    store = _pg_or_skip()
    store._ready = False
    store.migrate()  # running the DDL twice must not raise
    store._ready = False
    store.migrate()
