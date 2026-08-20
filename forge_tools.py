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


def _coverage_flag(complete: bool | None) -> str:
    """The one-line marker beside a technology in a listing."""
    if complete is False:
        return "  **[INCOMPLETE]**"
    if complete is None:
        return "  **[COVERAGE UNKNOWN]**"
    return ""


def _coverage_note(complete: bool | None, expected=None, stored=None) -> str:
    """The warning a model reading this documentation needs to see.

    Three states, three different things to say. The distinction matters: a
    model told nothing assumes it has everything, and then answers a question
    about a page that was never harvested by inventing one.
    """
    if complete is False:
        extent = (f" — {stored} of {expected} pages"
                  if expected and stored and expected > stored else "")
        return (f"\n> This copy is INCOMPLETE{extent}. Say so if the answer "
                f"depends on it, and do not treat a missing topic as absent "
                f"from the real documentation.\n")
    if complete is None:
        return ("\n> COVERAGE UNKNOWN — nothing established how much "
                "documentation exists here, so this copy cannot be called "
                "complete. Treat gaps as possible.\n")
    return ""


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
from resolver import normalise as _normalise, resolve as _resolve  # noqa: E402

_STORE = None
_RETRY_AT = 0.0

#: How long to wait before testing an unreachable database again. Long enough
#: not to stall every request on a dead socket, short enough that a database
#: which was merely slow to start is picked up while you are still looking.
RETRY_AFTER = 15.0


def store():
    """The active knowledge-base backend.

    A database that is down at startup must not downgrade the process for its
    whole lifetime: on Windows the Postgres service routinely finishes starting
    after the app does, and caching that first failed connection made every
    harvest ever taken look like it had vanished. So a fallback is retried.
    """
    global _STORE, _RETRY_AT
    if _STORE is None:
        _STORE = build_store()
        _RETRY_AT = time.time() + RETRY_AFTER
        return _STORE

    if getattr(_STORE, "wanted_dsn", "") and time.time() >= _RETRY_AT:
        _RETRY_AT = time.time() + RETRY_AFTER
        rebuilt = build_store()
        if rebuilt.kind == "postgres":
            _STORE = rebuilt
    return _STORE


def reset_store(new=None):
    """Swap the backend — used by tests and by anything that changes config."""
    global _STORE, _RETRY_AT
    _STORE = new
    _RETRY_AT = 0.0
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
    # Completeness is measured, not assumed. `None` means the harvest never
    # established how much there was to get — which is not the same claim as
    # "this is the whole thing", and must not be reported as one.
    whole = False if truncated else stats.get("whole")
    expected = stats.get("discovered")

    # Strip the per-page provenance comment: it is redundant once the page is
    # filed under its own title and URL.
    pages = [
        (d.title, d.url, re.sub(r"^<!-- source:.*?-->\n+", "", d.markdown, count=1, flags=re.S))
        for d in docs
    ]
    entry = store().save(slug, label, url, strategy, pages,
                         complete=whole, expected=expected)

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
    elif whole is False:
        warning = (
            f"\n\n**INCOMPLETE — {stats.get('reason', 'this is a partial copy')}.** "
            f"Say so if you answer from it, and prefer a direct URL to the full "
            f"documentation if you can find one."
        )
    elif whole is None:
        warning = (
            "\n\n**COVERAGE UNKNOWN — nothing established how much documentation "
            "exists here, so this copy cannot be called complete.** Treat gaps as "
            "possible rather than assuming anything missing does not exist."
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
        return ("Nothing is stored yet. Learn a technology with "
                "`learn_technology(name=\"...\")` — you do not need a URL.")

    lines = [f"{len(techs)} technolog{'y' if len(techs) == 1 else 'ies'} "
             f"stored in {backend.kind} ({backend.location}):", ""]
    for tech in techs:
        flag = _coverage_flag(tech.get("complete", True))
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
                  "it defaults to the newest version stored."]
    return "\n".join(lines)


def stored_name(name: str) -> str | None:
    """Match a caller's spelling of a technology against what is stored.

    A model reading `import { Effect } from "effect"` may ask for "Effect.ts";
    the store has "effect". Requiring the exact slug makes the caller guess our
    filing convention, which it has no way to know. Exact match wins, then a
    normalised match, then a unique prefix — anything ambiguous is refused
    rather than guessed.
    """
    backend = store()
    try:
        techs, _ = backend.technologies()
    except StoreError:
        return None
    names = [t["name"] for t in techs]
    if not names:
        return None

    if name in names:
        return name

    wanted = _normalise(name)
    exact = [n for n in names if _normalise(n) == wanted]
    if len(exact) == 1:
        return exact[0]

    if len(wanted) >= 3:
        near = [n for n in names if _normalise(n).startswith(wanted)]
        if len(near) == 1:
            return near[0]
    return None


