"""
Shared tool layer for DocsForge.

One definition of each tool, consumed by every caller:

  * mcp_server.py — exposes them over MCP (stdio / HTTP) to any MCP client.
  * providers/*   — hands the same schemas to Claude, Groq, OpenAI and Gemini.

Keeping everything on this module means the web chat and an MCP client such as
Claude Code get byte-identical behaviour from the same code path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from docsforge import Doc, ForgeError, Options, detect_source, Fetcher, forge, write_docs

# Cap what we hand back to a model — docs sites can be enormous and blowing the
# context window helps nobody.
MAX_CHARS = int(os.environ.get("DOCSFORGE_MAX_CHARS", "60000"))

# save_docs writes are confined to this root so a model cannot scribble
# anywhere on the filesystem.
OUT_ROOT = Path(os.environ.get("DOCSFORGE_OUT_ROOT", Path.cwd() / "docs_md")).resolve()


# Every extracted doc opens with `<!-- source: URL | type: KIND | scraped: … -->`,
# so the source type the detector picked is already in the tool result. Reading
# it back beats writing a second copy of the detection logic.
# Non-greedy up to the `| type:` delimiter, not `[^|]*`: a source URL may itself
# contain a pipe, and that would end the match on the wrong one.
_KIND_RE = re.compile(r"<!--\s*source:.*?\|\s*type:\s*([a-z0-9_.\-]+)", re.I)


def kind_of(result: str) -> str:
    """Which kind of source a tool result was forged from, or ""."""
    match = _KIND_RE.search(result or "")
    if not match:
        return ""
    kind = match.group(1).lower()
    if kind.startswith("github"):
        return "github"
    if kind.startswith("llms"):
        return "llms"
    return kind


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    keep = text[:limit].rsplit("\n", 1)[0]
    dropped = len(text) - len(keep)
    return f"{keep}\n\n<!-- truncated: {dropped:,} more characters omitted -->"


def _bundle(docs: list[Doc]) -> str:
    if not docs:
        return "No documents extracted."
    if len(docs) == 1:
        return _truncate(docs[0].markdown)
    parts = [f"# Extracted {len(docs)} documents", ""]
    for i, d in enumerate(docs, 1):
        parts.append(f"{i}. [{d.title}]({d.url})")
    parts.append("")
    body = "\n\n---\n\n".join(f"## {d.title}\n<{d.url}>\n\n{d.markdown}" for d in docs)
    return _truncate("\n".join(parts) + "\n" + body)


def _options(crawl=False, max_pages=25, js=False, force=None, delay=0.4) -> Options:
    return Options(
        crawl=bool(crawl),
        max_pages=max(1, min(int(max_pages), 200)),
        js=bool(js),
        delay=float(delay),
        force=force or None,
        verbose=False,
    )


# ─────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────
def tool_detect_source_type(url: str) -> str:
    """Sniff a URL and report which extraction strategy would be used."""
    opts = _options()
    with Fetcher(opts) as f:
        det = detect_source(url, f)
    note = f" (resolved to {det.url})" if det.url != url else ""
    return f"{det.kind}{note}"


def tool_fetch_docs(url: str, crawl: bool = False, max_pages: int = 25,
                    js: bool = False, force: str | None = None) -> str:
    """Extract documentation from a URL and return it as Markdown."""
    docs = forge(url, _options(crawl=crawl, max_pages=max_pages, js=js, force=force))
    return _bundle(docs)


def tool_save_docs(url: str, out_dir: str = "docs_md", crawl: bool = False,
                   max_pages: int = 25, js: bool = False,
                   force: str | None = None, single_file: bool = False) -> str:
    """Extract documentation and write it to disk under the output root."""
    target = (OUT_ROOT / out_dir).resolve() if not Path(out_dir).is_absolute() else Path(out_dir).resolve()
    if OUT_ROOT not in target.parents and target != OUT_ROOT:
        raise ForgeError(f"Refusing to write outside {OUT_ROOT}")

    docs = forge(url, _options(crawl=crawl, max_pages=max_pages, js=js, force=force))
    paths = write_docs(docs, str(target), single_file=single_file, source_url=url)
    listing = "\n".join(f"- `{p}`" for p in paths)
    return f"Wrote {len(paths)} file(s) to `{target}`:\n{listing}"


# ─────────────────────────────────────────────────────────────
# Schemas (JSON Schema, shared by MCP and Groq)
# ─────────────────────────────────────────────────────────────
_URL = {"type": "string", "description": "Absolute http(s) URL of the documentation source."}
_CRAWL = {"type": "boolean", "default": False,
          "description": "Follow same-host links from the start URL. HTML sources only."}
_MAX = {"type": "integer", "default": 25, "minimum": 1, "maximum": 200,
        "description": "Maximum pages to fetch."}
_JS = {"type": "boolean", "default": False,
       "description": "Render JavaScript with Playwright. Slow; only for client-rendered sites."}
_FORCE = {"type": "string",
          "enum": ["llms_txt", "openapi", "sitemap", "github", "raw_text", "html"],
          "description": "Skip auto-detection and force a strategy."}


class Tool:
    def __init__(self, name: str, description: str, schema: dict, fn: Callable[..., str]):
        self.name = name
        self.description = description
        self.schema = schema
        self.fn = fn


TOOLS: list[Tool] = [
    Tool(
        "detect_source_type",
        "Identify what kind of documentation source a URL is (llms_txt, openapi, "
        "sitemap, github, raw_text, or html) without extracting it. Cheap probe — "
        "use it first when you are unsure what a URL points at.",
        {
            "type": "object",
            "properties": {"url": _URL},
            "required": ["url"],
        },
        tool_detect_source_type,
    ),
    Tool(
        "fetch_docs",
        "Extract documentation from any URL and return it as clean Markdown. "
        "Auto-detects the source type: llms.txt, OpenAPI/Swagger specs (rendered as "
        "endpoint tables), sitemap.xml, GitHub repos (README + docs/), raw Markdown, "
        "or a generic HTML docs site (nav/footer stripped). This is the main tool.",
        {
            "type": "object",
            "properties": {
                "url": _URL,
                "crawl": _CRAWL,
                "max_pages": _MAX,
                "js": _JS,
                "force": _FORCE,
            },
            "required": ["url"],
        },
        tool_fetch_docs,
    ),
    Tool(
        "save_docs",
        "Extract documentation from a URL and write it to Markdown files on disk. "
        "Use when the user wants the docs saved rather than shown. Returns the paths written.",
        {
            "type": "object",
            "properties": {
                "url": _URL,
                "out_dir": {"type": "string", "default": "docs_md",
                            "description": "Directory under the output root to write into."},
                "crawl": _CRAWL,
                "max_pages": _MAX,
                "js": _JS,
                "force": _FORCE,
                "single_file": {"type": "boolean", "default": False,
                                "description": "Concatenate everything into one .md file."},
            },
            "required": ["url"],
        },
        tool_save_docs,
    ),
]

BY_NAME = {t.name: t for t in TOOLS}


def openai_tools() -> list[dict]:
    """Tool schemas in the OpenAI/Groq `tools=[...]` format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
            },
        }
        for t in TOOLS
    ]


def run_tool(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call. Errors come back as text so a model can recover
    from them rather than the whole turn dying."""
    tool = BY_NAME.get(name)
    if tool is None:
        return f"Error: unknown tool {name!r}. Available: {', '.join(BY_NAME)}"
    try:
        allowed = set((tool.schema.get("properties") or {}).keys())
        kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed}
        return tool.fn(**kwargs)
    except ForgeError as e:
        return f"Error: {e}"
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # a scrape can fail in a hundred ways
        return f"Error: {type(e).__name__}: {e}"
