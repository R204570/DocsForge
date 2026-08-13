"""
Where harvested documentation lives.

Two backends behind one interface:

* **files** — one Markdown file per technology in `knowledge_base/`. Zero setup,
  works anywhere, and the file is the deliverable you can hand to anyone.
* **postgres** — a row per page. Worth it because the file backend answers
  `section=` by regex over a 6.7 MB string, which cannot rank results and gets
  slower with every harvest. Postgres does it with a GIN-indexed tsvector,
  ranked and fast, and lets several DocsForge instances share one store.

Postgres is used when DOCSFORGE_DB (or DATABASE_URL) is set; otherwise files.
Nothing else in the codebase needs to know which one is active.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

# A page written by docsforge.combine() looks like:
#     ## {title}
#
#     Source: <url>
#
# Scraped pages contain their own "## " headings, so the Source line is what
# actually marks a boundary.
_PAGE_BOUNDARY = re.compile(r"\n(?=## [^\n]*\n+Source: <)")
_PAGE_HEAD = re.compile(r"^## (?P<title>[^\n]*)\n+Source: <(?P<url>[^>]*)>\s*", re.S)


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


class StoreError(RuntimeError):
    """Something the caller can act on: no such entry, unreachable database."""


class Store(Protocol):
    kind: str
    location: str

    def save(self, slug: str, source: str, strategy: str,
             pages: list[tuple[str, str, str]], complete: bool) -> dict: ...
    def entries(self) -> list[dict]: ...
    def entry(self, slug: str) -> dict | None: ...
    def read(self, slug: str, section: str | None = None) -> tuple[str, str, int]: ...


# ─────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────
class FileStore:
    kind = "files"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.index_path = self.root / "index.json"
        self.location = str(self.root)

    # -- index --------------------------------------------------
    def _load(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self, index: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    # -- api ----------------------------------------------------
    def save(self, slug, source, strategy, pages, complete) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{slug}.md"

        host = urlparse(source).hostname or source
        out = [f"# {host} documentation", "",
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
        index[slug] = {
            "name": slug, "source": source, "strategy": strategy,
            "pages": len(pages), "characters": len(text), "file": str(path),
            "harvested": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
            "titles": [t for t, _, _ in pages][:1000],
        }
        self._save(index)
        return index[slug]

    def entries(self) -> list[dict]:
        return sorted(self._load().values(), key=lambda e: e["name"])

    def entry(self, slug: str) -> dict | None:
        return self._load().get(slug)

    def titles(self, slug: str) -> list[str]:
        meta = self.entry(slug)
        return list(meta.get("titles", [])) if meta else []

    def read(self, slug, section=None) -> tuple[str, str, int]:
        meta = self.entry(slug)
        if meta is None:
            raise StoreError(f"no stored documentation called {slug!r}")
        path = Path(meta["file"])
        if not path.exists():
            raise StoreError(f"{slug} is in the index but its file is missing: {path}")
        body = path.read_text(encoding="utf-8")
        if not section:
            return body, "all", meta["pages"]

        needle = section.lower()
        _, blocks = split_pages(body)
        hits = [b for b in blocks if needle in b.split("\n", 1)[0].lower()]
        how = "title"
        if not hits:
            hits = [b for b in blocks if needle in b.lower()]
            how = "content"
        if not hits:
            raise StoreError(f"nothing in {slug} matches {section!r}")
        return "\n\n".join(hits), how, len(hits)


# ─────────────────────────────────────────────────────────────
# Postgres
# ─────────────────────────────────────────────────────────────
SCHEMA = """
create table if not exists technology (
    id            serial primary key,
    name          text unique not null,
    source        text not null,
    strategy      text not null,
    complete      boolean not null default true,
    harvested_at  timestamptz not null default now()
);

create table if not exists page (
    id             bigserial primary key,
    technology_id  integer not null references technology(id) on delete cascade,
    ordinal        integer not null,
    title          text not null,
    url            text not null,
    content        text not null,
    -- Generated, so it can never drift from the content it indexes. Titles are
    -- weighted above body text: a page called "Error Handling" should beat one
    -- that merely mentions errors.
    search         tsvector generated always as (
                       setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                       setweight(to_tsvector('english', coalesce(content, '')), 'B')
                   ) stored
);

