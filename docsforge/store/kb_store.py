"""
Where harvested documentation lives — the DocsStore.

Three levels, because documentation has three levels:

    technology        effect
      version         v3, v2, or the harvest date when a site is unversioned
        page          Introduction, Error Handling, Layers, …

Keeping versions apart matters: a project's v2 and v3 docs contradict each
other, and a model handed both will happily quote the wrong one. Re-harvesting
a version you already have replaces that version and leaves the others alone;
the revision it replaces is kept, unread, for export by its `harvest_id`
(`keep_harvests`, and `System Files/Contract.md`).

Two backends behind one interface:

* **files** — `knowledge_base/<tech>/<version>.md`. Zero setup, and the file is
  a deliverable you can hand to anyone.
* **postgres** — a row per page with a GIN-indexed tsvector. Ranked search
  across everything stored, snippets showing why a page matched, and pagination
  that does not load the whole store to count it.

Postgres is used when DOCSFORGE_DB (or DATABASE_URL) is set; files otherwise.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from docsforge.core import versions as versions_mod
from docsforge.store import contract

# A page written by a combined file looks like:
#     ## {title}
#
#     Source: <url>
#     <!-- docsforge:page n=… -->        (contract v1 only)
_PAGE_BOUNDARY = re.compile(r"\n(?=## [^\n]*\n+Source: <)")
_PAGE_HEAD = re.compile(r"^## (?P<title>[^\n]*)\n+Source: <(?P<url>[^>]*)>\s*"
                        r"(?:<!-- docsforge:page [^\n]*-->\s*)?", re.S)

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


#: The `---` written *before* each page, which a split leaves at the end of
#: the page in front of it.
_TRAILING_SEPARATOR = re.compile(r"\n+---[ \t]*\n*\Z")


def split_pages(body: str) -> tuple[str, list[str]]:
    """Return (header, [page, ...]) for a combined knowledge-base file.

    Each page is written as `---`, then its heading, so splitting on headings
    leaves every page but the last carrying the next page's `---`. Read back,
    its body was its stored body plus `\\n\\n---` — not the page that was
    stored, not what Postgres returns for the same page, and carried into
    Postgres by `migrate_kb`. One trailing separator is removed from each page
    but the last; a page whose own text ends in a rule keeps that rule.

    A contract-v1 file is read by length instead (`contract.parse_revision`),
    so a page whose body looks like a page boundary stays one page (Issues
    B6); its pages come back in the same `## title / Source: / body` shape as
    a contract-0 page, without the page line. One that does not parse as v1 —
    edited by hand, say — falls back to the pattern below.
    """
    if contract.is_v1(body):
        try:
            revision = contract.parse_revision(body)
        except contract.ContractError:
            revision = None
        if revision is not None:
            head = body.split("\n---\n\n## ", 1)[0]
            return head, [f"## {p.title}\n\nSource: <{p.url}>\n\n{p.body}"
                          for p in revision.pages]
    parts = _PAGE_BOUNDARY.split(body)
    if len(parts) > 1:
        blocks = parts[1:]
        blocks = [_TRAILING_SEPARATOR.sub("", b, count=1) for b in blocks[:-1]] + blocks[-1:]
        return parts[0], blocks
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


def _default_key(row: dict) -> tuple:
    """`versions.default_key` for one stored row, from either store."""
    return versions_mod.default_key(row["version"], row.get("pinned"),
                                    row.get("saved", 0.0))


def _default_row(rows: list[dict]) -> dict:
    """The row a read that names no version gets."""
    return max(rows, key=_default_key)


def _latest(labels, pinned, saved) -> str:
    """`_default_row` over the parallel arrays a grouped query returns."""
    rows = [{"version": v, "pinned": p, "saved": float(t or 0)}
            for v, p, t in zip(labels or [], pinned or [], saved or []) if v]
    return _default_row(rows)["version"] if rows else ""


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
# What both stores agree on
# ─────────────────────────────────────────────────────────────
def new_harvest_id() -> str:
    """An id for one stored revision: the UTC second it was opened, plus
    eight random hex characters.

    Not the job id. `effect-1` is a handle for people to quote and it is handed
    out again once its status record is swept, so it cannot name a revision
    that outlives the record. Sortable and readable, with no dependency.
    """
    return f"hv-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"


def new_run_id() -> str:
    """An id for one tracked run: one tool call, which may publish several
    revisions (the entry corpus and the corpora federated with it). Same shape
    as `new_harvest_id`, and for the same reason never handed out twice."""
    return f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"


class RunContext:
    """The tracked run a writer belongs to, and who to tell when it publishes.

    Set by `harvest_jobs` inside the worker thread — threads do not inherit
    context variables, so setting it in the caller would reach no writer —
    and read once by each writer when it opens. Outside a tracked run there is
    none, and a revision simply carries no `run_id`.
    """

    __slots__ = ("run_id", "on_publish", "job")

    def __init__(self, run_id: str, on_publish=None, job: str | None = None):
        self.run_id = run_id
        self.on_publish = on_publish
        #: The short handle (`effect-1`), recorded in the frontmatter as `job`.
        self.job = job


CURRENT_RUN: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "docsforge_run", default=None)


def _report_published(run: RunContext | None, result: dict) -> None:
    """Tell the run that a revision was published. Never raises: a status
    record that could not be updated must not undo a publish that happened."""
    if run is None or run.on_publish is None:
        return
    try:
        run.on_publish(result)
    except Exception:                                   # noqa: BLE001
        pass


def clean_page(title: str, url: str, body: str) -> tuple[str, str, str]:
    """The one form a page is stored in, by either store.

    * A title is one line. `<title>` text keeps the newlines of the HTML it
      came from, and a title with a newline in it no longer matched the page
      boundary, so the page vanished into the one before it on read.
    * A URL cannot hold whitespace or `>`, which would end `Source: <…>`.
    * A body has LF line endings. Text-mode files on Windows wrote `\\r\\n` as
      `\\r\\r\\n` and read it back as two line breaks; elsewhere the carriage
      returns were dropped on the copy. Either way the page read back was not
      the page stored, and it differed by operating system and by store.
    """
    title = " ".join((title or "").split())
    url = re.sub(r"\s", "%20", (url or "").strip()).replace(">", "%3E")
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return title, url, body


def keep_harvests() -> int:
    """How many superseded revisions of one version to keep (contract v1,
    decision D2). A re-harvest used to delete the revision it replaced, so a
    reader holding its `harvest_id` held an id for nothing. Read per call, so
    a change takes effect without a restart; 0 keeps none, as before."""
    try:
        return max(0, int(os.environ.get("DOCSFORGE_KEEP_HARVESTS", "3")))
    except ValueError:
        return 3


def _settle_meta(writer, complete, expected, facts: dict | None,
                 version: str) -> dict:
    """The contract-v1 frontmatter for a revision being published.

    `facts` is what the harvest knows and the writer does not: how the count
    was made, how the version was established, the scope, the pages reached
    and not stored, and why `complete` is what it is. A caller that passes
    none (a script, a test) gets `count_basis: "stated"` when it gave a count
    and `"none"` when it did not — and `complete: true` only in the first
    case, because nothing is `true` because nobody counted (§5.6).
    """
    facts = facts or {}
    stored = len(writer.titles)
    basis = facts.get("count_basis") or ("stated" if expected is not None else "none")
    missing = contract.not_stored(
        unextractable=facts.get("unextractable") or [],
        refused_429=facts.get("refused") or [],
        rejected_by_store=[url for url, _ in writer.rejected],
        off_topic=facts.get("off_topic") or [],
        dead_404=facts.get("dead") or [],
    )
    complete, reason = contract.judge(complete, facts.get("reason"), basis,
                                      expected, stored, missing)
    run = writer.run
    return {
        "docsforge_contract": contract.CONTRACT,
        "harvest_id": writer.harvest_id,
        "run_id": writer.run_id,
        "job": getattr(run, "job", None) if run is not None else None,
        "technology": facts.get("technology") or writer.tech.split("--")[0],
        "corpus": writer.tech,
        "corpus_role": facts.get("corpus_role")
                       or ("federated" if "--" in writer.tech else "entry"),
        "version": version,
        "version_basis": facts.get("version_basis") or "unstated",
        "source": writer.source,
        "strategy": writer.strategy,
        "store": writer.store.kind,
        "scope": facts.get("scope"),
        "started_at": contract.utc(writer.started),
        "harvested_at": contract.utc(),
        "complete": complete,
        "complete_reason": reason,
        "count_basis": basis,
        "expected_pages": expected,
        "stored_pages": stored,
        "not_stored": missing,
        "generator": contract.GENERATOR,
    }


def _replace(src: Path, dst: Path, attempts: int = 60) -> None:
    """`os.replace`, retried while another process holds `dst` open.

    Atomic on every platform, which is the point: a reader sees the old file or
    the new one, never half of either. On Windows it is refused while a reader
    has the target open, and readers open, read and close, so waiting works.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * min(attempt + 1, 10))


