"""
The hosted HTTP surface: a public landing page, a public health check, and a
`/mcp` that answers only to the bearer token.

Driven in-process through the same ASGI app `main() --http` serves, so what is
asserted here is what a deployment gets. The refusal to bind a public address
without a token is checked through `main()` itself, since that rule lives
there and nowhere else.
"""

import json
import os
import re
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge import SITE_URL
from docsforge.server import mcp_server

TOKEN = "s3cret-token"
LOOPBACK = "http://127.0.0.1:8765"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


@pytest.fixture
def gated():
    # Loopback host on purpose: the SDK's DNS-rebinding guard is on when the
    # bind address is loopback, and a Host of `testserver` is exactly what it
    # exists to refuse.
    with TestClient(mcp_server.build_http_app(token=TOKEN), base_url=LOOPBACK) as client:
        yield client


@pytest.fixture
def open_app():
    with TestClient(mcp_server.build_http_app(token=None), base_url=LOOPBACK) as client:
        yield client


# ── the public half ──────────────────────────────────────
@pytest.mark.parametrize("path,marker", [
    ("/", "Documentation for technologies"),
    ("/tools", "Thirteen tools, defined once."),
    ("/connect", "Connect your MCP client."),
])
def test_site_pages_are_public_and_self_contained(gated, path, marker):
    r = gated.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert marker in r.text
    # The version chip is the package's, not whatever the page was drawn with.
    assert f"v{mcp_server.__version__}" in r.text and "{{VERSION}}" not in r.text
    assert "{{BASE_URL}}" not in r.text and "YOUR-HOST" not in r.text
    # Self-contained: the Google tag is the one script fetched from elsewhere
    # (the client picker on /connect is the page's own inline script), and
    # nothing from the local web chat — that surface is not part of the hosted
    # process.
    assert re.findall(r'<script\b[^>]*\bsrc="([^"]*)"', r.text) == [
        "https://www.googletagmanager.com/gtag/js?id=G-KTBKMGZM75"]
    assert "/api/" not in r.text and "app.js" not in r.text and "/static/" not in r.text


DEVELOPER_LINKS = ("https://portfolio-raj-25.netlify.app/",
                   "https://www.linkedin.com/in/raj-patel5/",
                   "https://github.com/R204570")


def test_site_pages_link_only_to_each_other_github_and_the_developer(gated):
    import re
    for path in ("/", "/tools", "/connect"):
        hrefs = set(re.findall(r'href="([^"]+)"', gated.get(path).text))
        for h in hrefs:
            assert (h.startswith(("/", "https://github.com/R204570/DocsForge",
                                  "https://fonts.g", SITE_URL + "/"))
                    or h in DEVELOPER_LINKS), (path, h)
        # every internal page link resolves
        for h in hrefs:
            if h.startswith("/"):
                assert gated.get(h.split("#")[0]).status_code == 200, (path, h)


@pytest.mark.parametrize("path,kind,drawn", [
    ("/logo.svg", "image/svg+xml", "docsforge-logo.svg"),
    ("/favicon.ico", "image/x-icon", "docsforge-logo.ico"),
    ("/og.png", "image/png", "docsforge-og.png"),
    ("/apple-touch-icon.png", "image/png", "docsforge-icon-180.png"),
    ("/icon-192.png", "image/png", "docsforge-icon-192.png"),
    ("/icon-512.png", "image/png", "docsforge-icon-512.png"),
    ("/favicon-192.png", "image/png", "docsforge-favicon-192.png"),
    ("/mascot.webp", "image/webp", "docsforge-mascot.webp"),
])
def test_the_logo_and_favicon_are_public_and_are_the_ones_in_logo(gated, path, kind, drawn):
    r = gated.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
    # The packaged copy is the drawn one: scripts/build_site.py was re-run.
    logo_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Logo")
    with open(os.path.join(logo_dir, drawn), "rb") as f:
        assert r.content == f.read()
    for page in ("/", "/tools", "/connect"):
        html = gated.get(page).text
        assert '<link rel="icon" href="/favicon.ico"' in html and 'src="/logo.svg"' in html, page


