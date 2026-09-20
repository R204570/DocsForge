"""Offline unit tests — no network. Run: python -m pytest -q"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.tools import forge_tools


class FakeResponse:
    def __init__(self, text="", status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {"content-type": "text/plain; charset=utf-8"}


class FakeFetcher:
    """Stands in for Fetcher during detection tests."""

    def __init__(self, bodies=None, statuses=None):
        self.bodies = bodies or {}
        self.statuses = statuses or {}
        self.calls = []

    def text(self, url, **kw):
        self.calls.append(url)
        if url not in self.bodies:
            raise df.ForgeError(f"404 {url}")
        return self.bodies[url]

    def get(self, url, **kw):
        self.calls.append(url)
        status = self.statuses.get(url, 404)
        return FakeResponse(self.bodies.get(url, ""), status,
                            {"content-type": "text/plain; charset=utf-8"})


# ── slugs ────────────────────────────────────────────────
def test_slug_distinguishes_hosts():
    a = df._slug("https://docs.a.com/")
    b = df._slug("https://docs.b.com/")
    assert a != b
    assert "docs-a-com" in a and "docs-b-com" in b


def test_slug_distinguishes_query_strings():
    assert df._slug("https://x.com/page?v=1") != df._slug("https://x.com/page?v=2")


def test_slug_is_filesystem_safe():
    slug = df._slug("https://x.com/a b/c:d?e=f#g")
    assert not set(slug) & set('<>:"/\\|?*')


# ── detection ────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/tiangolo/fastapi", "github"),
    ("https://example.com/llms.txt", "llms_txt"),
    ("https://example.com/llms-full.txt", "llms_txt"),
    ("https://example.com/sitemap.xml", "sitemap"),
    ("https://example.com/README.md", "raw_text"),
    ("https://example.com/guide/intro", "html"),
])
def test_detect_without_network(url, expected):
    assert df.detect_source(url, FakeFetcher()).kind == expected


def test_detect_openapi_keeps_body_so_handler_never_refetches():
    spec = json.dumps({"openapi": "3.0.0", "info": {"title": "T"}, "paths": {}})
    f = FakeFetcher({"https://api.x.com/openapi.json": spec})
    det = df.detect_source("https://api.x.com/openapi.json", f)
    assert det.kind == "openapi"
    assert det.body == spec
    assert len(f.calls) == 1  # probed exactly once

    df.handle_openapi(det, f, df.Options(verbose=False))
    assert len(f.calls) == 1  # handler reused the probe body


def test_detect_json_that_is_not_openapi_falls_back_to_raw():
    f = FakeFetcher({"https://x.com/data.json": '{"hello": "world"}'})
    assert df.detect_source("https://x.com/data.json", f).kind == "raw_text"


# ── llms.txt: index versus full dump ─────────────────────
INDEX = "# Docs\n\n- [Full Docs](https://x.dev/llms-full.txt): everything\n"
DUMP = "# x\n\n" + ("Real documentation. " * 200)


def _fetcher(pages):
    return FakeFetcher(pages, {url: 200 for url in pages})


def test_an_index_is_not_taken_when_a_fuller_file_sits_beside_it():
    """The measured F9 failure.

    detect_source already preferred llms-full.txt — the probe just sat below an
    early return that fired on any URL ending in llms.txt, which is exactly
    what the resolver hands over. Two correct components, wrong together.
    """
    f = _fetcher({"https://x.dev/llms.txt": INDEX,
                  "https://x.dev/llms-full.txt": DUMP})
    det = df.detect_source("https://x.dev/llms.txt", f)
    assert det.url == "https://x.dev/llms-full.txt"
    assert det.body == DUMP


def test_a_dump_beside_the_index_is_found_in_its_own_directory():
    """Prisma publishes /docs/llms-full.txt, not /llms-full.txt."""
    f = _fetcher({"https://x.dev/docs/llms.txt": INDEX,
                  "https://x.dev/docs/llms-full.txt": DUMP})
    det = df.detect_source("https://x.dev/docs/llms.txt", f)
    assert det.url == "https://x.dev/docs/llms-full.txt"


def test_an_index_with_no_fuller_file_is_still_used():
    f = _fetcher({"https://x.dev/llms.txt": INDEX})
    det = df.detect_source("https://x.dev/llms.txt", f)
    assert det.kind == "llms_txt"
    assert det.url == "https://x.dev/llms.txt"


def test_a_full_dump_url_is_never_second_guessed():
    f = _fetcher({"https://x.dev/llms-full.txt": DUMP})
    det = df.detect_source("https://x.dev/llms-full.txt", f)
    assert det.url == "https://x.dev/llms-full.txt"
    assert f.calls == [], "it already had the answer; no probing needed"


# ── splitting a dump into searchable pages ───────────────
def test_a_large_dump_is_split_on_its_own_headings():
    """5.7 MB stored as one page is unsearchable: every query matches page 1,
    and ranking has nothing to choose between."""
    body = "".join(f"## Section {i}\n\n{'text ' * 400}\n\n" for i in range(20))
    parts = df._split_dump(body, above=0)
    assert len(parts) == 20
    assert parts[0][0] == "Section 0"


def test_a_small_dump_is_left_whole():
    assert df._split_dump("## A\n\ntiny\n\n## B\n\ntiny") == []


def test_a_preamble_before_the_first_heading_is_kept():
    body = "<SYSTEM>banner</SYSTEM>\n\n" + "".join(
        f"## S{i}\n\n{'text ' * 400}\n\n" for i in range(10))
    parts = df._split_dump(body, above=0)
    assert len(parts) == 11
    assert "<" not in parts[0][0], "a markup banner is not a page title"


def test_splitting_loses_nothing():
    body = "".join(f"## S{i}\n\n{'text ' * 400}\n\n" for i in range(15))
    joined = "\n".join(chunk for _, chunk in df._split_dump(body, above=0))
    assert body.split() == joined.split(), "every word survives the split"


# ── a homepage harvest must not return the blog ──────────
def test_a_whole_host_harvest_keeps_the_docs_and_drops_the_marketing():
    """Measured: harvesting `astro.build` returned 34 blog posts out of 40
    pages and not one page of documentation."""
    urls = ([f"https://astro.build/blog/post-{i}" for i in range(30)]
            + [f"https://astro.build/docs/guide-{i}" for i in range(8)]
            + ["https://astro.build/agencies", "https://astro.build/pricing"])
    kept = df._focus_on_docs(urls, "/")
    assert all("/docs/" in u for u in kept)
    assert len(kept) == 8


def test_marketing_sections_are_dropped_even_with_no_docs_section():
    urls = ["https://x.dev/blog/a", "https://x.dev/careers",
            "https://x.dev/getting-started", "https://x.dev/install"]
    kept = df._focus_on_docs(urls, "/")
    assert "https://x.dev/blog/a" not in kept
    assert "https://x.dev/getting-started" in kept


def test_a_scoped_harvest_is_left_alone():
    """The filter is for the case where resolution landed on a homepage. A
    caller who named a section meant that section."""
    urls = ["https://x.dev/docs/blog-plugin", "https://x.dev/docs/news-feed"]
    assert df._focus_on_docs(urls, "/docs/") == urls


def test_one_language_is_harvested_not_twenty():
    """docs.astro.build lists 5,880 URLs across every translation, sorted by
    locale — so a capped harvest returned Arabic and stopped before English."""
    urls = ([f"https://d.dev/ar/page-{i}" for i in range(20)]
            + [f"https://d.dev/en/page-{i}" for i in range(20)]
            + [f"https://d.dev/zh/page-{i}" for i in range(20)])
    kept = df._prefer_default_locale(urls)
    assert len(kept) == 20
    assert all("/en/" in u for u in kept)


def test_a_section_that_looks_like_a_locale_is_not_dropped():
    """`/go/`, `/js/` and `/ai/` are sections, not languages. Treating any two
    letters as a locale would silently lose real documentation."""
    urls = ([f"https://d.dev/go/page-{i}" for i in range(5)]
            + [f"https://d.dev/js/page-{i}" for i in range(5)])
    assert df._prefer_default_locale(urls) == urls


def test_an_untranslated_site_is_untouched():
    urls = [f"https://d.dev/guide/page-{i}" for i in range(5)]
    assert df._prefer_default_locale(urls) == urls


# ── the CLI can take a harvest back out ──────────────────
@pytest.fixture
def cli_store(tmp_path, monkeypatch):
    from docsforge.store.kb_store import build_store

    monkeypatch.setenv("DOCSFORGE_KB_ROOT", str(tmp_path))
    monkeypatch.delenv("DOCSFORGE_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = build_store()
    for version in ("1.10", "2.11"):
        store.save("pydantic", version, f"https://d.dev/{version}/", "crawl",
                   [("A", f"https://d.dev/{version}/a", "body")], complete=True)
    return store


def test_forget_removes_one_version(cli_store):
    assert df.main(["--forget", "pydantic@1.10", "--yes"]) == 0
    assert [v["version"] for v in cli_store.versions("pydantic")] == ["2.11"]


def test_forget_removes_every_version(cli_store):
    from docsforge.store.kb_store import StoreError

    assert df.main(["--forget", "pydantic", "--yes"]) == 0
    with pytest.raises(StoreError):
        cli_store.versions("pydantic")


def test_forget_does_nothing_without_confirmation(cli_store):
    """A closed stdin is not consent. The prompt cannot be answered here, so
    the only safe reading of that is "no"."""
    assert df.main(["--forget", "pydantic"]) == 1
    assert len(cli_store.versions("pydantic")) == 2


def test_forget_refuses_a_name_it_does_not_have(cli_store):
    assert df.main(["--forget", "nosuchthing", "--yes"]) == 1


def test_forget_refuses_a_version_it_does_not_have(cli_store):
    assert df.main(["--forget", "pydantic@9.9", "--yes"]) == 1
    assert len(cli_store.versions("pydantic")) == 2


def test_a_url_is_still_required_for_an_ordinary_run():
    with pytest.raises(SystemExit) as exit_info:
        df.main([])
    assert exit_info.value.code == 2


# ── completeness is measured, not assumed ────────────────
def test_storing_an_index_reports_itself_incomplete():
    stats = {}
    det = df.Detection("llms_txt", "https://x.dev/llms.txt", INDEX)
    docs = [df.Doc("https://x.dev/llms.txt", "llms.txt", INDEX)]
    df._note_coverage(stats, det, docs)
    assert stats["whole"] is False
    assert "names a fuller file" in stats["reason"]


def test_storing_a_full_dump_reports_itself_whole():
    stats = {}
    det = df.Detection("llms_txt", "https://x.dev/llms-full.txt", DUMP)
    docs = [df.Doc("https://x.dev/llms-full.txt", "llms.txt", DUMP)]
    df._note_coverage(stats, det, docs)
    assert stats["whole"] is True


def test_looks_like_openapi():
    assert df._looks_like_openapi('{"openapi": "3.1.0"}')
    assert df._looks_like_openapi("openapi: 3.0.0\ninfo:\n")
    assert df._looks_like_openapi('{"swagger": "2.0"}')
    assert not df._looks_like_openapi('{"name": "not a spec"}')
    # A doc merely *mentioning* openapi mid-line is not a spec.
    assert not df._looks_like_openapi("# Guide\nWe support openapi specs.\n")


# ── openapi rendering ────────────────────────────────────
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Pet API", "version": "2.1"},
    "servers": [{"url": "https://api.pets.dev"}],
    "components": {
        "parameters": {
            "PetId": {"name": "petId", "in": "path", "required": True,
                      "schema": {"type": "string"}, "description": "The pet"}
        },
        "schemas": {"Pet": {"type": "object"}},
    },
    "paths": {
        "/pets/{petId}": {
            "parameters": [{"$ref": "#/components/parameters/PetId"}],
            "get": {
                "summary": "Get a pet",
                "responses": {"200": {"description": "ok"}},
            },
            "put": {
                "summary": "Replace a pet",
                "parameters": [{"name": "dry", "in": "query",
                                "schema": {"type": "boolean"},
                                "description": "Pipe | inside"}],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}},
                },
                "responses": {"204": {"description": "done"}},
            },
        }
    },
}


def _render_spec(spec):
    det = df.Detection("openapi", "https://api.pets.dev/spec.json", json.dumps(spec))
    return df.handle_openapi(det, FakeFetcher(), df.Options(verbose=False))[0].markdown


def test_openapi_includes_path_level_parameters_on_every_operation():
    md = _render_spec(SPEC)
    # petId is declared once at path level but must show up under both verbs.
    assert md.count("`petId`") == 2


def test_openapi_resolves_refs():
    md = _render_spec(SPEC)
    assert "The pet" in md          # description came through the $ref
    assert "`Pet`" in md            # request body schema named from its $ref


def test_openapi_escapes_pipes_in_table_cells():
    md = _render_spec(SPEC)
    assert "Pipe \\| inside" in md


def test_openapi_renders_servers_and_title():
    md = _render_spec(SPEC)
    assert "# Pet API" in md
    assert "https://api.pets.dev" in md
    assert "`GET /pets/{petId}`" in md


def test_openapi_survives_junk_in_paths():
    spec = {"openapi": "3.0.0", "info": {"title": "X"},
            "paths": {"/a": None, "/b": {"get": "nope"}, "/c": {"x-vendor": {}}}}
    assert "# X" in _render_spec(spec)


def test_openapi_rejects_unparseable_body():
    det = df.Detection("openapi", "u", "this is not json or yaml: [unclosed")
    with pytest.raises(df.ForgeError):
        df.handle_openapi(det, FakeFetcher(), df.Options(verbose=False))


# ── html extraction ──────────────────────────────────────
HTML = """
<html><head><title>  Install Guide  </title></head>
<body>
  <nav><a href="/other">Other page</a></nav>
  <main>
    <h1>Install</h1>
    <p>%s</p>
    <a href="/deep">deep link</a>
  </main>
  <footer>copyright junk</footer>
