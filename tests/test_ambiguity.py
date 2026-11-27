"""
One name, several projects — and names no package registry carries.

Found from the home page's try-it box, 2026-11-27: `piet` resolved to
github.com/silvandiepen/piet, an npm package at 0.0.11, with nothing said
about anything else. Two packages called piet verified — that one and the
Rust crate linebender/piet — each against its own registry's claims, and the
npm README won on agreeing with npm more than the crate's page agreed with
crates.io. And Piet the programming language, which is what was meant, was
never a candidate: it is not a package, it owns no `piet.*` domain, and it
lives on its author's site, which no lap could see. Wikidata records that
page as its official website.

Offline: every registry, page and API is stubbed.
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from docsforge.core import resolver
from docsforge.core.resolver import Candidate, Resolution


class Response:
    def __init__(self, text="", status=200, url=""):
        self.text, self.status_code, self.url = text, status, url
        self.headers = {"content-type": "text/html"}


def payload(data):
    return Response(json.dumps(data))


def entity(qid, label, sites, kinds=("Q9143",), sitelinks=5, aliases=(), repo=""):
    claim = lambda value, rank="normal": {"rank": rank, "mainsnak": {
        "snaktype": "value", "datavalue": {"value": value}}}
    claims = {"P856": [claim(s) for s in sites],
              "P31": [claim({"id": k}) for k in kinds]}
    if repo:
        claims["P1324"] = [claim(repo)]
    return {"labels": {"en": {"value": label}},
            "aliases": {"en": [{"value": a} for a in aliases]},
            "claims": claims,
            "sitelinks": {f"site{i}": {} for i in range(sitelinks)}}


class Network:
    """Pages by URL, and Wikidata answered from a table of entities."""

    def __init__(self, pages=None, wikidata=None):
        self.pages = pages or {}
        self.wikidata = wikidata or {}          # qid -> entity, searched by label
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        if url.startswith(resolver.WIKIDATA_API):
            assert "DocsForge" in kw.get("headers", {}).get("User-Agent", "")
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            if q.get("action") == "query":
                term = q["srsearch"].split('"')[1].rsplit("@", 1)[0].lower()
                hits = [{"title": qid} for qid, e in self.wikidata.items()
                        if term in e["labels"]["en"]["value"].lower()
                        or any(term in a["value"].lower() for a in e["aliases"]["en"])]
                return payload({"query": {"search": hits}})
            if q.get("action") == "wbgetentities":
                return payload({"entities": {i: self.wikidata[i] for i in q["ids"].split("|")}})
        hit = self.pages.get(url) or self.pages.get(url.rstrip("/"))
        return hit or Response("not found", status=404, url=url)

    def text(self, url, **kw):
        r = self.get(url)
        if r.status_code != 200:
            from docsforge.core.engine import ForgeError
            raise ForgeError(f"HTTP {r.status_code} for {url}")
        return r.text

    def close(self):
        pass


def page(name, links="", words=40):
    return Response(f"<h1>{name}</h1>{links}" + f"<p>{name} </p>" * words)


def piet(wikidata=True):
    """npm's piet and crates.io's piet, two unrelated packages that both
    verify; and, in Wikidata, Piet the language at its author's site."""
    return Network(pages={
        "https://registry.npmjs.org/piet": payload({
            "dist-tags": {"latest": "0.0.11"},
            "homepage": "https://github.com/silvandiepen/piet#readme",
            "repository": {"url": "git+https://github.com/silvandiepen/piet.git"}}),
        "https://crates.io/api/v1/crates/piet": payload({"crate": {
            "max_stable_version": "0.7.0", "repository": "https://github.com/linebender/piet"}}),
        "https://github.com/silvandiepen/piet#readme": page(
            "piet", '<a href="https://github.com/silvandiepen/piet">repo</a>', 4),
        "https://github.com/silvandiepen/piet": page(
            "piet", '<a href="https://github.com/silvandiepen/piet">repo</a>', 4),
        "https://github.com/linebender/piet": page(
            "piet", '<a href="https://github.com/linebender/piet">repo</a>', 43),
        "https://dangermouse.net/esoteric/piet.html": page("Piet", "", 17),
    }, wikidata={"Q1886145": entity(
        "Q1886145", "Piet", ["https://dangermouse.net/esoteric/piet.html"],
        kinds=("Q610140", "Q683111", "Q9143"), sitelinks=16)} if wikidata else {})


