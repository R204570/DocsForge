"""Offline tests for crawl scoping and the knowledge base — no network."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.tools import forge_tools as ft
from docsforge.store.kb_store import FileStore


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
    from docsforge.store.kb_store import FileStore

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
        from docsforge.store.kb_store import FileStore
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


# ── the corpus shape is measured from what was stored ───────────────────────

class _CollectingWriter:
    """A writer that keeps everything, like the real one from the sink's view."""

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.pages = []

    def add(self, title, url, body):
        if self.refuse:
            return False
        self.pages.append((title, url, body))
        return True


def test_the_sink_keeps_the_size_of_every_page_it_stored():
    sink = ft._StripSink(_CollectingWriter())
    sink.add("A", "https://x.dev/a", "<!-- source: x | type: html -->\n\n" + "z" * 5000)
    sink.add("B", "https://x.dev/b", "y" * 3000)
    assert sink.sizes == [5000, 3000], "the provenance comment is not the page"


def test_a_page_the_store_refused_is_not_counted():
    sink = ft._StripSink(_CollectingWriter(refuse=True))
    sink.add("A", "https://x.dev/a", "z" * 5000)
    assert sink.sizes == [], "a page that was not stored has no stored size"


def test_the_corpus_shape_is_measured_from_what_was_stored():
    """`_drain` returns `Doc(url, title, "")` so peak memory stays proportional
    to page count, and the shape note was measured over those emptied
    documents. Every median was therefore zero, and every harvest of twenty
    pages or more was told it was "the shape of an API symbol index or a split
    dump, not prose documentation". Measured live on 2026-09-10: langchain's
    627 pages, 8,956,185 characters -- 14,283 per page -- reported "a median of
    0 characters and 627 of them are under 500"."""
    from docsforge.core import llmsfinder

    emptied = [df.Doc(f"https://x.dev/{i}", f"P{i}", "") for i in range(30)]
    assert llmsfinder.density_note([len(d.markdown) for d in emptied]), (
        "the defect: a corpus whose bodies were released always reads as stubs")

    sink = ft._StripSink(_CollectingWriter())
    for i in range(30):
        sink.add(f"P{i}", f"https://x.dev/{i}", "prose " * 500)
    assert not llmsfinder.density_note(sink.sizes), (
        "measured from what was stored, prose documentation is not a set of "
        "stubs")


def test_a_corpus_that_really_is_stubs_still_says_so():
    """The note has to keep working, or fixing it just removes it."""
    from docsforge.core import llmsfinder

    sink = ft._StripSink(_CollectingWriter())
    for i in range(30):
        sink.add(f"P{i}", f"https://x.dev/{i}", "attr: str")
    note = llmsfinder.density_note(sink.sizes)
    assert note and "median of 9 characters" in note


# ── one language, chosen at the sitemap index too ───────────────────────────

def test_a_per_language_sitemap_index_keeps_the_default_language():
    """`docs.djangoproject.com/sitemap.xml` is twelve per-language children and
    `sitemap-el.xml` sorts first, so a capped harvest filled up on 1,077 Greek
    URLs and never opened `sitemap-en.xml`. The per-URL locale filter then saw
    one language and had nothing to choose between."""
    kids = [f"https://d.dev/sitemap-{code}.xml" for code in
            ("el", "en", "es", "fr", "id", "it", "ja", "ko", "pl", "pt-br",
             "sv", "zh-hans")]
    assert df._prefer_default_locale(kids) == ["https://d.dev/sitemap-en.xml"]


def test_a_numbered_or_named_sitemap_index_is_left_whole():
    """Two letters, so a paged or sectioned index is not mistaken for a
    translation and silently truncated to one child."""
    numbered = ["https://d.dev/sitemap-1.xml", "https://d.dev/sitemap-2.xml"]
    named = ["https://d.dev/sitemap-posts.xml", "https://d.dev/sitemap-docs.xml"]
    assert df._prefer_default_locale(numbered) == numbered
    assert df._prefer_default_locale(named) == named


# ── a documentation host has no marketing to drop ───────────────────────────

