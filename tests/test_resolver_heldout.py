"""
The resolver fixes found by the held-out test of 2026-09-24.

`scripts/heldout.py` resolved 53 names never used while DocsForge was being
fixed; 33 came back right. The wrong ones fell into a few kinds, each pinned
here on the site it was found on:

* a same-named package won over what the name usually means -- `redis` was
  the Python client's docs, `helm` a Rust crate, `rails` a stranger's repo;
* a language binding won over the project's own site -- `docker` was
  `docker-py.readthedocs.io`;
* a package index's page was taken for documentation -- `uv` was
  `pypi.org/project/uv/`;
* a front page won over its own docs host -- `streamlit.io` beside a verified
  `docs.streamlit.io`;
* `docs.solidjs.com` could not be `solid-js`'s own domain, and `tokio-rs/axum`
  declares no homepage, so both stopped at the repository;
* one language's edition was never found where the site files it in an
  `llms.txt`, behind a hub page, under a `.md` name or on a sibling host --
  and a login page that repeats any path was taken for one.

No network: pages come from dicts.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from docsforge.core import languages, resolver
from docsforge.core.resolver import Candidate, Resolution


class Resp:
    def __init__(self, text="", status=200, ctype="text/html", url=""):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.url = url


class Site:
    def __init__(self, pages, redirects=None):
        self.pages = pages
        self.redirects = redirects or {}
        self.asked = []

    def get(self, url, **kw):
        self.asked.append(url)
        url = self.redirects.get(url, url)
        hit = self.pages.get(url)
        if hit is None:
            return Resp("not found", status=404, url=url)
        if isinstance(hit, Resp):
            return hit
        ctype = ("application/json" if hit.lstrip().startswith("{")
                 else "text/plain" if url.endswith(".txt") else "text/html")
        return Resp(hit, ctype=ctype, url=url)

    def text(self, url, **kw):
        r = self.get(url)
        if r.status_code != 200:
            raise resolver.ForgeError(f"HTTP {r.status_code} for {url}")
        return r.text

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_popularity():
    resolver._POPULAR.clear()
    yield
    resolver._POPULAR.clear()


def _search(*items):
    return json.dumps({"items": [
        {"name": n, "full_name": f, "stargazers_count": s, "homepage": h, "fork": False}
        for n, f, s, h in items]})


SEARCH = "https://api.github.com/search/repositories?"


def _with_search(pages, body):
    """A Site whose GitHub search answers `body` whatever the query."""
    site = Site(pages)
    plain = site.get

    def get(url, **kw):
        if url.startswith(SEARCH):
            site.asked.append(url)
            return Resp(body, ctype="application/json", url=url)
        return plain(url, **kw)

    site.get = get
    return site


def _answer(url, signals, registry=""):
    best = Candidate(url, f"{registry or 'domain'}:x", 0.9, "e", True, "identified")
    best.signals = signals
    best.registry = registry
    return Resolution(name="x", candidates=[best], best=best,
                      resolved_via="registry" if registry else "domain",
                      ecosystem=registry, release="5.0.0" if registry else "")


REDIS_IO = ("<html><head><title>Redis</title></head><body><main><h1>Redis docs</h1>"
            + "<p>Redis is an in-memory data store. Install Redis. </p>" * 30
            + '<a href="https://github.com/redis/redis">GitHub</a>'
            + "<pre><code>redis-cli PING</code></pre></main></body></html>")


# ─── popularity ──────────────────────────────────────────────────
def test_redis_is_the_database_not_the_python_client_pypi_files_under_the_word(monkeypatch):
    site = _with_search({"https://redis.io/docs/": REDIS_IO},
                        _search(("redis", "redis/redis", 76463, "http://redis.io"),
                                ("redis", "someone/redis", 900, "")))
    monkeypatch.setattr(resolver, "probe_docs_root",
                        lambda url, f: [Candidate("https://redis.io/docs/", "probe:/docs/", 0.7)])
    result = _answer("https://redis.readthedocs.io/en/latest/",
                     ["install:pypi", "names-it:40"], "pypi")
    resolver._weigh_popularity(result, "redis", site)
    assert result.best.url == "https://redis.io/docs/"
    assert result.resolved_via == "registry+popular"
    assert "redis/redis" in result.note and "76,463" in result.note
    # The client's release and registry were the client's, not Redis'.
    assert result.release == "" and result.ecosystem == ""


def test_an_answer_already_on_the_popular_projects_site_is_left_alone():
    site = _with_search({}, _search(("vue", "vuejs/vue", 209000, "http://v2.vuejs.org")))
    result = _answer("https://vuejs.org/guide/introduction", ["own-domain", "docs-host"])
    resolver._weigh_popularity(result, "vue", site)
    assert result.best.url == "https://vuejs.org/guide/introduction"
    assert result.resolved_via == "domain"


def test_docs_rs_is_the_same_project_only_for_the_same_crate():
    popular = {"repo": "clap-rs/clap", "stars": 16724, "homepage": "https://docs.rs/clap"}
    assert resolver._same_project_site("https://docs.rs/clap/latest/clap/", popular)
    assert not resolver._same_project_site("https://docs.rs/helm", popular)


def test_a_readthedocs_project_is_its_own_host():
    popular = {"repo": "psf/requests", "stars": 53000,
               "homepage": "https://requests.readthedocs.io"}
    assert resolver._same_project_site("https://requests.readthedocs.io/en/latest/", popular)
    assert not resolver._same_project_site("https://other.readthedocs.io/", popular)
    # ...whichever side carries the path (offline benchmark `requests_is_pypi`).
    popular["homepage"] = "https://requests.readthedocs.io/en/latest/"
    assert resolver._same_project_site("https://requests.readthedocs.io", popular)


def test_an_owner_page_is_one_project_per_first_segment():
    popular = {"repo": "a/tool", "stars": 5000, "homepage": "https://a.github.io/tool/"}
    assert resolver._same_project_site("https://a.github.io/tool/guide/", popular)
    assert not resolver._same_project_site("https://a.github.io/other/", popular)


def test_a_modest_repository_does_not_overrule_the_ladder():
    site = _with_search({}, _search(("biome", "someone/biome", 1200, "https://biome.example")))
    result = _answer("https://biomejs.dev/", ["own-domain", "names-it:30"])
    resolver._weigh_popularity(result, "biome", site)
    assert result.best.url == "https://biomejs.dev/"
    assert not any(u.startswith("https://biome.example") for u in site.asked)


def test_the_popular_site_must_still_pass_the_gate(monkeypatch):
    site = _with_search({"https://parked.example/": "<html><body>unrelated</body></html>"},
                        _search(("three.js", "mrdoob/three.js", 115832, "https://parked.example/")))
    monkeypatch.setattr(resolver, "probe_docs_root", lambda url, f: [])
    result = _answer("https://docs.three.dev/", ["own-domain", "names-it:12"])
    resolver._weigh_popularity(result, "three.js", site)
    assert result.best.url == "https://docs.three.dev/"


def test_a_scheme_less_homepage_is_read_as_https():
    site = _with_search({}, _search(("clap", "clap-rs/clap", 16724, "docs.rs/clap")))
    assert resolver._popular_repo("clap", site)["homepage"] == "https://docs.rs/clap"


def test_forks_and_bindings_are_not_what_the_name_means():
    body = json.loads(_search(("redis-py", "redis/redis-py", 13000, "https://redis.readthedocs.io"),
                              ("redis", "fork/redis", 90000, "https://evil.example")))
    body["items"][1]["fork"] = True
    assert resolver._popular_repo("redis", _with_search({}, json.dumps(body))) is None


def test_a_dotted_js_name_is_the_same_name():
    site = _with_search({}, _search(("three.js", "mrdoob/three.js", 115832, "https://threejs.org/")))
    assert resolver._popular_repo("three", site)["repo"] == "mrdoob/three.js"


def test_popularity_is_not_asked_when_the_caller_named_a_registry(monkeypatch):
    asked = []
    monkeypatch.setattr(resolver, "_weigh_popularity", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(resolver, "_official_answer", lambda *a: None)
    monkeypatch.setattr(resolver, "_resolve_uncached",
                        lambda *a, **k: _answer("https://redis.readthedocs.io/", ["install:pypi"], "pypi"))
    resolver.resolve("redis", ecosystem="pypi", fetcher=Site({}), use_memory=False)
    assert asked == []
    resolver.resolve("redis", fetcher=Site({}), use_memory=False)
    assert len(asked) == 1


def test_a_languages_own_manual_is_not_weighed(monkeypatch):
    asked = []
    monkeypatch.setattr(resolver, "_weigh_popularity", lambda *a, **k: asked.append(a))
    manual = Candidate("https://docs.python.org/3/", "official", 1.0, "e", True, "ok")
    monkeypatch.setattr(resolver, "_official_answer", lambda *a: Resolution(
        name="python", candidates=[manual], best=manual, resolved_via="official"))
    result = resolver.resolve("python", fetcher=Site({}), use_memory=False)
    assert result.resolved_via == "official"
    assert asked == []          # TheAlgorithms/Python has more stars than CPython


def test_a_rate_limited_search_learns_nothing_rather_than_failing():
    site = Site({})
    site.get = lambda url, **kw: Resp("{}", status=403, url=url)
    assert resolver._popular_repo("redis", site) is None


# ─── a binding is not the thing ──────────────────────────────────
@pytest.mark.parametrize("repo,name,client", [
    ("docker/docker-py", "docker", True),
    ("redis/redis-py", "redis", True),
    ("ghmlee/rust-docker", "docker", True),
    ("grpc/grpc-go", "grpc", True),
    ("mrdoob/three.js", "three", False),       # .js is the project itself
    ("solidjs/solid", "solid-js", False),
    ("pytorch/pytorch", "torch", False),       # a different name, not a binding
    ("pallets/flask", "flask", False),
    ("docker/docker-py", "docker-py", False),  # asked for by its own name
])
def test_a_binding_is_recognised_by_the_words_its_repository_adds(repo, name, client):
    assert resolver._is_a_client({"repository": f"https://github.com/{repo}"}, name) is client


def test_docker_py_cannot_unseat_dockers_own_site():
    held = Candidate("https://www.docker.com/", "domain:com", 0.75, "e", True, "ok")
    held.signals = ["own-domain", "names-it:46"]
    client = Candidate("https://docker-py.readthedocs.io", "pypi:Documentation", 0.92,
                       "e", True, "ok")
    client.signals = ["install:pypi", "names-it:14"]
    client.registry = "pypi"
    docs = Candidate("https://docs.docker.com/llms.txt", "probe:docs.llms.txt", 0.96,
                     "e", True, "ok")
    docs.signals = ["own-domain", "docs-host", "names-it:92"]
    picked = resolver._settle_held(held, [docs, client, held], frozenset({id(client)}))
    assert picked is docs


# ─── a front page gives way to its docs host ─────────────────────
def test_streamlit_io_gives_way_to_docs_streamlit_io():
    front = Candidate("https://streamlit.io/", "domain:io", 0.75, "e", True, "ok")
    front.signals = ["own-domain", "install:pypi", "repo-backlink", "names-it:101"]
    docs = Candidate("https://docs.streamlit.io/llms.txt", "domain:io/probe", 0.97,
                     "e", True, "ok")
    docs.signals = ["own-domain", "docs-host", "names-it:266"]
    held = Candidate("https://streamlit.io/", "domain:io", 0.75, "e", True, "ok")
    held.signals = ["own-domain", "names-it:101"]
    assert resolver._settle_held(held, [docs, front]) is docs


def test_a_documentation_page_is_not_swapped_for_another():
    held = Candidate("https://pydantic.dev/docs/validation/latest/", "domain:dev", 0.75,
                     "e", True, "ok")
    held.signals = ["own-domain", "names-it:50"]
    other = Candidate("https://docs.pydantic.dev/", "domain:dev/probe", 0.97, "e", True, "ok")
    other.signals = ["own-domain", "docs-host", "names-it:80"]
    assert resolver._docs_behind(held, [held, other]) is held


# ─── what is not documentation, and what is its own domain ───────
def test_a_package_index_page_is_never_documentation():
    site = Site({"https://pypi.org/project/uv/": "<html><body>" + "uv " * 400 + "</body></html>"})
    cand = resolver.verify(Candidate("https://pypi.org/project/uv/", "pypi:Homepage", 0.5),
                           "uv", site)
    assert cand.verified is False
    assert site.asked == []                    # not even fetched
    # docs.rs and pkg.go.dev *are* documentation.
    assert not resolver._is_index_listing("https://docs.rs/axum")
    assert not resolver._is_index_listing("https://pkg.go.dev/github.com/spf13/cobra")


def test_solidjs_com_is_solid_js_own_domain():
    assert resolver._owns_the_name("https://docs.solidjs.com/", resolver.normalise("solid-js"))
    assert resolver._owns_the_name("https://nodejs.org/", "node")
    assert not resolver._owns_the_name("https://solidjsx.com/", "solid")


# ─── a repository that won, and the documentation behind it ──────
def test_axum_declares_no_homepage_and_is_documented_on_docs_rs():
    rustdoc = ("<html><head><title>axum - Rust</title></head><body><main>"
               + "<p>axum is a web application framework. axum routes requests.</p>" * 20
               + '<a href="https://github.com/tokio-rs/axum">Repository</a>'
               + "</main></body></html>")
    site = Site({"https://api.github.com/repos/tokio-rs/axum": json.dumps({"homepage": ""}),
                 "https://docs.rs/axum": rustdoc})
    result = _answer("https://github.com/tokio-rs/axum", ["repo-identity", "names-it:30"],
                     "crates")
    resolver._prefer_docs_behind_repo(result, "axum", site)
    assert result.best.url == "https://docs.rs/axum"
    assert "docs.rs builds every crate" in result.note


def test_a_manifest_that_fails_leaves_its_site_to_be_tried():
    options = resolver._with_roots([Candidate("https://docs.solidjs.com/llms.txt",
                                              "probe:docs.llms.txt", 0.96)])
    assert [o.url for o in options] == ["https://docs.solidjs.com/llms.txt",
                                        "https://docs.solidjs.com/"]


# ─── language editions ───────────────────────────────────────────
PY_PAGE = ("<html><body><main><h1>Sentry for Python</h1>"
           + '<pre><code class="language-python">import sentry_sdk\nsentry_sdk.init()</code></pre>' * 3
           + "<p>pip install sentry-sdk</p></main></body></html>")


def test_sentry_python_is_read_from_an_llms_txt_whose_links_end_in_md():
    python = languages.canonical("python")
    manifest = ("# Sentry\n- [Python](https://docs.sentry.io/platforms/python.md)\n"
                "- [Python Django](https://docs.sentry.io/platforms/python/integrations/django.md)\n"
                "- [JavaScript](https://docs.sentry.io/platforms/javascript.md)\n")
    links = resolver._language_links(manifest, "https://docs.sentry.io/llms.txt", python,
                                     "sentry")
    assert links[0] == "https://docs.sentry.io/platforms/python/"


def test_temporal_files_its_typescript_guide_on_a_sibling_host():
    ts = languages.canonical("typescript")
    front = '<a href="https://docs.temporal.io/develop/typescript/set-up">TypeScript SDK</a>'
    links = resolver._language_links(front, "https://temporal.io/", ts, "temporal")
    assert "https://docs.temporal.io/develop/typescript/" in links
    # A stranger's host is not a sibling.
    assert resolver._language_links('<a href="https://evil.io/develop/typescript/x">',
                                    "https://temporal.io/", ts, "temporal") == []


def test_grpc_writes_unquoted_hrefs_and_lists_its_languages_on_a_hub():
    go = languages.canonical("go")
    docs = "<nav><a href=/docs/guides/>Guides</a><a href=/docs/languages/>Languages</a></nav>"
    hub = "<ul><li><a href=/docs/languages/go/>Go</a><li><a href=/docs/languages/java/>Java</a></ul>"
    site = Site({"https://grpc.io/docs/": docs, "https://grpc.io/docs/languages/": hub,
                 "https://grpc.io/docs/languages/go/": (
                     "<html><body><main><h1>Go</h1>"
                     + '<pre><code class="language-go">package main\nfunc main() { x := 1 }</code></pre>' * 3
                     + "<p>go get google.golang.org/grpc</p></main></body></html>")})
    assert resolver._language_links(docs, "https://grpc.io/docs/", go, "grpc") == []
    assert resolver._language_hubs(docs, "https://grpc.io/docs/") == [
        "https://grpc.io/docs/languages/"]
    best = Candidate("https://grpc.io/docs/", "domain:io/probe", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "docs-host"]
    result = Resolution(name="grpc", candidates=[best], best=best, resolved_via="domain")
    assert resolver._switch_to_language(result, "grpc", go, site, 6)
    assert result.best.url == "https://grpc.io/docs/languages/go/"


def test_opentelemetry_editions_are_its_manifests_directorys():
    go = languages.canonical("go")
    urls = resolver._variant_urls("https://opentelemetry.io/", go)
    assert "https://opentelemetry.io/go/" in urls


def test_a_login_page_that_repeats_the_path_is_not_an_edition():
    python = languages.canonical("python")
    login = "<html><body><h1>Log in</h1><p>Sign in to continue.</p></body></html>"
    site = Site({"https://sentry.io/": "<html><body><h1>Sentry</h1></body></html>",
                 "https://sentry.io/auth/login/python/": login},
                redirects={"https://sentry.io/python/": "https://sentry.io/auth/login/python/"})
    best = Candidate("https://sentry.io/", "domain:io", 0.9, "e", True, "ok")
    best.signals = ["own-domain"]
    result = Resolution(name="sentry", candidates=[best], best=best, resolved_via="domain")
    resolver._switch_to_language(result, "sentry", python, site, 6)
    assert "login" not in result.best.url


def test_a_site_that_answers_any_path_cannot_vouch_for_an_edition():
    python = languages.canonical("python")
    everything = Resp("<html><body><h1>Welcome</h1></body></html>")
    site = Site({})
    site.get = lambda url, **kw: Resp(everything.text, url=url)
    assert resolver._answers_anything("https://example.com/python/", python, site)
    fussy = Site({"https://example.com/python/": "<html></html>"})
    assert not resolver._answers_anything("https://example.com/python/", python, fussy)


def test_nestjs_means_nestjs_nest_not_the_repository_called_nestjs():
    """The search's top result is `nestjs/nest`; a third party's `nestjs`
    monorepo is the most-starred exact name, and is not what it means."""
    site = _with_search({}, _search(("nest", "nestjs/nest", 76726, "https://nestjs.com"),
                                    ("awesome-nestjs", "nestjs/awesome-nestjs", 13148, ""),
                                    ("nestjs", "golevelup/nestjs", 2600,
                                     "https://golevelup.github.io/nestjs/")))
    assert resolver._popular_repo("nestjs", site) is None


# ─── round 2 (57 fresh names) ────────────────────────────────────
def test_scikit_learn_on_npm_declares_an_scp_remote_and_nothing_crashes():
    """`git+ssh://git@github.com:owner/repo`: the colon is a path separator."""
    assert resolver._clean_repo("git+ssh://git@github.com:burkostya/scikit-learn.git") == \
        "https://github.com/burkostya/scikit-learn"
    assert resolver._clean_repo("https://git.example.com:8443/a/b") == \
        "https://git.example.com:8443/a/b"
    # And a malformed address that gets through anyway is a key, not a crash.
    assert resolver._same_page("https://github.com:burkostya/x") == "https://github.com/x"


def test_koa_a_dead_domain_from_an_unrelated_crate_is_a_finding_not_a_pause():
    dns = resolver.ForgeError(
        "Request failed for https://www.fedfans.com: HTTPSConnectionPool(host='www.fedfans.com'"
        ", port=443): Max retries exceeded (Caused by NameResolutionError(\"Failed to resolve "
        "'www.fedfans.com' ([Errno 11001] getaddrinfo failed)\"))")
    assert resolver._transient(dns) is False
    slow = resolver.ForgeError("Request failed for https://x.dev/: Read timed out.")
    assert resolver._transient(slow) is True


def test_gradio_declares_an_http_homepage_that_only_answers_https():
    assert resolver._declared("http://www.gradio.app") == "https://www.gradio.app"
    assert resolver._declared("docs.rs/clap") == "https://docs.rs/clap"
    assert resolver._declared("not a url") == ""


def test_a_repository_that_won_takes_the_site_the_search_says_it_declares(monkeypatch):
    gradio = REDIS_IO.replace("Redis", "Gradio").replace("redis", "gradio") \
        .replace("github.com/gradio/gradio", "github.com/gradio-app/gradio")
    site = _with_search({"https://www.gradio.app": gradio},
                        _search(("gradio", "gradio-app/gradio", 43616, "http://www.gradio.app")))
    monkeypatch.setattr(resolver, "probe_docs_root", lambda url, f: [])
    result = _answer("https://github.com/gradio-app/gradio", ["repo-identity", "names-it:76"])
    resolver._weigh_popularity(result, "gradio", site)
    assert result.best.url == "https://www.gradio.app"
    assert "declares https://www.gradio.app" in result.note


MYPY_FRONT = ('<html><body><h1>mypy</h1><nav><a href="/news">News</a>'
              '<a href="https://mypy.readthedocs.io/en/stable/">Documentation</a>'
              '<a href="https://github.com/python/mypy">GitHub</a></nav>'
              + "<p>mypy is a static type checker. Install mypy. </p>" * 20 + "</body></html>")
MYPY_DOCS = ("<html><head><title>mypy documentation</title></head><body><main><h1>Welcome to "
             "mypy documentation</h1>" + "<p>mypy checks types; run mypy on a program.</p>" * 30
             + '<a href="https://github.com/python/mypy">source</a></main></body></html>')


def test_mypy_lang_org_is_a_front_page_and_its_documentation_link_is_the_answer():
    site = Site({"https://mypy-lang.org/": MYPY_FRONT,
                 "https://mypy.readthedocs.io/en/stable/": MYPY_DOCS})
    best = Candidate("https://mypy-lang.org/", "domain:org", 0.75, "e", True, "ok")
    best.signals = ["own-domain", "repo-backlink", "names-it:20"]
    result = Resolution(name="mypy", candidates=[best], best=best, resolved_via="domain")
    resolver._front_page_docs(result, "mypy", site)
    assert result.best.url == "https://mypy.readthedocs.io/en/stable/"
    assert result.resolved_via == "domain+docs"


def test_a_documentation_page_already_verified_on_the_site_costs_no_request():
    front = Candidate("https://symfony.com/", "domain:com", 0.75, "e", True, "ok")
    front.signals = ["own-domain", "names-it:50"]
    docs = Candidate("https://symfony.com/doc", "domain:com/probe", 0.72, "e", True, "ok")
    docs.signals = ["own-domain", "names-it:40"]
    site = Site({})
    result = Resolution(name="symfony", candidates=[front, docs], best=front,
                        resolved_via="domain")
    resolver._front_page_docs(result, "symfony", site)
    assert result.best is docs and site.asked == []


DAPR_MANIFEST = ("# Dapr\n\n> A runtime.\n\n## Core pages\n\n- [Home](https://dapr.io/): Overview\n"
                 "- [Community](https://dapr.io/community/): Discord\n\n## Documentation\n\n"
                 "- [Official docs](https://docs.dapr.io/): Concepts, getting started\n"
                 "- [Blog](https://blog.dapr.io/): News\n")
DAPR_DOCS = ("<html><head><title>Dapr Docs</title></head><body><main><h1>Dapr documentation</h1>"
             + "<p>Dapr is a portable runtime. Run dapr init to start with dapr.</p>" * 30
             + "</main></body></html>")


def test_dapr_io_llms_txt_is_a_front_page_and_its_official_docs_are_the_answer():
    site = Site({"https://dapr.io/llms.txt": DAPR_MANIFEST, "https://docs.dapr.io/": DAPR_DOCS})
    best = Candidate("https://dapr.io/llms.txt", "domain:io/probe:llms.txt", 0.97, "e", True, "ok")
    best.signals = ["own-domain", "names-it:8"]
    result = Resolution(name="dapr", candidates=[best], best=best, resolved_via="domain")
    resolver._front_page_docs(result, "dapr", site)
    assert result.best.url == "https://docs.dapr.io/"


def test_a_long_site_manifest_lists_the_documentation_and_stays():
    long_manifest = DAPR_MANIFEST + "".join(
        f"- [Page {i}](https://dapr.io/docs/p{i}/): A page of the documentation.\n"
        for i in range(60))
    site = Site({"https://dapr.io/llms.txt": long_manifest, "https://docs.dapr.io/": DAPR_DOCS})
    best = Candidate("https://dapr.io/llms.txt", "domain:io/probe:llms.txt", 0.97, "e", True, "ok")
    best.signals = ["own-domain", "names-it:8"]
    result = Resolution(name="dapr", candidates=[best], best=best, resolved_via="domain")
    resolver._front_page_docs(result, "dapr", site)
    assert result.best is best


def test_a_page_that_is_already_documentation_is_left_alone():
    best = Candidate("https://docs.x.dev/", "domain:dev", 0.75, "e", True, "ok")
    best.signals = ["own-domain", "docs-host"]
    site = Site({})
    result = Resolution(name="x", candidates=[best], best=best, resolved_via="domain")
    resolver._front_page_docs(result, "x", site)
    assert result.best is best and site.asked == []


def test_zustand_documents_itself_where_only_its_readme_says():
    readme = ('<article>See <a href="https://zustand-demo.pmnd.rs/">the demo</a> and '
              'the docs at https://zustand.docs.pmnd.rs/ -- '
              'https://zustand.docs.pmnd.rs/learn/getting-started/comparison '
              '<img src="https://img.shields.io/npm/v/zustand"></article>')
    site = Site({"https://github.com/pmndrs/zustand": readme})
    found = resolver._readme_docs("https://github.com/pmndrs/zustand", "zustand", site, set())
    assert [c.url for c in found] == ["https://zustand.docs.pmnd.rs/"]


def test_elastic_files_its_python_client_under_python_api():
    python = languages.canonical("python")
    go = languages.canonical("go")
    url = "https://www.elastic.co/guide/en/elasticsearch/client/python-api/current/index.html"
    assert resolver._names_language(url, python)
    assert not resolver._names_language("https://x.io/go-live/", go)   # too short to trust
    assert resolver._names_language("https://x.io/docs/golang-sdk/", go)


def test_the_hub_that_names_the_project_comes_before_a_sibling_products():
    page = ('<a href="en/enterprise-search-clients/index.html">Enterprise Search clients</a>'
            '<a href="en/elasticsearch/client/index.html">Elasticsearch clients</a>')
    hubs = resolver._language_hubs(page, "https://www.elastic.co/guide/index.html",
                                   name="elasticsearch")
    assert hubs[0] == "https://www.elastic.co/guide/en/elasticsearch/client/index.html"


# ─── round 3 (56 fresh names), and the round-1 re-run it prompted ──
def test_a_compound_names_a_language_only_beside_an_edition_word():
    """`event-history-typescript` is one encyclopedia page, not an edition."""
    ts = languages.canonical("typescript")
    assert not resolver._names_language(
        "https://docs.temporal.io/encyclopedia/event-history/event-history-typescript", ts)
    assert resolver._names_language("https://x.dev/docs/typescript-sdk/", ts)


def test_redis_files_its_python_client_as_redis_py():
    """The project and its language, however short the language's word."""
    python = languages.canonical("python")
    go = languages.canonical("go")
    url = "https://redis.io/docs/latest/develop/clients/redis-py/"
    assert resolver._names_language(url, python, "redis")
    assert not resolver._names_language(url, python)            # without knowing the name
    assert resolver._names_language("https://redis.io/docs/latest/develop/clients/go-redis/",
                                    go, "redis")
    manifest = ("- [FastAPI](https://redis.io/tutorials/develop/python/fastapi.md)\n"
                "- [Flask](https://redis.io/tutorials/develop/python/flask.md)\n"
                "- [Streams](https://redis.io/tutorials/develop/python/streams.md)\n"
                "- [redis-py](https://redis.io/docs/latest/develop/clients/redis-py.md)\n")
    links = resolver._language_links(manifest, "https://redis.io/llms.txt", python, "redis")
    assert links[0] == "https://redis.io/docs/latest/develop/clients/redis-py/"


