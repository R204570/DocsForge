"""
Integration tests for the LLMSFinder correctness-hardening pass.

Covers, through the real `harvest()` path rather than isolated units:
  - version-scoped llms.txt acquisition (safe vs. ambiguous manifests)
  - hybrid coverage semantics (root vs. manifest vs. corpus completeness)
  - manifest link classification (`expected` excludes off-site links)
  - failed-URL identity (category + normalized URL, for a future retry)
  - failure semantics (no manifest ever silently reported whole)
  - no redundant discovery once an LLMS rung has won
  - the version a manifest declares about itself
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core import llmsfinder


class FakeResponse:
    def __init__(self, text="", status=200, ctype="text/plain", url=""):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.url = url

    @property
    def content(self):
        return self.text.encode("utf-8")


class FakeFetcher:
    """Answers from a dict of url -> FakeResponse; 404s anything else."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url, **kw):
        self.asked.append(url)
        hit = self.pages.get(url.rstrip("/")) or self.pages.get(url)
        return hit or FakeResponse("not found", status=404, url=url)

    def text(self, url, **kw):
        res = self.get(url, **kw)
        if res.status_code != 200:
            raise df.ForgeError(f"HTTP {res.status_code} for {url}")
        return res.text

    def html(self, url):
        return self.text(url)

    def render(self, url):
        return self.text(url)

    def close(self):
        pass


PAGE_HTML = (
    "<html><head><title>%s</title></head><body><main><h1>%s</h1>"
    "<p>%s</p></main></body></html>"
)


def _page(title):
    return PAGE_HTML % (title, title, "Real content. " * 40)


# ── 1. Version-aware LLMS acquisition ─────────────────────────
# These two used to call `_llms_txt_version_scope`, which the two-pathway
# split left with no production caller — so they passed while exercising
# code nothing ran. They now go through `_scope_site_wide_llms`, which is
# what `harvest()` actually calls, and assert the same two behaviours.
def _decision(url, body, version=""):
    det = df.Detection("llms_txt", "https://x.dev/llms.txt", body)
    opts = df.Options(verbose=False, delay=0.0, version=version)
    pathway = df._scope_site_wide_llms(url, det, FakeFetcher({}), opts)
    return pathway.skip, pathway.restrict_links


def test_version_scope_multi_version_manifest_is_filtered():
    """Case B: a manifest that mixes versions can be safely narrowed."""
    body = "# Docs\n\n" + "\n".join([
        "- [V1 A](https://x.dev/docs/v1/a.md)",
        "- [V2 A](https://x.dev/docs/v2/a.md)",
        "- [V2 B](https://x.dev/docs/v2/b.md)",
        "- [V3 A](https://x.dev/docs/v3/a.md)",
    ])
    skip, scoped = _decision("https://x.dev/docs/v2/", body)
    assert skip is False
    urls = sorted(u for _t, u in scoped)
    assert urls == ["https://x.dev/docs/v2/a.md", "https://x.dev/docs/v2/b.md"]


def test_version_scope_ambiguous_manifest_is_refused():
    """Case A: a manifest with no version signal cannot be safely scoped,
    so the release pathway refuses it rather than guessing."""
    body = "# Docs\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/docs/page{i}.md)" for i in range(10)
    )
    skip, scoped = _decision("https://x.dev/docs/v2/", body)
    assert skip is True
    assert scoped is None


