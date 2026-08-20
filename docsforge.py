#!/usr/bin/env python3
"""
docsforge — Universal software documentation → Markdown for LLMs.

Detects what KIND of source it is and extracts accordingly:
  - llms.txt / llms-full.txt      (the LLM-native docs standard)
  - OpenAPI / Swagger (JSON/YAML)  → API reference tables
  - sitemap.xml                    → structured crawl (incl. sitemap indexes)
  - GitHub repo                    → README + /docs via API
  - Generic HTML docs site         → readability extraction
  - Raw Markdown / plaintext       → passthrough + cleanup

Usage:
  python docsforge.py https://docs.stripe.com
  python docsforge.py https://api.example.com/openapi.json
  python docsforge.py https://github.com/tiangolo/fastapi
  python docsforge.py https://docs.example.com --crawl --max-pages 50
  python docsforge.py https://site.com --js            # JS-rendered
  python docsforge.py https://site.com --single-file   # one combined .md

Library use:
  from docsforge import forge, Options
  docs = forge("https://docs.example.com", Options(crawl=True, max_pages=10))
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field, replace
from urllib.parse import urldefrag, urljoin, urlparse

import requests

__version__ = "1.1.0"

HEADERS = {"User-Agent": f"docsforge/{__version__}"}
TIMEOUT = 25

# Extensions that are never worth following during a crawl.
SKIP_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".mov", ".avi",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".exe", ".dmg", ".msi", ".deb", ".rpm", ".whl", ".jar",
    ".css", ".js", ".map",
)

STRATEGIES = ("llms_txt", "openapi", "sitemap", "github", "raw_text", "html")


class ForgeError(RuntimeError):
    """A user-facing failure: bad URL, unreachable host, unusable source."""


@dataclass
class Doc:
    """One extracted document."""
    url: str
    title: str
    markdown: str

    def as_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "markdown": self.markdown}


@dataclass
class Options:
    crawl: bool = False
    #: 0 means no limit — keep going until the documentation section is
    #: exhausted. A page count is an arbitrary guess at how big a manual is;
    #: the scope prefix is the boundary that actually means something.
    max_pages: int = 25
    js: bool = False
    delay: float = 0.4
    force: str | None = None
    #: Crawl boundary: "section" keeps to the docs root the start URL sits in,
    #: "host" is the whole domain, anything else is used as a literal prefix.
    scope: str = "section"
    # Fetching a user-supplied URL server-side is an SSRF vector, so private /
    # loopback targets are refused unless explicitly allowed.
    allow_private: bool = field(
        default_factory=lambda: os.environ.get("DOCSFORGE_ALLOW_PRIVATE", "") not in ("", "0", "false", "False")
    )
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    verbose: bool = True

    def limit(self) -> int | None:
        """The page ceiling, or None for unlimited. Always go through this:
        `list[:0]` is empty, so treating an unlimited 0 as a slice bound would
        silently harvest nothing."""
        return self.max_pages if self.max_pages and self.max_pages > 0 else None


@dataclass
class Detection:
    """Result of source sniffing: the strategy, the URL to use, and any body
    we already downloaded while sniffing (so handlers never re-fetch)."""
    kind: str
    url: str
    body: str | None = None


def _log(opts: Options, msg: str) -> None:
    if opts.verbose:
        print(msg, file=sys.stderr)


def enable_utf8_console(streams=("stdout", "stderr")) -> None:
    """Windows consoles default to cp1252, which blows up on the arrows and box
    characters this tool prints. Force UTF-8 where we can, degrade where we can't."""
    for name in streams:
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


# ─────────────────────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────────────────────
class Fetcher:
    """Owns the HTTP session and (at most one) Playwright browser.

    The browser is started lazily and reused for every page, which is the
    difference between a 50-page JS crawl taking seconds vs. minutes.
    """

    def __init__(self, opts: Options):
        self.opts = opts
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self.session.close()

    # -- safety ------------------------------------------------
    def guard(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ForgeError(f"Only http/https URLs are supported, got: {url!r}")
        if not parsed.netloc:
            raise ForgeError(f"URL has no host: {url!r}")
        if self.opts.allow_private:
            return
        if _resolves_private(parsed.hostname or ""):
            raise ForgeError(
                f"Refusing to fetch private/loopback address: {parsed.hostname}. "
                f"Set DOCSFORGE_ALLOW_PRIVATE=1 to permit it."
            )

    # -- primitives --------------------------------------------
    def get(self, url: str, **kw) -> requests.Response:
        self.guard(url)
        kw.setdefault("timeout", TIMEOUT)
        try:
            return self.session.get(url, **kw)
        except requests.RequestException as e:
            raise ForgeError(f"Request failed for {url}: {e}") from e

    def text(self, url: str, **kw) -> str:
        r = self.get(url, **kw)
        if r.status_code >= 400:
            raise ForgeError(f"HTTP {r.status_code} for {url}")
        return _decode(r)

    def html(self, url: str) -> str:
        """Fetch a page as HTML, rendering JS if the run asked for it."""
        if self.opts.js:
            return self._render(url)
        r = self.get(url)
        if r.status_code >= 400:
            raise ForgeError(f"HTTP {r.status_code} for {url}")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or ctype.endswith(("xml", "json", "+xml"))):
            raise ForgeError(f"Not a text document ({ctype}) at {url}")
        return _decode(r)

    def _render(self, url: str) -> str:
        self.guard(url)
        page = self._page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.content()
        except Exception as e:
            raise ForgeError(f"JS render failed for {url}: {e}") from e
        finally:
            page.close()

    def _page(self):
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                raise ForgeError(
                    "--js needs Playwright: pip install playwright && playwright install chromium"
                ) from e
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        return self._browser.new_page()


