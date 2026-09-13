"""
OAuth beside the bearer token: a client that cannot send a static token logs
in with it once and walks away with an access token.

The flow is driven the way ChatGPT drives it — discovery from the 401, dynamic
client registration, PKCE authorize, the login page, the code exchange — and
then the access token is used on /mcp. Statelessness is the property under
test throughout: nothing is stored, so a second server instance built from
the same token must honour everything the first one issued.
"""

import base64
import hashlib
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.server import mcp_server, oauth

TOKEN = "the-docsforge-token"
PUBLIC = "http://127.0.0.1:8765"
CALLBACK = "https://chatgpt.com/connector/oauth/Sy8t7kbIwS18"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def app(token=TOKEN):
    return mcp_server.build_http_app(token=token, public_url=PUBLIC)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    with TestClient(app(), base_url=PUBLIC) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(c, auth_method="none"):
    r = c.post("/register", json={"client_name": "ChatGPT", "redirect_uris": [CALLBACK],
                                  "token_endpoint_auth_method": auth_method,
                                  "grant_types": ["authorization_code", "refresh_token"],
                                  "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()


def login_and_get_code(c, client_id, challenge, state="xyz"):
    r = c.get("/authorize", params={"client_id": client_id, "redirect_uri": CALLBACK,
                                    "response_type": "code", "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": state},
              follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    login_url = r.headers["location"]
    assert login_url.startswith(PUBLIC + oauth.LOGIN_PATH + "?req=")
    page = c.get(login_url)
    assert page.status_code == 200 and "ChatGPT is asking" in page.text
    req = parse_qs(urlparse(login_url).query)["req"][0]
    r = c.post(oauth.LOGIN_PATH, data={"req": req, "token": TOKEN}, follow_redirects=False)
    assert r.status_code == 302, r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    q = parse_qs(back.query)
    assert q["state"] == [state]
    return q["code"][0]


def exchange(c, client_id, code, verifier):
    r = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                               "code_verifier": verifier, "client_id": client_id,
                               "redirect_uri": CALLBACK})
    assert r.status_code == 200, r.text
    return r.json()


# ── discovery ────────────────────────────────────────────
def test_the_401_points_at_discovery_and_discovery_points_at_the_endpoints(client):
    r = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == \
        f'Bearer, resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"'
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == PUBLIC + "/mcp" and prm["authorization_servers"] == [PUBLIC + "/"]
    assert client.get("/.well-known/oauth-protected-resource").json() == prm, "the root copy is the same document"
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["authorization_endpoint"] == PUBLIC + "/authorize"
    assert asm["token_endpoint"] == PUBLIC + "/token"
    assert asm["registration_endpoint"] == PUBLIC + "/register"
    assert "S256" in asm["code_challenge_methods_supported"]


# ── the whole flow ───────────────────────────────────────
def test_chatgpt_style_flow_ends_with_a_working_access_token(client):
    reg = register(client)
    assert reg.get("client_secret") is None, "a public client (auth method none) gets no secret"
    verifier, challenge = pkce()
    code = login_and_get_code(client, reg["client_id"], challenge)
    tokens = exchange(client, reg["client_id"], code, verifier)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]
    r = client.post("/mcp", json=INIT, headers={**MCP_HEADERS,
                                                "Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200 and '"serverInfo"' in r.text
    # and the static token still works exactly as before
    r = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_a_confidential_client_gets_a_reproducible_secret(client):
    reg = register(client, auth_method="client_secret_post")
    assert reg["client_secret"]
    verifier, challenge = pkce()
    code = login_and_get_code(client, reg["client_id"], challenge)
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "code_verifier": verifier, "client_id": reg["client_id"],
                                    "client_secret": reg["client_secret"], "redirect_uri": CALLBACK})
    assert r.status_code == 200, r.text
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "code_verifier": verifier, "client_id": reg["client_id"],
                                    "client_secret": "wrong", "redirect_uri": CALLBACK})
    assert r.status_code == 401


def test_refresh_issues_a_new_access_token(client):
    reg = register(client)
    verifier, challenge = pkce()
    tokens = exchange(client, reg["client_id"], login_and_get_code(client, reg["client_id"], challenge), verifier)
    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                    "client_id": reg["client_id"]})
    assert r.status_code == 200, r.text
    fresh = r.json()["access_token"]
    assert fresh != tokens["access_token"]
    assert client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {fresh}"}).status_code == 200


# ── what must be refused ─────────────────────────────────
def test_the_wrong_docsforge_token_does_not_log_in(client):
    reg = register(client)
    _, challenge = pkce()
    r = client.get("/authorize", params={"client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                         "response_type": "code", "code_challenge": challenge,
                                         "code_challenge_method": "S256"}, follow_redirects=False)
    req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
    r = client.post(oauth.LOGIN_PATH, data={"req": req, "token": TOKEN + "x"}, follow_redirects=False)
    assert r.status_code == 401 and "not the server" in r.text


