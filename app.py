#!/usr/bin/env python3
"""
DocsForge web chat.

A single-page chat UI (input pinned to the bottom) backed by Groq, wired to the
DocsForge tools from forge_tools.py — the same tools mcp_server.py exposes over
MCP. Ask it about any docs URL and it fetches, extracts, and answers in
Markdown, which the page renders.

Run:
  python app.py                 # http://127.0.0.1:8000
  python app.py --port 8080 --reload

Requires GROQ_API_KEY in .env (or the environment).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Iterator

import nh3
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

load_dotenv(find_dotenv(usecwd=True))

import forge_tools  # noqa: E402  (after load_dotenv so tool config sees .env)
from docsforge import enable_utf8_console  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
TEMPERATURE = float(os.environ.get("GROQ_TEMPERATURE", "1"))
MAX_TOKENS = int(os.environ.get("GROQ_MAX_TOKENS", "2048"))
TOP_P = float(os.environ.get("GROQ_TOP_P", "1"))

MAX_TOOL_ROUNDS = 4        # tool → model → tool → … before we force an answer
MAX_HISTORY = 40           # messages accepted from the client
MAX_CONTENT = 100_000      # per-message character cap

SYSTEM_PROMPT = """You are DocsForge, an assistant that turns software documentation into clean, useful Markdown.

You have tools that fetch and extract documentation from any URL — docs sites, OpenAPI/Swagger specs, sitemaps, GitHub repos, llms.txt files, and raw Markdown.

Rules:
- When the user mentions a URL, actually fetch it with `fetch_docs` before answering. Never guess at what a page says.
- Use `detect_source_type` first only when you genuinely cannot tell what a URL is and it matters.
- Set `crawl: true` only when the user asks about a whole site or multiple pages, and keep `max_pages` modest (10–25) unless told otherwise.
- Use `js: true` only if a normal fetch came back empty or obviously JS-rendered.
- Use `save_docs` when the user asks to save, write, or export docs to files.

Answer formatting — this matters, the UI renders your reply as Markdown:
- ALWAYS reply in well-formed Markdown. Never wrap your whole answer in a code fence.
- Use `##` / `###` headings, bullet lists, and tables to organise information.
- Put code in fenced blocks with a language tag.
- Link to sources inline with real URLs.
- When you summarise fetched docs, be faithful to them and say so if something was truncated or failed to load.
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
# Groq
# ─────────────────────────────────────────────────────────────
def groq_client():
    from groq import Groq

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Put it in .env at the project root, e.g.\n"
            "  GROQ_API_KEY=gsk_..."
        )
    return Groq(api_key=key)


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)


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


# Every extracted doc opens with `<!-- source: URL | type: KIND | scraped: … -->`,
# so the source type the detector picked is already in the tool result.
_KIND_RE = re.compile(r"<!--\s*source:[^|]*\|\s*type:\s*([a-z0-9_.\-]+)", re.I)


def _kind_of(result: str) -> str:
    match = _KIND_RE.search(result or "")
    if not match:
        return ""
    kind = match.group(1).lower()
    if kind.startswith("github"):
        return "github"
    if kind.startswith("llms"):
        return "llms"
    return {"raw": "raw"}.get(kind, kind)


def _accumulate_tool_calls(delta, sink: dict[int, dict]) -> None:
    """Tool calls arrive split across streaming chunks; stitch them by index."""
    for tc in getattr(delta, "tool_calls", None) or []:
        slot = sink.setdefault(tc.index, {"id": "", "name": "", "args": ""})
        if getattr(tc, "id", None):
            slot["id"] = tc.id
        fn = getattr(tc, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] = fn.name
            if getattr(fn, "arguments", None):
                slot["args"] += fn.arguments


def chat_stream(history: list[dict]) -> Iterator[str]:
    """Drive Groq with tool calling, emitting SSE as it goes."""
    try:
        client = groq_client()
    except RuntimeError as e:
        yield _sse("error", {"message": str(e)})
        return

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    tools = forge_tools.openai_tools()
    answer_parts: list[str] = []

    try:
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_index == MAX_TOOL_ROUNDS
            completion = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                # On the final round drop tools so the model has to answer.
                tool_choice="none" if last_round else "auto",
                temperature=TEMPERATURE,
                max_completion_tokens=MAX_TOKENS,
                top_p=TOP_P,
                stream=True,
                stop=None,
            )

            content_parts: list[str] = []
            pending: dict[int, dict] = {}

            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    yield _sse("token", {"text": piece})
                _accumulate_tool_calls(delta, pending)

            text = "".join(content_parts)
            if text.strip():
                answer_parts.append(text)

            calls = [pending[i] for i in sorted(pending) if pending[i]["name"]]
            if not calls:
                break

            messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": c["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["args"] or "{}"},
                    }
                    for i, c in enumerate(calls)
                ],
            })

            for i, call in enumerate(calls):
                try:
                    args = json.loads(call["args"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments were not a JSON object")
                except (json.JSONDecodeError, ValueError) as e:
                    result = f"Error: could not parse arguments for {call['name']}: {e}"
                    args = {}
                    yield _sse("tool", {"phase": "start", "name": call["name"], "args": {}})
                else:
                    yield _sse("tool", {"phase": "start", "name": call["name"], "args": args})
                    result = forge_tools.run_tool(call["name"], args)

                ok = not result.startswith("Error:")
                yield _sse("tool", {
                    "phase": "end",
                    "name": call["name"],
                    "ok": ok,
                    "chars": len(result),
                    "kind": _kind_of(result),
                    "preview": result[:200],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{i}",
                    "name": call["name"],
                    "content": result,
                })

        markdown = "\n\n".join(p.strip() for p in answer_parts if p.strip())
        if not markdown:
            markdown = "_(no response generated)_"
        yield _sse("done", {"markdown": markdown, "html": render_markdown(markdown)})

    except Exception as e:  # network blips, bad key, model errors
        yield _sse("error", {"message": f"{type(e).__name__}: {e}"})


# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="DocsForge Chat", version="1.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/config")
def config():
    return {
        "model": MODEL,
        "groq_ready": bool(os.environ.get("GROQ_API_KEY")),
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
        chat_stream(history),
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

    if not os.environ.get("GROQ_API_KEY"):
        print("warning: GROQ_API_KEY not found in environment or .env — "
              "the UI will load but chat will error.", file=sys.stderr)

    import uvicorn
    print(f"DocsForge chat → http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run("app:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
