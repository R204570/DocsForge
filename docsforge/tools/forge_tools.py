"""
Shared tool layer for DocsForge.

One definition of each tool, consumed by every caller:

  * mcp_server.py — exposes them over MCP (stdio / HTTP) to any MCP client.
  * providers/*   — hands the same schemas to Claude, Groq, OpenAI and Gemini.

Keeping everything on this module means the web chat and an MCP client such as
Claude Code get byte-identical behaviour from the same code path.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from docsforge.core.engine import (
    Doc, Fetcher, ForgeError, Options, _html_to_md, _median, combine,
    detect_source, forge, harvest, probe, write_docs,
)
from docsforge.tools import applog
from docsforge.tools import harvest_jobs
from docsforge.core import llmsfinder
from docsforge.core import reasoning
from docsforge.tools import tracing
from docsforge.core import versions


def _load_env() -> None:
    """Read `.env` here, so every surface sees the same configuration.

    `app.py` called `load_dotenv()` and nothing else did. An MCP client
    launches `python main.py` with whatever environment the shell had,
    so `DOCSFORGE_DB` was unset there and `build_store()` returned a
    `FileStore`. Measured 2026-09-10: against a Postgres store holding 23
    technologies, `list_knowledge_base` over MCP answered "Nothing is stored
    yet", and after harvesting five technologies through that same surface
    `read_knowledge_base` could not read one of them back.

    The guard against that could not fire either. `build_store` records
    `degraded` and `wanted_dsn` precisely so a silent fallback is impossible,
    and sets both only when a DSN was present and *unreachable* — never when
    it was simply absent.

    Here rather than in each entry point because this module is what every
    surface imports, and a second copy of this line is a second thing to
    forget. `load_dotenv` does not override a variable already set, so a
    caller that exported its own still wins, and `app.py` calling it first
    changes nothing.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ModuleNotFoundError:      # optional: it ships with the web extra
        return
    load_dotenv(find_dotenv(usecwd=True))


_load_env()

# Cap what we hand back to a model. Not a storage limit and never has been:
# `save_docs` writes through `write_docs`, and the knowledge base stores every
# page whole, so a file on disk and a row in the store are both unbounded. This
# bounds one tool result.
#
# Raised from 60,000, which was cutting real documentation short — a single
# large specification page is bigger than that, and being handed a third of one
# is a poor answer to a question about it.
#
# The number cannot simply be removed, and that is worth being precise about:
# the binding constraint is not DocsForge but the *provider's context window*,
# and this project speaks to six of them. A result that overflows the window
# does not truncate gracefully, it fails the turn — so an unbounded default
# would break Ollama and Groq outright to slightly help Claude. 200,000
# characters is roughly 50-65k tokens, which sits comfortably inside a
# 200k-token window alongside the conversation.
#
# Where a page is larger still, nothing is lost or hidden: the omission is
# stated, the sections that were dropped are named, and `read_knowledge_base`
# takes a `section` so any part of a page can be asked for directly. Callers
# who know their own window can raise this as far as it will go.
#: Named so it can be asserted without depending on whatever the machine
#: running the suite happens to have in its `.env` — one was found setting
#: `DOCSFORGE_MAX_CHARS` twice, so its real cap was neither this default nor
#: the first value in its own config.
DEFAULT_MAX_CHARS = 200_000
MAX_CHARS = int(os.environ.get("DOCSFORGE_MAX_CHARS", DEFAULT_MAX_CHARS))

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
from docsforge.store.kb_store import (  # noqa: E402
    StoreError, build_store, name_from_url as _name_from_url, parse_page,
    slugify as _kb_slug, split_pages, version_from_url as _version_from_url,
)
from docsforge.core.resolver import normalise as _normalise, resolve as _resolve  # noqa: E402

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
             cap: int = FETCH_PAGE_CAP, version: str | None = None) -> Options:
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
        # Which release was asked for has to reach discovery, not just the
        # label: it decides whether a site's published `llms.txt` — always
        # its *current* documentation — can answer the question at all.
        version=(version or "").strip(),
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
def _version_label(url: str, docs: list[Doc], declared: str = "") -> str:
    """What to call this harvest, checked against what it actually collected.

    A start URL like `/docs/validation/2.11/get-started/` names a version, but
    the harvest may not have honoured it: `llms.txt` and a site-wide sitemap
    are published once for the whole site, so filing their output under "2.11"
    would claim a precision the content does not have. Trust the URL's label
    only when the pages that came back live under it.

    `declared` is what the source said about itself — an `llms.txt` header's
    `Version:` line. It is consulted only where the URL named nothing, and
    only when it is a real release number: the date fallback exists to admit
    "we could not establish a version", and replacing it with "latest" or
    "stable" would dress that admission up as an answer without adding one.
    """
    label = _version_from_url(url)
    if label == time.strftime("%Y-%m-%d"):
        # The URL named no version. A file that states its own outranks the
        # day we happened to fetch it -- that is the whole ordering in
        # versions.py, applied one step earlier.
        if declared and versions.kind(declared) == versions.RELEASE:
            return _kb_slug(declared)
        return label

    segment = f"/{label}/"
    carried = sum(1 for d in docs if segment in (d.url or "").lower())
    if carried * 2 >= len(docs):
        return label
    return time.strftime("%Y-%m-%d")


#: How many candidate hosts one harvest will spend a request confirming.
#: Federation is not permission to roam (Invariant 14), and an unbounded fan-out
#: is exactly the roaming the invariant forbids.
MAX_CORPORA = 4


def _identify_host(name: str, url: str) -> tuple[bool, str]:
    """Run the resolver's identity gate against one candidate host.

    Deliberately the same gate everything else uses. It is already written,
    already tested and already trusted, and it generalises to hosts nobody
    anticipated — which sibling-subdomain matching never did.

    Known risk (`ISSUES.md` R1): the gate confirms a page is about something
    *with this name*, not that it is this project. It was measured admitting a
    parked domain. The evidence it accepted is recorded on the corpus so a
    wrong admission can be seen rather than inferred.
    """
    from docsforge.core import resolver

    candidate = resolver.Candidate(url, "federation", 0.5)
    body = ""
    try:
        with Fetcher(Options(delay=0.0)) as fetcher:
            resolver.verify(candidate, name, fetcher, {})
            if candidate.verified:
                try:
                    body = _html_to_md(fetcher.html(url), url)[1]
                except ForgeError:
                    body = ""
    except ForgeError as e:
        return False, f"could not be checked: {e}"

    if not candidate.verified:
        return False, candidate.reason

    # Decision point 3, and the reason this one is worth paying for above all
    # the others. The gate confirms a page is about something *with this name*;
    # it does not confirm it is this project. Measured, it admitted a parked
    # domain, and 9 of 20 resolutions in the Phase C sample were a different
    # project sharing a name. Arithmetic cannot tell those apart — the evidence
    # it counts is identical in both cases — so this is exactly the shape of
    # question a model answers better than a word count.
    #
    # The consultation may only ever *veto*. A host the algorithmic gate
    # refused is not re-admitted by asking again, so reasoning can make the
    # gate stricter and never looser, and R1 cannot be made worse by turning it
    # on. Cached per host, so a 40-page corpus costs one call.
    verdict = _reason_about_identity(name, url, body, candidate.reason)
    if verdict:
        return False, verdict
    return True, candidate.reason


