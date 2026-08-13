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