def test_a_docs_host_beside_the_front_page_is_not_held_to_the_root_rule():
    """`traefik.io`'s "Docs" is `doc.traefik.io/`: another host, whose root is the docs."""
    front = ('<html><body><a href="https://doc.traefik.io/">Docs</a>'
             + "<p>Traefik is a reverse proxy. </p>" * 20 + "</body></html>")
    docs = ("<html><head><title>Traefik Docs</title></head><body><main><h1>Traefik</h1>"
            + "<p>Traefik is an edge router; configure traefik with providers.</p>" * 30
            + "</main></body></html>")
    site = Site({"https://traefik.io": front, "https://doc.traefik.io/": docs})
    best = Candidate("https://traefik.io", "popular:github", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "registry-agreement"]
    result = Resolution(name="traefik", candidates=[best], best=best, resolved_via="popular")
    resolver._front_page_docs(result, "traefik", site)
    assert result.best.url == "https://doc.traefik.io/"


@pytest.mark.parametrize("url,root", [
    ("https://symfony.com/doc", True),
    ("https://symfony.com/doc/current/index.html", True),
    ("https://nginx.org/en/docs/", True),
    ("https://www.elastic.co/guide/index.html", True),
    ("https://biomejs.dev/guides/getting-started", False),   # a page inside a section
    ("https://fastapi.tiangolo.com/reference/openapi/docs/", False),
])
def test_a_front_page_link_on_its_own_site_must_be_a_docs_root(url, root):
    assert resolver._docs_root(url) is root


