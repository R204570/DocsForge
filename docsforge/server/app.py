#!/usr/bin/env python3
"""
DocsForge web chat.

A single-page chat UI (a HyperCard-style card stack) backed by any of five
model providers, all wired to the DocsForge tools from forge_tools.py — the
same tools mcp_server.py exposes over MCP. Ask it about any docs URL and it
fetches, extracts, and answers in Markdown, which the page renders.

The provider is chosen per request, so when one runs out of quota you switch
in the UI and keep working.

Run:
  python -m docsforge.server.app                 # http://127.0.0.1:8000
  python -m docsforge.server.app --port 8080 --reload

Needs at least one provider configured — see .env.example.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Iterator

import nh3
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

load_dotenv(find_dotenv(usecwd=True))

from docsforge.tools import applog  # noqa: E402
from docsforge.tools import forge_tools  # noqa: E402  (after load_dotenv so tool config sees .env)
from docsforge.tools import harvest_jobs  # noqa: E402
from docsforge import providers  # noqa: E402
from docsforge.tools import tracing  # noqa: E402
from docsforge.core.engine import enable_utf8_console  # noqa: E402
from docsforge.store.kb_store import StoreError  # noqa: E402
from docsforge.providers import MAX_CONTENT, MAX_HISTORY, ProviderError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

SYSTEM_PROMPT = """You are DocsForge, an assistant that turns software documentation into clean, useful Markdown.

You have tools that fetch and extract documentation from any URL — docs sites, OpenAPI/Swagger specs, sitemaps, GitHub repos, llms.txt files, and raw Markdown.

## The one rule that matters

**Asked about a technology? Call `learn_technology(name="...")` immediately.**
You do not need a URL, and you do not need to check anything first. It already
looks in the store, and if the technology is there it tells you so and fetches
nothing. Then read it back with `read_knowledge_base` and answer.

    user: "what is astro js?"   ->  learn_technology(name="astro")
                                ->  read_knowledge_base(name="astro")
                                ->  answer from what came back

**Do NOT call `list_knowledge_base` to decide what to do.** It answers exactly
one question — "what is stored?" — and it is only ever the right call when the
user asked that. Calling it before `learn_technology` is wasted, and listing
the store back to someone who asked about a library is not an answer to their
question.

**Never stop at a tool result.** A tool returning something is the middle of
your turn, not the end of it. Do not summarise what a tool returned unless the
user asked for that summary — keep calling tools until you can answer what was
actually asked. If the user has asked twice, you have gone wrong somewhere: the
next call should be `learn_technology`.

## Choosing a tool

- **You do NOT need a URL.** Any library, framework or tool you do not already know well — from an import, a config file, an error message, a package name — is a `learn_technology(name="...")` call. It finds the official documentation, confirms the page really documents that package, harvests all of it and stores it.
- **Never invent a documentation URL.** A URL you recall comes from the same training data that did not know the library, and a wrong guess silently harvests the wrong project. If resolution fails, say so and ask the user for the URL — that is a good answer, not a failure.
- **Working on a codebase?** `scan_project` reads its manifests and tells you what it depends on, at which versions, and which are already documented. The manifest is the ONLY place the correct version can be read from — versions of the same library contradict each other, so pass `version` to `learn_technology` when you know it.
- **Have a symbol or error but not the library name?** `search_knowledge_base` searches the text of every stored page at once. `read_knowledge_base` needs you to know the name; this does not.
- **Answering a specific question** about something already stored: `read_knowledge_base` with a `section` phrase, so you pull the relevant pages rather than a whole manual.
- **You already have a URL**: `harvest_docs` for a whole documentation set, `fetch_docs` for one page. Set `crawl: true` only for a handful of linked pages.
- `find_docs` to see where something would be harvested from without harvesting it.
- `list_knowledge_base` **only** when the user asks what is stored.
- `save_docs` when the user explicitly wants files written somewhere.
- `detect_source_type` only when you genuinely cannot tell what a URL is and it matters.
- `js: true` only if a normal fetch came back empty or obviously JS-rendered.

Never answer about a library from memory when its docs are one `learn_technology` call away — being current is the entire point of this tool.

Other rules:
- When the user mentions a URL, actually fetch it before answering. Never guess at what a page says.
- `harvest_docs` returns a summary, not the documentation. Read the content back with `read_knowledge_base` before answering questions about it.
- Tool results may say a copy is INCOMPLETE or its COVERAGE UNKNOWN. Pass that on: say what you are working from, and do not present a gap in a partial copy as something the real documentation lacks.