</body></html>
""" % ("Real content. " * 40)


def test_html_extraction_keeps_main_drops_chrome():
    title, md = df._html_to_md(HTML, "https://x.com/install")
    assert title == "Install Guide"
    assert "Real content." in md
    assert "copyright junk" not in md
    assert "Other page" not in md
    assert "source: https://x.com/install" in md


def test_html_title_is_literal_text():
    # <title> is escapable raw text in HTML5 — markup inside it is not markup.
    # get_text() reproduces what a browser shows; .string would return None
    # whenever the tag ends up with more than one child node.
    html = "<html><head><title>A <b>B</b></title></head><body><p>hi</p></body></html>"
    title, _ = df._html_to_md(html, "https://x.com")
    assert title == "A <b>B</b>"


def test_html_missing_title_falls_back():
    title, _ = df._html_to_md("<html><body><p>hi</p></body></html>", "https://x.com")
    assert title == "Untitled"


# ── crawl filtering ──────────────────────────────────────
@pytest.mark.parametrize("link,ok", [
    ("https://x.com/docs/a", True),
    ("https://x.com/logo.png", False),
    ("https://x.com/app.js", False),
    ("https://x.com/manual.pdf", False),
    ("https://other.com/docs", False),
    ("mailto:a@b.com", False),
    ("javascript:alert(1)", False),
])
def test_crawlable(link, ok):
    assert df._crawlable(link, "x.com") is ok


# ── ssrf guard ───────────────────────────────────────────
def test_guard_blocks_loopback_by_default():
    f = df.Fetcher(df.Options(verbose=False, allow_private=False))
    try:
        with pytest.raises(df.ForgeError, match="private/loopback"):
            f.guard("http://127.0.0.1:8000/docs")
    finally:
        f.close()


def test_guard_allows_loopback_when_opted_in():
    f = df.Fetcher(df.Options(verbose=False, allow_private=True))
    try:
        f.guard("http://127.0.0.1:8000/docs")
    finally:
        f.close()


def test_guard_lets_the_request_report_a_name_the_resolver_cannot_answer(monkeypatch):
    """Live on Vercel, 2026-09-15: `detect_source_type` on a real host answered
    normally, and on `https://nope-91827.example.com/` answered
    `OSError: [Errno 16] Device or resource busy (at engine.py:396 in
    _resolves_private)`. That runtime reports a name it cannot resolve as
    `EAI_SYSTEM` (a plain OSError) rather than `EAI_NONAME` (a gaierror), and
    the guard caught only the latter — so `find_docs`, which probes guessed
    domains that mostly do not exist, died on its first miss before any fetch.
    A lookup the guard cannot complete is not a private address: the request
    that follows repeats the lookup and reports the real error."""
    def busy(host, *args, **kwargs):
        raise OSError(16, "Device or resource busy")
    monkeypatch.setattr(df.socket, "getaddrinfo", busy)

    f = df.Fetcher(df.Options(verbose=False, allow_private=False))
    try:
        f.guard("https://nope-91827.example.com/")       # must not raise
        assert df._private_address_of("nope-91827.example.com") == ""
    finally:
        f.close()


def test_guard_still_refuses_a_private_answer_when_lookups_are_flaky(monkeypatch):
    """Tolerating a failed lookup must not tolerate a private one: the same
    guard, given an answer, judges it exactly as before."""
    def private(host, *args, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.7", 0))]
    monkeypatch.setattr(df.socket, "getaddrinfo", private)

    f = df.Fetcher(df.Options(verbose=False, allow_private=False))
    try:
        with pytest.raises(df.ForgeError, match="resolves to 10.0.0.7"):
            f.guard("https://intranet.example/")
    finally:
        f.close()


# ── the guard sees every hop ─────────────────────────────
#
# Evaluation.md §2.1, reproduced 2026-09-17 with two local servers: `guard`
# saw only the URL a caller passed in, `requests` followed the `302` on its
# own, and a loopback server's body came back as documentation. The guard
# that refused 127.0.0.1 directly never saw it arrive by redirect.

class _Hop:
    """A response `requests` could have returned, with no socket behind it."""

    def __init__(self, status, url, location=None, body=""):
        self.status_code = status
        self.url = url
        self.headers = {"location": location} if location else {}
        self.text = body
        self.history = []
        self.closed = False

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in (301, 302, 303, 307, 308)

    def close(self):
        self.closed = True


class _Session:
    """Answers each URL from a script, refusing to follow anything itself."""

    def __init__(self, script):
        self.script = script
        self.asked = []
        self.headers = {}

    def get(self, url, **kw):
        assert kw.get("allow_redirects") is False, "the session must never hop on its own"
        self.asked.append(url)
        return self.script[url]

    def close(self):
        pass


def _hopping(script, monkeypatch, private=("127.0.0.1", "10.0.0.7")):
    monkeypatch.setattr(df, "_resolves_private", lambda host: host in private)
    f = df.Fetcher(df.Options(verbose=False, allow_private=False))
    f.session = _Session(script)
    return f


def test_a_redirect_to_a_private_address_is_refused_before_it_is_fetched(monkeypatch):
    f = _hopping({
        "https://public.example/docs": _Hop(302, "https://public.example/docs",
                                            location="http://127.0.0.1/latest/meta-data/"),
    }, monkeypatch)
    with pytest.raises(df.ForgeError, match="private/loopback"):
        f.get("https://public.example/docs", allow_redirects=True)
    assert f.session.asked == ["https://public.example/docs"],         "the private address must never have been requested"


def test_a_redirect_between_public_hosts_lands_where_requests_would_have(monkeypatch):
    """The resolver reads `r.url` to learn that terraform.io lives on
    developer.hashicorp.com. It must go on learning it."""
    f = _hopping({
        "https://x.io": _Hop(301, "https://x.io", location="https://x.dev/"),
        "https://x.dev/": _Hop(200, "https://x.dev/", body="home"),
    }, monkeypatch)
    r = f.get("https://x.io", allow_redirects=True)
    assert r.status_code == 200 and r.url == "https://x.dev/"
    assert [h.status_code for h in r.history] == [301]
    assert r.history[0].closed, "a hop's body is not kept open"


def test_a_relative_location_is_resolved_against_the_hop_that_sent_it(monkeypatch):
    f = _hopping({
        "https://x.dev/docs": _Hop(302, "https://x.dev/docs", location="/docs/"),
        "https://x.dev/docs/": _Hop(200, "https://x.dev/docs/", body="docs"),
    }, monkeypatch)
    assert f.get("https://x.dev/docs").url == "https://x.dev/docs/"


def test_a_caller_that_declined_redirects_gets_the_redirect(monkeypatch):
    f = _hopping({
        "https://x.io": _Hop(302, "https://x.io", location="http://127.0.0.1/"),
    }, monkeypatch)
    r = f.get("https://x.io", allow_redirects=False)
    assert r.status_code == 302, "not followed, so nothing to refuse"
    assert f.session.asked == ["https://x.io"]


def test_a_private_address_reached_on_the_third_hop_is_still_refused(monkeypatch):
    """Every hop, not just the first."""
    f = _hopping({
        "https://a.example": _Hop(302, "https://a.example", location="https://b.example"),
        "https://b.example": _Hop(302, "https://b.example", location="https://c.example"),
        "https://c.example": _Hop(302, "https://c.example", location="http://10.0.0.7/admin"),
    }, monkeypatch)
    with pytest.raises(df.ForgeError, match="private/loopback"):
        f.get("https://a.example")
    assert "http://10.0.0.7/admin" not in f.session.asked


def test_a_redirect_loop_is_cut_off(monkeypatch):
    f = _hopping({
        "https://x.dev/a": _Hop(302, "https://x.dev/a", location="/b"),
        "https://x.dev/b": _Hop(302, "https://x.dev/b", location="/a"),
    }, monkeypatch)
    with pytest.raises(df.ForgeError, match="Too many redirects"):
        f.get("https://x.dev/a")
    assert len(f.session.asked) == df.MAX_REDIRECTS + 1


def test_a_redirect_to_a_non_http_scheme_is_refused(monkeypatch):
    f = _hopping({
        "https://x.dev/": _Hop(302, "https://x.dev/", location="file:///etc/passwd"),
    }, monkeypatch)
    with pytest.raises(df.ForgeError, match="Only http/https"):
        f.get("https://x.dev/")


# ── and the browser ──────────────────────────────────────

class _Route:
    def __init__(self, url):
        self.request = type("R", (), {"url": url})()
        self.decision = None

    def abort(self, reason=""):
        self.decision = "abort"

    def continue_(self):
        self.decision = "continue"


class _Page:
    """A page whose navigation walks a redirect chain through the route."""

    def __init__(self, chain):
        self.chain = chain
        self.gate = None
        self.decisions = []

    def route(self, pattern, handler):
        self.gate = handler

    def goto(self, url, **kw):
        for hop in self.chain:
            route = _Route(hop)
            self.gate(route, route.request)
            self.decisions.append((hop, route.decision))
            if route.decision == "abort":
                raise RuntimeError("net::ERR_BLOCKED_BY_CLIENT")

    def content(self):
        return "<html>rendered</html>"

    def close(self):
        pass


def test_the_browser_is_refused_a_redirect_to_a_private_address(monkeypatch):
    """`page.goto` follows redirects on its own, HTTP and JavaScript alike;
    request interception is where the guard gets a say."""
    monkeypatch.setattr(df, "_resolves_private", lambda host: host == "127.0.0.1")
    page = _Page(["https://public.example/", "http://127.0.0.1/secret"])
    f = df.Fetcher(df.Options(verbose=False, allow_private=False, js=True))
    monkeypatch.setattr(f, "_page", lambda: page)
    with pytest.raises(df.ForgeError, match="private/loopback"):
        f._render("https://public.example/")
    assert page.decisions == [("https://public.example/", "continue"),
                              ("http://127.0.0.1/secret", "abort")]


def test_the_browser_renders_a_public_page_as_before(monkeypatch):
    monkeypatch.setattr(df, "_resolves_private", lambda host: False)
    page = _Page(["https://public.example/", "https://public.example/app.js"])
    f = df.Fetcher(df.Options(verbose=False, allow_private=False, js=True))
    monkeypatch.setattr(f, "_page", lambda: page)
    assert f.render("https://public.example/") == "<html>rendered</html>"
    # `render_at` also says where the browser ended up; a fake page with no
    # `url` is taken to have stayed put.
    assert f.render_at("https://public.example/") == ("<html>rendered</html>",
                                                      "https://public.example/")
    assert all(d == "continue" for _, d in page.decisions)


def test_every_inet_aton_spelling_of_loopback_is_read_as_loopback():
    # What libc's inet_aton accepts, and so what a Linux resolver hands back
    # for these "hostnames": all four are 127.0.0.1.
    for spelling in ("2130706433", "0x7f000001", "0177.0.0.1", "127.1", "0177.0000.0.01"):
        assert str(df._inet_aton(spelling)) == "127.0.0.1", spelling
    assert str(df._inet_aton("8.8.8.8")) == "8.8.8.8"
    assert str(df._inet_aton("0xa9.0xfe.169.254")) == "169.254.169.254"
    # Not addresses: names, a part out of range, Python-only digit syntax.
    for not_one in ("example.com", "256.1.1.1", "1.2.3.4.5", "1_0.0.0.1", "0x", "", "::1"):
        assert df._inet_aton(not_one) is None, not_one


def test_alternate_loopback_spellings_are_refused_without_a_resolver(monkeypatch):
    # Measured 2026-09-20 by the offline benchmark on Windows, whose resolver
    # rejects every one of these: `http://2130706433/` was not refused, it
    # "failed to resolve" -- sixteen seconds later, after a DNS lookup for a
    # host called 2130706433. The verdict must not depend on the platform's
    # resolver, so the resolver is taken away here and the guard still
    # answers 127.0.0.1.
    def no_resolver(*a, **k):
        raise OSError(11001, "getaddrinfo failed")
    monkeypatch.setattr(df.socket, "getaddrinfo", no_resolver)
    f = df.Fetcher(df.Options(verbose=False, allow_private=False))
    try:
        for spelling in ("2130706433", "0x7f000001", "0177.0.0.1", "127.1"):
            with pytest.raises(df.ForgeError, match="private/loopback.*127.0.0.1"):
                f.guard(f"http://{spelling}/")
        # A public literal is still let through, resolver or no resolver.
        f.guard("http://8.8.8.8/")
    finally:
        f.close()


def test_guard_rejects_non_http_schemes():
    f = df.Fetcher(df.Options(verbose=False, allow_private=True))
    try:
        with pytest.raises(df.ForgeError):
            f.guard("file:///etc/passwd")
    finally:
        f.close()


# ── writing ──────────────────────────────────────────────
def test_write_docs_per_file(tmp_path):
    docs = [df.Doc("https://a.com/x", "X", "# X"), df.Doc("https://b.com/x", "X", "# X2")]
    paths = df.write_docs(docs, str(tmp_path))
    assert len(paths) == 2
    assert len(set(paths)) == 2  # same path, different hosts → no collision
    assert all(os.path.exists(p) for p in paths)


def test_write_docs_single_file(tmp_path):
    docs = [df.Doc("https://a.com/x", "X", "# X"), df.Doc("https://a.com/y", "Y", "# Y")]
    paths = df.write_docs(docs, str(tmp_path), single_file=True, source_url="https://a.com")
    assert len(paths) == 1
    body = open(paths[0], encoding="utf-8").read()
    assert "# X" in body and "# Y" in body and "---" in body


def test_forge_rejects_unknown_strategy():
    with pytest.raises(df.ForgeError, match="Unknown strategy"):
        df.forge("https://x.com", df.Options(force="nonsense", verbose=False))


# ── tool layer ───────────────────────────────────────────
def test_truncate_marks_the_cut():
    out = forge_tools._truncate("line\n" * 5000, limit=200)
    assert len(out) < 400
    assert "truncated" in out


def test_truncate_leaves_short_text_alone():
    assert forge_tools._truncate("short", limit=200) == "short"


def test_unknown_tool_reports_instead_of_raising():
    assert "unknown tool" in forge_tools.run_tool("nope", {})


def test_run_tool_drops_unexpected_arguments():
    # `bogus` is not in the schema; it must be filtered rather than TypeError.
    out = forge_tools.run_tool("fetch_docs", {"url": "http://127.0.0.1:1/x", "bogus": 1})
    assert out.startswith("Error:")
    assert "bogus" not in out


def test_tool_schemas_are_wellformed():
    for tool in forge_tools.TOOLS:
        assert tool.name and tool.description
        assert tool.schema["type"] == "object"
        for req in tool.schema.get("required", []):
            assert req in tool.schema["properties"]


def test_openai_tool_format():
    tools = forge_tools.openai_tools()
    assert {t["function"]["name"] for t in tools} == set(forge_tools.BY_NAME)
    assert all(t["type"] == "function" for t in tools)


def test_save_docs_refuses_to_escape_output_root():
    with pytest.raises(df.ForgeError, match="Refusing to write outside"):
        forge_tools.tool_save_docs("https://x.com", out_dir="../../../../etc")


def test_run_tool_turns_the_path_guard_into_text_for_the_model():
    out = forge_tools.run_tool("save_docs", {"url": "https://x.com", "out_dir": "../../etc"})
    assert out.startswith("Error:")
    assert "Refusing to write outside" in out


# ── cli ──────────────────────────────────────────────────
def test_help_does_not_crash_on_a_legacy_console(monkeypatch, capsys):
    """--help prints the module docstring, which contains arrows. The console
    was only switched to UTF-8 *after* parse_args, so `docsforge.py --help`
    died with a UnicodeEncodeError on a cp1252 terminal."""
    calls = []
    monkeypatch.setattr(df, "enable_utf8_console", lambda *a, **k: calls.append(True))

    with pytest.raises(SystemExit) as exit_info:
        df.main(["--help"])

    assert exit_info.value.code == 0
    assert calls, "the console must be reconfigured before argparse prints help"
    assert "0 means no limit" in capsys.readouterr().out


# ── a dump states where its pages came from, so it can be narrowed ──────────
#
# `docs.langchain.com/llms-full.txt` carries 1,175 `Source:` lines, one per
# page. The old reading -- "a dump lists no pages, so it makes no checkable
# claim about what it covers" -- is true of its links and false of its text,
# and taking it as published returned 3,372 pages of LangChain, LangSmith and
# Fleet as LangGraph's documentation.

DUMP_WITH_SOURCES = (
    "# Docs by Example\n\n> Everything we publish.\n\n"
    "# Getting started\nSource: https://d.dev/guide/start\n\n"
    + ("Guide prose. " * 40) + "\n\n"
    "# Widgets\nSource: https://d.dev/widgets/overview\n\n"
    + ("Widget prose. " * 40) + "\n\n"
    "# Widgets in depth\nSource: https://d.dev/widgets/deep\n\n"
    + ("More widget prose. " * 40) + "\n"
)


def test_a_dump_is_narrowed_to_the_section_that_was_asked_for():
    narrowed = df._dump_under(DUMP_WITH_SOURCES, "/widgets/")
    assert narrowed, "the dump covers this section"
    assert "Widget prose." in narrowed
    assert "More widget prose." in narrowed
    assert "Guide prose." not in narrowed, "a scoped request must not broaden"
    assert "Everything we publish." in narrowed, (
        "narrowing to a section keeps the site's own overview -- the rule "
        "`drop_root` exists to express")


def test_a_dump_covering_none_of_the_section_is_refused():
    """Measured: the root `llms-full.txt` at docs.langchain.com states sources
    under `/build-overview` and `/langsmith/`, and publishes the Python corpus
    separately at `/oss/python/llms-full.txt`. It covers no page under
    `/oss/python/langgraph/`, so it is not LangGraph's documentation."""
    assert df._dump_under(DUMP_WITH_SOURCES, "/nothing-here/") == ""