def _decode(r: requests.Response) -> str:
    """requests guesses latin-1 for text/* without a charset, which mangles
    UTF-8 docs. Fall back to content sniffing when the server didn't say."""
    ctype = r.headers.get("content-type", "").lower()
    if "charset=" not in ctype:
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _resolves_private(host: str) -> bool:
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # let the actual request produce the real error
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


# ─────────────────────────────────────────────────────────────
# Source detection
# ─────────────────────────────────────────────────────────────
def detect_source(url: str, fetcher: Fetcher) -> Detection:
    """Pick an extraction strategy from the URL plus one cheap probe.

    Any body downloaded while probing is carried on the Detection so the
    handler does not fetch the same bytes twice.
    """
    u = url.lower()
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path

    if host in ("github.com", "www.github.com") and not u.endswith((".md", ".txt")):
        return Detection("github", url)
    if u.endswith("llms-full.txt"):
        return Detection("llms_txt", url)
    if u.endswith("llms.txt"):
        # The convention has two shapes: a full dump, and a short *index* that
        # names a fuller file beside it. Taking the index at face value is how
        # 2 KB of the AI SDK's 5.7 MB got stored and recorded as complete — and
        # it only ever happened on this path, because the probe below already
        # prefers llms-full.txt and never got the chance to run.
        return _fuller_dump(url, fetcher) or Detection("llms_txt", url)
    if u.endswith("sitemap.xml") or path.endswith("/sitemap_index.xml"):
        return Detection("sitemap", url)

    if u.endswith((".yaml", ".yml", ".json")):
        # Might be an OpenAPI spec — we need the body either way, so keep it.
        try:
            body = fetcher.text(url)
        except ForgeError:
            body = None
        if body is not None:
            kind = "openapi" if _looks_like_openapi(body) else "raw_text"
            return Detection(kind, url, body)

    if u.endswith((".md", ".markdown", ".txt", ".rst")):
        return Detection("raw_text", url)

    # Probe the origin for an LLM-native dump, whatever depth the URL is at.
    # A single docs page is rarely what someone wants when the whole site is
    # published as one file two directories up.
    for candidate in ("llms-full.txt", "llms.txt"):
        probe = urljoin(url, "/" + candidate)
        try:
            r = fetcher.get(probe, timeout=10, allow_redirects=True)
        except ForgeError:
            continue
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code == 200 and "html" not in ctype:
            return Detection("llms_txt", probe, _decode(r))

    return Detection("html", url)


SITEMAP_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                      "/sitemap-0.xml", "/docs/sitemap.xml")


#: Enumeration is meant to be cheap next to the harvest it precedes.
MAP_TIMEOUT = 10


def _weigh(url: str, fetcher: Fetcher) -> int:
    """How many bytes are at this URL, without downloading them if avoidable.

    Streaming means the headers arrive and the body does not, so a 5.7 MB dump
    can be measured for the cost of a request. Servers that decline to say fall
    back to reading it, which is still correct, only slower.
    """
    try:
        r = fetcher.get(url, timeout=MAP_TIMEOUT, allow_redirects=True, stream=True)
    except ForgeError:
        return 0
    try:
        if r.status_code != 200:
            return 0
        if "html" in (r.headers.get("content-type") or "").lower():
            return 0
        declared = r.headers.get("content-length")
        if declared and declared.isdigit():
            return int(declared)
        return len(_decode(r))
    finally:
        closer = getattr(r, "close", None)
        if callable(closer):
            closer()


@dataclass
class DocMap:
    """What documentation exists at a URL, established *before* fetching it.

    Enumeration is the stage DocsForge did not have, and its absence is why
    `complete` could only ever be an assertion: with no idea how many pages a
    site has, finishing and stopping are the same event. Counting first is what
    lets everything downstream be measured instead of assumed.
    """

    urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    dump_url: str = ""
    dump_bytes: int = 0

    @property
    def expected(self) -> int | None:
        """How many pages the site says it has, or None if nobody could tell."""
        return len(self.urls) or None

    def as_dict(self) -> dict:
        return {"expected": self.expected, "sources": self.sources,
                "dump_url": self.dump_url, "dump_bytes": self.dump_bytes}


def discover(url: str, fetcher: Fetcher, opts: Options | None = None) -> DocMap:
    """Enumerate the documentation at `url` without downloading it.

    Cheap on purpose — a handful of requests against a harvest that will fetch
    hundreds of pages. Three independent views, because no single one is
    reliable: the `llms.txt` index a site publishes for machines, the full dump
    beside it, and the sitemap it publishes for search engines.
    """
    opts = opts or Options()
    found = DocMap()

    # The full dump, if the site publishes one. Its *size* is what matters
    # here, not its contents — knowing it exists is what makes an index
    # recognisable as an index — so this asks for the headers and does not
    # pull the megabytes down a second time.
    for sibling in DUMP_SIBLINGS:
        for target in (urljoin(url, sibling), urljoin(url, "/" + sibling)):
            size = _weigh(target, fetcher)
            if size >= MIN_DUMP:
                found.dump_url, found.dump_bytes = target, size
                found.sources.append(sibling)
                break
        if found.dump_url:
            break

    # The index a site publishes for machines is a list of its own pages.
    try:
        index = fetcher.text(urljoin(url, "/llms.txt"), timeout=MAP_TIMEOUT)
    except ForgeError:
        index = ""
    links = [urljoin(url, m) for m in re.findall(r"\]\(([^)\s]+)\)", index or "")]
    if links:
        found.sources.append("llms.txt")

    # The sitemap is the site's own statement of what exists, and reaches
    # pages nothing links to.
    prefix = docs_scope(url)
    host = (urlparse(url).hostname or "").lower()
    sitemap = find_sitemap(url, fetcher, opts)
    if sitemap:
        try:
            listed = _sitemap_links(fetcher.text(sitemap), fetcher, opts)
        except ForgeError:
            listed = []
        scoped = [l for l in listed if _crawlable(l, host, prefix)]
        if scoped:
            found.sources.append("sitemap.xml")
            links += scoped

    found.urls = list(dict.fromkeys(_normalize(l) for l in links))
    return found


