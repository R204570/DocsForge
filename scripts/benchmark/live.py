"""One DocsForge deployment, spoken to over MCP, with every call timed.

This is the only file that knows how to reach the server. The cases in
`cases.py` describe *what* to ask and what a correct answer looks like; the
runner in `run.py` decides *which* to ask. Neither imports DocsForge itself:
the thing under measurement is the deployment, not this checkout, and a
benchmark that shared code with its subject would agree with it by
construction.

Reaching the server needs a URL and a bearer token. Both come from, in order:
an explicit argument, the environment (`DOCSFORGE_MCP_URL`,
`DOCSFORGE_MCP_TOKEN`), and finally the registration `claude mcp add` wrote
to `~/.claude.json` -- the same token Claude Code itself sends, so a person
who has connected the server once has nothing more to configure.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

DEFAULT_URL = "https://docsforge.vercel.app/mcp"

#: Vercel's function ceiling is 300 s (`vercel.json`); a harvest inside one
#: request is cut at 25 s. Nothing read-only should take longer than this,
#: and a call that does is itself a finding.
CALL_TIMEOUT = 180.0
CONNECT_TIMEOUT = 30.0


@dataclass
class Result:
    """What one call gave back, and how long it took to arrive."""
    text: str = ""
    ok: bool = False              # the tool returned normally (not `isError`)
    seconds: float = 0.0
    error: str = ""               # a transport failure: timeout, refused, 5xx
    meta: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return bool(self.error)


# -- credentials -------------------------------------------------------------

def _claude_registrations() -> list[tuple[str, dict]]:
    """Every `docsforge` HTTP server registered in ~/.claude.json, any project."""
    path = Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[tuple[str, dict]] = []
    scopes: list[tuple[str, dict]] = [("user", data.get("mcpServers") or {})]
    for project, conf in (data.get("projects") or {}).items():
        scopes.append((project, (conf or {}).get("mcpServers") or {}))
    for scope, servers in scopes:
        for name, spec in servers.items():
            if name.lower() == "docsforge" and (spec or {}).get("type") == "http":
                found.append((scope, spec))
    return found


def resolve_target(url: str | None = None, token: str | None = None) -> tuple[str, str, str]:
    """`(url, token, where the token came from)`.

    A registration in ~/.claude.json only counts when it carries an
    `Authorization` header and, if a URL was already chosen, points at the
    same server: a token for one deployment is no use against another.
    """
    url = (url or os.environ.get("DOCSFORGE_MCP_URL") or "").strip()
    token = (token or "").strip()
    source = "argument"
    if not token:
        token, source = (os.environ.get("DOCSFORGE_MCP_TOKEN") or "").strip(), "environment"
    if not token:
        for scope, spec in _claude_registrations():
            header = (spec.get("headers") or {}).get("Authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not presented.strip():
                continue
            if url and spec.get("url", "").rstrip("/") != url.rstrip("/"):
                continue
            token, source = presented.strip(), f"~/.claude.json ({scope})"
            url = url or spec.get("url", "")
            break
    return (url or DEFAULT_URL), token, (source if token else "none")


# -- the connection ----------------------------------------------------------

def _ours(e: BaseException) -> bool:
    """Is this the caller's own interruption, which must propagate?

    The SDK ends a session through a cancel scope, and its `CancelledError`
    surfaces from `call_tool` and from the session's own `__aexit__`. That
    is one call failing, and is recorded as such. A `CancelledError` while
    *this* task is being cancelled, or a keyboard interrupt, is the run
    being stopped, and is not swallowed.
    """
    if isinstance(e, (KeyboardInterrupt, SystemExit)):
        return True
    if isinstance(e, asyncio.CancelledError):
        task = asyncio.current_task()
        return bool(task is not None and task.cancelling())
    return False


class Live:
    """A session against one deployment. Use as `async with`."""

    def __init__(self, url: str, token: str, timeout: float = CALL_TIMEOUT):
        self.url = url.rstrip("/")
        self.base = self.url[:-len("/mcp")] if self.url.endswith("/mcp") else self.url
        self.token = token
        self.timeout = timeout
        self._stack: list = []
        self.session: ClientSession | None = None
        self.server: dict = {}

    def _http(self, token: str | None = None) -> httpx.AsyncClient:
        headers = {}
        if token is None:
            token = self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return create_mcp_http_client(
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=CONNECT_TIMEOUT))

    async def __aenter__(self) -> "Live":
        started = time.perf_counter()
        transport = streamable_http_client(self.url, http_client=self._http())
        streams = await transport.__aenter__()
        self._stack.append(transport)
        read, write = streams[0], streams[1]
        session = ClientSession(read, write, read_timeout_seconds=self.timeout)
        await session.__aenter__()
        self._stack.append(session)
        init = await session.initialize()
        self.session = session
        self.server = {
            "name": init.server_info.name,
            "version": init.server_info.version,
            "seconds": round(time.perf_counter() - started, 3),
        }
        return self

    async def __aexit__(self, *exc) -> None:
        while self._stack:
            ctx = self._stack.pop()
            try:
                await ctx.__aexit__(*exc)
            except BaseException as e:  # noqa: BLE001
                if _ours(e):
                    raise

    async def tools(self) -> list[dict]:
        listed = await self.session.list_tools()
        return [{"name": t.name, "description": t.description or "",
                 "schema": t.input_schema} for t in listed.tools]

    async def call(self, tool: str, args: dict[str, Any] | None = None,
                   timeout: float | None = None) -> Result:
        """One tool call. A transport failure is a Result too, never a raise."""
        started = time.perf_counter()
        try:
            res = await self.session.call_tool(tool, args or {},
                                               read_timeout_seconds=timeout or self.timeout)
        except BaseException as e:  # noqa: BLE001 -- the point is to record it
            if _ours(e):
                raise
            return Result(error=f"{type(e).__name__}: {e}"[:400],
                          seconds=time.perf_counter() - started)
        seconds = time.perf_counter() - started
        text = "\n".join(getattr(c, "text", "") for c in (res.content or []))
        return Result(text=text, ok=not bool(res.is_error), seconds=seconds)

    # -- plain HTTP, outside the protocol ------------------------------------

    async def health(self) -> Result:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=CONNECT_TIMEOUT)) as c:
                r = await c.get(f"{self.base}/health")
        except Exception as e:  # noqa: BLE001
            return Result(error=f"{type(e).__name__}: {e}"[:400],
                          seconds=time.perf_counter() - started)
        seconds = time.perf_counter() - started
        try:
            body = r.json()
        except ValueError:
            body = {}
        return Result(text=r.text, ok=r.status_code == 200, seconds=seconds,
                      meta={"status": r.status_code, "json": body})

    async def unauthorized(self, token: str | None = "") -> Result:
        """POST a bare `initialize` to /mcp with no (or a wrong) token."""
        started = time.perf_counter()
        headers = {"Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "docsforge-benchmarks", "version": "0"}}}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=CONNECT_TIMEOUT)) as c:
                r = await c.post(self.url, json=body, headers=headers)
        except Exception as e:  # noqa: BLE001
            return Result(error=f"{type(e).__name__}: {e}"[:400],
                          seconds=time.perf_counter() - started)
        return Result(text=r.text[:500], ok=r.status_code == 401,
                      seconds=time.perf_counter() - started,
                      meta={"status": r.status_code,
                            "www_authenticate": r.headers.get("www-authenticate", "")})


async def fan_out(url: str, token: str, tool: str, args: dict, n: int,
                  timeout: float = CALL_TIMEOUT) -> list[Result]:
    """`n` separate clients, each making the same call at the same moment.

    Separate sessions rather than one session with `n` requests in flight:
    the question is what happens when `n` people ask at once, and against a
    stateless host that is `n` connections to the database, which is exactly
    what the managed plan's backend limit is about.

    Each client runs on its own thread with its own event loop. The SDK
    tears a session down through a cancel scope, and one session's
    `CancelledError` escaping into a shared loop took the whole run with it
    -- a `BaseException`, so no `except Exception` on the way caught it. A
    thread boundary is where any failure of one client becomes that
    client's `Result` and nothing more.
    """
    def one() -> Result:
        async def go() -> Result:
            async with Live(url, token, timeout=timeout) as live:
                return await live.call(tool, args, timeout=timeout)
        try:
            return asyncio.run(go())
        except BaseException as e:  # noqa: BLE001 -- see the docstring
            return Result(error=f"{type(e).__name__}: {e}"[:400])

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(await asyncio.gather(*(loop.run_in_executor(pool, one) for _ in range(n))))