# ── one name, several projects ───────────────────────────
def test_piet_is_three_projects_and_none_is_picked():
    got = resolver.resolve("piet", fetcher=piet())
    assert got.best is None and got.resolved_via == "ambiguous"
    urls = [c.url for c in got.alternatives]
    assert set(urls) == {"https://dangermouse.net/esoteric/piet.html",
                         "https://github.com/silvandiepen/piet#readme",
                         "https://github.com/linebender/piet"}
    # best-evidenced first: a site of its own before two READMEs
    assert urls[0] == "https://dangermouse.net/esoteric/piet.html"
    assert all(c.verified for c in got.alternatives)
    # and the note says what each one is, and how to ask for one
    assert "3 different projects" in got.note and "None was chosen" in got.note
    assert "esoteric programming language, per Wikidata" in got.note
    assert "the npm package" in got.note and "the crates.io crate" in got.note
    assert "ecosystem= (crates, npm)" in got.note and "harvest_docs" in got.note
    assert got.release == ""


def test_without_wikidata_the_two_packages_are_still_reported_not_chosen():
    """The original failure: two packages, one silently returned."""
    got = resolver.resolve("piet", fetcher=piet(wikidata=False))
    assert got.best is None and len(got.alternatives) == 2
    assert "2 different projects" in got.note


def test_naming_the_registry_settles_it():
    got = resolver.resolve("piet", ecosystem="crates", fetcher=piet())
    assert got.best is not None and got.best.url == "https://github.com/linebender/piet"
    assert not got.alternatives


def test_a_named_registry_never_asks_wikidata():
    """A caller who named a registry meant a package."""
    net = piet()
    resolver.resolve("piet", ecosystem="npm", fetcher=net)
    assert not any(u.startswith(resolver.WIKIDATA_API) for u in net.asked)


def test_an_ambiguous_name_is_remembered_with_its_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(tmp_path / "r.json"))
    first = resolver.resolve("piet", fetcher=piet())
    again = resolver.resolve("piet", fetcher=Network())        # nothing on the network now
    assert again.remembered and again.best is None
    assert [c.url for c in again.alternatives] == [c.url for c in first.alternatives]
    assert again.note == first.note
    assert resolver.project_label(again.alternatives[0]).endswith("per Wikidata")


def test_popularity_below_the_override_does_not_settle_an_ambiguity(monkeypatch):
    monkeypatch.setattr(resolver, "_popular_repo", lambda *a, **k: {
        "repo": "linebender/piet", "stars": 1500, "homepage": "https://docs.rs/piet"})
    got = resolver.resolve("piet", fetcher=piet())
    assert got.best is None and len(got.alternatives) == 3


def test_the_same_project_reached_twice_is_one_project():
    """A project on npm and crates.io with one repository is one project, and
    its docs site and its repository are one project too."""
    npm = Candidate("https://zorp.dev/", "npm:homepage", 0.6, verified=True, registry="npm",
                    signals=["names-it:9", "registry-agreement"])
    crate = Candidate("https://github.com/acme/zorp", "crates:repository", 0.5,
                      verified=True, registry="crates", signals=["repo-identity", "names-it:9"])
    claims = {id(npm): {"homepage": "https://zorp.dev/", "repository": "https://github.com/acme/zorp"},
              id(crate): {"repository": "https://github.com/acme/zorp"}}
    groups = resolver.project_groups([npm, crate], lambda c: claims[id(c)])
    assert len(groups) == 1
    assert resolver.rival_projects([npm, crate], npm, lambda c: claims[id(c)]) == []


def test_a_project_that_owns_the_name_wins_over_one_that_does_not():
    """`requests`: the PyPI project's own docs host against a Rust crate's
    docs.rs page. Both verify; only one makes a claim to the name."""
    net = Network(pages={
        "https://pypi.org/pypi/zorp/json": payload({"info": {
            "version": "2.0", "project_urls": {"Documentation": "https://zorp.readthedocs.io/"}}}),
        "https://crates.io/api/v1/crates/zorp": payload({"crate": {
            "documentation": "https://docs.rs/zorp"}}),
        "https://zorp.readthedocs.io/": page("zorp", "<pre>pip install zorp</pre>"),
        "https://docs.rs/zorp": page("zorp", '<a href="https://github.com/other/zorp">src</a>'),
    })
    got = resolver.resolve("zorp", fetcher=net)
    assert got.best is not None and got.best.url == "https://zorp.readthedocs.io/"
    assert not got.alternatives
    rust = next(c for c in got.candidates if c.url == "https://docs.rs/zorp")
    assert rust.verified, "the point is that both verified"


# ── Wikidata ─────────────────────────────────────────────
def test_a_name_no_registry_carries_is_found_through_wikidata():
    net = Network(pages={"https://catseye.tc/node/Befunge-93.html": page("Befunge")},
                  wikidata={"Q814269": entity("Q814269", "Befunge",
                                              ["http://catseye.tc/node/Befunge-93.html"],
                                              kinds=("Q610140",), sitelinks=21)})
    got = resolver.resolve("befunge", fetcher=net)
    assert got.best is not None and got.best.url == "https://catseye.tc/node/Befunge-93.html"
    assert got.resolved_via == "wikidata" and got.ecosystem == "" and got.release == ""
    assert "Wikidata" in got.note and "No package registry carries it" in got.note
    assert "Befunge, esoteric programming language" in got.note


