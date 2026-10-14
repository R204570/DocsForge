#!/usr/bin/env python3
"""
DocsForge MCP server.

The product surface. Any MCP client — Claude Code, Claude Desktop, Cursor, an
agent framework — gets the same tools the web chat uses, because both are
generated from the one list in forge_tools.py.

That generation is the point. This file used to restate every tool by hand: its
description, its parameter types, its bounds. Two copies of a tool surface
always drift, and this one had: four tools existed in forge_tools and simply
did not exist over MCP, and `max_pages` was declared `ge=1, le=200` here while
the harvester treats 0 as unlimited — so an MCP client could not ask for a full
harvest at all. Now there is nothing to keep in sync.

Run:
  python main.py                        # at a terminal: the site + /mcp on 127.0.0.1:8765
  python main.py                        # launched by an MCP client (pipes): stdio
  python main.py --http | --stdio       # force either
  DOCSFORGE_MCP_TOKEN=… python main.py --http --host 0.0.0.0   # hosted

Over HTTP the process serves the public site (`/`, `/tools`, `/connect`,
`/contributors`, its icons, `/og.png`, `/site.webmanifest`, `/llms.txt`, and `/robots.txt` and
`/sitemap.xml` for search engines), `/health`, and `/mcp`; only `/mcp` needs
the bearer token. Any other address a browser asks for gets the site's 404.

Register with Claude Code:
  claude mcp add docsforge -- python E:/DocsForge/main.py
"""

from __future__ import annotations

import argparse
import hmac
import html
import inspect
import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from pydantic import Field
from starlette.requests import Request
from starlette.exceptions import HTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from docsforge.tools import forge_tools
from docsforge.tools import harvest_jobs
from docsforge import __version__
from docsforge.core.engine import enable_utf8_console

server = MCPServer(
    name="docsforge",
    title="DocsForge",
    version=__version__,
    instructions=(
        "Gives a model documentation for technologies it was not trained on. "
        "The usual entry point is learn_technology: pass the NAME of a library, "
        "framework or tool and DocsForge finds its official documentation, "
        "confirms the page really documents it, harvests the whole thing and "
        "stores it. You do not need a documentation URL, and you should not "
        "invent one — a remembered URL comes from the same knowledge that did "
        "not include the technology. "
        "Already harvested? read_knowledge_base and search_knowledge_base answer "
        "from the store without touching the network. Working in a repository? "
        "scan_project reports its dependencies, their versions, and which are "
        "already documented. Use fetch_docs and harvest_docs only when you "
        "genuinely already have a URL. "
        "A harvest that outlives the 25-second deadline keeps running in the "
        "background and hands back a harvest id; harvest_status reports its "
        "progress. Its pages land in DocsForge's own knowledge base, not in "
        "your context, so there is nothing for you to collect and no reason "
        "to stop it early — let it finish, then read it back."
    ),
)


# ─────────────────────────────────────────────────────────────
# JSON Schema -> a typed Python signature the SDK can read
# ─────────────────────────────────────────────────────────────
SCALARS: dict[str, type] = {
    "string": str, "integer": int, "boolean": bool, "number": float,
}


def _annotation(spec: dict) -> tuple[Any, dict]:
    """The Python type for one JSON Schema property, plus its Field kwargs."""
    base: Any = SCALARS.get(spec.get("type", "string"), str)
    if spec.get("enum"):
        base = Literal[tuple(spec["enum"])]        # type: ignore[misc]

    kwargs: dict = {}
    if spec.get("description"):
        kwargs["description"] = spec["description"]
    # Constraints only apply to numbers; Field would reject them elsewhere.
    if base in (int, float):
        if "minimum" in spec:
            kwargs["ge"] = spec["minimum"]
        if "maximum" in spec:
            kwargs["le"] = spec["maximum"]
    return base, kwargs


