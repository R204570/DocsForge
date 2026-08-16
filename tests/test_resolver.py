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


def test_an_html_docs_root_is_found_when_there_is_no_llms_txt():
    fetcher = FakeFetcher({
        "https://x.dev/docs": FakeResponse("<h1>Docs</h1>", url="https://x.dev/docs/"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and "/docs" in found[0].url


# ── verification ─────────────────────────────────────────
def test_a_page_that_repeats_the_name_verifies():
    fetcher = FakeFetcher({"https://x.dev": FakeResponse("effect effect effect")})
    got = resolver.verify(Candidate("https://x.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is True


def test_one_passing_mention_is_not_proof():
    # Every page on the internet says "effect" once. Accepting that is how a
    # resolver becomes an elaborate guess.
    fetcher = FakeFetcher({"https://x.dev": FakeResponse("this has some effect on things")})
    got = resolver.verify(Candidate("https://x.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is False
    assert "too weak" in got.reason


def test_a_page_that_never_names_it_fails():
    fetcher = FakeFetcher({"https://x.dev": FakeResponse("something else entirely")})
    got = resolver.verify(Candidate("https://x.dev", "t", 0.5), "effect", fetcher)
    assert got.verified is False
    assert "never mentions" in got.reason


def test_markup_does_not_hide_the_name():
    fetcher = FakeFetcher({"https://x.dev": FakeResponse(
        "<title>effect</title><h1>effect</h1><code>import effect</code>")})
    got = resolver.verify(Candidate("https://x.dev", "t", 0.5), "effect", fetcher)
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
