"""
OAuth for clients that cannot send a bearer token.

ChatGPT's custom connectors authenticate with OAuth or not at all; there is
no field for a static token. So the hosted server is also an OAuth 2.1
authorization server, and **the login is the DocsForge token**: the client is
sent to a page on this server, the person pastes `DOCSFORGE_MCP_TOKEN` once,
and the client walks away with an access token. Nobody gets further than
they could with the bearer header, and the bearer header keeps working.

Everything the flow needs to remember is carried in what it hands out
rather than stored: registered clients, authorization codes, access and
refresh tokens, and the pending login are all HMAC-signed blobs under a key
derived from the DocsForge token. That is what lets this run on a host where
no two requests share a process, and what keeps it off the database. The SDK
does the protocol — discovery metadata, dynamic client registration, the
authorize and token endpoints, PKCE, expiry, redirect checks; this module
supplies the provider those handlers call and the one page a person sees.

Trade-off, stated: a stateless authorization code cannot be marked used.
It lives ten minutes and is bound to the client's PKCE challenge, so a
replay needs the code_verifier that never left the client — the same
property OAuth 2.1 relies on for public clients — but it is not single-use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

LOGIN_PATH = "/oauth/login"

#: How long each signed thing is good for.
LOGIN_TTL = 15 * 60            # the pending authorization while a person types
CODE_TTL = 10 * 60             # an authorization code
ACCESS_TTL = 30 * 24 * 3600    # an access token
REFRESH_TTL = 180 * 24 * 3600  # a refresh token


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """Signed, expiring blobs: `<base64url json>.<base64url hmac>`.

    The key is derived from the DocsForge token, so rotating the token
    invalidates every client, code and access token at once — which is what
    rotating it should mean.
    """

    def __init__(self, token: str):
        self.key = hashlib.sha256(b"docsforge-oauth:" + token.encode()).digest()

    def sign(self, kind: str, ttl: int, **fields: Any) -> str:
        payload = {"k": kind, "exp": int(time.time()) + ttl, "n": secrets.token_hex(8), **fields}
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return body + "." + _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())

    def verify(self, blob: str, kind: str) -> dict[str, Any] | None:
        """The payload, or None for anything tampered, foreign, or expired."""
        try:
            body, mac = blob.split(".", 1)
            expected = hmac.new(self.key, body.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(_unb64(mac), expected):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        if payload.get("k") != kind or payload.get("exp", 0) < time.time():
            return None
        return payload


class Client(OAuthClientInformationFull):
    """A registered client that accepts whatever scopes it asks for.

    DocsForge has no scope model — the token is the whole permission — and
    a client rejected for naming a scope it was not registered with would
    be a refusal over nothing.
    """

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        return requested_scope.split(" ") if requested_scope else None


class StatelessProvider:
    """The SDK's authorization-server provider, remembering nothing.

    `token` is the DocsForge token: the login secret, and the root of the
    signing key. `public_url` is where this server is reached, which the
    login page redirects through.
    """

    def __init__(self, token: str, public_url: str):
        self.token = token
        self.public_url = public_url.rstrip("/")
        self.signer = Signer(token)

    # -- clients ------------------------------------------------------------
    async def get_client(self, client_id: str) -> Client | None:
        payload = self.signer.verify(client_id, "client")
        if payload is None:
            return None
        return Client(client_id=client_id, client_secret=self._secret_for(client_id),
                      **payload["c"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The handler generated a random id and secret and will return this
        # very object, so replacing both here is what the client receives.
        # The id carries the registration; the secret is derived from it, so
        # both are reproducible from the id alone.
        kept = client_info.model_dump(mode="json", exclude_none=True,
                                      exclude={"client_id", "client_secret",
                                               "client_id_issued_at", "client_secret_expires_at"})
        client_info.client_id = self.signer.sign("client", 10 * 365 * 24 * 3600, c=kept)
        if client_info.client_secret is not None:
            client_info.client_secret = self._secret_for(client_info.client_id)

    def _secret_for(self, client_id: str) -> str | None:
        payload = self.signer.verify(client_id, "client")
        if payload is None or payload["c"].get("token_endpoint_auth_method") == "none":
            return None
        return _b64(hmac.new(self.signer.key, b"secret:" + client_id.encode(), hashlib.sha256).digest())

    # -- authorization ------------------------------------------------------
    async def authorize(self, client: Client, params: AuthorizationParams) -> str:
        pending = self.signer.sign(
            "login", LOGIN_TTL,
            client_id=client.client_id, redirect_uri=str(params.redirect_uri),
            explicit=params.redirect_uri_provided_explicitly, state=params.state,
            scopes=params.scopes or [], challenge=params.code_challenge, resource=params.resource)
        return f"{self.public_url}{LOGIN_PATH}?{urlencode({'req': pending})}"

    def issue_code(self, pending: dict[str, Any]) -> str:
        """After a successful login: the code the client exchanges."""
        return self.signer.sign(
            "code", CODE_TTL,
            client_id=pending["client_id"], redirect_uri=pending["redirect_uri"],
            explicit=pending["explicit"], scopes=pending["scopes"],
            challenge=pending["challenge"], resource=pending.get("resource"))

    async def load_authorization_code(self, client: Client, authorization_code: str) -> AuthorizationCode | None:
        p = self.signer.verify(authorization_code, "code")
        if p is None or p["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=p["scopes"], expires_at=float(p["exp"]),
            client_id=p["client_id"], code_challenge=p["challenge"],
            redirect_uri=AnyUrl(p["redirect_uri"]), redirect_uri_provided_explicitly=p["explicit"],
            resource=p.get("resource"))

    async def exchange_authorization_code(self, client: Client, authorization_code: AuthorizationCode) -> OAuthToken:
        return self._tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    # -- tokens ---------------------------------------------------------------
    def _tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        return OAuthToken(
            access_token=self.signer.sign("access", ACCESS_TTL, client_id=client_id,
                                          scopes=scopes, resource=resource),
            token_type="Bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=self.signer.sign("refresh", REFRESH_TTL, client_id=client_id, scopes=scopes))

    async def load_refresh_token(self, client: Client, refresh_token: str) -> RefreshToken | None:
        p = self.signer.verify(refresh_token, "refresh")
        if p is None or p["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=p["client_id"], scopes=p["scopes"], expires_at=p["exp"])

    async def exchange_refresh_token(self, client: Client, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._tokens(client.client_id, scopes or refresh_token.scopes, None)

    def verify_access(self, token: str) -> AccessToken | None:
        """Synchronous, for the gate: an access token this server issued."""
        p = self.signer.verify(token, "access")
        if p is None:
            return None
        return AccessToken(token=token, client_id=p["client_id"], scopes=p["scopes"],
                           expires_at=p["exp"], resource=p.get("resource"))

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.verify_access(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Nothing is stored, so nothing can be struck out; tokens expire.
        # Rotating DOCSFORGE_MCP_TOKEN revokes everything at once.
        return None

    async def exchange_identity_assertion(self, client: Client, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "identity assertion is not supported")


# ─────────────────────────────────────────────────────────────
# The one page a person sees
# ─────────────────────────────────────────────────────────────
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DocsForge — sign in</title>
<style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b0d;color:#ececee;
font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif;padding:24px;box-sizing:border-box}}
.card{{width:100%;max-width:440px;background:#17171b;border:1px solid #2a2a31;border-radius:14px;padding:28px}}
.mark{{display:inline-block;width:12px;height:12px;border-radius:3px;background:#cf9fff;margin-right:10px;vertical-align:-1px}}
h1{{font-size:18px;margin:0 0 6px}}p{{margin:0 0 18px;color:#a9a9b3;font-size:14px}}
label{{display:block;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#a9a9b3;margin-bottom:6px}}
input{{width:100%;box-sizing:border-box;background:#0b0b0d;border:1px solid #2a2a31;border-radius:8px;padding:10px 12px;color:#ececee;
font:13px ui-monospace,SFMono-Regular,Menlo,monospace}}input:focus{{outline:none;border-color:#cf9fff}}
button{{margin-top:14px;width:100%;border:0;border-radius:10px;padding:11px;background:#cf9fff;color:#170b24;font-weight:600;font-size:14px;cursor:pointer}}
.err{{color:#ff9a9a;font-size:13px;margin:10px 0 0}}.who{{font:12px ui-monospace,Menlo,monospace;color:#7c7c88;margin-top:16px;word-break:break-all}}
</style></head><body><form class="card" method="post" action="{action}">
<h1><span class="mark"></span>DocsForge</h1>
<p>{client} is asking to use this DocsForge server. Paste the server's token to allow it.</p>
<label for="token">DOCSFORGE_MCP_TOKEN</label>
<input id="token" name="token" type="password" autocomplete="off" autofocus required>
<input type="hidden" name="req" value="{req}">
<button type="submit">Allow</button>{error}
<div class="who">redirects to {redirect}</div></form></body></html>"""


