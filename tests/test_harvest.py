"""Offline tests for crawl scoping and the knowledge base — no network."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docsforge as df
import forge_tools as ft


# ── crawl scoping ────────────────────────────────────────
# Docs share a domain with marketing, blogs and podcasts. Crawling by host
# walked from an Effect docs page straight into /podcast, and the off-topic
# pages then dominated the truncated result the model saw.
@pytest.mark.parametrize("url,expected", [
    ("https://www.effect.website/docs/v3/getting-started/introduction/", "/docs/v3/"),
    ("https://www.effect.website/docs/v3", "/docs/v3/"),
    ("https://docs.python.org/3/library/json.html", "/3/library/"),
    ("https://x.dev/guide/setup", "/guide/"),
    ("https://x.dev/reference/api/v2/things", "/reference/"),
    ("https://x.dev/documentation/v10/intro", "/documentation/v10/"),
    ("https://example.com/", "/"),
])
def test_docs_scope_anchors_on_the_documentation_root(url, expected):
    assert df.docs_scope(url) == expected


def test_scope_keeps_a_version_segment_but_not_a_word():
    assert df.docs_scope("https://x.dev/docs/v3/a/b") == "/docs/v3/"
    assert df.docs_scope("https://x.dev/docs/latest/a") == "/docs/"


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
def kb(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ft, "KB_INDEX", tmp_path / "index.json")
    return tmp_path


DOC = "# effect\n\n## Error Handling\n\nfail fast\n\n## Layers\n\nwiring with npm\n"


def _store(kb, name="effect", body=DOC, complete=True):
    (kb / f"{name}.md").write_text(body, encoding="utf-8")
    (kb / "index.json").write_text(json.dumps({name: {
        "name": name, "source": "https://x.dev/docs/", "strategy": "crawl",
        "pages": 2, "characters": len(body), "file": str(kb / f"{name}.md"),
        "harvested": "2026-01-01 00:00", "titles": ["Error Handling", "Layers"],
        "complete": complete,
    }}), encoding="utf-8")


def test_empty_knowledge_base_says_what_to_do(kb):
    assert "harvest_docs" in ft.tool_list_knowledge_base()


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
    (kb / "effect.md").unlink()
    with pytest.raises(ft.ForgeError, match="file is missing"):
        ft.tool_read_knowledge_base("effect")


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