def tool_read_knowledge_base(name: str, section: str | None = None,
                             version: str | None = None) -> str:
    """Read stored documentation back, optionally only the matching sections."""
    slug = stored_name(name) or _kb_slug(name)
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
    note = _coverage_note(entry.get("complete", True), entry.get("expected"),
                          entry.get("pages"))
    if note:
        header += note
    return _truncate(header + "\n" + body)


# ─────────────────────────────────────────────────────────────
# Answering by name instead of by URL
# ─────────────────────────────────────────────────────────────
def tool_find_docs(name: str, ecosystem: str | None = None) -> str:
    """Work out where a technology documents itself. Fetches nothing else."""
    found = _resolve(name, ecosystem=(ecosystem or "").strip())

    lines = [f"Resolving **{name}**"
             + (f" ({found.ecosystem})" if found.ecosystem else "") + ":", ""]
    if not found.candidates:
        lines.append(found.note or f"Nothing found for {name!r}.")
        return "\n".join(lines)

    for cand in found.candidates:
        mark = {True: "verified", False: "unverified", None: "not checked"}[cand.verified]
        lines.append(f"- {cand.url}")
        lines.append(f"    {mark} · confidence {cand.confidence:.2f} · {cand.evidence}")
        if cand.reason:
            lines.append(f"    {cand.reason}")

    lines.append("")
    if found.best:
        lines.append(
            f"Best: {found.best.url} — harvest it with "
            f"`learn_technology(name=\"{name}\")`, or "
            f"`harvest_docs(url=\"{found.best.url}\", name=\"{_kb_slug(name)}\")`."
        )
    else:
        lines.append(found.note)
    return "\n".join(lines)


def tool_learn_technology(name: str, version: str | None = None,
                          ecosystem: str | None = None, max_pages: int = 0,
                          js: bool = False) -> str:
    """Learn a technology from its name alone: resolve, verify, harvest, store."""
    # File under the canonical form, not the caller's spelling. Otherwise
    # "Effect.ts" and "effect" become two copies of the same library, and the
    # second harvest silently re-crawls a site already stored under the first.
    slug = _kb_slug(_normalise(name) or name)

    # Already known? Re-crawling a site to answer a question you can already
    # answer is the most expensive way to be unhelpful.
    known = stored_name(name)
    if known:
        backend = store()
        entry = backend.entry(known, version)
        if entry is not None:
            return (
                f"**{known}** {entry['version']} is already stored — "
                f"{entry['pages']} pages, harvested {entry['harvested']}.\n\n"
                f"Read it with `read_knowledge_base(name=\"{known}\", "
                f"version=\"{entry['version']}\")`. Nothing was fetched."
            )
        try:
            have = ", ".join(v["version"] for v in backend.versions(known))
            note = (f"**{known}** is stored, but not version {version!r} "
                    f"(have: {have}). Harvesting it now.\n\n")
        except StoreError:
            note = ""
    else:
        note = ""

    found = _resolve(name, ecosystem=(ecosystem or "").strip())
    if found.best is None:
        listed = "\n".join(f"- {c.url} ({c.reason or 'unverified'})"
                           for c in found.candidates)
        raise ForgeError(
            (found.note or f"Could not find documentation for {name!r}.")
            + (f"\n\nCandidates considered:\n{listed}" if listed else "")
            + "\n\nIf you know the URL, call harvest_docs with it directly."
        )

    harvested = tool_harvest_docs(url=found.best.url, name=slug,
                                  max_pages=max_pages, js=js, version=version)
    return (
        f"{note}Resolved **{name}** to {found.best.url}\n"
        f"({found.best.evidence}; {found.best.reason})\n\n{harvested}"
    )


def tool_search_knowledge_base(query: str, technology: str | None = None,
                               version: str | None = None, limit: int = 20) -> str:
    """Search the text of every stored page, across all technologies."""
    backend = store()
    tech = stored_name(technology) if technology else None
    if technology and not tech:
        raise ForgeError(f"Nothing stored under {technology!r}. "
                         f"Call list_knowledge_base to see what is available.")
    try:
        hits = backend.search(query, tech, version, max(1, min(int(limit), 100)))
    except StoreError as e:
        raise ForgeError(str(e)) from e

    if not hits:
        where = f" in {tech}" if tech else ""
        return (f"Nothing stored{where} matches {query!r}. "
                f"The technology may not be harvested yet — try "
                f"`learn_technology(name=...)`.")

    ranked = "ranked" if backend.kind == "postgres" else "unranked (file store)"
    lines = [f"{len(hits)} {ranked} match(es) for {query!r}:", ""]
    for hit in hits:
        snippet = hit["snippet"].replace("«", "**").replace("»", "**")
        lines.append(f"- **{hit['technology']}** {hit['version']} · page "
                     f"{hit['ordinal']}: {hit['title']}")
        lines.append(f"    {' '.join(snippet.split())}")
    lines += ["", "Read a whole page with "
                  "`read_knowledge_base(name=..., version=..., section=<title>)`."]
    return "\n".join(lines)