# ── search engines ───────────────────────────────────────
PUBLIC = ("/", "/tools", "/connect")


@pytest.mark.parametrize("path", PUBLIC)
def test_every_page_names_its_canonical_url_and_carries_share_metadata(gated, path):
    text = gated.get(path).text
    canonical = SITE_URL + path
    assert re.findall(r'<link rel="canonical" href="([^"]+)"', text) == [canonical]
    assert re.findall(r'<meta property="og:url" content="([^"]+)"', text) == [canonical]
    assert f'<meta property="og:image" content="{SITE_URL}/og.png">' in text
    assert '<meta name="twitter:card" content="summary_large_image">' in text
    assert "noindex" not in text
    # One title and one description, and the share cards repeat them exactly.
    title = re.findall(r"<title>(.*?)</title>", text)
    description = re.findall(r'<meta name="description" content="([^"]*)">', text)
    assert len(title) == len(description) == 1
    assert f'<meta property="og:title" content="{title[0]}">' in text
    assert f'<meta property="og:description" content="{description[0]}">' in text


def test_titles_and_descriptions_are_not_shared_between_pages(gated):
    pages = [gated.get(p).text for p in PUBLIC]
    titles = {re.search(r"<title>(.*?)</title>", t).group(1) for t in pages}
    descriptions = {re.search(r'<meta name="description" content="([^"]*)">', t).group(1)
                    for t in pages}
    assert len(titles) == len(descriptions) == len(PUBLIC)


