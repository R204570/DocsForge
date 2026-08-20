"""
Offline tests for name resolution — no network.

The registries and the candidate pages are stubbed, because what needs testing
is the judgement: which candidate wins, what counts as proof that a page
documents a package, and what happens when nothing can be confirmed.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolver
from resolver import Candidate, normalise


class FakeResponse:
    def __init__(self, text="", status=200, ctype="text/html", url=""):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.url = url


class FakeFetcher:
    """Answers from a dict of url -> FakeResponse; 404s anything else."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        hit = self.pages.get(url.rstrip("/")) or self.pages.get(url)
        return hit or FakeResponse("not found", status=404, url=url)

    def text(self, url, **kw):
        return self.get(url).text

    def close(self):
        pass


def registry(payload):
    return FakeResponse(json.dumps(payload), ctype="application/json")


# ── names ────────────────────────────────────────────────
@pytest.mark.parametrize("given,expected", [
    ("effect", "effect"),
    ("Effect.ts", "effect"),
    ("effect-ts", "effect"),
    ("  EFFECT  ", "effect"),
    ("@tanstack/react-query", "react-query"),
    ("Vue.js", "vue"),
    ("drizzle_orm", "drizzle-orm"),
])
def test_names_reduce_to_a_comparable_form(given, expected):
    assert normalise(given) == expected


def test_a_scoped_package_keeps_its_own_name():
    # @scope/pkg is filed under pkg; the scope is the publisher, not the library.
    assert normalise("@effect/platform") == "platform"


# ── scoring ──────────────────────────────────────────────
def test_an_explicit_documentation_field_outranks_a_homepage():
    assert resolver._score("https://docs.x.dev", "documentation") > \
           resolver._score("https://x.dev", "homepage")


def test_a_homepage_that_looks_like_docs_outranks_one_that_does_not():
    assert resolver._score("https://x.dev/docs/", "homepage") > \
           resolver._score("https://x.dev", "homepage")


def test_a_code_host_is_a_repository_whatever_field_it_came_from():
    # Some registries put a GitHub link in `documentation`. Taking that at face
    # value ranks a repo above the project's actual documentation site.
    assert resolver._score("https://github.com/a/b", "documentation") == \
           resolver._score("https://github.com/a/b", "repository")


def test_forges_are_recognised():
    assert resolver.is_forge("https://github.com/a/b")
    assert resolver.is_forge("https://www.gitlab.com/a/b")
    assert not resolver.is_forge("https://docs.pydantic.dev")


# ── probing ──────────────────────────────────────────────
def test_a_repository_origin_is_never_probed():
    """github.com/llms.txt is GitHub's own file.

    Probing a repository's origin offered it as the documentation for whatever
    package happened to be asked about, and it verified, because a big enough
    page mentions everything.
    """
    fetcher = FakeFetcher({"https://github.com/llms.txt": FakeResponse("x", ctype="text/plain")})
    assert resolver.probe_docs_root("https://github.com/Effect-TS/effect", fetcher) == []
    assert fetcher.asked == [], "the forge should not have been touched at all"


