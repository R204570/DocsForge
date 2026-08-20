"""Offline unit tests — no network. Run: python -m pytest -q"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docsforge as df
import forge_tools


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
    from kb_store import build_store

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
    from kb_store import StoreError

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