@pytest.mark.parametrize("path", PUBLIC)
def test_structured_data_parses_and_names_the_developer(gated, path):
    blocks = [json.loads(b) for b in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', gated.get(path).text, re.S)]
    graphs = [b for b in blocks if "@graph" in b]
    assert len(graphs) == 1
    graph = {node["@type"]: node for node in graphs[0]["@graph"]}
    assert {"WebSite", "Person", "SoftwareApplication", "WebPage"} <= set(graph)
    assert graph["Person"]["name"] == "Raj Patel"
    assert graph["Person"]["sameAs"] == list(DEVELOPER_LINKS)
    assert graph["SoftwareApplication"]["codeRepository"] == "https://github.com/R204570/DocsForge"
    assert graph["WebPage"]["url"] == SITE_URL + path


def test_the_home_page_shows_who_built_it(gated):
    text = gated.get("/").text
    section = text[text.index('id="developer"'):]
    section = section[:section.index("</section>")]
    assert "Built by Raj Patel" in section
    for href in DEVELOPER_LINKS:
        assert f'href="{href}"' in section, href
    # and every page's footer leads there
    for path in PUBLIC:
        assert 'href="/#developer"' in gated.get(path).text, path


def test_robots_allows_the_site_points_at_the_sitemap_and_keeps_mcp_out(gated):
    r = gated.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert "User-agent: *" in lines and "Allow: /" in lines and "Disallow: /" not in lines
    assert "Disallow: /mcp" in lines
    assert f"Sitemap: {SITE_URL}/sitemap.xml" in lines


def test_sitemap_lists_exactly_the_canonical_pages(gated):
    import xml.etree.ElementTree as ET
    r = gated.get("/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.fromstring(r.content).findall("s:url/s:loc", ns)]
    assert locs == [SITE_URL + p for p in PUBLIC]
    for loc in locs:  # each one is served, publicly, with no token
        assert gated.get(loc.removeprefix(SITE_URL)).status_code == 200, loc


def test_sitemap_dates_each_page_and_robots_and_sitemap_are_real_files(gated):
    import datetime
    import xml.etree.ElementTree as ET
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    served = gated.get("/sitemap.xml").content
    today = datetime.date.today().isoformat()
    for url in ET.fromstring(served).findall("s:url", ns):
        lastmod = url.find("s:lastmod", ns).text
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", lastmod) and lastmod <= today, lastmod
    # Served exactly as committed: what is in the repository is what crawlers get.
    assert served == (mcp_server.SITE / "sitemap.xml").read_bytes()
    assert gated.get("/robots.txt").content == (mcp_server.SITE / "robots.txt").read_bytes()


def test_the_home_page_faq_is_visible_and_its_markup_says_the_same(gated):
    text = gated.get("/").text
    section = text[text.index('id="faq"'):]
    section = section[:section.index("</section>")]
    shown = [(re.sub(r"<[^>]+>", "", q), re.sub(r"<[^>]+>", "", a)) for q, a in
             re.findall(r"<h3>(.*?)</h3>\s*<p>(.*?)</p>", section, re.S)]
    blocks = [json.loads(b) for b in
              re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)]
    graph = next(b["@graph"] for b in blocks if "@graph" in b)
    faq = next(n for n in graph if n["@type"] == "FAQPage")
    marked = [(q["name"], q["acceptedAnswer"]["text"]) for q in faq["mainEntity"]]
    import html as htmllib
    assert len(shown) >= 5
    assert [(htmllib.unescape(q), htmllib.unescape(a)) for q, a in shown] == marked


def test_llms_txt_describes_the_site_for_language_models(gated):
    r = gated.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert lines[0] == "# DocsForge" and any(l.startswith("> ") for l in lines)
    # every tool the tools page documents, and only those
    tools = re.findall(r'class="name">([a-z_]+)<', gated.get("/tools").text)
    listed = re.search(r"MCP tools: (.*)\.", r.text).group(1).split(", ")
    assert listed == tools and len(tools) == 13
    # every site link in it is a page that is served
    for url in re.findall(r"\((https://[^)]+)\)", r.text):
        if url.startswith(SITE_URL):
            assert gated.get(url.removeprefix(SITE_URL)).status_code == 200, url


def test_the_home_page_tells_google_the_site_is_called_docsforge(gated):
    """Without it Google falls back to the parent domain: "Vercel"."""
    text = gated.get("/").text
    blocks = [json.loads(b) for b in
              re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)]
    # On its own, in the form Google's site-name documentation shows ...
    alone = [b for b in blocks if b.get("@type") == "WebSite"]
    assert len(alone) == 1
    site = alone[0]
    assert site["@context"] == "https://schema.org"
    assert site["name"] == "DocsForge" and site["url"] == SITE_URL + "/"
    # ... the hostname, lowercase, as the last fallback instead of the parent's name
    assert site["alternateName"][-1] == SITE_URL.removeprefix("https://")
    # ... and the same entity as the WebSite in the graph, saying the same thing
    graph = next(b["@graph"] for b in blocks if "@graph" in b)
    in_graph = next(n for n in graph if n["@type"] == "WebSite")
    assert in_graph["@id"] == site["@id"]
    assert (in_graph["name"], in_graph["alternateName"]) == (site["name"], site["alternateName"])
    assert '<meta property="og:site_name" content="DocsForge">' in text
    assert '<meta name="application-name" content="DocsForge">' in text
    assert re.search(r"<title>DocsForge\b", text)
    # Only the home page carries the standalone block.
    for path in ("/tools", "/connect"):
        assert '"@type":"WebSite","@id"' not in gated.get(path).text.split('"@graph"')[0], path


def test_every_page_offers_google_a_large_square_favicon(gated):
    import struct
    for path in PUBLIC:
        assert ('<link rel="icon" type="image/png" sizes="192x192" href="/favicon-192.png">'
                in gated.get(path).text), path
    # A PNG's size is in its IHDR chunk, right after the 8-byte signature.
    png = gated.get("/favicon-192.png").content
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert width == height == 192 and width % 48 == 0
    # The .ico every browser asks for: a directory of 16-byte entries after a
    # 6-byte header, each starting with width and height (0 meaning 256).
    ico = gated.get("/favicon.ico").content
    count = struct.unpack("<H", ico[4:6])[0]
    sizes = {(ico[6 + 16 * i] or 256, ico[7 + 16 * i] or 256) for i in range(count)}
    assert {(48, 48), (256, 256)} <= sizes


def test_the_fox_is_on_the_home_page_and_the_404_and_sized_so_nothing_shifts(gated):
    home = gated.get("/").text
    foxes = re.findall(r'<img class="([a-z-]+)" src="/mascot.webp" alt="([^"]*)" width="564" '
                       r'height="480"', home)
    assert [c for c, _ in foxes] == ["perch", "close-fox"]
    assert foxes[0][1] == "The DocsForge fox"          # the first one is named
    lost = gated.get("/no-such-page", headers={"Accept": "text/html"}).text
    assert 'class="lost-fox" src="/mascot.webp"' in lost
    for path in ("/tools", "/connect"):
        assert "/mascot.webp" not in gated.get(path).text, path


