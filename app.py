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
  python app.py                 # http://127.0.0.1:8000
  python app.py --port 8080 --reload

Needs at least one provider configured — see .env.example.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterator

import nh3
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

load_dotenv(find_dotenv(usecwd=True))

import forge_tools  # noqa: E402  (after load_dotenv so tool config sees .env)
import providers  # noqa: E402
from docsforge import enable_utf8_console  # noqa: E402
from providers import MAX_CONTENT, MAX_HISTORY, ProviderError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

SYSTEM_PROMPT = """You are DocsForge, an assistant that turns software documentation into clean, useful Markdown.

You have tools that fetch and extract documentation from any URL — docs sites, OpenAPI/Swagger specs, sitemaps, GitHub repos, llms.txt files, and raw Markdown.

Choosing a tool — this is the important part:
- **A whole technology** ("get the whole documentation", "learn X for me", or any question about a library you do not already know well): call `harvest_docs` with any page of its docs. It finds the rest of the site itself and stores it. Do NOT try to assemble a manual out of repeated `fetch_docs` calls.
- **Before harvesting anything**, call `list_knowledge_base`. If the technology is already stored, use `read_knowledge_base` instead — re-scraping a site you already have is wasted time.
- **Answering a specific question** about something already harvested: `read_knowledge_base` with a `section` phrase, so you pull the relevant pages rather than a whole manual.
- **One specific page**: `fetch_docs`. Set `crawl: true` only for a handful of linked pages; for anything bigger, `harvest_docs` is the right tool.
- `save_docs` when the user explicitly wants files written somewhere.
- `detect_source_type` only when you genuinely cannot tell what a URL is and it matters.
- `js: true` only if a normal fetch came back empty or obviously JS-rendered.

Never answer about a library from memory when its docs are one `harvest_docs` call away — being current is the entire point of this tool.

Other rules:
- When the user mentions a URL, actually fetch it before answering. Never guess at what a page says.
- `harvest_docs` returns a summary, not the documentation. Read the content back with `read_knowledge_base` before answering questions about it.

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


def chat_stream(history: list[dict], provider_name: str | None) -> Iterator[str]:
    """Drive the chosen provider, mapping its events onto SSE."""
    try:
        provider = providers.get(provider_name)
    except ProviderError as e:
        yield _sse("error", {"message": str(e)})
        return

    answer: list[str] = []
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
                yield _sse("tool", {"phase": "start", "name": event["name"], "args": event["args"]})
            elif kind == "tool_end":
                yield _sse("tool", {
                    "phase": "end",
                    "name": event["name"],
                    "ok": event["ok"],
                    "chars": event["chars"],
                    "kind": event["kind"],
                    "preview": event["preview"],
                })
            elif kind == "notice":
                yield _sse("notice", {"message": event["message"]})

        markdown = "".join(answer).strip() or "_(no response generated)_"
        yield _sse("done", {
            "markdown": markdown,
            "html": render_markdown(markdown),
            "provider": provider.name,
            "model": provider.model(),
        })

    except ProviderError as e:
        yield _sse("error", {"message": str(e)})
    except Exception as e:  # network blips, bad key, model errors
        yield _sse("error", {"message": f"{type(e).__name__}: {e}"})


# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="DocsForge Chat", version="1.2.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/config")
def config():
    catalog = providers.catalog()
    return {
        "providers": catalog,
        "provider": providers.default_name(),
        "ready": any(p["available"] for p in catalog),
        "tools": [{"name": t.name, "description": t.description} for t in forge_tools.TOOLS],
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
    uvicorn.run("app:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