def _reason_about_identity(name: str, url: str, body: str, reason: str) -> str:
    """A veto and its reason, or "" to let the algorithmic gate stand."""
    from docsforge.core import reasoning

    reasoner = reasoning.current()
    if not reasoner.enabled() or not body:
        return ""

    answer = reasoner.ask(
        "identity gate", (urlparse(url).hostname or url),
        f"A documentation harvester is deciding whether this site documents the "
        f"project called {name!r}, or a DIFFERENT project that happens to share "
        f"that name. Judge the project, not the word.\n\n"
        f"Reply with exactly one word - SAME or DIFFERENT - then a short reason "
        f"on the same line.\n\nURL: {url}\n\n{body[:reasoning.MAX_SAMPLE]}",
        fallback="",
        check=lambda a: a.strip().upper().startswith(("SAME", "DIFFERENT")))

    if answer.strip().upper().startswith("DIFFERENT"):
        detail = answer.strip()[9:].strip(" -:") or "a different project of the same name"
        return f"{reason}, but reading it says otherwise: {detail}"
    return ""


def corpus_label(corpus, docs: list, declared: str = "") -> str:
    """What to file one corpus's harvest under.

    PROPOSAL-II §2.3: versions live on the corpus, because a specification is
    per-release while a set of enhancement proposals has none at all. Where
    nothing names a version, `_version_label` falls back to today's date — fine
    for a technology, wrong for a corpus, because a date reads as a claim about
    when the content is from. An undated corpus is filed as undated.
    """
    if corpus.version:
        return _kb_slug(corpus.version)
    label = _version_label(corpus.url, docs, declared)
    return "undated" if label == time.strftime("%Y-%m-%d") else label


def corpus_key(technology: str, corpus) -> str:
    """The store name for one corpus of a technology.

    PROPOSAL-II asks for a `tech/corpus/version` key. The store keys on
    `(technology, version)`, so the corpus rides in the technology half:
    `effect--api-effect-dev`, version unchanged. Isomorphic to the three-part
    key, and it needs no schema migration to a store that already holds
    people's harvests — which is the kind of change worth avoiding until the
    feature that needs it has proved itself. `ISSUES.md` F1 tracks making it a
    first-class column.
    """
    return f"{_kb_slug(technology)}--{corpus.slug}"


def _harvest_corpus(technology: str, corpus, max_pages: int, js: bool) -> str:
    """Fetch one federated corpus, store it, and settle its own count.

    Per its shape. A `page` corpus is one enormous self-referential document —
    a specification — and crawling it would find one page; it is fetched once
    and split on its own headings, which makes `expected` the section count and
    exact. A `tree` or `api` corpus goes through the ordinary harvest, which
    already prefers a generator manifest over a sitemap and so already gets an
    exact count where the site publishes one.
    """
    from docsforge.core import passages as psg

    opts = _options(crawl=True, max_pages=max_pages, js=js, delay=0.2,
                    cap=HARVEST_PAGE_CAP, version=corpus.version)
    stats: dict = {}
    strategy = corpus.shape

    provisional = (_kb_slug(corpus.version) if corpus.version
                   else _version_from_url(corpus.url))
    docs: list = []

    with store().writer(corpus_key(technology, corpus), provisional,
                        corpus.url, strategy) as writer:
        if corpus.shape == "page":
            docs = forge(corpus.url, _options(crawl=False, max_pages=1, js=js))
            if not docs:
                raise ForgeError(f"nothing at {corpus.url}")
            chunks = psg.sections(docs[0].markdown, page_title=docs[0].title,
                                  page_url=corpus.url)
            for i, chunk in enumerate(chunks, 1):
                writer.add(chunk.heading_path or docs[0].title,
                           f"{corpus.url}#section-{i}", chunk.text)
            expected, whole = len(chunks), True
        else:
            docs, strategy = harvest(corpus.url, opts, stats=stats,
                                     sink=_StripSink(writer))
            expected = stats.get("discovered")
            whole = False if stats.get("truncated") else stats.get("whole")

        if not writer.titles:
            raise ForgeError(f"harvested nothing from {corpus.url}")

        label = corpus_label(corpus, docs, stats.get("declared_version", ""))
        entry = writer.settle(complete=whole, expected=expected,
                              version=label, strategy=strategy)
        stored = len(writer.titles)

    # Invariant 8: this corpus settles its own count. Nothing here touches any
    # other corpus's `complete`.
    corpus.settle(stored, expected)
    corpus.version = label
    return entry["file"]


def _corpus_list(value) -> list[str] | None:
    """Accept a comma-separated string or a real list.

    The MCP schema builder maps only scalars, so the tool surface takes a
    string; Python callers pass a list. Normalising here beats two code paths.
    """
    if not value:
        return None
    if isinstance(value, str):
        return [u.strip() for u in value.split(",") if u.strip()] or None
    return [str(u) for u in value] or None


def _reason_about_kind(corpus, probe_result) -> None:
    """Decision point 2: what a corpus is *for*, when the path does not say.

    `classify_kind` reads the URL, which works whenever a project puts `/api/`
    or `/guide/` in its paths and not at all when it does not — and a corpus
    classified `""` matches no kind-specific intent, so it either escalates to
    a human or is quietly left out of scope. Escalating is the correct
    fallback and it stays the fallback; this just means a human is asked less
    often for something a page states plainly in its first paragraph.

    Done here rather than in `selection` because the probe has already fetched
    the page. Asking in `selection` would mean fetching it a second time to
    answer a question the crawl already had the material for.
    """
    from docsforge.core import reasoning
    from docsforge.core import selection as sel

    reasoner = reasoning.current()
    if not reasoner.enabled() or not probe_result.sample:
        return
    if corpus.kind and corpus.kind_confidence >= sel.MIN_KIND_CONFIDENCE:
        return                                  # the path already said so

    kinds = ("api", "sdk", "adk", "spec", "guide", "cookbook", "operations",
             "changelog", "language", "meta")
    answer = reasoner.ask(
        "corpus kind", corpus.url,
        "What kind of documentation is this? Reply with exactly one word from: "
        + ", ".join(kinds) +
        f".\n\nURL: {corpus.url}\n\n{probe_result.sample}",
        fallback="",
        check=lambda a: a.strip().lower().split()[0] in kinds if a.strip() else False)

    if answer:
        corpus.kind = answer.strip().lower().split()[0]
        # Below the threshold that would escalate: a read answer is better than
        # a URL guess and still weaker than a path that says so outright, and
        # claiming certainty here would silence the escalation that exists for
        # exactly this doubt.
        corpus.kind_confidence = max(corpus.kind_confidence,
                                     sel.MIN_KIND_CONFIDENCE)


def _measure_corpora(sites, entry, stats: dict, js: bool = False) -> None:
    """One GET per corpus: enough to know its shape and its rough size.

    Two things that PROPOSAL-II built, tested and then never called depend on
    exactly this measurement, which is why they are wired together rather than
    one at a time. `classify_shape` (W2) had no caller, so every corpus was a
    `tree` and the `page` branch of `_harvest_corpus` was unreachable — that is
    what made the `go.dev/ref/spec` failure possible, and it is `ISSUES.md` S7.
    `Corpus.magnitude` (W3) was never set, so every option in an escalation
    question read "size unknown" and "ordered by magnitude" ordered by zero.

    Shape is relative, so it needs all the corpora before it can classify any
    of them: `page` means "six times the median corpus", and there is no median
    until every corpus has been measured. Hence two passes over one set of
    probes rather than one pass that decides as it goes.

    The entry corpus is measured from the crawl that already happened rather
    than re-fetched. It is also never classified `page` — it has been crawled
    as a tree by the time this runs, and relabelling it would describe the
    harvest that was performed as something it was not.
    """
    from docsforge.core import federation

    probes: dict[str, object] = {}
    for corpus in sites.corpora:
        if corpus.url == entry.url:
            continue
        probes[corpus.url] = probe(corpus.url, opts=_options(js=js, delay=0.2))

    # The entry corpus's own numbers come from the crawl, free.
    fetched = stats.get("fetched") or 0
    entry.magnitude = stats.get("discovered") or fetched or entry.magnitude

    for corpus in sites.corpora:
        p = probes.get(corpus.url)
        if p is None:
            continue
        corpus.magnitude = p.magnitude
        _reason_about_kind(corpus, p)
        if p.failed:
            # Measured nothing, so claim nothing: `tree` is what the unprobed
            # code has always assumed, and it is the safe default because it
            # crawls rather than asserting an exact count.
            continue

        # Against its peers, not against itself. A corpus included in its own
        # median drags that median toward its own size, and with only two or
        # three corpora it dominates it outright — the one enormous document
        # this test exists to find is exactly the one that would be compared
        # against a median it had just defined, and so would never be six
        # times it. The same mistake `_neighbourhood` made counting a URL as
        # its own neighbour.
        peers = sorted(q.chars for u, q in probes.items()
                       if u != corpus.url and q.chars)
        corpus.shape = federation.classify_shape(
            chars=p.chars, anchors=p.anchors, median_chars=_median(peers),
            # An index is an API generator's entry list, not any manifest: a
            # MkDocs guide publishes a page list too, and calling that "api"
            # would file a tutorial under a shape it does not have.
            has_index=(corpus.kind == "api" and p.manifest > 0),
            has_manifest=p.manifest > 0,
        )