def test_harvest_versioned_request_scopes_llms_manifest():
    """End to end: a versioned request against a multi-version site-wide
    manifest acquires only the requested version's pages via llms.txt."""
    manifest = "# Docs\n\n" + "\n".join([
        "- [V1 A](https://x.dev/docs/v1/a.md)",
        "- [V2 A](https://x.dev/docs/v2/a.md)",
        "- [V2 B](https://x.dev/docs/v2/b.md)",
        "- [V3 A](https://x.dev/docs/v3/a.md)",
    ])
    pages = {
        "https://x.dev/llms.txt": FakeResponse(manifest),
        "https://x.dev/docs/v1/a.md": FakeResponse("# V1 A"),
        "https://x.dev/docs/v2/a.md": FakeResponse("# V2 A"),
        "https://x.dev/docs/v2/b.md": FakeResponse("# V2 B"),
        "https://x.dev/docs/v3/a.md": FakeResponse("# V3 A"),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, strat = df.harvest("https://x.dev/docs/v2/intro", fetcher=fetcher, stats=stats)

    urls = sorted(d.url for d in docs)
    assert urls == ["https://x.dev/docs/v2/a.md", "https://x.dev/docs/v2/b.md"]
    assert "v1" not in "".join(urls) and "v3" not in "".join(urls)
    assert stats["expected"] == 2
    assert stats["acquired"] == 2
    assert stats["whole"] is True
    assert "llms.txt" in strat


def test_harvest_versioned_request_falls_back_when_ambiguous():
    """End to end: an ambiguous manifest is not used at all; the harvest
    falls back to the version-scoped sitemap/crawl path, which still never
    admits another version's pages."""
    manifest = "# Docs\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/docs/page{i}.md)" for i in range(10)
    )
    sitemap = "<urlset>" + "".join(
        f"<url><loc>{u}</loc></url>" for u in [
            "https://x.dev/docs/v1/a",
            "https://x.dev/docs/v2/a",
            "https://x.dev/docs/v2/b",
            "https://x.dev/docs/v2/c",
            "https://x.dev/docs/v3/a",
        ]
    ) + "</urlset>"
    pages = {
        "https://x.dev/llms.txt": FakeResponse(manifest),
        "https://x.dev/sitemap.xml": FakeResponse(sitemap, ctype="application/xml"),
        "https://x.dev/docs/v2/a": FakeResponse(_page("A")),
        "https://x.dev/docs/v2/b": FakeResponse(_page("B")),
        "https://x.dev/docs/v2/c": FakeResponse(_page("C")),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, strat = df.harvest("https://x.dev/docs/v2/intro", fetcher=fetcher, stats=stats)

    urls = sorted(d.url for d in docs)
    assert urls == [
        "https://x.dev/docs/v2/a",
        "https://x.dev/docs/v2/b",
        "https://x.dev/docs/v2/c",
    ]
    assert strat == "sitemap"


# ── 2 & 3. Hybrid coverage semantics + manifest link classification ──
def test_hybrid_external_links_excluded_from_expected():
    hybrid_body = (
        "# Overview\n\nMain prose overview text here.\n\n"
        + ("Additional explanatory prose content. " * 30) + "\n\n"
        + "- [Doc 0](https://x.dev/d0.md)\n"
        + "- [Doc 1](https://x.dev/d1.md)\n"
        + "- [External](https://other.example/page)\n"
    )
    pages = {
        "https://x.dev/llms.txt": FakeResponse(hybrid_body),
        "https://x.dev/d0.md": FakeResponse("# D0"),
        "https://x.dev/d1.md": FakeResponse("# D1"),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, _strat = df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    urls = [d.url for d in docs]
    assert "https://other.example/page" not in urls
    # Root + 2 in-scope docs; the external link never counted or was fetched.
    assert stats["expected"] == 2
    assert stats["acquired"] == 2
    assert stats["whole"] is True
    assert "https://other.example/page" not in fetcher.asked


def test_hybrid_partial_failure_root_success_not_whole():
    """Root document acquisition succeeding must not imply corpus
    completeness — a hybrid with any failed linked page reports whole=False."""
    hybrid_body = (
        "# Overview\n\nMain prose overview text here.\n\n"
        + ("Additional explanatory prose content. " * 30) + "\n\n"
        + "- [Doc 0](https://x.dev/d0.md)\n"
        + "- [Doc 1](https://x.dev/d1.md)\n"
        + "- [Doc 2](https://x.dev/d2.md)\n"
    )
    pages = {
        "https://x.dev/llms.txt": FakeResponse(hybrid_body),
        "https://x.dev/d0.md": FakeResponse("# D0"),
        "https://x.dev/d2.md": FakeResponse("# D2"),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, _strat = df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    assert len(docs) == 3  # root + 2 acquired
    assert stats["whole"] is False
    assert stats["failed"] == 1
    assert len(stats["failed_urls"]) == 1
    assert stats["failed_urls"][0]["url"] == "https://x.dev/d1.md"


# ── 4 & 5. Failed URL identity, ready for a future targeted retry ────
def test_manifest_permanent_failure_identifies_missing_page():
    index_body = "# Index\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/p{i}.md)" for i in range(3)
    )
    pages = {
        "https://x.dev/llms.txt": FakeResponse(index_body),
        "https://x.dev/p0.md": FakeResponse("# P0"),
        "https://x.dev/p1.md": FakeResponse("# P1"),
        # p2.md permanently 404s
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, _strat = df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    assert len(docs) == 2
    assert stats["whole"] is False
    assert stats["failed"] == 1
    failed = stats["failed_urls"]
    assert len(failed) == 1
    assert failed[0]["url"] == "https://x.dev/p2.md"
    assert failed[0]["category"] == "http_error"
    # A future retry could reconstruct exactly the failed subset:
    acquired_urls = {d.url for d in docs}
    assert acquired_urls == {"https://x.dev/p0.md", "https://x.dev/p1.md"}
    assert {f["url"] for f in failed} == {"https://x.dev/p2.md"}


# ── 6 & 7. Failure semantics: no manifest reported whole by accident ──
def test_index_manifest_all_links_fail_is_not_reported_whole():
    """Regression: if every linked page fails, `handle_llms_txt` must not
    fall through to storing the raw manifest text as though it were the
    (whole) documentation — the old fallthrough this guards against
    returned `[Doc(det.url, "llms.txt", body)]` with `whole=True`."""
    index_body = "# Index\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/p{i}.md)" for i in range(3)
    )
    det = df.Detection("llms_txt", "https://x.dev/llms.txt", index_body)
    fetcher = FakeFetcher({"https://x.dev/llms.txt": FakeResponse(index_body)})
    stats = {}
    docs = df.handle_llms_txt(det, fetcher, df.Options(verbose=False), stats=stats)

    assert docs == []
    assert stats["whole"] is False
    assert stats["expected"] == 3
    assert stats["acquired"] == 0
    assert stats["failed"] == 3


def test_harvest_falls_back_to_crawl_when_manifest_totally_fails():
    """End to end: when every manifest link fails, the llms.txt rung has
    failed, so the harvest falls through to the next rung (sitemap here)
    instead of reporting an empty or fabricated success."""
    index_body = "# Index\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/p{i}.md)" for i in range(3)
    )
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://x.dev/docs/p{i}</loc></url>" for i in range(3))
               + "</urlset>")
    pages = {
        "https://x.dev/llms.txt": FakeResponse(index_body),
        "https://x.dev/sitemap.xml": FakeResponse(sitemap, ctype="application/xml"),
        "https://x.dev/docs/p0": FakeResponse(_page("P0")),
        "https://x.dev/docs/p1": FakeResponse(_page("P1")),
        "https://x.dev/docs/p2": FakeResponse(_page("P2")),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, strat = df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    assert strat == "sitemap"
    assert len(docs) == 3


def test_root_llms_txt_fetch_failure_falls_back():
    """A root llms.txt that cannot even be fetched must not crash the whole
    harvest — the ladder should fall through to the next rung."""
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://x.dev/docs/p{i}</loc></url>" for i in range(3))
               + "</urlset>")
    pages = {
        # llms.txt / llms-full.txt both 404 during detect_source's probe...
        "https://x.dev/sitemap.xml": FakeResponse(sitemap, ctype="application/xml"),
        "https://x.dev/docs/p0": FakeResponse(_page("P0")),
        "https://x.dev/docs/p1": FakeResponse(_page("P1")),
        "https://x.dev/docs/p2": FakeResponse(_page("P2")),
    }
    fetcher = FakeFetcher(pages)
    # Force detection straight to a broken llms.txt reference (body=None),
    # simulating a race where the file vanished between detection and fetch.
    det = df.Detection("llms_txt", "https://x.dev/llms-full.txt", None)
    import unittest.mock as mock
    with mock.patch.object(df, "detect_source", return_value=det):
        stats = {}
        docs, strat = df.harvest("https://x.dev/docs/p0", fetcher=fetcher, stats=stats)

    assert strat == "sitemap"
    assert len(docs) == 3