def find_sitemap(url: str, fetcher: Fetcher, opts: Options) -> str | None:
    """Look for a sitemap: robots.txt first (it is the declared location),
    then the conventional paths. Returns a URL or None."""
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    try:
        robots = fetcher.text(origin + "/robots.txt", timeout=10)
    except ForgeError:
        robots = ""
    for line in robots.splitlines():
        if line.lower().startswith("sitemap:"):
            found = line.split(":", 1)[1].strip()
            if found:
                _log(opts, f"  sitemap from robots.txt: {found}")
                return found

    for candidate in SITEMAP_CANDIDATES:
        probe = origin + candidate
        try:
            r = fetcher.get(probe, timeout=10, allow_redirects=True)
        except ForgeError:
            continue
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code == 200 and ("xml" in ctype or r.text.lstrip().startswith("<?xml")):
            _log(opts, f"  sitemap found at {probe}")
            return probe
    return None


def _looks_like_openapi(text: str) -> bool:
    t = text.lstrip()[:2000]
    return ('"openapi"' in t or re.search(r"^openapi\s*:", t, re.M) is not None
            or '"swagger"' in t or re.search(r"^swagger\s*:", t, re.M) is not None)


# ─────────────────────────────────────────────────────────────
# Strategy: llms.txt (already LLM-ready)
# ─────────────────────────────────────────────────────────────
#: A full dump runs to megabytes — ai-sdk.dev publishes 5.7 MB — so it needs a
#: budget the ordinary probe timeout does not give it. The short timeout used
#: to bias *against* large files: the more documentation a site published, the
#: likelier the fetch lost and a 2 KB index won instead.
DUMP_TIMEOUT = 45

#: Below this a dump is left as one page; splitting a short file just scatters
#: it. Above it, one page makes the whole document rank as a single search hit.
SPLIT_ABOVE = 60_000
SPLIT_MIN_PARTS = 3
SPLIT_MAX_PARTS = 4_000

#: Files an `llms.txt` index points at, best first.
DUMP_SIBLINGS = ("llms-full.txt", "llms-medium.txt")

#: A sibling has to carry real text to be worth preferring over the index.
MIN_DUMP = 1_000


def _fuller_dump(url: str, fetcher: Fetcher) -> "Detection | None":
    """The full dump sitting beside an `llms.txt` index, if the site has one.

    Checked in the index's own directory first and then at the origin, because
    both are in use — Prisma publishes `/docs/llms-full.txt` while most sites
    put it at the root.
    """
    seen = set()
    for sibling in DUMP_SIBLINGS:
        for target in (urljoin(url, sibling), urljoin(url, "/" + sibling)):
            if target in seen or target.lower() == url.lower():
                continue
            seen.add(target)
            try:
                r = fetcher.get(target, timeout=DUMP_TIMEOUT, allow_redirects=True)
            except ForgeError:
                continue
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or "html" in ctype:
                continue
            body = _decode(r)
            if len(body) >= MIN_DUMP:
                return Detection("llms_txt", target, body)
    return None


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "section"


def _split_dump(text: str, above: int = SPLIT_ABOVE) -> list[tuple[str, str]]:
    """Cut a large single-file dump into pages on its own headings.

    Returns [(title, chunk), ...], or [] to leave the text alone. The heading
    level is chosen by result rather than assumed: whichever of `#`, `##` or
    `###` yields the most pages without going silly is the one the document
    actually uses for sections.
    """
    if len(text) < above:
        return []

    best, best_hits = None, []
    for prefix in ("#", "##", "###"):
        hits = list(re.finditer(rf"^{prefix}[ \t]+(\S[^\n]*)$", text, re.M))
        if SPLIT_MIN_PARTS <= len(hits) <= SPLIT_MAX_PARTS and len(hits) > len(best_hits):
            best, best_hits = prefix, hits
    if not best:
        return []

    parts: list[tuple[str, str]] = []
    # Anything before the first heading is the document's own preamble. Its
    # first line is often a machine-readable banner rather than a title —
    # Hono's opens with a <SYSTEM> tag — so it gets tidied before being shown.
    if best_hits[0].start() > 0:
        head = text[:best_hits[0].start()].strip()
        if head:
            first = re.sub(r"<[^>]*>", " ", head.split("\n", 1)[0]).lstrip("# ").strip()
            parts.append(((first[:90].rstrip() or "Overview"), head))

    for i, hit in enumerate(best_hits):
        end = best_hits[i + 1].start() if i + 1 < len(best_hits) else len(text)
        chunk = text[hit.start():end].strip()
        if chunk:
            parts.append((hit.group(1).strip(), chunk))
    return parts


