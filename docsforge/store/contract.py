"""
Contract v1: what a stored revision says about itself.

Agreed in Discussion #35 and tracked in issue #42. A revision is one stored
harvest of one corpus at one version, and under contract v1 its Markdown
carries, in YAML frontmatter, everything needed to judge it without asking
anyone: which harvest it is (`harvest_id`), when it was taken, how much there
was to take and how much was stored, and whether it is `complete` — with the
reason whenever it is not. Every page carries when it was captured, its byte
length and its SHA-256, so a reader can check that a page is the page that
was stored, and a page whose body happens to look like a page boundary
cannot split in two (Issues B6): the parser reads exactly `bytes` bytes.

One renderer, one parser, for both stores. The file store writes its files
through `render_page` and `render_header`; Postgres keeps the same values in
columns and renders the export through the same functions, so the export is
the same document whichever store produced it.

The rules (`System Files/Contract.md` has the full specification):

1. A revision file describes itself.
2. Decisions stay outside the file: `usable_for_planning` is a sidecar that
   points at revisions by `harvest_id`.
3. `complete: true` is earned — `judge()` enforces §5.6, and only the writer,
   which knows what was stored, calls it.
4. Both stores give the same bytes.
5. Additive: a file with no frontmatter is contract 0 and still reads.

Every frontmatter value is written as JSON, which keeps the block valid YAML
and lets it be parsed without a YAML library.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from docsforge import __version__

CONTRACT = 1
GENERATOR = f"docsforge {__version__}"

#: The frontmatter, in the order it is written.
FIELDS = (
    "docsforge_contract", "harvest_id", "run_id", "job",
    "technology", "corpus", "corpus_role", "version", "version_basis",
    "source", "strategy", "store", "scope",
    "started_at", "harvested_at",
    "complete", "complete_reason", "count_basis",
    "expected_pages", "stored_pages", "not_stored", "generator",
)

#: Kinds of page that were reached or known of and are not in the revision.
#: `off_topic` and `dead_404` are not gaps: a topic left them out on purpose,
#: and a 404 is not a page.
NOT_STORED_KINDS = ("unextractable", "refused_429", "rejected_by_store",
                    "off_topic", "dead_404")
NOT_GAPS = {"off_topic", "dead_404"}

#: URLs kept per kind. Counts are always exact; the list is for retrying.
URL_CAP = 100

#: How `expected_pages` was established. Only these state a number
#: independently of the harvest, which is what `complete: true` requires.
STATED_BASES = {"manifest", "manifest+links", "sitemap", "sitemap+links",
                "dump-file", "repository", "api-spec", "spec-sections", "stated"}
COUNT_BASES = STATED_BASES | {"crawl-frontier", "none", "legacy"}

VERSION_BASES = {"site-path", "site-declared", "registry-current",
                 "requested-confirmed", "requested-unconfirmed", "harvest-date",
                 "undated", "legacy", "unstated"}

#: `complete_reason.code` values, by the `complete` they explain.
FALSE_CODES = {"page-limit", "rate-limited", "unextractable", "rejected-by-store",
               "listed-not-acquired", "index-without-dump", "shortfall",
               "related-corpora"}
NULL_CODES = {"crawl-uncounted", "no-enumeration", "legacy-unverified",
              "related-corpora-unknown"}


class ContractError(ValueError):
    """A file that claims contract v1 and does not follow it."""


def utc(ts: float | None = None) -> str:
    """ISO-8601 UTC to the second: `2026-11-18T10:15:12Z`."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def not_stored(**kinds) -> dict:
    """The `not_stored` block. Each kind is a list of URLs (counted, and
    capped at `URL_CAP`) or a bare count."""
    out = {}
    for kind in NOT_STORED_KINDS:
        value = kinds.get(kind) or []
        if isinstance(value, int):
            out[kind] = {"count": value, "urls": []}
        else:
            urls = [str(u) for u in value]
            out[kind] = {"count": len(urls), "urls": urls[:URL_CAP]}
    return out


