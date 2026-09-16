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

from docsforge.core import resolver
from docsforge.core.resolver import Candidate, normalise


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


# --- the release follows the winner, exactly as the ecosystem does -----------
#
# Measured live, 2026-09-16, twice. `learn_technology("click")` resolved to
# click.palletsprojects.com — the real PyPI project — and stored it as version
# **0.1.0**: the `dist-tags.latest` of an unrelated npm package that shares the
# word. PyPI's click was at 8.5.0. `result.release` was taken from the first
# candidate in registry-query order with any release at all, npm's, before
# verification had chosen anything, and never revisited. The ecosystem beside
# it got exactly that correction the day this bug was found for *it* — with a
# comment stating the principle — and the release did not.

def _colliding(name="click"):
    """The same word on two registries: an npm package nobody asked for at
    0.1.0, and the PyPI project that verifies at 9.9.9."""
    return FakeFetcher({
        f"https://registry.npmjs.org/{name}": registry(
            {"dist-tags": {"latest": "0.1.0"},
             "repository": {"url": f"git+https://github.com/someone/{name}.git"}}),
        f"https://pypi.org/pypi/{name}/json": registry(
            {"info": {"version": "9.9.9",
                      "project_urls": {"Documentation": f"https://{name}.example.dev/"}}}),
        f"https://{name}.example.dev/": FakeResponse(
            f"<h1>{name}</h1><pre>pip install {name}</pre>" + f"{name} " * 40,
            url=f"https://{name}.example.dev/"),
    })


def test_the_release_is_the_winning_registrys_not_the_first_to_answer():
    got = resolver.resolve("click", fetcher=_colliding())
    assert got.best is not None and got.best.url == "https://click.example.dev/"
    assert got.ecosystem == "pypi"
    assert got.release == "9.9.9", got.release


def test_a_probed_docs_root_carries_its_registrys_release():
    """PyPI names a homepage, DocsForge finds /llms.txt beneath it, and that
    is the winner. It is still PyPI's project."""
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/zorp": registry(
            {"dist-tags": {"latest": "0.1.0"},
             "repository": {"url": "git+https://github.com/someone/zorp.git"}}),
        "https://pypi.org/pypi/zorp/json": registry(
            {"info": {"version": "9.9.9",
                      "project_urls": {"Homepage": "https://zorp.example.dev/"}}}),
        "https://zorp.example.dev/llms.txt": FakeResponse(
            "# zorp " + "zorp " * 40, ctype="text/plain",
            url="https://zorp.example.dev/llms.txt"),
    })
    got = resolver.resolve("zorp", fetcher=fetcher)
    assert got.best is not None and got.best.url == "https://zorp.example.dev/llms.txt"
    assert got.release == "9.9.9", got.release


def test_a_probed_docs_root_is_still_not_a_registry_nomination():
    """Carrying the release must not carry the `pypi:` prefix with it — that
    prefix means "a registry nominated this exact URL" to `_path_identity`,
    and a path DocsForge guessed is not that."""
    fetcher = FakeFetcher({
        "https://pypi.org/pypi/zorp/json": registry(
            {"info": {"version": "9.9.9",
                      "project_urls": {"Homepage": "https://zorp.example.dev/"}}}),
        "https://zorp.example.dev/llms.txt": FakeResponse(
            "# zorp " + "zorp " * 40, ctype="text/plain",
            url="https://zorp.example.dev/llms.txt"),
    })
    got = resolver.resolve("zorp", ecosystem="pypi", fetcher=fetcher)
    assert got.best.source.startswith("probe:"), got.best.source


def test_a_pip_install_line_is_not_held_against_the_pypi_project():
    """Found writing the fixture above. `install-mismatch` is a veto below
    two strong signals, and it fired on the real project's own `pip install`
    line because the ecosystem it was judged against was npm's — the first
    registry to answer. A smaller project than click, with only its domain
    and its install line to show, was refused outright for sharing a word
    with an npm package."""
    got = resolver.resolve("click", fetcher=_colliding())
    assert got.best is not None
    assert "install:pypi" in got.best.signals, got.best.signals
    assert not any(s.startswith("install-mismatch") for s in got.best.signals)


def test_a_candidate_is_judged_against_its_own_registrys_repository():
    """`repo-backlink` compares the page against the repository the registry
    declared. Pooled first-wins, that was npm's repository for a PyPI page,
    so the real project could never earn it for linking to its own source."""
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/zorp": registry(
            {"dist-tags": {"latest": "0.1.0"},
             "repository": {"url": "git+https://github.com/squatter/zorp.git"}}),
        "https://pypi.org/pypi/zorp/json": registry(
            {"info": {"version": "9.9.9",
                      "project_urls": {"Documentation": "https://zorp-docs.example/",
                                       "Source": "https://github.com/real/zorp"}}}),
        "https://zorp-docs.example/": FakeResponse(
            '<a href="https://github.com/real/zorp">source</a>' + "zorp " * 40,
            url="https://zorp-docs.example/"),
    })
    got = resolver.resolve("zorp", fetcher=fetcher)
    docs = next(c for c in got.candidates if c.url == "https://zorp-docs.example/")
    assert "repo-backlink" in docs.signals, docs.signals