create index if not exists page_search_idx on page using gin (search);
create index if not exists page_tech_idx on page (technology_id, ordinal);
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
            raise StoreError(f"cannot reach the knowledge-base database ({self.location}): {e}") from e

    def migrate(self) -> None:
        if self._ready:
            return
        with self._connect() as cx:
            cx.execute(SCHEMA)
            cx.commit()
        self._ready = True

    def available(self) -> bool:
        try:
            self.migrate()
            return True
        except StoreError:
            return False

    def save(self, slug, source, strategy, pages, complete) -> dict:
        self.migrate()
        chars = sum(len(b) for _, _, b in pages)
        with self._connect() as cx:
            # Re-harvesting replaces the old copy rather than accumulating
            # duplicates; the cascade clears its pages.
            cx.execute("delete from technology where name = %s", (slug,))
            tech_id = cx.execute(
                "insert into technology (name, source, strategy, complete) "
                "values (%s, %s, %s, %s) returning id",
                (slug, source, strategy, complete),
            ).fetchone()[0]
            with cx.cursor().copy(
                "copy page (technology_id, ordinal, title, url, content) from stdin"
            ) as copy:
                for i, (title, url, body) in enumerate(pages, 1):
                    copy.write_row((tech_id, i, title, url, body))
            cx.commit()
        return {
            "name": slug, "source": source, "strategy": strategy,
            "pages": len(pages), "characters": chars,
            "file": f"postgres://{self.location} (technology {slug!r})",
            "harvested": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
            "titles": [t for t, _, _ in pages][:1000],
        }

    def entries(self) -> list[dict]:
        self.migrate()
        with self._connect() as cx:
            rows = cx.execute("""
                select t.name, t.source, t.strategy, t.complete,
                       to_char(t.harvested_at, 'YYYY-MM-DD HH24:MI'),
                       count(p.id), coalesce(sum(length(p.content)), 0)
                  from technology t
                  left join page p on p.technology_id = t.id
                 group by t.id
                 order by t.name
            """).fetchall()
        return [{
            "name": r[0], "source": r[1], "strategy": r[2], "complete": r[3],
            "harvested": r[4], "pages": r[5], "characters": r[6],
            "file": f"postgres://{self.location} (technology {r[0]!r})",
        } for r in rows]

    def entry(self, slug: str) -> dict | None:
        return next((e for e in self.entries() if e["name"] == slug), None)

    def titles(self, slug: str) -> list[str]:
        self.migrate()
        with self._connect() as cx:
            rows = cx.execute(
                "select p.title from page p join technology t on t.id = p.technology_id "
                " where t.name = %s order by p.ordinal limit 1000", (slug,)).fetchall()
        return [r[0] for r in rows]

    def read(self, slug, section=None) -> tuple[str, str, int]:
        self.migrate()
        with self._connect() as cx:
            row = cx.execute("select id, complete from technology where name = %s",
                             (slug,)).fetchone()
            if row is None:
                raise StoreError(f"no stored documentation called {slug!r}")
            tech_id = row[0]

            if not section:
                rows = cx.execute(
                    "select title, url, content from page where technology_id = %s "
                    "order by ordinal", (tech_id,)).fetchall()
                how = "all"
            else:
                # Title match first — a page named for the topic is what was
                # asked for. Only then rank the full text.
                rows = cx.execute(
                    "select title, url, content from page "
                    " where technology_id = %s and title ilike %s order by ordinal",
                    (tech_id, f"%{section}%")).fetchall()
                how = "title"
                if not rows:
                    rows = cx.execute("""
                        select title, url, content
                          from page
                         where technology_id = %s
                           and search @@ websearch_to_tsquery('english', %s)
                         order by ts_rank(search, websearch_to_tsquery('english', %s)) desc
                         limit 40
                    """, (tech_id, section, section)).fetchall()
                    how = "content"
                if not rows:
                    raise StoreError(f"nothing in {slug} matches {section!r}")

        text = "\n\n".join(
            f"## {t}\n\nSource: <{u}>\n\n{c}".rstrip() for t, u, c in rows
        )
        return text, how, len(rows)


# ─────────────────────────────────────────────────────────────
def build_store(root: Path | str | None = None, dsn: str | None = None) -> Store:
    """Postgres when a DSN is configured and reachable, files otherwise."""
    dsn = dsn if dsn is not None else (
        os.environ.get("DOCSFORGE_DB") or os.environ.get("DATABASE_URL") or ""
    )
    if dsn:
        store = PostgresStore(dsn)
        if store.available():
            return store
        # A misconfigured database must not lose you a harvest: fall back to
        # files and let the caller notice via store.kind.
    here = Path(__file__).resolve().parent
    return FileStore(Path(root) if root else Path(
        os.environ.get("DOCSFORGE_KB_ROOT") or (here / "knowledge_base")))