def _build(tool: forge_tools.Tool):
    """A callable whose signature mirrors `tool.schema`.

    The SDK derives a tool's input schema from type hints, so the schema has to
    become a signature. Everything runs in a worker thread: the tool bodies are
    blocking, and a crawl must not stall the event loop serving the client.
    """
    properties: dict = tool.schema.get("properties") or {}
    required: list[str] = list(tool.schema.get("required") or [])

    params, hints = [], {}
    # Required first — a signature cannot put a defaulted parameter before one
    # without a default.
    for key in sorted(properties, key=lambda k: k not in required):
        spec = properties[key] or {}
        base, kwargs = _annotation(spec)

        if key in required:
            annotation = Annotated[base, Field(**kwargs)]
            params.append(inspect.Parameter(
                key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation))
        else:
            default = spec.get("default", None)
            if default is None:
                base = base | None
            annotation = Annotated[base, Field(**kwargs)]
            params.append(inspect.Parameter(
                key, inspect.Parameter.KEYWORD_ONLY,
                default=default, annotation=annotation))
        hints[key] = annotation

    async def run(**kwargs: Any) -> str:
        # Drop unset optionals so each tool's own defaults stay authoritative.
        given = {k: v for k, v in kwargs.items()
                 if v is not None or k in required}
        # Through the library's runner, as the web chat goes: the call is
        # traced and logged, and a failure comes back as text a model can act
        # on. Raised as the SDK's *deliberate* error so the result is marked
        # `isError` and keeps its text — from SDK 2.1 any other exception is
        # a "crash" whose message stays on the server, which is how a client
        # came to see a bare "Error executing tool find_docs" and nothing
        # else for a registry lookup that had a perfectly good reason.
        text, ok = await anyio.to_thread.run_sync(
            lambda: forge_tools.run_tool_checked(tool.name, given))
        if not ok:
            raise ToolError(text[len("Error: "):] if text.startswith("Error: ") else text)
        return text

    run.__name__ = tool.name
    run.__doc__ = tool.description
    run.__signature__ = inspect.Signature(params, return_annotation=str)
    run.__annotations__ = dict(hints, **{"return": str})
    return run


def register(target: MCPServer = server) -> list[str]:
    """Expose every tool in forge_tools over MCP. Returns the names."""
    for tool in forge_tools.TOOLS:
        target.add_tool(_build(tool), name=tool.name, description=tool.description)
    return [t.name for t in forge_tools.TOOLS]


register()


# ─────────────────────────────────────────────────────────────
# The HTTP surface around /mcp
#
# Hosted, this process is reached at one public hostname. `/mcp` is the tool
# surface and is gated by a bearer token; `/` is the landing page and `/health`
# is what a platform polls, and both are public. The SDK registers
# `custom_route` handlers outside its own auth on purpose, and the gate below
# is our own ASGI layer rather than the SDK's OAuth machinery, because that
# machinery has to be configured when `server` is constructed — which would put
# a token requirement on every local `--http` run too.
# ─────────────────────────────────────────────────────────────

#: The public site: three self-contained HTML pages, no outside script but the
#: Google tag, nothing from the web chat in `app.py` — that surface is for
#: local testing and is not part of the hosted process. Package data, so it
#: ships in the wheel. Every file here is written by scripts/build_site.py.
SITE = Path(__file__).resolve().parent / "site"
PAGES = {"/": "index.html", "/tools": "tools.html", "/connect": "connect.html",
         "/contributors": "contributors.html"}
#: What any other address a browser asks for gets, with a 404 and noindex.
NOT_FOUND = "404.html"
#: Everything else the site serves, at the path it is asked for by. The logo
#: and favicon are also what the chat in `app.py` and the sign-in page in
#: `oauth.py` ask for; the rest are for search engines (`robots.txt`,
#: `sitemap.xml`), social cards, home screens and language models (`llms.txt`).
FILES = {"/logo.svg": ("logo.svg", "image/svg+xml"),
         "/favicon.ico": ("favicon.ico", "image/x-icon"),
         "/og.png": ("og.png", "image/png"),
         "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
         "/icon-192.png": ("icon-192.png", "image/png"),
         "/icon-512.png": ("icon-512.png", "image/png"),
         "/favicon-192.png": ("favicon-192.png", "image/png"),
         "/mascot.webp": ("mascot.webp", "image/webp"),
         "/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
         "/llms.txt": ("llms.txt", "text/plain; charset=utf-8"),
         "/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
         "/sitemap.xml": ("sitemap.xml", "application/xml")}

