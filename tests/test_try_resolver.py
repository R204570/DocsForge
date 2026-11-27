"""
The home page's try-it box: `find_docs`, live, for someone who has installed
nothing.

From a visitor's feedback (2026-11-26): nothing on the site could be tried in
a browser, so seeing DocsForge work meant installing Python tools and wiring
up a client first. `/find-docs` answers with the candidates `find_docs` would
hand a model. It is public, so what is pinned here is what keeps that safe:
it needs no token, it refuses anything that is not a name, it counts lookups
per caller, it caps how many run at once, and it can be switched off.
"""

import json

import pytest
from starlette.testclient import TestClient

from docsforge.core.resolver import Candidate, Resolution
from docsforge.server import mcp_server

TOKEN = "s3cret-token"
LOOPBACK = "http://127.0.0.1:8765"


def _found(name="zod", unexamined=False):
    best = Candidate("https://zod.dev/", "domain:dev", 0.75,
                     "zod.dev is the project's own domain and carries an install command", True)
    other = Candidate("https://zod.dev/llms.txt", "domain:dev/probe:llms.txt", 0.97,
                      "https://zod.dev/llms.txt exists and is not HTML", True)
    return Resolution(name=name, candidates=[other, best], best=best,
                      note="Resolved from 'zod''s own domain.", resolved_via="domain",
                      unexamined=unexamined)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(mcp_server.TRY_VAR, raising=False)
    monkeypatch.setattr(mcp_server, "_try_limit", mcp_server.RateLimit(3))
    calls = []

    def resolve(name):
        calls.append(name)
        return _found(name)

    monkeypatch.setattr(mcp_server, "resolve_name", resolve)
    with TestClient(mcp_server.build_http_app(token=TOKEN), base_url=LOOPBACK) as c:
        c.calls = calls
        yield c


def test_a_name_comes_back_as_the_candidates_find_docs_reports(client):
    r = client.get("/find-docs", params={"name": "zod"})      # no token: public
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    d = r.json()
    assert d["name"] == "zod" and d["best"] == "https://zod.dev/"
    assert [c["url"] for c in d["candidates"]] == ["https://zod.dev/llms.txt", "https://zod.dev/"]
    assert all(c["verified"] is True for c in d["candidates"])
    assert d["candidates"][1]["evidence"].startswith("zod.dev is the project's own domain")
    assert d["resolved_via"] == "domain" and d["remembered"] is False
    assert isinstance(d["seconds"], float)
    # repeats of a name may be answered by a CDN, never by a browser's own cache
    assert r.headers["cache-control"] == "public, max-age=0, s-maxage=3600"


def test_a_site_that_could_not_be_read_is_not_cached(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "resolve_name", lambda name: _found(name, unexamined=True))
    assert client.get("/find-docs", params={"name": "zod"}).headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", ["", "   ", "https://evil.example/", "a" * 65,
                                  "zod; rm -rf /", "<script>", "-flag"])
def test_anything_but_a_name_is_refused_before_any_lookup(client, name):
    r = client.get("/find-docs", params={"name": name})
    assert r.status_code == 400 and r.json()["error"] == "bad_name"
    assert client.calls == []


@pytest.mark.parametrize("name", ["zod", "@tanstack/query", "scikit-learn", "next.js",
                                  "react router", "c++", "Pydantic"])
def test_names_as_people_type_them_are_accepted(client, name):
    assert client.get("/find-docs", params={"name": name}).status_code == 200


def test_lookups_are_counted_per_caller(client):
    for _ in range(3):
        assert client.get("/find-docs", params={"name": "zod"}).status_code == 200
    r = client.get("/find-docs", params={"name": "zod"})
    assert r.status_code == 429 and r.json()["error"] == "slow_down"
    assert int(r.headers["retry-after"]) >= 1 and r.headers["cache-control"] == "no-store"
    assert len(client.calls) == 3


def test_a_full_house_is_told_to_come_back(client, monkeypatch):
    class Full:
        def acquire(self, blocking=True):
            return False
    monkeypatch.setattr(mcp_server, "_try_slots", Full())
    r = client.get("/find-docs", params={"name": "zod"})
    assert r.status_code == 503 and r.json()["error"] == "busy" and client.calls == []


@pytest.mark.parametrize("value", ["0", "off", "false", "no"])
def test_it_can_be_switched_off(client, monkeypatch, value):
    monkeypatch.setenv(mcp_server.TRY_VAR, value)
    r = client.get("/find-docs", params={"name": "zod"})
    assert r.status_code == 503 and r.json()["error"] == "disabled" and client.calls == []


def test_a_lookup_that_breaks_says_so_and_frees_its_slot(client, monkeypatch):
    def broken(name):
        raise RuntimeError("registry down")
    monkeypatch.setattr(mcp_server, "resolve_name", broken)
    r = client.get("/find-docs", params={"name": "zod"})
    assert r.status_code == 502 and r.json()["error"] == "failed"
    assert "registry down" not in r.text            # the reason stays on the server
    # the slot came back: every one of them can still be taken
    taken = [mcp_server._try_slots.acquire(blocking=False) for _ in range(mcp_server.TRY_AT_ONCE)]
    for ok in taken:
        if ok:
            mcp_server._try_slots.release()
    assert all(taken)


def test_the_rate_limit_forgets_calls_older_than_a_minute():
    now = [1000.0]
    limit = mcp_server.RateLimit(2, clock=lambda: now[0])
    assert limit.wait("a") == 0 and limit.wait("a") == 0
    assert limit.wait("a") == pytest.approx(60)
    assert limit.wait("b") == 0                      # another caller has their own count
    now[0] += 30
    assert limit.wait("a") == pytest.approx(30)
    now[0] += 30
    assert limit.wait("a") == 0


def test_only_vercel_is_trusted_to_say_who_the_caller_is(monkeypatch):
    class Req:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}

        class client:
            host = "10.0.0.1"
    monkeypatch.delenv("VERCEL", raising=False)
    assert mcp_server._caller(Req()) == "10.0.0.1"
    monkeypatch.setenv("VERCEL", "1")
    assert mcp_server._caller(Req()) == "203.0.113.9"


def test_the_resolver_seam_is_the_real_resolver():
    from docsforge.core import resolver
    assert mcp_server.resolver is resolver


# ── the box on the home page ─────────────────────────────
def test_the_home_page_offers_the_box_and_says_nothing_is_stored(client):
    html = client.get("/").text
    assert 'id="try"' in html and 'action="/find-docs"' in html and 'id="try-name"' in html
    assert "Nothing is harvested or stored." in html
    # the hero's second action leads to it
    assert 'href="/#try"' in html
    # the script calls the endpoint, and puts its answers in the page as text
    assert 'fetch("/find-docs?name=" + encodeURIComponent(name)' in html
    assert "innerHTML" not in html


def test_robots_keeps_engines_off_the_resolver(client):
    assert "Disallow: /find-docs" in client.get("/robots.txt").text.splitlines()