def test_a_probe_winner_corrects_the_ecosystem_too():
    """The ecosystem correction keyed on the source prefix, so a `probe:`
    winner beneath a PyPI homepage left the ecosystem at whichever registry
    answered first."""
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/zorp": registry(
            {"dist-tags": {"latest": "0.1.0"},
             "repository": {"url": "git+https://github.com/someone/zorp.git"}}),
        "https://pypi.org/pypi/zorp/json": registry(
            {"info": {"version": "9.9.9",
                      "project_urls": {"Homepage": "https://zorp.example.dev/"}}}),
        "https://zorp.example.dev/llms.txt": FakeResponse(
            "# zorp " + "zorp " * 40, ctype="text/plain",
            url="https://zorp.example.dev/llms.txt"),
    })
    got = resolver.resolve("zorp", fetcher=fetcher)
    assert got.best.source.startswith("probe:")
    assert got.ecosystem == "pypi"


def test_release_from_never_borrows_another_registrys_number():
    found = [Candidate("https://a", "npm:homepage", 0.5, release="0.1.0", registry="npm"),
             Candidate("https://b", "pypi:Documentation", 0.9, release="9.9.9",
                       registry="pypi")]
    assert resolver.release_from(found, "pypi") == "9.9.9"
    assert resolver.release_from(found, "npm") == "0.1.0"
    assert resolver.release_from(found, "crates") == ""
    assert resolver.release_from(found, "") == ""


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


# ── a refusal caused by the network is not a fact about the name ────
# Found live: on a NAT64 network every candidate for "mojo" was refused as
# a "private address", so six were found and none could be read. That
# refusal was cached for REJECT_TTL -- seven days of confident wrong
# answers for a cause fixed the same day.
def _refusal(reasons):
    got = resolver.Resolution(name="mojo", ecosystem="pypi")
    got.candidates = [
        resolver.Candidate(f"https://x.dev/{i}", "registry", 0.8, "e", False, r)
        for i, r in enumerate(reasons)
    ]
    return got


def test_a_refusal_where_nothing_could_be_read_is_not_remembered():
    unreachable = _refusal([
        "could not be read: Refusing to fetch private/loopback address: x.dev",
        "could not be read: HTTP 000 for https://x.dev/1",
    ])
    assert resolver.learned_nothing(unreachable) is True


def test_a_refusal_reached_by_actually_reading_the_pages_is_remembered():
    """This one IS a finding: the candidates were fetched and did not
    document the package. Forgetting it would re-crawl them every time."""
    checked = _refusal(["never mentions it", "names a different project"])
    assert resolver.learned_nothing(checked) is False


def test_a_partly_unreachable_refusal_is_still_remembered():
    """If even one candidate was actually read, the run learned something."""
    mixed = _refusal(["could not be read: timeout", "never mentions it"])
    assert resolver.learned_nothing(mixed) is False


def test_a_successful_resolution_is_always_remembered():
    got = resolver.Resolution(name="mojo")
    cand = resolver.Candidate("https://x.dev", "registry", 0.9, "e", True, "names it")
    got.candidates, got.best = [cand], cand
    assert resolver.learned_nothing(got) is False


def test_remember_files_nothing_when_nothing_was_learned(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "_load_cache", lambda: {})
    saved = {}
    monkeypatch.setattr(resolver, "_save_cache", lambda d: saved.update(d))

    resolver.remember("mojo", _refusal(["could not be read: refused"]))
    assert saved == {}, "an unreachable run must leave the cache untouched"

    resolver.remember("mojo", _refusal(["never mentions it"]))
    assert "mojo" in saved, "a real refusal is still filed"


# ── a guessed domain that does not exist is a miss, not a crash ─────
# Live on Vercel, 2026-09-15: `find_docs("markdownify")` and
# `learn_technology` answered `OSError: [Errno 16] Device or resource busy
# (at engine.py:396 in _resolves_private)` at 0s, in resolution, before any
# fetch — while `detect_source_type` on a real host worked. That runtime
# reports a name it cannot resolve as EAI_SYSTEM, a plain OSError, and the
# guard caught only gaierror; the domain lap's first nonexistent guess took
# the whole resolution down with it.
def test_a_guessed_domain_that_does_not_exist_is_skipped_not_fatal(monkeypatch):
    import requests
    from docsforge.core import engine

    def busy(host, *args, **kwargs):
        raise OSError(16, "Device or resource busy")
    monkeypatch.setattr(engine.socket, "getaddrinfo", busy)

    fetcher = engine.Fetcher(engine.Options(delay=0.0, verbose=False,
                                            allow_private=False))
    asked = []

    def unreachable(url, **kwargs):
        # What requests says when its own lookup fails the same way.
        asked.append(url)
        raise requests.ConnectionError(
            f"Failed to establish a new connection to {url}: "
            f"[Errno 16] Device or resource busy")
    monkeypatch.setattr(fetcher.session, "get", unreachable)

    try:
        got = resolver._probe_origins(
            [("org", "https://markdownify.org"), ("dev", "https://markdownify.dev")],
            "markdownify", fetcher)
    finally:
        fetcher.close()
    assert got == []
    assert asked == ["https://markdownify.org", "https://markdownify.dev"],         "every guess was tried; the first miss did not end the lap"