# ── No redundant discovery after a successful LLMS acquisition ───────
def test_no_redundant_discovery_after_index_manifest_success():
    index_body = "# Index\n\n" + "\n".join(
        f"- [Page {i}](https://x.dev/p{i}.md)" for i in range(3)
    )
    pages = {
        "https://x.dev/llms.txt": FakeResponse(index_body),
        "https://x.dev/p0.md": FakeResponse("# P0"),
        "https://x.dev/p1.md": FakeResponse("# P1"),
        "https://x.dev/p2.md": FakeResponse("# P2"),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, strat = df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    assert len(docs) == 3
    assert stats["whole"] is True
    assert not any("sitemap" in u for u in fetcher.asked)
    assert not any("robots" in u for u in fetcher.asked)


# ── Declared version: the manifest states what the URL cannot ────────
# Found against the real mojolang.org harvest, which stored 1.1 MB of Mojo
# 1.0.0 documentation under the label "2026-09-01" -- the date fallback that
# exists to admit "no version could be established" -- while the file itself
# said `Version: 1.0.0` in its first 200 bytes.
def test_declared_version_is_read_from_the_header():
    body = ("# Mojo programming language documentation\n\n"
            "> Official documentation for the Mojo programming language.\n\n"
            "Version: 1.0.0\n\n## Install Mojo\n\nprose\n")
    assert llmsfinder.declared_version(body) == "1.0.0"


def test_a_version_line_below_the_first_section_is_not_a_claim_about_the_file():
    """Deep in the body, `Version:` is documentation *about* versions -- a
    changelog entry or an install transcript -- not the file's own label."""
    body = "# Docs\n\n> summary\n\n## Changelog\n\nVersion: 9.9.9\n\nolder notes\n"
    assert llmsfinder.declared_version(body) == ""


def test_a_manifest_that_states_nothing_yields_nothing():
    assert llmsfinder.declared_version("# Docs\n\n> summary\n\n## A\n\nprose") == ""
    assert llmsfinder.declared_version("") == ""


def test_the_declared_version_reaches_stats_through_a_real_harvest():
    body = ("# Docs\n\n> summary\n\nVersion: 2.4.1\n\n"
            + "\n".join(f"- [Page {i}](https://x.dev/p{i}.md)" for i in range(3)))
    pages = {"https://x.dev/llms.txt": FakeResponse(body)}
    for i in range(3):
        pages[f"https://x.dev/p{i}.md"] = FakeResponse(f"# Page {i}")

    stats = {}
    df.harvest("https://x.dev/llms.txt", fetcher=FakeFetcher(pages), stats=stats)
    assert stats["declared_version"] == "2.4.1"


def test_a_declared_release_replaces_the_date_but_a_vague_label_does_not():
    from docsforge.tools import forge_tools as ft

    docs = [df.Doc("https://x.dev/a", "A", "")]
    today = time.strftime("%Y-%m-%d")

    # No version in the URL: the date is all the URL can offer.
    assert ft._version_label("https://x.dev/llms.txt", docs) == today
    # A real release number is a better answer than the date.
    assert ft._version_label("https://x.dev/llms.txt", docs, "1.0.0") == "1.0.0"
    # "latest" is not. It says no more than the fallback it would replace,
    # so the honest admission stays.
    assert ft._version_label("https://x.dev/llms.txt", docs, "latest") == today
    assert ft._version_label("https://x.dev/llms.txt", docs, "stable") == today


def test_a_version_in_the_url_still_wins_over_one_the_file_declares():
    """The URL is what the caller asked for. A site-wide file's own label
    must not quietly relabel a harvest the caller scoped to one version."""
    from docsforge.tools import forge_tools as ft

    docs = [df.Doc("https://x.dev/docs/v3/a", "A", ""),
            df.Doc("https://x.dev/docs/v3/b", "B", "")]
    assert ft._version_label("https://x.dev/docs/v3/", docs, "9.9.9") == "v3"


# ── The coverage denominator counts documentation, not citations ─────
def test_off_site_citations_are_excluded_from_the_expected_count():
    """The mojo harvest recorded `expected: 435` -- 434 links plus a root --
    and so reported INCOMPLETE for failing to fetch Wikipedia and YouTube.
    Only pages this harvest intends to acquire may set the denominator."""
    body = ("# Docs\n\n> summary\n\n"
            + ("Substantial prose that makes this a hybrid rather than an index. " * 40)
            + "\n\n- [Real page](https://x.dev/a.md)\n"
            + "- [Also real](https://x.dev/b.md)\n"
            + "- [Wikipedia](https://en.wikipedia.org/wiki/Thing)\n"
            + "- [A talk](https://www.youtube.com/watch?v=abc)\n")
    pages = {
        "https://x.dev/llms.txt": FakeResponse(body),
        "https://x.dev/a.md": FakeResponse("# A"),
        "https://x.dev/b.md": FakeResponse("# B"),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    df.harvest("https://x.dev/llms.txt", fetcher=fetcher, stats=stats)

    assert stats["expected"] == 2, "only the two same-host pages were promised"
    assert stats["acquired"] == 2
    assert stats["whole"] is True, "acquiring everything promised is complete"
    # Never reached for, so never counted as missing.
    assert not any("wikipedia" in u for u in fetcher.asked)
    assert not any("youtube" in u for u in fetcher.asked)


def test_a_partial_dump_with_many_citations_stays_hybrid():
    """Regression guard for a fix that would have been wrong.

    mojolang.org/llms-full.txt is 1.1 MB with 434 links, and the obvious
    reading is "a dump misclassified as a hybrid". It is not: 155 of its 213
    same-host linked pages hold content the dump does not, so calling it a
    dump would silently discard them. Size alone must never override the
    shape, or a partial dump stops being followed."""
    body = ("# Big docs\n\n> summary\n\n"
            + ("Real documentation prose, paragraphs of it, the manual itself. " * 3000)
            + "\n\n"
            + "\n".join(f"- [Further page {i}](https://x.dev/docs/section/{i}/)"
                        for i in range(250)))

    # Shaped like the real thing: megabyte-scale prose whose citations sit
    # squarely inside the hybrid density window, which is exactly why size
    # cannot be used to overrule it.
    link_chars = sum(len(l) for l in body.splitlines() if llmsfinder._LINK_RE.search(l))
    density = link_chars / len(body)
    assert len(body) > 180_000, "large enough that a naive size rule would fire"
    assert 0.03 <= density < 0.5, f"density {density:.3f} must sit in the hybrid window"

    assert llmsfinder.classify_llms_shape(body) == "hybrid"


# ── Never broaden a scoped request, version or not ──────────────────
# docs.modular.com publishes one llms.txt for Modular Cloud. A request for
# /mojo/ was answered with API-key and billing documentation: nothing about
# that is version-specific, so `_asks_for_a_version` never fired and the
# site-wide file was used unchallenged.
def test_a_site_wide_manifest_covering_another_product_is_refused():
    manifest = ("# Modular Cloud\n\n> Cloud infrastructure docs.\n\n"
                + "\n".join(f"- [Admin {i}](https://d.dev/administration/{i}/)"
                            for i in range(6)))
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://d.dev/mojo/p{i}</loc></url>" for i in range(3))
               + "</urlset>")
    pages = {
        "https://d.dev/llms.txt": FakeResponse(manifest),
        "https://d.dev/sitemap.xml": FakeResponse(sitemap, ctype="application/xml"),
        "https://d.dev/mojo/p0": FakeResponse(_page("P0")),
        "https://d.dev/mojo/p1": FakeResponse(_page("P1")),
        "https://d.dev/mojo/p2": FakeResponse(_page("P2")),
    }
    fetcher = FakeFetcher(pages)
    docs, strategy = df.harvest("https://d.dev/mojo/", fetcher=fetcher, stats={})

    assert strategy == "sitemap", "the manifest documents another product"
    assert [d.url for d in docs] == ["https://d.dev/mojo/p0",
                                     "https://d.dev/mojo/p1",
                                     "https://d.dev/mojo/p2"]
    assert not any("/administration/" in d.url for d in docs)


def test_a_site_wide_manifest_is_narrowed_when_it_does_cover_the_scope():
    """Refusal is the last resort, not the reflex: a manifest that lists
    pages under the requested prefix is used, minus everything else."""
    manifest = ("# Everything\n\n> All products.\n\n"
                + "- [Cloud](https://d.dev/administration/keys/)\n"
                + "- [Mojo A](https://d.dev/mojo/a/)\n"
                + "- [Mojo B](https://d.dev/mojo/b/)\n")
    pages = {
        "https://d.dev/llms.txt": FakeResponse(manifest),
        "https://d.dev/mojo/a/": FakeResponse(_page("A")),
        "https://d.dev/mojo/b/": FakeResponse(_page("B")),
    }
    fetcher = FakeFetcher(pages)
    stats = {}
    docs, _ = df.harvest("https://d.dev/mojo/", fetcher=fetcher, stats=stats)

    assert sorted(d.url for d in docs) == ["https://d.dev/mojo/a/", "https://d.dev/mojo/b/"]
    assert stats["expected"] == 2
    assert not any("/administration/" in u for u in fetcher.asked)


def test_asking_for_the_whole_site_still_gets_the_published_file_in_one_request():
    """The headline case must not become collateral damage: a request with
    no section in it is not a scoped request, and still costs one fetch."""
    dump = "# Complete Docs Dump\n\n" + ("Full documentation text. " * 200)
    pages = {"https://d.dev/llms-full.txt": FakeResponse(dump)}
    fetcher = FakeFetcher(pages)
    docs, strategy = df.harvest("https://d.dev/", fetcher=fetcher, stats={})

    assert strategy == "llms-full.txt"
    assert len(docs) == 1
    assert not any("sitemap" in u for u in fetcher.asked)


def test_a_dump_is_still_used_for_a_scoped_request():
    """A dump lists no pages, so it makes no checkable claim about what it
    covers. Refusing on a suspicion nothing supports would trade a site's
    whole published corpus for a crawl."""
    dump = "# Complete Docs Dump\n\n" + ("Full documentation text. " * 200)
    pages = {"https://d.dev/llms-full.txt": FakeResponse(dump)}
    fetcher = FakeFetcher(pages)
    docs, strategy = df.harvest("https://d.dev/docs/sub/page.html",
                                fetcher=fetcher, stats={})

    assert strategy == "llms-full.txt"
    assert len(docs) == 1


# ── Two pathways: latest takes the published file, a release earns it ──
# `llms.txt` is published for a site's CURRENT release. Until this split,
# the requested version reached only the label, so asking for 1.10 fetched
# the latest dump and filed it as "1.10" -- the wrong documentation under
# exactly the right name.
def _site(dump_body):
    return {"https://d.dev/llms-full.txt": FakeResponse(dump_body)}


def test_no_version_asked_takes_the_published_file_in_one_request():
    dump = "# Docs\n\n" + ("Current release prose. " * 300)
    fetcher = FakeFetcher(_site(dump))
    docs, strategy = df.harvest("https://d.dev/docs/", fetcher=fetcher, stats={})

    assert strategy == "llms-full.txt"
    assert len(docs) == 1
    assert len(fetcher.asked) == 1, "the whole point: one request"


def test_a_named_release_refuses_a_file_that_cannot_show_it_is_that_release():
    dump = "# Docs\n\n" + ("Current release prose. " * 300)
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://d.dev/docs/p{i}</loc></url>" for i in range(3))
               + "</urlset>")
    pages = _site(dump)
    pages["https://d.dev/sitemap.xml"] = FakeResponse(sitemap, ctype="application/xml")
    for i in range(3):
        pages[f"https://d.dev/docs/p{i}"] = FakeResponse(_page(f"P{i}"))

    fetcher = FakeFetcher(pages)
    opts = df.Options(verbose=False, delay=0.0, version="1.10")
    docs, strategy = df.harvest("https://d.dev/docs/", opts, fetcher=fetcher, stats={})

    assert strategy != "llms-full.txt", "the current release is not release 1.10"
    assert strategy == "sitemap"
    assert len(docs) == 3