def test_the_web_manifest_names_icons_that_are_served(gated):
    r = gated.get("/site.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    manifest = r.json()
    assert manifest["name"] == "DocsForge" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        got = gated.get(icon["src"])
        assert got.status_code == 200 and got.headers["content-type"] == icon["type"]
    for path in PUBLIC:
        text = gated.get(path).text
        assert '<link rel="manifest" href="/site.webmanifest">' in text
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in text


def test_an_unknown_page_is_a_404_a_person_can_use_and_no_engine_indexes(gated):
    r = gated.get("/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-robots-tag"] == "noindex"
    assert '<meta name="robots" content="noindex">' in r.text
    assert '<link rel="canonical"' not in r.text
    assert 'href="/"' in r.text and "{{" not in r.text
    # Not a browser, or the tool surface: the plain answer, unchanged.
    assert gated.get("/no-such-page").text == "Not Found"
    assert gated.get("/mcp/elsewhere", headers={"Accept": "text/html"}).text == "Not Found"
    # and /mcp itself still answers with its own gate, not a page
    assert gated.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401


def test_the_indexnow_key_file_is_served_only_for_the_configured_key(gated, monkeypatch):
    monkeypatch.delenv(mcp_server.INDEXNOW_VAR, raising=False)
    assert gated.get("/abcdef0123456789.txt").status_code == 404
    monkeypatch.setenv(mcp_server.INDEXNOW_VAR, "abcdef0123456789")
    r = gated.get("/abcdef0123456789.txt")
    assert r.status_code == 200 and r.text == "abcdef0123456789"
    assert gated.get("/0123456789abcdef.txt").status_code == 404
    # the site's other .txt files are not shadowed by it
    assert gated.get("/robots.txt").text.startswith("User-agent")
    assert gated.get("/llms.txt").text.startswith("# DocsForge")
    monkeypatch.setenv(mcp_server.INDEXNOW_VAR, "short")      # not a valid key
    assert gated.get("/short.txt").status_code == 404


def test_the_site_is_compressed_and_mcp_is_left_as_the_sdk_sends_it(gated):
    r = gated.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and "Documentation for" in r.text
    assert gated.get("/sitemap.xml", headers={"Accept-Encoding": "gzip"}).status_code == 200
    m = gated.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Accept-Encoding": "gzip",
                                               "Authorization": f"Bearer {TOKEN}"})
    assert m.status_code == 200 and "content-encoding" not in m.headers


def test_the_committed_site_is_what_build_site_builds():
    """Edit scripts/site_src/ and forget to rebuild, and this fails."""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "build_site", os.path.join(root, "scripts", "build_site.py"))
    build_site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_site)
    css = (build_site.SRC / "site.css").read_text(encoding="utf-8")
    js = (build_site.SRC / "site.js").read_text(encoding="utf-8")
    picker = (build_site.SRC / "picker.html").read_text(encoding="utf-8")
    for page in build_site.PAGES + build_site.UNLISTED:
        committed = (build_site.SITE / f"{page}.html").read_text(encoding="utf-8")
        assert committed == build_site.build(page, css, js, picker), page
    assert (build_site.SITE / "llms.txt").read_text(encoding="utf-8") == build_site.llms_txt()
    assert (build_site.SITE / "site.webmanifest").read_text(encoding="utf-8") == build_site.manifest()
    assert (build_site.SITE / "robots.txt").read_text(encoding="utf-8") == build_site.ROBOTS
    dates = build_site.recorded_lastmods()
    assert set(dates) == set(build_site.PATHS.values())
    assert (build_site.SITE / "sitemap.xml").read_text(encoding="utf-8") == build_site.sitemap(dates)