# --- A redirect onto a code host is not an ownership claim -------------------
#
# Live regression. `mojo.dev` redirects onto `github.com/gdejohn/procrastination`
# - a Java library, since Maven plugins are also called "mojos". The domain
# probe recorded "we got here from mojo.dev", `identity_signals` turned that
# into `own-domain` without looking at where it landed, a registry package
# named `mojo` supplied the second strong signal, and the gate stamped
# `verified` on a page that never says the word "mojo" at all.

def _via_domain(url, name="mojo", body=""):
    return resolver.identity_signals(
        Candidate(url, "domain:dev", 0.75, ""), name, body, {"via_domain": True})


def test_a_domain_redirecting_onto_a_forge_does_not_own_the_name():
    signals = _via_domain("https://github.com/gdejohn/procrastination")
    assert "own-domain" not in signals


def test_forges_are_refused_however_we_arrived_at_them():
    for url in ("https://gitlab.com/someone/mojo",
                "https://bitbucket.org/someone/mojo",
                "https://raw.githubusercontent.com/x/mojo/main/README.md"):
        assert "own-domain" not in _via_domain(url), url


def test_a_domain_that_redirects_off_itself_still_owns_the_name():
    """The case the rule exists for: terraform.io lands on hashicorp's host."""
    signals = _via_domain("https://developer.hashicorp.com/terraform",
                          name="terraform")
    assert "own-domain" in signals


def test_a_repo_page_is_still_reachable_by_its_own_evidence():
    """Denying `own-domain` must not deny the candidate outright.

    A repo page can still be identified - it just has to say so itself rather
    than inherit the claim from a redirect it had no part in.
    """
    signals = _via_domain("https://github.com/modular/mojo",
                          body="mojo " * (resolver.MIN_MENTIONS + 2))
    assert "own-domain" not in signals
    assert any(s.startswith("names-it") for s in signals)


def test_the_wrong_mojo_no_longer_clears_the_identity_gate():
    """Exactly the signals the live run produced, minus the one it should not."""
    assert resolver.is_identified(["own-domain", "registry-agreement"])
    assert not resolver.is_identified(["registry-agreement"])


def test_a_language_owns_its_lang_suffixed_domain():
    """The suffix exists because the bare name is a common word.

    Every one of these was refused before, so Go, Rust, Julia, Nim, Crystal,
    Elixir and Mojo could not claim the domain each of them publishes from.
    """
    for host, name in (("mojolang.org", "mojo"), ("golang.org", "go"),
                       ("rust-lang.org", "rust"), ("julialang.org", "julia"),
                       ("nim-lang.org", "nim"), ("crystal-lang.org", "crystal"),
                       ("elixir-lang.org", "elixir")):
        assert resolver._owns_the_name(f"https://{host}/docs/", name), host


def test_the_lang_suffix_is_the_only_one_allowed():
    """A prefix match would hand `mojo` to anything starting with it."""
    for host in ("mojoportal.org", "mojolicious.org", "mojo-tools.com",
                 "gopher.org", "rustacean.net"):
        for name in ("mojo", "go", "rust"):
            assert not resolver._owns_the_name(f"https://{host}/", name), host


def test_mojos_own_docs_now_clear_the_gate_and_the_npm_package_does_not():
    """The live outcome, as signals: 0.92 unverified became the answer."""
    real = resolver.identity_signals(
        Candidate("https://mojolang.org/docs/", "domain:org", 0.92, ""),
        "mojo", "mojo " * (resolver.MIN_MENTIONS + 2), {"via_domain": True})
    assert "own-domain" in real
    assert resolver.is_identified(real)

    # `github.com/classdojo/mojo.js` reached the gate on registry-agreement
    # plus a handful of mentions. One strong signal and the name is the bar,
    # so this still passes - which is why owning the domain has to outrank it.
    assert resolver._owns_the_name("https://mojolang.org/docs/", "mojo")
    assert not resolver._owns_the_name("https://github.com/classdojo/mojo.js",
                                       "mojo")


# --- a fix has to reach the cache too ---------------------------------------

def test_an_entry_decided_under_older_rules_is_not_recalled(tmp_path, monkeypatch):
    """The wrong answer for `mojo` was filed as a success, TTL thirty days."""
    cache = tmp_path / "resolve.json"
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(cache))

    result = resolver.Resolution(name="mojo")
    result.best = Candidate("https://github.com/gdejohn/procrastination",
                            "domain:dev", 0.75, "", True, "identified by ...")
    result.candidates = [result.best]
    resolver.remember("mojo", result)
    assert resolver.recall("mojo") is not None

    monkeypatch.setattr(resolver, "RULES", resolver.RULES + 1)
    assert resolver.recall("mojo") is None