def test_a_named_release_takes_the_file_when_it_states_that_version():
    dump = ("# Docs\n\n> summary\n\nVersion: 1.10.4\n\n"
            + ("Release 1.10 prose. " * 300))
    fetcher = FakeFetcher(_site(dump))
    opts = df.Options(verbose=False, delay=0.0, version="1.10")
    docs, strategy = df.harvest("https://d.dev/docs/", opts, fetcher=fetcher, stats={})

    assert strategy == "llms-full.txt", "1.10.4 answers a request for 1.10"
    assert len(docs) == 1


def test_a_stated_version_from_a_different_release_is_still_refused():
    dump = ("# Docs\n\n> summary\n\nVersion: 2.11.0\n\n"
            + ("Release 2.11 prose. " * 300))
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://d.dev/docs/p{i}</loc></url>" for i in range(3))
               + "</urlset>")
    pages = _site(dump)
    pages["https://d.dev/sitemap.xml"] = FakeResponse(sitemap, ctype="application/xml")
    for i in range(3):
        pages[f"https://d.dev/docs/p{i}"] = FakeResponse(_page(f"P{i}"))

    fetcher = FakeFetcher(pages)
    opts = df.Options(verbose=False, delay=0.0, version="1.10")
    docs, strategy = df.harvest("https://d.dev/docs/", opts, fetcher=fetcher, stats={})
    assert strategy == "sitemap", "2.11 must never answer a request for 1.10"