def _federate(name: str, harvested_url: str, stats: dict, intent: str = "",
              explicit: list[str] | None = None, strict: bool = False,
              max_pages: int = 0, js: bool = False) -> str:
    """Discover, admit, classify and select the corpora around a harvest.

    Ordering note: selection runs *after* the entry corpus is crawled, not
    before, and that is not a shortcut. Corpora are discovered from link
    evidence gathered while crawling, so there is nothing to select between
    until at least one corpus has been read. What selection governs is which
    *additional* corpora are harvested.
    """
    from docsforge.core import federation
    from docsforge.core import selection as sel

    proposed = (stats or {}).get("corpora") or []
    entry = federation.Corpus(url=harvested_url, entry=True)
    entry.kind, entry.kind_confidence = federation.classify_kind(harvested_url)
    entry.settle(stats.get("fetched") or stats.get("discovered") or 0,
                 stats.get("discovered"))
    entry.complete = stats.get("whole") if stats.get("whole") is not None else entry.complete

    sites = federation.Federation.single(harvested_url, technology=name)
    sites.corpora[0] = entry
    for item in proposed[:MAX_CORPORA]:
        corpus = federation.Corpus(url=item["url"], host=item["host"],
                                   votes=item.get("votes", 0.0))
        corpus.kind, corpus.kind_confidence = federation.classify_kind(corpus.url)
        if sites.admit(corpus, lambda u: _identify_host(name, u)):
            sites.add(corpus)

    _measure_corpora(sites, entry, stats, js=js)

    policy = sel.recall_policy(name) if not explicit else None
    chosen = sel.select(sites.corpora, intent=intent or sel.DEFAULT_INTENT,
                        explicit=explicit, policy=policy)

    lines: list[str] = []
    if chosen.needs_selection:
        # Invariant 10, in the order the invariant states it: ask if there is
        # anyone to ask, and refuse only when there is not. Refusing is the
        # fallback, not the policy.
        answered = sel.ask(chosen)
        if answered:
            chosen = sel.select(sites.corpora,
                                intent=intent or sel.DEFAULT_INTENT,
                                explicit=answered)
            sel.remember_policy(name, answered,
                                known=[c.url for c in sites.corpora])
            lines += ["", "", f"Asked, and taking the {len(answered)} corpus/corpora "
                              f"chosen. The answer is remembered, so this is asked "
                              f"once — `forget_selection` clears it."]
        else:
            # Nobody to ask. A machine-readable refusal carrying the options is
            # strictly better than a silently truncated harvest that looks
            # successful, which is the outcome this whole layer exists to avoid.
            lines += ["", "", "**NEEDS SELECTION — more than one corpus could be "
                             "what you meant, and guessing is not allowed.**", "",
                      chosen.question(),
                      "", "The corpus already harvested above is stored and is "
                          "unaffected. Nothing else was fetched."]
            if strict:
                lines += ["", "`usable_for_planning: false` — selection unresolved."]
            return "\n".join(lines)

    if explicit or chosen.trigger == "":
        # Record what was chosen AND what existed, so a later run can tell a
        # brand-new corpus from one that was offered and turned down.
        if len(sites.corpora) > 1:
            sel.remember_policy(name, [c.url for c in chosen.selected],
                                known=[c.url for c in sites.corpora])

    # Harvest what selection chose. Bounded by MAX_CORPORA, because federation
    # is not permission to roam (Invariant 14) — and each corpus keeps its own
    # count, so a partial one cannot make a complete one look partial.
    for corpus in [c for c in chosen.selected if c.url != harvested_url][:MAX_CORPORA]:
        try:
            where = _harvest_corpus(name, corpus, max_pages, js)
            corpus.status = f"harvested separately -> `{where}`"
        except (ForgeError, StoreError) as e:
            # A corpus that could not be fetched is reported, not dropped, and
            # not counted as covered.
            corpus.status = f"selected, but could not be harvested: {e}"
            corpus.selected = False

    if strict:
        ok, why = sel.usable_for_planning(chosen, sites.corpora)
        lines += ["", "", f"`usable_for_planning: {str(ok).lower()}` — {why}"]

    # Invariant 9's roll-up, recorded where the caller's headline can reach it.
    # `Federation.complete` was implemented and tested under PROPOSAL-II and
    # nothing ever read it (W1).
    stats["federation"] = {"complete": sites.complete,
                           "corpora": len(sites.corpora),
                           "selected": len(sites.selected)}

    note = sites.note(intent=chosen.intent)
    if note:
        lines += ["", "", note]

    # Invariant 18: recorded. A judgement nobody can audit is worse than a
    # heuristic, because a heuristic can at least be read.
    from docsforge.core import reasoning
    spent = reasoning.current()
    if spent.log:
        stats["reasoning"] = spent.record()
        lines += ["", "", spent.note()]
    return "\n".join(lines)


class _StripSink:
    """Hands each page to the store, minus its provenance comment.

    Every extracted document opens with `<!-- source: … -->`, which is
    redundant once the page is filed under its own title and URL. Stripping it
    here rather than in a list comprehension at the end is what lets the body
    be written and released instead of held until the harvest finishes.

    It also keeps each stored page's length, because this is the last place
    that ever sees one. Peak memory is bounded by dropping the bodies —
    `_drain` returns `Doc(url, title, "")` — so measuring the corpus's shape
    afterwards measured a list of empty strings. Every harvest of twenty pages
    or more was told it was "the shape of an API symbol index or a split dump,
    not prose documentation": langchain's 627 pages averaging 14,283
    characters reported "a median of 0 characters and 627 of them under 500".
    An integer per page costs nothing and is the one number that survives.
    """

    def __init__(self, writer):
        self.writer = writer
        self.sizes: list[int] = []

    def add(self, title: str, url: str, body: str) -> bool:
        stripped = re.sub(r"^<!-- source:.*?-->\n+", "", body, count=1, flags=re.S)
        kept = self.writer.add(title, url, stripped)
        if kept:
            self.sizes.append(len(stripped))
        return kept


#: How often a running harvest re-emits its page-fetch trace tick. Every
#: single page would work — the trace id stays the same, so it never adds a
#: DOM node — but there is no reason to take a lock and touch a queue 1,799
#: times when 360 evenly-spaced updates read identically to a human.
TICK_EVERY_PAGES = 5
TICK_EVERY_SECONDS = 2.0


