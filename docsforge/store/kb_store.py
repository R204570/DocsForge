"""
Where harvested documentation lives — the DocsStore.

Three levels, because documentation has three levels:

    technology        effect
      version         v3, v2, or the harvest date when a site is unversioned
        page          Introduction, Error Handling, Layers, …

Keeping versions apart matters: a project's v2 and v3 docs contradict each
other, and a model handed both will happily quote the wrong one. Re-harvesting
a version you already have replaces that version and leaves the others alone.

Two backends behind one interface:

* **files** — `knowledge_base/<tech>/<version>.md`. Zero setup, and the file is
  a deliverable you can hand to anyone.
* **postgres** — a row per page with a GIN-indexed tsvector. Ranked search
  across everything stored, snippets showing why a page matched, and pagination
  that does not load the whole store to count it.

Postgres is used when DOCSFORGE_DB (or DATABASE_URL) is set; files otherwise.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from docsforge.core import versions as versions_mod

# A page written by a combined file looks like:
#     ## {title}
#
#     Source: <url>
_PAGE_BOUNDARY = re.compile(r"\n(?=## [^\n]*\n+Source: <)")
_PAGE_HEAD = re.compile(r"^## (?P<title>[^\n]*)\n+Source: <(?P<url>[^>]*)>\s*", re.S)

#: A path segment that looks like a documentation version: v3, 2.1, latest…
_VERSION_SEGMENT = re.compile(r"^(v\d+(\.\d+)*|\d+\.\d+(\.\d+)*|latest|stable|next|canary)$", re.I)


def merge_complete(*values) -> bool | None:
    """Combine per-version completeness into one answer for a technology.

    Three states, and the order they resolve in matters. A known-partial copy
    stays partial no matter what else is stored beside it. Failing that, a copy
    whose extent was never established makes the whole answer `unknown` —
    because a caller told `True` will stop looking, and we have no grounds to
    say `True` about something nobody counted.
    """
    seen = list(values)
    if any(v is False for v in seen):
        return False
    if any(v is None for v in seen):
        return None
    return True


def split_pages(body: str) -> tuple[str, list[str]]:
    """Return (header, [page, ...]) for a combined knowledge-base file."""
    parts = _PAGE_BOUNDARY.split(body)
    if len(parts) > 1:
        return parts[0], parts[1:]
    loose = re.split(r"\n(?=## )", body)
    return (loose[0], loose[1:]) if len(loose) > 1 else (body, [])


def parse_page(block: str) -> tuple[str, str, str]:
    """A combined-file page block -> (title, url, body)."""
    match = _PAGE_HEAD.match(block)
    if not match:
        first = block.split("\n", 1)[0].lstrip("# ").strip()
        return first or "Untitled", "", block
    return match.group("title").strip(), match.group("url").strip(), block[match.end():].strip()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-")
    return slug[:64] or "untitled"


def name_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "docs").lower()
    for strip in ("www.", "docs."):
        if host.startswith(strip):
            host = host[len(strip):]
    return host.split(".")[0] or "docs"


def version_from_url(url: str) -> str:
    """The documentation version a URL points at.

    Most docs sites put it in the path (/docs/v3/…, /3.12/…). When there is no
    such segment the site publishes one version at a time, so the harvest date
    is the only honest label — it says which snapshot this is.
    """
    for part in (p for p in urlparse(url).path.split("/") if p):
        if _VERSION_SEGMENT.match(part):
            return part.lower()
    return time.strftime("%Y-%m-%d")


class StoreError(RuntimeError):
    """Something the caller can act on: no such entry, unreachable database."""


class Store(Protocol):
    kind: str
    location: str

    def session(self): ...


@contextmanager
def _no_session():
    """A store with nothing to pool. Files have no connections to share."""
    yield


# ─────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────
class FileStore:
    """One Markdown file per version: knowledge_base/<tech>/<version>.md"""

    kind = "files"

    #: Set by build_store when this store is standing in for an unreachable
    #: database: the reason, and the DSN worth retrying.
    degraded = ""
    wanted_dsn = ""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.index_path = self.root / "index.json"
        self.location = str(self.root)

    #: Nothing to share: a file store opens no connections.
    session = staticmethod(_no_session)

    # -- index --------------------------------------------------
    def _load(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return self._upgrade_v1(data)

    def _upgrade_v1(self, index: dict) -> dict:
        """Read an index written before versions existed.

        v1 keyed entries by technology alone and stored `name`; v2 keys them by
        technology and version. Postgres got a migration for this and the file
        store did not, so an older knowledge_base crashed the whole store with
        `KeyError: 'technology'` on the first read.

        The Markdown stays where it is — the entry already records its path, so
        only the index needs rewriting.
        """
        old = {k: v for k, v in index.items()
               if isinstance(v, dict) and "technology" not in v and "name" in v}
        if not old:
            return index

        upgraded = {k: v for k, v in index.items() if k not in old}
        for entry in old.values():
            tech = entry["name"]
            version = version_from_url(entry.get("source", ""))
            moved = dict(entry, technology=tech, version=version)
            moved.pop("name", None)
            moved.setdefault("saved", 0.0)
            upgraded[self._key(tech, version)] = moved

        try:
            self._save(upgraded)
        except OSError:
            pass       # read-only checkout: still usable in memory
        return upgraded

    def _save(self, index: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    def _key(self, tech: str, version: str) -> str:
        return f"{tech}@{version}"

    # -- writing ------------------------------------------------
    def writer(self, tech, version, source, strategy, expected: int | None = None):
        """A writer that makes each page durable as it arrives.

        Blue/green: pages stream into a `.partial` file that no reader can see,
        and the real file is only replaced once the harvest settles. A crash
        therefore costs the new harvest and never the one already stored —
        which is the property the old delete-then-write transaction gave by
        accident, kept deliberately here.
        """
        return _FileWriter(self, tech, version, source, strategy, expected)

    def save(self, tech, version, source, strategy, pages, complete,
             expected: int | None = None) -> dict:
        """Write a whole harvest at once. Delegates, so there is one write path.

        Kept because callers and tests use it, but it is now a thin wrapper: a
        batch save and a streamed one go through exactly the same code, so
        neither can drift from the other.
        """
        with self.writer(tech, version, source, strategy, expected) as w:
            for title, url, body in pages:
                w.add(title, url, body)
            return w.settle(complete=complete, expected=expected)

    def _finish(self, tech, version, source, strategy, path, text_len,
                titles, complete, expected) -> dict:
        index = self._load()
        index[self._key(tech, version)] = {
            "technology": tech, "version": version, "source": source,
            "strategy": strategy, "pages": len(titles), "characters": text_len,
            "file": str(path), "harvested": time.strftime("%Y-%m-%d %H:%M"),
            # Displayed to the minute, ordered to the microsecond: two harvests
            # in the same minute still have a newest one, and "read the newest
            # version" has to agree with Postgres about which that is.
            "saved": time.time(),
            "complete": complete,
            "expected": expected,
            "titles": list(titles[:2000]),
        }
        self._save(index)
        return index[self._key(tech, version)]

    def delete(self, tech: str, version: str | None = None) -> int:
        index = self._load()
        doomed = [k for k, v in index.items()
                  if v["technology"] == tech and (version is None or v["version"] == version)]
        for key in doomed:
            path = Path(index[key]["file"])
            if path.exists():
                path.unlink()
            del index[key]
        self._save(index)
        return len(doomed)

    # -- reading ------------------------------------------------
    def _rows(self) -> list[dict]:
        return sorted(self._load().values(),
                      key=lambda e: (e["technology"], e["version"]))

    def technologies(self, offset: int = 0, limit: int | None = None,
                     query: str = "") -> tuple[list[dict], int]:
        grouped: dict[str, dict] = {}
        for row in self._rows():
            tech = grouped.setdefault(row["technology"], {
                "name": row["technology"], "versions": 0, "pages": 0,
                "characters": 0, "latest": "", "harvested": "",
                "complete": True, "saved": 0.0, "_labels": [],
            })
            tech["versions"] += 1
            tech["pages"] += row["pages"]
            tech["characters"] += row["characters"]
            tech["complete"] = merge_complete(tech["complete"], row.get("complete"))
            tech["_labels"].append((row.get("saved", 0.0), row["version"]))
            if row.get("saved", 0) >= tech["saved"]:
                tech["saved"] = row.get("saved", 0)
                tech["harvested"] = row["harvested"]

        # "latest" is the newest version, not the newest download. Handing a
        # model 1.10 because it was crawled after 2.11 is the contradiction the
        # versioned store exists to prevent. Labels that carry no ordering fall
        # back to harvest time, hence the pre-sort.
        for tech in grouped.values():
            labels = sorted(tech.pop("_labels"), reverse=True)
            tech["latest"] = versions_mod.newest([label for _, label in labels])

        rows = sorted(grouped.values(), key=lambda t: t["name"])
        if query:
            needle = query.lower()
            rows = [t for t in rows if needle in t["name"].lower()]
        total = len(rows)
        if limit is not None:
            rows = rows[offset:offset + limit]
        return rows, total

    def versions(self, tech: str) -> list[dict]:
        rows = [dict(r) for r in self._rows() if r["technology"] == tech]
        if not rows:
            raise StoreError(f"nothing stored for {tech!r}")
        # Newest *version* first, not most recently harvested — a caller that
        # names no version is asking for the current one. Harvest time only
        # breaks ties between labels that cannot be ordered against each other.
        rows.sort(key=lambda r: (versions_mod.sort_key(r["version"]),
                                 r.get("saved", 0.0), r["harvested"]),
                  reverse=True)
        return rows

    def entry(self, tech: str, version: str | None = None) -> dict | None:
        try:
            rows = self.versions(tech)
        except StoreError:
            return None
        if version is None:
            return rows[0]
        return next((r for r in rows if r["version"] == version), None)

    def _blocks(self, tech: str, version: str | None):
        meta = self.entry(tech, version)
        if meta is None:
            raise StoreError(f"no stored documentation for {tech!r}"
                             + (f" version {version!r}" if version else ""))
        path = Path(meta["file"])
        if not path.exists():
            raise StoreError(f"{tech} is in the index but its file is missing: {path}")
        _, blocks = split_pages(path.read_text(encoding="utf-8"))
        return meta, blocks

    def pages(self, tech: str, version: str | None = None) -> list[dict]:
        _, blocks = self._blocks(tech, version)
        out = []
        for i, block in enumerate(blocks, 1):
            title, url, body = parse_page(block)
            out.append({"ordinal": i, "title": title, "url": url, "characters": len(body)})
        return out

    def page(self, tech: str, version: str | None, ordinal: int) -> dict:
        _, blocks = self._blocks(tech, version)
        if not 1 <= ordinal <= len(blocks):
            raise StoreError(f"{tech} has no page {ordinal}")
        title, url, body = parse_page(blocks[ordinal - 1])
        return {"ordinal": ordinal, "title": title, "url": url, "content": body}

    def read(self, tech: str, section: str | None = None,
             version: str | None = None) -> tuple[str, str, int]:
        meta, blocks = self._blocks(tech, version)
        if not section:
            return "\n\n".join(blocks), "all", len(blocks)

        needle = section.lower()
        hits = [b for b in blocks if needle in b.split("\n", 1)[0].lower()]
        how = "title"
        if not hits:
            hits = [b for b in blocks if needle in b.lower()]
            how = "content"
        if not hits:
            raise StoreError(f"nothing in {tech} matches {section!r}")
        return "\n\n".join(hits), how, len(hits)

    def titles(self, tech: str, version: str | None = None) -> list[str]:
        meta = self.entry(tech, version)
        return list(meta.get("titles", [])) if meta else []

    def search(self, query: str, tech: str | None = None, version: str | None = None,
               limit: int = 30) -> list[dict]:
        """The query as a phrase, then — if nothing holds it — as terms.

        Substring matching answers "where does it say this exact thing", which
        is the wrong question when the caller is a model asking a sentence. No
        page contains a whole question, so a corpus that certainly holds the
        answer reported nothing at all, and the reply DocsForge gave was *"try
        `learn_technology`"* — advice to re-harvest a site it already had.

        Postgres had the same defect for the same reason and it is fixed there
        the same way: precise first, and only fall back when precise found
        nothing, so no query that worked before changes its answer."""
        hits = self._search_substring(query, tech, version, limit)
        return hits or self._search_terms(query, tech, version, limit)

    def _search_terms(self, query: str, tech: str | None, version: str | None,
                      limit: int) -> list[dict]:
        """Rank sections by the query's terms, using read-time relevance.

        `passages` already scores a section against a query — heading matches
        above body matches, lightly length-normalised — and it is what the read
        path uses. Search asking a different question of the same corpus than
        reading does is how the two drift apart."""
        from docsforge.core import passages as psg

        terms = psg._terms(query)
        if not terms:
            return []
        scored: list[tuple[float, dict]] = []
        for row in self._rows():
            if tech and row["technology"] != tech:
                continue
            if version and row["version"] != version:
                continue
            try:
                _, blocks = self._blocks(row["technology"], row["version"])
            except StoreError:
                continue
            for i, block in enumerate(blocks, 1):
                p_title, p_url, body = parse_page(block)
                for sec in psg.sections(body, page_title=p_title, page_url=p_url):
                    weight = psg.score(sec, terms)
                    if weight <= 0:
                        continue
                    display = (f"{p_title} > {sec.heading_path}"
                               if sec.heading_path and p_title != sec.heading_path
                               else (sec.heading_path or p_title))
                    scored.append((weight, {
                        "technology": row["technology"], "version": row["version"],
                        "ordinal": i if len(blocks) > 1 else sec.ordinal,
                        "title": display, "heading_path": sec.heading_path,
                        "url": p_url,
                        "snippet": sec.text[:240].strip() + "…",
                    }))
        scored.sort(key=lambda pair: -pair[0])
        return [hit for _w, hit in scored[:limit]]

    def _search_substring(self, query: str, tech: str | None = None,
                          version: str | None = None,
                          limit: int = 30) -> list[dict]:
        """The query as an exact phrase. Ranking is not meaningful without an
        index, so results come back in store order."""
        needle = query.lower()
        hits: list[dict] = []
        for row in self._rows():
            if tech and row["technology"] != tech:
                continue
            if version and row["version"] != version:
                continue
            try:
                _, blocks = self._blocks(row["technology"], row["version"])
            except StoreError:
                continue
            if len(blocks) == 1:
                from docsforge.core import passages as psg
                p_title, p_url, body = parse_page(blocks[0])
                secs = psg.sections(body, page_title=p_title, page_url=p_url)
                for sec in secs:
                    if needle in sec.text.lower() or needle in sec.heading_path.lower():
                        where = sec.text.lower().find(needle)
                        start = max(0, where - 90) if where >= 0 else 0
                        display_title = (
                            f"{p_title} > {sec.heading_path}"
                            if sec.heading_path and p_title != sec.heading_path
                            else (sec.heading_path or p_title)
                        )
                        hits.append({
                            "technology": row["technology"], "version": row["version"],
                            "ordinal": sec.ordinal, "title": display_title,
                            "heading_path": sec.heading_path, "url": p_url,
                            "snippet": ("…" if start else "") + sec.text[start:start + 240].strip() + "…",
                        })
                        if len(hits) >= limit:
                            return hits
            else:
                for i, block in enumerate(blocks, 1):
                    if needle not in block.lower():
                        continue
                    title, url, body = parse_page(block)
                    where = body.lower().find(needle)
                    start = max(0, where - 90)
                    hits.append({
                        "technology": row["technology"], "version": row["version"],
                        "ordinal": i, "title": title, "url": url,
                        "snippet": ("…" if start else "") + body[start:start + 240].strip() + "…",
                    })
                    if len(hits) >= limit:
                        return hits
        return hits


class _FileWriter:
    """Streams one harvest into the file store, page by page.

    The Contents block at the top of the file lists every page, so it cannot be
    written until the last page has arrived. Pages therefore stream into a
    `.partial` file that no reader can see, and the finished file is assembled
    at settle time by writing the header and copying the partial across in
    chunks. Bounded memory, and byte-identical to writing it all at once.

    A crash leaves the `.partial` on disk, recoverable by hand. An orderly
    failure removes it. Either way the previously stored version is untouched,
    which is the property the old write had by accident and this one keeps on
    purpose.
    """

    #: Copied in blocks rather than read whole, so peak memory stays flat
    #: however large the harvest grows.
    COPY_CHUNK = 1 << 20

    def __init__(self, store, tech, version, source, strategy, expected):
        self.store = store
        self.tech, self.version = tech, version
        self.source, self.strategy = source, strategy
        self.expected = expected
        self.titles: list[str] = []
        self.urls: list[str] = []
        #: Pages the store itself refused. Empty for files, which refuse
        #: nothing — kept so both writers answer the same questions.
        self.rejected: list[tuple[str, str]] = []
        self._chars = 0

        folder = store.root / tech
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{slugify(version)}.md"
        self.partial = folder / f"{slugify(version)}.md.partial"
        self._handle = self.partial.open("w", encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.close()
        return False

    def add(self, title: str, url: str, body: str) -> bool:
        """Append one page, flushed before the next is fetched (Invariant 16)."""
        block = "\n" + "\n".join(
            ["", "---", "", f"## {title}", "", f"Source: <{url}>", "", body.strip()])
        self._handle.write(block)
        self._handle.flush()
        self.titles.append(title)
        self.urls.append(url)
        self._chars += len(block)
        return True

    def settle(self, complete, expected=None, version=None, strategy=None) -> dict:
        """Assemble the finished file and publish it.

        `version` may differ from the label the writer opened with. A harvest's
        real label depends on what it actually collected — a URL naming "2.11"
        is not honoured by an `llms.txt` published once for the whole site — and
        that is only knowable at the end. Renaming here is safe because nothing
        has been able to see this harvest until now.
        """
        if version and version != self.version:
            self.version = version
            self.path = self.path.parent / f"{slugify(version)}.md"
        if strategy:
            # Which strategy won is decided by the harvest, not by the caller
            # who opened the writer before it ran.
            self.strategy = strategy
        if self._handle is not None:
            self._handle.close()
            self._handle = None

        header = [f"# {self.tech} {self.version} documentation", "",
                  f"<!-- harvested: {len(self.titles)} pages | from: {self.source} | "
                  f"via: {self.strategy} | {time.strftime('%Y-%m-%d %H:%M')} -->",
                  "", "## Contents", ""]
        for i, (title, url) in enumerate(zip(self.titles, self.urls), 1):
            header.append(f"{i}. [{title}]({url})")
        header.append("")
        head = "\n".join(header)

        with self.path.open("w", encoding="utf-8") as out:
            out.write(head)
            if self.partial.exists():
                with self.partial.open("r", encoding="utf-8") as src:
                    while True:
                        chunk = src.read(self.COPY_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
            out.write("\n")
        self.partial.unlink(missing_ok=True)

        return self.store._finish(
            self.tech, self.version, self.source, self.strategy, self.path,
            len(head) + self._chars + 1, self.titles, complete,
            self.expected if expected is None else expected)

    def close(self) -> None:
        """Abandon an unsettled harvest. The stored version stays as it was."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self.partial.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# Postgres