def _gaps(missing: dict) -> dict:
    return {k: v for k, v in (missing or {}).items()
            if k not in NOT_GAPS and (v or {}).get("count")}


def judge(complete, reason: dict | None, count_basis: str, expected: int | None,
          stored: int, missing: dict) -> tuple[bool | None, dict | None]:
    """`complete` as it may be stored, and its `complete_reason` (§5.6).

    Only `True` is ever changed, and only downwards: a harvest decides from
    what it *acquired*, and only the writer knows what was *stored*.

    * Pages the store refused, or fewer stored than counted → `false`.
    * Pages reached and not stored for any reason that is a gap → `false`.
    * No independent count (`crawl-frontier`, `none`, `legacy`, or no
      `expected_pages` at all) → `null`: nothing is `true` because nobody
      counted.

    `false` and `null` always carry a reason; one the caller did not give is
    filled in from what is known.
    """
    gaps = _gaps(missing)
    if complete is True:
        if gaps.get("rejected_by_store"):
            n = gaps["rejected_by_store"]["count"]
            return False, {"code": "rejected-by-store",
                           "text": f"the store refused {n} page(s) the harvest "
                                   f"acquired, so {stored} are stored"}
        if expected is not None and stored < expected:
            return False, {"code": "listed-not-acquired",
                           "text": f"stored {stored} of the {expected} pages counted"}
        if gaps:
            kind = next(iter(gaps))
            code = "rate-limited" if kind == "refused_429" else "unextractable"
            return False, {"code": code,
                           "text": f"{gaps[kind]['count']} page(s) {kind.replace('_', ' ')}"}
        # A legacy count was a count; only how it was made went unrecorded.
        if count_basis not in STATED_BASES | {"legacy"} or expected is None:
            return None, {"code": "no-enumeration",
                          "text": "nothing independent of the harvest stated how "
                                  "many pages exist"}
        return True, None
    if reason:
        return complete, reason
    if complete is False:
        return False, {"code": "shortfall", "text": "the harvest did not get every page"}
    return None, {"code": "no-enumeration",
                  "text": "nothing established how much documentation exists"}


def reason_of(text: str, code: str) -> dict | None:
    return {"code": code, "text": text} if text or code else None


# ─────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────
#: Key order for the nested objects. Postgres `jsonb` keeps keys in its own
#: order, not the order written, so without this the same revision rendered
#: from the database differed from the file — byte for byte the thing rule 4
#: forbids. Keys not listed follow, sorted.
_NESTED_ORDER = {
    "complete_reason": ("code", "text"),
    "scope": ("mode", "start", "widened_to", "topic", "max_pages"),
    "not_stored": NOT_STORED_KINDS,
}


def _ordered(value: dict, order) -> dict:
    head = [k for k in order if k in value]
    return {k: value[k] for k in head + sorted(k for k in value if k not in head)}


def _canonical(key: str, value):
    if not isinstance(value, dict):
        return value
    if key == "not_stored":
        return {kind: _ordered(v, ("count", "urls")) if isinstance(v, dict) else v
                for kind, v in _ordered(value, NOT_STORED_KINDS).items()}
    return _ordered(value, _NESTED_ORDER.get(key, ()))


def render_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key in FIELDS:
        value = _canonical(key, meta.get(key))
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines += ["---", ""]
    return "\n".join(lines)


def render_header(meta: dict, titles: list[str], urls: list[str]) -> str:
    """Frontmatter, the title and the Contents: everything before the pages."""
    out = [render_frontmatter(meta),
           f"# {meta.get('corpus') or meta.get('technology')} {meta.get('version')} "
           f"documentation", "", "## Contents", ""]
    out += [f"{i}. [{t}]({u})" for i, (t, u) in enumerate(zip(titles, urls), 1)]
    return "\n".join(out) + "\n"


def page_line(n: int, captured_at: str | None, digest: str, size: int) -> str:
    cap = f'"{captured_at}"' if captured_at else "null"
    return (f'<!-- docsforge:page n={n} captured_at={cap} sha256="{digest}" '
            f"bytes={size} -->")