def test_a_named_release_is_narrowed_to_the_pages_filed_under_it():
    """Sites that keep every release side by side list them all in one
    manifest, and the path is what says which is which."""
    manifest = ("# Docs\n\n> summary\n\n"
                + "- [Old](https://d.dev/docs/1.9/a.md)\n"
                + "- [Wanted A](https://d.dev/docs/1.10/a.md)\n"
                + "- [Wanted B](https://d.dev/docs/1.10/b.md)\n"
                + "- [New](https://d.dev/docs/2.11/a.md)\n")
    pages = {
        "https://d.dev/llms.txt": FakeResponse(manifest),
        "https://d.dev/docs/1.10/a.md": FakeResponse("# A"),
        "https://d.dev/docs/1.10/b.md": FakeResponse("# B"),
    }
    fetcher = FakeFetcher(pages)
    opts = df.Options(verbose=False, delay=0.0, version="1.10")
    stats = {}
    docs, _ = df.harvest("https://d.dev/docs/", opts, fetcher=fetcher, stats=stats)

    assert sorted(d.url for d in docs) == ["https://d.dev/docs/1.10/a.md",
                                           "https://d.dev/docs/1.10/b.md"]
    assert stats["expected"] == 2
    assert not any("/1.9/" in u or "/2.11/" in u for u in fetcher.asked)