def test_sympy_org_page_gives_way_to_docs_sympy_org():
    docs = ("<html><head><title>SymPy documentation</title></head><body><main>"
            "<h1>Welcome to SymPy's documentation!</h1>"
            + "<p>SymPy is a Python library for symbolic mathematics. import sympy.</p>" * 30
            + "</main></body></html>")
    site = Site({"https://docs.sympy.org/": docs})
    best = Candidate("https://www.sympy.org/en/docs.html", "pypi:Documentation", 0.92, "e",
                     True, "ok")
    best.signals = ["own-domain", "registry-agreement", "names-it:20"]
    result = Resolution(name="sympy", candidates=[best], best=best, resolved_via="registry")
    resolver._docs_host_beside(result, "sympy", site)
    assert result.best.url == "https://docs.sympy.org/"
    assert result.resolved_via == "registry+docs-host"


def test_a_docs_host_that_is_the_front_page_under_another_name_is_not_taken():
    """`docs.helm.sh` serves Helm's front page, canonical `https://helm.sh/`."""
    alias = ('<html><head><title>Artboard</title>'
             '<link data-rh=true rel=canonical href=https://helm.sh/ /></head><body>'
             + "<p>Helm is the package manager for Kubernetes. helm install.</p>" * 20
             + "</body></html>")
    site = Site({"https://docs.helm.sh/": alias})
    best = Candidate("https://helm.sh/docs/", "popular:github", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "registry-agreement"]
    result = Resolution(name="helm", candidates=[best], best=best, resolved_via="popular")
    resolver._docs_host_beside(result, "helm", site)
    assert result.best is best


