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
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import versions as versions_mod

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
    def save(self, tech, version, source, strategy, pages, complete,
             expected: int | None = None) -> dict:
        folder = self.root / tech
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{slugify(version)}.md"

        out = [f"# {tech} {version} documentation", "",
               f"<!-- harvested: {len(pages)} pages | from: {source} | via: {strategy} | "
               f"{time.strftime('%Y-%m-%d %H:%M')} -->", "", "## Contents", ""]
        for i, (title, url, _) in enumerate(pages, 1):
            out.append(f"{i}. [{title}]({url})")
        out.append("")
        for title, url, body in pages:
            out += ["", "---", "", f"## {title}", "", f"Source: <{url}>", "", body.strip(), ""]
        text = "\n".join(out).rstrip() + "\n"
        path.write_text(text, encoding="utf-8")

        index = self._load()
        index[self._key(tech, version)] = {
            "technology": tech, "version": version, "source": source,
            "strategy": strategy, "pages": len(pages), "characters": len(text),
            "file": str(path), "harvested": time.strftime("%Y-%m-%d %H:%M"),
            # Displayed to the minute, ordered to the microsecond: two harvests
            # in the same minute still have a newest one, and "read the newest
            # version" has to agree with Postgres about which that is.
            "saved": time.time(),
            "complete": complete,
            "expected": expected,
            "titles": [t for t, _, _ in pages][:2000],
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
        """Substring search. Ranking is not meaningful without an index, so
        results come back in store order — Postgres is what makes this good."""
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
    harvested_at   timestamptz not null default now(),
    unique (technology_id, version)
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
    search      tsvector generated always as (
                    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('english', coalesce(content, '')), 'B')
                ) stored
);