def handle_llms_txt(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    body = det.body if det.body is not None else fetcher.text(det.url, timeout=DUMP_TIMEOUT)
    body = body.strip()

    # A multi-megabyte dump kept as a single page is technically stored and
    # practically unsearchable: every query matches "page 1" and the snippet
    # ranking has nothing to choose between. Effect's 703 pages rank well
    # precisely because they are 703 pages.
    parts = _split_dump(body)
    if len(parts) < 2:
        return [Doc(det.url, "llms.txt", _meta_header(det.url, "llms.txt") + body)]

    docs = [Doc(f"{det.url}#{_anchor(title)}", title,
                _meta_header(det.url, "llms.txt") + chunk)
            for title, chunk in parts]
    _log(opts, f"  split {len(body):,} characters into {len(docs)} pages")
    return docs


# ─────────────────────────────────────────────────────────────
# Strategy: OpenAPI / Swagger → readable API reference
# ─────────────────────────────────────────────────────────────
def handle_openapi(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    body = det.body if det.body is not None else fetcher.text(det.url)
    spec = _parse_spec(body)

    info = spec.get("info") or {}
    title = info.get("title") or "API Reference"
    version = info.get("version") or ""
    desc = info.get("description") or ""

    out: list[str] = [_meta_header(det.url, "openapi").rstrip("\n"), "", f"# {title}", ""]
    if version:
        out += [f"**Version:** {version}", ""]

    servers = [s.get("url", "") for s in (spec.get("servers") or []) if s.get("url")]
    if servers:
        out += ["**Servers:** " + ", ".join(f"`{s}`" for s in servers), ""]
    if desc.strip():
        out += [desc.strip(), ""]

    out += ["## Endpoints", ""]

    paths = spec.get("paths") or {}
    for path, item in sorted(paths.items()):
        if not isinstance(item, dict):
            continue
        item = _deref(spec, item)
        # Parameters declared once for the whole path apply to every operation.
        shared = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]

        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            if not isinstance(op, dict):
                continue
            out += _render_operation(spec, path, method, op, shared)

    return [Doc(det.url, title, "\n".join(out).rstrip() + "\n")]


def _render_operation(spec: dict, path: str, method: str, op: dict, shared: list) -> list[str]:
    lines = [f"### `{method.upper()} {path}`", ""]

    if op.get("deprecated"):
        lines += ["> **Deprecated**", ""]
    if op.get("summary"):
        lines += [str(op["summary"]).strip(), ""]
    if op.get("description"):
        lines += [str(op["description"]).strip(), ""]

    params = [_deref(spec, p) for p in shared + list(op.get("parameters") or [])]
    params = [p for p in params if isinstance(p, dict) and p.get("name")]
    # An operation-level param overrides a path-level one with the same name+in.
    seen: dict[tuple, dict] = {}
    for p in params:
        seen[(p.get("name"), p.get("in"))] = p
    params = list(seen.values())

    if params:
        lines += ["| Param | In | Type | Required | Description |",
                  "|---|---|---|---|---|"]
        for p in params:
            schema = _deref(spec, p.get("schema") or {})
            lines.append(
                f"| `{p.get('name', '')}` "
                f"| {p.get('in', '')} "
                f"| {_type_of(spec, schema)} "
                f"| {'yes' if p.get('required') else 'no'} "
                f"| {_cell(p.get('description', ''))} |"
            )
        lines.append("")

    rb = _deref(spec, op.get("requestBody") or {})
    if rb:
        content = rb.get("content") or {}
        required = " (required)" if rb.get("required") else ""
        types = ", ".join(f"`{c}`" for c in content) or "`—`"
        lines.append(f"**Request body{required}:** {types}")
        for ctype, media in content.items():
            schema = _deref(spec, (media or {}).get("schema") or {})
            named = _type_of(spec, schema, raw=(media or {}).get("schema"))
            if named and named != "object":
                lines.append(f"- `{ctype}` → {named}")
        lines.append("")

    responses = op.get("responses") or {}
    if responses:
        lines += ["| Response | Description |", "|---|---|"]
        for code, resp in responses.items():
            resp = _deref(spec, resp if isinstance(resp, dict) else {})
            lines.append(f"| `{code}` | {_cell(resp.get('description', ''))} |")
        lines.append("")

    return lines


def _parse_spec(body: str) -> dict:
    try:
        spec = json.loads(body)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as e:
            raise ForgeError("YAML spec needs PyYAML: pip install pyyaml") from e
        try:
            spec = yaml.safe_load(body)
        except Exception as e:
            raise ForgeError(f"Could not parse spec as JSON or YAML: {e}") from e
    if not isinstance(spec, dict):
        raise ForgeError("Spec did not parse to an object")
    return spec


def _deref(spec: dict, node, depth: int = 0):
    """Resolve local `#/...` JSON pointers. Foreign refs are left alone."""
    while isinstance(node, dict) and "$ref" in node and depth < 10:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return node
        cur = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cur, dict) or part not in cur:
                return node
            cur = cur[part]
        node, depth = cur, depth + 1
    return node


def _type_of(spec: dict, schema, raw=None) -> str:
    """Human-readable type, preferring the component name behind a $ref."""
    if isinstance(raw, dict) and isinstance(raw.get("$ref"), str):
        name = raw["$ref"].rsplit("/", 1)[-1]
        if name:
            return f"`{name}`"
    if not isinstance(schema, dict):
        return ""
    if schema.get("enum"):
        return "enum"
    t = schema.get("type")
    if t == "array":
        inner = schema.get("items") or {}
        return f"{_type_of(spec, _deref(spec, inner), inner) or 'any'}[]"
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    for combiner in ("oneOf", "anyOf", "allOf"):
        if schema.get(combiner):
            return combiner
    return str(t or "")