def tool_scan_project(path: str | None = None, unknown_only: bool = False) -> str:
    """List a project's dependencies and say which are already documented here."""
    from pathlib import Path as _Path

    import manifests

    root = _Path(path or ".").expanduser().resolve()
    if not root.is_dir():
        raise ForgeError(f"Not a directory: {root}")

    deps = manifests.read_project(root)
    if not deps:
        raise ForgeError(
            f"No dependency manifests under {root}. Looked for: "
            + ", ".join(sorted(manifests.MANIFESTS))
        )

    rows, missing = [], []
    for dep in sorted(deps, key=lambda d: d.name.lower()):
        known = stored_name(dep.name)
        pinned = manifests.pinned_version(dep.version)
        if not known:
            missing.append(dep)
        if unknown_only and known:
            continue
        rows.append(f"- `{dep.name}`{' ' + pinned if pinned else ''} "
                    f"({dep.ecosystem}, {dep.manifest}) — "
                    + (f"stored as **{known}**" if known else "not stored"))

    shown = "not yet documented" if unknown_only else "declared"
    lines = [f"{len(rows)} of {len(deps)} dependenc"
             f"{'y' if len(deps) == 1 else 'ies'} {shown} in `{root}`:", ""] + rows

    if missing:
        first = missing[0]
        want = manifests.doc_versions(first.version)
        lines += ["", f"{len(missing)} not yet documented. Learn one with "
                      f"`learn_technology(name=\"{first.name}\""
                      + (f", version=\"{want[0]}\"" if want else "")
                      + f", ecosystem=\"{first.ecosystem}\")`."]
    else:
        lines += ["", "Every dependency is already documented in the knowledge base."]
    return "\n".join(lines)


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
        "learn_technology",
        "Learn a technology from its NAME alone, with no URL. Use this the moment "
        "you meet a library, framework or tool you do not already know well — from "
        "an import, a config file, an error message, anything. It finds the official "
        "documentation via the package registries, confirms the page really does "
        "document that package, harvests the whole thing and stores it. "
        "Prefer this over guessing a documentation URL yourself: a guessed URL comes "
        "from the same training data that did not know the library, and a wrong guess "
        "silently stores the wrong project. If it is already stored, it says so and "
        "fetches nothing.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The package or technology name, as you saw it "
                                        "written, e.g. \"effect\", \"pydantic\", "
                                        "\"@tanstack/react-query\"."},
                "version": {"type": "string",
                            "description": "Which version's documentation you need, e.g. "
                                           "\"1.10\". Take it from the project's lockfile "
                                           "or manifest when you can — versions of the "
                                           "same library contradict each other."},
                "ecosystem": {"type": "string", "enum": ["npm", "pypi", "crates"],
                              "description": "Which registry to trust. Omit to try all."},
                "max_pages": {"type": "integer", "default": 0, "minimum": 0,
                              "description": "0 (default) harvests the whole documentation."},
                "js": _JS,
            },
            "required": ["name"],
        },
        tool_learn_technology,
    ),
    Tool(
        "find_docs",
        "Work out where a technology documents itself, WITHOUT harvesting anything. "
        "Returns candidate URLs with evidence and whether each was confirmed to "
        "actually document that package. Use it when you want to check what would "
        "be harvested first, or when learn_technology could not resolve a name and "
        "you want to see what it considered.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The package or technology name."},
                "ecosystem": {"type": "string", "enum": ["npm", "pypi", "crates"],
                              "description": "Which registry to trust. Omit to try all."},
            },
            "required": ["name"],
        },
        tool_find_docs,
    ),
    Tool(
        "search_knowledge_base",
        "Search the full text of every stored page, across all technologies at once. "
        "Use this when you have a symbol, error message or snippet but do not know "
        "which library it belongs to — read_knowledge_base needs you to already know "
        "the name, and this does not.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Words to search for, e.g. \"exponential backoff\"."},
                "technology": {"type": "string",
                               "description": "Optional: restrict to one stored technology."},
                "version": {"type": "string",
                            "description": "Optional: restrict to one version of it."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
        tool_search_knowledge_base,
    ),
    Tool(
        "scan_project",
        "Read a project's dependency manifests (package.json, pyproject.toml, "
        "requirements.txt, Cargo.toml, go.mod) and list what it depends on, at which "
        "versions, and which of those are already documented in the knowledge base. "
        "This is the best way to find out what a codebase actually uses before "
        "answering questions about it — and the manifest is the only place the "
        "correct VERSION of each library can be read from.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Project root. Defaults to the working directory."},
                "unknown_only": {"type": "boolean", "default": False,
                                 "description": "List only dependencies not yet stored."},
            },
        },
        tool_scan_project,
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
                                           "Defaults to the newest version stored — the highest release number, not the most recent download."},
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