Answer formatting — this matters, the UI renders your reply as Markdown:
- ALWAYS reply in well-formed Markdown. Never wrap your whole answer in a code fence.
- Use `##` / `###` headings, bullet lists, and tables to organise information.
- Put code in fenced blocks with a language tag.
- Link to sources inline with real URLs.
- When you summarise fetched docs, be faithful to them and say so if something was truncated or failed to load.
- Keep responses focused and concise; put the answer first and supporting detail after.
"""


# ─────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────
_MD = MarkdownIt("gfm-like")

_ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) | {"del", "s", "input"}
_ALLOWED_ATTRS: dict[str, set[str]] = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
for _tag in ("code", "pre", "span", "div", "table", "th", "td"):
    _ALLOWED_ATTRS.setdefault(_tag, set()).add("class")


def render_markdown(text: str) -> str:
    """Markdown → sanitized HTML. The content is model output mixed with
    scraped pages, so it is treated as untrusted and run through nh3."""
    html = _MD.render(text or "")
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        link_rel="noopener noreferrer nofollow",
    )


# ─────────────────────────────────────────────────────────────
# Request handling
# ─────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    provider: str | None = None


def _clean_history(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages[-MAX_HISTORY:]:
        if m.role not in ("user", "assistant"):
            continue
        content = (m.content or "")[:MAX_CONTENT]
        if not content.strip():
            continue
        out.append({"role": m.role, "content": content})
    return out


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _trace_a_reported_call(name: str, args: dict, result: str, ok: bool) -> str | None:
    """Record a tool call this process did not run itself.

    Not every provider executes tools in this process: the Claude Code
    provider hands DocsForge's tool surface to the CLI, which calls it over
    MCP from a process of its own, so `run_tool` -- and the trace it builds
    -- never happen here. The provider still reports what it called and what
    came back, and that is precisely what the detail view is for.

    What such a trace cannot contain is the internal stages (resolving,
    harvesting, storing): those really did happen somewhere else. It says
    what ran and what it returned, and claims nothing further.
    """
    try:
        ctx = tracing.start(name)
        stage = ctx.stage(name, target=forge_tools.target_of(args),
                          metadata=dict(args or {}))
        stage.start(f"called {name}")
        if ok:
            stage.finish(tracing.COMPLETED,
                         message=f"returned {len(result):,} characters",
                         result={"characters": len(result)}, output=result)
        else:
            stage.finish(tracing.FAILED, error=result[:300], output=result)
        ctx.close()
        return ctx.trace_id
    except Exception:
        return None   # observability must never break the turn it describes


def chat_stream(history: list[dict], provider_name: str | None) -> Iterator[str]:
    """Drive the chosen provider, mapping its events onto SSE."""
    turn_started = time.perf_counter()
    tool_log: list[str] = []          # the "behaviour pattern" this turn traced
    outcome = "error"
    # Server threads are reused, so an id left by an earlier turn must not be
    # mistaken for this one's.
    forge_tools.reset_trace_id()

    try:
        provider = providers.get(provider_name)
    except ProviderError as e:
        yield _sse("error", {"message": str(e)})
        applog.turn(tool_log, "provider_unavailable",
                   (time.perf_counter() - turn_started) * 1000)
        return

    answer: list[str] = []
    #: Arguments seen at tool_start, kept until the matching tool_end, for
    #: providers whose tools this process never sees run.
    pending_args: dict[str, dict] = {}
    claimed: set[str] = set()
    try:
        for event in provider.stream(
            system=SYSTEM_PROMPT,
            history=history,
            tools=forge_tools.TOOLS,
            run_tool=forge_tools.run_tool,
        ):
            kind = event["type"]
            if kind == "text":
                answer.append(event["text"])
                yield _sse("token", {"text": event["text"]})
            elif kind == "tool_start":
                pending_args[event["name"]] = event.get("args") or {}
                yield _sse("tool", {"phase": "start", "name": event["name"], "args": event["args"]})
            elif kind == "tool_end":
                # An in-process provider has just returned from `run_tool()`
                # on this very thread, so the id it recorded is still in the
                # thread-local and carries the full nested trace. A provider
                # that ran the tool elsewhere leaves nothing there -- or,
                # after an earlier in-process call this turn, leaves an id
                # already spoken for. Either way the fallback records what
                # the provider itself reported.
                trace_id = forge_tools.last_trace_id()
                if not trace_id or trace_id in claimed:
                    trace_id = _trace_a_reported_call(
                        event["name"], pending_args.pop(event["name"], {}),
                        event.get("result") or event.get("preview") or "",
                        event["ok"])
                if trace_id:
                    claimed.add(trace_id)
                tool_log.append(f"{event['name']}:{'ok' if event['ok'] else 'error'}")
                yield _sse("tool", {
                    "phase": "end",
                    "name": event["name"],
                    "ok": event["ok"],
                    "chars": event["chars"],
                    "kind": event["kind"],
                    "preview": event["preview"],
                    "trace_id": trace_id,
                })
            elif kind == "notice":
                yield _sse("notice", {"message": event["message"]})

        markdown = "".join(answer).strip() or "_(no response generated)_"
        outcome = "done"
        yield _sse("done", {
            "markdown": markdown,
            "html": render_markdown(markdown),
            "provider": provider.name,
            "model": provider.model(),
        })

    except ProviderError as e:
        outcome = "provider_error"
        yield _sse("error", {"message": str(e)})
    except GeneratorExit:
        # The client disconnected (the Stop button, a closed tab) and
        # Starlette stopped iterating us. This is a *client* stopping, not
        # DocsForge failing -- logged as its own outcome so a "how did this
        # turn end" grep does not conflate the two. Whatever tool call was
        # in flight keeps running server-side; nothing here cancels it, see
        # tracing.TraceContext.detach().
        outcome = "cancelled"
        raise
    except Exception as e:  # network blips, bad key, model errors
        outcome = "error"
        yield _sse("error", {"message": f"{type(e).__name__}: {e}"})
    finally:
        applog.turn(tool_log, outcome, (time.perf_counter() - turn_started) * 1000,
                   provider=provider_name or "")


def trace_stream(trace_id: str) -> Iterator[str]:
    """Live execution timeline for one tool call.

    Opened by the browser as soon as a `tool_end` SSE event carries a
    `trace_id` — a second, independent HTTP connection, which is what makes
    this genuinely live rather than polled: it runs on its own thread, so it
    keeps reading `Trace.subscribe()` for as long as the underlying tool call
    keeps appending to it, whether that call is still on the original chat
    request's thread or, past `learn_technology`'s deadline, a harvest_jobs
    background thread the original request already returned from.

    Event names deliberately avoid the bare `error` EventSource already
    treats specially (it fires that on the browser's own reconnect logic,
    with no `data` to parse) -- `trace_error` and `trace_end` are ours.
    """
    trace = tracing.get(trace_id)
    if trace is None:
        yield _sse("trace_error", {"message": f"No trace {trace_id!r} (finished "
                                   f"and pruned, or the server restarted)."})
        return
    for event in trace.subscribe():
        yield _sse("trace", event.as_dict())
    # The trace closed -- tell the browser so it can stop this EventSource
    # rather than let it auto-reconnect to a stream that will only ever
    # replay the same, now-finished, history.
    yield _sse("trace_end", {"trace_id": trace_id})


# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
# FastAPI mounts Swagger UI at /docs by default, which silently shadows the
# product's own documentation page. This app is not an API playground, so the
# URL goes to the page users are sent to; the schema stays at /openapi.json.
app = FastAPI(title="DocsForge Chat", version="1.3.0",
              docs_url=None, redoc_url=None)


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One log line per HTTP request -- the other half of 'debugging is
    easy and fast': `tracing`/`applog.turn` cover tool-call behaviour,
    this covers the requests that triggered them."""
    started = time.perf_counter()
    response = await call_next(request)
    try:
        applog.request(request.method, request.url.path, response.status_code,
                       (time.perf_counter() - started) * 1000,
                       client=request.client.host if request.client else "")
    except Exception:
        pass
    return response