class _CountingFetcher(Fetcher):
    """A Fetcher that reports how far it has got.

    Progress could have been a callback threaded through every strategy in
    `harvest()`, touching each branch and risking the one thing this phase must
    not disturb. Counting successful page fetches costs one line and is
    accurate for the same reason: a page is fetched exactly once.

    Two ways a page arrives, so two entry points. A crawl and a sitemap
    fetch pages as HTML, which `html()` sees. A Markdown-twin manifest
    fetches them as text -- and `text()` cannot simply be counted too,
    because it also fetches the manifest itself, robots.txt and sitemaps,
    which are not pages. So that path calls `page_fetched()` explicitly,
    from the one place that knows a documentation page was just obtained.
    Missing this is why an `adk.dev`-shaped harvest -- 229 links, every one
    of them `.md` -- reported "starting" for its entire run.

    `stage`, when given, is the same counter re-expressed as a live trace
    tick for the Web UI — throttled independently of `progress`, which stays
    exact for `list_knowledge_base` because nothing here changes how often
    *it* updates.
    """

    def __init__(self, opts: Options, progress, stats: dict,
                stage: "tracing.Stage | None" = None):
        super().__init__(opts)
        self._progress = progress
        self._stats = stats
        self._stage = stage
        self._last_tick = 0.0

    def page_fetched(self) -> None:
        """One documentation page obtained by a route `html()` never sees."""
        self._progress.pages += 1
        if self._progress.expected is None:
            # harvest() records the site's own count before it starts fetching
            # pages, so by the first page this is usually already true.
            self._progress.expected = self._stats.get("discovered")
        if self._stage is None:
            return
        now = time.time()
        due = (self._progress.pages % TICK_EVERY_PAGES == 0
              or now - self._last_tick >= TICK_EVERY_SECONDS)
        if due:
            self._last_tick = now
            expected = self._progress.expected
            message = (f"fetched {self._progress.pages}/{expected} pages"
                      if expected else f"fetched {self._progress.pages} pages")
            counters = {"pages": self._progress.pages}
            if expected:
                counters["expected"] = expected
            try:
                self._stage.tick(message, counters=counters)
            except Exception:
                pass  # a trace hiccup must never interrupt a harvest

    def html(self, url: str) -> str:
        out = super().html(url)
        self.page_fetched()
        return out


def tool_harvest_docs(url: str, name: str | None = None, max_pages: int = 0,
                      js: bool = False, scope: str = "section",
                      version: str | None = None, intent: str = "",
                      corpora: list | None = None, strict: bool = False,
                      progress=None,
                      trace: "tracing.TraceContext | None" = None,
                      release_hint: str = "") -> str:
    """Harvest a WHOLE documentation set and store it in the knowledge base.

    `release_hint` is not in the tool schema either: it is the registry's
    current release, handed down by `learn_technology`. A site that names no
    version in its URLs publishes one version at a time — the current one —
    and "2026-09-13" is a poorer label for that than the release the registry
    says is current. It ranks below a version the site declares for itself,
    and the result says where the label came from.

    `progress` is not in the tool schema and no model passes it: it is how a
    background harvest reports itself to `list_knowledge_base`. `trace` is
    the same idea for the Web UI's execution timeline instead of a status
    poll — also not in the schema, also never seen by a model.
    """
    trace = trace or tracing.NULL_CONTEXT
    opts = _options(crawl=True, max_pages=max_pages, js=js, delay=0.2,
                    cap=HARVEST_PAGE_CAP, version=version)
    opts.scope = scope or "section"

    started = time.time()
    stats: dict = {}
    # One budget for the whole harvest, entry corpus and federated corpora
    # together — twelve calls per *harvest*, not twelve per corpus, or a
    # federation would multiply the cap by the thing it is meant to bound.
    # `active` is a no-op when reasoning is off, which is the default and is
    # what every test runs under.
    reasoner = reasoning.Reasoner()
    slug = _kb_slug(name or _name_from_url(url))
    # The real label depends on what the harvest actually collected, which is
    # only knowable at the end — so the writer opens under the URL's guess and
    # is renamed at settle. Nothing can see it in the meantime.
    provisional = _kb_slug(version) if version else _version_from_url(url)

    #: Bound below, and read again after the writer has closed — the only
    #: record of how big each stored page was.
    sink = None

    with reasoning.active(reasoner), store().writer(slug, provisional, url,
                                                    "crawl") as writer:
        # Opened by hand rather than `with trace.stage(...) as sub:`, because
        # the fetcher below must be able to `.tick()` this same stage *while*
        # `harvest()` is still running, not only report it once the call
        # returns.
        harvest_stage = trace.stage("harvesting", message="acquiring pages",
                                    target=url)
        harvest_stage.start()
        try:
            sink = _StripSink(writer)
            if progress is None:
                docs, strategy = harvest(url, opts, stats=stats, sink=sink)
            else:
                with _CountingFetcher(opts, progress, stats,
                                      stage=harvest_stage) as fetcher:
                    docs, strategy = harvest(url, opts, fetcher=fetcher, stats=stats,
                                             sink=sink)
        except BaseException as e:
            harvest_stage.finish(tracing.FAILED, error=f"{type(e).__name__}: {e}")
            raise
        if progress is not None:
            progress.phase = "storing"
        if not docs:
            # Abandoned rather than settled: whatever was already stored under
            # this label stays exactly as it was.
            harvest_stage.finish(tracing.FAILED, error="nothing was harvested")
            raise ForgeError(f"Harvested nothing from {url}")
        harvest_stage.finish(
            tracing.COMPLETED, message=f"{len(docs)} pages via {strategy}",
            result={"pages": len(docs), "strategy": strategy,
                   "expected": stats.get("discovered")})

        # v3 and v2 of the same library contradict each other, so they are
        # stored side by side rather than one overwriting the other.
        label = (_kb_slug(version) if version
                 else _version_label(url, docs,
                                     stats.get("declared_version", "") or release_hint))
        inferred_release = ""
        if (not version and release_hint and not stats.get("declared_version")
                and label == _kb_slug(release_hint)):
            inferred_release = (
                f"\n\nVersion **{label}** is the registry's current release, not "
                f"something the site said: its URLs name no version, so it is "
                f"taken to document the current one. Pass `version=` to file it "
                f"under something else."
            )

        # Did anything actually show these pages are that release, or is the
        # label just the request repeated back? `versions.same_release` is
        # strict about a release answering a request and was never consulted
        # here: `_version_label` takes `opts.version` whenever it is given one.
        #
        # Measured 2026-09-10: `pytorch` was stored as **lts** — a Korean
        # community site with no LTS scoping at all, under a release line
        # PyTorch retired after 1.8.2 — and `django` as **5.2** while holding
        # 214 weblog posts. Storing an unconfirmed version label is the same
        # failure as stamping a corpus complete, in a different field.
        declared = stats.get("declared_version", "")
        release_claim = ""
        if version:
            confirmed = bool(stats.get("release_confirmed")) or (
                declared and versions.same_release(version, declared))
            if not confirmed:
                release_claim = (
                    f"\n\n**Version {version!r} is the label you asked for, not "
                    f"a finding.** Nothing in this harvest — no path, no "
                    f"manifest declaration — shows these pages document that "
                    f"release. Treat the version as unverified, and say so if "
                    f"you answer from it."
                )
        truncated = bool(stats.get("truncated"))
        # Completeness is measured, not assumed. `None` means the harvest never
        # established how much there was to get — which is not the same claim
        # as "this is the whole thing", and must not be reported as one.
        whole = False if truncated else stats.get("whole")
        expected = stats.get("discovered")
        with trace.stage("storing", message="writing pages to the knowledge base",
                         target=slug):
            entry = writer.settle(complete=whole, expected=expected,
                                  version=label, strategy=strategy)

    # Pages the store itself refused — too large to index, most often. They
    # were reached and extracted, so they are disclosed rather than lost.
    for url_, why in (entry.get("rejected") or []):
        stats.setdefault("unextractable", []).append(f"{url_} ({why})")

    listing = "\n".join(f"{i}. {d.title}" for i, d in enumerate(docs[:30], 1))
    more = f"\n… and {len(docs) - 30} more" if len(docs) > 30 else ""

    # Federate BEFORE the headline is written, not after. `_federate` is what
    # discovers, admits and harvests the other corpora, so until it has run
    # there is no federation-level completeness to report — and reporting the
    # entry corpus's completeness as the headline is `ISSUES.md` W1: a harvest
    # that got all of one corpus and half of another announced itself complete,
    # with the shortfall visible only to a reader who scrolled to the note at
    # the bottom. Invariant 9 says the roll-up is the headline.
    federation_stage = trace.stage("corpus selection",
                                   message="checking for related corpora")
    federation_stage.start()
    with reasoning.active(reasoner):
        try:
            note = _federate(name or slug, url, stats, intent=intent,
                             explicit=_corpus_list(corpora), strict=bool(strict),
                             max_pages=max_pages, js=js)
        except BaseException as e:
            federation_stage.finish(tracing.FAILED, error=f"{type(e).__name__}: {e}")
            raise
    across = stats.get("federation") or {}
    federation_stage.finish(
        tracing.COMPLETED,
        message=(f"{across.get('selected', 0)} of {across.get('corpora', 1)} "
                 f"corpora selected" if across else "no related corpora"),
        result=across or None)
    if across.get("corpora", 1) > 1 and across.get("complete") != whole:
        whole = across.get("complete")
        truncated = False           # the shortfall is another corpus's, not a cap
        stats["reason"] = (f"{across.get('selected', 0)} of "
                           f"{across.get('corpora', 0)} corpora were selected and "
                           f"at least one of them is not completely covered")

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

    # Reached, fetched, and unreadable. Disclosed next to the coverage note
    # rather than logged: a page nobody could extract is a hole in the harvest,
    # and a hole nobody is told about is indistinguishable from a page that
    # never existed.
    unreadable = stats.get("unextractable") or []
    if unreadable:
        listed = "\n".join(f"- {u}" for u in unreadable[:10])
        warning += (
            f"\n\n**{len(unreadable)} page(s) reached but not extractable** — "
            f"nothing on them read like documentation, so they were not stored "
            f"and are not counted as covered:\n{listed}"
            + (f"\n… and {len(unreadable) - 10} more" if len(unreadable) > 10 else "")
        )

    warning += note + release_claim + inferred_release

    # What shape is this corpus? A caller who asked for "the documentation" and
    # received 560 pages averaging 490 characters -- one API symbol each -- has
    # something that cannot answer the question they were about to ask, and
    # nothing in the numbers above would have told them.
    #
    # Measured from what the sink stored, not from `docs`: their bodies were
    # released as they were written, so every length here was zero and this
    # note fired on every harvest of twenty pages or more. See `_StripSink`.
    shape_note = llmsfinder.density_note(sink.sizes if sink is not None
                                         else [len(d.markdown) for d in docs])
    if shape_note:
        warning += "\n\n" + shape_note

    where = entry["file"]
    trace.event(
        "harvest_docs completed", message=f"{len(docs)} pages via {strategy}",
        target=url,
        result={"pages": len(docs), "characters": entry["characters"],
               "strategy": strategy, "complete": whole, "truncated": truncated,
               "seconds": round(time.time() - started, 1)})
    return (
        f"Harvested **{slug}** {label} — {len(docs)} pages, "
        f"{entry['characters']:,} characters, "
        f"via {strategy}, in {time.time() - started:.0f}s.\n"
        f"Stored in {store().kind}: `{where}`.{warning}\n\n"
        f"Read it back with `read_knowledge_base(name=\"{slug}\", version=\"{label}\")` "
        f"— do NOT re-harvest to answer questions about it.\n\n"
        f"Pages:\n{listing}{more}"
    )


