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
# A harvested technology is stored once and read back afterwards. The point of
# the whole tool is that a model which does not know a stack can be handed the
# stack; re-scraping a docs site on every question defeats that.
#
# Where it goes lives in kb_store: Postgres when DOCSFORGE_DB is set and
# reachable, a Markdown file per technology otherwise. Nothing here needs to
# know which.
from kb_store import (  # noqa: E402
    StoreError, build_store, name_from_url as _name_from_url, parse_page,
    slugify as _kb_slug, split_pages, version_from_url as _version_from_url,
)

_STORE = None


def store():
    """The active knowledge-base backend, built once."""
    global _STORE
    if _STORE is None:
        _STORE = build_store()
    return _STORE


def reset_store(new=None):
    """Swap the backend — used by tests and by anything that changes config."""
    global _STORE
    _STORE = new
    return _STORE


# A single fetch_docs call should not be able to start an open-ended crawl by
# accident, so it keeps a ceiling. A harvest is explicitly asking for the whole
# manual, and any page count there is a guess at how big someone else's
# documentation is — the scope prefix is the boundary that actually means
# something, so harvests are unlimited unless you ask for a limit.
FETCH_PAGE_CAP = 200
HARVEST_PAGE_CAP = 0  # 0 = no limit


def _options(crawl=False, max_pages=25, js=False, force=None, delay=0.4,
             cap: int = FETCH_PAGE_CAP) -> Options:
    requested = max(0, int(max_pages))
    if cap:
        pages = min(requested, cap) if requested else cap
    else:
        pages = requested  # 0 stays 0: unlimited
    return Options(
        crawl=bool(crawl),
        max_pages=pages,
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
def _version_label(url: str, docs: list[Doc]) -> str:
    """What to call this harvest, checked against what it actually collected.

    A start URL like `/docs/validation/2.11/get-started/` names a version, but
    the harvest may not have honoured it: `llms.txt` and a site-wide sitemap
    are published once for the whole site, so filing their output under "2.11"
    would claim a precision the content does not have. Trust the URL's label
    only when the pages that came back live under it.
    """
    label = _version_from_url(url)
    if label == time.strftime("%Y-%m-%d"):
        return label  # nothing to check: the URL named no version

    segment = f"/{label}/"
    carried = sum(1 for d in docs if segment in (d.url or "").lower())
    if carried * 2 >= len(docs):
        return label
    return time.strftime("%Y-%m-%d")


def tool_harvest_docs(url: str, name: str | None = None, max_pages: int = 0,
                      js: bool = False, scope: str = "section",
                      version: str | None = None) -> str:
    """Harvest a WHOLE documentation set and store it in the knowledge base."""
    opts = _options(crawl=True, max_pages=max_pages, js=js, delay=0.2,
                    cap=HARVEST_PAGE_CAP)
    opts.scope = scope or "section"

    started = time.time()
    stats: dict = {}
    docs, strategy = harvest(url, opts, stats=stats)
    if not docs:
        raise ForgeError(f"Harvested nothing from {url}")

    slug = _kb_slug(name or _name_from_url(url))
    # v3 and v2 of the same library contradict each other, so they are stored
    # side by side rather than one overwriting the other.
    label = _kb_slug(version) if version else _version_label(url, docs)
    truncated = bool(stats.get("truncated"))

    # Strip the per-page provenance comment: it is redundant once the page is
    # filed under its own title and URL.
    pages = [
        (d.title, d.url, re.sub(r"^<!-- source:.*?-->\n+", "", d.markdown, count=1, flags=re.S))
        for d in docs
    ]
    entry = store().save(slug, label, url, strategy, pages, complete=not truncated)

    listing = "\n".join(f"{i}. {d.title}" for i, d in enumerate(docs[:30], 1))
    more = f"\n… and {len(docs) - 30} more" if len(docs) > 30 else ""

    warning = ""
    if truncated:
        left = stats.get("remaining") or 0
        warning = (
            f"\n\n**INCOMPLETE — stopped at the {opts.max_pages}-page limit"
            f"{f', {left}+ pages still queued' if left else ''}.** "
            f"This is a partial copy of the documentation. Say so if you answer from it, "
            f"and re-run with a higher `max_pages` to finish the job."
        )

    where = entry["file"]
    return (
        f"Harvested **{slug}** {label} — {len(docs)} pages, "
        f"{entry['characters']:,} characters, "
        f"via {strategy}, in {time.time() - started:.0f}s.\n"
        f"Stored in {store().kind}: `{where}`.{warning}\n\n"
        f"Read it back with `read_knowledge_base(name=\"{slug}\", version=\"{label}\")` "
        f"— do NOT re-harvest to answer questions about it.\n\n"
        f"Pages:\n{listing}{more}"
    )


def tool_list_knowledge_base() -> str:
    """What technologies have already been harvested."""
    backend = store()
    try:
        techs, _ = backend.technologies()
    except StoreError as e:
        raise ForgeError(str(e)) from e

    if not techs:
        return ("The knowledge base is empty. Harvest a technology first with "
                "`harvest_docs(url=...)`.")

    lines = [f"{len(techs)} technolog{'y' if len(techs) == 1 else 'ies'} "
             f"stored in {backend.kind} ({backend.location}):", ""]
    for tech in techs:
        flag = "" if tech.get("complete", True) else "  **[INCOMPLETE — hit the page limit]**"
        try:
            versions = backend.versions(tech["name"])
        except StoreError:
            versions = []
        labels = ", ".join(
            f"{v['version']} ({v['pages']} pages, {v['harvested']})" for v in versions
        )
        lines.append(
            f"- **{tech['name']}** — {tech['pages']} pages across "
            f"{tech['versions']} version{'s' if tech['versions'] != 1 else ''}, "
            f"{tech['characters']:,} chars{flag}"
        )
        if labels:
            lines.append(f"    versions: {labels}")
    lines += ["", "Pass `version=` to read_knowledge_base to pick one; "
                  "it defaults to the most recently harvested."]
    return "\n".join(lines)


def tool_read_knowledge_base(name: str, section: str | None = None,
                             version: str | None = None) -> str:
    """Read stored documentation back, optionally only the matching sections."""
    slug = _kb_slug(name)
    backend = store()
    try:
        entry = backend.entry(slug, version)
        if entry is None:
            if version:
                try:
                    have = ", ".join(v["version"] for v in backend.versions(slug))
                    raise ForgeError(
                        f"{slug} has no version {version!r}. Stored versions: {have}")
                except StoreError:
                    pass
            stored, _ = backend.technologies()
            known = ", ".join(t["name"] for t in stored) or "(nothing stored yet)"
            raise ForgeError(f"No stored documentation called {slug!r}. Available: {known}")
        body, how, found = backend.read(slug, section, version)
    except StoreError as e:
        if section:
            # Naming real pages lets a model retry with something that exists,
            # instead of guessing at another phrase.
            titles = ", ".join(backend.titles(slug, version)[:40])
            if titles:
                raise ForgeError(
                    f"Nothing in {slug} matches {section!r}, in page titles or text. "
                    f"Pages include: {titles}"
                ) from e
        raise ForgeError(str(e)) from e

    label = entry.get("version", "")
    if how == "all":
        if len(body) > MAX_CHARS:
            return _truncate(
                f"<!-- {slug} {label} is {len(body):,} characters; showing the first "
                f"{MAX_CHARS:,}. Pass `section` to get the relevant pages instead. -->\n\n"
                + body
            )
        return body

    header = (
        f"# {slug} {label}: {found} page{'s' if found != 1 else ''} "
        f"matching {section!r} (by {how})\n"
    )
    if not entry.get("complete", True):
        header += (
            "\n> This copy is INCOMPLETE — the harvest hit its page limit. "
            "Say so if the answer depends on it.\n"
        )
    return _truncate(header + "\n" + body)


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
                "max_pages": {
                    "type": "integer", "default": 0, "minimum": 0,
                    "description": "0 (the default) crawls the whole documentation "
                                   "section with no page limit. Set a number only to "
                                   "deliberately cut a harvest short.",
                },
                "js": _JS,
                "scope": {"type": "string", "default": "section",
                          "description": "\"section\" stays inside the docs root the URL "
                                         "sits in (right for almost every site), \"host\" "
                                         "allows the whole domain, or give a literal path "
                                         "prefix such as \"/docs/v3/\"."},
                "version": {"type": "string",
                            "description": "Which version of the docs this is, e.g. \"v3\". "
                                           "Detected from the URL when omitted. Harvesting a "
                                           "version you already hold replaces just that one; "
                                           "other versions are kept."},
            },
            "required": ["url"],
        },
        tool_harvest_docs,
    ),
    Tool(
        "list_knowledge_base",
        "List the technologies already harvested and stored locally, with every "
        "version stored for each. Check this FIRST when asked about a library or "
        "framework — if it is already stored, read it instead of fetching anything.",
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
                "version": {"type": "string",
                            "description": "Which stored version to read, e.g. \"v3\". "
                                           "Defaults to the most recently harvested one."},
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