#: How long a writer waits for another to finish publishing. Publishing is a
#: file copy and an index update — seconds at worst — so a wait this long
#: means something is stuck, and saying so beats waiting forever.
LOCK_TIMEOUT = 120.0

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_HELD = threading.local()


def _os_lock(handle, deadline: float, path: Path) -> None:
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise StoreError(
                    f"timed out after {LOCK_TIMEOUT:.0f}s waiting for another "
                    f"DocsForge process to release {path}") from None
            time.sleep(0.05)


def _os_unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _file_lock(path: Path, timeout: float | None = None):
    """Hold `path` exclusively against every other thread and process.

    The web app, a stdio MCP server per turn and the detached harvest worker
    are separate processes writing one `knowledge_base/`. A thread lock covers
    the threads of one of them; the OS lock covers the rest. Re-entrant within
    a thread, because reading the index can repair it, and the repair takes the
    same lock its caller may already hold.
    """
    key = os.path.normcase(str(path))
    held = getattr(_HELD, "depth", None)
    if held is None:
        held = _HELD.depth = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    with _LOCKS_GUARD:
        local = _THREAD_LOCKS.setdefault(key, threading.Lock())
    limit = LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    if not local.acquire(timeout=limit):
        raise StoreError(f"timed out after {limit:.0f}s waiting for {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as handle:
            _os_lock(handle, deadline, path)
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0
                _os_unlock(handle)
    finally:
        local.release()


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

    def footprint(self) -> dict:
        """Bytes on disk under the store root. Best effort, like Postgres's."""
        try:
            total = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
            return {"bytes": total}
        except Exception:                          # noqa: BLE001
            return {}

    # -- locks --------------------------------------------------
    def _index_lock(self):
        """Every read-modify-write of `index.json` happens under this."""
        return _file_lock(self.root / ".locks" / "index.lock")

    def _version_lock(self, tech: str, version: str):
        """Publishing one version happens under this.

        Keyed on the slug, not the label: two labels that slug alike would
        otherwise share a file without sharing a lock.
        """
        return _file_lock(self.root / ".locks" / f"{slugify(tech)}@{slugify(version)}.lock")

    # -- index --------------------------------------------------
    def _load(self, for_write: bool = False) -> dict:
        """The index, or a rebuilt one if it is damaged — never "empty".

        An index that will not parse used to read as an empty store, and the
        next harvest to settle wrote back an index holding only itself: every
        other technology stayed on disk and vanished from every listing. Now
        the damaged file is kept beside the store and the index is rebuilt
        from the Markdown files themselves.

        `for_write` is the caller about to write the index back. It must not
        be handed `{}` for an index it merely failed to read.
        """
        if not self.index_path.exists():
            return {}
        for attempt in range(5):
            try:
                text = self.index_path.read_text(encoding="utf-8")
                break
            except OSError as e:
                if attempt == 4:
                    if for_write:
                        raise StoreError(f"cannot read {self.index_path}: {e}") from e
                    return {}
                time.sleep(0.05 * (attempt + 1))
        try:
            data = json.loads(text)
        except ValueError:
            return self._recover_index()
        if not isinstance(data, dict):
            return self._recover_index()
        return self._upgrade_v1(data)

    def _recover_index(self) -> dict:
        """Rebuild a damaged `index.json` from the files it describes.

        What a contract-0 file says about itself is its header: pages, source,
        strategy, time and Contents. What it does not say is whether it was
        complete, so a recovered entry is `None` there — unknown, which is the
        honest answer, rather than a guess in either direction. The damaged
        index is kept, so nothing it held is lost to the repair.
        """
        rebuilt = self._scan_files()
        try:
            with self._index_lock():
                # Another process may have repaired it while this one waited.
                try:
                    data = json.loads(self.index_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return self._upgrade_v1(data)
                except (ValueError, OSError):
                    pass
                stamp = time.strftime("%Y%m%dT%H%M%S")
                try:
                    shutil.copyfile(self.index_path,
                                    self.root / f"index.json.damaged-{stamp}")
                except OSError:
                    pass
                self._save(rebuilt)
        except (OSError, StoreError):
            pass                                # read-only: usable in memory
        return rebuilt

    _HEADER = re.compile(r"<!-- harvested: (?P<pages>\d+) pages \| from: (?P<source>.*?) "
                         r"\| via: (?P<strategy>.*?) \| (?P<when>[\d-]+ [\d:]+) -->")

    def _scan_files(self) -> dict:
        """An index read off the Markdown files on disk. See `_recover_index`."""
        index: dict = {}
        for path in sorted(self.root.glob("*/*.md")):
            tech = path.parent.name
            if tech.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            head, blocks = split_pages(text)
            if contract.is_v1(text):
                # A contract-v1 file says everything the index held about it,
                # `complete` included, so nothing is lost to the rebuild.
                try:
                    front = contract.parse_frontmatter(text)
                except contract.ContractError:
                    front = {}
                if front.get("version"):
                    when = front.get("harvested_at") or ""
                    index[self._key(tech, front["version"])] = {
                        "technology": tech, "version": front["version"],
                        "source": front.get("source") or "",
                        "strategy": front.get("strategy") or "",
                        "pages": len(blocks), "characters": len(text), "file": str(path),
                        "harvested": when.replace("T", " ")[:16] or time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(mtime)),
                        "saved": mtime,
                        "complete": front.get("complete"),
                        "expected": front.get("expected_pages"),
                        "pinned": None,
                        "harvest_id": front.get("harvest_id"),
                        "run_id": front.get("run_id"),
                        "complete_reason": front.get("complete_reason"),
                        "count_basis": front.get("count_basis"),
                        "contract": contract.CONTRACT,
                        "titles": [parse_page(b)[0] for b in blocks][:2000],
                        "recovered": True,
                    }
                    continue
            meta = self._HEADER.search(head)
            first = head.lstrip().split("\n", 1)[0]
            named = re.match(rf"# {re.escape(tech)} (.+) documentation\s*$", first)
            version = named.group(1) if named else path.stem
            index[self._key(tech, version)] = {
                "technology": tech, "version": version,
                "source": meta.group("source") if meta else "",
                "strategy": meta.group("strategy") if meta else "",
                "pages": len(blocks), "characters": len(text), "file": str(path),
                "harvested": (meta.group("when") if meta
                              else time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))),
                "saved": mtime,
                "complete": None, "expected": None, "pinned": None,
                "titles": [parse_page(b)[0] for b in blocks][:2000],
                "recovered": True,
            }
        return index

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
            with self._index_lock():
                self._save(upgraded)
        except (OSError, StoreError):
            pass       # read-only checkout: still usable in memory
        return upgraded

    def _save(self, index: dict) -> None:
        """Write the index whole or not at all. Callers hold `_index_lock`.

        `write_text` truncates and then writes, so a reader could see half an
        index — and a half index read as an empty store. A temporary file
        renamed over the old one is never seen half-written.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_name(
            f"index.json.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(index, indent=2, sort_keys=True))
                out.flush()
                os.fsync(out.fileno())
            _replace(tmp, self.index_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _key(self, tech: str, version: str) -> str:
        return f"{tech}@{version}"

    def _path_for(self, tech: str, version: str, index: dict) -> Path:
        """Where this version's file lives, never shared with another label.

        The index is keyed on the label and the file on its slug, so `v3 beta`
        and `v3-beta` used to be two entries pointing at one file, each harvest
        overwriting the other. A label whose slug is taken gets a suffix.
        """
        folder = self.root / tech
        taken = {os.path.normcase(str(e.get("file", ""))) for e in index.values()
                 if isinstance(e, dict) and e.get("technology") == tech
                 and e.get("version") != version}
        mine = (index.get(self._key(tech, version)) or {}).get("file")
        if (mine and Path(mine).parent == folder
                and os.path.normcase(str(mine)) not in taken):
            return Path(mine)
        base = folder / f"{slugify(version)}.md"
        if os.path.normcase(str(base)) not in taken:
            return base
        digest = hashlib.sha1(version.encode("utf-8")).hexdigest()[:8]
        return folder / f"{slugify(version)}-{digest}.md"

    # -- writing ------------------------------------------------
    def writer(self, tech, version, source, strategy, expected: int | None = None,
               pinned: bool | None = None):
        """A writer that makes each page durable as it arrives.

        `pinned` is whether the caller asked for this version by name (True)
        or the harvest took the release the site presented as current
        (False). It decides which version a read that names none gets — see
        `versions.default_key`. None records nothing, as before.

        Blue/green: pages stream into a `.partial` file that no reader can see,
        and the real file is only replaced once the harvest settles. A crash
        therefore costs the new harvest and never the one already stored —
        which is the property the old delete-then-write transaction gave by
        accident, kept deliberately here.
        """
        return _FileWriter(self, tech, version, source, strategy, expected, pinned)

    def save(self, tech, version, source, strategy, pages, complete,
             expected: int | None = None, pinned: bool | None = None) -> dict:
        """Write a whole harvest at once. Delegates, so there is one write path.

        Kept because callers and tests use it, but it is now a thin wrapper: a
        batch save and a streamed one go through exactly the same code, so
        neither can drift from the other.
        """
        with self.writer(tech, version, source, strategy, expected, pinned) as w:
            for title, url, body in pages:
                w.add(title, url, body)
            return w.settle(complete=complete, expected=expected)

    def _finish(self, tech, version, source, strategy, path, text_len,
                titles, complete, expected, pinned=None, harvest_id=None,
                run_id=None, complete_reason=None, count_basis=None) -> dict:
        with self._index_lock():
            index = self._load(for_write=True)
            previous = index.get(self._key(tech, version)) or {}
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
                "pinned": versions_mod.still_pinned(pinned, previous.get("pinned")),
                # Which revision this is, and which run published it. Absent
                # from entries written before revisions had ids.
                "harvest_id": harvest_id,
                "run_id": run_id,
                "complete_reason": complete_reason,
                "count_basis": count_basis,
                "contract": contract.CONTRACT if harvest_id else 0,
                "titles": list(titles[:2000]),
            }
            self._save(index)
            return index[self._key(tech, version)]

    def revisions_of_run(self, run_id: str) -> list[dict]:
        """The stored revisions a run published, oldest first.

        How a status check tells "stopped, and nothing was stored" from
        "stopped after storing the main documentation": the process that died
        cannot say, but what it published carries its `run_id`.
        """
        if not run_id:
            return []
        rows = [e for e in self._load().values() if e.get("run_id") == run_id]
        return [{"technology": e["technology"], "version": e["version"],
                 "harvest_id": e.get("harvest_id"), "complete": e.get("complete"),
                 "stored_pages": e.get("pages"), "store": self.kind}
                for e in sorted(rows, key=lambda e: e.get("saved") or 0.0)]

    # -- contract v1 --------------------------------------------
    def export(self, tech: str, version: str | None = None,
               harvest_id: str | None = None) -> str:
        """The stored revision as a contract-v1 document.

        A v1 file *is* its export, byte for byte. A file written before
        contract v1 is rendered as one, with `legacy` provenance
        (`contract.legacy_meta`); the file itself is not changed.

        `harvest_id` picks a revision: the current one, or one a re-harvest
        superseded and `keep_harvests()` kept.
        """
        entry = self.entry(tech, version)
        if entry is None:
            raise StoreError(f"no stored documentation for {tech!r}"
                             + (f" version {version!r}" if version else ""))
        path = Path(entry["file"])
        if not path.exists():
            raise StoreError(f"{tech} is in the index but its file is missing: {path}")
        text = path.read_bytes().decode("utf-8")
        rendered = text if contract.is_v1(text) else self._legacy_render(entry, text)
        if not harvest_id or harvest_id == contract.parse_frontmatter(rendered).get("harvest_id"):
            return rendered
        kept = self._history_dir(path) / f"{slugify(harvest_id)}.md"
        if not kept.exists():
            raise StoreError(f"no revision {harvest_id} of {tech} {entry['version']} is "
                             f"stored; it was never stored here, or more than "
                             f"{keep_harvests()} newer revisions replaced it")
        old = kept.read_bytes().decode("utf-8")
        if contract.is_v1(old):
            return old
        head = self._HEADER.search(split_pages(old)[0])
        return self._legacy_render(
            {"technology": entry["technology"], "version": entry["version"],
             "source": head.group("source") if head else "",
             "strategy": head.group("strategy") if head else "",
             "complete": None, "expected": None, "saved": kept.stat().st_mtime}, old)

    # -- history ------------------------------------------------
    @staticmethod
    def _history_dir(path: Path) -> Path:
        """Where superseded revisions of the version published at `path` go:
        beside it, out of reach of the `*/*.md` scan that rebuilds the index."""
        return path.with_name(f"{path.stem}.history")

    @staticmethod
    def _revision_id_of(text: str) -> str:
        if contract.is_v1(text):
            held = contract.parse_frontmatter(text).get("harvest_id")
            if held:
                return held
        return "legacy-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def _archive(self, path: Path) -> None:
        """Keep the revision about to be replaced at `path`, under its id.

        A hard link where the filesystem has them: no copy, and it survives
        the rename that replaces the published file. Done before that rename,
        so a crash between the two leaves a duplicate, never a gap.
        """
        if keep_harvests() <= 0 or not path.exists():
            return
        text = path.read_bytes().decode("utf-8")
        folder = self._history_dir(path)
        folder.mkdir(exist_ok=True)
        kept = folder / f"{slugify(self._revision_id_of(text))}.md"
        if kept.exists():
            return
        try:
            os.link(path, kept)
        except OSError:
            shutil.copy2(path, kept)

    def _prune_history(self, path: Path) -> None:
        """Keep the newest `keep_harvests()` superseded revisions."""
        folder = self._history_dir(path)
        if not folder.exists():
            return
        # Newest first: by when the revision was written (a hard link keeps
        # it), then by id, whose timestamp breaks a tie within one second.
        kept = sorted(folder.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name),
                      reverse=True)
        for old in kept[keep_harvests():]:
            try:
                old.unlink()
            except OSError:
                pass                        # held open by a reader; next settle retries

    def history(self, tech: str, version: str) -> list[dict]:
        """Every kept revision of one version, newest first; the first is the
        one readers see."""
        entry = self.entry(tech, version)
        if entry is None:
            raise StoreError(f"{tech} has no version {version!r}")
        path = Path(entry["file"])
        files = [path] + sorted(self._history_dir(path).glob("*.md"),
                                key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        out = []
        for i, f in enumerate(files):
            try:
                text = f.read_bytes().decode("utf-8")
            except OSError:
                continue
            front = contract.parse_frontmatter(text) if contract.is_v1(text) else {}
            out.append({
                "harvest_id": self._revision_id_of(text),
                "harvested_at": front.get("harvested_at") or contract.utc(f.stat().st_mtime),
                "complete": front.get("complete") if front else (
                    entry.get("complete") if i == 0 else None),
                "current": i == 0,
                "pages": front.get("stored_pages") if front else len(split_pages(text)[1]),
            })
        return out

    def _legacy_render(self, entry: dict, text: str) -> str:
        _, blocks = split_pages(text)
        pages = [parse_page(b) for b in blocks]
        saved = entry.get("saved")
        meta = contract.legacy_meta(
            harvest_id="legacy-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            technology=entry["technology"], version=entry["version"],
            source=entry.get("source") or "", strategy=entry.get("strategy") or "",
            store=self.kind, complete=entry.get("complete"),
            expected=entry.get("expected"), stored=len(pages),
            harvested_at=contract.utc(saved) if saved else None)
        return contract.render_revision(meta, [(t, u, b, None) for t, u, b in pages])

    def upgrade(self) -> list[tuple[str, str, str]]:
        """Rewrite every contract-0 file as contract v1 (`--upgrade-store`).

        Each file becomes its own export: frontmatter with `legacy`
        provenance, and a page line per page with its hash and `captured_at:
        null`, since nobody recorded when it was fetched. A `complete: true`
        with nothing counted becomes `null` in the file and the index alike
        (decision D3). Idempotent — a v1 file is left alone — and the index is
        copied to `index.json.bak` first.
        """
        with self._index_lock():
            if self.index_path.exists():
                shutil.copyfile(self.index_path, self.root / "index.json.bak")
            entries = list(self._load(for_write=True).items())
        done: list[tuple[str, str, str]] = []
        for key, entry in entries:
            tech, version = entry["technology"], entry["version"]
            with self._version_lock(tech, version):
                path = Path(entry["file"])
                if not path.exists():
                    done.append((tech, version, f"skipped: its file is missing ({path})"))
                    continue
                text = path.read_bytes().decode("utf-8")
                if contract.is_v1(text):
                    continue
                rendered = self._legacy_render(entry, text)
                tmp = path.with_name(f"{path.name}.upgrade.tmp")
                try:
                    with tmp.open("w", encoding="utf-8", newline="\n") as out:
                        out.write(rendered)
                        out.flush()
                        os.fsync(out.fileno())
                    _replace(tmp, path)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                front = contract.parse_frontmatter(rendered)
                with self._index_lock():
                    index = self._load(for_write=True)
                    if key in index:
                        index[key].update(
                            harvest_id=front["harvest_id"], contract=contract.CONTRACT,
                            complete=front["complete"],
                            complete_reason=front["complete_reason"],
                            count_basis=front["count_basis"], characters=len(rendered))
                        self._save(index)
                done.append((tech, version, f"upgraded, {front['stored_pages']} pages"))
        return done

    def delete(self, tech: str, version: str | None = None) -> int:
        with self._index_lock():
            index = self._load(for_write=True)
            doomed = [k for k, v in index.items()
                      if v["technology"] == tech and (version is None or v["version"] == version)]
            for key in doomed:
                path = Path(index[key]["file"])
                if path.exists():
                    path.unlink()
                # Forgetting a version forgets its history too.
                shutil.rmtree(self._history_dir(path), ignore_errors=True)
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
                "complete": True, "saved": 0.0, "_rows": [],
            })
            tech["versions"] += 1
            tech["pages"] += row["pages"]
            tech["characters"] += row["characters"]
            tech["complete"] = merge_complete(tech["complete"], row.get("complete"))
            tech["_rows"].append(row)
            if row.get("saved", 0) >= tech["saved"]:
                tech["saved"] = row.get("saved", 0)
                tech["harvested"] = row["harvested"]

        # "latest" is the version a read naming none would get — the current
        # release, not the newest download and not the highest pinned number.
        for tech in grouped.values():
            tech["latest"] = _default_row(tech.pop("_rows"))["version"]

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
        # The version a read naming none gets comes first — the current
        # release, not the most recently harvested and not the highest number
        # someone pinned (`versions.default_key`).
        rows.sort(key=_default_key, reverse=True)
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

    Each writer has a partial of its own, named by its harvest id, and the
    finished file is written beside the published one and renamed over it.
    Two harvests of one version used to share `<version>.md.partial`, so their
    pages interleaved; and the published file was truncated and rewritten in
    place, so a crash part-way through destroyed the version it was replacing.
    Now the published version is untouched until the instant a whole new one
    replaces it, and two harvests of one version each publish a whole file —
    the later one wins, and neither is ever mixed with the other.

    A crash leaves the partial in `<tech>/.incoming/`, recoverable by hand. An
    orderly failure removes it.
    """

    #: Copied in blocks rather than read whole, so peak memory stays flat
    #: however large the harvest grows.
    COPY_CHUNK = 1 << 20

    def __init__(self, store, tech, version, source, strategy, expected, pinned=None):
        self.store = store
        self.tech, self.version = tech, version
        self.source, self.strategy = source, strategy
        self.expected = expected
        self.pinned = pinned
        self.harvest_id = new_harvest_id()
        self.run = CURRENT_RUN.get()
        self.run_id = self.run.run_id if self.run else None
        self.started = time.time()
        self.titles: list[str] = []
        self.urls: list[str] = []
        #: Pages the store itself refused. Empty for files, which refuse
        #: nothing — kept so both writers answer the same questions.
        self.rejected: list[tuple[str, str]] = []
        self._chars = 0
        self._settled = False

        folder = store.root / tech
        incoming = folder / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        #: Where it will probably be published; `settle()` decides for certain.
        self.path = folder / f"{slugify(version)}.md"
        self.partial = incoming / f"{slugify(version)}.{self.harvest_id}.partial"
        self._handle = self.partial.open("w", encoding="utf-8", newline="\n")

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.close()
        return False

    def add(self, title: str, url: str, body: str, captured_at: str | None = None) -> bool:
        """Append one page, flushed before the next is fetched (Invariant 16).

        Written as a contract-v1 page block: its page line carries the number,
        `captured_at` (now, unless the caller knows when it was fetched), the
        byte length and the SHA-256 of exactly the body written.
        """
        title, url, body = clean_page(title, url, body)
        block = contract.render_page(len(self.titles) + 1, title, url, body,
                                     captured_at or contract.utc())
        self._handle.write(block)
        self._handle.flush()
        self.titles.append(title)
        self.urls.append(url)
        self._chars += len(block)
        return True

    def settle(self, complete, expected=None, version=None, strategy=None,
               facts: dict | None = None) -> dict:
        """Assemble the finished file and publish it.

        `version` may differ from the label the writer opened with. A harvest's
        real label depends on what it actually collected — a URL naming "2.11"
        is not honoured by an `llms.txt` published once for the whole site — and
        that is only knowable at the end. Renaming here is safe because nothing
        has been able to see this harvest until now.

        `complete` is judged against what was stored and how it was counted
        (`contract.judge`, §5.6): the result carries the value actually stored
        and its `complete_reason`, and the file carries both in its frontmatter
        (`_settle_meta`, which also says what `facts` holds).
        """
        if version and version != self.version:
            self.version = version
        if strategy:
            # Which strategy won is decided by the harvest, not by the caller
            # who opened the writer before it ran.
            self.strategy = strategy
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        expected = self.expected if expected is None else expected
        meta = _settle_meta(self, complete, expected, facts, self.version)
        complete, reason = meta["complete"], meta["complete_reason"]
        head = contract.render_header(meta, self.titles, self.urls)

        # One publisher per version at a time, and the index updated inside the
        # same hold: otherwise two settles could leave one's file under the
        # other's index entry.
        with self.store._version_lock(self.tech, self.version):
            self.path = self.store._path_for(self.tech, self.version,
                                             self.store._load(for_write=True))
            tmp = self.path.with_name(f"{self.path.name}.{self.harvest_id}.tmp")
            try:
                with tmp.open("w", encoding="utf-8", newline="\n") as out:
                    out.write(head)
                    with self.partial.open("r", encoding="utf-8", newline="") as src:
                        while True:
                            chunk = src.read(self.COPY_CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                self.store._archive(self.path)
                _replace(tmp, self.path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self.store._prune_history(self.path)
            entry = self.store._finish(
                self.tech, self.version, self.source, self.strategy, self.path,
                len(head) + self._chars, self.titles, complete,
                expected, self.pinned, self.harvest_id, self.run_id,
                complete_reason=reason, count_basis=meta["count_basis"])
        self._settled = True
        self.partial.unlink(missing_ok=True)
        result = dict(entry, stored_pages=len(self.titles),
                      complete_reason=reason, rejected=list(self.rejected),
                      store=self.store.kind, meta=meta,
                      # Set when a database is configured but was unreachable,
                      # so this went to files instead (B9): once the database
                      # is back, readers stop seeing it.
                      fallback=bool(getattr(self.store, "wanted_dsn", "")))
        _report_published(self.run, result)
        return result

    def close(self) -> None:
        """Abandon an unsettled harvest. The stored version stays as it was."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if not self._settled:
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
            self._upgrade_v5(cx)
            self._upgrade_v6(cx)
            self._upgrade_v7(cx)
            self._upgrade_v8(cx)
            cx.commit()
        self._ready = True

    @staticmethod
    def _upgrade_v8(cx) -> None:
        """Contract v1: what a revision says about itself, and per-page hashes.

        `meta` holds the revision's frontmatter exactly as the file store
        writes it, so an export renders the same document from either store.
        `captured_at` is kept as the ISO string the frontmatter uses, so it
        renders back byte for byte. All null on rows written before this;
        their export says `legacy` (`contract.legacy_meta`).
        """
        cx.execute("alter table doc_version add column if not exists meta jsonb")
        cx.execute("alter table page add column if not exists captured_at text")
        cx.execute("alter table page add column if not exists sha256 text")

    @staticmethod
    def _upgrade_v7(cx) -> None:
        """Give every revision an id of its own, and the run that published it.

        `harvest_id` names one stored revision for good: the job id (`effect-1`)
        is handed out again once its status record is swept, so it cannot name
        anything that outlives the record. `run_id` groups the revisions one
        tool call published, which is how a status check finds what a run that
        died had already stored. Null on rows written before this.
        """
        cx.execute("alter table doc_version add column if not exists harvest_id text")
        cx.execute("alter table doc_version add column if not exists run_id text")
        cx.execute("create unique index if not exists doc_version_harvest_id_idx "
                   "on doc_version (harvest_id) where harvest_id is not null")
        cx.execute("create index if not exists doc_version_run_id_idx "
                   "on doc_version (run_id) where run_id is not null")

    @staticmethod
    def _upgrade_v6(cx) -> None:
        """Give an in-flight harvest a pulse another writer can check.

        A writer used to clear every unsettled row for its version before
        starting, on the theory that any such row was an abandoned attempt. A
        harvest still running under that label was not abandoned: its row and
        its pages went, every page it wrote after that was refused, and when it
        settled it deleted the published version and published nothing. Now
        only a row whose heartbeat has stopped is treated as abandoned.

        Null for rows written before this; their `harvested_at` (when they
        opened) stands in for it.
        """
        cx.execute("alter table doc_version add column if not exists "
                   "heartbeat_at timestamptz")

    @staticmethod
    def _upgrade_v5(cx) -> None:
        """Record whether a version was asked for or found as current.

        Null for every row written before this: the store never kept it, and
        guessing would be the failure this column exists to stop (`Issues.md`
        V3). Those rows keep the label ordering they always had.
        """
        cx.execute("alter table doc_version add column if not exists pinned boolean")

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
             expected: int | None = None, pinned: bool | None = None) -> dict:
        with self.writer(tech, version, source, strategy, expected, pinned) as w:
            for title, url, body in pages:
                w.add(title, url, body)
            return w.settle(complete=complete, expected=expected)

    def writer(self, tech, version, source, strategy, expected: int | None = None,
               pinned: bool | None = None, harvest_id: str | None = None,
               run_id: str | None = None):
        """A writer that makes each page durable as it arrives.

        `harvest_id` and `run_id` keep the ids of a revision copied in from
        elsewhere (`sync_store`); a new harvest mints its own."""
        return _PgWriter(self, tech, version, source, strategy, expected, pinned,
                         harvest_id=harvest_id, run_id=run_id)

    def delete(self, tech: str, version: str | None = None) -> int:
        """Remove a technology or one version of it; how many versions went.

        Versions, whichever way it was asked, because that is what the file
        store counts and what the tool reports: deleting a whole technology
        used to return the `technology` rowcount, so two versions of
        `django` were reported as "1 version(s)" (bench-2, Issues.md V4).

        Only 'ready' versions count, as everywhere else a version is read: an
        abandoned or in-flight harvest goes with the rest, but it is not a
        version anyone could have read, and counting it put "3 version(s)"
        beside the pages of two.
        """
        self.migrate()
        with self._borrow() as cx:
            if version is None:
                n = cx.execute(
                    "select count(*) from doc_version v join technology t on t.id = v.technology_id "
                    " where t.name = %s and v.state = 'ready'", (tech,)).fetchone()[0]
                cx.execute("delete from technology where name = %s", (tech,))
            else:
                states = cx.execute(
                    "delete from doc_version v using technology t "
                    " where v.technology_id = t.id and t.name = %s and v.version = %s "
                    "returning v.state", (tech, version)).fetchall()
                n = sum(state == "ready" for (state,) in states)
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

    def claim_harvest(self, record: dict) -> bool:
        """Take a job id, or say it is taken. Two instances picking ids at the
        same moment both saw it free; only one insert can win."""
        from psycopg.types.json import Jsonb

        self.migrate()
        with self._borrow() as cx:
            won = cx.execute(
                "insert into harvest (id, updated, record) values (%s, %s, %s) "
                "on conflict (id) do nothing returning id",
                (str(record["id"]), float(record.get("updated") or 0.0),
                 Jsonb(record))).fetchone()
            cx.commit()
        return won is not None

    def harvests(self) -> list[dict]:
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute("select record from harvest order by updated desc").fetchall()
        return [row[0] for row in rows]

    def revisions_of_run(self, run_id: str) -> list[dict]:
        """The published revisions carrying `run_id`, oldest first. See
        `FileStore.revisions_of_run`."""
        if not run_id:
            return []
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute(
                "select t.name, v.version, v.harvest_id, v.complete, "
                "       (select count(*) from page p where p.version_id = v.id) "
                "from doc_version v join technology t on t.id = v.technology_id "
                "where v.run_id = %s and v.state = 'ready' order by v.id",
                (run_id,)).fetchall()
        return [{"technology": r[0], "version": r[1], "harvest_id": r[2],
                 "complete": r[3], "stored_pages": r[4], "store": self.kind}
                for r in rows]

    def export(self, tech: str, version: str | None = None,
               harvest_id: str | None = None) -> str:
        """The stored revision as a contract-v1 document — the same renderer,
        and so the same document, as the file store's. A row written before
        contract v1 is rendered with `legacy` provenance (`contract.legacy_meta`)."""
        self.migrate()
        with self._borrow() as cx:
            vid = (self._revision_id(cx, tech, version, harvest_id) if harvest_id
                   else self._version_id(cx, tech, version))
            row = cx.execute(
                "select v.version, v.source, v.strategy, v.complete, v.expected, "
                "       v.harvest_id, v.meta, v.harvested_at "
                "  from doc_version v where v.id = %s", (vid,)).fetchone()
            pages = cx.execute(
                "select title, url, content, captured_at, sha256 from page "
                " where version_id = %s order by ordinal", (vid,)).fetchall()
        label, source, strategy, complete, expected, held, meta, when = row
        if meta is None:
            held = held or f"legacy-{vid}"
            meta = contract.legacy_meta(
                harvest_id=held, technology=tech, version=label, source=source,
                strategy=strategy, store=self.kind, complete=complete,
                expected=expected, stored=len(pages),
                harvested_at=contract.utc(when.timestamp()) if when else None)
        return contract.render_revision(meta, [tuple(p) for p in pages])

    def _revision_id(self, cx, tech: str, version: str | None, harvest_id: str) -> int:
        """The row of one revision — current or superseded — by its id.
        `legacy-<n>` names a row written before revisions had ids."""
        legacy = re.fullmatch(r"legacy-(\d+)", harvest_id)
        row = cx.execute(
            "select v.id, v.version from doc_version v "
            "join technology t on t.id = v.technology_id "
            "where t.name = %s and v.state in ('ready', 'superseded') and "
            + ("v.id = %s and v.harvest_id is null" if legacy else "v.harvest_id = %s"),
            (tech, int(legacy.group(1)) if legacy else harvest_id)).fetchone()
        if row is None or (version is not None and row[1] != version):
            raise StoreError(f"no revision {harvest_id} of {tech}"
                             + (f" {version}" if version else "")
                             + f" is stored; it was never stored here, or more than "
                               f"{keep_harvests()} newer revisions replaced it")
        return row[0]

    def history(self, tech: str, version: str) -> list[dict]:
        """Every kept revision of one version, newest first; the first is the
        one readers see."""
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute(
                "select v.id, v.harvest_id, v.meta ->> 'harvested_at', v.harvested_at, "
                "       v.complete, v.state, "
                "       (select count(*) from page p where p.version_id = v.id) "
                "  from doc_version v join technology t on t.id = v.technology_id "
                " where t.name = %s and v.version = %s "
                "   and v.state in ('ready', 'superseded') "
                " order by (v.state = 'ready') desc, v.id desc", (tech, version)).fetchall()
        return [{"harvest_id": r[1] or f"legacy-{r[0]}",
                 "harvested_at": r[2] or (contract.utc(r[3].timestamp()) if r[3] else None),
                 "complete": r[4], "current": r[5] == "ready", "pages": r[6]}
                for r in rows]

    def forget_harvest(self, job_id: str) -> None:
        self.migrate()
        with self._borrow() as cx:
            cx.execute("delete from harvest where id = %s", (job_id,))
            cx.commit()

    # -- capacity -----------------------------------------------
    def footprint(self) -> dict:
        """How much room the store is using, and how much is left.

        A plan runs out of disk long before anyone thinks to look, and the
        first symptom is a harvest failing at the very end, after all the
        crawling is done. The GIN index is the part that grows: Postgres
        TOASTs and compresses text columns over 2 KB, so a megabyte of
        Markdown lands well under a megabyte on disk while its index does not.

        `pg_database_size` is the honest total — it counts indexes, TOAST and
        bloat, which summing `length(content)` does not. Best effort: a
        database that will not answer this still answers everything else, and
        a store that cannot report its size must not fail the listing.
        """
        self.migrate()
        try:
            with self._borrow() as cx:
                total = cx.execute("select pg_database_size(current_database())"
                                   ).fetchone()[0]
                indexes = cx.execute(
                    "select coalesce(sum(pg_total_relation_size(indexrelid)), 0) "
                    "from pg_stat_user_indexes").fetchone()[0]
            return {"bytes": int(total), "index_bytes": int(indexes)}
        except Exception:                          # noqa: BLE001
            return {}

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
                   array_agg(v.version order by v.harvested_at desc),
                   array_agg(v.pinned order by v.harvested_at desc),
                   array_agg(extract(epoch from v.harvested_at)
                             order by v.harvested_at desc)
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
            "latest": _latest(r[6], r[7], r[8]),
        } for r in rows], total

    def versions(self, tech: str) -> list[dict]:
        self.migrate()
        with self._borrow() as cx:
            rows = cx.execute("""
                select v.version, v.source, v.strategy, v.complete,
                       to_char(v.harvested_at, 'YYYY-MM-DD HH24:MI'),
                       count(p.id), coalesce(sum(length(p.content)), 0),
                       v.expected, extract(epoch from v.harvested_at), v.pinned,
                       v.harvest_id, v.run_id, v.meta -> 'complete_reason',
                       v.meta ->> 'count_basis', v.meta is not null
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
            "expected": r[7], "saved": float(r[8] or 0), "pinned": r[9],
            "file": f"postgres://{self.location} ({tech} {r[0]})",
            "harvest_id": r[10], "run_id": r[11], "complete_reason": r[12],
            "count_basis": r[13], "contract": contract.CONTRACT if r[14] else 0,
        } for r in rows]
        # The default version first — `entry(tech, None)` takes the head of
        # this list, and "no version named" means "the current release", not
        # the newest download nor the highest number someone pinned.
        out.sort(key=_default_key, reverse=True)
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
                "select v.id, v.version, v.pinned, extract(epoch from v.harvested_at) "
                "  from doc_version v "
                "  join technology t on t.id = v.technology_id "
                " where t.name = %s and v.state = 'ready' "
                " order by v.harvested_at desc", (tech,)).fetchall()
            row = max(rows, key=lambda r: versions_mod.default_key(
                r[1], r[2], float(r[3] or 0))) if rows else None
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

    Two harvests of one version may run at once — two processes, two hosted
    instances, a stalled harvest restarted while it was in fact still alive.
    Neither touches the other's row while it runs, settles are serialised by
    an advisory lock, and a writer whose row has gone publishes nothing and
    says so, rather than deleting the published version and reporting success.
    """

    #: How often a running harvest refreshes `heartbeat_at`.
    HEARTBEAT_EVERY = 30.0
    #: How long without a heartbeat before an unsettled row counts as
    #: abandoned. Generous: a crawl can wait out rate limits for minutes
    #: between pages, and clearing a live harvest is far worse than keeping
    #: a dead row a little longer.
    STALE_AFTER = "30 minutes"

    def __init__(self, store, tech, version, source, strategy, expected, pinned=None,
                 harvest_id: str | None = None, run_id: str | None = None):
        store.migrate()
        self.store = store
        self.tech, self.version = tech, version
        self.source, self.strategy = source, strategy
        self.expected = expected
        self.pinned = pinned
        self.harvest_id = harvest_id or new_harvest_id()
        # A copy keeps the run it came from and reports to no run here.
        self.run = None if harvest_id else CURRENT_RUN.get()
        self.run_id = run_id if harvest_id else (self.run.run_id if self.run else None)
        self.titles: list[str] = []
        self.urls: list[str] = []
        self.rejected: list[tuple[str, str]] = []
        self._chars = 0
        self._n = 0
        self._beat = time.monotonic()
        self.started = time.time()

        self.cx = store._connect()
        self.tech_id = self.cx.execute(
            "insert into technology (name) values (%s) "
            "on conflict (name) do update set name = excluded.name returning id",
            (tech,)).fetchone()[0]
        # Clear earlier attempts that will never settle — failed ones, and
        # unsettled ones whose heartbeat stopped — so retries do not pile up
        # abandoned rows. Never a harvest that is still running: that used to
        # be deleted here too, out from under it.
        self.cx.execute(
            "delete from doc_version where technology_id = %s and (state = 'failed' "
            "  or (state = 'harvesting' and coalesce(heartbeat_at, harvested_at) "
            f"      < now() - interval '{self.STALE_AFTER}'))", (self.tech_id,))
        self.version_id = self.cx.execute(
            "insert into doc_version "
            "  (technology_id, version, source, strategy, complete, expected, state, "
            "   heartbeat_at, harvest_id, run_id) "
            "values (%s, %s, %s, %s, %s, %s, 'harvesting', now(), %s, %s) returning id",
            (self.tech_id, version, source, strategy, None, expected,
             self.harvest_id, self.run_id)).fetchone()[0]
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

    def add(self, title: str, url: str, body: str, captured_at: str | None = None) -> bool:
        """Store one page. Returns False if the store refused it.

        With the same `captured_at` and SHA-256 the file store writes in its
        page line, so an export renders the page identically."""
        title, url, body = clean_page(title, url, body)
        try:
            page_id = self.cx.execute(
                "insert into page (version_id, ordinal, title, url, content, "
                "                  captured_at, sha256) "
                "values (%s, %s, %s, %s, %s, %s, %s) returning id",
                (self.version_id, self._n + 1, title, url, body,
                 captured_at or contract.utc(), contract.sha256(body))).fetchone()[0]
            if len(body) > INDEX_CHARS:
                self._index_tail(page_id, title, url, body)
            if time.monotonic() - self._beat >= self.HEARTBEAT_EVERY:
                self.cx.execute("update doc_version set heartbeat_at = now() "
                                "where id = %s", (self.version_id,))
                self._beat = time.monotonic()
            self.cx.commit()
        except Exception as e:                          # noqa: BLE001
            # The failed statement poisons the transaction, so it has to be
            # rolled back before the next page can be written.
            self.cx.rollback()
            if getattr(e, "sqlstate", "") == "23503":
                # foreign_key_violation: this harvest's row is gone — removed
                # with `forget_documentation`, or cleared as abandoned. Every
                # page after this would be refused the same way, so stop now
                # rather than crawl the rest of the site into nothing.
                raise StoreError(
                    f"this harvest of {self.tech} {self.version} was removed from "
                    f"the store while it ran; nothing more can be stored for it") from e
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

    def settle(self, complete, expected=None, version=None, strategy=None,
               facts: dict | None = None) -> dict:
        """Publish this harvest, replacing the version it supersedes.

        `version` may differ from the label the writer opened with; see
        `_FileWriter.settle`. Renaming is safe because a `harvesting` row is
        invisible to every reader until this method flips it.

        One settle per version at a time (an advisory lock on technology and
        label), and only by a writer whose own row is still `harvesting`. A
        writer whose row is gone raises before deleting anything.

        The contract-v1 frontmatter (`_settle_meta`) is kept in `meta`, so the
        export renders the same document the file store would have written.
        """
        if expected is None:
            expected = self.expected
        label = version or self.version
        if strategy:
            self.strategy = strategy
        meta = _settle_meta(self, complete, expected, facts, label)
        complete, reason = meta["complete"], meta["complete_reason"]
        from psycopg.types.json import Jsonb
        try:
            self.cx.execute("select pg_advisory_xact_lock(%s::integer, hashtext(%s::text))",
                            (self.tech_id, label))
            row = self.cx.execute(
                "select state from doc_version where id = %s for update",
                (self.version_id,)).fetchone()
            if row is None or row[0] != "harvesting":
                raise StoreError(
                    f"this harvest of {self.tech} {label} is no longer in the store "
                    f"({'removed' if row is None else row[0]}) — removed with "
                    f"forget_documentation, or cleared as abandoned. Nothing was "
                    f"published, and the version already stored is unchanged.")
            if label != self.version:
                self.version = label
                self.cx.execute("update doc_version set version = %s where id = %s",
                                (label, self.version_id))
            if strategy:
                self.cx.execute("update doc_version set strategy = %s where id = %s",
                                (strategy, self.version_id))
            previous = self.cx.execute(
                "select pinned from doc_version where technology_id = %s and version = %s "
                "and state = 'ready'", (self.tech_id, self.version)).fetchone()
            pinned = versions_mod.still_pinned(self.pinned,
                                               previous[0] if previous else None)
            # Superseded, not deleted: every reader filters on 'ready', so the
            # revision this replaces stops being read at the same instant it
            # used to stop existing — and stays exportable by its harvest_id.
            self.cx.execute(
                "update doc_version set state = 'superseded' where technology_id = %s "
                "and version = %s and state = 'ready'", (self.tech_id, self.version))
            self.cx.execute(
                "update doc_version set state = 'ready', complete = %s, expected = %s, "
                "pinned = %s, meta = %s where id = %s",
                (complete, expected, pinned, Jsonb(meta), self.version_id))
            # The newest `keep_harvests()` superseded revisions stay; their
            # pages go with them by cascade.
            self.cx.execute(
                "delete from doc_version where id in ("
                "  select id from doc_version where technology_id = %s and version = %s "
                "   and state = 'superseded' order by id desc offset %s)",
                (self.tech_id, self.version, keep_harvests()))
            self.cx.commit()
        except BaseException:
            try:
                self.cx.rollback()              # also releases the advisory lock
            except Exception:                   # noqa: BLE001
                pass
            raise
        self._done()
        result = {
            "technology": self.tech, "version": self.version,
            "source": self.source, "strategy": self.strategy,
            "pages": self._n, "characters": self._chars,
            "file": f"postgres://{self.store.location} ({self.tech} {self.version})",
            "harvested": time.strftime("%Y-%m-%d %H:%M"), "complete": complete,
            "expected": expected, "pinned": pinned, "titles": self.titles[:2000],
            "rejected": list(self.rejected), "harvest_id": self.harvest_id,
            "run_id": self.run_id, "stored_pages": self._n, "complete_reason": reason,
            "store": self.store.kind, "fallback": False, "meta": meta,
            "count_basis": meta["count_basis"], "contract": contract.CONTRACT,
        }
        _report_published(self.run, result)
        return result

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


def sync_store(files: FileStore, db: PostgresStore) -> list[tuple[str, str, str]]:
    """Copy into the database the revisions a fallback wrote to files.

    A harvest that starts while the configured database is unreachable goes to
    the file store, and the moment the database is back every reader switches
    to it — so that harvest, reported as done, is no longer what anyone sees
    (Issues B9). This puts it where the readers are, under the same
    `harvest_id`, and leaves alone:

    * a revision the database already holds (same `harvest_id`);
    * a version the database has a newer harvest of;
    * an entry written before revisions had ids, which nothing can match.

    Returns `(technology, version, what happened)` for every revision it
    looked at with an id.
    """
    db.migrate()
    with db._borrow() as cx:
        known = {r[0] for r in cx.execute(
            "select harvest_id from doc_version where harvest_id is not null").fetchall()}
        newest = {(r[0], r[1]): r[2].timestamp() for r in cx.execute(
            "select t.name, v.version, v.harvested_at from doc_version v "
            "join technology t on t.id = v.technology_id "
            "where v.state = 'ready'").fetchall()}

    done: list[tuple[str, str, str]] = []
    for entry in sorted(files._load().values(), key=lambda e: e.get("saved") or 0.0):
        harvest_id = entry.get("harvest_id")
        tech, version = entry["technology"], entry["version"]
        if not harvest_id or harvest_id in known:
            continue
        theirs = newest.get((tech, version))
        if theirs is not None and theirs >= float(entry.get("saved") or 0.0):
            done.append((tech, version, "skipped: the database has a newer harvest"))
            continue
        path = Path(entry["file"])
        if not path.exists():
            done.append((tech, version, f"skipped: its file is missing ({path})"))
            continue
        text = path.read_bytes().decode("utf-8")
        revision = contract.parse_revision(text) if contract.is_v1(text) else None
        if revision is not None:
            pages = [(p.title, p.url, p.body, p.captured_at) for p in revision.pages]
        else:
            pages = [(*parse_page(b), None) for b in split_pages(text)[1]]
        with db.writer(tech, version, entry.get("source") or "",
                       entry.get("strategy") or "files", entry.get("expected"),
                       entry.get("pinned"), harvest_id=harvest_id,
                       run_id=entry.get("run_id")) as w:
            for title, url, body, captured in pages:
                w.add(title, url, body, captured_at=captured)
            got = w.settle(complete=entry.get("complete"), expected=entry.get("expected"))
        if revision is not None:
            # The revision as it was published, not as this copy re-judged it:
            # same ids, same times, same verdict. Only where it lives changed.
            from psycopg.types.json import Jsonb
            with db._borrow() as cx:
                cx.execute("update doc_version set meta = %s, complete = %s "
                           "where harvest_id = %s",
                           (Jsonb(dict(revision.meta, store=db.kind)),
                            revision.meta.get("complete"), harvest_id))
                cx.commit()
        done.append((tech, version, f"copied, {got['stored_pages']} pages"))
    return done