def test_an_entry_written_before_the_stamp_existed_is_discarded(tmp_path,
                                                               monkeypatch):
    """Every cache in the wild predates it, and every one of them is stale."""
    import json
    import time
    cache = tmp_path / "resolve.json"
    cache.write_text(json.dumps({"mojo": {
        "at": time.time(), "url": "https://github.com/gdejohn/procrastination",
        "evidence": "", "reason": "", "signals": [], "ecosystem": "",
        "resolved_via": "domain", "note": "",
    }}), encoding="utf-8")
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(cache))
    assert resolver.recall("mojo") is None


def test_a_fresh_entry_under_the_current_rules_is_recalled(tmp_path, monkeypatch):
    """The stamp must not break the cache it is protecting."""
    cache = tmp_path / "resolve.json"
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(cache))

    result = resolver.Resolution(name="mojo")
    result.best = Candidate("https://mojolang.org/docs/", "domain:org", 0.92,
                            "", True, "identified by own-domain")
    result.candidates = [result.best]
    resolver.remember("mojo", result)

    got = resolver.recall("mojo")
    assert got is not None and got.best.url == "https://mojolang.org/docs/"


# --- languages publish on a `lang` domain, and nothing tried one ------------

def _origins_probed_for(name, monkeypatch):
    captured = []

    def _spy(origins, slug, fetcher, state=None):
        captured.extend(origins)
        return []

    monkeypatch.setattr(resolver, "_probe_origins", _spy)
    resolver.from_domains(name, fetcher=None)
    return [url for _label, url in captured]


def test_from_domains_probes_the_lang_suffixed_shapes(monkeypatch):
    """`zig` resolved to an npm templating library because nothing tried
    ziglang.org, and `nim` to an unrelated repository for the same reason."""
    probed = _origins_probed_for("zig", monkeypatch)
    assert "https://ziglang.org" in probed
    assert "https://zig-lang.org" in probed
    assert "https://zig.dev" in probed, "the plain shapes must survive"


def test_the_hyphenated_lang_shape_is_probed_too(monkeypatch):
    """nim-lang.org and rust-lang.org carry the hyphen; golang.org does not."""
    assert "https://nim-lang.org" in _origins_probed_for("nim", monkeypatch)
    assert "https://rust-lang.org" in _origins_probed_for("rust", monkeypatch)


def test_a_name_already_ending_in_lang_is_not_doubled(monkeypatch):
    probed = _origins_probed_for("golang", monkeypatch)
    assert not any("golanglang" in url for url in probed), probed
    assert "https://golang.org" in probed


def test_the_lang_shapes_cost_four_probes(monkeypatch):
    """Two shapes over two TLDs. Nearly every one of them sits on .org, so a
    full NAME_TLDS spread would be latency for nothing."""
    probed = _origins_probed_for("zig", monkeypatch)
    assert len(probed) == len(resolver.NAME_TLDS) + 4


# --- choosing between candidates that all verify -----------------------------
#
# `resolve()` used to stop at the first candidate that passed, walking in
# confidence order — and confidence there is a prior about the *source type*,
# decided before anything was read. `pypi:Documentation` outranks
# `pypi:Homepage`, so `langchain` resolved to `reference.langchain.com` (an API
# symbol index: 560 pages, median 490 characters, one attribute per page) while
# `docs.langchain.com` sat second at 0.78 and was never even checked.

def _cand(url, signals, confidence=0.5, source="pypi:Homepage"):
    c = Candidate(url, source, confidence, "", True, "")
    c.signals = list(signals)
    return c


def test_the_projects_own_docs_host_beats_its_reference_site():
    """The live case, as the signals it actually produced."""
    reference = _cand("https://reference.langchain.com/python/langchain/langchain/",
                      ["own-domain", "names-it:11"], 0.92, "pypi:Documentation")
    docs = _cand("https://docs.langchain.com/", ["own-domain", "docs-host"], 0.78)

    assert resolver.best_verified([reference, docs]) is docs
    assert resolver.best_verified([docs, reference]) is docs, "order must not decide"


def test_a_source_repository_never_outranks_a_documentation_site():
    """Counting signals was tried first and was worse: a repo page is dense
    with the name and carries a backlink, so it won on raw totals."""
    repo = _cand("https://github.com/langchain-ai/langchainjs/tree/main/libs/langchain/",
                 ["repo-backlink", "registry-agreement", "names-it:18"], 0.35)
    docs = _cand("https://docs.langchain.com/", ["own-domain", "docs-host"], 0.78)

    assert resolver.best_verified([repo, docs]) is docs
    assert resolver.evidence(repo) < resolver.evidence(docs)


def test_a_repository_still_wins_when_it_is_all_there_is():
    """Many small libraries really do document themselves in a README.
    Demotion must not become refusal."""
    repo = _cand("https://github.com/someone/tiny", ["repo-identity", "names-it:9"])
    assert resolver.best_verified([repo]) is repo


def test_owning_the_domain_beats_not_owning_it():
    theirs = _cand("https://someblog.example/htmx-guide", ["install:npm", "names-it:30"])
    ours = _cand("https://htmx.org/", ["own-domain", "names-it:12"])
    assert resolver.best_verified([theirs, ours]) is ours