# Served with `Last-Modified` but no cache directive, a browser is free to
# invent its own freshness lifetime and serve the file again without asking —
# which is how you pull a new version of the UI and keep seeing the old one.
# `no-cache` does not mean "do not store": it means "revalidate every time",
# so an unchanged file still costs one 304 and a changed one is never missed.
REVALIDATE = "no-cache"


class FreshFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = REVALIDATE
        return response


app.mount("/static", FreshFiles(directory=STATIC), name="static")


def page(name: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC, name),
                        headers={"Cache-Control": REVALIDATE})


@app.get("/")
def index():
    return page("index.html")


@app.get("/docs")
def docs():
    """Everything that would otherwise clutter the chat screen."""
    return page("docs.html")


@app.get("/library")
def library():
    """DocsStore — everything harvested so far, by technology and version."""
    return page("library.html")


# ── DocsStore API ────────────────────────────────────────────
# The store grows without limit, so the technology list is paged rather than
# returned whole. Everything below reports which backend answered, because a
# Postgres store and a file store have visibly different capabilities.
PER_PAGE = 12


def _store():
    return forge_tools.store()


def _store_error(e: Exception, status: int = 404):
    return JSONResponse({"detail": str(e)}, status_code=status)


@app.get("/api/library")
def library_index(page: int = 1, q: str = ""):
    backend = _store()
    page = max(1, int(page))
    try:
        techs, total = backend.technologies(
            offset=(page - 1) * PER_PAGE, limit=PER_PAGE, query=q.strip())
    except StoreError as e:
        return _store_error(e, 503)
    pages = max(1, -(-total // PER_PAGE))
    return {
        "technologies": techs,
        "page": min(page, pages),
        "pages": pages,
        "total": total,
        "per_page": PER_PAGE,
        "query": q,
        # `degraded` is why the store may look empty when it is not: the
        # database is configured but unreachable, so this is the file store
        # standing in for it.
        "backend": {
            "kind": backend.kind,
            "location": backend.location,
            "degraded": getattr(backend, "degraded", "") or "",
        },
    }


@app.get("/api/library/{tech}")
def library_versions(tech: str):
    try:
        return {"technology": tech, "versions": _store().versions(tech)}
    except StoreError as e:
        return _store_error(e)


# ── Removing things ──────────────────────────────────────────
# A harvest can be wrong — the wrong project, a partial copy, a table of
# contents stored as if it were the documentation — and until now there was no
# way to take it back out. The store could delete; nothing could ask it to.
#
# These are deliberately HTTP DELETE and deliberately not exposed to the chat
# model by default (see forge_tools.ALLOW_DELETE): the person who harvested
# something is the one who should decide it was a mistake.
@app.delete("/api/library/{tech}")
def library_forget(tech: str):
    """Remove a technology and every version of it."""
    try:
        removed = _store().delete(tech)
    except StoreError as e:
        return _store_error(e, 503)
    if not removed:
        return _store_error(StoreError(f"nothing stored for {tech!r}"))
    return {"technology": tech, "removed": removed, "versions": None}


@app.delete("/api/library/{tech}/{version}")
def library_forget_version(tech: str, version: str):
    """Remove one version, leaving the others alone."""
    try:
        removed = _store().delete(tech, version)
    except StoreError as e:
        return _store_error(e, 503)
    if not removed:
        return _store_error(StoreError(f"{tech} has no version {version!r}"))
    return {"technology": tech, "version": version, "removed": removed}


@app.get("/api/library/{tech}/{version}")
def library_pages(tech: str, version: str):
    backend = _store()
    try:
        entry = backend.entry(tech, version)
        if entry is None:
            return _store_error(StoreError(f"{tech} has no version {version!r}"))
        return {"technology": tech, "version": version,
                "meta": entry, "pages": backend.pages(tech, version)}
    except StoreError as e:
        return _store_error(e)


# Most extracted pages open with their own title as an H1. The reader already
# prints the title above the document, so rendering both says it twice.
_OPENING_HEADING = re.compile(r"\A\s*#{1,2}\s+(?P<title>[^\n]+?)\s*\n+")

# A stored title usually comes from <title>, which carries the site name:
# "Index | Pydantic Docs" over a document whose own heading is just "Index".
_SITE_SUFFIX = re.compile(r"\s*(?:\||—|–|·|\s-\s).*\Z")


def _without_repeated_title(content: str, title: str) -> str:
    match = _OPENING_HEADING.match(content or "")
    if not match:
        return content
    heading = match.group("title").strip().lower()
    stored = (title or "").strip().lower()
    if heading in (stored, _SITE_SUFFIX.sub("", stored)):
        return content[match.end():]
    return content


@app.get("/api/library/{tech}/{version}/page/{ordinal}")
def library_page(tech: str, version: str, ordinal: int):
    try:
        page = _store().page(tech, version, ordinal)
    except StoreError as e:
        return _store_error(e)
    body = _without_repeated_title(page["content"], page["title"])
    return {**page, "html": render_markdown(body)}


@app.get("/api/library-search")
def library_search(q: str, tech: str = "", version: str = "", limit: int = 30):
    q = q.strip()
    if not q:
        return {"query": "", "hits": []}
    try:
        hits = _store().search(q, tech or None, version or None,
                               max(1, min(int(limit), 100)))
    except StoreError as e:
        return _store_error(e, 503)
    return {"query": q, "technology": tech, "version": version,
            "hits": hits, "ranked": _store().kind == "postgres"}


@app.get("/api/config")
def config():
    catalog = providers.catalog()
    return {
        "providers": catalog,
        "provider": providers.default_name(),
        "ready": any(p["available"] for p in catalog),
        "tools": [{"name": t.name, "description": t.description} for t in forge_tools.TOOLS],
    }


@app.post("/api/harvests")
def start_harvest(payload: dict):
    """Run a harvest here, on behalf of a host that cannot survive one.

    The stdio MCP server is launched per turn by the `claude` CLI and torn
    down with it — and *with its whole job object*, so nothing it starts can
    outlive the turn, however detached. Windows kills every process in a job
    on close, and `CREATE_BREAKAWAY_FROM_JOB` is refused unless the job grants
    it. No spawn flag wins that argument.

    This process was never in that job. It is the host where background
    harvests have always worked, and it is already the one showing the tracker.

    The record is reserved before the thread starts, so the id can be returned
    at once and the caller is never told to watch something that does not yet
    exist.
    """
    kwargs = payload.get("kwargs") or {}
    label = str(payload.get("label") or kwargs.get("name") or "").strip()
    if not label or not isinstance(kwargs, dict):
        return JSONResponse({"error": "a label and kwargs are required"},
                            status_code=400)

    # Already being fetched? Say so rather than crawl the site twice.
    wanted = forge_tools._kb_slug(forge_tools._normalise(label) or label)
    for other in harvest_jobs.running():
        if forge_tools._kb_slug(
                forge_tools._normalise(other.label) or other.label) == wanted:
            return {"job": other.id, "already": True}

    job = harvest_jobs.reserve(label)

    def run() -> None:
        harvest_jobs.adopt(job.id)
        try:
            forge_tools.tool_learn_technology(**kwargs)
        except Exception as e:                          # noqa: BLE001
            applog.error("harvest_handoff", f"{type(e).__name__}: {e}")
            current = harvest_jobs.get(job.id)
            if current is None or current.state == harvest_jobs.RUNNING:
                job.state = harvest_jobs.FAILED
                job.error = str(e) or type(e).__name__
                job.finished = time.time()
                harvest_jobs._publish(job)

    threading.Thread(target=run, name=f"handoff:{job.id}", daemon=True).start()
    return {"job": job.id}


@app.get("/api/harvests")
def harvests():
    """Every harvest in flight, and how the recent ones ended.

    A background harvest used to be visible only to the process that started
    it, and with the `claudecode` provider that is a subprocess which exits
    with the turn. So a harvest ran for minutes with nothing to watch: not in
    the panel, not in the log, not in `list_knowledge_base` — and then its
    documentation simply appeared in DocsStore, as though from nowhere.

    `fraction` is null wherever there is no honest denominator. A manifest
    states how many pages it lists; a crawl's frontier grows as it is walked,
    and dividing by it would be inventing a number.
    """
    live = [j.as_dict() for j in harvest_jobs.running()]
    return {
        "running": live,
        "recent": [j.as_dict() for j in harvest_jobs.recent()[:10]],
        "count": len(live),
    }


@app.post("/api/render")
def render(payload: dict):
    return {"html": render_markdown(str(payload.get("markdown", "")))}


@app.post("/api/chat")
def chat(req: ChatRequest):
    history = _clean_history(req.messages)
    if not history:
        return JSONResponse({"detail": "No messages provided."}, status_code=400)
    return StreamingResponse(
        chat_stream(history, req.provider),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/trace/{trace_id}")
def trace(trace_id: str):
    """The execution timeline for one tool call: what it is doing (or did)
    underneath the single work row the chat stream shows. A second SSE
    connection, deliberately -- see `trace_stream`."""
    return StreamingResponse(
        trace_stream(trace_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="docsforge-web", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)

    enable_utf8_console()

    ready = [p for p in providers.PROVIDERS if p.available()]
    if not ready:
        print("warning: no provider is configured — the UI will load but chat will error.\n"
              "         Add a key to .env (see .env.example) or install the claude CLI.",
              file=sys.stderr)
    else:
        print("providers ready: " + ", ".join(p.label for p in ready), file=sys.stderr)

    import uvicorn
    print(f"DocsForge chat → http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run("docsforge.server.app:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