def _in_flight() -> str:
    """Harvests running or recently failed, as a block to prepend.

    Progress lives here rather than in a status tool of its own because a
    model wanting to know whether something is ready already calls
    list_knowledge_base; a separate tool would be one more thing for it not to
    call. Successful harvests need no line — they appear in the listing itself.
    A failed background harvest leaves no other trace, so it gets one.
    """
    live = harvest_jobs.running()
    failed = [j for j in harvest_jobs.recent() if j.state == "failed"]
    if not live and not failed:
        return ""

    out: list[str] = []
    if live:
        out.append(f"{len(live)} harvest{'s' if len(live) != 1 else ''} still running:")
        out += [j.line() for j in live]
        out += ["", "Those are being fetched now — nothing below counts them yet.", ""]
    if failed:
        out.append("Recent harvests that failed:")
        out += [j.line() for j in failed]
        out.append("")
    return "\n".join(out) + "\n"


def tool_list_knowledge_base() -> str:
    """What technologies have already been harvested, and what is still coming."""
    backend = store()
    try:
        techs, _ = backend.technologies()
    except StoreError as e:
        raise ForgeError(str(e)) from e

    flight = _in_flight()

    if not techs:
        return flight + ("Nothing is stored yet. Learn a technology with "
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
    return flight + "\n".join(lines)


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
            from docsforge.core import passages as psg
            all_sections = psg.sections(body)
            shown_sections = psg.sections(body[:MAX_CHARS])
            omitted_count = max(0, len(all_sections) - len(shown_sections))
            omitted_names = [s.heading_path for s in all_sections[len(shown_sections):][:5] if s.heading_path]
            omitted_desc = (", ".join(omitted_names) + ("…" if omitted_count > 5 else "")) if omitted_names else f"{omitted_count} sections"
            header = (
                f"<!-- {slug} {label} is {len(body):,} characters; showing the first "
                f"{MAX_CHARS:,}. Omitted {len(body) - MAX_CHARS:,} characters ({omitted_count} sections including {omitted_desc}). "
                f"Pass `section` to read specific pages/sections. -->\n\n"
            )
            return _truncate(header + body)
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
                          js: bool = False, intent: str = "",
                          corpora: list | None = None,
                          strict: bool = False, refresh: bool = False,
                          trace: "tracing.TraceContext | None" = None) -> str:
    """Learn a technology from its name alone: resolve, verify, harvest, store."""
    trace = trace or tracing.NULL_CONTEXT
    # File under the canonical form, not the caller's spelling. Otherwise
    # "Effect.ts" and "effect" become two copies of the same library, and the
    # second harvest silently re-crawls a site already stored under the first.
    slug = _kb_slug(_normalise(name) or name)

    # Already known? Re-crawling a site to answer a question you can already
    # answer is the most expensive way to be unhelpful.
    #
    # Unless what is stored is wrong, and until `refresh` there was no way to
    # say so. Measured 2026-09-10: `pytorch` had been stored from a community
    # mirror, and once resolution was fixed this tool still answered "already
    # stored — nothing was fetched" and returned the mirror. A caller that
    # harvests the wrong corpus once could never correct it through the tool
    # it harvests with; the only routes out were `forget_documentation`, which
    # is gated behind an environment variable, and knowing the URL already —
    # which is the thing the caller came here missing.
    known = stored_name(name)
    if refresh and known:
        trace.event("refreshing", target=name,
                    message=f"{known} is stored; re-fetching because refresh was asked for")
        known = ""
    if known:
        backend = store()
        entry = backend.entry(known, version)
        if entry is not None:
            trace.event("already stored", target=name,
                       message=f"{known} {entry['version']} — nothing fetched",
                       result={"technology": known, "version": entry["version"],
                              "pages": entry["pages"]})
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

    def work(progress: harvest_jobs.Progress) -> str:
        """Resolve then harvest. Runs on a worker thread past the deadline."""
        try:
            progress.phase = "resolving"
            resolve_stage = trace.stage("resolving identity",
                                        message="finding official documentation",
                                        target=name)
            resolve_stage.start()
            try:
                found = _resolve(name, ecosystem=(ecosystem or "").strip())
            except BaseException as e:
                resolve_stage.finish(tracing.FAILED, error=f"{type(e).__name__}: {e}")
                raise
            if found.best is None:
                listed = "\n".join(f"- {c.url} ({c.reason or 'unverified'})"
                                   for c in found.candidates)
                # Every candidate DocsForge looked at and rejected, not just
                # the fact that none worked -- this is the "what result was
                # produced" answer for a failed resolution, sourced straight
                # from Resolution.as_dict() rather than re-derived here.
                resolve_stage.finish(
                    tracing.FAILED,
                    error=found.note or f"could not find documentation for {name!r}",
                    result=found.as_dict())
                raise ForgeError(
                    (found.note or f"Could not find documentation for {name!r}.")
                    + (f"\n\nCandidates considered:\n{listed}" if listed else "")
                    + "\n\nIf you know the URL, call harvest_docs with it directly."
                )
            resolve_stage.finish(
                tracing.COMPLETED,
                message=f"resolved to {found.best.url} via {found.resolved_via or 'registry'}",
                result=found.as_dict())

            progress.url = found.best.url
            progress.phase = "harvesting"
            harvested = tool_harvest_docs(url=found.best.url, name=slug,
                                          max_pages=max_pages, js=js, version=version,
                                          intent=intent, corpora=corpora,
                                          strict=strict, progress=progress, trace=trace,
                                          release_hint=found.release)
            return (
                f"{note}Resolved **{name}** to {found.best.url}\n"
                f"({found.best.evidence}; {found.best.reason})\n\n{harvested}"
            )
        finally:
            # Closing here is only correct once run_tool() has *already*
            # given up ownership of this trace by detaching (the deadline
            # passed and it returned "still running" without closing it).
            # In the common case -- work() finishes inside the deadline --
            # run_tool() is still on the stack above `tool.fn()` and has
            # not detached; it closes the trace itself once `tool.fn()`
            # actually returns, which happens after any error-handling
            # event run_tool() still needs to add. Closing unconditionally
            # here would race that and could close the trace before that
            # event is recorded.
            if trace.is_detached():
                trace.close()

    # Already being fetched? Say so rather than crawl the site twice.
    #
    # `_still_harvesting` has always told the model not to call again, and
    # that instruction was the only thing preventing it -- which held for
    # exactly as long as one process. The `claudecode` provider runs each
    # turn's tools in a fresh subprocess, so the second call landed somewhere
    # that had never heard of the first, and langchain was crawled twice.
    # Now that a running harvest is visible from any process, this is a check
    # rather than a request.
    wanted = _kb_slug(_normalise(name) or name)
    for other in harvest_jobs.running():
        # Not the record we are here to fulfil. A detached worker runs this
        # function with the launcher's job id already published as running, so
        # without this the worker reads its own record, concludes the harvest
        # is already in hand, and returns having done nothing — which is
        # exactly what happened, silently, three times over.
        if other.id and other.id == harvest_jobs.adopting():
            continue
        if _kb_slug(_normalise(other.label) or other.label) == wanted:
            return _still_harvesting(other)

    # Under a host that will be torn down with the turn, the harvest goes into
    # a process of its own. A thread here would die with us -- measured, and
    # the reason: a client teardown *kills*, and the linger that handles a
    # polite hang-up cannot help. Everything below is unchanged, because a
    # detached harvest that finishes inside the deadline still returns its own
    # summary: the worker writes the result into the record and we read it back.
    if harvest_jobs.DETACHED:
        handed = harvest_jobs.hand_off(
            name,
            {"name": name, "version": version, "max_pages": max_pages,
             "js": js, "intent": intent, "corpora": corpora, "strict": strict,
             "refresh": refresh})
        if handed is not None:
            # Watched exactly as a local job would be, so a harvest small
            # enough to finish inside the deadline still returns its own
            # summary rather than a progress line.
            settled = harvest_jobs.await_record(handed.id, harvest_jobs.DEADLINE)
            if settled is not None and settled.state == harvest_jobs.DONE:
                return settled.result
            if settled is not None and settled.state == harvest_jobs.FAILED:
                raise ForgeError(settled.error or f"Harvesting {name!r} failed.")
            trace.detach()
            return _still_harvesting(harvest_jobs.get(handed.id) or handed)
        # Nobody answered. Run it here and say plainly that it will not
        # outlive this turn — a promise of background work this host cannot
        # keep is what sent three langchain harvests to their deaths without
        # a word.
        _log_no_server()

    # The harvest starts on its own thread and we wait on it -- but only up to
    # the deadline. Anything finishing in time returns exactly what it always
    # returned, which is nearly every harvest and every test.
    job = harvest_jobs.start(name, work)
    if harvest_jobs.wait(job):
        if job.exc is not None:
            # Re-raise the original, not a copy: a caller who would have seen a
            # ForgeError listing rejected candidates still sees precisely that.
            raise job.exc
        return job.result

    # Still running past the deadline: run_tool() must not close this trace
    # when this call returns, because `work()` -- and the trace events it is
    # still emitting -- keeps going on its own thread after this line.
    trace.detach()
    return _still_harvesting(job)