create index if not exists page_search_idx on page using gin (search);
create index if not exists page_version_idx on page (version_id, ordinal);
"""


class PostgresStore:
    kind = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn
        parsed = urlparse(dsn)
        self.location = f"{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
        self._ready = False

    def _connect(self):
        try:
            import psycopg
        except ImportError as e:
            raise StoreError("Postgres storage needs: pip install psycopg[binary]") from e
        try:
            return psycopg.connect(self.dsn, connect_timeout=8)
        except Exception as e:
            raise StoreError(f"cannot reach the DocsStore database ({self.location}): {e}") from e

    def migrate(self) -> None:
        if self._ready:
            return
        with self._connect() as cx:
            self._upgrade_v1(cx)
            cx.execute(SCHEMA)
            self._upgrade_v2(cx)
            cx.commit()
        self._ready = True

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
        self.migrate()
        chars = sum(len(b) for _, _, b in pages)
        with self._connect() as cx:
            tech_id = cx.execute(
                "insert into technology (name) values (%s) "
                "on conflict (name) do update set name = excluded.name returning id",
                (tech,)).fetchone()[0]
            # Re-harvesting a version replaces that version and leaves the
            # others alone; the cascade clears its pages.
            cx.execute("delete from doc_version where technology_id = %s and version = %s",
                       (tech_id, version))
            version_id = cx.execute(
                "insert into doc_version "
                "  (technology_id, version, source, strategy, complete, expected) "
                "values (%s, %s, %s, %s, %s, %s) returning id",
                (tech_id, version, source, strategy, complete, expected)).fetchone()[0]
            with cx.cursor().copy(
                "copy page (version_id, ordinal, title, url, content) from stdin"
            ) as copy:
                for i, (title, url, body) in enumerate(pages, 1):
                    copy.write_row((version_id, i, title, url, body))
            cx.commit()
        return {
            "technology": tech, "version": version, "source": source,
            "strategy": strategy, "pages": len(pages), "characters": chars,
            "file": f"postgres://{self.location} ({tech} {version})",
            "harvested": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
            "expected": expected,
            "titles": [t for t, _, _ in pages][:2000],
        }

    def delete(self, tech: str, version: str | None = None) -> int:
        self.migrate()
        with self._connect() as cx:
            if version is None:
                n = cx.execute("delete from technology where name = %s", (tech,)).rowcount
            else:
                n = cx.execute(
                    "delete from doc_version v using technology t "
                    " where v.technology_id = t.id and t.name = %s and v.version = %s",
                    (tech, version)).rowcount
            cx.commit()
        return n

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
              left join doc_version v on v.technology_id = t.id
              left join page p on p.version_id = v.id
              {where}
             group by t.id
             order by t.name
        """
        with self._connect() as cx:
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
        with self._connect() as cx:
            rows = cx.execute("""
                select v.version, v.source, v.strategy, v.complete,
                       to_char(v.harvested_at, 'YYYY-MM-DD HH24:MI'),
                       count(p.id), coalesce(sum(length(p.content)), 0),
                       v.expected, extract(epoch from v.harvested_at)
                  from doc_version v
                  join technology t on t.id = v.technology_id
                  left join page p on p.version_id = v.id
                 where t.name = %s
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
            row = cx.execute(
                "select v.id from doc_version v join technology t on t.id = v.technology_id "
                " where t.name = %s order by v.harvested_at desc limit 1", (tech,)).fetchone()
        else:
            row = cx.execute(
                "select v.id from doc_version v join technology t on t.id = v.technology_id "
                " where t.name = %s and v.version = %s", (tech, version)).fetchone()
        if row is None:
            raise StoreError(f"no stored documentation for {tech!r}"
                             + (f" version {version!r}" if version else ""))
        return row[0]

    def pages(self, tech: str, version: str | None = None) -> list[dict]:
        self.migrate()
        with self._connect() as cx:
            vid = self._version_id(cx, tech, version)
            rows = cx.execute(
                "select ordinal, title, url, length(content) from page "
                " where version_id = %s order by ordinal", (vid,)).fetchall()
        return [{"ordinal": r[0], "title": r[1], "url": r[2], "characters": r[3]} for r in rows]

    def page(self, tech: str, version: str | None, ordinal: int) -> dict:
        self.migrate()
        with self._connect() as cx:
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
        with self._connect() as cx:
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

    def search(self, query: str, tech: str | None = None, version: str | None = None,
               limit: int = 30) -> list[dict]:
        """Ranked search across the whole store, with a highlighted snippet
        showing why each page matched."""
        self.migrate()
        narrow, scope = [], []
        if tech:
            narrow.append("and t.name = %s")
            scope.append(tech)
        if version:
            narrow.append("and v.version = %s")
            scope.append(version)
        clause = " ".join(narrow)
        params = [query, query, query] + scope + [limit]

        with self._connect() as cx:
            rows = cx.execute(f"""
                select t.name, v.version, p.ordinal, p.title, p.url,
                       -- One short fragment: the index is for choosing a page,
                       -- not for reading it. Long snippets push the next hit
                       -- off the screen.
                       ts_headline('english', p.content,
                                   websearch_to_tsquery('english', %s),
                                   'MaxFragments=1, MinWords=6, MaxWords=18,
                                    StartSel=«, StopSel=»'),
                       ts_rank(p.search, websearch_to_tsquery('english', %s)) as rank
                  from page p
                  join doc_version v on v.id = p.version_id
                  join technology t on t.id = v.technology_id
                 where p.search @@ websearch_to_tsquery('english', %s) {clause}
                 order by rank desc
                 limit %s
            """, params).fetchall()

        return [{
            "technology": r[0], "version": r[1], "ordinal": r[2],
            "title": r[3], "url": r[4], "snippet": r[5],
        } for r in rows]


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

    here = Path(__file__).resolve().parent
    files = FileStore(Path(root) if root else Path(
        os.environ.get("DOCSFORGE_KB_ROOT") or (here / "knowledge_base")))
    files.degraded = problem
    files.wanted_dsn = dsn if problem else ""
    return files
