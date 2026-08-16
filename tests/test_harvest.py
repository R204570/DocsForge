"""Offline tests for crawl scoping and the knowledge base — no network."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docsforge as df
import forge_tools as ft
from kb_store import FileStore


# ── crawl scoping ────────────────────────────────────────
# Docs share a domain with marketing, blogs and podcasts. Crawling by host
# walked from an Effect docs page straight into /podcast, and the off-topic
# pages then dominated the truncated result the model saw.
@pytest.mark.parametrize("url,expected", [
    ("https://www.effect.website/docs/v3/getting-started/introduction/", "/docs/v3/"),
    ("https://www.effect.website/docs/v3", "/docs/v3/"),
    ("https://docs.python.org/3/library/json.html", "/3/library/"),
    ("https://x.dev/guide/setup", "/guide/"),
    # Stops at v2 rather than /reference/: mixing two versions of an API
    # reference into one harvest is the thing versioned storage exists to stop.
    ("https://x.dev/reference/api/v2/things", "/reference/api/v2/"),
    ("https://x.dev/documentation/v10/intro", "/documentation/v10/"),
    ("https://example.com/", "/"),
])
def test_docs_scope_anchors_on_the_documentation_root(url, expected):
    assert df.docs_scope(url) == expected


def test_scope_keeps_a_version_segment_but_not_a_word():
    assert df.docs_scope("https://x.dev/docs/v3/a/b") == "/docs/v3/"
    assert df.docs_scope("https://x.dev/docs/latest/a") == "/docs/"


def test_scope_finds_a_version_below_the_docs_root():
    # Pydantic files versions at /docs/validation/2.11/. Stopping at /docs/
    # crawls every version of the manual at once and calls it one harvest.
    assert (df.docs_scope("https://pydantic.dev/docs/validation/2.11/get-started/")
            == "/docs/validation/2.11/")
    assert (df.docs_scope("https://pydantic.dev/docs/validation/1.10/overview/")
            == "/docs/validation/1.10/")


def test_scope_does_not_chase_a_version_buried_deep_in_a_path():
    # A number five levels down is far more likely to be content than a
    # version root, and narrowing that far would miss most of the docs.
    assert df.docs_scope("https://x.dev/docs/a/b/c/d/2.0/deep") == "/docs/"


@pytest.mark.parametrize("link,ok", [
    ("https://www.effect.website/docs/v3/error-management/", True),
    ("https://www.effect.website/docs/v3", True),          # prefix without slash
    ("https://www.effect.website/podcast", False),         # the actual bug
    ("https://www.effect.website/blog", False),
    ("https://www.effect.website/", False),
    ("https://www.effect.website/docs/v2/intro", False),   # a different version
    ("https://other.com/docs/v3/x", False),                # different host
])
def test_crawlable_respects_the_section_prefix(link, ok):
    assert df._crawlable(link, "www.effect.website", "/docs/v3/") is ok


def test_crawlable_falls_back_to_whole_host():
    assert df._crawlable("https://x.com/anything", "x.com", "/") is True


def test_normalize_collapses_trailing_slash_and_fragment():
    # /intro/ and /intro were being fetched as two separate pages.
    assert df._normalize("https://x.com/a/intro/") == df._normalize("https://x.com/a/intro")
    assert df._normalize("https://x.com/a#frag") == "https://x.com/a"
    assert df._normalize("https://x.com/") == "https://x.com/"  # root keeps its slash


# ── combined output ──────────────────────────────────────
def test_combine_builds_contents_then_every_page():
    docs = [
        df.Doc("https://x.dev/docs/a", "Alpha", "<!-- source: x -->\n\nalpha body"),
        df.Doc("https://x.dev/docs/b", "Beta", "beta body"),
    ]
    out = df.combine(docs, "https://x.dev/docs/a", "sitemap")

    assert "## Contents" in out
    assert "1. [Alpha](https://x.dev/docs/a)" in out
    assert "## Alpha" in out and "## Beta" in out
    assert "alpha body" in out and "beta body" in out
    assert "2 pages" in out and "via: sitemap" in out
    # The per-page provenance comment is redundant once pages are combined.
    assert "<!-- source: x -->" not in out


# ── knowledge base ───────────────────────────────────────
@pytest.fixture
def kb(tmp_path):
    """Point the tools at a throwaway file store for the duration of a test."""
    ft.reset_store(FileStore(tmp_path))
    yield tmp_path
    ft.reset_store(None)


PAGES = [
    ("Error Handling", "https://x.dev/docs/errors", "fail fast"),
    ("Layers", "https://x.dev/docs/layers", "wiring with npm"),
]


def _store(kb, name="effect", pages=PAGES, complete=True, version="v3"):
    return ft.store().save(name, version, "https://x.dev/docs/", "crawl", pages,
                           complete=complete)


def test_empty_knowledge_base_says_what_to_do(kb):
    # It used to point at harvest_docs, which needs a URL the caller does not
    # have. An empty store should send a model to the tool it can actually use.
    empty = ft.tool_list_knowledge_base()
    assert "learn_technology" in empty
    assert "do not need a URL" in empty


def test_list_reports_what_is_stored(kb):
    _store(kb)
    out = ft.tool_list_knowledge_base()
    assert "effect" in out and "2 pages" in out


def test_read_returns_the_whole_document(kb):
    _store(kb)
    out = ft.tool_read_knowledge_base("effect")
    assert "Error Handling" in out and "Layers" in out


def test_read_section_returns_only_matching_pages(kb):
    _store(kb)
    out = ft.tool_read_knowledge_base("effect", section="error")
    assert "fail fast" in out
    assert "wiring" not in out, "a section lookup must not drag in unrelated pages"


def test_unknown_name_lists_what_is_available(kb):
    _store(kb)
    with pytest.raises(ft.ForgeError) as excinfo:
        ft.tool_read_knowledge_base("nope")
    assert "effect" in str(excinfo.value)


def test_unmatched_section_suggests_real_page_titles(kb):
    _store(kb)
    with pytest.raises(ft.ForgeError, match="Error Handling"):
        ft.tool_read_knowledge_base("effect", section="quantum tunnelling")


def test_missing_file_is_reported_not_crashed(kb):
    _store(kb)
    (kb / "effect" / "v3.md").unlink()
    with pytest.raises(ft.ForgeError, match="file is missing"):
        ft.tool_read_knowledge_base("effect")


def test_version_label_is_trusted_when_the_pages_carry_it():
    docs = [df.Doc(f"https://pydantic.dev/docs/validation/2.11/p{i}", f"P{i}", "x")
            for i in range(6)]
    assert ft._version_label(
        "https://pydantic.dev/docs/validation/2.11/get-started/", docs) == "2.11"


def test_version_label_falls_back_when_the_harvest_ignored_the_version():
    # A site-wide llms.txt is published once for the current release. Filing it
    # under the version the URL happened to name would claim a precision the
    # content does not have.
    docs = [df.Doc("https://pydantic.dev/llms.txt", "Everything", "x")]
    label = ft._version_label(
        "https://pydantic.dev/docs/validation/1.10/overview/", docs)
    assert label != "1.10"
    assert len(label) == 10, "an unverifiable version falls back to the harvest date"


def test_a_fallback_store_is_retried_rather_than_cached_forever(kb, monkeypatch):
    """A database that is slow to start must not downgrade the whole process.

    On Windows the Postgres service routinely finishes starting after the app
    does. Caching that first failed connection made every harvest ever taken
    look like it had vanished, for as long as the server stayed up.
    """
    from kb_store import FileStore

    down = FileStore(kb)
    down.degraded = "connection refused"
    down.wanted_dsn = "postgresql://nobody@127.0.0.1:1/none"

    class Fake:
        kind = "postgres"
        location = "127.0.0.1:5432/DocsForge"

    built = [down, Fake()]
    monkeypatch.setattr(ft, "build_store", lambda *a, **k: built.pop(0))

    ft.reset_store(None)
    assert ft.store() is down, "first build fell back, as the database was down"

    # Still inside the retry window: no second connection attempt.
    assert ft.store() is down

    monkeypatch.setattr(ft.time, "time", lambda: 10 ** 12)
    assert ft.store().kind == "postgres", "once it comes up, the store recovers"


def test_a_healthy_file_store_is_never_rebuilt(kb, monkeypatch):
    # Only a fallback is retried. Somebody with no database configured must not
    # pay for a rebuild on every single call.
    calls = []

    def build(*a, **k):
        calls.append(1)
        from kb_store import FileStore
        return FileStore(kb)

    monkeypatch.setattr(ft, "build_store", build)
    ft.reset_store(None)
    monkeypatch.setattr(ft.time, "time", lambda: 10 ** 12)
    ft.store(); ft.store(); ft.store()
    assert len(calls) == 1


def test_names_are_slugged_consistently():
    assert ft._kb_slug("Effect v3!") == "effect-v3"
    assert ft._kb_slug("") == "untitled"
    assert ft._kb_slug("A" * 200) == "a" * 64


@pytest.mark.parametrize("url,expected", [
    ("https://www.effect.website/docs/v3/x", "effect"),
    ("https://docs.python.org/3/", "python"),
    ("https://fastapi.tiangolo.com/", "fastapi"),
])
def test_name_defaults_to_the_project_not_the_www(url, expected):
    assert ft._name_from_url(url) == expected


# ── tool surface ─────────────────────────────────────────
def test_knowledge_base_tools_are_exposed():
    assert {"harvest_docs", "list_knowledge_base", "read_knowledge_base"} <= set(ft.BY_NAME)


def test_harvest_schema_defaults_to_section_scope():
    schema = ft.BY_NAME["harvest_docs"].schema
    assert schema["properties"]["scope"]["default"] == "section"
    assert schema["required"] == ["url"]


def test_list_knowledge_base_takes_no_arguments():
    assert ft.BY_NAME["list_knowledge_base"].schema["properties"] == {}


# ── truncation must never be silent ──────────────────────
# A 600-page manual harvested at max_pages=200 gave a third of the docs and
# said nothing, so answers were confidently based on a partial copy.
def test_crawl_reports_when_the_page_cap_cut_it_short():
    stats = {}

    class Stub:
        """Two pages that link to each other plus a third, so the queue is
        never empty when the cap is reached."""
        def html(self, url):
            return ('<html><head><title>P</title></head><body><main>'
                    + "body text " * 40
                    + '<a href="/docs/a">a</a><a href="/docs/b">b</a>'
                      '<a href="/docs/c">c</a></main></body></html>')

    opts = df.Options(crawl=True, max_pages=2, delay=0, verbose=False)
    df._crawl_html("https://x.dev/docs/start", Stub(), opts, stats)

    assert stats["fetched"] == 2
    assert stats["truncated"] is True
    assert stats["remaining"] >= 1


def test_crawl_reports_completion_when_it_runs_out_of_links():
    stats = {}

    class Stub:
        def html(self, url):
            return ('<html><head><title>Only</title></head><body><main>'
                    + "body text " * 40 + '</main></body></html>')

    opts = df.Options(crawl=True, max_pages=50, delay=0, verbose=False)
    df._crawl_html("https://x.dev/docs/start", Stub(), opts, stats)

    assert stats["fetched"] == 1
    assert stats["truncated"] is False


def test_incomplete_harvest_is_flagged_in_the_listing(kb):
    _store(kb, complete=False)
    assert "INCOMPLETE" in ft.tool_list_knowledge_base()


def test_complete_harvest_is_not_flagged(kb):
    _store(kb, complete=True)
    assert "INCOMPLETE" not in ft.tool_list_knowledge_base()


def test_incompleteness_follows_the_content_into_reads(kb):
    _store(kb, complete=False)
    out = ft.tool_read_knowledge_base("effect", section="error")
    assert "INCOMPLETE" in out, "a partial copy must say so at the point of use"


# ── search falls back from titles to content ─────────────
def test_section_prefers_a_title_match(kb):
    _store(kb)
    out = ft.tool_read_knowledge_base("effect", section="layers")
    assert "by title" in out
    assert "wiring" in out and "fail fast" not in out


def test_section_falls_back_to_searching_the_text(kb):
    _store(kb)
    # "npm" appears in a body, never in a heading.
    out = ft.tool_read_knowledge_base("effect", section="npm")
    assert "by content" in out
    assert "wiring with npm" in out


def test_section_reports_how_many_pages_matched(kb):
    _store(kb)
    assert "1 page matching" in ft.tool_read_knowledge_base("effect", section="layers")


# ── page splitting ───────────────────────────────────────
# Scraped pages contain their own "## " headings, so splitting a combined file
# on those alone reported a 30-page harvest as 167 pages -- and mis-counted
# every section lookup with it.
def test_split_pages_ignores_headings_inside_a_page():
    docs = [
        df.Doc("https://x.dev/docs/a", "Alpha",
               "intro\n\n## Inner Heading\n\nmore\n\n## Another Inner\n\nyet more"),
        df.Doc("https://x.dev/docs/b", "Beta", "beta body"),
    ]
    body = df.combine(docs, "https://x.dev/docs/a", "crawl")

    head, pages = ft.split_pages(body)
    assert len(pages) == 2, "inner ## headings must not count as pages"
    assert pages[0].startswith("## Alpha")
    assert pages[1].startswith("## Beta")
    assert "Inner Heading" in pages[0], "inner content stays with its page"
    assert "## Contents" in head


def test_split_pages_handles_a_file_with_no_pages():
    head, pages = ft.split_pages("# just a header\n\nnothing else")
    assert pages == []


def test_section_count_is_pages_not_blocks(kb):
    # One page whose own body contains "## " sub-headings.
    inner = "text\n\n## Sub One\n\na\n\n## Sub Two\n\nb"
    _store(kb, name="t", pages=[("Error Handling", "https://x.dev/docs/e", inner)])

    out = ft.tool_read_knowledge_base("t", section="error")
    assert "1 page matching" in out
    assert "Sub One" in out and "Sub Two" in out, "the whole page comes back, not one block"


# ── page caps ────────────────────────────────────────────
# A page count is a guess at how big someone else's documentation is. The
# scope prefix is the real boundary, so a harvest runs unlimited by default and
# max_pages=0 means "until the section is exhausted".
def test_limit_treats_zero_as_unlimited():
    assert df.Options(max_pages=0).limit() is None
    assert df.Options(max_pages=25).limit() == 25


def test_harvest_is_unlimited_by_default():
    assert ft.HARVEST_PAGE_CAP == 0
    opts = ft._options(crawl=True, max_pages=0, cap=ft.HARVEST_PAGE_CAP)
    assert opts.max_pages == 0 and opts.limit() is None


def test_harvest_still_honours_a_deliberate_limit():
    opts = ft._options(crawl=True, max_pages=50, cap=ft.HARVEST_PAGE_CAP)
    assert opts.limit() == 50


def test_a_plain_fetch_stays_bounded():
    # fetch_docs must not be able to start an open-ended crawl by accident.
    assert ft._options(max_pages=0).max_pages == ft.FETCH_PAGE_CAP
    assert ft._options(max_pages=99999).max_pages == ft.FETCH_PAGE_CAP
    assert ft._options(max_pages=10).max_pages == 10


def test_harvest_schema_advertises_unlimited():
    schema = ft.BY_NAME["harvest_docs"].schema["properties"]["max_pages"]
    assert schema["default"] == 0
    assert schema["minimum"] == 0
    assert "maximum" not in schema, "an arbitrary ceiling is exactly what was removed"


class _LinkedPages:
    """A finite docs section whose pages all link to each other."""

    def __init__(self, count=6):
        self.count = count

    def html(self, url):
        links = "".join(f'<a href="/docs/p{i}">p{i}</a>' for i in range(self.count))
        return ("<html><head><title>P</title></head><body><main>"
                + "body text " * 40 + links + "</main></body></html>")


def test_unlimited_crawl_stops_when_the_section_runs_out():
    stats = {}
    docs = df._crawl_html("https://x.dev/docs/start", _LinkedPages(),
                          df.Options(crawl=True, max_pages=0, delay=0, verbose=False), stats)
    assert len(docs) == 7                 # the start page plus its six links
    assert stats["truncated"] is False
    assert stats["remaining"] == 0


def test_a_limit_still_reports_what_it_skipped():
    stats = {}
    docs = df._crawl_html("https://x.dev/docs/start", _LinkedPages(),
                          df.Options(crawl=True, max_pages=3, delay=0, verbose=False), stats)
    assert len(docs) == 3
    assert stats["truncated"] is True
    assert stats["remaining"] == 4


def test_unlimited_does_not_empty_a_sitemap_slice():
    # `links[:0]` is empty, so an unlimited harvest must not go through a slice.
    opts = df.Options(max_pages=0)
    links = ["a", "b", "c"]
    cap = opts.limit()
    assert (links if cap is None else links[:cap]) == links