def test_a_version_in_the_url_routes_the_same_way_as_one_passed_in():
    """`/docs/v3/` names a release as plainly as version="v3" does."""
    dump = "# Docs\n\n" + ("Current release prose. " * 300)
    sitemap = ("<urlset>"
               + "".join(f"<url><loc>https://d.dev/docs/v3/p{i}</loc></url>"
                         for i in range(3))
               + "</urlset>")
    pages = _site(dump)
    pages["https://d.dev/sitemap.xml"] = FakeResponse(sitemap, ctype="application/xml")
    for i in range(3):
        pages[f"https://d.dev/docs/v3/p{i}"] = FakeResponse(_page(f"P{i}"))

    fetcher = FakeFetcher(pages)
    docs, strategy = df.harvest("https://d.dev/docs/v3/", fetcher=fetcher, stats={})
    assert strategy == "sitemap"
    assert all("/v3/" in d.url for d in docs)


def test_an_unorderable_label_answers_nothing():
    """"latest" and "stable" are moving targets. A file claiming one has
    made no checkable claim, so it cannot satisfy a release request."""
    from docsforge.core import versions as V

    assert V.same_release("1.10", "1.10.4") is True
    assert V.same_release("2", "2.11") is True
    assert V.same_release("1.10", "1.9") is False
    assert V.same_release("1.10", "2.11") is False
    assert V.same_release("1.10", "latest") is False
    assert V.same_release("latest", "1.10") is False


def test_a_less_specific_label_does_not_answer_a_more_specific_request():
    """Found live: a request for Mojo 2.5 matched a stray link to `/2`,
    because the comparison was symmetric. A page filed under the 2.x line
    establishes nothing about 2.5 — the answer has to be at least as
    specific as the question."""
    from docsforge.core import versions as V

    assert V.same_release("2", "2.11") is True, "asking broadly accepts a point release"
    assert V.same_release("2.5", "2") is False, "asking precisely rejects the whole line"
    assert V.same_release("1.10", "1") is False
    assert V.same_release("1.10", "1.10") is True


def test_a_bare_version_segment_is_not_mistaken_for_a_release_directory():
    """The live manifest that surfaced this had a link to `/2` in prose.
    Asking for 2.5 must fall through to the crawl rather than harvest it."""
    body = ("# Docs\n\n> summary\n\n"
            + ("Current release prose. " * 200) + "\n\n"
            + "- [add](https://d.dev/2)\n"
            + "- [guide](https://d.dev/docs/guide/)\n"
            + "- [api](https://d.dev/docs/api/)\n")
    det = df.Detection("llms_txt", "https://d.dev/llms.txt", body)
    opts = df.Options(verbose=False, delay=0.0, version="2.5")
    pathway = df._scope_site_wide_llms("https://d.dev/docs/", det,
                                       FakeFetcher({}), opts)
    assert pathway.skip is True, "nothing here documents 2.5"
    assert pathway.restrict_links is None