def test_mentions_only_break_a_tie_between_equals():
    quiet = _cand("https://a.dev/", ["own-domain", "names-it:3"])
    loud = _cand("https://b.dev/", ["own-domain", "names-it:40"])
    assert resolver.best_verified([quiet, loud]) is loud


def test_nothing_verified_means_no_answer():
    a = Candidate("https://a.dev/", "pypi:Homepage", 0.9, "", False, "no")
    b = Candidate("https://b.dev/", "pypi:Homepage", 0.8, "", None, "")
    assert resolver.best_verified([a, b]) is None


# --- a generated symbol reference is the wrong half of the documentation -----

def test_a_reference_subdomain_is_recognised_without_a_fetch():
    for url in ("https://reference.langchain.com/python/langchain/",
                "https://api.example.com/v2/",
                "https://apidocs.example.io/",
                "https://javadoc.example.org/",
                "https://pkg.go.dev/net/http"):
        assert resolver.is_reference_site(url), url


def test_a_reference_path_counts_too():
    assert resolver.is_reference_site("https://x.dev/reference/widgets")
    assert resolver.is_reference_site("https://x.dev/api-reference")


def test_ordinary_documentation_is_not_mistaken_for_a_reference():
    for url in ("https://docs.langchain.com/", "https://mojolang.org/docs/",
                "https://htmx.org/docs/", "https://x.dev/guide/intro",
                "https://apify.com/", "https://api-platform.com/"):
        assert not resolver.is_reference_site(url), url


def test_the_reference_site_loses_to_the_projects_own_docs():
    """What the user asked for: the reference must not win just because it
    ranks well on everything else."""
    reference = _cand("https://reference.langchain.com/python/langchain/",
                      ["own-domain", "names-it:40"], 0.92, "pypi:Documentation")
    docs = _cand("https://docs.langchain.com/", ["own-domain", "docs-host"], 0.78)
    assert resolver.best_verified([reference, docs]) is docs


def test_the_reference_loses_to_a_plain_site_that_docs_host_cannot_separate():
    """The case `docs-host` cannot reach: reference on a subdomain, guides on
    the bare name. Neither is a `docs.` host, so the tier below decides."""
    reference = _cand("https://reference.thing.dev/", ["own-domain", "names-it:50"])
    site = _cand("https://thing.dev/", ["own-domain", "names-it:12"])
    assert resolver.best_verified([reference, site]) is site


def test_a_reference_still_wins_when_it_is_the_only_thing_verified():
    """Demotion, never refusal — a signature is exactly what you want when
    you need a signature."""
    only = _cand("https://reference.thing.dev/", ["own-domain", "names-it:9"])
    assert resolver.best_verified([only]) is only


# --- graded authority over the name: the 2026-09-10 five ---------------------
#
# Every case below is a measured resolution from that run, with the signals
# each candidate actually produced. All of them lost to a page that was not the
# project's documentation, and they lost the same way: `own-domain` was one
# bit, so ranking fell through to counting strong signals and a second signal
# outweighed being the project's own site.

def test_a_github_pages_label_never_outranks_the_projects_own_domain():
    """Measured: `tensorflow` resolved to `tensorflow.github.io/rust/tensorflow`
    -- a page titled "tensorflow - Rust" whose description reads "Rust bindings
    for the TensorFlow machine learning library". `www.tensorflow.org/guide/`
    was fetched, verified, and lost 2 strong signals to 1."""
    bindings = _cand("https://tensorflow.github.io/rust/tensorflow",
                     ["own-domain", "repo-identity", "names-it:14"], 0.92,
                     "crates:documentation")
    official = _cand("https://www.tensorflow.org/guide/",
                     ["own-domain", "names-it:17"], 0.70, "probe:/guide/")

    assert resolver.best_verified([bindings, official]) is official
    assert resolver.name_authority(bindings) == resolver.SHARED_LABEL
    assert resolver.name_authority(official) == resolver.APEX_DOMAIN


def test_a_country_code_mirror_never_outranks_the_projects_own_domain():
    """Measured: `pytorch` resolved to `pytorch.kr`, the Korean user group, and
    stored 66 pages of Korean. It qualified because it carries a `pip install`
    line; `pytorch.org` names PyTorch 129 times and had one strong signal."""
    korean = _cand("https://pytorch.kr/",
                   ["own-domain", "install:pypi", "names-it:37"], 0.75, "domain:io")
    official = _cand("https://pytorch.org/llms.txt/",
                     ["own-domain", "names-it:129"], 0.97,
                     "domain:dev/probe:llms.txt")

    assert resolver.best_verified([korean, official]) is official
    assert resolver.name_authority(korean) == resolver.LOCAL_DOMAIN
    assert resolver.name_authority(official) == resolver.APEX_DOMAIN


def test_a_local_mirror_is_still_an_answer_when_it_is_the_only_one():
    """Demotion must not become refusal -- the same rule the repository case
    already holds to. A national community site is real documentation when the
    project's own domain produced nothing at all."""
    korean = _cand("https://pytorch.kr/", ["own-domain", "install:pypi"], 0.75)
    assert resolver.best_verified([korean]) is korean