# ─────────────────────────────────────────────────────────────
SCHEMA = """
create table if not exists technology (
    id    serial primary key,
    name  text unique not null
);

create table if not exists doc_version (
    id             serial primary key,
    technology_id  integer not null references technology(id) on delete cascade,
    version        text not null,
    source         text not null,
    strategy       text not null,
    -- Nullable on purpose: null means "nobody counted", which is not the same
    -- claim as "this is partial" and very much not the same as "this is whole".
    complete       boolean,
    -- How many pages discovery said existed, when discovery ran at all. This
    -- is what makes `complete` a measurement instead of an assertion.
    expected       integer,
    -- 'harvesting' while pages are streaming in, 'ready' once settled,
    -- 'failed' when a harvest was abandoned. Readers see only 'ready', which
    -- is what lets a new harvest be written alongside the one it will replace
    -- instead of deleting it first and hoping.
    state          text not null default 'ready',
    harvested_at   timestamptz not null default now()
);

create table if not exists page (
    id          bigserial primary key,
    version_id  integer not null references doc_version(id) on delete cascade,
    ordinal     integer not null,
    title       text not null,
    url         text not null,
    content     text not null,
    -- Generated, so it can never drift from the content it indexes. Titles are
    -- weighted above body text: a page called "Error Handling" should beat one
    -- that merely mentions errors.
    --
    -- `left(...)` is load-bearing, not defensive. A tsvector cannot exceed
    -- 1 MB, and because this column is GENERATED the vector is built during
    -- the INSERT — so an over-ceiling page is not a page that indexes badly,
    -- it is a page that cannot be stored at all. That is what `go.dev` hit.
    -- The bound makes every page storable; `section` below keeps the tail of
    -- an over-bound page searchable, so nothing is quietly dropped from the
    -- index either.
    search      tsvector generated always as (
                    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('english',
                              left(coalesce(content, ''), 300000)), 'B')
                ) stored
);

create index if not exists page_search_idx on page using gin (search);
create index if not exists page_version_idx on page (version_id, ordinal);

-- Only for pages longer than the index bound. Splitting every page would
-- double the store to buy nothing: an ordinary page is indexed whole. A
-- specification published as one 1.19 MB document is not ordinary, and its
-- last 900 KB would otherwise be stored but unfindable — an undisclosed
-- subset of the index, which is the failure mode this product exists to
-- refuse.
create table if not exists section (
    id           bigserial primary key,
    page_id      bigint not null references page(id) on delete cascade,
    ordinal      integer not null,
    heading_path text not null,
    content      text not null,
    search       tsvector generated always as (
                     setweight(to_tsvector('english', coalesce(heading_path, '')), 'A') ||
                     setweight(to_tsvector('english',
                               left(coalesce(content, ''), 300000)), 'B')
                 ) stored
);

create index if not exists section_search_idx on section using gin (search);
create index if not exists section_page_idx on section (page_id, ordinal);

-- Harvest status records, shared by every process that shares this store.
-- The JSON records under DOCSFORGE_HARVEST_STATE reach every process on one
-- machine; a serverless host gives each instance its own /tmp, so a harvest
-- that failed on one instance was invisible from the next -- harvest_status
-- listed it once and then reported that nothing had run. Status, not state:
-- nothing is ever resumed from a row, and one whose heartbeat stopped reads
-- as stalled. `updated` is the record's own clock, epoch seconds, so a reader
-- can tell the fresher of two copies of the same harvest without parsing.
create table if not exists harvest (
    id       text primary key,
    updated  double precision not null,
    record   jsonb not null
);
"""