#: Set once a handoff has failed, so the warning is attached to the result
#: rather than only written to a log the caller never reads.
_NO_SERVER = threading.local()


def _log_no_server() -> None:
    _NO_SERVER.value = True
    applog.error("harvest_handoff",
                 f"no DocsForge server at {harvest_jobs.SERVER}; the harvest "
                 f"will not outlive this process")


def _server_warning() -> str:
    """What to add when a harvest is running somewhere that cannot keep it."""
    if not getattr(_NO_SERVER, "value", False):
        return ""
    _NO_SERVER.value = False
    return (
        f"\n\n**This harvest will stop when this turn ends.** No DocsForge "
        f"server answered at {harvest_jobs.SERVER}, so it is running inside a "
        f"process the client tears down — start `python -m docsforge.server.app` and ask again "
        f"to have it run somewhere that survives."
    )


def _still_harvesting(job: harvest_jobs.Job) -> str:
    """What a caller gets when the harvest outlives the deadline.

    The instruction not to call again is the load-bearing line. A model reading
    "still running" as "did not work" will call `learn_technology` a second
    time, and crawling the same site twice is the exact cost this change was
    made to avoid.
    """
    where = f" from {job.progress.url}" if job.progress.url else ""
    slug = _kb_slug(_normalise(job.label) or job.label)
    if harvest_jobs.EPHEMERAL:
        # No background exists here. Saying "it continues" would be the same
        # lie the detached case was built to stop telling — and the pages
        # streamed so far are never published, so nothing partial is stored.
        return (
            f"**Learning {job.label}{where} did not finish within "
            f"{harvest_jobs.DEADLINE:.0f}s — it was {job.progress.line()}, and "
            f"this host cannot keep working after a request ends, so that "
            f"harvest is discarded.** Nothing partial was stored.\n\n"
            f"Large harvests need a long-lived DocsForge: run "
            f"`python main.py --stdio` (or `--http`) on any machine with the same "
            f"`DOCSFORGE_DB`, learn {job.label!r} there, and it is readable "
            f"here immediately with `read_knowledge_base(name=\"{slug}\")`.\n\n"
            f"Tell the user that; do not call learn_technology for "
            f"{job.label!r} again here."
        )
    return (
        f"**Learning {job.label}{where} - still running.** "
        f"Harvest id `{job.id}`.\n\n"
        f"Currently {job.progress.line()}, {job.elapsed:.0f}s elapsed. This "
        f"returned before the harvest finished so your client would not time "
        f"out; it continues in the background while this server runs.\n\n"
        f"- `list_knowledge_base()` reports it, and every other harvest in flight.\n"
        f"- When it finishes, read it with `read_knowledge_base(name=\"{slug}\")`.\n"
        f"- **Do not call learn_technology for {job.label!r} again** - it is "
        f"already running, and a second call would crawl the same site twice.\n\n"
        f"Tell the user it is being fetched now rather than reporting a failure."
        + _server_warning()
    )