def test_a_dump_that_states_no_sources_is_left_alone():
    """Unchanged where the claim genuinely cannot be checked: refusing on a
    suspicion nothing supports would trade a whole published corpus for a
    crawl."""
    plain = "# Docs\n\n" + ("Prose with no source lines. " * 100)
    assert df._dump_under(plain, "/docs/") is None


def test_every_word_of_a_narrowed_section_survives():
    narrowed = df._dump_under(DUMP_WITH_SOURCES, "/widgets/")
    for chunk in ("Widget prose.", "More widget prose."):
        assert narrowed.count(chunk.split()[0]) >= 40


# ── the level a dump is split on is chosen by page size, not heading count ──

def test_the_split_level_is_chosen_by_page_size_not_heading_count():
    """Measured on `docs.langchain.com/llms-full.txt`, 6,749,200 characters:

           level   headings   median span
           #           2,528          179
           ##          3,371        1,066
           ###         2,081          961

    Most-parts counts headings and a heading count says nothing about what is
    under it -- `#` here is a per-page title with 179 characters beneath it."""
    body = ("# T1\ntiny\n# T2\ntiny\n# T3\ntiny\n# T4\ntiny\n"
            + "".join(f"## Section {i}\n\n{'text ' * 400}\n\n" for i in range(6)))
    parts = df._split_dump(body, above=0)
    titles = [t for t, _ in parts]

    # `#` has four headings with one word under each; `##` has six with 400.
    assert "Section 0" in titles and "Section 5" in titles
    assert "T2" not in titles and "T3" not in titles, (
        "the four near-empty `#` titles are preamble, not four pages")
    assert len(parts) == 7, "six sections plus the preamble above the first"