def test_a_documentation_host_keeps_its_whole_sitemap():
    """Measured: `docs.djangoproject.com` publishes 11,209 English URLs and the
    marketing filter cut them to 66 — `/topics/`, `/howto/`, `/ref/` and
    `/intro/` are not docs-shaped by that list, and `/releases/` is 4,898 pages
    it drops outright. A request for Django 5.2 came back with three pages."""
    urls = ([f"https://docs.d.dev/en/5.2/topics/{i}" for i in range(20)]
            + [f"https://docs.d.dev/en/5.2/releases/{i}" for i in range(20)]
            + [f"https://docs.d.dev/en/5.2/howto/{i}" for i in range(20)])
    assert df._focus_on_docs(urls, "/") == urls


def test_a_marketing_host_still_loses_its_blog():
    """The case the filter was written for is untouched: `astro.build` returned
    34 blog posts out of 40."""
    urls = ([f"https://d.dev/blog/post-{i}" for i in range(20)]
            + ["https://d.dev/start/", "https://d.dev/install/"])
    kept = df._focus_on_docs(urls, "/")
    assert not [u for u in kept if "/blog/" in u]
    assert "https://d.dev/start/" in kept


# ── a named release scopes the sitemap, not just the label ──────────────────

def test_a_named_release_narrows_the_sitemap():
    """The manifest path has honoured a named release since
    `_links_for_release`; the sitemap path never did, so `django` 5.2 harvested
    `/en/5.0/`, `/en/4.2/` and `/en/dev/` together and labelled the mixture
    5.2 — the same page under four releases, under exactly the right name."""
    urls = [f"https://docs.d.dev/en/{v}/topics/forms/"
            for v in ("5.2", "5.0", "4.2", "3.0")]
    kept = df._urls_for_release(urls, "5.2", df.Options())
    assert kept == ["https://docs.d.dev/en/5.2/topics/forms/"]


def test_a_release_that_scopes_a_path_beats_one_that_is_its_subject():
    """Django files the 5.2 manual at `/en/5.2/…` and its 5.2 release notes at
    `/en/dev/releases/5.2/`. Both name the release; only the first is the 5.2
    documentation, and the difference is how deep the segment sits."""
    urls = ["https://docs.d.dev/en/5.2/topics/forms/",
            "https://docs.d.dev/en/dev/releases/5.2/",
            "https://docs.d.dev/en/dev/releases/5.2.1/"]
    kept = df._urls_for_release(urls, "5.2", df.Options())
    assert kept == ["https://docs.d.dev/en/5.2/topics/forms/"]


def test_a_site_that_does_not_version_its_paths_is_left_alone():
    """A request for a release a site files somewhere else should get that
    site's documentation, not an empty harvest."""
    urls = ["https://docs.d.dev/topics/forms/", "https://docs.d.dev/howto/"]
    assert df._urls_for_release(urls, "5.2", df.Options()) == urls
    assert df._urls_for_release(urls, "", df.Options()) == urls


# ── every surface reads the same configuration ──────────────────────────────