def test_verification_tags_come_from_the_environment_only(gated, monkeypatch):
    for var in mcp_server.VERIFICATION:
        monkeypatch.delenv(var, raising=False)
    text = gated.get("/").text
    assert "google-site-verification" not in text and "msvalidate.01" not in text
    assert "{{SITE_VERIFICATION}}" not in text
    monkeypatch.setenv("DOCSFORGE_GOOGLE_SITE_VERIFICATION", "abc123")
    monkeypatch.setenv("DOCSFORGE_BING_SITE_VERIFICATION", '"><script>x</script>')
    for path in PUBLIC:
        text = gated.get(path).text
        assert '<meta name="google-site-verification" content="abc123">' in text, path
        assert ('<meta name="msvalidate.01" content="&quot;&gt;&lt;script&gt;x&lt;/script&gt;">'
                in text), path


def test_health_is_public_and_names_no_host(gated):
    r = gated.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] == mcp_server.__version__
    assert set(body) == {"status", "version", "store", "degraded"}, \
        "health must not carry the store location — it names the database host"
    assert body["degraded"] is False


# ── the gate ─────────────────────────────────────────────
def test_mcp_without_a_token_is_refused(gated):
    r = gated.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    # `Bearer`, plus where OAuth discovery starts (tests/test_oauth.py has the rest)
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_mcp_with_the_wrong_token_is_refused(gated):
    r = gated.post("/mcp", json=INIT,
                   headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}x"})
    assert r.status_code == 401