# ── A narrowed manifest must not smuggle its own root back in ───────
# Narrowing says "the file as published is not what was asked for; only
# this subset of it is". The root prose is part of what was refused, so
# storing it anyway put the rejected content into the corpus under the
# name of the thing that WAS asked for -- silently undoing the refusal.
def _versioned_hybrid():
    return ("# Docs\n\n> summary\n\n"
            + ("CURRENT RELEASE 2.11 PROSE. " * 60) + "\n\n"
            + "- [Old](https://d.dev/docs/1.9/a.md)\n"
            + "- [Want A](https://d.dev/docs/1.10/a.md)\n"
            + "- [Want B](https://d.dev/docs/1.10/b.md)\n"
            + "- [New](https://d.dev/docs/2.11/a.md)\n")


def test_narrowing_to_a_release_drops_the_current_release_root():
    pages = {
        "https://d.dev/llms.txt": FakeResponse(_versioned_hybrid()),
        "https://d.dev/docs/1.10/a.md": FakeResponse("# A 1.10"),
        "https://d.dev/docs/1.10/b.md": FakeResponse("# B 1.10"),
    }
    opts = df.Options(verbose=False, delay=0.0, version="1.10")
    stats = {}
    docs, _ = df.harvest("https://d.dev/docs/", opts,
                         fetcher=FakeFetcher(pages), stats=stats)

    assert sorted(d.url for d in docs) == ["https://d.dev/docs/1.10/a.md",
                                           "https://d.dev/docs/1.10/b.md"]
    assert not any("CURRENT RELEASE 2.11" in d.markdown for d in docs)
    # The counts follow the documents: no root stored, no root counted.
    assert stats["expected"] == 2
    assert stats["discovered"] == 2
    assert stats["fetched"] == 2
    assert stats["whole"] is True


def test_narrowing_to_a_section_keeps_the_root():
    """Narrowing for a *section* must not discard the file's own prose.

    This asserted the opposite until a live run showed the cost: asking
    mojolang.org for /docs/ narrowed to 212 pages and threw away 1.1 MB
    whose root is literally "Mojo programming language documentation".

    A section narrowing only happens once the file has shown it covers the
    section — one covering none of it is refused outright and never reaches
    here, which is what actually protects the docs.modular.com case, as
    `test_a_site_wide_manifest_covering_another_product_is_refused` shows
    against the real shape of that file (0 of 57 links under /mojo/)."""
    body = ("# The Product\n\n> summary\n\n"
            + ("Overview prose belonging to this very site. " * 60) + "\n\n"
            + "- [Other](https://d.dev/administration/keys/)\n"
            + "- [Mojo A](https://d.dev/mojo/a/)\n"
            + "- [Mojo B](https://d.dev/mojo/b/)\n")
    pages = {
        "https://d.dev/llms.txt": FakeResponse(body),
        "https://d.dev/mojo/a/": FakeResponse(_page("A")),
        "https://d.dev/mojo/b/": FakeResponse(_page("B")),
    }
    docs, _ = df.harvest("https://d.dev/mojo/", fetcher=FakeFetcher(pages), stats={})

    assert sorted(d.url for d in docs) == ["https://d.dev/llms.txt",
                                           "https://d.dev/mojo/a/",
                                           "https://d.dev/mojo/b/"]
    assert any("Overview prose belonging" in d.markdown for d in docs)
    # Out-of-section pages are still never fetched.
    assert not any(d.url.startswith("https://d.dev/administration/") for d in docs)


def test_an_unnarrowed_hybrid_still_keeps_its_root():
    """The root is dropped because it was refused, not because narrowing
    exists. A file taken as published still contributes its own prose."""
    body = ("# Docs\n\n> summary\n\n" + ("Root prose worth keeping. " * 60) + "\n\n"
            + "- [A](https://d.dev/a.md)\n- [B](https://d.dev/b.md)\n"
            + "- [C](https://d.dev/c.md)\n")
    pages = {
        "https://d.dev/llms.txt": FakeResponse(body),
        "https://d.dev/a.md": FakeResponse("# A"),
        "https://d.dev/b.md": FakeResponse("# B"),
        "https://d.dev/c.md": FakeResponse("# C"),
    }
    assert llmsfinder.classify_llms_shape(body) == "hybrid", "the case under test"

    stats = {}
    docs, _ = df.harvest("https://d.dev/llms.txt", fetcher=FakeFetcher(pages), stats=stats)

    assert any(d.url.endswith("/llms.txt") for d in docs), "the root belongs here"
    assert any("Root prose worth keeping" in d.markdown for d in docs)
    assert stats["expected"] == 3
    assert stats["discovered"] == 4, "the root is counted alongside the three links"


# ── The caller's page limit binds this strategy too ─────────────────
# `max_pages` is documented as "set a number only to deliberately cut a
# harvest short". Every strategy honoured it except this one: asking for
# ten pages of a 120-page manifest fetched all 120 and said nothing.
def _manifest_of(n):
    pages = {"https://d.dev/llms.txt":
             FakeResponse("# Index\n\n" + "\n".join(
                 f"- [P{i}](https://d.dev/p{i}.md)" for i in range(n)))}
    for i in range(n):
        pages[f"https://d.dev/p{i}.md"] = FakeResponse(f"# P{i}")
    return pages