def test_a_docs_host_that_is_a_stub_back_to_the_site_is_not_taken():
    """`docs.diesel.rs` refreshes to `diesel.rs/docs`: the site already found."""
    stub = "<meta http-equiv=refresh content=0;url=https://diesel.rs/docs>"
    site = Site({"https://docs.diesel.rs/": stub,
                 "https://diesel.rs/docs": "<html><body>Diesel guides</body></html>"})
    best = Candidate("https://diesel.rs", "crates:homepage", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "repo-backlink"]
    result = Resolution(name="diesel", candidates=[best], best=best, resolved_via="registry")
    resolver._docs_host_beside(result, "diesel", site)
    assert result.best is best


def test_a_stub_to_somewhere_unreadable_is_not_a_docs_host():
    site = Site({"https://docs.diesel.rs/": "<meta http-equiv=refresh content=0;url=/gone>"})
    best = Candidate("https://diesel.rs", "crates:homepage", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "repo-backlink"]
    result = Resolution(name="diesel", candidates=[best], best=best, resolved_via="registry")
    resolver._docs_host_beside(result, "diesel", site)
    assert result.best is best


def test_a_generated_api_reference_is_not_a_better_answer_than_the_guides():
    """`docs.serde.rs` refreshes to Serde's rustdoc index; `serde.rs` is the guide."""
    rustdoc = ('<html><head><meta name="generator" content="rustdoc"><title>serde - Rust</title>'
               '</head><body>' + "<p>serde serializes; serde deserializes.</p>" * 30
               + "</body></html>")
    site = Site({"https://docs.serde.rs/": "<meta http-equiv=refresh content=0;url=serde/index.html>",
                 "https://docs.serde.rs/serde/index.html": rustdoc})
    best = Candidate("https://serde.rs", "crates:homepage", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "repo-backlink"]
    result = Resolution(name="serde", candidates=[best], best=best, resolved_via="registry")
    resolver._docs_host_beside(result, "serde", site)
    assert result.best is best