#: How much of a page goes into its own full-text index. A tsvector cannot
#: exceed 1 MB, and `page.search` is a GENERATED column, so an unbounded index
#: expression makes an over-ceiling page unstorable rather than merely
#: unindexed — the `go.dev` failure exactly. 300,000 characters is far above
#: any ordinary documentation page (the Phase B sample ranged 1,254 to 31,687
#: median chars) and far below what could build a 1 MB vector. Anything past it
#: is indexed section by section instead; see `_PgWriter._index_tail`.
INDEX_CHARS = 300_000


#: Per-session limits, set on the connection so they apply wherever DocsForge
#: runs rather than depending on how the server was configured.
#:
#: `statement_timeout` bounds one query, and on a single-CPU managed plan that
#: is what stops one pathological full-text search pinning the processor while
#: every other tool call queues behind it — a database-wide stall presenting
#: as a dozen unrelated timeouts. Generous, because a GIN search across a
#: 2 MB corpus is legitimately slow; anything past it is not working.
#:
#: `idle_in_transaction_session_timeout` is the one that protects the
#: *connection* count. A transaction left open by a killed serverless
#: invocation holds its backend until something reaps it, and on a plan with
#: twenty backends a handful of those is the whole budget. The writer commits
#: per page and can legitimately pause between them, so this sits well above a
#: page fetch.
STATEMENT_TIMEOUT_MS = 30_000
IDLE_TX_TIMEOUT_MS = 60_000
SESSION_LIMITS = (f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                  f"-c idle_in_transaction_session_timeout={IDLE_TX_TIMEOUT_MS}")