# ── a client-side redirect is a signpost, not a failed page ─────────────────

STUB = ('<!DOCTYPE html><meta charset="utf-8"><title>Redirecting&hellip;</title>'
        '<script>location.replace("../2.14/torch.html" + location.hash);</script>'
        '<meta http-equiv="refresh" content="0; url=../2.14/torch.html">'
        '<link rel="canonical" href="../2.14/torch.html">'
        '<a href="../2.14/torch.html">Continue to ../2.14/torch.html</a>')

REAL = ('<html><head><title>torch — PyTorch 2.14 documentation</title></head>'
        '<body><main><h1>torch</h1><p>' + ('prose ' * 300) +
        '</p></main></body></html>')


class _Redirecting:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def html(self, url, **kw):
        self.asked.append(url)
        if url not in self.pages:
            raise df.ForgeError(f"HTTP 404 for {url}")
        return self.pages[url]


def test_a_versioned_alias_stub_is_followed_to_the_real_page():
    """Measured 2026-09-11: every URL under `docs.pytorch.org/docs/stable/`
    serves a 1,400-byte stub redirecting to `/docs/2.14/`, because `stable` is
    an alias for the current release. HTTP never redirects, so the fetcher
    lands on the stub and extraction refuses it — several thousand pages of
    PyTorch's documentation, every one "not extractable"."""
    fetcher = _Redirecting({
        "https://d.dev/docs/stable/torch.html": STUB,
        "https://d.dev/docs/2.14/torch.html": REAL,
    })
    title, body = df._extract_page("https://d.dev/docs/stable/torch.html",
                                   fetcher, df.Options())
    assert "torch" in title
    assert "prose" in body
    assert "https://d.dev/docs/2.14/torch.html" in fetcher.asked