def _cell(text) -> str:
    """Flatten arbitrary text into something safe for a Markdown table cell."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s.replace("|", "\\|")


# ─────────────────────────────────────────────────────────────
# Strategy: GitHub repo → README + docs via API
# ─────────────────────────────────────────────────────────────
def handle_github(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    parts = [p for p in urlparse(det.url).path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ForgeError(f"Not a GitHub repo URL: {det.url}")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    api = f"https://api.github.com/repos/{owner}/{repo}"

    auth = {"Authorization": f"Bearer {opts.github_token}"} if opts.github_token else {}
    if not opts.github_token:
        _log(opts, "  note: set GITHUB_TOKEN to raise the GitHub API rate limit")

    docs: list[Doc] = []

    rr = fetcher.get(api + "/readme",
                     headers={**auth, "Accept": "application/vnd.github.raw"})
    if rr.status_code == 404:
        raise ForgeError(f"GitHub repo not found (or private): {owner}/{repo}")
    if rr.status_code == 403 and "rate limit" in rr.text.lower():
        raise ForgeError("GitHub API rate limit hit. Set GITHUB_TOKEN and retry.")
    if rr.status_code == 200:
        docs.append(Doc(det.url, f"{repo} — README",
                        _meta_header(det.url, "github-readme") + _decode(rr).strip()))

    tree = fetcher.get(api + "/git/trees/HEAD?recursive=1", headers=auth)
    if tree.status_code == 200:
        try:
            nodes = tree.json().get("tree", [])
        except ValueError:
            nodes = []
        cap = opts.limit()
        for node in nodes:
            if cap is not None and len(docs) >= cap:
                break
            p = node.get("path", "")
            low = p.lower()
            if not low.endswith((".md", ".mdx")):
                continue
            if not (low.startswith("docs/") or "/docs/" in low):
                continue
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{p}"
            fr = fetcher.get(raw)
            if fr.status_code == 200:
                docs.append(Doc(raw, p, _meta_header(raw, "github-doc") + _decode(fr).strip()))
                _log(opts, f"  [{len(docs)}] {p}")

    if not docs:
        raise ForgeError(f"No README or docs/*.md found in {owner}/{repo}")
    return docs


# ─────────────────────────────────────────────────────────────
# Strategy: raw markdown / text passthrough
# ─────────────────────────────────────────────────────────────
def handle_raw_text(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    body = det.body if det.body is not None else fetcher.text(det.url)
    name = os.path.basename(urlparse(det.url).path) or det.url
    return [Doc(det.url, name, _meta_header(det.url, "raw") + body.strip())]


# ─────────────────────────────────────────────────────────────
# Strategy: generic HTML (with optional crawl / JS)
# ─────────────────────────────────────────────────────────────
STRIP = ["nav", "header", "footer", "aside", "script", "style", "noscript",
         "form", "iframe", "[role=navigation]", "[role=banner]",
         "[role=contentinfo]", ".sidebar", ".navbar", ".toc",
         ".breadcrumb", ".ad", ".cookie", "[aria-hidden=true]"]
CONTENT = ["main", "article", "[role=main]", ".markdown-body",
           ".doc-content", ".content", ".prose", "#content", "#main"]


def _soup(html: str, parser: str = "html.parser"):
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise ForgeError("HTML extraction needs: pip install beautifulsoup4") from e
    return BeautifulSoup(html, parser)


def _html_to_md(html: str, url: str, soup=None) -> tuple[str, str]:
    try:
        from markdownify import markdownify as md
    except ImportError as e:
        raise ForgeError("HTML extraction needs: pip install markdownify") from e

    soup = soup if soup is not None else _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    title = title or "Untitled"

    for sel in STRIP:
        for el in soup.select(sel):
            el.decompose()

    main = None
    for sel in CONTENT:
        found = soup.select_one(sel)
        if found and len(found.get_text(strip=True)) > 200:
            main = found
            break
    main = main if main is not None else (soup.body or soup)

    body = md(str(main), heading_style="ATX", bullets="-")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, _meta_header(url, "html") + body


def handle_html(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    if opts.crawl:
        return _crawl_html(det.url, fetcher, opts)
    html = fetcher.html(det.url)
    title, doc = _html_to_md(html, det.url)
    return [Doc(det.url, title, doc)]


# Path segments that mark the start of a documentation section.
DOC_ROOTS = ("docs", "doc", "documentation", "guide", "guides", "manual",
             "reference", "learn", "handbook", "api")

_VERSION = re.compile(r"^v?\d+(\.\d+)*$", re.I)


def docs_scope(url: str) -> str:
    """The path prefix a crawl should stay inside, derived from the start URL.

    Docs usually share a domain with marketing, a blog and a changelog, so
    "same host" is far too wide a net — crawling from an Effect docs page that
    way walks straight into /podcast. Anchor on the documentation root instead:

        /docs/v3/getting-started/introduction/  ->  /docs/v3/
        /guide/setup                            ->  /guide/
        /some/deep/page                         ->  /some/deep/   (its folder)
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return "/"

    for i, part in enumerate(parts):
        if part.lower() in DOC_ROOTS:
            keep = parts[: i + 1]
            # Keep a version segment with it: /docs/v3/, not just /docs/.
            # It is not always the next segment — Pydantic files versions under
            # /docs/validation/2.11/ — and stopping at /docs/ there crawls every
            # version of the manual at once and calls the result one harvest.
            for j in range(i + 1, min(i + 4, len(parts))):
                if _VERSION.match(parts[j]):
                    keep = parts[: j + 1]
                    break
            return "/" + "/".join(keep) + "/"

    # No recognisable docs root: stay in the start page's own folder.
    folder = parts[:-1] if "." in parts[-1] or len(parts) > 1 else parts
    return "/" + "/".join(folder) + "/" if folder else "/"


def _asks_for_a_version(url: str) -> bool:
    """Does this URL name a particular version of the documentation?"""
    return any(_VERSION.match(p) for p in urlparse(url).path.split("/") if p)


def _probed_at_the_root(url: str, det: Detection) -> bool:
    """True when a detection came from probing the origin rather than from the
    URL the caller actually gave us."""
    return det.url != url and urlparse(det.url).path.count("/") == 1


def _normalize(url: str) -> str:
    """Drop the fragment and a trailing slash so `/intro` and `/intro/` are
    one page, not two fetches of the same content."""
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        url = url.replace(path, path[:-1], 1)
    return url


def _crawlable(link: str, host: str, prefix: str = "/") -> bool:
    p = urlparse(link)
    if p.scheme not in ("http", "https"):
        return False
    if (p.hostname or "").lower() != host:
        return False
    if p.path.lower().endswith(SKIP_EXT):
        return False
    # Compare with a trailing slash on both sides so /docs/v3 matches /docs/v3/.
    path = p.path if p.path.endswith("/") else p.path + "/"
    return path.startswith(prefix)