def render_page(n: int, title: str, url: str, body: str,
                captured_at: str | None, digest: str | None = None) -> str:
    """One page block. Inputs are already in their stored form
    (`kb_store.clean_page`); the hash is of exactly the body written."""
    raw = body.encode("utf-8")
    digest = digest or hashlib.sha256(raw).hexdigest()
    return (f"\n---\n\n## {title}\n\nSource: <{url}>\n"
            f"{page_line(n, captured_at, digest, len(raw))}\n\n{body}\n")


def render_revision(meta: dict, pages: list) -> str:
    """A whole revision. `pages` are `Page`s or `(title, url, body,
    captured_at[, sha256])` tuples. A stored hash is written as stored, not
    recomputed, so a body changed since it was stored fails `validate()`."""
    rows = [p if isinstance(p, Page) else Page(
                i, p[0], p[1], p[2], p[3] if len(p) > 3 else None,
                (p[4] or "") if len(p) > 4 else "")
            for i, p in enumerate(pages, 1)]
    out = [render_header(meta, [p.title for p in rows], [p.url for p in rows])]
    out += [render_page(i, p.title, p.url, p.body, p.captured_at, p.sha256 or None)
            for i, p in enumerate(rows, 1)]
    return "".join(out)


# ─────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────
@dataclass
class Page:
    n: int
    title: str
    url: str
    body: str
    captured_at: str | None = None
    sha256: str = ""
    bytes: int = 0


@dataclass
class Revision:
    meta: dict
    pages: list[Page] = field(default_factory=list)

    @property
    def contract(self) -> int:
        return int(self.meta.get("docsforge_contract") or 0)


_FRONT = re.compile(rb"\A---\n(?P<body>.*?)\n---\n", re.S)
_PAGE = re.compile(
    rb"\n---\n\n## (?P<title>[^\n]*)\n\nSource: <(?P<url>[^>\n]*)>\n"
    rb"<!-- docsforge:page n=(?P<n>\d+) captured_at=(?P<cap>\"[^\"\n]*\"|null) "
    rb"sha256=\"(?P<sha>[0-9a-f]{64})\" bytes=(?P<bytes>\d+) -->\n\n")


def is_v1(text: str) -> bool:
    return text.startswith("---\n") and "\ndocsforge_contract: 1\n" in text[:4096]


def parse_frontmatter(text: str) -> dict:
    match = _FRONT.match(text.encode("utf-8"))
    if not match:
        return {}
    meta = {}
    for line in match.group("body").decode("utf-8").split("\n"):
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        try:
            meta[key.strip()] = json.loads(value)
        except ValueError as e:
            raise ContractError(f"frontmatter field {key!r} is not JSON: {value[:80]}") from e
    return meta


def parse_revision(text: str) -> Revision:
    """A contract-v1 revision, read by length rather than by pattern.

    Each page's body is exactly `bytes` bytes after its page line, so nothing
    inside a body can be mistaken for the next page. A file without
    frontmatter is contract 0 and yields its meta as `{}` — read it with
    `kb_store.split_pages` instead. Raises `ContractError` on a v1 file whose
    pages are not where their lengths say.
    """
    data = text.encode("utf-8")
    front = _FRONT.match(data)
    if not front:
        return Revision({"docsforge_contract": 0})
    meta = parse_frontmatter(text)
    pages: list[Page] = []
    found = _PAGE.search(data, front.end())
    while found is not None:
        size = int(found.group("bytes"))
        start = found.end()
        end = start + size
        if data[end:end + 1] != b"\n":
            raise ContractError(
                f"page {found.group('n').decode()} is not {size} bytes long — "
                f"the file was edited, or truncated")
        cap = found.group("cap").decode()
        pages.append(Page(
            n=int(found.group("n")),
            title=found.group("title").decode("utf-8"),
            url=found.group("url").decode("utf-8"),
            body=data[start:end].decode("utf-8"),
            captured_at=None if cap == "null" else cap.strip('"'),
            sha256=found.group("sha").decode(),
            bytes=size,
        ))
        if end + 1 >= len(data):
            break
        found = _PAGE.match(data, end + 1)
        if found is None:
            raise ContractError(f"unexpected text after page {pages[-1].n}")
    return Revision(meta, pages)


