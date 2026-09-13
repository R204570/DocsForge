"""
The DocsForge server as a Vercel Function.

Vercel loads the `app` below (named in `[tool.vercel] entrypoint`) and routes
every request to it: the site at `/`, `/tools` and `/connect`, `/health`, and
the MCP endpoint at `/mcp` behind the bearer token. Same routes as
`python main.py`, with three differences that a serverless host forces:

* **Stateless.** No two requests are promised the same process, so `/mcp`
  runs the SDK's stateless mode: every request carries everything and answers
  are plain JSON. No session lives in memory between calls.

* **Ephemeral.** Nothing runs after the response is sent, so a harvest that
  outlives `DOCSFORGE_HARVEST_DEADLINE` is lost rather than continued in the
  background, and the result says exactly that (`harvest_jobs.EPHEMERAL`).
  Large harvests are done from a long-lived DocsForge — `python main.py` on
  any machine — pointed at the same database; they are readable here at once.

* **Only /tmp is writable.** The caches DocsForge keeps on disk (harvest
  status, resolution cache, selection policy, logs) go there. They are
  caches; losing them costs nothing but a repeat lookup.

Configuration is the platform's environment, never a file:

    DOCSFORGE_DB / DATABASE_URL   required — the store must be Postgres here
    DOCSFORGE_MCP_TOKEN           required — without it /mcp answers 503, not open
    DOCSFORGE_HARVEST_DEADLINE    seconds a harvest may take inside one request;
                                  keep it under vercel.json's maxDuration
"""

from __future__ import annotations

import asyncio
import os
import sys

# Before any DocsForge import: `applog` reads its directory at import time,
# and on Vercel the project directory is read-only.
if os.environ.get("VERCEL"):
    for var, path in (("DOCSFORGE_LOG_DIR", "/tmp/docsforge/logs"),
                      ("DOCSFORGE_HARVEST_STATE", "/tmp/docsforge/harvests"),
                      ("DOCSFORGE_RESOLVE_CACHE", "/tmp/docsforge/resolutions.json"),
                      ("DOCSFORGE_SELECTION_POLICY", "/tmp/docsforge/selection.json"),
                      ("DOCSFORGE_OUT_ROOT", "/tmp/docsforge/out")):
        os.environ.setdefault(var, path)

from starlette.responses import JSONResponse  # noqa: E402

from docsforge.server import mcp_server  # noqa: E402
from docsforge.tools import harvest_jobs  # noqa: E402

harvest_jobs.EPHEMERAL = True


class LifespanOnDemand:
    """Start the inner app's lifespan before its first request — per event loop.

    The SDK's HTTP app starts its session manager in ASGI lifespan startup.
    Vercel sends lifespan events; not every ASGI host or test client does,
    and an app whose startup never ran answers every request with a 500. So
    the first request starts it if nothing else has.

    Per event loop, because a serverless adapter may run each request on a
    fresh loop, and a session manager belongs to the loop that started it.
    The inner app is therefore built by a factory and rebuilt whenever the
    loop changes. Stateless mode keeps nothing between requests, so a
    rebuilt app is not a different server. A host that does send `lifespan`
    goes through here too and is not started twice.
    """

    def __init__(self, factory):
        self.factory = factory
        self._loop = None
        self._app = None
        self._lock = None

    async def _startup(self, app) -> None:
        inbox: asyncio.Queue = asyncio.Queue()
        started = asyncio.get_running_loop().create_future()

        async def receive():
            return await inbox.get()

        async def send(message):
            if started.done():
                return
            if message["type"] == "lifespan.startup.complete":
                started.set_result(None)
            elif message["type"] == "lifespan.startup.failed":
                started.set_exception(RuntimeError(message.get("message", "startup failed")))

        # Kept alive by the loop for as long as the loop lives, which is as
        # long as the session manager can be used at all.
        asyncio.ensure_future(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
        await inbox.put({"type": "lifespan.startup"})
        await started

    async def _current(self):
        loop = asyncio.get_running_loop()
        if self._app is not None and self._loop is loop:
            return self._app
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._app is None or self._loop is not loop:
                app = self.factory()
                await self._startup(app)
                self._app, self._loop = app, loop
        return self._app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # The host drives startup itself: build for this loop and let it.
            self._app, self._loop = self.factory(), asyncio.get_running_loop()
            await self._app(scope, receive, send)
            return
        app = await self._current()
        await app(scope, receive, send)


def _unavailable(reason: str):
    """An app that serves the site and refuses `/mcp` with the reason.

    A hosted instance with no database would store every harvest in a file
    system that vanishes with the instance, and one with no token would be
    open to the internet. Both are misconfigurations to surface, not
    conditions to run in — but the site is still worth serving.
    """
    def factory():
        inner = mcp_server.build_http_app(host="0.0.0.0", token=None, stateless=True)

        async def app(scope, receive, send):
            if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
                await JSONResponse({"error": "unavailable", "reason": reason},
                                   status_code=503)(scope, receive, send)
                return
            await inner(scope, receive, send)
        return app

    return LifespanOnDemand(factory)


def build(env=os.environ):
    dsn = env.get("DOCSFORGE_DB") or env.get("DATABASE_URL")
    token = (env.get(mcp_server.TOKEN_VAR) or "").strip()
    if not dsn:
        return _unavailable("DOCSFORGE_DB is not set; a stateless host needs Postgres")
    if not token:
        return _unavailable(f"{mcp_server.TOKEN_VAR} is not set; /mcp would be open")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return _unavailable("psycopg is not installed; the store would fall back to files")
    # `host` is not a bind address here — the platform owns that — it only
    # tells the SDK this is not loopback, so its localhost-only Host check
    # stays off and the deployment's own hostname is accepted.
    return LifespanOnDemand(
        lambda: mcp_server.build_http_app(host="0.0.0.0", token=token, stateless=True))


app = build()

if __name__ == "__main__":                      # a local look, nothing more
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT") or 8765))
    sys.exit(0)
