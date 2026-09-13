"""
Offline tests for the Phase C resolution ladder — no network.

The ladder's whole claim is that more laps never mean a lower bar. Every lap
therefore gets two tests: one that it can now reach something it could not
reach before, and one that a candidate it reaches still has to pass exactly the
same identity gate. A lap that resolves more names by believing more of them
would be a regression dressed as a feature.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import resolver
from docsforge.core.resolver import Budget, ResolveState


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


SOFTWARE = ("<pre>code</pre><pre>more</pre><pre>again</pre>"
            "<a href='https://github.com/apache/airflow'>source</a>")


# ── L0: memory ───────────────────────────────────────────
def test_a_remembered_resolution_costs_no_requests():
    resolver.remember("effect", resolver.Resolution(
        name="effect",
        best=resolver.Candidate("https://effect.website/docs/", "domain:dev",
                                0.9, "stubbed", True, "identified"),
        resolved_via="domain"))

    fetcher = FakeFetcher({})
    got = resolver.resolve("effect", fetcher=fetcher)

    assert got.best is not None
    assert got.best.url == "https://effect.website/docs/"
    # The point of a cache. One that still opens a connection is not one.
    assert fetcher.asked == [], f"memory hit still fetched {fetcher.asked}"


def test_forget_resolution_clears_it():
    resolver.remember("effect", resolver.Resolution(
        name="effect",
        best=resolver.Candidate("https://effect.website/", "domain:dev", 0.9,
                                "stubbed", True, "identified")))
    assert resolver.recall("effect") is not None

    said = resolver.forget_resolution("effect")

    assert "effect" in said
    assert resolver.recall("effect") is None


def test_forgetting_everything_empties_the_cache():
    for name in ("a-thing", "b-thing"):
        resolver.remember(name, resolver.Resolution(name=name))
    assert "2" in resolver.forget_resolution()
    assert resolver.recall("a-thing") is None


def test_a_stale_memory_is_not_used():
    resolver.remember("effect", resolver.Resolution(
        name="effect",
        best=resolver.Candidate("https://effect.website/", "domain:dev", 0.9,
                                "stubbed", True, "identified")))
    data = json.loads(open(os.environ["DOCSFORGE_RESOLVE_CACHE"], encoding="utf-8").read())
    data["effect"]["at"] = time.time() - resolver.CACHE_TTL - 1
    open(os.environ["DOCSFORGE_RESOLVE_CACHE"], "w", encoding="utf-8").write(json.dumps(data))

    assert resolver.recall("effect") is None


def test_a_refusal_is_forgotten_sooner_than_a_success():
    # A refusal is a claim about what could not be found today; a site can add
    # the evidence tomorrow, so it must not be cached for a month.
    resolver.remember("ghost", resolver.Resolution(name="ghost"))
    data = json.loads(open(os.environ["DOCSFORGE_RESOLVE_CACHE"], encoding="utf-8").read())
    data["ghost"]["at"] = time.time() - resolver.REJECT_TTL - 1
    open(os.environ["DOCSFORGE_RESOLVE_CACHE"], "w", encoding="utf-8").write(json.dumps(data))

    assert resolver.recall("ghost") is None
    assert resolver.REJECT_TTL < resolver.CACHE_TTL


# ── L3: name shapes ──────────────────────────────────────
def test_a_single_word_name_produces_no_shapes():
    # from_domains already tried the only shape a one-token name has.
    assert resolver.name_shapes("fastapi") == []


def test_the_shapes_cover_how_vendors_actually_publish():
    urls = [url for _label, url in resolver.name_shapes("apache airflow")]
    assert "https://apacheairflow.io" in urls          # run together
    assert "https://airflow.apache.org" in urls        # product under vendor
    assert "https://apache.io/airflow" in urls         # product as a path
    assert "https://docs.apache.io/airflow" in urls    # vendor docs portal


def test_a_shape_hit_still_has_to_pass_the_identity_gate():
    # airflow.apache.org exists and is clearly software, but nothing on it
    # identifies the project, so it must not resolve.
    fetcher = FakeFetcher({
        "https://airflow.apache.org": FakeResponse(
            "<pre>x</pre><pre>y</pre><pre>z</pre> a page about scheduling"),
    })
    got = resolver.resolve("apache airflow", fetcher=fetcher, use_memory=False)
    assert got.best is None, f"identified on no evidence: {got.best}"


def test_a_shape_hit_with_real_evidence_resolves():
    fetcher = FakeFetcher({
        # Long enough to clear the visible-text floor a real docs page clears.
        "https://airflow.apache.org": FakeResponse(
            SOFTWARE + " Apache Airflow is a platform to author and schedule "
                       "workflows. " * 12),
    })
    got = resolver.resolve("apache airflow", fetcher=fetcher, use_memory=False)
    assert got.best is not None
    assert got.best.url.startswith("https://airflow.apache.org")
    assert got.resolved_via.startswith("shape:")


# ── the identity signals the ladder leans on ─────────────
def test_a_multi_word_name_can_own_a_hostname_across_labels():
    # apache airflow lives at airflow.apache.org; visual studio code at
    # code.visualstudio.com. Requiring every token is stricter, not looser.
    assert resolver._owns_the_name("https://airflow.apache.org", "apache-airflow")
    assert resolver._owns_the_name("https://code.visualstudio.com", "visual-studio-code")
    assert not resolver._owns_the_name("https://apache.org", "apache-airflow")


def test_a_project_writing_its_name_run_together_is_counted():
    # "OpenTelemetry" normalises to opentelemetry and never matched the
    # hyphenated slug, so the name went uncounted on the project's own site.
    signals = resolver.identity_signals(
        resolver.Candidate("https://elsewhere.example", "x", 0.5),
        "open telemetry",
        "OpenTelemetry is great. OpenTelemetry docs. Use OpenTelemetry.")
    assert any(s.startswith("names-it") for s in signals)


def test_a_repository_that_names_the_project_identifies_it():
    body = "<a href='https://github.com/TanStack/query'>source</a>"
    got = resolver._repo_identity(body, "tanstack-query")
    assert got == "TanStack/query"


def test_a_repository_that_merely_contains_the_word_does_not():
    # The measured failure: an unrelated to-do app called Flask Lists must not
    # claim `flask` through a repository whose name merely embeds it.
    assert resolver._repo_identity(
        "<a href='https://github.com/flaskio/web'>source</a>", "flask") == ""
    assert resolver._repo_identity(
        "<a href='https://github.com/pallets/flask'>source</a>", "flask") == "pallets/flask"


@pytest.mark.parametrize("repo,name", [
    # Every one of these was returned by npm search and wrongly identified the
    # first time the search lap ran. A longer repository name that happens to
    # contain the tokens is not the project.
    ("https://github.com/awslabs/aws-lambda-invoke-store", "aws lambda"),
    ("https://github.com/estruyf/playwright-github-actions-reporter", "github actions"),
    ("https://github.com/strapi-community/strapi-provider-upload-google-cloud-storage",
     "google cloud storage"),
])
def test_a_longer_repository_cannot_claim_the_name(repo, name):
    assert resolver._repo_identity(f"<a href='{repo}'>src</a>",
                                   resolver.normalise(name)) == ""


@pytest.mark.parametrize("repo,name", [
    ("https://github.com/apache/airflow", "apache airflow"),
    ("https://github.com/TanStack/query", "tanstack query"),
    ("https://github.com/pallets/flask", "flask"),
    ("https://github.com/react-hook-form/react-hook-form", "react hook form"),
])
def test_a_repository_that_is_the_project_still_identifies_it(repo, name):
    assert resolver._repo_identity(f"<a href='{repo}'>src</a>",
                                   resolver.normalise(name)) != ""


def test_repo_identity_is_strong_but_not_sufficient_alone():
    # One strong signal still needs the name present, exactly as before.
    assert not resolver.is_identified(["repo-identity"])
    assert resolver.is_identified(["repo-identity", "names-it:9"])
    assert "repo-identity" in resolver.STRONG


# ── L5: search ───────────────────────────────────────────
def test_a_search_hit_still_has_to_pass_the_identity_gate():
    fetcher = FakeFetcher({
        "https://registry.npmjs.org/-/v1/search?text=ghostly&size=4": FakeResponse(
            json.dumps({"objects": [{"package": {
                "name": "ghostly", "links": {"homepage": "https://unrelated.example"}}}]}),
            ctype="application/json"),
        "https://unrelated.example": FakeResponse("a page about something else"),
    })
    got = resolver.resolve("ghostly", fetcher=fetcher, use_memory=False)
    assert got.best is None


def test_search_is_never_a_search_engine():
    # Scraping a search engine's HTML endpoint is brittle, against their terms,
    # and a poor look for a resolver whose pitch is trustworthiness.
    source = open(resolver.__file__, encoding="utf-8").read()
    for engine in ("google.com/search", "bing.com/search", "duckduckgo.com/html"):
        assert engine not in source


# ── Budget ───────────────────────────────────────────────
def test_a_pathological_name_refuses_within_its_budget():
    fetcher = FakeFetcher({})
    got = resolver.resolve("not a real technology at all xyz", fetcher=fetcher,
                           use_memory=False)
    assert got.best is None
    assert "harvest_docs" in got.note or "could not" in got.note.lower()
    # Bounded: the ladder must not fan out without limit on a hopeless name.
    assert len(fetcher.asked) < 80, f"spent {len(fetcher.asked)} requests"


def test_the_budget_reports_what_it_would_refuse_with():
    spent = Budget(requests=2, seconds=999)
    spent.charge(2)
    assert spent.exhausted and "2 requests" in spent.why()


# ── ResolveState: the hinge ──────────────────────────────
def test_a_failed_candidate_still_deposits_its_evidence():
    state = ResolveState(name="effect")
    state.record("https://x.dev/", "<a href='https://github.com/eff/effect'>src</a>")
    assert "https://github.com/eff/effect" in state.repos_seen


# ── L4: evidence the failed candidates left behind ───────
def _github(payload: dict) -> FakeResponse:
    return FakeResponse(json.dumps(payload), ctype="application/json")


def test_a_repository_homepage_becomes_a_candidate():
    state = ResolveState(name="ghostlib")
    state.record("https://ghostlib.dev/",
                 "<a href='https://github.com/ghost/ghostlib'>source</a>")
    fetcher = FakeFetcher({
        "https://api.github.com/repos/ghost/ghostlib":
            _github({"homepage": "https://ghostlib.readthedocs.io"}),
    })

    found = resolver.from_evidence("ghostlib", state, fetcher)

    assert any(c.url == "https://ghostlib.readthedocs.io" for c in found)
    assert any("declares its homepage" in c.evidence for c in found)


def test_an_unrelated_repository_is_not_asked_about():
    # Every documentation page links to something on GitHub. Most of it has
    # nothing to do with the technology being resolved, and each question costs
    # a request.
    state = ResolveState(name="ghostlib")
    state.record("https://ghostlib.dev/",
                 "<a href='https://github.com/some/analytics-sdk'>tracker</a>")
    fetcher = FakeFetcher({})

    resolver.from_evidence("ghostlib", state, fetcher)

    assert fetcher.asked == [], f"asked about an unrelated repo: {fetcher.asked}"


def test_outbound_and_canonical_evidence_become_candidates():
    state = ResolveState(name="ghostlib")
    state.record("https://ghostlib.dev/",
                 "<link rel='canonical' href='https://docs.ghostlib.dev/'>"
                 "<a href='https://other.example/docs/api'>API reference</a>")

    found = resolver.from_evidence("ghostlib", state, FakeFetcher({}))
    urls = [c.url for c in found]

    assert "https://other.example/docs/api" in urls
    assert "https://docs.ghostlib.dev/" in urls


def test_an_evidence_candidate_still_has_to_pass_the_identity_gate():
    state = ResolveState(name="ghostlib")
    state.record("https://ghostlib.dev/",
                 "<a href='https://other.example/docs/api'>API</a>")
    fetcher = FakeFetcher({
        "https://other.example/docs/api": FakeResponse("a page about something else"),
    })

    found = resolver.from_evidence("ghostlib", state, fetcher)
    for cand in found:
        resolver.verify(cand, "ghostlib", fetcher, {})
    assert not any(c.verified for c in found)


def test_a_name_only_findable_through_repository_metadata_resolves():
    # The acceptance criterion this lap exists for. No registry knows it. Its
    # own domain answers but is far too thin to be documentation — and on its
    # way to being rejected it leaks the one thing that matters: a link to the
    # repository, whose homepage field says where the docs actually live.
    real_docs = "<html><body>" + " ghostlib ghostlib ghostlib " * 30 + "</body></html>"

    fetcher = FakeFetcher({
        "https://ghostlib.dev": FakeResponse(
            "<a href='https://github.com/ghost/ghostlib'>source</a>"),
        "https://api.github.com/repos/ghost/ghostlib":
            _github({"homepage": "https://ghostlib.readthedocs.io"}),
        "https://ghostlib.readthedocs.io": FakeResponse(real_docs),
    })

    got = resolver.resolve("ghostlib", fetcher=fetcher, use_memory=False)

    assert got.best is not None, f"unresolved: {got.note}"
    assert "ghostlib.readthedocs.io" in got.best.url
    assert got.resolved_via.startswith("evidence:")


def test_evidence_is_recorded_before_a_page_is_judged():
    # Recording after the software and text floors would miss exactly the
    # pages worth mining: the ones too thin to be documentation themselves.
    state = ResolveState(name="ghostlib")
    fetcher = FakeFetcher({
        "https://ghostlib.dev": FakeResponse(
            "<a href='https://github.com/ghost/ghostlib'>source</a>"),
    })

    resolver.from_domains("ghostlib", fetcher, state=state)

    assert "https://github.com/ghost/ghostlib" in state.repos_seen


def test_a_failing_candidate_deposits_its_page_for_the_next_lap():
    # verify() is the only place that holds a candidate's page, and it used to
    # read it once and throw it away.
    state = ResolveState(name="ghostlib")
    fetcher = FakeFetcher({
        "https://unrelated.example": FakeResponse(
            "nothing relevant <a href='https://github.com/ghost/ghostlib'>src</a>"),
    })
    cand = resolver.Candidate("https://unrelated.example", "registry", 0.5)

    resolver.verify(cand, "ghostlib", fetcher, {}, state=state)

    assert cand.verified is False
    assert "https://github.com/ghost/ghostlib" in state.repos_seen


def test_evidence_costs_nothing_when_there_is_none():
    assert resolver.from_evidence("x", ResolveState(name="x"), FakeFetcher({})) == []
    assert resolver.from_evidence("x", None, FakeFetcher({})) == []