@pytest.mark.parametrize("stub,target", [
    ("<meta http-equiv=refresh content=0;url=serde/index.html>", "serde/index.html"),
    ('<meta http-equiv="refresh" content="0; url=overview.html">', "overview.html"),
])
def test_a_refresh_stub_is_read_quoted_or_not(stub, target):
    assert resolver._META_REFRESH.search(stub).group(1) == target


def test_jquery_documentation_link_to_its_contributors_guide_is_not_followed():
    front = ('<html><body><a href="https://contribute.jquery.org/documentation/">Documentation</a>'
             + "<p>jQuery is a JavaScript library. </p>" * 20 + "</body></html>")
    guide = ("<html><head><title>Documentation | jQuery</title></head><body><main>"
             + "<p>How to contribute to jQuery documentation.</p>" * 30 + "</main></body></html>")
    site = Site({"https://jquery.com/": front,
                 "https://contribute.jquery.org/documentation/": guide})
    best = Candidate("https://jquery.com/", "domain:com", 0.75, "e", True, "ok")
    best.signals = ["own-domain", "install:npm"]
    result = Resolution(name="jquery", candidates=[best], best=best, resolved_via="domain")
    resolver._front_page_docs(result, "jquery", site)
    assert result.best is best


def test_a_language_guide_comes_before_its_api_reference():
    """From `pulumi.com/docs`, `/reference/pkg/python/` outnumbers the guide."""
    python = languages.canonical("python")
    page = "".join(f'<a href="/docs/reference/pkg/python/pulumi_{i}/">m{i}</a>' for i in range(8))
    page += '<a href="/docs/iac/languages-sdks/python/">Python</a>'
    links = resolver._language_links(page, "https://www.pulumi.com/docs/", python, "pulumi")
    assert links[0] == "https://www.pulumi.com/docs/iac/languages-sdks/python/"