def _crawl_html(start: str, fetcher: Fetcher, opts: Options,
                stats: dict | None = None) -> list[Doc]:
    seen: set[str] = set()
    out: list[Doc] = []
    queue = deque([_normalize(start)])
    host = (urlparse(start).hostname or "").lower()

    # "Same host" is not the right boundary for a docs site — see docs_scope.
    if opts.scope == "host":
        prefix = "/"
    elif opts.scope in ("", "section", None):
        prefix = docs_scope(start)
    else:
        prefix = opts.scope if opts.scope.endswith("/") else opts.scope + "/"
    # 0 = no limit: crawl until the section is exhausted.
    limit = opts.limit()
    _log(opts, f"  crawling within {prefix}"
               f" ({'no page limit' if limit is None else f'up to {limit} pages'})")

    while queue and (limit is None or len(out) < limit):
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetcher.html(url)

            # Parse once: link discovery needs the nav _html_to_md strips out.
            soup = _soup(html)
            for a in soup.find_all("a", href=True):
                link = _normalize(urljoin(url, a["href"]))
                if link not in seen and link not in queue and _crawlable(link, host, prefix):
                    queue.append(link)

            title, doc = _html_to_md(html, url, soup=soup)
        except ForgeError as e:
            _log(opts, f"  skip {url}: {e}")
            continue
        except Exception as e:  # one broken page must not end the crawl
            _log(opts, f"  skip {url}: {type(e).__name__}: {e}")
            continue

        out.append(Doc(url, title, doc))
        _log(opts, f"  [{len(out)}] {url}")

        if queue and (limit is None or len(out) < limit):
            time.sleep(opts.delay)

    if stats is not None:
        # Anything still queued means max_pages cut the harvest short. Silent
        # truncation is worse than a slow crawl: you get a third of a manual
        # and no way to know it.
        stats["fetched"] = len(out)
        stats["remaining"] = len(queue)
        stats["truncated"] = bool(queue)
        # A crawl that drained its frontier reached everything linked inside
        # its scope. That is a real claim, and a weaker one than a sitemap:
        # pages nothing links to are invisible to it either way.
        stats["whole"] = not queue
        stats.setdefault("discovered", len(out) + len(queue))

    if not out:
        raise ForgeError(f"Crawl produced no pages from {start}")
    return out


# ─────────────────────────────────────────────────────────────
# Strategy: sitemap.xml
# ─────────────────────────────────────────────────────────────
#: Sections of a project's site that are emphatically not its documentation.
#: Astro's sitemap is mostly these: harvesting `astro.build` returned 34 blog
#: posts out of 40 pages and not one page of documentation.
_NOT_DOCS = re.compile(
    r"/(blog|news|posts?|articles?|changelog|releases?|careers?|jobs|pricing|"
    r"about|contact|team|events?|showcase|agencies|partners|sponsors|store|"
    r"shop|legal|privacy|terms|press|community)(/|$)", re.I)

#: …and the sections that are.
_DOCSY = re.compile(
    r"/(docs?|documentation|guide|guides|manual|reference|api|learn|tutorial)(/|$)", re.I)

#: Locale codes common on documentation sites. A curated list rather than
#: "any two letters", because `/go/`, `/js/` and `/ai/` are sections, not
#: languages, and dropping them would lose real documentation.
_LOCALES = {
    "ar", "bn", "cs", "da", "de", "el", "es", "fa", "fi", "fr", "he", "hi",
    "hu", "id", "it", "ja", "ko", "ms", "nl", "no", "pl", "pt", "ro", "ru",
    "sv", "th", "tr", "uk", "vi", "zh",
}
_LOCALE_SEGMENT = re.compile(r"^/([a-z]{2})(?:-[a-z]{2})?(?:/|$)", re.I)


def _focus_on_docs(urls: list[str], prefix: str) -> list[str]:
    """Drop the marketing when the harvest was pointed at a whole site.

    Only applies when scope is the entire host, which is what happens when
    resolution lands on a homepage rather than a docs root. Somebody asking
    for a technology's documentation does not want its careers page.
    """
    if prefix not in ("", "/"):
        return urls
    docsy = [u for u in urls if _DOCSY.search(urlparse(u).path)]
    if len(docsy) >= 5:
        return docsy
    trimmed = [u for u in urls if not _NOT_DOCS.search(urlparse(u).path)]
    return trimmed or urls


def _prefer_default_locale(urls: list[str]) -> list[str]:
    """One language, not all of them.

    A sitemap that lists every translation is sorted by locale, so a capped
    harvest of `docs.astro.build` returns Arabic — `/ar/` sorts first — and
    stops before reaching English. Storing every translation is no better: it
    multiplies the corpus by twenty and makes search return the same page in
    languages the caller cannot read.
    """
    groups: dict[str, list[str]] = {}
    for url in urls:
        match = _LOCALE_SEGMENT.match(urlparse(url).path)
        code = match.group(1).lower() if match else ""
        groups.setdefault(code if code in _LOCALES or code == "en" else "", []).append(url)
    if len(groups) < 2:
        return urls
    # Untagged pages are the default language; `en` is the default when the
    # site tags every language including its own.
    keep = groups.get("", []) + groups.get("en", [])
    return keep or urls


def _xml_soup(text: str):
    """Prefer a real XML parser, but degrade instead of exploding when lxml
    is not installed — the README used to call it optional."""
    for parser in ("lxml-xml", "xml", "html.parser"):
        try:
            return _soup(text, parser)
        except Exception:
            continue
    raise ForgeError("Could not parse sitemap XML")