def test_wikidata_names_the_site_a_registry_left_out():
    """npm gave only the repository; Wikidata records the project's own site,
    and says it is the same project — the same repository."""
    net = Network(pages={
        "https://registry.npmjs.org/zorp": payload({
            "dist-tags": {"latest": "3.1.0"},
            "repository": {"url": "git+https://github.com/acme/zorp.git"}}),
        "https://github.com/acme/zorp": page("zorp", '<a href="https://github.com/acme/zorp">r</a>'),
        "https://zorp-lang.org/": page("zorp", '<a href="https://github.com/acme/zorp">source</a>'),
    }, wikidata={"Q1": entity("Q1", "Zorp", ["https://zorp-lang.org/"], kinds=("Q188860",),
                              repo="https://github.com/acme/zorp")})
    got = resolver.resolve("zorp", fetcher=net)
    assert got.best is not None and got.best.url == "https://zorp-lang.org/"
    assert got.resolved_via == "registry+wikidata"
    # still npm's project, so npm's release
    assert got.ecosystem == "npm" and got.release == "3.1.0"
    assert not got.alternatives


def test_only_an_exact_name_with_an_article_counts():
    """`inlabel` matches inside labels: "SSSOM Pydantic" is not Pydantic,
    and an item no Wikipedia writes about is not what a bare name means."""
    net = Network(pages={"https://x.example/": page("zorp")}, wikidata={
        "Q2": entity("Q2", "SSSOM Zorp", ["https://x.example/"]),
        "Q3": entity("Q3", "Zorp", ["https://x.example/"], sitelinks=0)})
    assert resolver.from_wikidata("zorp", net) == []


def test_of_several_exact_matches_the_best_known_is_taken():
    net = Network(wikidata={
        "Q5": entity("Q5", "Zorp", ["https://lute-maker.example/"], kinds=("Q7397",), sitelinks=1),
        "Q6": entity("Q6", "Zorp", ["https://zorp.example/"], kinds=("Q1330336",), sitelinks=47),
    })
    got = resolver.from_wikidata("zorp", net)
    assert [c.url for c in got] == ["https://zorp.example/"]
    assert got[0].source == "wikidata:Q6" and "web framework" in got[0].evidence


def test_an_alias_counts_as_the_name():
    net = Network(wikidata={"Q1131652": entity(
        "Q1131652", "Rocq prover", ["https://rocq-prover.org/"], aliases=("Coq",), sitelinks=17)})
    assert [c.url for c in resolver.from_wikidata("coq", net)] == ["https://rocq-prover.org/"]


def test_a_wikidata_page_still_has_to_pass_the_gate():
    """Wikidata says where to look; the page still has to name the thing."""
    net = Network(pages={"https://quiet.example/": page("something else entirely")},
                  wikidata={"Q7": entity("Q7", "Zorp", ["https://quiet.example/"])})
    got = resolver.resolve("zorp", fetcher=net)
    assert got.best is None
    cand = next(c for c in got.candidates if c.source == "wikidata:Q7")
    assert cand.verified is False


def test_a_failing_wikidata_costs_nothing():
    class Down(Network):
        def get(self, url, **kw):
            if url.startswith(resolver.WIKIDATA_API):
                return Response("busy", status=503)
            return super().get(url, **kw)
    assert resolver.from_wikidata("piet", Down()) == []


# ── what the tools say ───────────────────────────────────
def test_find_docs_lists_the_projects_and_the_way_to_pick_one(monkeypatch):
    from docsforge.tools import forge_tools as ft
    monkeypatch.setattr(ft, "_resolve", lambda name, **kw: resolver.resolve(name, fetcher=piet()))
    out = ft.tool_find_docs("piet")
    assert "dangermouse.net/esoteric/piet.html" in out and "linebender/piet" in out
    assert "None was chosen" in out and "Best:" not in out


def test_the_try_box_marks_the_alternatives():
    from docsforge.server import mcp_server
    got = resolver.resolve("piet", fetcher=piet())
    d = mcp_server.try_answer("piet", got, 1.0)
    assert d["ambiguous"] is True and d["best"] is None
    assert [c["url"] for c in d["candidates"][:3]] == [c.url for c in got.alternatives]
    assert all(c["alternative"] for c in d["candidates"][:3])
    assert d["candidates"][0]["label"].endswith("per Wikidata")