#: Search-engine ownership checks, by meta tag, so verifying the site needs no
#: DNS record on a domain the project does not own. Each value is the token
#: the engine hands out (the `content` of the tag it shows), set in the
#: platform's environment; unset, no tag is written.
VERIFICATION = {"DOCSFORGE_GOOGLE_SITE_VERIFICATION": "google-site-verification",
                "DOCSFORGE_BING_SITE_VERIFICATION": "msvalidate.01"}

#: IndexNow (Bing, Yandex, Seznam, Naver) takes a site's word that a page
#: changed, instead of waiting for a crawl. The key is any 8-128 letters,
#: digits or dashes; the site proves it owns it by serving `/<key>.txt`.
#: Unset, nothing is served. `scripts/indexnow.py` submits the sitemap.
INDEXNOW_VAR = "DOCSFORGE_INDEXNOW_KEY"

TOKEN_VAR = "DOCSFORGE_MCP_TOKEN"
INSECURE_VAR = "DOCSFORGE_MCP_INSECURE"


def public_url(request: Request) -> str:
    """Where this server is reachable, as the page's reader reached it.

    `DOCSFORGE_PUBLIC_URL` wins when set. Otherwise the request's own host,
    with the scheme a proxy forwarded — on a platform like Vercel the app
    sees plain HTTP behind TLS termination, and a `Connect` page that told
    people to use `http://` there would be wrong.
    """
    configured = (os.environ.get("DOCSFORGE_PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{scheme}://{host}"


def site_verification(env=os.environ) -> str:
    """The verification meta tags whose tokens are configured, else nothing."""
    tags = []
    for var, name in VERIFICATION.items():
        token = (env.get(var) or "").strip()
        if token:
            tags.append(f'<meta name="{name}" content="{html.escape(token, quote=True)}">')
    return "\n  ".join(tags)


def page(name: str, request: Request, status: int = 200) -> Response:
    """One site page, with the version chip and the server's own URL filled in."""
    path = SITE / name
    if not path.exists():
        if status != 200:
            return PlainTextResponse("Not Found", status_code=status)
        return JSONResponse({"name": "docsforge", "version": __version__, "mcp": "/mcp"})
    body = (path.read_text(encoding="utf-8")
            .replace("{{VERSION}}", __version__)
            .replace("{{BASE_URL}}", public_url(request))
            .replace("{{SITE_VERIFICATION}}", site_verification()))
    headers = ({"Cache-Control": "public, max-age=300"} if status == 200 else
               {"Cache-Control": "no-cache", "X-Robots-Tag": "noindex"})
    return Response(body, status_code=status, media_type="text/html; charset=utf-8",
                    headers=headers)


async def not_found(request: Request, exc: Exception) -> Response:
    """A 404 a person can do something with: the site's own page, still a 404
    and marked noindex. Anything that is not a browser asking for a page —
    an MCP client, an OAuth discovery probe, a script — keeps the plain one."""
    browser = (request.method in ("GET", "HEAD")
               and "text/html" in request.headers.get("accept", "")
               and not request.url.path.startswith(("/mcp", "/.well-known")))
    if browser:
        return page(NOT_FOUND, request, status=404)
    return PlainTextResponse("Not Found", status_code=404,
                             headers=getattr(exc, "headers", None))


@server.custom_route("/", methods=["GET"], include_in_schema=False)
async def landing(request: Request) -> Response:
    return page(PAGES["/"], request)


@server.custom_route("/tools", methods=["GET"], include_in_schema=False)
async def tools_page(request: Request) -> Response:
    return page(PAGES["/tools"], request)


@server.custom_route("/connect", methods=["GET"], include_in_schema=False)
async def connect_page(request: Request) -> Response:
    return page(PAGES["/connect"], request)


@server.custom_route("/contributors", methods=["GET"], include_in_schema=False)
async def contributors_page(request: Request) -> Response:
    return page(PAGES["/contributors"], request)


def site_file(path: str) -> Response:
    """One of FILES. An hour, not a day: the names carry no hash, so a new
    logo has to be able to replace the old one."""
    name, media_type = FILES[path]
    return FileResponse(SITE / name, media_type=media_type,
                        headers={"Cache-Control": "public, max-age=3600"})


def _serves(path: str):
    async def endpoint(request: Request) -> Response:
        return site_file(path)
    return endpoint


for _path in FILES:
    server.custom_route(_path, methods=["GET"], include_in_schema=False)(_serves(_path))


def indexnow_key(env=os.environ) -> str:
    """The configured IndexNow key, or "" when unset or not a valid key."""
    key = (env.get(INDEXNOW_VAR) or "").strip()
    return key if re.fullmatch(r"[A-Za-z0-9-]{8,128}", key) else ""


# Registered after every other `.txt` route, so it only sees what they did not.
@server.custom_route("/{key}.txt", methods=["GET"], include_in_schema=False)
async def indexnow_key_file(request: Request) -> Response:
    key = indexnow_key()
    if not key or request.path_params["key"] != key:
        raise HTTPException(status_code=404)
    return Response(key, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@server.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> Response:
    # `kind` only. `location` names the database host and that is nobody's
    # business but the operator's. `degraded` is the one thing a platform
    # check should see: a configured database that could not be reached,
    # which on a stateless host means nothing harvested here will persist.
    backend = forge_tools.store()
    degraded = bool(getattr(backend, "degraded", ""))
    return JSONResponse({"status": "degraded" if degraded else "ok",
                         "version": __version__, "store": backend.kind,
                         "degraded": degraded})


class BearerGate:
    """Require `Authorization: Bearer <token>` on the MCP path.

    Two kinds of bearer are accepted: the DocsForge token itself, and an
    access token this server's OAuth side issued to a client that logged in
    with that same token (see `oauth.py`). Everything else passes through
    untouched. Comparison is constant-time and the token is read once, at
    construction, so rotating it means restarting — which is also the only
    way the SDK's own session state would notice.

    The 401 names where OAuth discovery starts, which is how a client that
    cannot send a static token finds its way to the login page.
    """

    def __init__(self, app, token: str, protect: str = "/mcp", verifier=None,
                 resource_metadata: str | None = None):
        self.app, self.token, self.protect = app, token.encode(), protect
        self.verifier = verifier
        challenge = "Bearer"
        if resource_metadata:
            challenge += f', resource_metadata="{resource_metadata}"'
        self.challenge = challenge

    def accepts(self, presented: bytes) -> bool:
        if hmac.compare_digest(presented, self.token):
            return True
        return bool(self.verifier and self.verifier(presented.decode("utf-8", "replace")))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == self.protect.rstrip("/"):
            header = dict(scope.get("headers") or {}).get(b"authorization", b"")
            scheme, _, presented = header.partition(b" ")
            if scheme.lower() != b"bearer" or not self.accepts(presented.strip()):
                response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": self.challenge})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class CompressSite:
    """gzip the site — its pages are 40-140 KB of inline HTML, CSS and SVG —
    and leave `/mcp` exactly as the SDK sends it, event stream included."""

    def __init__(self, app, protect: str = "/mcp"):
        self.app, self.protect = app, protect
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    @property
    def routes(self):
        return getattr(self.app, "routes", [])

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith(self.protect):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def is_loopback(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def choose_http(http_flag: bool, stdio_flag: bool) -> bool:
    """Which transport `main.py` runs with no flag given.

    One command has to do two jobs. A person typing `python main.py` wants
    the server *and* the site, and stdio cannot carry a web page. An MCP
    client launching the same command wants JSON-RPC on stdin/stdout and
    nothing else on that channel. The two are told apart by the one thing
    that always differs: a client speaks over pipes, a person sits at a
    terminal. Either flag overrides the guess — a service launched with no
    terminal passes `--http`, as the Containerfile does.
    """
    if http_flag:
        return True
    if stdio_flag:
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):        # stdin closed or replaced
        return False


def configured_public_url(fallback: str) -> str:
    """Where this server says it lives, for OAuth discovery.

    Discovery documents name absolute URLs, so unlike the Connect page this
    cannot be taken from each request. `DOCSFORGE_PUBLIC_URL` if set; on
    Vercel the production domain the platform provides; else the fallback
    the caller knows (the bind address, for a local run).
    """
    configured = (os.environ.get("DOCSFORGE_PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    vercel = (os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or "").strip()
    if vercel:
        return f"https://{vercel}"
    return fallback.rstrip("/")


def build_http_app(host: str = "127.0.0.1", token: str | None = None,
                   stateless: bool = False, public_url: str | None = None):
    """The ASGI app `--http` serves: `/`, `/health`, and a gated `/mcp`.

    Separate from `main()` so a test can drive it in-process. `token=None`
    leaves `/mcp` open, which is only acceptable on loopback — `main()` is
    where that rule is enforced, because it is the one place that knows the
    bind address.

    With a token, the server is also an OAuth authorization server whose
    login is that token (`oauth.py`), so clients that cannot send a bearer
    header — ChatGPT — can still get in. `public_url` is what discovery
    advertises; see `configured_public_url`.

    `stateless` is for a host where no two requests are guaranteed the same
    process: every request then carries everything, answers are plain JSON
    rather than an event stream, and no session lives in memory between them.
    """
    app = server.streamable_http_app(host=host, stateless_http=stateless,
                                     json_response=stateless)
    app.add_exception_handler(404, not_found)
    if not token:
        return CompressSite(app)
    from docsforge.server import oauth
    base = configured_public_url(public_url or f"http://{host}:8765")
    provider = oauth.StatelessProvider(token, base)
    # Mounted here, at runtime, rather than handed to `MCPServer` at import:
    # the SDK would then gate /mcp on every run, token or not, and the
    # decision belongs to whoever built this app.
    try:
        oauth_routes = oauth.routes(provider)
    except ValueError as why:
        # The SDK insists an issuer be HTTPS unless it is loopback — right,
        # since codes and tokens travel through it. A deployment that cannot
        # say where it lives keeps the bearer token and loses only OAuth.
        print(f"DocsForge: OAuth not offered ({why}; public URL is {base}) — "
              f"set DOCSFORGE_PUBLIC_URL to an https address to enable it", file=sys.stderr)
        return BearerGate(CompressSite(app), token)
    app.router.routes.extend(oauth_routes)
    return BearerGate(CompressSite(app), token, verifier=provider.verify_access,
                      resource_metadata=oauth.resource_metadata_url(base))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="docsforge-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--http", action="store_true",
                      help="serve the site and /mcp over HTTP (the default at a terminal)")
    mode.add_argument("--stdio", action="store_true",
                      help="speak MCP on stdin/stdout (the default when launched by a client)")
    ap.add_argument("--host", default="127.0.0.1")
    # `PORT` is how every container platform says which port it routes to.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765))
    ap.add_argument("--list", action="store_true", help="print the tool surface and exit")
    args = ap.parse_args(argv)

    # Only stderr: on stdio transport, stdout is the JSON-RPC channel and the
    # SDK owns its encoding.
    enable_utf8_console(("stderr",))

    if args.list:
        for tool in forge_tools.TOOLS:
            print(f"{tool.name}\n    {tool.description[:120]}…")
        return 0

    # Say which store answered, on the one surface that could not say it. The
    # panel shows a storage chip for the same reason: reading a folder of
    # files while believing you are reading a ranked Postgres index is a lie
    # about the quality of the answer, and this surface used to tell it
    # silently — `.env` was never read here, so `DOCSFORGE_DB` was invisible
    # and every harvest went to a `FileStore` nobody had asked for.
    backend = forge_tools.store()
    # `location`, never `dsn`: the DSN carries the password.
    print(f"DocsForge: knowledge base = {backend.kind} "
          f"({backend.location})", file=sys.stderr)
    if getattr(backend, "degraded", ""):
        print(f"DocsForge: WARNING — fell back to files: {backend.degraded}",
              file=sys.stderr)

    if choose_http(args.http, args.stdio):
        token = (os.environ.get(TOKEN_VAR) or "").strip()
        if not token and not is_loopback(args.host) and not os.environ.get(INSECURE_VAR):
            # An open /mcp on a public address lets anyone on the internet
            # make this machine crawl sites into its database. Refuse, rather
            # than start and hope nobody finds it.
            print(f"DocsForge: refusing to bind {args.host} without a token — set "
                  f"{TOKEN_VAR} (clients send it as `Authorization: Bearer …`), "
                  f"or {INSECURE_VAR}=1 if the network itself is private.",
                  file=sys.stderr)
            return 2
        shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        print(f"DocsForge site → http://{shown}:{args.port}/", file=sys.stderr)
        print(f"DocsForge MCP  → http://{shown}:{args.port}/mcp "
              f"({'bearer token required' if token else 'no token: open'})",
              file=sys.stderr)
        import uvicorn
        uvicorn.run(build_http_app(host=args.host, token=token or None,
                                   public_url=f"http://{shown}:{args.port}"),
                    host=args.host, port=args.port, log_level="info")
    else:
        # This process is launched per turn by whatever client attached us --
        # the `claude` CLI does exactly that -- and is torn down with it,
        # together with its whole job object. Nothing started here can outlive
        # the turn: a thread dies with the process, the wait below only helps
        # a polite hang-up, and a detached child is killed with the job anyway.
        # Measured, all three:
        #
        #     close-stdin            lived a further 232s   stored: 1.0.0.md
        #     kill                   lived a further   2s   stored: NOTHING
        #     detached, job closed   tick 4 -> 4            DIED
        #
        # So a harvest asked for here is handed to the long-lived DocsForge
        # server, which was never in this job.
        harvest_jobs.DETACHED = True

        print("DocsForge: stdio transport — MCP on stdin/stdout"
              + (" (pass --http for the site and an HTTP endpoint)" if sys.stdin.isatty() else ""),
              file=sys.stderr, flush=True)
        # stdout is the protocol channel on stdio; never print to it.
        server.run(transport="stdio")

        # `run` returns when the client closes stdin, and this process is
        # launched per turn by whatever client attached us -- the `claude` CLI
        # does exactly that. Harvest threads are daemons, so returning here
        # killed a 561-page langchain harvest at page 59, nine seconds after
        # `learn_technology` had promised it would keep going. Nothing was
        # stored and nothing said why.
        #
        # So wait for our own harvests before leaving. Bounded, and the
        # threads are still daemons underneath: whatever has not finished by
        # then is abandoned rather than orphaning this process forever, and
        # its status record goes stale and reports itself stopped.
        # Ours only. Another process's harvest is its own problem, and
        # waiting on one we cannot even observe finishing would hang.
        left = [j for j in harvest_jobs.running() if j.mine]
        if left:
            print(f"DocsForge: waiting for {len(left)} harvest(s) to finish "
                  f"before exiting", file=sys.stderr, flush=True)
            abandoned = harvest_jobs.wait_for_all()
            if abandoned:
                print(f"DocsForge: gave up on {abandoned} harvest(s) after "
                      f"{harvest_jobs.LINGER:.0f}s -- set DOCSFORGE_HARVEST_LINGER=0 "
                      f"to wait for however long a harvest takes",
                      file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