RAILS_HUB = ('<html><body><h1>Docs</h1><a href="https://guides.rubyonrails.org/">Guides</a>'
             '<a href="https://guides.rubyonrails.org/getting_started.html">Start</a>'
             '<a href="https://api.rubyonrails.org/">API</a>'
             + "<p>Ruby on Rails documentation, guides and API. </p>" * 10 + "</body></html>")
RAILS_GUIDES = ("<html><head><title>Ruby on Rails Guides</title></head><body><main>"
                "<h1>Rails Guides</h1>" + "<p>Rails is a web framework. rails new blog.</p>" * 30
                + '<a href="https://github.com/rails/rails">rails/rails</a></main></body></html>')


def test_rubyonrails_org_docs_is_a_hub_and_the_guides_are_the_manual():
    site = Site({"https://rubyonrails.org/docs": RAILS_HUB,
                 "https://guides.rubyonrails.org/": RAILS_GUIDES})
    resolver._POPULAR[("rails", "")] = {"repo": "rails/rails", "stars": 58000,
                                        "homepage": "https://rubyonrails.org"}
    best = Candidate("https://rubyonrails.org/docs", "popular:github", 0.9, "e", True, "ok")
    best.signals = ["registry-agreement", "names-it:20"]
    result = Resolution(name="rails", candidates=[best], best=best, resolved_via="popular")
    resolver._docs_host_beside(result, "rails", site)
    assert result.best.url == "https://guides.rubyonrails.org/"
    # ...and the guides' own "Docs" link back to the hub is not followed.
    resolver._front_page_docs(result, "rails", site)
    assert result.best.url == "https://guides.rubyonrails.org/"