def test_a_real_page_is_never_mistaken_for_a_signpost():
    """A page with prose in it is a page, even if it carries a canonical link."""
    page = ('<html><head><link rel="canonical" href="/elsewhere"></head><body>'
            '<main><h1>Real</h1><p>' + ('prose ' * 300) + '</p></main></body></html>')
    fetcher = _Redirecting({"https://d.dev/a": page})
    title, body = df._extract_page("https://d.dev/a", fetcher, df.Options())
    assert "prose" in body
    assert fetcher.asked == ["https://d.dev/a"], "no second request"


def test_a_redirect_loop_gives_up_rather_than_spinning():
    fetcher = _Redirecting({
        "https://d.dev/a": '<meta http-equiv="refresh" content="0; url=/b">',
        "https://d.dev/b": '<meta http-equiv="refresh" content="0; url=/a">',
    })
    try:
        df._extract_page("https://d.dev/a", fetcher, df.Options())
    except df.ForgeError:
        pass
    assert len(fetcher.asked) <= df.REDIRECT_HOPS + 1


def test_the_stub_size_bound_keeps_a_long_page_out_of_it():
    big = '<meta http-equiv="refresh" content="0; url=/b">' + ("x" * 5000)
    assert df._redirect_target(big, "https://d.dev/a") == ""