def validate(revision: Revision) -> list[str]:
    """Everything wrong with a v1 revision; empty when it holds.

    Checks every page against its own `bytes` and `sha256`, the numbering,
    `stored_pages`, and the §5.6 rules for `complete`.
    """
    meta, problems = revision.meta, []
    if revision.contract != CONTRACT:
        return [f"not a contract-{CONTRACT} revision"]
    for key in FIELDS:
        if key not in meta:
            problems.append(f"frontmatter has no {key!r}")
    for i, page in enumerate(revision.pages, 1):
        raw = page.body.encode("utf-8")
        if page.n != i:
            problems.append(f"page {i} is numbered {page.n}")
        if len(raw) != page.bytes:
            problems.append(f"page {i} is {len(raw)} bytes, its line says {page.bytes}")
        if hashlib.sha256(raw).hexdigest() != page.sha256:
            problems.append(f"page {i} does not match its sha256")
    stored = meta.get("stored_pages")
    if stored != len(revision.pages):
        problems.append(f"stored_pages is {stored}, the file holds {len(revision.pages)}")
    complete, reason = meta.get("complete"), meta.get("complete_reason")
    if complete not in (True, False, None):
        problems.append(f"complete is {complete!r}, not true, false or null")
    if complete is not True and not (isinstance(reason, dict) and reason.get("code")):
        problems.append("complete is not true, and complete_reason does not say why")
    if complete is True:
        again, _ = judge(True, None, meta.get("count_basis") or "none",
                         meta.get("expected_pages"), len(revision.pages),
                         meta.get("not_stored") or {})
        if again is not True:
            problems.append("complete is true, but §5.6 does not allow it")
    if meta.get("count_basis") not in COUNT_BASES:
        problems.append(f"count_basis {meta.get('count_basis')!r} is not a known basis")
    return problems


# ─────────────────────────────────────────────────────────────
# Contract 0
# ─────────────────────────────────────────────────────────────
def legacy_meta(*, harvest_id: str, technology: str, version: str, source: str,
                strategy: str, store: str, complete, expected, stored: int,
                harvested_at: str | None) -> dict:
    """Frontmatter for a revision written before contract v1.

    Nothing recorded how its count was made, so `count_basis` is `legacy` —
    and a `complete: true` with nothing counted behind it (Issues M7) is
    reported as `null`, `legacy-unverified` (decision D3). What the store
    holds is not changed; only what the export claims.
    """
    reason = None
    if complete is True and expected is None:
        complete, reason = None, {"code": "legacy-unverified",
                                  "text": "stored before contract v1 as complete, "
                                          "with nothing counted behind it"}
    elif complete is True and expected is not None and stored < expected:
        complete, reason = False, {"code": "listed-not-acquired",
                                   "text": f"stored {stored} of the {expected} pages counted"}
    elif complete is not True:
        reason = {"code": "legacy-unverified" if complete is None else "shortfall",
                  "text": "stored before contract v1; the reason was not recorded"}
    return {
        "docsforge_contract": CONTRACT, "harvest_id": harvest_id, "run_id": None,
        "job": None, "technology": technology.split("--")[0], "corpus": technology,
        "corpus_role": "federated" if "--" in technology else "entry",
        "version": version, "version_basis": "legacy", "source": source,
        "strategy": strategy, "store": store, "scope": None,
        "started_at": None, "harvested_at": harvested_at,
        "complete": complete, "complete_reason": reason, "count_basis": "legacy",
        "expected_pages": expected, "stored_pages": stored,
        "not_stored": not_stored(), "generator": GENERATOR,
    }