def test_a_shared_namespace_label_is_still_an_answer_on_its_own():
    """Plenty of real projects publish their documentation on `<name>.github.io`
    and must keep resolving there."""
    pages = _cand("https://someproject.github.io/", ["own-domain", "names-it:20"])
    assert resolver.best_verified([pages]) is pages
    assert resolver.name_authority(pages) == resolver.SHARED_LABEL


def test_a_host_a_name_domain_redirected_onto_keeps_apex_authority():
    """`terraform.io` redirects to `developer.hashicorp.com/terraform`, which is
    somebody consolidating their documentation. The host does not carry the
    name and does not need to: `own-domain` is granted by the arrival."""
    landed = _cand("https://developer.hashicorp.com/terraform",
                   ["own-domain", "names-it:40"], 0.75, "domain:io")
    assert resolver.name_authority(landed) == resolver.APEX_DOMAIN


def test_a_package_host_makes_no_claim_on_the_name():
    """`docs.rs/htmx` carries the name in its path and is a registry serving
    somebody else's crate -- the same reason `_owns_the_name` refuses it a
    hostname claim."""
    hosted = _cand("https://docs.rs/htmx", ["names-it:12"], 0.9,
                   "crates:documentation")
    assert resolver.name_authority(hosted) == resolver.NO_CLAIM
    assert resolver._path_identity(hosted, "htmx") == ""


# --- a sub-project documented on its parent's domain -------------------------

def test_a_registry_nominated_path_identifies_a_sub_project():
    """Measured: `langgraph` is documented at
    `docs.langchain.com/oss/python/langgraph/overview`. The host carries
    `langchain`, so the page earned neither `own-domain` nor `docs-host` and was
    refused with "only registry-agreement" -- while the source tree passed on
    `repo-identity` and the harvest stored a 6,350-character README."""
    nominated = Candidate("https://docs.langchain.com/oss/python/langgraph/overview",
                          "pypi:Homepage", 0.78)
    assert resolver._path_identity(nominated, "langgraph") == "langgraph"
    assert resolver.is_identified(["registry-agreement", "path-identity"])


def test_a_path_name_alone_identifies_nothing():
    """The signal is the registry nomination *and* the path together. A name in
    a path with nobody vouching for the URL is what makes
    `github.com/sintaxi/terraform` look like Terraform."""
    unvouched = Candidate("https://someblog.example/langgraph/tutorial",
                          "probe:/docs/", 0.5)
    assert resolver._path_identity(unvouched, "langgraph") == ""


def test_a_project_suffix_domain_is_the_projects_own():
    """`djangoproject.com` is Django's own domain. Refusing it the claim meant
    `docs.djangoproject.com` earned no `docs-host`, so `django` resolved to
    `www.djangoproject.com` and harvested 214 weblog posts as version 5.2."""
    assert resolver._owns_the_name("https://docs.djangoproject.com/", "django")
    assert resolver._owns_the_name("https://www.djangoproject.com/", "django")
    # The suffix list stays short on purpose.
    assert not resolver._owns_the_name("https://mojoportal.org/", "mojo")


def test_the_django_docs_host_outranks_the_django_homepage():
    docs = _cand("https://docs.djangoproject.com/",
                 ["own-domain", "docs-host"], 0.92, "pypi:Documentation")
    home = _cand("https://www.djangoproject.com/",
                 ["own-domain", "repo-identity", "names-it:47"], 0.78,
                 "pypi:Homepage")
    assert resolver.best_verified([docs, home]) is docs


# --- a published llms.txt that is a newsroom ---------------------------------

def test_an_llms_txt_of_articles_is_not_a_documentation_root():
    """`pytorch.org/llms.txt` is 8 KB under a `## Posts` heading: conference
    announcements, newsletters, venues and organisers -- 52% of its 60 links are
    articles and none is documentation. Taking it as the docs root also stopped
    `/docs/` ever being probed, because that loop breaks on a 0.95."""
    body = "# PyTorch\n\n## Posts\n" + "\n".join(
        f"- [Announcement {i}](https://pytorch.org/blog/announcement-{i}/): news"
        for i in range(20))
    assert resolver._indexes_only_articles(body, "https://pytorch.org/llms.txt")


def test_a_documentation_manifest_without_docs_paths_is_still_documentation():
    """Measured against the manifests actually in use: Prisma and LangChain both
    publish real documentation manifests with *no* `/docs/` paths, because they
    are already on a documentation host. Asking whether a manifest links to
    docs-shaped paths rejects both; asking whether it links to articles does
    not."""
    body = "# Prisma\n\n" + "\n".join(
        f"- [ORM page {i}](https://www.prisma.io/orm/page-{i}): guide"
        for i in range(14))
    assert not resolver._indexes_only_articles(body, "https://www.prisma.io/llms.txt")


def test_a_manifest_with_a_few_posts_is_still_a_documentation_manifest():
    """0.3, not 0. A documentation manifest that also lists a changelog and two
    blog posts is still a documentation manifest."""
    body = "# Docs\n\n" + "\n".join(
        f"- [Guide {i}](https://x.dev/guide/{i})" for i in range(9)
    ) + "\n" + "\n".join(
        f"- [Post {i}](https://x.dev/blog/{i})" for i in range(2))
    assert not resolver._indexes_only_articles(body, "https://x.dev/llms.txt")