def test_a_sibling_that_neither_carries_the_name_nor_links_the_repository_is_refused():
    hub = RAILS_HUB.replace("rubyonrails", "example")
    guides = RAILS_GUIDES.replace("github.com/rails/rails", "example.com/about")
    site = Site({"https://example.org/docs": hub, "https://guides.example.org/": guides})
    best = Candidate("https://example.org/docs", "domain:org", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "names-it:20"]
    result = Resolution(name="rails", candidates=[best], best=best, resolved_via="domain")
    resolver._docs_host_beside(result, "rails", site)
    assert result.best is best


def test_tokio_documents_itself_so_its_api_docs_link_is_not_the_answer():
    front = ('<html><body><a href="/tokio/tutorial">Docs</a>'
             '<a href="https://docs.rs/tokio">API docs</a>'
             + "<p>Tokio is an asynchronous runtime for Rust. </p>" * 20 + "</body></html>")
    site = Site({"https://tokio.rs/": front})
    best = Candidate("https://tokio.rs/", "crates:homepage", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "registry-agreement"]
    result = Resolution(name="tokio", candidates=[best], best=best, resolved_via="registry")
    resolver._front_page_docs(result, "tokio", site)
    assert result.best is best
    assert not any("docs.rs" in u for u in site.asked)


def test_a_docs_host_that_lands_on_a_preview_is_not_the_documentation():
    """`docs.jenkins.io` redirects to `alpha.docs.jenkins.io`."""
    preview = ("<html><head><title>Jenkins User Documentation</title></head><body><main>"
               + "<p>Jenkins is an automation server; install the jenkins package.</p>" * 30
               + "</main></body></html>")
    site = Site({"https://alpha.docs.jenkins.io/": preview},
                redirects={"https://docs.jenkins.io/": "https://alpha.docs.jenkins.io/"})
    best = Candidate("https://www.jenkins.io/", "domain:io", 0.75, "e", True, "ok")
    best.signals = ["own-domain", "names-it:30"]
    result = Resolution(name="jenkins", candidates=[best], best=best, resolved_via="domain")
    resolver._docs_host_beside(result, "jenkins", site)
    assert result.best is best