class PostgresStore:
    kind = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn
        parsed = urlparse(dsn)
        self.location = f"{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
        self._ready = False
        #: The connection the current `session()` is serving from, per thread.
        self._local = threading.local()

    def _connect(self):
        try:
            import psycopg
        except ImportError as e:
            raise StoreError("Postgres storage needs: pip install psycopg[binary]") from e
        try:
            return psycopg.connect(self.dsn, connect_timeout=8,
                                   options=SESSION_LIMITS)
        except Exception as e:
            raise StoreError(self._why_unreachable(e)) from e

    @contextmanager
    def session(self):
        """Serve everything inside this block from one connection.

        Measured against Postgres: one `list_knowledge_base()` opened four
        connections, `read_knowledge_base()` three, `search_knowledge_base()`
        two — each operation opening and closing its own. That is fine against
        a database with connections to spare and is the binding constraint
        against a managed 1 GB plan allowing on the order of twenty backends,
        because a serverless host runs invocations concurrently and scales
        instances freely: ten concurrent searches were twenty connections, and
        `too many clients already` arrives as an unexplained tool failure.

        Deliberately *not* wrapped around every tool. A harvest spends its
        whole call fetching pages over the network, and holding a connection
        open across that is the problem this is meant to avoid, not a smaller
        version of it. The read tools are the ones that are short, frequent
        and database-bound, and they are the ones that open a session.

        Thread-local, because a connection is not safe to share between
        threads, and nestable, so a tool that calls another does not close a
        connection out from under its caller.
        """
        held = getattr(self._local, "cx", None)
        if held is not None:
            yield held                      # an outer session already owns it
            return
        cx = self._connect()
        self._local.cx = cx
        try:
            yield cx
        finally:
            self._local.cx = None
            try:
                cx.close()
            except Exception:               # noqa: BLE001
                pass

    @contextmanager
    def _borrow(self):
        """A connection for one operation: the session's, or its own.

        The session's is committed but not closed — it belongs to the block
        that opened it. Its own is both, which is what `with connect()` has
        always done here.
        """
        held = getattr(self._local, "cx", None)
        if held is not None:
            yield held
            held.commit()
            return
        with self._connect() as cx:
            yield cx

    def _why_unreachable(self, e: Exception) -> str:
        """Say what a failed connection actually means.

        "too many clients already" is not a DocsForge outage and not a bad
        DSN; it is the database's backend limit, and it arrives as a random
        tool failure unless it is named. A managed 1 GB plan allows on the
        order of twenty backends with some reserved, while a serverless host
        runs invocations concurrently and scales instances freely — so this is
        a capacity message, and it says which lever moves it.
        """
        text = str(e)
        if "too many clients" in text.lower() or "53300" in text:
            return (
                f"the DocsStore database ({self.location}) is at its "
                f"connection limit — every backend is in use. This is the "
                f"database's ceiling, not a DocsForge failure: point "
                f"DOCSFORGE_DB at the connection pooler's port if the plan "
                f"offers one (Aiven ships PgBouncer), or raise the plan's "
                f"limit. The call can simply be retried."
            )
        return f"cannot reach the DocsStore database ({self.location}): {e}"

    def migrate(self) -> None:
        if self._ready:
            return
        with self._borrow() as cx:
            self._upgrade_v1(cx)
            cx.execute(SCHEMA)
            self._upgrade_v2(cx)
            self._upgrade_v3(cx)
            self._upgrade_v4(cx)
            cx.commit()
        self._ready = True

    @staticmethod
    def _upgrade_v4(cx) -> None:
        """Bound the full-text index so that no page is unstorable.

        Until now `page.search` was generated over the whole of `content`, and
        a tsvector cannot exceed 1 MB. Because the column is generated, that
        ceiling was not an indexing limit but a *storage* limit: `go.dev`
        produced one 1.19 MB page and the INSERT failed. Under the batched
        writer that discarded the other ~1,200 pages with it; under the
        streaming writer it cost one page. Under this it costs nothing.

        Rebuilding a generated column rewrites the table, so this runs only
        where the old unbounded definition is still in place — which is also
        what keeps it from re-running on every startup.
        """
        row = cx.execute(
            "select generation_expression from information_schema.columns "
            "where table_name = 'page' and column_name = 'search'").fetchone()
        # Matched on the bound itself, not on the function name: Postgres
        # renders `left` as the quoted `"left"` because it is a reserved word,
        # so looking for `left(` never matches and this rebuilds the whole
        # table on every single startup. Found by the test that asserts the
        # column definition, which failed for exactly the same reason.
        if row and row[0] and str(INDEX_CHARS) in row[0]:
            return                              # already bounded
        if row:
            cx.execute("alter table page drop column search")
        cx.execute("""
            alter table page add column search tsvector generated always as (
                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('english',
                          left(coalesce(content, ''), 300000)), 'B')
            ) stored
        """)
        cx.execute("create index if not exists page_search_idx "
                   "on page using gin (search)")

    @staticmethod
    def _upgrade_v3(cx) -> None:
        """Let a harvest be written beside the version it will replace.

        v2 had `unique (technology_id, version)`, so the only way to re-harvest
        was to delete the stored version first and write the new one in the same
        transaction. That kept the old data safe on failure, but it also meant
        the whole harvest had to be held in memory and written at the end — and
        one rejected row discarded every good page with it.

        Replacing the constraint with a partial unique index over `state =
        'ready'` allows exactly one published version per `(technology,
        version)` while a second streams in behind it.
        """
        cx.execute("alter table doc_version add column if not exists "
                   "state text not null default 'ready'")
        cx.execute("alter table doc_version "
                   "drop constraint if exists doc_version_technology_id_version_key")
        cx.execute("create unique index if not exists doc_version_ready_idx "
                   "on doc_version (technology_id, version) where state = 'ready'")

    @staticmethod
    def _upgrade_v2(cx) -> None:
        """Let completeness be unknown, and record what discovery expected.

        v2 stored `complete boolean not null default true`, so every harvest
        that never counted anything claimed to be whole — the defect this
        column existed to warn about. Existing rows keep their value; only the
        ability to say "unknown" is added.
        """
        cx.execute("alter table doc_version add column if not exists expected integer")
        cx.execute("alter table doc_version alter column complete drop not null")
        cx.execute("alter table doc_version alter column complete drop default")

    @staticmethod
    def _upgrade_v1(cx) -> None:
        """Lift a pre-versioning store into the three-level schema.

        v1 hung pages straight off `technology` and made `name` unique, so a
        re-harvest overwrote what was there. Everything already stored becomes
        one version, labelled from its source URL — no harvest is lost.
        """
        old = cx.execute("""
            select 1 from information_schema.columns
             where table_name = 'page' and column_name = 'technology_id'
        """).fetchone()
        if not old:
            return

        cx.execute("""
            create table if not exists doc_version (
                id             serial primary key,
                technology_id  integer not null references technology(id) on delete cascade,
                version        text not null,
                source         text not null,
                strategy       text not null,
                complete       boolean not null default true,
                harvested_at   timestamptz not null default now(),
                unique (technology_id, version)
            )
        """)
        rows = cx.execute(
            "select id, source, strategy, complete, harvested_at from technology").fetchall()
        for tech_id, source, strategy, complete, harvested in rows:
            label = version_from_url(source or "")
            cx.execute(
                "insert into doc_version "
                "  (technology_id, version, source, strategy, complete, harvested_at) "
                "values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
                (tech_id, label, source or "", strategy or "crawl",
                 complete if complete is not None else True, harvested))

        cx.execute("alter table page add column if not exists version_id integer")
        cx.execute("""
            update page p set version_id = v.id
              from doc_version v
             where v.technology_id = p.technology_id and p.version_id is null
        """)
        cx.execute("delete from page where version_id is null")
        cx.execute("alter table page alter column version_id set not null")
        cx.execute("""
            alter table page add constraint page_version_fk
              foreign key (version_id) references doc_version(id) on delete cascade
        """)
        cx.execute("drop index if exists page_tech_idx")
        cx.execute("alter table page drop column technology_id")

        # The version columns now live on doc_version; leaving copies on
        # technology invites the two to disagree.
        for column in ("source", "strategy", "complete", "harvested_at"):
            cx.execute(f"alter table technology drop column if exists {column}")

    def available(self) -> bool:
        try:
            self.migrate()
            return True
        except StoreError:
            return False

    # -- writing ------------------------------------------------
    def save(self, tech, version, source, strategy, pages, complete,
             expected: int | None = None) -> dict:
        with self.writer(tech, version, source, strategy, expected) as w:
            for title, url, body in pages:
                w.add(title, url, body)
            return w.settle(complete=complete, expected=expected)

    def writer(self, tech, version, source, strategy, expected: int | None = None):
        """A writer that makes each page durable as it arrives."""
        return _PgWriter(self, tech, version, source, strategy, expected)

    def delete(self, tech: str, version: str | None = None) -> int:
        self.migrate()
        with self._borrow() as cx:
            if version is None:
                n = cx.execute("delete from technology where name = %s", (tech,)).rowcount
            else:
                n = cx.execute(
                    "delete from doc_version v using technology t "
                    " where v.technology_id = t.id and t.name = %s and v.version = %s",
                    (tech, version)).rowcount
            cx.commit()
        return n

    # -- harvest status -----------------------------------------
    # The store is the one thing every DocsForge on a database has in common,
    # which makes it the one place a harvest's status can be read from all of
    # them. `harvest_jobs` writes through these and never depends on them
    # succeeding: a status row that could not be written costs a stale line,
    # not a harvest.
    def publish_harvest(self, record: dict) -> None:
        from psycopg.types.json import Jsonb

        self.migrate()
        with self._borrow() as cx:
            cx.execute(
                "insert into harvest (id, updated, record) values (%s, %s, %s) "
                "on conflict (id) do update "
                "   set updated = excluded.updated, record = excluded.record",
                (str(record["id"]), float(record.get("updated") or 0.0), Jsonb(record)))
            cx.commit()

    def harvests(self) -> list[dict]:
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute("select record from harvest order by updated desc").fetchall()
        return [row[0] for row in rows]

    def forget_harvest(self, job_id: str) -> None:
        self.migrate()
        with self._borrow() as cx:
            cx.execute("delete from harvest where id = %s", (job_id,))
            cx.commit()

    # -- reading ------------------------------------------------
    def technologies(self, offset: int = 0, limit: int | None = None,
                     query: str = "") -> tuple[list[dict], int]:
        self.migrate()
        where, params = "", []
        if query:
            where = "where t.name ilike %s"
            params.append(f"%{query}%")

        sql = f"""
            select t.name,
                   count(distinct v.id),
                   count(p.id),
                   coalesce(sum(length(p.content)), 0),
                   to_char(max(v.harvested_at), 'YYYY-MM-DD HH24:MI'),
                   array_agg(v.complete),
                   array_agg(v.version order by v.harvested_at desc)
              from technology t
              left join doc_version v
                     on v.technology_id = t.id and v.state = 'ready'
              left join page p on p.version_id = v.id
              {where}
             group by t.id
            having count(v.id) > 0
             order by t.name
        """
        with self._borrow() as cx:
            rows = cx.execute(sql, params).fetchall()
            total = len(rows)
            if limit is not None:
                rows = rows[offset:offset + limit]
        # `latest` and `complete` are both computed here rather than in SQL:
        # version labels do not sort lexically (1.10 > 1.9), and completeness
        # is three-valued in a way `bool_and` cannot express.
        return [{
            "name": r[0], "versions": r[1], "pages": r[2], "characters": r[3],
            "harvested": r[4] or "",
            # A technology with no versions at all is vacuously whole; the
            # left join hands us [null] for it, which must not read as unknown.
            "complete": merge_complete(*(r[5] or [])) if r[1] else True,
            "latest": versions_mod.newest([v for v in (r[6] or []) if v]),
        } for r in rows], total

    def versions(self, tech: str) -> list[dict]:
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute("""
                select v.version, v.source, v.strategy, v.complete,
                       to_char(v.harvested_at, 'YYYY-MM-DD HH24:MI'),
                       count(p.id), coalesce(sum(length(p.content)), 0),
                       v.expected, extract(epoch from v.harvested_at)
                  from doc_version v
                  join technology t on t.id = v.technology_id
                  left join page p on p.version_id = v.id
                 where t.name = %s and v.state = 'ready'
                 group by v.id
                 order by v.harvested_at desc
            """, (tech,)).fetchall()
        if not rows:
            raise StoreError(f"nothing stored for {tech!r}")
        out = [{
            "technology": tech, "version": r[0], "source": r[1], "strategy": r[2],
            "complete": r[3], "harvested": r[4], "pages": r[5], "characters": r[6],
            "expected": r[7], "saved": float(r[8] or 0),
            "file": f"postgres://{self.location} ({tech} {r[0]})",
        } for r in rows]
        # Newest version first — `entry(tech, None)` takes the head of this
        # list, and "no version named" means "the current one", not "the one
        # that happened to be downloaded most recently".
        out.sort(key=lambda r: (versions_mod.sort_key(r["version"]), r["saved"]),
                 reverse=True)
        return out

    def entry(self, tech: str, version: str | None = None) -> dict | None:
        try:
            rows = self.versions(tech)
        except StoreError:
            return None
        if version is None:
            return rows[0]
        return next((r for r in rows if r["version"] == version), None)

    def _version_id(self, cx, tech: str, version: str | None) -> int:
        if version is None:
            # Naming no version means "the current one". Ordering by harvest
            # time answered a different question and got it wrong: Pydantic
            # 1.10 was crawled after 2.11, so every unqualified read returned
            # the older major. Rows arrive harvest-newest-first so that labels
            # carrying no ordering still break ties sensibly.
            rows = cx.execute(
                "select v.id, v.version from doc_version v "
                "  join technology t on t.id = v.technology_id "
                " where t.name = %s and v.state = 'ready' "
                " order by v.harvested_at desc", (tech,)).fetchall()
            row = max(rows, key=lambda r: versions_mod.sort_key(r[1])) if rows else None
        else:
            row = cx.execute(
                "select v.id from doc_version v join technology t on t.id = v.technology_id "
                " where t.name = %s and v.version = %s and v.state = 'ready'",
                (tech, version)).fetchone()
        if row is None:
            raise StoreError(f"no stored documentation for {tech!r}"
                             + (f" version {version!r}" if version else ""))
        return row[0]

    def pages(self, tech: str, version: str | None = None) -> list[dict]:
        self.migrate()
        with self._borrow() as cx:
            vid = self._version_id(cx, tech, version)
            rows = cx.execute(
                "select ordinal, title, url, length(content) from page "
                " where version_id = %s order by ordinal", (vid,)).fetchall()
        return [{"ordinal": r[0], "title": r[1], "url": r[2], "characters": r[3]} for r in rows]

    def page(self, tech: str, version: str | None, ordinal: int) -> dict:
        self.migrate()
        with self._borrow() as cx:
            vid = self._version_id(cx, tech, version)
            row = cx.execute(
                "select ordinal, title, url, content from page "
                " where version_id = %s and ordinal = %s", (vid, ordinal)).fetchone()
        if row is None:
            raise StoreError(f"{tech} has no page {ordinal}")
        return {"ordinal": row[0], "title": row[1], "url": row[2], "content": row[3]}

    def read(self, tech: str, section: str | None = None,
             version: str | None = None) -> tuple[str, str, int]:
        self.migrate()
        with self._borrow() as cx:
            vid = self._version_id(cx, tech, version)
            if not section:
                rows = cx.execute("select title, url, content from page where version_id = %s "
                                  "order by ordinal", (vid,)).fetchall()
                how = "all"
            else:
                rows = cx.execute(
                    "select title, url, content from page "
                    " where version_id = %s and title ilike %s order by ordinal",
                    (vid, f"%{section}%")).fetchall()
                how = "title"
                if not rows:
                    rows = cx.execute("""
                        select title, url, content from page
                         where version_id = %s
                           and search @@ websearch_to_tsquery('english', %s)
                         order by ts_rank(search, websearch_to_tsquery('english', %s)) desc
                         limit 40
                    """, (vid, section, section)).fetchall()
                    how = "content"
                if not rows:
                    raise StoreError(f"nothing in {tech} matches {section!r}")
        text = "\n\n".join(f"## {t}\n\nSource: <{u}>\n\n{c}".rstrip() for t, u, c in rows)
        return text, how, len(rows)

    def titles(self, tech: str, version: str | None = None) -> list[str]:
        try:
            return [p["title"] for p in self.pages(tech, version)][:2000]
        except StoreError:
            return []

    #: Every significant word must appear. Precise, and the right first
    #: question.
    _ALL_TERMS = "websearch_to_tsquery('english', %s)"

    #: Any of them, ranked, so the best-matching page wins. Built by putting
    #: the query through the same analyser the index used and OR-ing the
    #: lexemes it produces — stopwords are already gone by then, and nothing
    #: is interpolated, so this is exactly as safe as the query above.
    _ANY_TERM = ("to_tsquery('english', array_to_string("
                 "tsvector_to_array(to_tsvector('english', %s)), ' | '))")

    def search(self, query: str, tech: str | None = None, version: str | None = None,
               limit: int = 30) -> list[dict]:
        """Ranked search across the whole store, with a highlighted snippet
        showing why each page matched.

        Asked for every term and then, if that finds nothing, for any of them.

        `websearch_to_tsquery` conjoins: every significant word in the query
        has to appear in one page or section. That is right for a phrase and
        wrong for a question, and a question is how a model asks. Measured
        2026-09-10 against a 1,799-page `google-adk` corpus that certainly
        contains `LlmAgent`:

            search("In Google's ADK, what is the exact name of the class used
                    to build a simple LLM-backed agent?")
                -> Nothing stored in google-adk matches ...

        The corpus was fine, the retrieval was unreachable, and the answer
        DocsForge gave was *"try `learn_technology`"* — so a model following
        its advice re-harvests a site it already has. That is what a small
        model was measured doing: fifteen `learn_technology` calls in one run.

        Conjunction stays first because it is more precise; the fallback only
        runs when it returned nothing at all, so no query that worked before
        changes its answer."""
        self.migrate()
        narrow, scope = [], []
        if tech:
            narrow.append("and t.name = %s")
            scope.append(tech)
        if version:
            narrow.append("and v.version = %s")
            scope.append(version)
        clause = " ".join(narrow)
        # rank, match, section rank, section match, headline, then scope.
        params = [query] * 5 + scope + [limit]

        found = self._search_with(self._ALL_TERMS, clause, params)
        if not found:
            found = self._search_with(self._ANY_TERM, clause, params)
        return found

    def _search_with(self, tsquery: str, clause: str, params: list) -> list[dict]:
        """One pass of the search, with `tsquery` deciding how strict it is."""
        tsq = tsquery          # named for the f-string below

        # Two indexes, one result set. `page.search` covers the first 300,000
        # characters of every page; `section.search` covers what is past that
        # bound on the few pages long enough to have a past-that-bound. Without
        # the second half, bounding the page index would have traded a visible
        # failure — a page that could not be stored — for an invisible one: a
        # page stored whole and findable only by its opening. Sections exist to
        # be searched, so this is the read path that makes writing them mean
        # something.
        with self._borrow() as cx:
            rows = cx.execute(f"""
                with hit as (
                    select p.id, p.ordinal, p.title, p.url, p.version_id,
                           p.content as body,
                           ts_rank(p.search, {tsq}) as rank,
                           '' as heading_path
                      from page p
                     where p.search @@ {tsq}
                    union all
                    select p.id, p.ordinal, p.title, p.url, p.version_id,
                           s.content as body,
                           ts_rank(s.search, {tsq}) as rank,
                           s.heading_path as heading_path
                      from section s
                      join page p on p.id = s.page_id
                     where s.search @@ {tsq}
                )
                select t.name, v.version, h.ordinal, h.title, h.url,
                       ts_headline('english', h.body, {tsq},
                                   'MaxFragments=1, MinWords=6, MaxWords=18,
                                    StartSel=«, StopSel=»'),
                       max(h.rank) as rank,
                       h.heading_path,
                       (select count(*) from page p2 where p2.version_id = v.id) as page_count
                  from hit h
                  join doc_version v on v.id = h.version_id
                  join technology t on t.id = v.technology_id
                 where v.state = 'ready' {clause}
                 group by t.name, v.version, v.id, h.ordinal, h.title, h.url, h.body, h.heading_path
                 order by rank desc
                 limit %s
            """, params).fetchall()

        found, seen = [], set()
        for r in rows:
            tech_name, ver, ord_num, p_title, p_url, snippet, rank_val, heading_path, page_count = r
            if page_count == 1 and heading_path:
                display_title = f"{p_title} > {heading_path}" if p_title and p_title != heading_path else heading_path
            else:
                display_title = p_title

            # Always by URL. A single-page corpus can match through the page's
            # own index *and* through one of its sections — the `page` and
            # `section` branches of `hit` above are redundant coverage of the
            # same document, not two different documents — so keying on
            # `(url, heading_path)` let the page-level row (heading_path='')
            # and its own lead section (heading_path=title) survive as two
            # results for one match. Rows already arrive rank-ordered, so
            # deduping on the URL alone keeps the best-ranked one and still
            # shows its heading-qualified title.
            dedup_key = p_url

            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            found.append({
                "technology": tech_name, "version": ver, "ordinal": ord_num,
                "title": display_title, "heading_path": heading_path or "",
                "url": p_url, "snippet": snippet,
            })
        return found