#: How many matching pages to open and chunk. Bounded because ranking sections
#: means reading page bodies, and reading a hundred of them to answer one
#: question is the cost this whole layer exists to avoid.
PASSAGE_PAGES = 10


def tool_search_knowledge_base(query: str, technology: str | None = None,
                               version: str | None = None, limit: int = 20,
                               kind: str = "") -> str:
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

    from docsforge.core import federation
    from docsforge.core import passages as psg

    # Open the best-matching pages and rank their SECTIONS. One page of a
    # generated API reference can be 20k tokens spent answering a one-line
    # question; a documentation tool that costs more context than it saves has
    # inverted its own purpose.
    chunks: list = []
    #: Which technology each passage came from. A search across the whole store
    #: that does not say which manual answered is unciteable.
    origin: dict[str, tuple[str, str]] = {}
    skipped_kind = 0
    for hit in hits[:PASSAGE_PAGES]:
        try:
            page = backend.page(hit["technology"], hit["version"], hit["ordinal"])
        except StoreError:
            continue
        if kind:
            # Kind is derived from the page's own URL rather than read from the
            # store, which does not record it yet (see ISSUES F1). That makes
            # the filter real today at the cost of being only as good as the
            # path tokens.
            page_kind, _confidence = federation.classify_kind(page.get("url") or "")
            if page_kind != kind:
                skipped_kind += 1
                continue
        origin[page.get("url") or hit["title"]] = (hit["technology"], hit["version"])
        chunks.extend(psg.sections(page.get("content") or "",
                                   page_title=hit["title"],
                                   page_url=page.get("url") or ""))

    best = psg.rank(query, chunks, limit=max(1, min(int(limit), 10)))
    if not best:
        where = f" in {tech}" if tech else ""
        extra = (f" {skipped_kind} page(s) matched but were not of kind {kind!r}."
                 if skipped_kind else "")
        return (f"No passage{where} answers {query!r} directly.{extra} "
                f"`read_knowledge_base` will show whole pages.")

    ranked = "ranked" if backend.kind == "postgres" else "unranked (file store)"
    tokens = sum(section.tokens for section in best)
    lines = [f"{len(best)} passage(s) for {query!r}, from a {ranked} search "
             f"over {len(hits)} matching page(s) — about {tokens:,} tokens "
             f"rather than the whole pages:", ""]
    for section in best:
        tech_name, tech_version = origin.get(
            section.page_url or section.page_title, ("", ""))
        lines.append(f"- **{tech_name}** {tech_version} · {section.page_title} "
                     f"— {section.heading_path}")
        lines.append(f"    {' '.join(section.text.split())[:600]}")
        if section.page_url:
            lines.append(f"    {section.page_url}")
        lines.append("")
    lines += ["Read the whole page with "
              "`read_knowledge_base(name=..., version=..., section=<title>)`."]
    return "\n".join(lines)


def tool_forget_resolution(name: str = "") -> str:
    """Drop a remembered resolution so the next lookup starts over.

    Ungated, unlike `forget_documentation`: this deletes no harvested pages,
    only the memory of where a name pointed. A remembered resolution that is
    confidently wrong is precisely the state a caller needs to be able to
    clear without an environment variable standing in the way.
    """
    from docsforge.core import resolver
    return resolver.forget_resolution(name or "")


def tool_forget_selection(name: str = "") -> str:
    """Drop a remembered choice of which corpora to harvest.

    Ships alongside the policy cache for the same reason `forget_resolution`
    ships with the resolution cache: a remembered choice that has become wrong
    is exactly the state a caller must be able to clear.
    """
    from docsforge.core import selection
    return selection.forget_selection(name or "")


def tool_forget_documentation(name: str, version: str | None = None) -> str:
    """Delete a stored technology, or one version of it. Irreversible."""
    backend = store()
    # Resolve the caller's spelling to what is actually filed, so "Effect.ts"
    # removes `effect` rather than reporting that nothing matched.
    known = stored_name(name) or name
    try:
        rows = backend.versions(known)
    except StoreError:
        raise ForgeError(
            f"Nothing stored for {name!r}. `list_knowledge_base` shows what is."
        ) from None

    doomed = [v for v in rows if version is None or v["version"] == version]
    if not doomed:
        have = ", ".join(v["version"] for v in rows)
        raise ForgeError(f"{known} has no version {version!r} (have: {have}).")

    pages = sum(v["pages"] for v in doomed)
    chars = sum(v["characters"] for v in doomed)
    removed = backend.delete(known, version)
    left = len(rows) - len(doomed)

    return (
        f"Deleted **{known}**"
        + (f" {version}" if version else " (all versions)")
        + f" — {removed} version(s), {pages:,} pages, {chars:,} characters.\n\n"
        + (f"{left} other version(s) of {known} are still stored.\n\n" if left else "")
        + "This cannot be undone; re-harvesting means crawling the site again."
    )


def tool_scan_project(path: str | None = None, unknown_only: bool = False) -> str:
    """List a project's dependencies and say which are already documented here."""
    from pathlib import Path as _Path

    from docsforge.core import manifests

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
                "intent": {"type": "string", "default": "reference",
                           "enum": ["reference", "resolve-import", "implement",
                                    "learn", "operate"],
                           "description": "What the documentation is FOR, which decides "
                                          "which corpora enter scope when a technology "
                                          "documents itself in several places. "
                                          "\"resolve-import\" takes the API and SDK "
                                          "references; \"learn\" takes the guide; "
                                          "\"reference\" (default) takes everything. "
                                          "Corpora left out are always listed, never "
                                          "silently dropped."},
                "corpora": {"type": "string",
                            "description": "Comma-separated corpus URLs to harvest, when "
                                           "a previous call returned NEEDS SELECTION and "
                                           "you are answering it. Your answer is "
                                           "remembered, so this is asked once."},
                "strict": {"type": "boolean", "default": False,
                           "description": "Report `usable_for_planning`. True only when "
                                          "every corpus your intent requires came back "
                                          "complete. Gate on it if you are generating "
                                          "code or a plan from the result."},
                "refresh": {"type": "boolean", "default": False,
                            "description": "Harvest again even though this technology is "
                                           "already stored, replacing what is there. Use "
                                           "it ONLY when the stored copy is demonstrably "
                                           "wrong — it documents a different project, or "
                                           "the source URL in the result is not the "
                                           "project's own documentation. It re-crawls the "
                                           "whole site, so never use it merely to get a "
                                           "newer copy or because a search missed "
                                           "something."},
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
                "kind": {"type": "string",
                         "enum": ["api", "sdk", "spec", "guide", "cookbook",
                                  "operations", "changelog", "language", "meta"],
                         "description": "Optional: only return passages from pages of "
                                        "this kind. Use \"api\" when you want a "
                                        "signature and do not want tutorial prose "
                                        "competing with it."},
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
    Tool(
        "forget_resolution",
        "Forget where a technology was previously resolved to, so the next "
        "learn_technology works it out again from scratch. Use this when a "
        "harvest turned out to be the WRONG project, or when a project has "
        "moved its documentation. It deletes no stored documentation — only "
        "the remembered lookup. Omit `name` to clear every remembered lookup.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The technology whose remembered lookup to "
                                        "drop. Omit to clear all of them."},
            },
            "required": [],
        },
        tool_forget_resolution,
    ),
    Tool(
        "forget_selection",
        "Forget which corpora were chosen for a technology, so the next "
        "learn_technology asks again. Use it when a technology has grown new "
        "documentation, or when the earlier choice turned out to be the wrong "
        "part. It deletes no stored documentation. Omit `name` to clear all.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The technology whose remembered corpus "
                                        "choice to drop. Omit to clear all of them."},
            },
            "required": [],
        },
        tool_forget_selection,
    ),
]