def test_env_is_loaded_by_the_module_every_surface_imports(tmp_path, monkeypatch):
    """`app.py` called `load_dotenv()` and nothing else did. An MCP client
    launches `python main.py` with the ambient environment, so
    `DOCSFORGE_DB` was unset there and `build_store()` returned a `FileStore`.

    Measured 2026-09-10 against a Postgres store holding 23 technologies:
    `list_knowledge_base` over MCP answered "Nothing is stored yet", and after
    harvesting five technologies through that same surface,
    `read_knowledge_base` could not read one of them back."""
    (tmp_path / ".env").write_text("DOCSFORGE_ENV_PROBE=from-dotenv\n",
                                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    os.environ.pop("DOCSFORGE_ENV_PROBE", None)
    try:
        ft._load_env()
        assert os.environ.get("DOCSFORGE_ENV_PROBE") == "from-dotenv"
    finally:
        os.environ.pop("DOCSFORGE_ENV_PROBE", None)


def test_loading_env_never_overrides_what_the_caller_set(tmp_path, monkeypatch):
    """A caller that exported its own still wins, so `app.py` calling
    `load_dotenv` first changes nothing, and a test harness pointing at a
    throwaway database is not quietly redirected at the real one."""
    (tmp_path / ".env").write_text("DOCSFORGE_ENV_PROBE=from-dotenv\n",
                                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    os.environ["DOCSFORGE_ENV_PROBE"] = "from-the-caller"
    try:
        ft._load_env()
        assert os.environ["DOCSFORGE_ENV_PROBE"] == "from-the-caller"
    finally:
        os.environ.pop("DOCSFORGE_ENV_PROBE", None)


def test_a_missing_dotenv_package_is_not_an_error():
    """`python-dotenv` ships with the web extra, and the MCP server is a core
    install. Reading configuration must not become a new dependency."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "dotenv":
            raise ModuleNotFoundError("No module named 'dotenv'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = refuse
    try:
        ft._load_env()          # must not raise
    finally:
        builtins.__import__ = real_import


# ── coverage is unknown unless something stated the size ────────────────────

class _OnePage:
    """Serves one documentation page that links nowhere."""

    PAGE = ("<html><body><main><h1>Only page</h1><p>"
            + ("word " * 200) + "</p></main></body></html>")

    def __init__(self):
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        if url.rstrip("/").endswith("/docs"):
            r = _Resp(self.PAGE)
            r.url = url
            return r
        raise df.ForgeError(f"HTTP 404 for {url}")

    def text(self, url, **kw):
        return self.get(url).text

    def html(self, url, **kw):
        return self.get(url).text

    def close(self):
        pass


class _Resp:
    def __init__(self, text, ctype="text/html"):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": ctype}
        self.url = ""


def test_a_crawl_that_drained_its_frontier_reports_unknown_not_complete():
    """Measured 2026-09-10: `tensorflow` resolved to a rustdoc index page that
    links almost nowhere. The frontier drained after one page and the store
    recorded a one-page corpus as **complete** — the strongest claim this
    product makes, from the weakest evidence it has.

    A drained frontier is a real claim and a weaker one than a sitemap's:
    pages nothing links to are invisible to a crawl either way."""
    stats = {}
    docs, strategy = df.harvest("https://d.dev/docs/",
                                opts=df.Options(crawl=True, max_pages=25),
                                fetcher=_OnePage(), stats=stats)
    assert strategy == "crawl" and len(docs) == 1
    assert stats["whole"] is None, "unknown, not complete"
    assert stats["frontier_drained"] is True, "and say what was established"
    assert "nothing links to" in stats["reason"]


def test_unknown_coverage_is_reported_as_unknown_not_as_success():
    """`forge_tools` has always had the branch; nothing could reach it."""
    assert "COVERAGE UNKNOWN" in ft._coverage_note(None)
    assert ft._coverage_flag(None) != ft._coverage_flag(True)


def test_a_source_that_states_its_own_size_still_reports_complete():
    """A repository is enumerated through the API, and a spec or a single
    published document is one artifact that either arrived whole or did not.
    Those really do know their size, and demoting them would be losing a true
    claim rather than dropping a false one."""
    for kind in ("github", "openapi", "raw_text"):
        stats = {}
        det = df.Detection(kind, "https://d.dev/thing")
        df._note_coverage(stats, det, [df.Doc("https://d.dev/a", "A", "body")])
        assert stats["whole"] is True, kind

    stats = {}
    df._note_coverage(stats, df.Detection("html", "https://d.dev/x"),
                      [df.Doc("https://d.dev/a", "A", "body")])
    assert stats["whole"] is None, "everything else has established nothing"


# ── a version label is a finding or it is a caveat ──────────────────────────

def test_a_release_the_pages_name_is_recorded_as_confirmed():
    """`docs.djangoproject.com` files the 5.2 manual under `/en/5.2/`, so the
    pages themselves say which release they are."""
    stats = {}
    urls = [f"https://docs.d.dev/en/{v}/topics/forms/" for v in ("5.2", "4.2")]
    kept = df._urls_for_release(urls, "5.2", df.Options(), stats)
    assert kept == ["https://docs.d.dev/en/5.2/topics/forms/"]
    assert stats.get("release_confirmed") is True


def test_a_release_nothing_names_is_not_recorded_as_confirmed():
    """`pytorch.kr` has no release scoping at all, and `pytorch` was stored as
    **lts** — a release line PyTorch retired after 1.8.2."""
    stats = {}
    urls = ["https://pytorch.kr/", "https://pytorch.kr/hub/"]
    kept = df._urls_for_release(urls, "lts", df.Options(), stats)
    assert kept == urls, "taken as published rather than harvested empty"
    assert "release_confirmed" not in stats


# ── a wrong corpus has to be correctable through the tool that made it ──────

def _stored(tmp_path, name="pytorch", version="lts"):
    """A store already holding one version of `name`."""
    store = FileStore(tmp_path)
    with store.writer(name, version, "https://pytorch.kr/", "sitemap") as w:
        w.add("Korean page", "https://pytorch.kr/a", "body " * 50)
        w.settle(complete=False, expected=72, version=version, strategy="sitemap")
    ft.reset_store(store)
    return store


def test_an_already_stored_technology_is_not_re_harvested(tmp_path):
    """The guard that stops a site being crawled twice, unchanged."""
    _stored(tmp_path)
    out = ft.tool_learn_technology(name="pytorch")
    assert "already stored" in out
    assert "Nothing was fetched" in out


def test_refresh_re_harvests_a_stored_technology(tmp_path, monkeypatch):
    """Measured 2026-09-10: `pytorch` had been stored from a community mirror,
    and once resolution was fixed `learn_technology` still answered "already
    stored — nothing was fetched" and handed back the mirror. A caller that
    harvests the wrong corpus once could not correct it through the tool it
    harvests with: `forget_documentation` is gated behind an environment
    variable, and `harvest_docs` needs the URL the caller came here missing."""
    _stored(tmp_path)

    reached = {}

    def _fake_harvest(url, name=None, **kw):
        reached["url"] = url
        return "harvested"

    monkeypatch.setattr(ft, "_harvest_now", _fake_harvest)
    monkeypatch.setattr(ft, "_resolve", lambda name, ecosystem="": _Resolved())

    out = ft.tool_learn_technology(name="pytorch", refresh=True)
    assert "already stored" not in out
    assert reached.get("url") == "https://docs.pytorch.org/docs/stable/index.html"


class _Resolved:
    """The shape `_resolve` returns, reduced to what learn_technology reads."""

    class _Best:
        url = "https://docs.pytorch.org/docs/stable/index.html"
        evidence = "own-domain"
        reason = "identified by own-domain, docs-host"

    best = _Best()
    release = ""            # what the registry said is current; nothing here
    candidates: list = []
    note = ""
    resolved_via = "domain"

    def as_dict(self):
        return {"best": {"url": self.best.url}}


def test_refresh_reaches_a_detached_harvest_too(tmp_path):
    """A stdio MCP server hands its harvest to the long-lived server, and a
    flag that stops at the handoff is a flag that does nothing where the
    product actually runs."""
    import inspect
    # The handoff call itself now lives in `_as_harvest`, shared with
    # `harvest_docs`; what each tool still owns is the arguments it sends.
    source = inspect.getsource(ft.tool_learn_technology)
    handoff = source.split("handoff=", 1)[1]
    assert '"refresh": refresh' in handoff[:500]
    assert "hand_off(label, handoff, tool=tool)" in inspect.getsource(ft._as_harvest)


# ── no release asked for means the current one, not every one ───────────────

class _Site:
    """A fetcher that answers a few URLs, remembers what was asked, and can
    redirect its front page the way `docs.djangoproject.com/` does."""

    def __init__(self, pages=None, landing=None):
        self.pages = pages or {}
        self.landing = landing or {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if url not in self.pages:
            raise df.ForgeError(f"404 {url}")
        r = _Resp(self.pages[url])
        r.url = self.landing.get(url, url)
        return r

    def text(self, url, **kw):
        return self.get(url).text


def _django_sitemap():
    return ([f"https://docs.d.dev/en/{v}/topics/{i}/" for v in ("dev", "4.2", "6.1", "3.2")
             for i in range(4)] + ["https://docs.d.dev/en/"])


def test_an_unpinned_harvest_takes_the_registry_release_not_the_sitemap_order():
    """Measured 2026-09-21 (bench-2, Issues.md V1): `learn_technology("django")`
    took the English sitemap in the sitemap's order -- forty pages of
    `/en/dev/` -- and stored them under PyPI's 6.1.1."""
    kept, chosen = df._prefer_current_release(_django_sitemap(), df.Options(verbose=False),
                                              hint="6.1.1")
    assert chosen == "6.1"
    assert kept and all("/en/6.1/" in u for u in kept)


def test_the_unversioned_pages_are_the_current_release_when_there_are_enough():
    """`python-poetry.org` keeps the current docs at `/docs/` beside `/docs/1.8/`
    and `/docs/main/`; forty pages stored as 2.5.1 were three releases mixed."""
    urls = ([f"https://p.org/docs/{p}/" for p in ("cli", "basic-usage", "config", "faq")]
            + [f"https://p.org/docs/1.8/{p}/" for p in ("cli", "basic-usage")]
            + [f"https://p.org/docs/main/{p}/" for p in ("cli", "basic-usage")])
    kept, chosen = df._prefer_current_release(urls, df.Options(verbose=False))
    assert chosen == "" and len(kept) == 4
    assert not [u for u in kept if "/1.8/" in u or "/main/" in u]


def test_the_front_page_decides_when_nothing_cheaper_does():
    """`sequelize.org` files `/docs/v6/` (stable) beside `/docs/v7/` (alpha),
    was found by its own domain so no registry release is known, and its
    front page links to v6 eight times and v7 once. One request, paid only
    here."""
    urls = [f"https://s.org/docs/v6/{i}/" for i in range(3)] + \
           [f"https://s.org/docs/v7/{i}/" for i in range(3)]
    front = "".join(f'<a href="/docs/v6/{i}/">v6</a>' for i in range(8)) + '<a href="/docs/v7/">v7</a>'
    site = _Site({"https://s.org/": front})
    kept, chosen = df._prefer_current_release(
        urls, df.Options(verbose=False), entry=lambda: df._entry_page("https://s.org/", site))
    assert chosen == "v6" and all("/docs/v6/" in u for u in kept)
    assert site.calls == ["https://s.org/"], "the front page was read once"


def test_a_front_page_that_redirects_names_the_current_release():
    """`docs.djangoproject.com/` answers 302 to `/en/6.1/`: the site's own
    statement of which release is current, with no registry needed."""
    site = _Site({"https://docs.d.dev/": "<html></html>"},
                 landing={"https://docs.d.dev/": "https://docs.d.dev/en/6.1/"})
    kept, chosen = df._prefer_current_release(
        _django_sitemap(), df.Options(verbose=False),
        entry=lambda: df._entry_page("https://docs.d.dev/", site))
    assert chosen == "6.1"


def test_a_development_line_is_never_current_by_number():
    """With no registry, no front page and no `stable`, the highest-numbered
    release is current; `dev`, `next` and `main` are never chosen by number."""
    urls = [f"https://d.dev/en/{v}/x/" for v in ("dev", "4.2", "5.0", "next")]
    kept, chosen = df._prefer_current_release(urls, df.Options(verbose=False),
                                              entry=lambda: ("", []))
    assert chosen == "5.0"
    only_dev = [f"https://d.dev/en/{v}/x/" for v in ("dev", "next")]
    assert df._prefer_current_release(only_dev, df.Options(verbose=False),
                                      entry=lambda: ("", []))[1] == ""


def test_a_site_with_one_release_or_none_is_left_alone():
    one = [f"https://d.dev/en/5.2/{i}/" for i in range(3)]
    none = [f"https://d.dev/docs/{i}/" for i in range(3)]
    for urls in (one, none):
        kept, chosen = df._prefer_current_release(urls, df.Options(verbose=False), hint="5.2")
        assert kept == urls and chosen == ""


def test_a_bare_integer_is_not_a_release_line():
    """`jestjs.io/blog/2016/09/01/` sits beside `/docs/29.7/`; a year, a day, a
    numbered chapter or a page of an archive is not a release line to be
    chosen or filed under. `docs.python.org/3/` is the current documentation
    beside `/3.12/`, and belongs with the unversioned pages."""
    assert df._release_segment("https://j.io/blog/2016/09/01/post/") is None
    assert df._release_segment("https://d.dev/docs/1/intro/") is None
    assert df._release_segment("https://docs.python.org/3/library/") is None
    assert df._release_segment("https://j.io/docs/29.7/api/") == (1, "29.7")
    assert df._release_segment("https://s.org/docs/v6/") == (1, "v6")
    assert df._release_segment("https://j.io/docs/next/api/") == (1, "next")


# ── a pinned release starts where the site files it ─────────────────────────

def test_a_pinned_release_moves_the_start_url_when_the_site_answers_there():
    """`pydantic` resolves to `/docs/validation/latest/llms.txt`; a request for
    1.10 was answered with the 2.x dump under a 1.10 label (bench-2, V2). The
    site publishes `/docs/validation/1.10/llms.txt`."""
    site = _Site({"https://p.dev/docs/validation/1.10/llms.txt": "# Pydantic 1.10"})
    stats = {}
    url = df._url_for_release("https://p.dev/docs/validation/latest/llms.txt", "1.10",
                              site, df.Options(verbose=False), stats)
    assert url == "https://p.dev/docs/validation/1.10/llms.txt"
    assert stats["release_confirmed"] is True and stats["release_url"] == url


def test_a_pinned_release_keeps_the_sites_spelling():
    site = _Site({"https://s.org/docs/v7/": "v7"})
    assert df._url_for_release("https://s.org/docs/v6/", "7", site,
                               df.Options(verbose=False)) == "https://s.org/docs/v7/"


def test_a_pinned_release_the_site_does_not_have_leaves_the_url_alone():
    """No such path, or a redirect back to the release it already had: the
    harvest goes on from where it was, and the caveat says what that means."""
    absent = _Site({})
    assert df._url_for_release("https://p.dev/docs/latest/", "1.10", absent,
                               df.Options(verbose=False)) == "https://p.dev/docs/latest/"
    bounced = _Site({"https://p.dev/docs/1.10/": "x"},
                    landing={"https://p.dev/docs/1.10/": "https://p.dev/docs/latest/"})
    stats = {}
    assert df._url_for_release("https://p.dev/docs/latest/", "1.10", bounced,
                               df.Options(verbose=False), stats) == "https://p.dev/docs/latest/"
    assert "release_confirmed" not in stats


def test_a_url_that_names_no_release_or_the_right_one_is_not_touched():
    site = _Site({})
    for url in ("https://d.dev/", "https://d.dev/docs/1.10/x/"):
        assert df._url_for_release(url, "1.10", site, df.Options(verbose=False)) == url
    assert site.calls == []


# ── the label follows the release the site filed the pages under ────────────

def test_the_label_is_the_release_the_site_filed_the_pages_under():
    """`sequelize.org/` names no version; forty pages of `/docs/v6/` were filed
    under a date. The pages say v6, so the label does."""
    docs = [df.Doc(f"https://s.org/docs/v6/{i}/", "t", "b") for i in range(3)]
    assert ft._version_label("https://s.org/", docs, site="v6") == "v6"


def test_a_registry_release_that_agrees_with_the_site_keeps_its_precision():
    docs = [df.Doc(f"https://docs.d.dev/en/6.1/{i}/", "t", "b") for i in range(3)]
    assert ft._version_label("https://docs.d.dev/", docs, declared="6.1.1", site="6.1") == "6.1.1"


def test_a_registry_release_that_disagrees_with_the_site_loses():
    """The registry says 7.0.0; the site filed the pages it handed over under
    6.4. The pages are the finding."""
    docs = [df.Doc(f"https://docs.d.dev/6.4/{i}/", "t", "b") for i in range(3)]
    assert ft._version_label("https://docs.d.dev/", docs, declared="7.0.0", site="6.4") == "6.4"