# ── the registry's current release rides along ───────────
class _Scripted:
    """A fetcher whose one JSON answer is scripted."""

    def __init__(self, body):
        self.body = body

    def get(self, url, **kw):
        import json
        return type("R", (), {"status_code": 200, "text": json.dumps(self.body)})()


def test_each_registry_reports_its_current_release():
    npm = resolver._npm("angular", _Scripted({"dist-tags": {"latest": "20.2.1"}, "homepage": "https://angular.dev/"}))
    pypi = resolver._pypi("pydantic", _Scripted({"info": {"version": "2.11.7",
                                                          "project_urls": {"Documentation": "https://docs.pydantic.dev/"}}}))
    crates = resolver._crates("serde", _Scripted({"crate": {"max_stable_version": "1.0.219",
                                                           "documentation": "https://docs.rs/serde"}}))
    assert [c.release for c in npm] == ["20.2.1"]
    assert [c.release for c in pypi] == ["2.11.7"]
    assert [c.release for c in crates] == ["1.0.219"]


def test_a_registry_without_a_release_leaves_it_blank():
    npm = resolver._npm("x", _Scripted({"homepage": "https://x.dev/"}))
    assert npm and npm[0].release == ""


# --- one page, one candidate -------------------------------------------------
#
# Measured live, 2026-09-16: `find_docs("pydantic")` printed both candidates
# twice with identical confidence and evidence, and fetched pydantic.dev seven
# times for one resolution. `pydantic.io` 301s onto `pydantic.dev`, so two
# origins landed on the same page and `_probe_origins` explored each one whole
# — probing `/llms.txt`, then verifying both copies of every candidate it
# produced. Nothing was wrong with the answer; it was said twice and paid for
# twice.

#: Long enough to clear the two-character floor in `from_domains`.
ALIASED = "zorp"


def _redirecting_pair():
    """Two origins for one site: `zorp.io` lands on `zorp.dev`."""
    page = ('<h1>zorp</h1><pre>pip install zorp</pre>'
            '<a href="https://github.com/zorp/zorp">source</a>' + ("zorp " * 60))
    return FakeFetcher({
        "https://zorp.dev": FakeResponse(page, url="https://zorp.dev/"),
        "https://zorp.io": FakeResponse(page, url="https://zorp.dev/"),
        "https://zorp.dev/llms.txt": FakeResponse("# zorp docs\n" + "zorp " * 60,
                                                  ctype="text/plain",
                                                  url="https://zorp.dev/llms.txt"),
    })


def test_two_names_landing_on_one_site_produce_one_set_of_candidates():
    fetcher = _redirecting_pair()
    found = resolver.from_domains(ALIASED, fetcher)
    assert len(found) == len({c.url for c in found}), [c.url for c in found]


def test_a_redirected_alias_is_not_probed_a_second_time():
    fetcher = _redirecting_pair()
    resolver.from_domains(ALIASED, fetcher)
    probes = [u for u in fetcher.asked if u == "https://zorp.dev/llms.txt"]
    assert len(probes) == 1, fetcher.asked


def test_the_origin_that_did_not_redirect_is_the_one_kept():
    """The sort tiebreaks on the label, so `io` beat `dev` on the letter `i`
    and the candidate came back sourced `domain:io`, claiming that pydantic.dev
    redirects onto itself. The redirect target is the canonical name."""
    fetcher = _redirecting_pair()
    home = [c for c in resolver.from_domains(ALIASED, fetcher) if c.confidence == 0.75]
    assert home and home[0].source == "domain:dev", home[0].source
    assert "zorp.io redirects onto it" in home[0].evidence


def test_a_collapsed_alias_is_still_reported_as_evidence():
    """Two names pointing at one site is a stronger ownership claim than one,
    so collapsing them must not silently drop the second."""
    fetcher = _redirecting_pair()
    home = [c for c in resolver.from_domains(ALIASED, fetcher) if c.confidence == 0.75]
    assert "zorp.io" in home[0].evidence


def test_dedupe_keeps_the_first_and_merges_what_the_rest_knew():
    first = Candidate("https://x.dev/docs", "domain:dev", 0.80, "found by domain")
    second = Candidate("https://x.dev/docs/", "pypi:Documentation", 0.92,
                       "declared by pypi", True, "reads like x's docs")
    second.signals = ["own-domain"]
    merged = resolver.dedupe([first, second])
    assert len(merged) == 1
    assert merged[0].source == "domain:dev", "the first entry survives"
    assert merged[0].confidence == 0.92, "the higher confidence wins"
    assert merged[0].verified is True, "a verdict beats not-checked-yet"
    assert merged[0].signals == ["own-domain"]


def test_dedupe_never_lowers_a_verdict_that_was_already_reached():
    verified = Candidate("https://x.dev/docs", "a", 0.9, "e", True, "confirmed")
    unchecked = Candidate("https://x.dev/docs", "b", 0.4, "e2")
    merged = resolver.dedupe([verified, unchecked])
    assert merged[0].verified is True and merged[0].reason == "confirmed"