def test_a_non_bearer_scheme_is_refused_even_with_the_right_secret(gated):
    r = gated.post("/mcp", json=INIT,
                   headers={**MCP_HEADERS, "Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_mcp_with_the_token_reaches_the_server(gated):
    r = gated.post("/mcp", json=INIT,
                   headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    # Streamable HTTP answers as JSON or as one SSE frame; either way the
    # initialize result is in there and it is ours.
    assert '"serverInfo"' in r.text and '"docsforge"' in r.text


def test_a_trailing_slash_does_not_bypass_the_gate(gated):
    r = gated.post("/mcp/", json=INIT, headers=MCP_HEADERS)
    assert r.status_code in (401, 307, 404)
    assert r.status_code != 200


def test_without_a_configured_token_mcp_is_open(open_app):
    # The local case: `python main.py --http` on loopback, no token, works.
    r = open_app.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text


# ── the bind rule ────────────────────────────────────────
def test_public_bind_without_a_token_is_refused(monkeypatch, capsys):
    monkeypatch.delenv(mcp_server.TOKEN_VAR, raising=False)
    monkeypatch.delenv(mcp_server.INSECURE_VAR, raising=False)
    monkeypatch.setattr(mcp_server.forge_tools, "store", lambda: type(
        "S", (), {"kind": "files", "location": "x", "degraded": ""})())
    code = mcp_server.main(["--http", "--host", "0.0.0.0"])
    assert code == 2
    err = capsys.readouterr().err
    assert mcp_server.TOKEN_VAR in err and "refusing to bind" in err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_recognised(host):
    assert mcp_server.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
def test_public_addresses_are_not_loopback(host):
    assert not mcp_server.is_loopback(host)


def test_port_defaults_to_the_platform_variable(monkeypatch):
    # Container platforms say which port they route to through PORT.
    monkeypatch.setenv("PORT", "9312")
    import argparse
    seen = {}

    def fake_run(app, host, port, **kw):
        seen.update(host=host, port=port)

    monkeypatch.setenv(mcp_server.TOKEN_VAR, TOKEN)
    monkeypatch.setattr(mcp_server.forge_tools, "store", lambda: type(
        "S", (), {"kind": "files", "location": "x", "degraded": ""})())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert mcp_server.main(["--http", "--host", "0.0.0.0"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9312}


# ── one command, two callers ──────────────────────────────
def _fake_store(monkeypatch):
    monkeypatch.setattr(mcp_server.forge_tools, "store", lambda: type(
        "S", (), {"kind": "files", "location": "x", "degraded": ""})())


def test_at_a_terminal_bare_main_serves_http(monkeypatch):
    # A person typed it: they get the site and /mcp in one process.
    _fake_store(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    served = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: served.update(host=host, port=port))
    monkeypatch.setattr(mcp_server.server, "run", lambda *a, **kw: pytest.fail("stdio must not run"))
    monkeypatch.delenv("PORT", raising=False)
    assert mcp_server.main([]) == 0
    assert served == {"host": "127.0.0.1", "port": 8765}


def test_over_pipes_bare_main_speaks_stdio(monkeypatch):
    # An MCP client launched it: JSON-RPC on stdin/stdout, no listening port.
    _fake_store(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    ran = {}
    monkeypatch.setattr(mcp_server.server, "run", lambda transport, **kw: ran.update(transport=transport))
    monkeypatch.setattr(mcp_server.harvest_jobs, "running", lambda: [])
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("HTTP must not start"))
    assert mcp_server.main([]) == 0
    assert ran == {"transport": "stdio"}


@pytest.mark.parametrize("flag,expect", [("--http", True), ("--stdio", False)])
def test_a_flag_overrides_the_guess(flag, expect):
    assert mcp_server.choose_http(flag == "--http", flag == "--stdio") is expect


def test_the_two_flags_exclude_each_other():
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--stdio"])


def test_health_reports_a_store_that_fell_back(gated, monkeypatch):
    # A configured database that could not be reached: the store is files,
    # and on a stateless host that means nothing persists. Health says so —
    # still a 200, so the platform keeps the instance, but visibly degraded.
    monkeypatch.setattr(mcp_server.forge_tools, "store", lambda: type(
        "S", (), {"kind": "files", "location": "x", "degraded": "cannot reach db"})())
    body = gated.get("/health").json()
    assert body["status"] == "degraded" and body["degraded"] is True and body["store"] == "files"
    assert "x" not in body.values() and "cannot reach" not in str(body)


# ── the page names the server it came from ───────────────
def test_connect_shows_the_url_the_reader_used(gated, monkeypatch):
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    assert "http://127.0.0.1:8765/mcp" in gated.get("/connect").text


def test_connect_behind_a_tls_proxy_shows_https_and_the_public_host(gated, monkeypatch):
    # Vercel terminates TLS: the app sees http, the reader used https.
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    r = gated.get("/connect", headers={"x-forwarded-proto": "https",
                                       "x-forwarded-host": "temp-repo-murex.vercel.app"})
    assert "https://temp-repo-murex.vercel.app/mcp" in r.text
    assert "127.0.0.1" not in r.text


def test_a_configured_public_url_wins(gated, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_PUBLIC_URL", "https://docs.example.com/")
    assert "https://docs.example.com/mcp" in gated.get("/connect").text


# ── the client picker on /connect ────────────────────────
CLIENTS = ("Claude Code", "Codex", "ChatGPT", "Cursor", "Windsurf", "Antigravity",
           "Gemini CLI", "VS Code", "Claude Desktop", "Any client (JSON)")


def test_connect_offers_every_client_with_the_servers_own_url(gated, monkeypatch):
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    html = gated.get("/connect", headers={"x-forwarded-proto": "https",
                                          "x-forwarded-host": "temp-repo-murex.vercel.app"}).text
    for name in CLIENTS:
        assert f">{name}<" in html, name
    # every snippet is built from the one filled-in URL; no placeholder leaks
    assert 'var MCP = "https://temp-repo-murex.vercel.app/mcp"' in html
    assert "YOUR-HOST" not in html and "{{BASE_URL}}" not in html
    # the shapes the clients actually differ on
    assert "--bearer-token-env-var DOCSFORGE_MCP_TOKEN" in html      # Codex: token via env
    assert '"serverUrl": "' in html                                   # Windsurf / Antigravity
    assert "mcp-remote" in html                                       # Claude Desktop bridge
    assert "code --add-mcp" in html and "gemini mcp add" in html
    # ChatGPT: its connectors take OAuth or nothing, never a bearer token, so
    # the tab walks through the OAuth connector and the DocsForge sign-in
    # page that the token is pasted into.
    assert "Authentication   OAuth" in html and "Dynamic Client Registration" in html
    assert "Sign-in page" in html and "Authentication   Token" not in html
    # the token is never in the page: only the reader's input fills it in
    assert 'id="token"' in html and 'type="password"' in html