class _PgWriter:
    """Streams one harvest into Postgres, page by page.

    Two properties the old batched write did not have, and both were paid for
    in a real 16-minute harvest of `go.dev` that stored nothing:

    * **A rejected page costs one page.** Each page is its own statement, so a
      row Postgres refuses — a document too large for a `tsvector`, most
      often — is recorded and skipped while the rest of the harvest proceeds.
    * **A page is durable before the next is fetched.** Committing per page
      costs a few milliseconds against a crawl spending far longer waiting on
      the network, and it means an interrupted harvest keeps what it had.

    Blue/green throughout: this writes a `state='harvesting'` version alongside
    whatever is published, and only the final `settle()` swaps them. Nothing
    that is already stored is at risk until the moment there is something
    complete to replace it with.
    """

    def __init__(self, store, tech, version, source, strategy, expected):
        store.migrate()
        self.store = store
        self.tech, self.version = tech, version
        self.source, self.strategy = source, strategy
        self.expected = expected
        self.titles: list[str] = []
        self.urls: list[str] = []
        self.rejected: list[tuple[str, str]] = []
        self._chars = 0
        self._n = 0

        self.cx = store._connect()
        self.tech_id = self.cx.execute(
            "insert into technology (name) values (%s) "
            "on conflict (name) do update set name = excluded.name returning id",
            (tech,)).fetchone()[0]
        # Clear any earlier attempt that never settled, so a retry does not
        # accumulate abandoned rows.
        self.cx.execute(
            "delete from doc_version where technology_id = %s and version = %s "
            "and state <> 'ready'", (self.tech_id, version))
        self.version_id = self.cx.execute(
            "insert into doc_version "
            "  (technology_id, version, source, strategy, complete, expected, state) "
            "values (%s, %s, %s, %s, %s, %s, 'harvesting') returning id",
            (self.tech_id, version, source, strategy, None, expected)).fetchone()[0]
        self.cx.commit()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.close()
        return False

    @staticmethod
    def _why(error: Exception) -> str:
        """Say what actually went wrong, in the caller's terms.

        The raw driver message for an oversized page is "string is too long for
        tsvector", which a reader reasonably but wrongly hears as "the
        documentation is too big for the database". It is neither the database
        nor the documentation: it is one page exceeding one index's ceiling.
        """
        text = str(error).strip().splitlines()[0] if str(error).strip() else repr(error)
        if "tsvector" in text.lower():
            return ("too large for the full-text index (a single page over "
                    "Postgres's 1 MB tsvector ceiling)")
        return text

    def add(self, title: str, url: str, body: str) -> bool:
        """Store one page. Returns False if the store refused it."""
        try:
            page_id = self.cx.execute(
                "insert into page (version_id, ordinal, title, url, content) "
                "values (%s, %s, %s, %s, %s) returning id",
                (self.version_id, self._n + 1, title, url, body)).fetchone()[0]
            if len(body) > INDEX_CHARS:
                self._index_tail(page_id, title, url, body)
            self.cx.commit()
        except Exception as e:                          # noqa: BLE001
            # The failed statement poisons the transaction, so it has to be
            # rolled back before the next page can be written.
            self.cx.rollback()
            self.rejected.append((url, self._why(e)))
            return False
        self._n += 1
        self.titles.append(title)
        self.urls.append(url)
        self._chars += len(body)
        return True

    def _index_tail(self, page_id: int, title: str, url: str, body: str) -> None:
        """Split an over-bound page so its tail stays searchable.

        The page itself is stored whole and is served whole; this only exists
        so that search can reach past `INDEX_CHARS`. Sections are the natural
        unit because they are already what read-time relevance returns, and a
        section carries its heading path, so a hit in the back half of a
        specification can still be cited rather than paraphrased.
        """
        from docsforge.core import passages as psg

        chunks = psg.sections(body, page_title=title, page_url=url)
        for i, chunk in enumerate(chunks, 1):
            self.cx.execute(
                "insert into section (page_id, ordinal, heading_path, content) "
                "values (%s, %s, %s, %s)",
                (page_id, i, chunk.heading_path or title, chunk.text))

    def settle(self, complete, expected=None, version=None, strategy=None) -> dict:
        """Publish this harvest, replacing the version it supersedes.

        `version` may differ from the label the writer opened with; see
        `_FileWriter.settle`. Renaming is safe because a `harvesting` row is
        invisible to every reader until this method flips it.
        """
        if expected is None:
            expected = self.expected
        if version and version != self.version:
            self.version = version
            self.cx.execute("update doc_version set version = %s where id = %s",
                            (version, self.version_id))
        if strategy:
            self.strategy = strategy
            self.cx.execute("update doc_version set strategy = %s where id = %s",
                            (strategy, self.version_id))
        self.cx.execute(
            "delete from doc_version where technology_id = %s and version = %s "
            "and state = 'ready'", (self.tech_id, self.version))
        self.cx.execute(
            "update doc_version set state = 'ready', complete = %s, expected = %s "
            "where id = %s", (complete, expected, self.version_id))
        self.cx.commit()
        self._done()
        return {
            "technology": self.tech, "version": self.version,
            "source": self.source, "strategy": self.strategy,
            "pages": self._n, "characters": self._chars,
            "file": f"postgres://{self.store.location} ({self.tech} {self.version})",
            "harvested": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
            "expected": expected, "titles": self.titles[:2000],
            "rejected": list(self.rejected),
        }

    def close(self) -> None:
        """Abandon an unsettled harvest, leaving the published one alone."""
        if self.cx is None:
            return
        try:
            self.cx.rollback()
            self.cx.execute("update doc_version set state = 'failed' where id = %s "
                            "and state = 'harvesting'", (self.version_id,))
            self.cx.commit()
        except Exception:                               # noqa: BLE001
            pass
        self._done()

    def _done(self) -> None:
        try:
            self.cx.close()
        except Exception:                               # noqa: BLE001
            pass
        self.cx = None


# ─────────────────────────────────────────────────────────────
def build_store(root: Path | str | None = None, dsn: str | None = None) -> Store:
    """Postgres when a DSN is configured and reachable, files otherwise.

    A store that fell back carries `degraded` — the DSN it could not reach and
    why. Falling back silently means everything you ever harvested appears to
    have vanished, with the interface calmly reporting an empty store.
    """
    dsn = dsn if dsn is not None else (
        os.environ.get("DOCSFORGE_DB") or os.environ.get("DATABASE_URL") or ""
    )
    problem = ""
    if dsn:
        store = PostgresStore(dsn)
        try:
            store.migrate()
            return store
        except StoreError as e:
            # A database that is down must not lose you a harvest: fall back to
            # files, but say so, and let the caller try again later.
            problem = str(e)

    # The repository root, not this module's folder: the store has always
    # been `knowledge_base/` beside `main.py`, and the modules moving into a
    # package must not move the data.
    from docsforge import ROOT
    files = FileStore(Path(root) if root else Path(
        os.environ.get("DOCSFORGE_KB_ROOT") or (ROOT / "knowledge_base")))
    files.degraded = problem
    files.wanted_dsn = dsn if problem else ""
    return files