# ── which build is answering, and how full the store is ───
#
# When the EBUSY fix appeared not to have landed, nothing in any tool result
# could distinguish "the deploy did not take" from "the fix missed the line".
# And a plan runs out of disk long before anyone thinks to look: the first
# symptom is a harvest failing at the very end, after all the crawling.

def test_the_listing_names_the_build_it_came_from(monkeypatch, tmp_path):
    from docsforge.store.kb_store import FileStore
    from docsforge.tools import forge_tools as ft
    monkeypatch.setattr(ft, "BUILD", "abc1234")
    monkeypatch.setattr(ft, "store", lambda: FileStore(tmp_path))
    assert "build `abc1234`" in ft.tool_list_knowledge_base()


def test_an_empty_store_still_names_the_build(monkeypatch, tmp_path):
    """The case that matters most: "nothing is stored" is exactly the answer
    you get from a deployment pointed at the wrong database."""
    from docsforge.store.kb_store import FileStore
    from docsforge.tools import forge_tools as ft
    monkeypatch.setattr(ft, "BUILD", "abc1234")
    monkeypatch.setattr(ft, "store", lambda: FileStore(tmp_path))
    out = ft.tool_list_knowledge_base()
    assert "nothing is stored yet" in out and "build `abc1234`" in out


def test_the_build_falls_back_to_local_off_a_platform(monkeypatch):
    import importlib
    from docsforge.tools import forge_tools as ft
    monkeypatch.delenv("VERCEL_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("DOCSFORGE_BUILD", raising=False)
    importlib.reload(ft)
    assert ft.BUILD == "local"
    importlib.reload(ft)


def test_a_roomy_store_reports_its_size_quietly(monkeypatch):
    from docsforge.tools import forge_tools as ft

    class Store:
        def footprint(self):
            return {"bytes": 200 * 1024 ** 2}          # 0.2 GB of 8
    note = ft._capacity_note(Store())
    assert "0.20 GB" in note
    assert "**" not in note, "a quiet line, not a warning"


def test_a_nearly_full_store_says_so_loudly(monkeypatch):
    from docsforge.tools import forge_tools as ft

    class Store:
        def footprint(self):
            return {"bytes": int(7.2 * 1024 ** 3)}     # 90% of 8
    note = ft._capacity_note(Store())
    assert "90% full" in note
    assert "fails at the end" in note, "say what running out actually costs"


def test_the_plan_size_can_be_stated(monkeypatch):
    from docsforge.tools import forge_tools as ft
    monkeypatch.setenv("DOCSFORGE_DB_LIMIT_GB", "1")

    class Store:
        def footprint(self):
            return {"bytes": int(0.9 * 1024 ** 3)}
    assert "90% full" in ft._capacity_note(Store())


def test_a_store_that_cannot_report_its_size_does_not_break_the_listing():
    """Best effort: a database that will not answer this still answers
    everything else."""
    from docsforge.tools import forge_tools as ft

    class Mute:
        def footprint(self):
            raise RuntimeError("no permission")

    class Empty:
        def footprint(self):
            return {}
    assert ft._capacity_note(Mute()) == ""
    assert ft._capacity_note(Empty()) == ""