def _sitemap_links(text: str, fetcher: Fetcher, opts: Options, depth: int = 0) -> list[str]:
    soup = _xml_soup(text)
    locs = [el.get_text(strip=True) for el in soup.find_all("loc")]
    locs = [l for l in locs if l]

    # A sitemap index points at more sitemaps; follow one level down.
    if soup.find("sitemapindex") is not None and depth < 2:
        nested: list[str] = []
        cap = opts.limit()
        for sm in locs:
            if cap is not None and len(nested) >= cap:
                break
            try:
                nested += _sitemap_links(fetcher.text(sm), fetcher, opts, depth + 1)
            except ForgeError as e:
                _log(opts, f"  skip sitemap {sm}: {e}")
        return nested
    return locs


def handle_sitemap(det: Detection, fetcher: Fetcher, opts: Options) -> list[Doc]:
    body = det.body if det.body is not None else fetcher.text(det.url)
    links = _sitemap_links(body, fetcher, opts)
    cap = opts.limit()
    if cap is not None:
        links = links[:cap]
    if not links:
        raise ForgeError(f"No <loc> entries found in {det.url}")

    out: list[Doc] = []
    for link in links:
        try:
            html = fetcher.html(link)
            title, doc = _html_to_md(html, link)
        except ForgeError as e:
            _log(opts, f"  skip {link}: {e}")
            continue
        except Exception as e:  # one broken page must not end the run
            _log(opts, f"  skip {link}: {type(e).__name__}: {e}")
            continue
        out.append(Doc(link, title, doc))
        _log(opts, f"  [{len(out)}] {link}")
        time.sleep(opts.delay)

    if not out:
        raise ForgeError(f"Every page listed in {det.url} failed to fetch")
    return out


# ─────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────
def _meta_header(url: str, kind: str) -> str:
    return (f"<!-- source: {url} | type: {kind} | "
            f"scraped: {time.strftime('%Y-%m-%d %H:%M')} -->\n\n")


def _slug(url: str) -> str:
    """Filename stem for a URL. Includes the host and a short hash so pages
    from different sites (or different query strings) never collide."""
    p = urlparse(url)
    host = re.sub(r"[^a-zA-Z0-9]+", "-", (p.hostname or "")).strip("-")
    path = re.sub(r"[^a-zA-Z0-9\-_.]+", "-", p.path.strip("/").replace("/", "-")).strip("-")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    stem = "-".join(x for x in (host, path or "index") if x)[:80].strip("-")
    return f"{stem or 'doc'}-{digest}"


HANDLERS = {
    "llms_txt": handle_llms_txt,
    "openapi": handle_openapi,
    "sitemap": handle_sitemap,
    "github": handle_github,
    "raw_text": handle_raw_text,
    "html": handle_html,
}


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────
def forge(url: str, opts: Options | None = None, fetcher: Fetcher | None = None) -> list[Doc]:
    """Extract `url` into a list of Docs. This is the entry point the MCP
    server and the web app both call."""
    opts = opts or Options()
    if opts.force and opts.force not in HANDLERS:
        raise ForgeError(f"Unknown strategy {opts.force!r}. Choose from: {', '.join(HANDLERS)}")

    own = fetcher is None
    fetcher = fetcher or Fetcher(opts)
    try:
        if opts.force:
            det = Detection(opts.force, url)
        else:
            det = detect_source(url, fetcher)
        _log(opts, f"Detected source type: {det.kind}")
        return HANDLERS[det.kind](det, fetcher, opts)
    finally:
        if own:
            fetcher.close()


#: An index that still names a fuller file we could not fetch. Storing it is
#: storing a table of contents, and the caller has to be told so.
_NAMES_A_FULLER_FILE = re.compile(r"llms-(full|medium)\.txt", re.I)


def _note_coverage(stats: dict | None, det: "Detection", docs: list,
                   found: "DocMap | None" = None) -> None:
    """Record whether this harvest actually got the whole documentation.

    `whole` is three-valued on purpose. `True` is a claim we can defend, `False`
    is a known gap, and `None` means nobody counted — which must never be
    presented to a model as if it were `True`.
    """
    if stats is None:
        return
    whole: bool | None
    if det.kind == "llms_txt":
        body = "\n".join(d.markdown for d in docs)[:200_000]
        # A full dump is the site handing over everything it has. An index that
        # still points at a fuller file is the opposite, and is exactly what
        # got stored as `complete` for seven technologies.
        whole = not (det.url.lower().endswith("llms.txt")
                     and _NAMES_A_FULLER_FILE.search(body))
        if not whole:
            missing = found.dump_bytes if found else 0
            stats["reason"] = (
                "stored an llms.txt index that names a fuller file which could "
                "not be fetched"
                + (f" ({missing:,} characters of it)" if missing else ""))
    else:
        # A spec, a single file, or a repository's Markdown tree: each is
        # enumerated in full by its handler.
        whole = True
    stats["whole"] = whole
    if found is not None:
        stats["map"] = found.as_dict()
        # What the site says it has beats how many pieces we happened to cut
        # its dump into. Only fall back to our own count when nobody could say.
        stats.setdefault("discovered", found.expected or len(docs))
    else:
        stats.setdefault("discovered", len(docs))