def login_routes(provider: StatelessProvider) -> list[Route]:
    """GET shows the form; POST checks the token and sends the code back."""

    def render(pending: dict[str, Any], req: str, error: str = "", status: int = 200) -> Response:
        name = "A client"
        client_payload = provider.signer.verify(pending["client_id"], "client")
        if client_payload and client_payload["c"].get("client_name"):
            name = client_payload["c"]["client_name"]
        return HTMLResponse(_PAGE.format(
            action=LOGIN_PATH, client=html.escape(name), req=html.escape(req),
            error=f'<p class="err">{html.escape(error)}</p>' if error else "",
            redirect=html.escape(pending["redirect_uri"])), status_code=status)

    async def get(request: Request) -> Response:
        req = request.query_params.get("req", "")
        pending = provider.signer.verify(req, "login")
        if pending is None:
            return HTMLResponse("<p>This sign-in link is invalid or has expired. Start again from your client.</p>",
                                status_code=400)
        return render(pending, req)

    async def post(request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        pending = provider.signer.verify(req, "login")
        if pending is None:
            return HTMLResponse("<p>This sign-in has expired. Start again from your client.</p>", status_code=400)
        presented = str(form.get("token", "")).strip()
        if not hmac.compare_digest(presented.encode(), provider.token.encode()):
            return render(pending, req, "That is not the server's token.", status=401)
        return RedirectResponse(
            construct_redirect_uri(pending["redirect_uri"], code=provider.issue_code(pending),
                                   state=pending.get("state")),
            status_code=302)

    return [Route(LOGIN_PATH, get, methods=["GET"]), Route(LOGIN_PATH, post, methods=["POST"])]


def routes(provider: StatelessProvider) -> list[Route]:
    """Everything OAuth: discovery, registration, authorize, token, login."""
    issuer = AnyHttpUrl(provider.public_url)
    resource = AnyHttpUrl(provider.public_url + "/mcp")
    out = create_auth_routes(provider, issuer_url=issuer,
                             client_registration_options=ClientRegistrationOptions(enabled=True))
    out += create_protected_resource_routes(resource_url=resource, authorization_servers=[issuer],
                                            resource_name="DocsForge")
    # The path-aware location is the current spec; the root one is what
    # older clients look for. Same document at both.
    out.append(Route("/.well-known/oauth-protected-resource", out[-1].endpoint, methods=["GET"]))
    out += login_routes(provider)
    return out


def resource_metadata_url(public_url: str) -> str:
    return public_url.rstrip("/") + "/.well-known/oauth-protected-resource/mcp"
