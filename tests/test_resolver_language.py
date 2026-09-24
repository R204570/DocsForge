"""
Resolving a technology for one language, and the resolver fixes found with it.

Measured live on 2026-09-24:

* `langgraph` resolves to `docs.langchain.com/oss/python/langgraph/overview`;
  asked for Node it must be `/oss/javascript/langgraph/overview`.
* `langgraph` on npm resolved to `docs.rs/rust-langgraph`, a Rust crate: the
  search lap searched crates.io whatever the caller had said.
* `@langchain/langgraph` resolved to its GitHub README, while the repository
  declares its documentation site as its homepage.
* `go` resolved to `docs.rs/go`, a Rust crate, and `golang` to a stranger's
  repository: a language is not a package.

No network: pages come from dicts.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import languages, resolver
from docsforge.core.resolver import Candidate, Resolution


class Resp:
    def __init__(self, text="", status=200, ctype="text/html", url=""):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.url = url


class Site:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        hit = self.pages.get(url)
        if hit is None:
            return Resp("not found", status=404, url=url)
        if isinstance(hit, Resp):
            return hit
        ctype = "application/json" if hit.lstrip().startswith("{") else "text/html"
        return Resp(hit, ctype=ctype, url=url)

    def text(self, url, **kw):
        r = self.get(url)
        if r.status_code != 200:
            raise resolver.ForgeError(f"HTTP {r.status_code} for {url}")
        return r.text

    def close(self):
        pass


def doc(title, code_lang, code, install):
    block = f'<pre><code class="language-{code_lang}">{code}</code></pre>'
    return (f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
            f"<p>{'LangGraph builds agents as graphs. ' * 20}</p>"
            f"{block * 3}<p>{install}</p></main></body></html>")


PY = doc("LangGraph overview", "python", "graph = StateGraph(State)", "pip install -U langgraph")
JS = doc("LangGraph overview", "typescript", "const graph = new StateGraph(State)",
         "npm install @langchain/langgraph")


def _found(url):
    best = Candidate(url, "pypi:Documentation", 0.9, "e", True, "identified")
    best.signals = ["registry-agreement", "names-it:40"]
    return Resolution(name="langgraph", candidates=[best], best=best, resolved_via="registry",
                      release="1.2.0")


def test_the_edition_asked_for_is_found_beside_the_one_resolved(monkeypatch):
    site = Site({
        "https://docs.x.com/oss/python/langgraph/overview": PY,
        "https://docs.x.com/oss/javascript/langgraph/overview": JS,
    })
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda *a, **k: _found("https://docs.x.com/oss/python/langgraph/overview"))
    got = resolver.resolve("langgraph", fetcher=site, use_memory=False, language="node")
    assert got.best.url == "https://docs.x.com/oss/javascript/langgraph/overview"
    assert got.language == "javascript"
    assert got.release == "", "the default edition's package release is not this one's"
    assert "javascript edition" in got.note


def test_the_edition_already_resolved_is_kept(monkeypatch):
    site = Site({"https://docs.x.com/oss/python/langgraph/overview": PY})
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda *a, **k: _found("https://docs.x.com/oss/python/langgraph/overview"))
    got = resolver.resolve("langgraph", fetcher=site, use_memory=False, language="python")
    assert got.best.url.endswith("/oss/python/langgraph/overview")


def test_a_variant_the_site_does_not_have_is_not_invented(monkeypatch):
    site = Site({"https://docs.x.com/oss/python/langgraph/overview": PY})
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda name, eco="", *a, **k:
                        _found("https://docs.x.com/oss/python/langgraph/overview")
                        if not eco else Resolution(name=name))
    got = resolver.resolve("langgraph", fetcher=site, use_memory=False, language="rust")
    assert got.best.url.endswith("/oss/python/langgraph/overview")
    assert "No rust edition" in got.note


def test_where_an_edition_would_be():
    lang = languages.canonical("python")
    swapped = resolver._variant_urls("https://d.dev/oss/javascript/x/overview", lang)
    assert swapped[0] == "https://d.dev/oss/python/x/overview"
    prefixed = resolver._variant_urls("https://playwright.dev/docs/intro", lang)
    assert "https://playwright.dev/python/docs/intro" in prefixed
    host = resolver._variant_urls("https://js.x.com/docs/", lang)
    assert "https://python.x.com/docs/" in host


def test_a_search_keeps_to_the_registry_the_caller_named():
    site = Site({"https://registry.npmjs.org/-/v1/search?text=langgraph&size=4":
                 json.dumps({"objects": []})})
    resolver.from_search("langgraph", site, ecosystem="npm")
    assert not [u for u in site.asked if "crates.io" in u]


def test_a_search_hit_is_judged_against_its_own_entry_only_when_it_is_the_name():
    npm = {"objects": [
        {"package": {"name": "@langchain/langgraph", "links": {
            "homepage": "https://github.com/langchain-ai/langgraphjs#readme",
            "repository": "git+ssh://git@github.com/langchain-ai/langgraphjs.git"}}},
        {"package": {"name": "@langchain/langgraph-sdk", "links": {
            "homepage": "https://example.com/sdk"}}},
    ]}
    site = Site({"https://registry.npmjs.org/-/v1/search?text=langgraph&size=4": json.dumps(npm)})
    found = resolver.from_search("langgraph", site, ecosystem="npm")
    by_url = {c.url: c for c in found}
    readme = by_url["https://github.com/langchain-ai/langgraphjs#readme"]
    assert readme.facts.get("homepage") and readme.facts.get("repository")
    assert "https://github.com/langchain-ai/langgraphjs" in by_url, "the git+ssh remote, cleaned"
    assert by_url["https://example.com/sdk"].facts == {}, "a near miss vouches for nothing"


def test_a_winning_repository_gives_way_to_the_site_it_declares():
    best = Candidate("https://github.com/o/langgraphjs#readme", "npm:homepage", 0.35, "e", True)
    result = Resolution(name="@o/langgraph", candidates=[best], best=best,
                        resolved_via="registry")
    site = Site({
        "https://api.github.com/repos/o/langgraphjs":
            json.dumps({"homepage": "https://docs.o.dev/oss/javascript/langgraph/"}),
        "https://docs.o.dev/oss/javascript/langgraph/":
            doc("LangGraph", "typescript", "x", "npm install @o/langgraph")
            .replace("</main>", '<a href="https://github.com/o/langgraphjs">source</a></main>'),
    })
    resolver._prefer_docs_behind_repo(result, "@o/langgraph", site)
    assert result.best.url == "https://docs.o.dev/oss/javascript/langgraph/"
    assert "repo-homepage" in result.resolved_via


def test_a_language_resolves_to_its_own_manual(monkeypatch):
    site = Site({"https://go.dev/doc/":
                 "<html><head><title>Documentation - The Go Programming Language</title>"
                 "</head><body><main>" + "Go is an open source language. " * 30
                 + "</main></body></html>"})
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ladder")))
    got = resolver.resolve("go", fetcher=site, use_memory=False)
    assert got.best.url == "https://go.dev/doc/" and got.resolved_via == "official"


def test_a_named_registry_means_a_package_not_the_language(monkeypatch):
    called = {}
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda name, eco="", *a, **k: called.setdefault("eco", eco) and
                        Resolution(name=name))
    resolver.resolve("go", ecosystem="crates", fetcher=Site({}), use_memory=False)
    assert called["eco"] == "crates"


def test_editions_are_remembered_apart(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(tmp_path / "r.json"))
    for lang, url in (("", "https://d.dev/py/"), ("javascript", "https://d.dev/js/")):
        best = Candidate(url, "x", 0.9, "e", True, "ok")
        resolver.remember("langgraph", Resolution(name="langgraph", best=best,
                                                  candidates=[best], language=lang), lang)
    assert resolver.recall("langgraph").best.url == "https://d.dev/py/"
    assert resolver.recall("langgraph", "javascript").best.url == "https://d.dev/js/"
    resolver.forget_resolution("langgraph")
    assert resolver.recall("langgraph", "javascript") is None