def harvest(url: str, opts: Options | None = None, fetcher: Fetcher | None = None,
            stats: dict | None = None) -> tuple[list[Doc], str]:
    """Get a WHOLE documentation set from one starting URL.

    `forge()` answers "extract this URL". This answers "extract this
    technology", which is a different question — the caller has one link into a
    docs site and wants everything under it. Strategies, best first:

      1. llms-full.txt / llms.txt — the site already published itself for us.
      2. sitemap.xml, filtered to the docs section — complete and cheap, and it
         finds pages no nav links to.
      3. A scoped crawl — works anywhere, but only reaches what is linked.

    Returns the documents and the name of the strategy that produced them.
    """
    opts = opts or Options()
    own = fetcher is None
    fetcher = fetcher or Fetcher(opts)
    try:
        det = detect_source(url, fetcher)
        if det.kind in ("llms_txt", "openapi", "github", "raw_text"):
            # A site publishes one llms.txt for its current release. When the
            # caller asked for a specific version, handing them that file would
            # answer a question they did not ask — quietly, and with the wrong
            # version. Crawl the version they named instead.
            if _asks_for_a_version(url) and _probed_at_the_root(url, det):
                _log(opts, "  ignoring the site-wide llms.txt: "
                           "the URL asks for one version of the docs")
            else:
                _log(opts, f"  harvesting via {det.kind}")
                docs = HANDLERS[det.kind](det, fetcher, opts)
                # This is the path with no enumeration of its own: one artifact
                # arrives and there is nothing to compare it against. Ask the
                # site what it has, so completeness can be measured rather
                # than assumed.
                found = discover(url, fetcher, opts) if stats is not None else None
                _note_coverage(stats, det, docs, found)
                return docs, det.kind

        prefix = docs_scope(url) if opts.scope in ("", "section", None) else (
            "/" if opts.scope == "host" else opts.scope)
        host = (urlparse(url).hostname or "").lower()

        sitemap = find_sitemap(url, fetcher, opts)
        if sitemap:
            try:
                links = _sitemap_links(fetcher.text(sitemap), fetcher, opts)
            except ForgeError:
                links = []
            scoped = [l for l in dict.fromkeys(_normalize(l) for l in links)
                      if _crawlable(l, host, prefix)]
            before = len(scoped)
            scoped = _prefer_default_locale(_focus_on_docs(scoped, prefix))
            if len(scoped) != before:
                _log(opts, f"  narrowed {before} sitemap URLs to {len(scoped)} "
                           f"(documentation, default language)")
            # One or two hits usually means the sitemap does not really cover
            # the docs; a crawl will do better than a near-empty list.
            if len(scoped) >= 3:
                _log(opts, f"  harvesting {len(scoped)} pages from the sitemap")
                cap = opts.limit()
                if stats is not None:
                    over = 0 if cap is None else max(0, len(scoped) - cap)
                    stats["discovered"] = len(scoped)
                    stats["truncated"] = over > 0
                    stats["remaining"] = over
                out: list[Doc] = []
                for link in (scoped if cap is None else scoped[:cap]):
                    try:
                        title, body = _html_to_md(fetcher.html(link), link)
                    except Exception as e:
                        _log(opts, f"  skip {link}: {e}")
                        continue
                    out.append(Doc(link, title, body))
                    _log(opts, f"  [{len(out)}] {link}")
                    time.sleep(opts.delay)
                if out:
                    # The sitemap is the site's own list of what exists, so
                    # this is the one strategy that can measure completeness
                    # against something other than its own effort.
                    if stats is not None:
                        stats["whole"] = len(out) >= len(scoped)
                        if not stats["whole"]:
                            stats["reason"] = (
                                f"stored {len(out)} of the {len(scoped)} pages "
                                f"the sitemap lists")
                    return out, "sitemap"

        _log(opts, "  harvesting by crawl")
        crawl_opts = replace(opts, crawl=True)
        return _crawl_html(url, fetcher, crawl_opts, stats), "crawl"
    finally:
        if own:
            fetcher.close()


def combine(docs: list[Doc], url: str, strategy: str = "") -> str:
    """One Markdown file for a whole technology: contents, then every page."""
    host = urlparse(url).hostname or url
    lines = [
        f"# {host} documentation",
        "",
        f"<!-- harvested: {len(docs)} pages | from: {url} | via: {strategy} | "
        f"{time.strftime('%Y-%m-%d %H:%M')} -->",
        "",
        "## Contents",
        "",
    ]
    for i, d in enumerate(docs, 1):
        lines.append(f"{i}. [{d.title}]({d.url})")
    lines.append("")

    for d in docs:
        body = re.sub(r"^<!-- source:.*?-->\n+", "", d.markdown, count=1, flags=re.S)
        lines += ["", "---", "", f"## {d.title}", "", f"Source: <{d.url}>", "", body.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def write_docs(docs: list[Doc], out_dir: str, single_file: bool = False,
               source_url: str = "") -> list[str]:
    """Write Docs to disk; returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    if single_file:
        combined = "\n\n---\n\n".join(d.markdown for d in docs)
        path = os.path.join(out_dir, _slug(source_url or (docs[0].url if docs else "doc")) + "-combined.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(combined)
        written.append(path)
    else:
        for d in docs:
            path = os.path.join(out_dir, _slug(d.url) + ".md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(d.markdown)
            written.append(path)
    return written


# ─────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    # Before parse_args: --help prints the module docstring, which contains
    # arrows, and argparse writes it straight to a cp1252 console.
    enable_utf8_console()

    ap = argparse.ArgumentParser(
        prog="docsforge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default="./docs_md", help="output directory")
    ap.add_argument("--crawl", action="store_true", help="follow same-host links")
    ap.add_argument("--max-pages", type=int, default=25, metavar="N",
                    help="page ceiling for a crawl; 0 means no limit")
    ap.add_argument("--js", action="store_true", help="render JS (needs playwright)")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--single-file", action="store_true")
    ap.add_argument("--force", choices=list(HANDLERS), help="skip detection, force a strategy")
    ap.add_argument("--allow-private", action="store_true",
                    help="permit private/loopback hosts (off by default)")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=f"docsforge {__version__}")
    args = ap.parse_args(argv)

    opts = Options(
        crawl=args.crawl,
        max_pages=args.max_pages,
        js=args.js,
        delay=args.delay,
        force=args.force,
        verbose=not args.quiet,
    )
    if args.allow_private:
        opts.allow_private = True

    try:
        docs = forge(args.url, opts)
        paths = write_docs(docs, args.out, args.single_file, source_url=args.url)
    except ForgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    for p in paths:
        print(f"  wrote {p}")
    print(f"\nDone. {len(docs)} document(s) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