def test_a_published_llms_txt_wins_the_probe():
    fetcher = FakeFetcher({
        "https://x.dev/llms.txt": FakeResponse("# x docs", ctype="text/plain",
                                               url="https://x.dev/llms.txt"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url == "https://x.dev/llms.txt"
    assert found[0].confidence >= 0.95


PAGE = "Getting started. " * 40      # enough text to clear the content floor


def test_an_html_docs_root_is_found_when_there_is_no_llms_txt():
    fetcher = FakeFetcher({
        "https://x.dev/docs": FakeResponse(f"<h1>Docs</h1><p>{PAGE}</p>",
                                           url="https://x.dev/docs/"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and "/docs" in found[0].url


def test_an_empty_redirect_shell_is_not_a_docs_root():
    """The measured Astro failure: docs.astro.build answers 200 with 3
    characters of text, and accepting it cost the correct answer — it entered
    the pool, failed verification, and handed the win to the marketing page."""
    fetcher = FakeFetcher({
        "https://x.dev/docs": FakeResponse("<html><body></body></html>",
                                           url="https://x.dev/docs"),
    })
    assert resolver.probe_docs_root("https://x.dev", fetcher) == []


def test_a_client_side_redirect_is_followed_to_the_real_page():
    fetcher = FakeFetcher({
        "https://x.dev/docs": FakeResponse(
            '<meta http-equiv="refresh" content="0; url=/guide/intro">',
            url="https://x.dev/docs"),
        "https://x.dev/guide/intro": FakeResponse(f"<h1>Guide</h1>{PAGE}",
                                                  url="https://x.dev/guide/intro"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url.endswith("/guide/intro")


# ── identity ─────────────────────────────────────────────
def test_repeating_the_name_is_not_proof_of_identity():
    """The F1 failure in miniature.

    A page about *any* project called terraform says "terraform" constantly, so
    counting the word measures the topic and not the project. Three live
    resolutions passed this check and landed on the wrong software.
    """
    fetcher = FakeFetcher({"https://unrelated.example": FakeResponse("effect " * 40)})
    got = resolver.verify(Candidate("https://unrelated.example", "t", 0.5),
                          "effect", fetcher)
    assert got.verified is False
    assert "names-it:40" in got.signals, "the mention count is still reported…"
    assert "not enough to identify" in got.reason, "…just no longer sufficient"


def test_the_projects_own_domain_plus_the_name_identifies_it():
    fetcher = FakeFetcher({"https://effect.dev": FakeResponse("effect " * 40)})
    got = resolver.verify(Candidate("https://effect.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is True
    assert "own-domain" in got.signals


def test_a_forge_url_never_counts_as_the_projects_own_domain():
    fetcher = FakeFetcher({
        "https://github.com/sintaxi/terraform": FakeResponse("terraform " * 40)})
    got = resolver.verify(Candidate("https://github.com/sintaxi/terraform", "t", 0.5),
                          "terraform", fetcher)
    assert got.verified is False
    assert "own-domain" not in got.signals


def test_an_install_line_identifies_the_ecosystem():
    body = "<h1>htmx</h1><pre>npm install htmx</pre>" + "htmx " * 40
    fetcher = FakeFetcher({"https://elsewhere.example": FakeResponse(body)})
    got = resolver.verify(Candidate("https://elsewhere.example", "t", 0.5), "htmx",
                          fetcher, {"ecosystem": "npm"})
    assert "install:npm" in got.signals
    assert got.verified is True


def test_an_install_line_from_the_wrong_ecosystem_is_held_against_it():
    """`npm i htmx` and `cargo add htmx` are different projects sharing a word.
    Resolving htmx landed on a Rust crate; the ecosystems disagreeing is the
    signal that should have stopped it."""
    body = "<h1>htmx</h1><pre>cargo add htmx</pre>" + "htmx " * 40
    fetcher = FakeFetcher({"https://docs.rs/htmx": FakeResponse(body)})
    got = resolver.verify(Candidate("https://docs.rs/htmx", "t", 0.5), "htmx",
                          fetcher, {"ecosystem": "npm"})
    assert got.verified is False
    assert any(s.startswith("install-mismatch") for s in got.signals)


def test_a_backlink_to_the_declared_repository_identifies_it():
    body = ('<a href="https://github.com/honojs/hono">source</a>' + "hono " * 40)
    fetcher = FakeFetcher({"https://elsewhere.example": FakeResponse(body)})
    got = resolver.verify(Candidate("https://elsewhere.example", "t", 0.5), "hono",
                          fetcher, {"repository": "https://github.com/honojs/hono"})
    assert "repo-backlink" in got.signals
    assert got.verified is True


def test_a_projects_own_docs_host_identifies_it_even_when_it_renders_nothing():
    """`docs.astro.build` serves an empty shell and renders client-side.

    Measuring its text rejects it, and rejecting it hands the harvest to
    `astro.build` — whose sitemap is mostly blog posts. Pointing `/docs` at
    `docs.<project>` is a statement about where the documentation lives, and it
    outranks what the index happens to render without JavaScript.
    """
    fetcher = FakeFetcher({"https://docs.astro.build": FakeResponse("<html></html>")})
    got = resolver.verify(Candidate("https://docs.astro.build", "t", 0.7),
                          "astro", fetcher)
    assert got.verified is True
    assert "docs-host" in got.signals


def test_a_third_party_docs_host_is_not_the_projects_own():
    """`docs.rs` is also a "docs." host. It is a Rust crate registry hosting
    somebody else's package, which is exactly how htmx resolved to a crate."""
    fetcher = FakeFetcher({"https://docs.rs/htmx": FakeResponse("htmx " * 40)})
    got = resolver.verify(Candidate("https://docs.rs/htmx", "t", 0.7), "htmx", fetcher)
    assert "docs-host" not in got.signals
    assert got.verified is False


def test_an_empty_page_on_an_unrelated_host_is_still_rejected():
    fetcher = FakeFetcher({"https://x.dev/docs": FakeResponse("<html></html>",
                                                             url="https://x.dev/docs")})
    assert resolver.probe_docs_root("https://x.dev", fetcher) == []


def test_a_docs_subdomain_survives_the_content_floor():
    fetcher = FakeFetcher({
        "https://astro.build/docs": FakeResponse("<html></html>",
                                                 url="https://docs.astro.build/"),
    })
    found = resolver.probe_docs_root("https://astro.build", fetcher)
    assert found and found[0].url == "https://docs.astro.build/"


def test_a_page_that_never_names_it_fails():
    fetcher = FakeFetcher({"https://x.dev": FakeResponse("something else entirely")})
    got = resolver.verify(Candidate("https://x.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is False
    assert got.signals == []
    assert "nothing on the page identifies it" in got.reason


def test_markup_does_not_hide_the_name():
    fetcher = FakeFetcher({"https://effect.dev": FakeResponse(
        "<title>effect</title><h1>effect</h1><code>import effect</code>")})
    got = resolver.verify(Candidate("https://effect.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is True


# ── the chain ────────────────────────────────────────────
DOCS = "pydantic " * 40


def test_resolution_prefers_the_declared_documentation_url():
    fetcher = FakeFetcher({
        "https://pypi.org/pypi/pydantic/json": registry(
            {"info": {"project_urls": {"Documentation": "https://docs.pydantic.dev",
                                       "Source": "https://github.com/pydantic/pydantic"}}}),
        "https://docs.pydantic.dev": FakeResponse(DOCS),
    })
    got = resolver.resolve("pydantic", ecosystem="pypi", fetcher=fetcher)
    assert got.best is not None
    assert got.best.url == "https://docs.pydantic.dev"
    assert got.best.verified is True


def test_the_reported_ecosystem_is_the_one_that_answered():
    # The same name exists in several registries, on different projects. The
    # label has to follow the winner, not whichever replied first.
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/fastapi": registry(
            {"homepage": "https://github.com/someone/fastapi"}),
        "https://pypi.org/pypi/fastapi/json": registry(
            {"info": {"project_urls": {"Documentation": "https://fastapi.tiangolo.com"}}}),
        "https://fastapi.tiangolo.com": FakeResponse("fastapi " * 40),
    })
    got = resolver.resolve("fastapi", fetcher=fetcher)
    assert got.best.url == "https://fastapi.tiangolo.com"
    assert got.ecosystem == "pypi"


def test_nothing_is_returned_as_best_when_nothing_verifies():
    # Better to report failure than to hand back a plausible wrong project.
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/ghost": registry({"homepage": "https://elsewhere.dev"}),
        "https://elsewhere.dev": FakeResponse("a page about something else"),
    })
    got = resolver.resolve("ghost", ecosystem="npm", fetcher=fetcher)
    assert got.best is None
    assert got.candidates, "the candidates it considered are still reported"
    assert "none could be confirmed" in got.note


def test_an_unknown_package_says_so_and_suggests_a_url():
    fetcher = FakeFetcher({})
    got = resolver.resolve("not-a-real-package-xyz", fetcher=fetcher)
    assert got.best is None and not got.candidates
    assert "harvest_docs" in got.note
