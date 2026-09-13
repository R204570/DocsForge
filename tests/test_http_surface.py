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
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    ("/tools", "Twelve tools, defined once."),
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
    # Self-contained: nothing fetched from elsewhere (the client picker on
    # /connect is the page's own inline script), and nothing from the local
    # web chat — that surface is not part of the hosted process.
    assert "<script src=" not in r.text
    assert "/api/" not in r.text and "app.js" not in r.text and "/static/" not in r.text


def test_site_pages_link_only_to_each_other_and_github(gated):
    import re
    for path in ("/", "/tools", "/connect"):
        hrefs = set(re.findall(r'href="([^"]+)"', gated.get(path).text))
        for h in hrefs:
            assert (h.startswith(("/", "https://github.com/R204570/DocsForge",
                                  "https://fonts.g"))), (path, h)
        # every internal page link resolves
        for h in hrefs:
            if h.startswith("/"):
                assert gated.get(h.split("#")[0]).status_code == 200, (path, h)


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
    assert r.headers["www-authenticate"] == "Bearer"


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
CLIENTS = ("Claude Code", "Codex", "Cursor", "Windsurf", "Antigravity",
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
    # the token is never in the page: only the reader's input fills it in
    assert 'id="token"' in html and 'type="password"' in html