def test_dedupe_does_not_merge_paths_that_differ_only_in_case():
    """Hosts are case-insensitive; paths are not, and `/API` is a real page
    somewhere that is not `/api`."""
    merged = resolver.dedupe([Candidate("https://X.dev/API", "a", 0.5),
                              Candidate("https://x.dev/api", "b", 0.5)])
    assert len(merged) == 2


# --- the company is not the library ------------------------------------------
#
# Measured live, 2026-09-16. `find_docs("pydantic")` answered
# `https://pydantic.dev/llms.txt` at 0.97, verified, and `learn_technology`
# stored what that led to: **24 pages of Pydantic Logfire** — the observability
# product — including "Pricing, enterprise and security", "Customer evidence"
# and a competition winner's congratulations, filed under the name of the
# validation library. Nothing in the result suggested anything was wrong; the
# harvest was complete, confident and about the wrong software.
#
# `pydantic.dev` is the company and `docs.pydantic.dev` is the library. The
# apex answers `/llms.txt` with a 15 KB index, `probe_docs_root` took it at
# 0.95 and broke out of the loop, and the registry lap — which would have seen
# PyPI's Documentation field — never ran, because the domain lap had already
# won. Asking the documentation subdomain first is what separates them:
# 695 pages and 2,145,530 characters against 24 and 681,899.

def test_the_docs_subdomain_is_asked_before_the_apexs_own_paths():
    fetcher = FakeFetcher({
        "https://x.dev/llms.txt": FakeResponse("# x, the company\n" + "x " * 60,
                                               ctype="text/plain",
                                               url="https://x.dev/llms.txt"),
        "https://docs.x.dev/llms.txt": FakeResponse("# x, the library\n" + "x " * 60,
                                                    ctype="text/plain",
                                                    url="https://docs.x.dev/llms.txt"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url == "https://docs.x.dev/llms.txt", found[0].url
    assert found[0].confidence > 0.95, "it has to outrank the apex's own 0.95"


def test_the_apex_still_wins_when_there_is_no_docs_subdomain():
    """The overwhelmingly common shape, and it must cost one request and no
    change of answer."""
    fetcher = FakeFetcher({
        "https://x.dev/llms.txt": FakeResponse("# x docs\n" + "x " * 60,
                                               ctype="text/plain",
                                               url="https://x.dev/llms.txt"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url == "https://x.dev/llms.txt"


def test_a_docs_host_is_not_asked_for_a_docs_host_of_its_own():
    fetcher = FakeFetcher({
        "https://docs.x.dev/llms.txt": FakeResponse("# x docs\n" + "x " * 60,
                                                    ctype="text/plain",
                                                    url="https://docs.x.dev/llms.txt"),
    })
    resolver.probe_docs_root("https://docs.x.dev", fetcher)
    assert not any("docs.docs.x.dev" in u for u in fetcher.asked), fetcher.asked


def test_a_docs_subdomain_serving_html_is_not_taken_as_a_corpus():
    """`llms.txt` that comes back as a rendered 404 page is not a corpus, and
    admitting one would hand the harvest a site's error template."""
    fetcher = FakeFetcher({
        "https://docs.x.dev/llms.txt": FakeResponse("<h1>Not found</h1>",
                                                    url="https://docs.x.dev/llms.txt"),
        "https://x.dev/llms.txt": FakeResponse("# x docs\n" + "x " * 60,
                                               ctype="text/plain",
                                               url="https://x.dev/llms.txt"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url == "https://x.dev/llms.txt"


def test_a_docs_subdomain_publishing_only_articles_is_refused_like_any_other():
    """A subdomain does not get a softer bar than the apex — `_indexes_only_articles`
    is what stopped pytorch.org/llms.txt becoming a newsroom harvest, and it
    applies here identically."""
    newsroom = "# Blog\n\n## Posts\n" + "\n".join(
        f"- [Announcing thing {i}](https://docs.x.dev/blog/{i})" for i in range(40))
    fetcher = FakeFetcher({
        "https://docs.x.dev/llms.txt": FakeResponse(newsroom, ctype="text/plain",
                                                    url="https://docs.x.dev/llms.txt"),
        "https://x.dev/llms.txt": FakeResponse("# x docs\n" + "x " * 60,
                                               ctype="text/plain",
                                               url="https://x.dev/llms.txt"),
    })
    found = resolver.probe_docs_root("https://x.dev", fetcher)
    assert found and found[0].url == "https://x.dev/llms.txt"


def test_a_remembered_resolution_keeps_its_release(tmp_path, monkeypatch):
    """The cache stored everything but the release, so the second
    `learn_technology` for a name — the one served from memory — filed its
    harvest under the date where the first had filed it under the version."""
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(tmp_path / "r.json"))
    got = resolver.resolve("click", fetcher=_colliding())
    assert got.release == "9.9.9"
    resolver.remember("click", got)
    again = resolver.recall("click")
    assert again is not None and again.release == "9.9.9", again