# ── Deletion, off by default ─────────────────────────────────
# Removing a harvest is the one irreversible thing DocsForge can do, and a
# model that has just mis-resolved a name is exactly the caller you do not want
# holding that lever: 703 pages of Effect are one confident hallucination away.
# The person who harvested something is the one who should decide it was a
# mistake, so the human surfaces — DocsStore's UI and `docsforge --forget` —
# are always available and this one is opt-in.
ALLOW_DELETE = os.environ.get("DOCSFORGE_ALLOW_DELETE", "").strip().lower() in (
    "1", "true", "yes", "on")

if ALLOW_DELETE:
    TOOLS.append(Tool(
        "forget_documentation",
        "Delete stored documentation. IRREVERSIBLE — the pages are gone and "
        "re-harvesting means crawling the site again. Use it only when the "
        "user has asked for something to be removed, or when a harvest "
        "demonstrably stored the wrong project. Never call it to 'refresh' "
        "something: learn_technology(refresh=true) replaces a stored version "
        "without destroying it first.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The stored technology name, exactly as "
                                        "list_knowledge_base reports it."},
                "version": {"type": "string",
                            "description": "Which version to remove. Omit to remove "
                                           "the technology and every version of it."},
            },
            "required": ["name"],
        },
        tool_forget_documentation,
    ))

BY_NAME = {t.name: t for t in TOOLS}

#: Tool functions willing to report an execution trace, discovered once from
#: their real signature rather than a second, separately-maintained list --
#: a tool that gains a `trace` parameter is traced automatically, and one
#: that never does costs nothing extra here.
_TRACED = {t.name for t in TOOLS if "trace" in inspect.signature(t.fn).parameters}

#: The trace id `run_tool` minted for the most recent call *on this thread*.
#: app.py reads it immediately after relaying a provider's `tool_end` event
#: -- reliable because both happen on the same thread, in the order the call
#: actually ran: `run_tool` only returns once `tool.fn` has, and no other
#: tool call can interleave on one thread inside one provider's loop.
_last_trace = threading.local()


def last_trace_id() -> str | None:
    return getattr(_last_trace, "value", None)


def reset_trace_id() -> None:
    """Forget the id from the previous call on this thread.

    Server threads are reused. Without this, a turn whose provider runs its
    tools out of process (the Claude Code CLI calls them over MCP) would
    find the id left behind by an earlier, unrelated turn that did run one
    in process -- and attach a browser to somebody else's execution trace.
    """
    _last_trace.value = None


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


#: Argument names worth showing as the headline "what this acted on", in
#: the order a reader would want them. Everything else still travels in the
#: event's metadata; this only decides what the one-line summary says.
_TARGET_KEYS = ("url", "name", "query", "technology", "section", "path", "out_dir")


def target_of(arguments: dict) -> str:
    for key in _TARGET_KEYS:
        value = (arguments or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def run_tool(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call. Errors come back as text so a model can recover
    from them rather than the whole turn dying.

    `run_tool_checked` is the same call with a flag saying whether that text
    is an error, for a caller (the MCP surface) that has a way to mark one.
    """
    return run_tool_checked(name, arguments)[0]


def run_tool_checked(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """`(text, ok)`: what the model is handed, and whether the call succeeded.

    Every call is wrapped in one root trace stage recording what was asked
    (the arguments, sanitized) and what came back (the tool's own returned
    text, bounded). That happens for *every* tool, not only the handful
    that report their own internal stages -- a tool nobody has instrumented
    still answers "what ran, and what did it return", which is the whole
    question the detail view exists for. Tools that do report internals
    (see `_TRACED`) are handed a context parented to this stage, so their
    stages nest underneath it rather than floating beside it.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        return f"Error: unknown tool {name!r}. Available: {', '.join(BY_NAME)}", False

    ctx = tracing.start(name)
    _last_trace.value = ctx.trace_id
    started = time.perf_counter()

    allowed = set((tool.schema.get("properties") or {}).keys())
    kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed}
    call = ctx.stage(name, target=target_of(kwargs), metadata=dict(kwargs))
    inner = call.start(f"called {name}")

    try:
        if name in _TRACED:
            kwargs["trace"] = inner
        result = tool.fn(**kwargs)
        duration_ms = (time.perf_counter() - started) * 1000
        # A detached trace means the call returned but the work did not stop
        # -- a harvest past its deadline, still running on its own thread and
        # still filling in the child stages below this one. Saying "returned
        # N characters" and nothing else would read as finished.
        still_running = ctx.is_detached()
        summary = f"returned {len(result):,} characters in {duration_ms / 1000:.1f}s"
        call.finish(tracing.COMPLETED,
                    message=(f"{summary} — work continues in the background"
                            if still_running else summary),
                    result={"characters": len(result),
                           "seconds": round(duration_ms / 1000, 2),
                           "still_running": still_running},
                    output=result)
        try:
            applog.tool_call(name, ok=True, duration_ms=duration_ms,
                             trace_id=ctx.trace_id, chars=len(result))
        except Exception:
            pass
        return result, True
    except ForgeError as e:
        return _tool_error(name, ctx, call, started, f"Error: {e}", str(e)), False
    except TypeError as e:
        return _tool_error(name, ctx, call, started,
                          f"Error: bad arguments for {name}: {e}", str(e)), False
    except Exception as e:  # a scrape can fail in a hundred ways
        # Not one of ours, so the type, the text, and the line it came from —
        # a bare "Error executing tool" reaching a client is what this exists
        # to prevent. The full traceback goes to the server log.
        detail = f"{type(e).__name__}: {e}".rstrip(": ")
        return _tool_error(name, ctx, call, started,
                          f"Error: {detail}{_origin(e)}", detail, traceback=True), False
    finally:
        # A tool whose real work continues past this call (a harvest still
        # running on a background thread) calls `ctx.detach()` before
        # returning, precisely so this does not cut its trace off early --
        # see tool_learn_technology's still-running path, which closes it
        # itself once the background thread actually finishes.
        if not ctx.is_detached():
            ctx.close()


def _origin(e: BaseException) -> str:
    return harvest_jobs.origin_of(e, skip=os.path.abspath(__file__))


def _tool_error(name: str, ctx: "tracing.TraceContext", call: "tracing.Stage",
                started: float, message: str, detail: str,
                traceback: bool = False) -> str:
    # The failure is recorded against the call that actually failed, and the
    # text the model was handed is stored as its output -- a failed tool
    # call still produced a result, and hiding it would leave the detail
    # view unable to answer "what came back" for exactly the calls where
    # that question matters most.
    call.finish(tracing.FAILED, error=detail, output=message)
    duration_ms = (time.perf_counter() - started) * 1000
    try:
        applog.tool_call(name, ok=False, duration_ms=duration_ms,
                         trace_id=ctx.trace_id, error=detail)
        if traceback:
            import traceback as _tb
            applog.error(f"tool:{name}", _tb.format_exc())
    except Exception:
        pass
    return message