def test_a_page_limit_actually_stops_a_manifest_harvest():
    fetcher = FakeFetcher(_manifest_of(120))
    opts = df.Options(verbose=False, delay=0.0, max_pages=10)
    stats = {}
    docs, _ = df.harvest("https://d.dev/llms.txt", opts, fetcher=fetcher, stats=stats)

    assert len(docs) == 10, "the caller asked for ten"
    assert len([u for u in fetcher.asked if u.endswith(".md")]) == 10, "and ten were fetched"


def test_a_truncated_manifest_never_looks_complete():
    """The denominator stays the site's own count, so cutting a harvest
    short shows up as a shortfall instead of as success."""
    fetcher = FakeFetcher(_manifest_of(120))
    opts = df.Options(verbose=False, delay=0.0, max_pages=10)
    stats = {}
    df.harvest("https://d.dev/llms.txt", opts, fetcher=fetcher, stats=stats)

    assert stats["expected"] == 120, "what the site says exists"
    assert stats["acquired"] == 10
    assert stats["whole"] is False
    assert stats["truncated"] is True
    assert stats["remaining"] == 110
    # The shortfall is the caller's own limit, not the site failing.
    assert stats["failed"] == 0
    assert "could not be acquired" not in stats.get("reason", "")


def test_no_page_limit_still_takes_the_whole_manifest():
    fetcher = FakeFetcher(_manifest_of(30))
    opts = df.Options(verbose=False, delay=0.0, max_pages=0)
    stats = {}
    docs, _ = df.harvest("https://d.dev/llms.txt", opts, fetcher=fetcher, stats=stats)

    assert len(docs) == 30
    assert stats["truncated"] is False
    assert stats["whole"] is True


# --- the manifest path is a crawl, and owes the host the same courtesy ------

def test_manifest_acquisition_spaces_its_requests():
    """`opts.delay` was honoured by `_crawl_html` and ignored here.

    A 200-page manifest went out as 200 back-to-back requests to one host.
    """
    import time
    from docsforge.core import engine as df

    seen = []

    class _Clock(df.Fetcher):
        def __init__(self):
            pass

        def text(self, url, timeout=None):
            seen.append(time.monotonic())
            return "# page" + chr(10) * 2 + "body"

    links = [(f"p{i}", f"https://mojolang.org/docs/{i}.md") for i in range(4)]
    docs, failed = df._acquire_manifest_links(
        links, _Clock(), df.Options(delay=0.05))

    assert len(docs) == 4 and not failed
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert all(g >= 0.04 for g in gaps), gaps


def test_manifest_acquisition_does_not_pace_when_no_delay_was_asked_for():
    """The default must stay as fast as it was."""
    import time
    from docsforge.core import engine as df

    class _Instant(df.Fetcher):
        def __init__(self):
            pass

        def text(self, url, timeout=None):
            return "# page" + chr(10) * 2 + "body"

    links = [(f"p{i}", f"https://mojolang.org/docs/{i}.md") for i in range(40)]
    started = time.monotonic()
    docs, _ = df._acquire_manifest_links(links, _Instant(), df.Options(delay=0.0))
    assert len(docs) == 40
    assert time.monotonic() - started < 0.5


# --- a site-wide file is site-wide however we came by its URL ---------------

_ROOT_MANIFEST = (
    "# Mojo" + chr(10) + "version: 1.0.0" + chr(10) * 2 + "## Docs" + chr(10)
    + chr(10).join(f"- [p{i}](https://mojolang.org/docs/p{i})" for i in range(3))
)


def _scope(url, det_url, version=""):
    from docsforge.core import engine as df
    det = df.Detection(kind="llms_txt", url=det_url, body=_ROOT_MANIFEST)
    return df._scope_site_wide_llms(url, det, None, df.Options(version=version))


def test_a_release_request_is_checked_even_when_handed_the_root_file():
    """Resolution now hands `learn_technology` the llms.txt URL itself.

    The guard used to require that *we* had probed for the file, on the
    reasoning that a URL the caller typed is a URL the caller meant. The
    caller stopped typing it, so asking for Mojo 0.9 went straight past the
    release check and took the current manifest whole.
    """
    got = _scope("https://mojolang.org/llms.txt",
                 "https://mojolang.org/llms.txt", version="0.9")
    assert got.skip, "0.9 must not be answered with the current release"


def test_the_release_it_does_declare_is_still_taken_whole():
    got = _scope("https://mojolang.org/llms.txt",
                 "https://mojolang.org/llms.txt", version="1.0")
    assert not got.skip and got.restrict_links is None


def test_no_release_named_still_takes_the_whole_site():
    """The 211-page Mojo harvest goes through here; it must not narrow."""
    got = _scope("https://mojolang.org/llms.txt",
                 "https://mojolang.org/llms.txt")
    assert not got.skip and got.restrict_links is None and not got.drop_root


def test_a_file_below_the_root_is_left_alone():
    """`/en/4.2/llms.txt` is already the file that was asked for. Putting it
    through the release check would narrow, or refuse, the right one."""
    got = _scope("https://d.org/en/4.2/", "https://d.org/en/4.2/llms.txt",
                 version="4.2")
    assert not got.skip and got.restrict_links is None and not got.drop_root
