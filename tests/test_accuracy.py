"""
Live resolution accuracy — the test that would have caught the wrong answers.

Every other resolver test stubs the fetcher, so they check the *chain logic*
and never the *outcome*. That is why `terraform` could resolve to an unrelated
static-site tool, `kubernetes` to its Python client and `htmx` to a Rust crate
while the whole suite stayed green: the failures were in what the internet
actually returns, and nothing in the repository looked.

This file looks. It is off by default because it needs the network and takes
about a minute:

    DOCSFORGE_TEST_NETWORK=1 python -m pytest tests/test_accuracy.py -v

Two things are asserted, and the second matters more than the first.

    1. The right project is found.
    2. **Nothing wrong is ever marked `verified`.**

Accuracy will never be perfect — some names are genuinely ambiguous, and the
web moves. Zero confidently-wrong answers is achievable anyway, because it
depends on our own honesty rather than on the web. A wrong answer a caller can
see is unproven costs them a second look; a wrong answer stamped `verified`
costs them the habit of looking at all.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolver

LIVE = os.environ.get("DOCSFORGE_TEST_NETWORK", "")

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set DOCSFORGE_TEST_NETWORK=1 to resolve against the live web")


#: name -> hosts that are an acceptable answer. More than one is allowed where
#: a project genuinely publishes under several: Terraform's docs live on
#: HashiCorp's developer portal, which `terraform.io` redirects to.
EXPECTED = {
    "fastapi":    ("fastapi.tiangolo.com",),
    "vitest":     ("vitest.dev",),
    "deno":       ("deno.com", "docs.deno.com", "deno.land"),
    # NOT astro.build. Accepting the marketing homepage here is what let a real
    # bug survive this fixture: resolution landed on astro.build, the harvest
    # scoped to the whole host, and a user asking about Astro was handed 34
    # blog posts and no documentation at all.
    "astro":      ("docs.astro.build",),
    "htmx":       ("htmx.org",),
    "kubernetes": ("kubernetes.io",),
    "terraform":  ("developer.hashicorp.com", "terraform.io"),
    "pydantic":   ("docs.pydantic.dev", "pydantic.dev"),
    "zod":        ("zod.dev",),
    "hono":       ("hono.dev",),
}

#: Resolution is allowed to fail on these — they are the known gap (F6), not a
#: regression. What is *not* allowed is resolving them to something wrong.
MAY_NOT_RESOLVE = ("cloudflare workers",)


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


@pytest.fixture(scope="module")
def fetcher():
    import docsforge
    f = docsforge.Fetcher(docsforge.Options(delay=0.0))
    yield f
    f.close()


@pytest.mark.parametrize("name,hosts", sorted(EXPECTED.items()))
def test_resolves_to_the_right_project(name, hosts, fetcher):
    got = resolver.resolve(name, fetcher=fetcher)
    assert got.best is not None, f"{name} did not resolve at all: {got.note}"
    host = _host(got.best.url)
    assert host in hosts, (
        f"{name} resolved to {got.best.url} (host {host}), expected one of "
        f"{hosts}. Signals: {got.best.signals}")


@pytest.mark.parametrize("name,hosts", sorted(EXPECTED.items()))
def test_never_marks_a_wrong_answer_verified(name, hosts, fetcher):
    """The hard gate. A wrong answer is survivable; a wrong answer that says
    it checked is not, because the caller has been given a reason to stop."""
    got = resolver.resolve(name, fetcher=fetcher)
    if got.best is None or _host(got.best.url) in hosts:
        return
    assert got.best.verified is not True, (
        f"{name} resolved to the wrong host {_host(got.best.url)} and reported "
        f"verified: true — {got.best.reason}")


@pytest.mark.parametrize("name", MAY_NOT_RESOLVE)
def test_an_unreachable_name_fails_honestly(name, fetcher):
    got = resolver.resolve(name, fetcher=fetcher)
    if got.best is not None:
        assert got.best.verified is True, (
            f"{name} returned an unverified best guess — it should report "
            f"failure instead of guessing")
    else:
        assert got.note, "a failure has to explain itself"


def test_identity_beats_a_name_collision(fetcher):
    """The three measured F1 failures, as one assertion.

    Each of these names belongs to a well-known technology *and* to an
    unrelated package that happens to share the word. Registries answer the
    second question; callers are asking the first.
    """
    wrong = {
        "terraform":  "sintaxi",                  # an unrelated static-site tool
        "kubernetes": "kubernetes-client",        # the Python client, not the platform
        "htmx":       "docs.rs",                  # a Rust crate
    }
    for name, trap in wrong.items():
        got = resolver.resolve(name, fetcher=fetcher)
        assert got.best is not None, f"{name} stopped resolving entirely"
        assert trap not in got.best.url, (
            f"{name} resolved to {got.best.url}, which is the known-wrong "
            f"project this check exists for")