def test_the_wrong_pkce_verifier_is_refused(client):
    reg = register(client)
    _, challenge = pkce()
    code = login_and_get_code(client, reg["client_id"], challenge)
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "code_verifier": "not-the-verifier", "client_id": reg["client_id"],
                                    "redirect_uri": CALLBACK})
    assert r.status_code == 400 and "code_verifier" in r.text


def test_an_unregistered_redirect_uri_is_refused(client):
    reg = register(client)
    _, challenge = pkce()
    r = client.get("/authorize", params={"client_id": reg["client_id"], "redirect_uri": "https://evil.example/cb",
                                         "response_type": "code", "code_challenge": challenge,
                                         "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 400


@pytest.mark.parametrize("bad", ["", "not.a.token", "x" * 80])
def test_a_forged_or_garbled_access_token_is_refused(client, bad):
    r = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_a_token_signed_under_another_docsforge_token_is_refused(client):
    other = oauth.StatelessProvider("some-other-token", PUBLIC)
    foreign = other._tokens("c", [], None).access_token
    assert client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {foreign}"}).status_code == 401


def test_an_expired_access_token_is_refused(monkeypatch):
    provider = oauth.StatelessProvider(TOKEN, PUBLIC)
    tok = provider._tokens("c", [], None).access_token
    assert provider.verify_access(tok) is not None
    monkeypatch.setattr(time, "time", lambda: time.time.__wrapped__() + oauth.ACCESS_TTL + 1
                        if hasattr(time.time, "__wrapped__") else 4102444800.0)
    assert provider.verify_access(tok) is None


def test_the_login_link_cannot_be_reused_after_it_expires(client, monkeypatch):
    reg = register(client)
    _, challenge = pkce()
    r = client.get("/authorize", params={"client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                         "response_type": "code", "code_challenge": challenge,
                                         "code_challenge_method": "S256"}, follow_redirects=False)
    req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
    monkeypatch.setattr(oauth.time, "time", lambda: 4102444800.0)      # 2100-01-01
    assert client.get(oauth.LOGIN_PATH, params={"req": req}).status_code == 400


# ── stateless: a second instance honours the first's tokens ──
def test_tokens_survive_a_new_process_built_from_the_same_token():
    with TestClient(app(), base_url=PUBLIC) as first:
        reg = register(first)
        verifier, challenge = pkce()
        tokens = exchange(first, reg["client_id"], login_and_get_code(first, reg["client_id"], challenge), verifier)
    with TestClient(app(), base_url=PUBLIC) as second:           # nothing carried over but the token
        r = second.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200
        # the client registration is honoured too — it lives in the client_id
        assert second.get("/authorize", params={"client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                                "response_type": "code", "code_challenge": challenge,
                                                "code_challenge_method": "S256"},
                          follow_redirects=False).status_code in (302, 307)


def test_rotating_the_docsforge_token_revokes_everything():
    with TestClient(app(), base_url=PUBLIC) as before:
        reg = register(before)
        verifier, challenge = pkce()
        tokens = exchange(before, reg["client_id"], login_and_get_code(before, reg["client_id"], challenge), verifier)
    with TestClient(app("rotated"), base_url=PUBLIC) as after:
        assert after.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}).status_code == 401
        assert after.get("/authorize", params={"client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                               "response_type": "code", "code_challenge": challenge,
                                               "code_challenge_method": "S256"},
                         follow_redirects=False).status_code == 400


# ── without a token there is no OAuth, and nothing to log in to ──
def test_without_a_token_no_oauth_routes_are_mounted():
    with TestClient(mcp_server.build_http_app(token=None), base_url=PUBLIC) as c:
        assert c.get("/.well-known/oauth-authorization-server").status_code == 404
        assert c.get(oauth.LOGIN_PATH).status_code == 404


def test_discovery_advertises_the_configured_public_url(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_PUBLIC_URL", "https://docs.example.com/")
    with TestClient(mcp_server.build_http_app(token=TOKEN), base_url="https://docs.example.com") as c:
        asm = c.get("/.well-known/oauth-authorization-server").json()
        assert asm["token_endpoint"] == "https://docs.example.com/token"
    monkeypatch.delenv("DOCSFORGE_PUBLIC_URL")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "temp-repo-murex.vercel.app")
    with TestClient(mcp_server.build_http_app(token=TOKEN), base_url="https://temp-repo-murex.vercel.app") as c:
        asm = c.get("/.well-known/oauth-authorization-server").json()
        assert asm["authorization_endpoint"] == "https://temp-repo-murex.vercel.app/authorize"
