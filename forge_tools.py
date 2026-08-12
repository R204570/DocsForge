"""
Shared tool layer for DocsForge.

One definition of each tool, consumed by every caller:

  * mcp_server.py — exposes them over MCP (stdio / HTTP) to any MCP client.
  * providers/*   — hands the same schemas to Claude, Groq, OpenAI and Gemini.

Keeping everything on this module means the web chat and an MCP client such as
Claude Code get byte-identical behaviour from the same code path.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from docsforge import (
    Doc, Fetcher, ForgeError, Options, combine, detect_source, forge, harvest, write_docs,
)

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


# ─────────────────────────────────────────────────────────────
# Knowledge base
# ─────────────────────────────────────────────────────────────
# A harvested technology is written here once and read back afterwards. The
# point of the whole tool is that a model that does not know a stack can be
# handed the stack; re-scraping a docs site on every question defeats that.
KB_ROOT = Path(os.environ.get("DOCSFORGE_KB_ROOT", Path.cwd() / "knowledge_base")).resolve()
KB_INDEX = KB_ROOT / "index.json"


def _kb_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-")
    return slug[:64] or "untitled"


def _kb_load() -> dict:
    if not KB_INDEX.exists():
        return {}
    try:
        return json.loads(KB_INDEX.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _kb_save(index: dict) -> None:
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    KB_INDEX.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")


def _name_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "docs").lower()
    for strip in ("www.", "docs."):
        if host.startswith(strip):
            host = host[len(strip):]
    return host.split(".")[0] or "docs"


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
def tool_harvest_docs(url: str, name: str | None = None, max_pages: int = 200,
                      js: bool = False, scope: str = "section") -> str:
    """Harvest a WHOLE documentation set and store it in the knowledge base."""
    opts = _options(crawl=True, max_pages=max_pages, js=js, delay=0.2)
    opts.scope = scope or "section"

    started = time.time()
    stats: dict = {}
    docs, strategy = harvest(url, opts, stats=stats)
    if not docs:
        raise ForgeError(f"Harvested nothing from {url}")

    slug = _kb_slug(name or _name_from_url(url))
    KB_ROOT.mkdir(parents=True, exist_ok=True)
    path = KB_ROOT / f"{slug}.md"
    body = combine(docs, url, strategy)
    path.write_text(body, encoding="utf-8")

    index = _kb_load()
    truncated = bool(stats.get("truncated"))
    index[slug] = {
        "name": slug,
        "source": url,
        "strategy": strategy,
        "pages": len(docs),
        "characters": len(body),
        "file": str(path),
        "harvested": time.strftime("%Y-%m-%d %H:%M"),
        "complete": not truncated,
        "titles": [d.title for d in docs][:1000],
    }
    _kb_save(index)

    listing = "\n".join(f"{i}. {d.title}" for i, d in enumerate(docs[:30], 1))
    more = f"\n… and {len(docs) - 30} more" if len(docs) > 30 else ""

    warning = ""
    if truncated:
        left = stats.get("remaining") or 0
        warning = (
            f"\n\n**INCOMPLETE — stopped at the {max_pages}-page limit"
            f"{f', {left}+ pages still queued' if left else ''}.** "
            f"This is a partial copy of the documentation. Say so if you answer from it, "
            f"and re-run with a higher `max_pages` to finish the job."
        )

    return (
        f"Harvested **{slug}** — {len(docs)} pages, {len(body):,} characters, "
        f"via {strategy}, in {time.time() - started:.0f}s.\n"
        f"Stored at `{path}`.{warning}\n\n"
        f"Read it back with `read_knowledge_base(name=\"{slug}\")` — do NOT re-harvest "
        f"to answer questions about it.\n\n"
        f"Pages:\n{listing}{more}"
    )


def tool_list_knowledge_base() -> str:
    """What technologies have already been harvested."""
    index = _kb_load()
    if not index:
        return ("The knowledge base is empty. Harvest a technology first with "
                "`harvest_docs(url=...)`.")
    lines = [f"{len(index)} technolog{'y' if len(index) == 1 else 'ies'} stored in {KB_ROOT}:", ""]
    for entry in sorted(index.values(), key=lambda e: e["name"]):
        flag = "" if entry.get("complete", True) else "  **[INCOMPLETE — hit the page limit]**"
        lines.append(
            f"- **{entry['name']}** — {entry['pages']} pages, "
            f"{entry['characters']:,} chars, from {entry['source']} "
            f"({entry['harvested']}, via {entry['strategy']}){flag}"
        )
    return "\n".join(lines)


def tool_read_knowledge_base(name: str, section: str | None = None) -> str:
    """Read stored documentation back, optionally only the matching sections."""
    index = _kb_load()
    slug = _kb_slug(name)
    entry = index.get(slug)
    if entry is None:
        known = ", ".join(sorted(index)) or "(nothing stored yet)"
        raise ForgeError(f"No stored documentation called {slug!r}. Available: {known}")

    path = Path(entry["file"])
    if not path.exists():
        raise ForgeError(f"{slug} is in the index but its file is missing: {path}")
    body = path.read_text(encoding="utf-8")

    if not section:
        if len(body) > MAX_CHARS:
            return _truncate(
                f"<!-- {slug} is {len(body):,} characters; showing the first "
                f"{MAX_CHARS:,}. Pass `section` to get the relevant pages instead. -->\n\n"
                + body
            )
        return body

    # Titles first: a page whose heading matches is what was asked for. Only if
    # nothing matches by title is the body searched, because a manual this size
    # mentions "error" on nearly every page.
    needle = section.lower()
    blocks = re.split(r"\n(?=## )", body)
    titled = [b for b in blocks if needle in b.split("\n", 1)[0].lower()]
    how = "title"

    if not titled:
        titled = [b for b in blocks if needle in b.lower()]
        how = "content"

    if not titled:
        titles = ", ".join(entry.get("titles", [])[:40])
        raise ForgeError(
            f"Nothing in {slug} matches {section!r}, in page titles or text. "
            f"Pages include: {titles}"
        )

    found = len(titled)
    header = (
        f"# {slug}: {found} page{'s' if found != 1 else ''} matching {section!r} "
        f"(by {how})\n"
    )
    if not entry.get("complete", True):
        header += (
            "\n> This copy is INCOMPLETE — the harvest hit its page limit. "
            "Say so if the answer depends on it.\n"
        )
    return _truncate(header + "\n" + "\n\n".join(titled))


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
    Tool(
        "harvest_docs",
        "Learn a WHOLE technology from one starting URL. Use this whenever the user "
        "wants all of something's documentation, or asks about a library or framework "
        "you do not already know well. Give it any page of the docs and it finds the "
        "rest — via llms.txt, the sitemap, or a crawl scoped to that docs section — "
        "then stores everything as one Markdown file in the knowledge base. "
        "Prefer this over repeated fetch_docs calls: it is the tool that turns an "
        "unknown stack into something you can actually answer questions about. "
        "It returns a summary, not the documentation; read it back with "
        "read_knowledge_base.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "Any page of the documentation — usually the "
                                       "introduction or getting-started page."},
                "name": {"type": "string",
                         "description": "What to file it under, e.g. \"effect\". "
                                        "Defaults to the site's domain."},
                "max_pages": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000,
                              "description": "Upper bound on pages to fetch."},
                "js": _JS,
                "scope": {"type": "string", "default": "section",
                          "description": "\"section\" stays inside the docs root the URL "
                                         "sits in (right for almost every site), \"host\" "
                                         "allows the whole domain, or give a literal path "
                                         "prefix such as \"/docs/v3/\"."},
            },
            "required": ["url"],
        },
        tool_harvest_docs,
    ),
    Tool(
        "list_knowledge_base",
        "List the technologies already harvested and stored locally. Check this FIRST "
        "when asked about a library or framework — if it is already stored, read it "
        "instead of fetching anything.",
        {"type": "object", "properties": {}},
        tool_list_knowledge_base,
    ),
    Tool(
        "read_knowledge_base",
        "Read stored documentation back out of the knowledge base. Pass `section` to get "
        "only the pages whose title matches a phrase (for example \"error handling\"), "
        "which is how you answer a specific question without pulling a whole manual "
        "into context.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The stored name, as shown by list_knowledge_base."},
                "section": {"type": "string",
                            "description": "Optional phrase to match against page titles."},
            },
            "required": ["name"],
        },
        tool_read_knowledge_base,
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