def test_gohugo_docs_redirects_out_of_itself_so_the_site_is_the_manual():
    about = ("<html><body><main><h1>About Hugo</h1>"
             + "<p>Hugo is a static site generator written in Go. </p>" * 20
             + "</main></body></html>")
    site = Site({"https://gohugo.io/about/": about},
                redirects={"https://gohugo.io/docs/": "https://gohugo.io/about/"})
    found = resolver.probe_docs_root("https://gohugo.io/", site)
    urls = [c.url for c in found]
    assert "https://gohugo.io/" in urls and "https://gohugo.io/about/" not in urls


def test_the_search_window_is_waited_out_rather_than_given_up_on(monkeypatch):
    import time as _time
    slept = []
    monkeypatch.setattr(resolver.time, "sleep", lambda s: slept.append(s))
    answers = [Resp("{}", status=403, url=SEARCH),
               Resp(_search(("kafka", "apache/kafka", 31000, "https://kafka.apache.org")),
                    ctype="application/json", url=SEARCH)]
    answers[0].headers["x-ratelimit-reset"] = str(int(_time.time()) + 40)
    site = Site({})
    site.get = lambda url, **kw: answers.pop(0)
    popular = resolver._popular_repo("kafka", site)
    assert popular and popular["repo"] == "apache/kafka"
    assert slept and 35 <= slept[0] <= resolver.SEARCH_WAIT_MAX + 1


def test_a_published_llms_txt_is_not_traded_for_a_docs_host():
    site = Site({"https://docs.redis.io/": "<html><body>docs</body></html>"})
    best = Candidate("https://redis.io/llms.txt", "popular:github", 0.9, "e", True, "ok")
    best.signals = ["own-domain", "registry-agreement"]
    result = Resolution(name="redis", candidates=[best], best=best, resolved_via="popular")
    resolver._docs_host_beside(result, "redis", site)
    assert result.best is best and site.asked == []


def test_a_rust_repository_that_declares_no_site_is_documented_on_docs_rs():
    body = json.dumps({"items": [{"name": "tonic", "full_name": "hyperium/tonic",
                                  "stargazers_count": 11000, "homepage": None,
                                  "language": "Rust", "fork": False}]})
    assert resolver._popular_repo("tonic", _with_search({}, body))["homepage"] == \
        "https://docs.rs/tonic"


def test_hexdocs_refresh_stub_is_followed_before_judging():
    front = ('<html><body><a href="https://phoenix.hexdocs.pm/">Docs</a>'
             + "<p>Phoenix is a web framework. </p>" * 20 + "</body></html>")
    stub = '<html><head><meta http-equiv="refresh" content="0; url=overview.html"></head></html>'
    docs = ("<html><head><title>Overview - Phoenix</title></head><body><main><h1>Phoenix</h1>"
            + "<p>Phoenix is a web development framework. mix phx.new creates a Phoenix app.</p>" * 30
            + '<a href="https://github.com/phoenixframework/phoenix">source</a></main></body></html>')
    site = Site({"https://www.phoenixframework.org": front, "https://phoenix.hexdocs.pm/": stub,
                 "https://phoenix.hexdocs.pm/overview.html": docs})
    resolver._POPULAR[("phoenix", "")] = {"repo": "phoenixframework/phoenix", "stars": 23000,
                                          "homepage": "https://www.phoenixframework.org"}
    best = Candidate("https://www.phoenixframework.org", "popular:github", 0.9, "e", True, "ok")
    best.signals = ["registry-agreement", "names-it:40"]
    result = Resolution(name="phoenix", candidates=[best], best=best, resolved_via="popular")
    resolver._front_page_docs(result, "phoenix", site)
    assert result.best.url == "https://phoenix.hexdocs.pm/overview.html"


def test_a_dominant_meaning_may_answer_a_refusal_caused_by_another_project(monkeypatch):
    """`tonic` was refused over a Python project's rate-limited readthedocs."""
    site = _with_search({"https://docs.rs/tonic": REDIS_IO.replace("Redis", "tonic")
                         .replace("redis/redis", "hyperium/tonic").replace("redis", "tonic")},
                        _search(("tonic", "hyperium/tonic", 11000, "https://docs.rs/tonic")))
    result = Resolution(name="tonic", candidates=[], best=None, unexamined=True,
                        note="https://tonic.readthedocs.org could not be read")
    resolver._weigh_popularity(result, "tonic", site)
    assert result.best is not None and result.best.url == "https://docs.rs/tonic"
    assert result.unexamined is False


def test_a_refusal_is_not_filled_by_a_modest_repository():
    site = _with_search({}, _search(("tonic", "someone/tonic", 900, "https://tonic.example/")))
    result = Resolution(name="tonic", candidates=[], best=None, unexamined=True)
    resolver._weigh_popularity(result, "tonic", site)
    assert result.best is None and result.unexamined is True
