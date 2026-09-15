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
  python -m docsforge https://docs.stripe.com
  python -m docsforge https://api.example.com/openapi.json
  python -m docsforge https://github.com/tiangolo/fastapi
  python -m docsforge https://docs.example.com --crawl --max-pages 50
  python -m docsforge https://site.com --js            # JS-rendered
  python -m docsforge https://site.com --single-file   # one combined .md

Library use:
  from docsforge.core.engine import forge, Options
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
import heapq
import time
import zlib
from collections import deque
import threading
from contextlib import contextmanager

from docsforge.core import llmsfinder
from docsforge.core import reasoning
from docsforge.core import versions
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import NamedTuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests

# Pure data and classification, no HTTP and no store — importing these here
# cannot create a cycle, and keeps evidence collected where the soup already is.
from docsforge.core.federation import Federation
from docsforge.core.observation import Ledger, Observation, ancestry, bucket

# One version, defined on the package where packaging can read it without
# importing the engine. Re-exported here because `--version` and the
# User-Agent are built in this module.
from docsforge import __version__

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
    #: Which release the caller asked for, when they named one. This decides
    #: which of the two acquisition pathways a harvest takes, so it has to
    #: reach discovery rather than only the label at the end: a site
    #: publishes `llms.txt` for its *current* release, and answering "give me
    #: 1.10" with it stores the wrong documentation under the right name.
    #: Empty means "whatever is current", which is what that file is for.
    version: str = ""
    # Fetching a user-supplied URL server-side is an SSRF vector, so private /
    # loopback targets are refused unless explicitly allowed.
    allow_private: bool = field(
        default_factory=lambda: os.environ.get("DOCSFORGE_ALLOW_PRIVATE", "") not in ("", "0", "false", "False")
    )
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    verbose: bool = True
    #: How many pages may be in flight at once. Fetching is almost all of a
    #: crawl's wall-clock time and almost none of its CPU, so this is where the
    #: only real speedup lives. It bounds requests in flight, not politeness:
    #: `delay` still sets the minimum gap between two requests to the same host,
    #: so raising this makes a crawl overlap its waiting rather than hammer.
    workers: int = 4

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
#: The most requests that may be in flight to one host at once. Distinct from
#: `Options.workers`, which bounds the crawl as a whole: a federated harvest
#: touching three hosts may have more requests open than any one host sees. A
#: rate limit alone does not bound concurrency — four requests spaced 0.4s apart
#: are still four open sockets if each takes two seconds — and it is open
#: sockets, not request frequency, that a small documentation host notices.
HOST_CONCURRENCY = 4


class _Pace:
    """The minimum gap between two requests to the same host, and a cap on how
    many may be open to it at once.

    Politeness is per host, not per crawl, and it spaces request *starts*
    rather than sleeping between completed pages. That distinction is the whole
    of Phase 3's speedup: with a 0.4s delay and a page taking 0.8s to come
    back, sleeping between completions costs 1.2s per page and overlaps
    nothing. Spacing the starts costs the host the same 0.4s while several
    requests are in flight, so the crawl waits once rather than once per page.

    Slots are reserved under the lock and slept off outside it, so a worker
    waiting its turn is not also holding every other worker up.
    """

    def __init__(self, delay: float, concurrency: int = HOST_CONCURRENCY):
        self.delay = max(0.0, delay)
        self.concurrency = max(1, concurrency)
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._slots: dict[str, threading.Semaphore] = {}
        #: Only for the assertion in the tests: the high-water mark of requests
        #: open to any one host. A cap nobody measures is a comment.
        self.peak: dict[str, int] = {}
        self._open: dict[str, int] = {}

    def _semaphore(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._slots:
                self._slots[host] = threading.Semaphore(self.concurrency)
            return self._slots[host]

    @contextmanager
    def host(self, url: str):
        """Hold one of this host's slots for the duration of a request."""
        name = (urlparse(url).hostname or "").lower()
        slot = self._semaphore(name)
        slot.acquire()
        with self._lock:
            self._open[name] = self._open.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self._open[name])
        try:
            yield
        finally:
            with self._lock:
                self._open[name] -= 1
            slot.release()

    def wait(self, url: str) -> None:
        if not self.delay:
            return
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            when = max(time.monotonic(), self._next.get(host, 0.0))
            self._next[host] = when + self.delay
        gap = when - time.monotonic()
        if gap > 0:
            time.sleep(gap)


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
        host = parsed.hostname or ""
        if _resolves_private(host):
            where = _private_address_of(host)
            raise ForgeError(
                f"Refusing to fetch private/loopback address: {host}"
                + (f" resolves to {where}" if where else "")
                + ". Set DOCSFORGE_ALLOW_PRIVATE=1 to permit it."
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

    def render(self, url: str) -> str:
        """Fetch with JavaScript executed, whatever the run asked for.

        `html()` renders only when the run opted in. This is the one-shot
        escape hatch for a page that turned out to be a shell, and it is what
        makes `--js` unnecessary on the sites that used to need it.
        """
        return self._render(url)

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
        encoding = getattr(r, "apparent_encoding", None) or "utf-8"
        try:
            r.encoding = encoding
        except (AttributeError, TypeError):
            pass
    return r.text


#: RFC 6052's well-known prefix for embedding an IPv4 address inside an
#: IPv6 one. A DNS64 resolver hands these out on NAT64 networks — common on
#: mobile carriers, IPv6-only CI runners, and plenty of home connections —
#: and Python reports them as `is_reserved`, because the *prefix* is
#: reserved. The address they carry is whatever IPv4 is in the low 32 bits,
#: which is usually an ordinary public host.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def _unwrap_nat64(ip):
    """The IPv4 address a NAT64 address carries, or the address itself.

    Judging the wrapper instead of its contents is how `github.com` came to
    be refused as a "private/loopback address" on a NAT64 network: it
    resolves to 64:ff9b::14cf:4952, which carries the entirely public
    20.207.73.82. Unwrapping loses no protection — a NAT64 address carrying
    10.0.0.1 still unwraps to 10.0.0.1 and is still refused.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN:
        return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_private_address(ip) -> bool:
    ip = _unwrap_nat64(ip)
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _addresses(host: str) -> list:
    """Every address `host` resolves to, or nothing when the lookup itself
    could not be completed. The request that follows performs the same lookup
    and reports whatever it finds, which is the real error.

    `OSError`, not only `socket.gaierror`. A name the resolver does not know
    normally comes back as `EAI_NONAME`, which CPython raises as `gaierror`;
    a resolver that fails *systemically* answers `EAI_SYSTEM` instead, and
    CPython raises that as a plain `OSError` carrying the libc errno. Vercel's
    runtime answers the latter for every name that does not exist —
    `[Errno 16] Device or resource busy` — and since `find_docs` probes
    guessed domains that mostly do not exist, the first miss crashed the whole
    resolution before a single page was fetched, while a URL to a real host
    went through untouched. Locally the same miss is a `gaierror`, which is
    why no test saw it.

    Catching the parent class opens nothing: the request's own lookup goes
    through the same resolver a moment later, so a name this could not
    resolve is a name it cannot connect to either, and an address that does
    come back is still judged exactly as before.
    """
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:                 # gaierror is one of these; EAI_SYSTEM is another
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def _resolves_private(host: str) -> bool:
    return any(_is_private_address(ip) for ip in _addresses(host))


def _private_address_of(host: str) -> str:
    """The address that made `_resolves_private` say yes, for the message.

    Naming only the host left the refusal undiagnosable: a NAT64 network
    refusing `github.com` reads as a bug in DocsForge, in the sandbox, or
    in the site, and the one fact that distinguishes them — what the name
    actually resolved to — was the one thing not reported.
    """
    return next((str(ip) for ip in _addresses(host) if _is_private_address(ip)), "")


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


def _looks_like_shell(html: str) -> bool:
    """An empty container beside a script bundle.

    The diagnosis that makes `--js` automatic. A page with negligible visible
    text that nonetheless ships JavaScript has not failed to be documentation;
    it has failed to be *rendered*, and those are different failures deserving
    different responses.
    """
    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "",
                      flags=re.S | re.I)
    if len(" ".join(re.sub(r"<[^>]+>", " ", stripped).split())) >= MIN_MAIN_CHARS:
        return False
    return bool(re.search(r"<script[^>]+src=", html or "", re.I))


#: `<meta http-equiv="refresh" content="0; url=…">` and its script twin, the
#: two ways a page says "the content is over there".
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*"""
    r"""["'][^"']*url\s*=\s*([^"'\s>]+)""", re.I)
_JS_REPLACE = re.compile(
    r"""location\s*\.\s*(?:replace\s*\(|href\s*=)\s*["']([^"']+)["']""", re.I)
#: Deliberately NOT a canonical link. Nearly every real page carries one, and
#: treating it as a redirect sent a page with prose in it off to whatever it
#: named — caught by `test_a_real_page_is_never_mistaken_for_a_signpost`, which
#: failed the moment it was written. A canonical URL says "index me as that";
#: only a refresh or a `location.replace` says "the content is not here".

#: A redirect stub is small. Anything with real prose in it is a page that
#: happens to mention a redirect, not a signpost.
REDIRECT_STUB_CHARS = 4_000

#: How many signposts to follow before deciding it is a loop.
REDIRECT_HOPS = 3


def _redirect_target(html: str, url: str) -> str:
    """Where a client-side redirect stub points, or ""."""
    if len(html or "") > REDIRECT_STUB_CHARS:
        return ""
    for pattern in (_JS_REPLACE, _META_REFRESH):
        found = pattern.search(html or "")
        if found:
            target = urljoin(url, found.group(1).strip())
            if _normalize(target) != _normalize(url):
                return target
    return ""


def _extract_page(link: str, fetcher: Fetcher, opts: Options) -> tuple[str, str]:
    """Fetch and extract one page, rendering once if it turns out to be a shell.

    Exactly one retry. A site that renders nothing without JavaScript is a
    known, common shape and worth the second request; a site that renders
    nothing *with* it is broken, and asking twice more will not change that.

    A client-side redirect is followed first, and that is not the same thing as
    a shell. `docs.pytorch.org/docs/stable/…` serves a 1,400-byte stub for every
    page — `location.replace("../../2.14/…")` plus a meta refresh and a
    canonical link — because `stable` is an alias for the current release.
    HTTP never redirects, so the fetcher lands on the stub, extraction finds no
    prose in it, and the page is refused.

    Measured 2026-09-11: every page of PyTorch's documentation, several
    thousand of them, refused with "no recognised content container and nothing
    dense enough to be prose". The resolver has followed these since
    `probe_docs_root` was written; the harvester never learned to, so a
    versioned-alias docs site — the ordinary shape for Sphinx and Read the Docs
    — could be resolved to and then not harvested.
    """
    seen = {_normalize(link)}
    for _ in range(REDIRECT_HOPS):
        html = fetcher.html(link)
        target = _redirect_target(html, link)
        if not target or _normalize(target) in seen:
            break
        _log(opts, f"  {link} redirects to {target}")
        seen.add(_normalize(target))
        link = target
    try:
        return _html_to_md(html, link)
    except ForgeError:
        if opts.js or not _looks_like_shell(html):
            raise
        _log(opts, f"  {link} is a JS shell; retrying rendered")
        return _html_to_md(fetcher.render(link), link)


#: Generators that publish a machine-readable list of their own pages.
MANIFEST_PATHS = (("mkdocs", "search/search_index.json"), ("sphinx", "objects.inv"))


def _mkdocs_pages(text: str, base: str) -> list[str]:
    """Page URLs from a MkDocs / Material search index."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    found = []
    for doc in (data.get("docs") or []):
        where = (doc.get("location") or "").split("#")[0]
        if where:
            found.append(urljoin(base, where))
    return list(dict.fromkeys(found))


def _sphinx_pages(raw: bytes, base: str) -> list[str]:
    """Page URLs from a Sphinx `objects.inv`.

    Four plain-text header lines, then a zlib stream of
    `name domain:role priority uri dispname` records. A `uri` ending in `$`
    means "append the object's name as the anchor", so either way the page is
    everything before the fragment.
    """
    _head, marker, packed = raw.partition(
        b"# The remainder of this file is compressed using zlib.\n")
    if not marker or not packed:
        return []
    try:
        body = zlib.decompress(packed).decode("utf-8", "replace")
    except zlib.error:
        return []
    found = []
    for line in body.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        where = parts[3].split("#")[0].rstrip("$")
        if where:
            found.append(urljoin(base, where))
    return list(dict.fromkeys(found))


def site_manifest(url: str, fetcher: Fetcher, opts: Options) -> tuple[list[str], str]:
    """The site's own list of its pages, and the generator that published it.

    Worth more than a sitemap, and the strongest form a completeness claim can
    take. A sitemap is a hint addressed to crawlers: it may carry marketing
    pages, redirects and URLs that no longer resolve. `search_index.json` and
    `objects.inv` *are* the documentation's own table of contents — the
    generator wrote them from the same source it rendered the pages from. Where
    one exists, `expected` stops being an estimate and becomes the site's own
    count, and it costs one request to find out.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    roots = list(dict.fromkeys([origin + docs_scope(url), origin + "/"]))

    for root in roots:
        for kind, path in MANIFEST_PATHS:
            probe = urljoin(root, path)
            try:
                r = fetcher.get(probe, timeout=10, allow_redirects=True)
            except ForgeError:
                continue
            if getattr(r, "status_code", 0) != 200:
                continue
            if kind == "mkdocs":
                pages = _mkdocs_pages(getattr(r, "text", "") or "", root)
            else:
                raw = getattr(r, "content", None)
                if raw is None:
                    raw = (getattr(r, "text", "") or "").encode("utf-8", "replace")
                pages = _sphinx_pages(raw, root)
            if len(pages) >= 3:
                _log(opts, f"  {kind} manifest at {probe}: {len(pages)} pages, "
                           f"the site's own count")
                return pages, kind
    return [], ""


@dataclass(frozen=True)
class Probe:
    """What one GET tells you about a corpus before committing to crawl it.

    This is the "pay a little, as you go" purchase at its cheapest: a single
    request that decides whether the next few hundred are worth making. It is
    what `go.dev/ref/spec` needed and never got — measured, that corpus is one
    1.19 MB document with its own table of contents, and crawling it as a tree
    finds one page and stores none.
    """

    chars: int = 0          # extracted markdown length, AFTER any render
    anchors: int = 0        # in-page `#` links: the page's own contents list
    links: int = 0          # distinct in-scope links: the crawl's opening breadth
    manifest: int = 0       # pages the site lists itself; 0 if it publishes none
    generator: str = ""     # which generator published that list
    failed: str = ""        # why the probe learned nothing, if it did not
    #: The opening of the extracted text. Carried so a decision point that needs
    #: to *read* the corpus does not have to fetch it a second time — the probe
    #: has already paid for this page.
    sample: str = ""

    @property
    def magnitude(self) -> int:
        """A rough page count. The site's own list beats counting links."""
        return self.manifest or self.links


def probe(url: str, fetcher: Fetcher | None = None,
          opts: Options | None = None) -> Probe:
    """Measure one corpus cheaply: one page fetch, plus the manifest lookup.

    Deliberately tolerant. A probe that raises turns a corpus that is merely
    hard to measure into a corpus that cannot be harvested, which is a worse
    outcome than harvesting it as a `tree` — the default the unprobed code has
    always used. A failed probe therefore returns a `Probe` that says so.
    """
    opts = opts or Options(delay=0.0)
    own = fetcher is None
    fetcher = fetcher or Fetcher(opts)
    try:
        try:
            html = fetcher.html(url)
            try:
                markdown = _html_to_md(html, url)[1]
            except ForgeError:
                if _looks_like_shell(html):
                    # Invariant: shape must be decided AFTER the render
                    # decision. A JS-driven API reference measures as 2 KB of
                    # shell and classifies as a tree, which is the one mistake
                    # that turns an exact count into a guess.
                    html = fetcher.render(url)
                    markdown = _html_to_md(html, url)[1]
                else:
                    raise
        except ForgeError as e:
            return Probe(failed=str(e))

        soup = _soup(html)
        scope = docs_scope(url)
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        anchors, seen = 0, set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#"):
                anchors += 1
                continue
            full = urljoin(url, href).split("#")[0]
            if full.startswith(origin) and urlparse(full).path.startswith(scope):
                seen.add(full)
        seen.discard(url.split("#")[0])

        try:
            pages, generator = site_manifest(url, fetcher, opts)
        except ForgeError:
            pages, generator = [], ""

        return Probe(chars=len(markdown), anchors=anchors, links=len(seen),
                     manifest=len(pages), generator=generator,
                     sample=markdown[:reasoning.MAX_SAMPLE])
    finally:
        if own:
            fetcher.close()


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


def _median_span(text: str, hits: list) -> float:
    """Median characters between one heading and the next."""
    spans = []
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        spans.append(end - hit.start())
    return _median(sorted(spans)) if spans else 0.0


def _split_dump(text: str, above: int = SPLIT_ABOVE) -> list[tuple[str, str]]:
    """Cut a large single-file dump into pages on its own headings.

    Returns [(title, chunk), ...], or [] to leave the text alone. The heading
    level is chosen by result rather than assumed: whichever of `#`, `##` or
    `###` yields the most pages without going silly is the one the document
    actually uses for sections.
    """
    if len(text) < above:
        return []

    levels: list[tuple[str, list]] = []
    for prefix in ("#", "##", "###"):
        hits = list(re.finditer(rf"^{prefix}[ \t]+(\S[^\n]*)$", text, re.M))
        if SPLIT_MIN_PARTS <= len(hits) <= SPLIT_MAX_PARTS:
            levels.append((prefix, hits))
    if not levels:
        return []

    # Choose the level by the size of the pages it makes, not by how many.
    #
    # "Whichever yields the most pages" counts headings, and a heading count
    # says nothing about what is under it. Measured on
    # `docs.langchain.com/llms-full.txt`, 6,749,200 characters:
    #
    #       level   headings   median span
    #       #           2,528          179     per-page titles, nearly empty
    #       ##          3,371        1,066     the document's real sections
    #       ###         2,081          961
    #
    # Most-parts picks `##` here and happens to be right, but only by luck:
    # `#` has 2,528 headings with 179 characters under each, and on a document
    # whose title level is used slightly more often it would win and cut the
    # corpus into two and a half thousand empty titles.
    #
    # So: the coarsest level whose pages are the size of pages. Bounded at
    # both ends — too small and they are fragments, too large and they are not
    # pages, which is the judgement `SPLIT_ABOVE` already makes about the
    # document as a whole. Where nothing lands inside the window, take the
    # level that misses by least: a corpus of genuinely short pages (this one,
    # at 1,066) should still be cut where its author cut it.
    scored = []
    for prefix, hits in levels:
        median = _median_span(text, hits)
        if median < llmsfinder.STUB_MEDIAN:
            miss = llmsfinder.STUB_MEDIAN - median
        elif median > SPLIT_ABOVE:
            miss = median - SPLIT_ABOVE
        else:
            miss = 0.0
        scored.append((miss, prefix, hits))

    inside = [row for row in scored if row[0] == 0.0]
    best_miss, best, best_hits = (inside or sorted(scored, key=lambda r: r[0]))[0]

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


def _classify_manifest_links(links: list[tuple[str, str]], base_url: str
                             ) -> tuple[list[tuple[str, str]], int]:
    """Split manifest links `parse_llms_links()` already validated into ones
    this harvest will try to acquire and ones it intentionally leaves out.

    The exclusion applied here is host: a link off the site the docs are
    published on (a GitHub badge, a support form, a partner's own
    changelog) is not part of the documentation being asked for, and
    counting it toward `expected` would make an untouchable page look like
    a missing one. `parse_llms_links()` has already dropped the invalid
    kind (bad scheme, anchors, mailto), so what is left to sort is real
    absolute URLs — actionable ones and off-site ones.

    Returns (actionable_links, excluded_count).
    """
    host = (urlparse(base_url).hostname or "").lower()
    actionable, excluded = [], 0
    for title, link in links:
        if (urlparse(link).hostname or "").lower() != host:
            excluded += 1
            continue
        actionable.append((title, link))
    return actionable, excluded


def _categorize_failure(exc: Exception) -> str:
    """A coarse bucket for a failed manifest-page fetch.

    Read off what `Fetcher` and `_extract_page` actually raise — a
    `ForgeError` wrapping either a `requests` exception or an HTTP status —
    rather than a parallel error-code system. Coarse on purpose: enough for
    a future retry pass to tell "worth trying again" apart from "this page
    will never work" without over-building on a single hardening pass.
    """
    cause = exc.__cause__
    if isinstance(cause, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(cause, requests.exceptions.RequestException):
        return "network_error"
    msg = str(exc)
    if re.match(r"HTTP \d+", msg):
        return "http_error"
    if "Not a text document" in msg:
        return "unsupported_content"
    if "reads like documentation" in msg:
        return "extraction_failure"
    return "invalid_response"


def _publish_denominator(stats: dict | None, fetcher: Fetcher, discovered: int,
                         expected: int, already_in_hand: int) -> None:
    """Say how many pages are promised *before* fetching them, not after.

    A published manifest is the one strategy that knows its exact
    denominator up front — that is the whole reason its coverage claim is
    stronger than a sitemap's. Writing it only once the fetching finished
    threw that away for the entire time it would have been useful: a
    229-page harvest showed "fetched 40 pages" for ten minutes when it
    could have said "40/229".

    `already_in_hand` is documents obtained before the loop starts — a
    hybrid's root prose, which arrived with the manifest itself — counted
    now so the progress figure and the denominator describe the same set.
    """
    if stats is not None:
        stats.setdefault("expected", expected)
        stats["discovered"] = discovered
    note_page = getattr(fetcher, "page_fetched", None)
    if callable(note_page):
        for _ in range(already_in_hand):
            note_page()


def _acquire_manifest_links(links: list[tuple[str, str]], fetcher: Fetcher,
                            opts: Options) -> tuple[list[Doc], list[dict]]:
    """Fetch every manifest link, keeping successes and failures apart.

    A failure here is one page out of a promised set, not the whole
    acquisition, so it is recorded and skipped rather than allowed to abort
    the rest of the manifest or vanish silently. Each failure record keeps
    a normalized URL, a coarse category, and a short detail — enough for a
    future targeted retry to operate on the failed subset instead of
    reacquiring everything, without dumping a full traceback into a
    user-facing result.
    """
    docs: list[Doc] = []
    failed: list[dict] = []
    #: Optional, duck-typed like `sink`: a fetcher that wants to report
    #: progress says so by having this. A Markdown twin is fetched with
    #: `text()`, which no progress counter can hook the way it hooks
    #: `html()` -- `text()` also fetches manifests, robots.txt and sitemaps,
    #: and counting those as pages would inflate the very number the
    #: coverage claim rests on. So the acquisition loop, which is the one
    #: place that knows a *documentation page* was just obtained, says so.
    note_page = getattr(fetcher, "page_fetched", None)

    #: A manifest is a list of pages on one host, so acquiring it is a crawl
    #: in everything but name -- 211 requests, for mojolang.org. `_crawl_html`
    #: has spaced its requests to a host since the beginning; this path
    #: ignored `opts.delay` outright and fired the lot back to back, which is
    #: how a small documentation host learns to refuse us. Sequential here, so
    #: spacing the starts is the whole of the politeness: there is never more
    #: than one request open.
    pace = _Pace(opts.delay)

    for title, link_url in links:
        pace.wait(link_url)
        try:
            if llmsfinder.is_markdown_link(link_url):
                text = fetcher.text(link_url, timeout=MAP_TIMEOUT)
                docs.append(Doc(link_url, title, _meta_header(link_url, "llms_txt") + text))
                if callable(note_page):
                    # Only here: the branch below goes through `html()`,
                    # which such a fetcher already counts for itself.
                    note_page()
            else:
                # `_extract_page` returns the page with its provenance comment
                # already on it; prepending another here put `<!-- source: … -->`
                # twice on every HTML page an llms.txt index pointed at.
                doc_title, md = _extract_page(link_url, fetcher, opts)
                docs.append(Doc(link_url, doc_title or title, md))
        except Exception as e:
            failed.append({
                "url": _normalize(link_url),
                "category": _categorize_failure(e),
                "detail": str(e)[:200],
            })
    return docs, failed


def handle_llms_txt(det: Detection, fetcher: Fetcher, opts: Options,
                    stats: dict | None = None,
                    restrict_links: list[tuple[str, str]] | None = None,
                    drop_root: bool = False) -> list[Doc]:
    """Acquire documentation from a detected llms.txt / llms-full.txt source.

    `restrict_links` is set by `harvest()` when the published file is not
    what the caller asked for but part of it is — one release out of
    several, or one section of a site (see `_scope_site_wide_llms`). When
    present it replaces whatever `parse_llms_links()` would find in the
    body, so acquisition only ever touches pages already shown to belong to
    what was asked for.
    """
    body = det.body if det.body is not None else fetcher.text(det.url, timeout=DUMP_TIMEOUT)
    body = body.strip()

    shape = llmsfinder.classify_llms_shape(body, det.url)

    # Whatever shape it turns out to be, a file that states which release it
    # documents has answered a question the URL usually cannot. Recorded
    # here, once, for every branch below; what to do with it is the caller's
    # decision, not this handler's.
    if stats is not None:
        declared = llmsfinder.declared_version(body)
        if declared:
            stats["declared_version"] = declared
            _log(opts, f"  the manifest states it documents version {declared}")

    if shape in ("index", "hybrid"):
        raw_links = restrict_links if restrict_links is not None else llmsfinder.parse_llms_links(body, det.url)
        links, excluded = _classify_manifest_links(raw_links, det.url)
        if excluded:
            _log(opts, f"  excluded {excluded} off-site manifest link(s) from the expected count")

        # `max_pages` means "deliberately cut this harvest short", and the
        # manifest path used to be the one strategy that ignored it: asking
        # for ten pages of a 229-page manifest fetched all 229, and reported
        # nothing about having done so. `promised` stays the site's own
        # count so the coverage figure is still measured against what
        # exists, while `truncated` is what makes the shortfall speak.
        promised = len(links)
        cap = opts.limit()
        over = 0 if cap is None else max(0, promised - cap)
        if over:
            links = links[:cap]
            _log(opts, f"  stopping at the {cap}-page limit: the manifest lists "
                       f"{promised}, so {over} are left unfetched")
        if stats is not None:
            stats["truncated"] = over > 0
            stats["remaining"] = over

        if shape == "hybrid":
            # A hybrid manifest promises two different things: its own root
            # prose (already in hand as `body`) and whatever pages its links
            # describe. The two are recorded separately so root success can
            # never stand in for corpus completeness.
            #
            # Whether the root survives is decided upstream, by *why* the
            # links were narrowed -- see `Pathway.drop_root`. Narrowing for
            # a release condemns the prose with the links, because the file
            # documents a different release. Narrowing for a section does
            # not: the file already showed it covers that section, and its
            # overview is the same site's own words about it. Deciding here
            # from `restrict_links is not None` conflated the two and threw
            # away 1.1 MB of real documentation on mojolang.org.
            keep_root = not drop_root
            root_doc = Doc(det.url, "llms.txt Overview", _meta_header(det.url, "llms.txt") + body)
            if not keep_root:
                _log(opts, "  dropping the root document: the manifest was narrowed, "
                           "and its own prose is not what was asked for")
            # Measured against what the site says exists, not against the
            # slice a page limit left behind — otherwise cutting a harvest
            # short would make it *look* complete.
            expected_count = promised
            root_count = 1 if keep_root else 0
            _publish_denominator(stats, fetcher, expected_count + root_count,
                                 expected_count, root_count)

            docs, failed = _acquire_manifest_links(links, fetcher, opts)
            acquired_count = len(docs)
            failed_count = len(failed)
            is_whole = acquired_count == expected_count

            if stats is not None:
                stats["expected"] = expected_count
                stats["discovered"] = expected_count + root_count
                stats["acquired"] = acquired_count
                stats["fetched"] = acquired_count + root_count
                stats["failed"] = failed_count
                stats["failed_urls"] = failed
                stats["whole"] = is_whole
                if not is_whole and not over:
                    # A truncated harvest is already explained by the page
                    # limit; calling those pages "could not be acquired"
                    # would blame the site for the caller's own bound.
                    stats["reason"] = (
                        f"{'hybrid root document stored, but ' if keep_root else ''}"
                        f"{failed_count} of {expected_count} "
                        f"manifest linked pages could not be acquired"
                    )

            return ([root_doc] + docs) if keep_root else docs

        # shape == "index"
        if links:
            # As above: the denominator is the site's own count, so a page
            # limit shows up as a shortfall rather than as completeness.
            expected_count = promised
            _publish_denominator(stats, fetcher, expected_count, expected_count, 0)

            docs, failed = _acquire_manifest_links(links, fetcher, opts)
            acquired_count = len(docs)
            failed_count = len(failed)
            is_whole = (acquired_count == expected_count and expected_count > 0)

            if stats is not None:
                stats["expected"] = expected_count
                stats["discovered"] = expected_count
                stats["acquired"] = acquired_count
                stats["fetched"] = acquired_count
                stats["failed"] = failed_count
                stats["failed_urls"] = failed
                stats["whole"] = is_whole
                if not is_whole and not over:
                    stats["reason"] = (
                        f"manifest declared {expected_count} unique pages, but {failed_count} "
                        f"could not be acquired"
                    )

            if docs:
                _log(opts, f"  harvested {len(docs)}/{expected_count} pages from llms.txt index manifest")
                return docs

            # Every linked page failed: report the failure rather than
            # falling through to the raw-dump path below and calling a
            # manifest nobody could resolve a single page from "complete".
            _log(opts, f"  0/{expected_count} pages resolved from llms.txt index manifest")
            return []

    # Shape B (dump), or an index/hybrid manifest with no actionable link at
    # all: one request already holds the whole corpus. When there was no
    # actionable link, this genuinely is the whole of what the manifest
    # promised — not a fallback pretending a failed manifest is complete.
    #
    # Split on the document's own headings before storing. `_split_dump` was
    # written for this, tested, and never called from anywhere but its own
    # tests — and its first test says why it exists: "5.7 MB stored as one
    # page is unsearchable: every query matches page 1, and ranking has
    # nothing to choose between." Storing `docs.langchain.com/llms-full.txt`
    # whole is 6.7 MB behind a single title.
    parts = _split_dump(body)
    docs = [Doc(f"{det.url}#{_anchor(title)}", title,
                _meta_header(det.url, "llms.txt") + chunk)
            for title, chunk in parts]
    if not docs:
        docs = [Doc(det.url, "llms.txt", _meta_header(det.url, "llms.txt") + body)]
    if parts:
        _log(opts, f"  split the dump into {len(docs)} pages on its own headings")

    if stats is not None:
        # One request, and it returned everything the file contains — so the
        # denominator is what we stored, and `whole` is measured rather than
        # assumed. Splitting changes how many pages that is; it cannot make
        # the corpus any less complete than the file we were given.
        stats["expected"] = len(docs)
        stats["discovered"] = len(docs)
        stats["acquired"] = len(docs)
        stats["fetched"] = len(docs)
        stats["failed"] = 0
        stats["failed_urls"] = []
        stats["whole"] = True

    return docs


def _links_under(links: list[tuple[str, str]], prefix: str) -> list[tuple[str, str]]:
    """Those links whose own path sits under `prefix`."""
    out = []
    for title, link in links:
        path = urlparse(link).path
        path = path if path.endswith("/") else path + "/"
        if path.startswith(prefix):
            out.append((title, link))
    return out


def _urls_for_release(urls: list[str], asked: str, opts: Options,
                      stats: dict | None = None) -> list[str]:
    """A sitemap's URLs narrowed to the release the caller named.

    The manifest path has honoured a named release since `_links_for_release`;
    the sitemap path never did, and `docs.djangoproject.com` files every
    release side by side. So `learn_technology("django", version="5.2")`
    harvested `/en/5.0/`, `/en/4.2/`, `/en/dev/` and the rest, stored them
    together, and labelled the mixture **5.2** — the same page under four
    different releases, under exactly the right name.

    Left alone when the site does not version its paths, and when nothing
    matches: a request for a release a site files somewhere else should get
    that site's documentation, not an empty harvest.
    """
    if not asked:
        return urls

    # Where the release sits in the path matters. Django files the 5.2 manual
    # at `/en/5.2/topics/…` and its 5.2 release notes at
    # `/en/dev/releases/5.2/` — both name the release, and only the first is
    # the 5.2 documentation. The difference is the segment it appears in: a
    # release that *scopes* a path comes early in it, and one that is merely
    # the subject of a page comes late. So match at the shallowest depth any
    # URL manages, and take only the URLs that match there.
    matched: list[tuple[int, str]] = []
    for url in urls:
        for depth, part in enumerate(p for p in urlparse(url).path.split("/") if p):
            if _VERSION.match(part) and versions.same_release(asked, part):
                matched.append((depth, url))
                break
    kept = []
    if matched:
        shallowest = min(depth for depth, _ in matched)
        kept = [url for depth, url in matched if depth == shallowest]
    if kept and stats is not None:
        # The pages themselves say which release they are. That is what makes
        # the label a finding rather than a repetition of the request.
        stats["release_confirmed"] = True
    if not kept:
        _log(opts, f"  the {len(urls)} indexed URLs name no release matching "
                   f"{asked!r}; taking them as published")
        return urls
    if len(kept) != len(urls):
        _log(opts, f"  narrowed {len(urls)} URLs to {len(kept)} under release {asked}")
    return kept


def _links_for_release(links: list[tuple[str, str]], asked: str) -> list[tuple[str, str]]:
    """Those links whose own path names the release asked for.

    For sites that file every release side by side — `/docs/1.10/…` beside
    `/docs/2.11/…` — the manifest lists them all and the path is what says
    which is which. Compared through `versions.same_release`, so asking for
    "1.10" also matches "1.10.4" and never matches "1.9".
    """
    out = []
    for title, link in links:
        for part in (p for p in urlparse(link).path.split("/") if p):
            if _VERSION.match(part) and versions.same_release(asked, part):
                out.append((title, link))
                break
    return out



def _requested_release(url: str, opts: Options) -> str:
    """The release the caller asked for, or `""` for "whatever is current".

    Read from the `version` they passed first, and from the URL they pointed
    at second — `/docs/v3/` names a release just as plainly as `version="v3"`
    does, and a caller who gave both meant the one they typed.
    """
    asked = (getattr(opts, "version", "") or "").strip()
    if asked:
        return asked
    for part in (p for p in urlparse(url).path.split("/") if p):
        if _VERSION.match(part):
            return part
    return ""


#: A full dump states where each page it contains came from:
#:
#:      # Build
#:      Source: https://docs.langchain.com/build-overview
#:
#: 1,175 of them in `docs.langchain.com/llms-full.txt`. That is the file
#: saying, per page, what it covers — which is exactly the checkable claim a
#: dump was assumed not to make.
_DUMP_SOURCE = re.compile(r"^Source:[ \t]*(\S+)[ \t]*$", re.M)


def _dump_sections(text: str) -> list[tuple[str, int, int]]:
    """`[(source_url, start, end)]` for a dump that states each page's origin."""
    marks = list(_DUMP_SOURCE.finditer(text))
    if not marks:
        return []

    starts: list[tuple[int, str]] = []
    for mark in marks:
        line_start = text.rfind("\n", 0, mark.start()) + 1
        # The heading above it introduces the page, so the section starts there.
        previous_end = line_start - 1
        previous_start = text.rfind("\n", 0, previous_end) + 1 if previous_end > 0 else 0
        previous = text[previous_start:previous_end]
        starts.append((previous_start if previous.lstrip().startswith("#")
                       else line_start, mark.group(1)))

    out = []
    for i, (start, source) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        out.append((source, start, end))
    return out


def _dump_under(text: str, prefix: str) -> str | None:
    """A dump narrowed to the pages it says came from under `prefix`.

    `None` when the file states no sources and the question cannot be
    answered; `""` when it states them and none is under the prefix, which
    means it documents something else.
    """
    sections = _dump_sections(text)
    if not sections:
        return None

    head = text[:sections[0][1]].strip()
    kept = []
    for source, start, end in sections:
        path = urlparse(source).path
        path = path if path.endswith("/") else path + "/"
        if path.startswith(prefix):
            kept.append(text[start:end].strip())
    if not kept:
        return ""
    # The root prose stays, for the reason `drop_root` exists: narrowing to a
    # section keeps the site's own overview of a site that includes it.
    return "\n\n".join(([head] if head else []) + kept)


class Pathway(NamedTuple):
    """How a published file may be used for one request.

        skip           do not use it at all; fall down the ladder to a crawl
        restrict_links use it, but only these entries (None = as published)
        drop_root      discard its own prose along with the links it lost
        restrict_body  use this text instead of the file as published, for a
                       dump narrowed to the section that was asked for

    `drop_root` is separate from `restrict_links` because narrowing happens
    for two different reasons and only one of them condemns the prose. A
    release request narrows because the file documents a *different*
    release, so its text is the wrong release's text. A section request
    narrows a file that has already shown it covers the section, and its
    text is then the same site's overview of it.
    """

    skip: bool
    restrict_links: list[tuple[str, str]] | None
    drop_root: bool
    restrict_body: str | None = None


def _scope_site_wide_llms(url: str, det: "Detection", fetcher: Fetcher,
                          opts: Options) -> Pathway:
    """Which of the two acquisition pathways this request takes.

    `llms.txt` and `llms-full.txt` are the reason to prefer publication over
    crawling: a site that publishes them has already produced its *current*
    documentation, complete and LLM-ready, and reading it costs one request
    against a crawl's hundreds. Which is exactly why the two cases divide:

    **No release named** — that published file is precisely what was asked
    for. Take it. This is the pathway worth having, and it stays cheap.

    **A release named** — the published file is the current one, and current
    is not what was asked for. It answers only if it can *show* it is that
    release: by stating so in its header, or by listing pages filed under
    it. Otherwise the version-scoped crawl is the honest answer, because
    storing one release's documentation under another's name is the failure
    the whole version contract exists to prevent.

    Sitting across both: never broaden a scoped request. `docs.modular.com`
    publishes one `llms.txt` for Modular Cloud, so a request for `/mojo/`
    came back as API-key and billing documentation — no release involved,
    just a file about a different product on the same host.
    """
    if not _at_the_site_root(det):
        return Pathway(False, None, False)   # already scoped below the root

    asked = _requested_release(url, opts)

    if det.kind != "llms_txt":
        # Another strategy's artifact is all-or-nothing, and a site-wide one
        # cannot answer for a release nothing has checked.
        return Pathway(bool(asked), None, False)

    # Nothing below can change the answer when no release was named and the
    # caller's URL already covers the whole site: `_pathway_for_latest` takes
    # the file as published, whatever its links turn out to say. Reading the
    # body to discover that costs a second fetch of a file already in hand,
    # which is precisely the redundant discovery the ladder forbids.
    if not asked and docs_scope(url) == "/":
        return Pathway(False, None, False)

    try:
        body = det.body if det.body is not None else fetcher.text(det.url,
                                                                  timeout=DUMP_TIMEOUT)
    except ForgeError:
        return Pathway(bool(asked), None, False)
    body = body.strip()
    links = (llmsfinder.parse_llms_links(body, det.url)
             if llmsfinder.classify_llms_shape(body, det.url) in ("index", "hybrid")
             else None)

    if asked:
        return _pathway_for_release(asked, body, links, det, opts)
    return _pathway_for_latest(url, links, det, opts, body)


def _pathway_for_release(asked: str, body: str, links: list[tuple[str, str]] | None,
                         det: "Detection", opts: Options) -> "Pathway":
    """A specific release was named, so the published file has to earn it."""
    name = det.url.rsplit("/", 1)[-1]

    declared = llmsfinder.declared_version(body)
    if declared and versions.same_release(asked, declared):
        _log(opts, f"  {name} states version {declared} — that is the {asked} "
                   f"documentation, taking it whole")
        return Pathway(False, None, False)

    if links:
        scoped = _links_for_release(links, asked)
        if scoped:
            _log(opts, f"  narrowing {name} to the {len(scoped)} page(s) it files "
                       f"under version {asked}")
            # The root prose is the file's own text, and the file is
            # published for the CURRENT release. Only its per-release links
            # survived the check, so keeping the prose would put the wrong
            # release's documentation in beside the right one's pages.
            return Pathway(False, scoped, True)

    _log(opts, f"  ignoring {name}: it is published for the current release and "
               f"cannot show it documents {asked} — crawling that version instead")
    return Pathway(True, None, False)


def _pathway_for_latest(url: str, links: list[tuple[str, str]] | None,
                        det: "Detection", opts: Options,
                        body: str = "") -> "Pathway":
    """No release named: the current documentation is the thing wanted, and
    the published file is it — subject only to actually covering the section
    that was asked for."""
    prefix = docs_scope(url)
    if prefix == "/":
        return Pathway(False, None, False)   # the whole site, in one request
    if links is None:
        # A dump lists no pages -- but it does state, per page, where that page
        # came from, and that is a checkable claim about what it covers.
        #
        # It used to be taken as published here, on the reasoning that
        # refusing would trade a site's whole published corpus for a crawl.
        # Measured, that reasoning inverts: asking for `langgraph`, which
        # documents itself at `docs.langchain.com/oss/python/langgraph/`,
        # returned the site-wide dump -- 3,372 pages of LangChain, LangSmith
        # and Fleet -- as LangGraph's documentation. Broadening a scoped
        # request is the one thing acquisition must never do.
        narrowed = _dump_under(body, prefix)
        if narrowed is None:
            # No per-page sources: this file really does make no checkable
            # claim, and the original reasoning stands for it unchanged —
            # refusing on a suspicion nothing supports would trade a site's
            # whole published corpus for a crawl. What changed is only that
            # the claim is now checked wherever the file makes one.
            return Pathway(False, None, False)
        if not narrowed:
            _log(opts, f"  the site-wide {det.url.rsplit('/', 1)[-1]} covers no "
                       f"page under {prefix} — it documents something else")
            return Pathway(True, None, False)
        _log(opts, f"  narrowed the site-wide dump to {prefix}: "
                   f"{len(narrowed):,} of {len(body):,} characters")
        return Pathway(False, None, False, narrowed)

    scoped = _links_under(links, prefix)
    if scoped:
        # Narrowed, but the root prose stays. Reaching here means the file
        # has already shown it covers this section -- a file covering none of
        # it is refused below and never narrowed at all. Its overview is then
        # this site's own words about a site that includes the section, and
        # dropping it cost 1.1 MB of real documentation on mojolang.org,
        # whose root is literally "Mojo programming language documentation".
        return Pathway(False, scoped, False)

    _log(opts, f"  the site-wide {det.url.rsplit('/', 1)[-1]} lists {len(links)} "
               f"page(s) and none under {prefix} — it documents something else")
    return Pathway(True, None, False)


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

# How much text a CONTENT match must hold before it is believed. Below this the
# selector is assumed to have found a stub — a heading, an empty shell — and the
# search carries on.
MIN_MAIN_CHARS = 200


def strip_chrome(soup) -> None:
    """Remove navigation, chrome and scripts, in place."""
    for sel in STRIP:
        for el in soup.select(sel):
            el.decompose()


#: Containers worth scoring when no CONTENT selector matched.
_DENSITY_TAGS = "main, article, section, div"

#: Below this a container reads as navigation or chrome rather than
#: documentation. Calibrated against the 452 pages measured in Phase B: real
#: documentation on correctly-resolved sites scored well above it, and the
#: pages that were silently storing navigation scored below.
DENSITY_FLOOR = 0.30

#: A large page has thousands of divs and the answer is always among the
#: biggest few, so scoring is bounded rather than exhaustive.
DENSITY_CANDIDATES = 60


def density(el) -> float:
    """How much this container reads like documentation rather than chrome.

    Text length, link density, code blocks and headings — the components
    Phase B measured across 452 real pages. Deliberately arithmetic a person
    can follow: the point of replacing a silent fall-through is that a rejected
    page can be explained, and a score nobody can read is a different kind of
    silence.
    """
    text = el.get_text(" ", strip=True)
    if not text:
        return 0.0
    # Deliberately no length floor. A short page is still documentation — an
    # API stub or a one-line note — and rejecting it for being brief would
    # throw away real pages to catch navigation. What actually separates the
    # two is how much of the text lives inside links, so that is what decides.
    anchors = el.select("a")
    anchor_chars = sum(len(a.get_text(" ", strip=True)) for a in anchors)

    prose = 1.0 - min(anchor_chars / len(text), 1.0)
    length = min(len(text) / 3000.0, 1.0)
    structure = min((len(el.select("h1,h2,h3,h4,h5,h6"))
                     + len(el.select("pre"))) / 10.0, 1.0)
    return 0.55 * prose + 0.30 * length + 0.15 * structure


def _density_candidates(soup) -> list:
    """The containers worth scoring, biggest first. Includes <body> on merit."""
    els = list(soup.select(_DENSITY_TAGS))
    els.sort(key=lambda e: len(e.get_text(" ", strip=True)), reverse=True)
    els = els[:DENSITY_CANDIDATES]
    if soup.body is not None:
        # Scored like any other candidate rather than accepted by default.
        # That single change is the difference between "we could not find the
        # documentation" and "here is the navigation menu, filed as prose".
        els.append(soup.body)
    return els


def _by_density(soup, plan=None) -> tuple[object | None, str]:
    """The best-scoring container, or `(None, "")` if none clears its floor.

    The floor is this template's own where the crawl has learned one, and the
    global constant otherwise — which is every page until a template has been
    seen enough times to have a distribution.
    """
    best, best_score = None, 0.0
    for el in _density_candidates(soup):
        scored = density(el)
        if scored > best_score:
            best, best_score = el, scored
    if best is None:
        return None, ""
    floor = plan.floor_for(ancestry(best)) if plan is not None else DENSITY_FLOOR
    return (best, "density") if best_score >= floor else (None, "")


def pick_main(soup, plan=None) -> tuple[object | None, str]:
    """The element holding the documentation, and how it was found.

    Split out of `_html_to_md` so instrumentation can record which selector
    actually won without reimplementing the choice. A second copy of this loop
    would drift, and a measurement that drifts from the code it measures is
    worse than no measurement.

    Returns `(None, "")` when nothing on the page reads like documentation.
    That is the case that used to fall through to `soup.body` in silence, and
    it was measured storing navigation as documentation on 2.8% of pages from
    correctly-resolved sites — and on 61% of pages from wrongly-resolved ones,
    where it is the loudest available signal that the resolution was wrong.
    """
    chosen, selector = None, ""
    for sel in CONTENT:
        found = soup.select_one(sel)
        if found and len(found.get_text(strip=True)) > MIN_MAIN_CHARS:
            chosen, selector = found, sel
            break

    # What the crawl has learned about pages built like this one. This is the
    # "re-extracts" half of Invariant 7: the same page, read differently,
    # because twelve of its siblings showed the first answer was the wrong one.
    if plan is not None and chosen is not None:
        signature = ancestry(chosen)
        if signature in plan.density_clusters:
            scored, how = _by_density(soup, plan)
            if scored is not None:
                return scored, how
        pinned = plan.pinned.get(signature)
        if pinned and pinned != selector:
            found = soup.select_one(pinned)
            if found and len(found.get_text(strip=True)) > MIN_MAIN_CHARS:
                return found, pinned

    if chosen is not None:
        return chosen, selector

    scored, how = _by_density(soup, plan)
    if scored is not None:
        return scored, how
    # Decision point 1. Nine selectors and a density score have all declined,
    # and the fallback from here is to refuse the page. Refusing is right far
    # more often than not — it is what stops navigation being stored as
    # documentation — but it is also how a site with an unusual template loses
    # every page it has. One cached call per template is a cheap way to tell
    # those apart, and the answer is validated against the page before it is
    # trusted: the model proposes, the code disposes.
    return _ask_for_selector(soup)


def _skeleton(soup) -> str:
    """A coarse fingerprint of a page's layout, for caching a selector answer.

    Deliberately not the content: two pages of one template must share a key or
    the cache buys nothing, and that is the whole economy of decision point 1.
    """
    body = soup.body if soup.body is not None else soup
    parts = []
    for child in list(getattr(body, "children", []))[:12]:
        name = getattr(child, "name", None)
        if not name:
            continue
        css = ".".join((child.get("class") or [])[:2])
        parts.append(f"{name}.{css}" if css else name)
    return "|".join(parts) or "bare"


def _ask_for_selector(soup) -> tuple[object | None, str]:
    """Consult about an unrecognised template. Returns the refusal unchanged
    when reasoning is off, which is every run that has not opted in."""
    reasoner = reasoning.current()
    if not reasoner.enabled():
        return None, ""

    html = str(soup)[:reasoning.MAX_SAMPLE]

    def usable(answer: str) -> bool:
        try:
            found = soup.select_one(answer)
        except Exception:                               # noqa: BLE001 - bad CSS
            return False
        return bool(found) and len(found.get_text(strip=True)) > MIN_MAIN_CHARS

    answer = reasoner.ask(
        "unrecognised template", _skeleton(soup),
        "Here is the start of a documentation page whose main content none of "
        "the usual selectors found. Reply with ONE CSS selector matching the "
        "element that holds the documentation prose - not the navigation, "
        "sidebar or footer. Reply with the selector only.\n\n" + html,
        fallback="", check=usable)

    if not answer:
        return None, ""
    return soup.select_one(answer), f"reasoned:{answer}"


#: Words that appear on soft-404s and almost never in a whole documentation
#: page. Only a gate on whether the question is worth asking — being wrong here
#: costs one call, and being wrong in the permissive direction costs nothing.
_ERROR_WORDS = ("page not found", "404", "not found", "does not exist",
                "no longer available", "something went wrong", "error occurred",
                "access denied", "forbidden")

#: Above this a page has too much real content to be an error, whatever words
#: it contains. A genuine "Error handling" chapter is long.
_ERROR_CHARS = 1200


def _error_shaped(title: str, doc: str) -> bool:
    """Cheap gate: short, and reads like an error. Asks nothing."""
    if len(doc) > _ERROR_CHARS:
        return False
    haystack = f"{title}\n{doc}".lower()
    return any(word in haystack for word in _ERROR_WORDS)


def _is_error_page(title: str, doc: str, url: str) -> bool:
    """Decision point 4, consulted only for pages that already look wrong.

    With reasoning off this is always False, which is exactly today's
    behaviour: a soft-404 is stored as documentation and nothing notices.
    """
    reasoner = reasoning.current()
    if not reasoner.enabled():
        return False
    answer = reasoner.ask(
        "soft error page", (urlparse(url).hostname or "") + ":" + title[:40],
        "A documentation crawler fetched this page and the server answered 200. "
        "Is this real documentation, or an error/not-found page? Reply with "
        "exactly one word: DOCUMENTATION or ERROR.\n\n"
        f"Title: {title}\n\n{doc[:reasoning.MAX_SAMPLE]}",
        fallback="DOCUMENTATION",
        check=lambda a: a.strip().upper().startswith(("DOCUMENTATION", "ERROR")))
    return answer.strip().upper().startswith("ERROR")


def _soup(html: str, parser: str = "html.parser"):
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise ForgeError("HTML extraction needs: pip install beautifulsoup4") from e
    return BeautifulSoup(html, parser)


def _measure(el, selector: str, url: str, title: str) -> dict:
    """What extraction just did, from the parse it already has.

    The crawler needs an `Observation` per page to adapt on, and
    `instrument.observe()` would re-parse the document to produce one. Paying
    for a second parse of every page in a 700-page harvest to learn what the
    first parse already knew would be a strange way to make a crawl faster,
    so extraction reports instead.
    """
    text = el.get_text(" ", strip=True) if el is not None else ""
    anchors = el.select("a") if el is not None else []
    anchor_chars = sum(len(a.get_text(" ", strip=True)) for a in anchors)
    headings = len(el.select("h1,h2,h3,h4,h5,h6")) if el is not None else 0
    code = len(el.select("pre")) if el is not None else 0
    return {
        "url": url, "title": title, "selector": selector,
        "signature": ancestry(el) if el is not None else "",
        "shape": f"h{bucket(headings)}|c{bucket(code)}|a{bucket(len(anchors))}",
        "chars": len(text), "links": len(anchors),
        "link_text_ratio": round(anchor_chars / len(text), 3) if text else 0.0,
        "code_blocks": code, "headings": headings,
        "extractable": el is not None,
        "density_score": round(density(el), 4) if el is not None else 0.0,
    }


def _html_to_md(html: str, url: str, soup=None, plan=None,
                report: dict | None = None) -> tuple[str, str]:
    try:
        from markdownify import markdownify as md
    except ImportError as e:
        raise ForgeError("HTML extraction needs: pip install markdownify") from e

    soup = soup if soup is not None else _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    title = title or "Untitled"

    strip_chrome(soup)
    main, selector = pick_main(soup, plan=plan)
    if report is not None:
        # Measured even when the page is about to be refused: a rise in
        # refusals has to be distinguishable from a rise in bad pages.
        report.update(_measure(main if main is not None else (soup.body or soup),
                               selector, url, title))
    if main is None:
        # Reported, not stored. A page filed as documentation when it is a
        # navigation menu counts toward `expected` and toward `complete`, so a
        # silent fall-through does not merely waste a slot — it inflates a
        # coverage figure the whole product is built on being able to trust.
        raise ForgeError(
            f"nothing on {url} reads like documentation: no recognised content "
            f"container and nothing dense enough to be prose")

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


def _at_the_site_root(det: Detection) -> bool:
    """True when the detected file sits at the origin root.

    A file there describes the whole site, so it cannot be assumed to answer
    a request for one release or one section of it. What matters is where the
    file sits, not how we came by its URL: this used to also require that we
    had *probed* for it, on the reasoning that a URL the caller typed is a URL
    the caller meant. But the caller does not type it any more — resolution
    hands `learn_technology` whatever it found, and it now finds
    `mojolang.org/llms.txt` directly. Asking for Mojo 0.9 then went straight
    past the release check and took the current manifest whole.

    A file below the root is still left alone: `/en/4.2/llms.txt` is already
    scoped to what was asked for, and putting it through the release check
    would narrow, or refuse, a file that is the right one.
    """
    return urlparse(det.url).path.count("/") == 1


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


def _median(sorted_values: list[float]) -> float:
    """The middle of an already-sorted list, or 0.0 if it is empty."""
    if not sorted_values:
        return 0.0
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def _neighbourhood(url: str) -> str:
    """The first two path segments — the unit a yield map is kept per.

    Finer than a host and coarser than a page, which is the granularity at
    which documentation sites actually differ: `/reference/` is dense and
    `/blog/` is not, and neither fact is about any individual page.
    """
    parts = [p for p in (urlparse(url).path or "/").split("/") if p]
    # Drop the page itself. Keeping it would make every shallow URL its own
    # neighbourhood, and a yield map with one page per bucket is just the
    # page's own score wearing a hat.
    return "/" + "/".join(parts[:-1][:2])


class Plan:
    """The crawl's working hypothesis about this site, revised as it goes.

    The plan used to be fixed from the entry page, which is the least
    representative page on any documentation site: it is the one page built as
    a landing page. Everything after it was crawled on a guess made from it.

    So the plan is a *hypothesis*. Every page reports measurements, and every
    twelfth page the plan is re-derived from the last twelve — recent rather
    than cumulative, because a site that changes template halfway should be
    noticed halfway rather than averaged into silence.

    Adaptation reorders and re-extracts. It never filters (Invariant 7), and
    every revision is recorded and surfaced beside the coverage note rather
    than logged where nobody reads it (Invariant 11).
    """

    WINDOW = 12                 # how many recent pages a revision reads
    REVISE_EVERY = 12           # how often to re-derive
    PIN_AFTER = 3               # wins before a selector is pinned to a template
    SHELL_FRACTION = 0.4        # recent shells before switching to rendered
    LOW_SCORE = 0.35            # below this, a cluster is not being read well

    #: How far below a template's own median a page may score before it stops
    #: reading as documentation. 1.5 MADs is the conventional outlier distance
    #: and is provisional like every other number here (`ISSUES.md` F2).
    FLOOR_MADS = 1.5
    #: Enough pages of one template to have a distribution worth fitting.
    FLOOR_SAMPLES = 5
    #: A fitted floor may move either way, but not to absurdity.
    FLOOR_RANGE = (0.05, 0.60)
    #: The fraction of a template's median score a floor may never exceed.
    #:
    #: Without this, `median - k*MAD` collapses onto the median whenever a
    #: template scores consistently — which is the normal case, not the odd
    #: one — and the floor ends up refusing roughly the bottom half of a site's
    #: own documentation. Measured on docs.astro.build: a tight distribution
    #: around 0.75 fitted a floor of 0.60. A page has to be *unusually* poor
    #: for its own site, not merely below average.
    FLOOR_MARGIN = 0.5

    def __init__(self, generator: str = "") -> None:
        #: A hypothesis, held loosely. Seeded from the generator fingerprint and
        #: withdrawn the moment recent pages stop fitting it.
        self.generator = generator
        self.pinned: dict[str, str] = {}        # template signature -> selector
        self.floors: dict[str, float] = {}      # template signature -> density floor
        self.density_clusters: set[str] = set()
        self.render = False
        self.yield_map: dict[str, float] = {}
        self.revisions: list[str] = []
        self._wins: dict[tuple[str, str], int] = {}

    def floor_for(self, signature: str) -> float:
        """The density a page of this template has to clear to be documentation.

        The global constant until the site has shown enough of this template to
        say otherwise. A fixed floor assumes every site's pages sit on the same
        scale and they do not: an API reference where every page is mostly
        signatures and anchor links scores low throughout, so one global floor
        refuses the entire corpus; a wordy tutorial site scores high throughout,
        so the same floor never catches its navigation. Both are real, and a
        constant cannot be right for both.
        """
        return self.floors.get(signature, DENSITY_FLOOR)

    def revise(self, ledger: Ledger) -> list[str]:
        """Re-derive from the rolling window. Returns what changed, if anything."""
        recent = ledger.recent(self.WINDOW)
        if len(recent) < 4:
            return []
        changed: list[str] = []

        # R1 — the platform hypothesis stopped fitting. Withdraw it rather than
        # keep extracting against a template the site is no longer serving.
        if self.generator and all(o.fell_through for o in recent):
            changed.append(f"withdrew the {self.generator!r} hypothesis: none of "
                           f"the last {len(recent)} pages matched its selectors")
            self.generator = ""

        # R2 — one selector has won repeatedly on one template. Pin it, so the
        # CONTENT list stops being consulted for pages built that way.
        for obs in recent:
            if not obs.selector or obs.selector == "density" or not obs.signature:
                continue
            key = (obs.signature, obs.selector)
            self._wins[key] = self._wins.get(key, 0) + 1
            if (self._wins[key] >= self.PIN_AFTER
                    and self.pinned.get(obs.signature) != obs.selector):
                self.pinned[obs.signature] = obs.selector
                changed.append(f"pinned {obs.selector!r} for template "
                               f"{obs.signature or '(none)'}")

        # R3 — a template the selector list does not recognise, scoring badly.
        # Route it to density rather than keep failing the same way.
        clusters: dict[str, list] = {}
        for obs in recent:
            clusters.setdefault(obs.signature, []).append(obs)
        for signature, group in clusters.items():
            if len(group) < 3 or not all(o.fell_through for o in group):
                continue
            mean = sum(o.score() for o in group) / len(group)
            if mean < self.LOW_SCORE and signature not in self.density_clusters:
                self.density_clusters.add(signature)
                changed.append(f"routed template {signature or '(none)'} to density "
                               f"scoring (mean score {mean:.2f})")

        # R4 — the site is rendering client-side. One switch, for the rest of
        # the crawl; retrying every page individually would cost far more.
        shells = sum(1 for o in recent if o.shell)
        if not self.render and shells / len(recent) >= self.SHELL_FRACTION:
            self.render = True
            changed.append(f"switched to rendered fetching: {shells} of "
                           f"{len(recent)} recent pages are JS shells")

        # R6 — fit each template's density floor to its own distribution.
        # Documentation and navigation separate into two humps on essentially
        # every site; the floor wants to sit in the valley between them,
        # wherever that happens to fall on this site's scale. Fitted over the
        # whole ledger rather than the window, because a distribution wants
        # every sample it can get.
        for signature, group in ledger.by_signature().items():
            if len(group) < self.FLOOR_SAMPLES:
                continue
            scores = sorted(o.score() for o in group)
            middle = _median(scores)
            spread = _median(sorted(abs(s - middle) for s in scores))
            low, high = self.FLOOR_RANGE
            # The lower of the two candidates, deliberately: losing a real page
            # is a worse failure than storing a thin one, and only one of those
            # is recoverable by reading the result.
            fitted = min(middle - self.FLOOR_MADS * spread,
                         middle * self.FLOOR_MARGIN)
            fitted = max(low, min(high, fitted))
            if abs(self.floor_for(signature) - fitted) >= 0.02:
                was = self.floor_for(signature)
                self.floors[signature] = round(fitted, 3)
                changed.append(
                    f"fitted the density floor for template "
                    f"{signature or '(none)'} to {fitted:.2f} (was {was:.2f}) "
                    f"from {len(group)} pages")

        # R5 — refresh the yield map. Not a rule on its own; it is what
        # reprioritising the frontier reads.
        self.yield_map = self.refresh_yield(ledger)

        self.revisions.extend(changed)
        return changed

    @staticmethod
    def refresh_yield(ledger: Ledger) -> dict[str, float]:
        """Mean readability score per path neighbourhood, over the whole crawl."""
        buckets: dict[str, list[float]] = {}
        for obs in ledger.observations:
            buckets.setdefault(_neighbourhood(obs.url), []).append(obs.score())
        return {where: sum(scores) / len(scores)
                for where, scores in buckets.items() if scores}


class _Frontier:
    """The crawl's queue, ordered by how likely a URL is to be documentation.

    A `deque` made truncation arbitrary: `max_pages` kept whatever the
    navigation happened to list first, which on most sites means index pages
    and whatever the footer links to. Ordering decides which pages you get when
    a harvest is cut short; it never decides which pages are *eligible*.

    Nothing is dropped here. `_crawlable` decides membership and this decides
    only sequence, so an exhaustive crawl returns exactly the page set it
    always did — just in a better order (Invariant 7).
    """

    def __init__(self, start: str) -> None:
        self._heap: list[tuple[tuple, str]] = []
        self._seen: set[str] = set()
        self._yield: dict[str, float] = {}
        self.append(start)

    def _rank(self, url: str) -> tuple:
        path = urlparse(url).path or "/"
        # Demoted, never removed: a changelog is documentation-adjacent, and a
        # truncated harvest should spend its budget on the manual first.
        chaff = 1 if _NOT_DOCS.search(path) else 0
        docsy = 0 if _DOCSY.search(path) else 1
        # What the crawl has learned about this neighbourhood, bounded to
        # fifteen steps. Bounded on purpose: a yield map should reorder within
        # a class, never promote a changelog above the manual because a couple
        # of release notes happened to read well.
        mean = self._yield.get(_neighbourhood(url))
        bonus = 0 if mean is None else max(0, min(15, int(round(mean * 15))))
        return (chaff, docsy, -bonus, path.count("/"), url)

    def reprioritise(self, yield_map: dict[str, float]) -> None:
        """Rescore every queued URL in place.

        Drops nothing. The frontier decides sequence; `_crawlable` decides
        membership, and keeping those two apart is Invariant 7.
        """
        self._yield = dict(yield_map or {})
        queued = [url for _rank, url in self._heap]
        self._heap = []
        for url in queued:
            heapq.heappush(self._heap, (self._rank(url), url))

    def append(self, url: str) -> None:
        if url in self._seen:
            return
        self._seen.add(url)
        heapq.heappush(self._heap, (self._rank(url), url))

    def popleft(self) -> str:
        return heapq.heappop(self._heap)[1]

    def __contains__(self, url: str) -> bool:
        return url in self._seen

    def __len__(self) -> int:
        return len(self._heap)


def _drain(docs: list[Doc], sink) -> list[Doc]:
    """Hand a finished list of documents to a sink, keeping only their shape.

    The strategies that return an artifact whole — `llms.txt`, OpenAPI, a
    GitHub repo — have already built their list, so there is nothing to stream.
    Passing them through the sink anyway keeps one storage path, and dropping
    the bodies afterwards keeps peak memory proportional to the number of
    pages rather than their total size.
    """
    if sink is None:
        return docs
    kept = []
    for doc in docs:
        if sink.add(doc.title, doc.url, doc.markdown):
            kept.append(Doc(doc.url, doc.title, ""))
    return kept


def _crawl_html(start: str, fetcher: Fetcher, opts: Options,
                stats: dict | None = None, sink=None) -> list[Doc]:
    seen: set[str] = set()
    out: list[Doc] = []
    #: Reached, fetched, and nothing on them read like documentation. Kept
    #: apart from pages that simply failed to fetch, because "we could not read
    #: this" and "this was not there" are different claims about coverage.
    unreadable: list[str] = []
    #: Out-of-scope link evidence, summed across the whole crawl. Costs no
    #: requests: it reads soup that link discovery has already parsed.
    sites = Federation()
    #: What this crawl has seen, and the hypothesis it is currently working to.
    #: The plan starts empty and is re-derived every twelfth page from the last
    #: twelve — the entry page no longer decides how the rest is read.
    ledger = Ledger()
    plan = Plan()
    last_revised = 0
    queue = _Frontier(_normalize(start))
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

    # Rendering stays sequential whatever the caller asked for: Playwright's
    # sync API is bound to the thread that created the browser and this Fetcher
    # keeps exactly one. Plain HTTP is what gets overlapped, which is the case
    # that matters — a rendered crawl is slow for reasons a thread pool cannot
    # fix.
    workers = 1 if (opts.js or opts.workers < 2) else int(opts.workers)
    pace = _Pace(opts.delay)
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    window: deque = deque()

    def _fetch(link: str, render: bool) -> str:
        pace.wait(link)
        with pace.host(link):
            return fetcher.render(link) if render else fetcher.html(link)

    def _fill() -> None:
        """Top the window up, in queue order.

        Dispatch order is queue order and results are consumed in dispatch
        order, so a crawl returns its pages in the sequence it would have
        sequentially. One real difference, worth stating rather than leaving to
        be discovered: a plan revision rescores what is still *queued*, and the
        window has already been taken off the queue, so a revision reaches the
        crawl up to `workers` pages later than it otherwise would.
        """
        while (pool is not None and len(window) < workers and queue
               and (limit is None or len(out) + len(window) < limit)):
            nxt = queue.popleft()
            if nxt in seen:
                continue
            seen.add(nxt)
            window.append((nxt, pool.submit(_fetch, nxt, plan.render)))

    try:
        while True:
            # Every twelfth page, re-derive the plan from the last twelve and
            # rescore what is still queued. Recent rather than cumulative: a
            # site that changes template halfway should be noticed halfway.
            if len(ledger) >= Plan.REVISE_EVERY and len(ledger) != last_revised \
                    and len(ledger) % Plan.REVISE_EVERY == 0:
                last_revised = len(ledger)
                for line in plan.revise(ledger):
                    _log(opts, f"  plan revised: {line}")
                queue.reprioritise(plan.yield_map)

            if limit is not None and len(out) >= limit:
                break
            _fill()
            if window:
                url, pending = window.popleft()
            elif queue:
                url, pending = queue.popleft(), None     # workers == 1
                if url in seen:
                    continue
                seen.add(url)
            else:
                break

            report: dict = {}
            html = ""
            try:
                if pending is not None:
                    html = pending.result()
                else:
                    pace.wait(url)
                    html = (fetcher.render(url) if plan.render
                            else fetcher.html(url))

                # Parse once: link discovery needs the nav _html_to_md strips out.
                soup = _soup(html)
                # Same soup, same reason. The sidebar is where a project states its
                # own structure, so out-of-scope evidence is collected here, before
                # extraction strips the chrome away.
                sites.record_page(url, soup)
                for a in soup.find_all("a", href=True):
                    link = _normalize(urljoin(url, a["href"]))
                    if link not in seen and link not in queue and _crawlable(link, host, prefix):
                        queue.append(link)

                try:
                    title, doc = _html_to_md(html, url, soup=soup, plan=plan,
                                             report=report)
                except ForgeError:
                    if opts.js or plan.render or not _looks_like_shell(html):
                        raise
                    _log(opts, f"  {url} is a JS shell; retrying rendered")
                    title, doc = _html_to_md(fetcher.render(url), url, plan=plan,
                                             report=report)
            except ForgeError as e:
                if report:
                    # A refused page is still evidence — it is what tells the plan
                    # that a template is not being read well.
                    report["shell"] = _looks_like_shell(html)
                    ledger.record(Observation(**report))
                unreadable.append(url)
                _log(opts, f"  unextractable {url}: {e}")
                continue
            except Exception as e:  # one broken page must not end the crawl
                _log(opts, f"  skip {url}: {type(e).__name__}: {e}")
                continue

            report["shell"] = _looks_like_shell(html)
            ledger.record(Observation(**report))

            # Decision point 4. A page that answers 200 while rendering "Page
            # not found" is invisible to every status check, and it is stored
            # as documentation. Only pages that are both short and error-shaped
            # are worth asking about, so this costs at most one call per
            # template however many such pages a site has.
            if _error_shaped(title, doc) and _is_error_page(title, doc, url):
                unreadable.append(url)
                _log(opts, f"  {url} answers 200 but renders an error")
                continue

            if sink is not None:
                # Durable before the next page is *stored* (Invariant 16, as
                # amended for Phase 3: up to `workers` pages are now in flight
                # ahead of this one, so an interruption can lose that much
                # fetching — never anything already stored). The body is
                # released rather than carried to the end of the harvest, and a
                # page the store refuses costs that page and nothing else.
                if not sink.add(title, url, doc):
                    continue
                out.append(Doc(url, title, ""))
            else:
                out.append(Doc(url, title, doc))
            _log(opts, f"  [{len(out)}] {url}")

    finally:
        if pool is not None:
            # Nothing in flight is worth waiting for once the crawl has
            # stopped: the pages it would return are past the limit or past a
            # failure either way.
            pool.shutdown(wait=False, cancel_futures=True)


    if stats is not None:
        # Anything still queued means max_pages cut the harvest short. Silent
        # truncation is worse than a slow crawl: you get a third of a manual
        # and no way to know it.
        stats["fetched"] = len(out)
        stats["host_peak"] = dict(pace.peak)
        # The window counts. Pages prefetched but never processed were taken
        # off the queue, so counting only the queue would understate a
        # truncated harvest by up to `workers` pages — and report `whole` for a
        # crawl that stopped with pages in hand. Undercounting a shortfall is
        # the one direction this must never round.
        left = len(queue) + len(window)
        stats["remaining"] = left
        stats["truncated"] = bool(left)
        # A crawl that drained its frontier reached everything linked inside
        # its scope. That is a real claim, and a weaker one than a sitemap:
        # pages nothing links to are invisible to it either way.
        #
        # Weaker than `complete`, then, and it used to be recorded as the same
        # thing. `tensorflow` resolved to a rustdoc index page that links
        # almost nowhere, the frontier drained after one page, and the store
        # recorded a one-page corpus as complete — the strongest claim this
        # product can make, from the weakest evidence it has. A drained
        # frontier is `unknown` with the reason attached; a frontier still
        # holding pages is plainly incomplete.
        stats["frontier_drained"] = not left
        stats["whole"] = False if left else None
        if not left:
            stats.setdefault("reason", (
                f"a crawl reached every page linked under {prefix} and stopped. "
                f"Nothing stated how many pages exist, so a page nothing links "
                f"to would not have been seen"))
        stats.setdefault("discovered", len(out) + len(queue))
        if unreadable:
            stats["unextractable"] = unreadable
        # Invariant 11: every revision surfaced next to the coverage note
        # rather than in a log nobody reads. A crawler that changes its plan
        # can change what it was measuring, and the defence is disclosure.
        if plan.revisions:
            stats["revisions"] = list(plan.revisions)
        if len(ledger):
            stats["templates"] = len(ledger.by_signature())
        # Documentation this crawl kept pointing at but could never reach,
        # because it lives outside the one prefix a crawl is scoped to. Naming
        # it is the whole point: a harvest that agrees with its own sitemap and
        # reports `complete` while a second corpus sits unmentioned is the most
        # serious defect the project has.
        proposed = sites.proposals(start)
        if proposed:
            stats["corpora"] = [{"url": c.url, "host": c.host,
                                 "votes": round(c.votes, 1)} for c in proposed]

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
    r"/(blog|weblog|news|posts?|articles?|journal|updates|announcements?|"
    r"changelog|releases?|careers?|jobs|pricing|"
    r"about|contact|team|events?|showcase|agencies|partners|sponsors|store|"
    r"shop|legal|privacy|terms|press|community)(/|$)", re.I)

#: A dated path is an article. Every site that has ever published one dates it,
#: whatever it calls the section — and naming the sections is what failed:
#: `/blog` was on the list above and Django spells it `/weblog/`, so a request
#: for Django 5.2 stored 214 weblog posts as documentation. Adding `weblog` to
#: the list fixes Django and loses to the next site that says `/journal/`;
#: this is the test that does not depend on guessing the noun.
_DATED_PATH = re.compile(r"/(?:19|20)\d\d(?:/\d{1,2}){0,2}(?:/|$)")


#: Hosts that exist to serve documentation and nothing else.
_DOCS_HOSTS = ("docs.", "developer.", "devdocs.", "doc.")


def is_documentation_host(url: str) -> bool:
    """Is every path on this host documentation?"""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host.startswith(_DOCS_HOSTS) or ".readthedocs." in host


def looks_like_article(url: str) -> bool:
    """Is this URL a post rather than documentation?

    One vocabulary, shared: the sitemap filter and the resolver's `llms.txt`
    probe were each about to grow their own idea of what a blog looks like,
    and two lists that mean the same thing drift apart. `pytorch.org/llms.txt`
    is a published manifest of conference announcements, and nothing about it
    is different in kind from Django's weblog.
    """
    path = urlparse(url).path if "://" in url else url
    return bool(_NOT_DOCS.search(path) or _DATED_PATH.search(path))

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

    And only on a host that has marketing to drop. On a dedicated
    documentation host every path is documentation and this filter is pure
    loss: `docs.djangoproject.com` publishes 11,209 English URLs, and it cut
    them to **66** — `/topics/`, `/howto/`, `/ref/` and `/intro/` are not on
    the docs-shaped list, and `/releases/`, 4,898 pages of release notes, is
    on the not-docs one. A request for Django 5.2 came back with three pages.
    The filter was written for `astro.build`, a marketing site with a blog,
    and that is still exactly where it belongs.
    """
    if prefix not in ("", "/"):
        return urls
    if urls and is_documentation_host(urls[0]):
        return urls
    docsy = [u for u in urls if _DOCSY.search(urlparse(u).path)]
    if len(docsy) >= 5:
        return docsy
    trimmed = [u for u in urls if not looks_like_article(u)]
    return trimmed or urls


#: A sitemap index names its children by language: `sitemap-el.xml`,
#: `sitemap-en.xml`, `sitemap-pt-br.xml`. Two letters, so `sitemap-posts.xml`
#: and `sitemap-1.xml` are not languages and keep their whole listing.
_LOCALE_FILE = re.compile(r"[-_]([a-z]{2}(?:-[a-z]{2,4})?)\.xml$", re.I)


def _locale_of(url: str) -> str:
    """The language this URL is in, by path segment or by sitemap filename."""
    path = urlparse(url).path
    match = _LOCALE_SEGMENT.match(path)
    if match:
        code = match.group(1).lower()
    else:
        match = _LOCALE_FILE.search(path)
        code = match.group(1).lower().split("-")[0] if match else ""
    return code if code in _LOCALES or code == "en" else ""


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
        groups.setdefault(_locale_of(url), []).append(url)
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
        # One language, at the index too. `docs.djangoproject.com/sitemap.xml`
        # is twelve per-language children and `sitemap-el.xml` sorts first, so
        # a capped harvest filled up on 1,077 Greek URLs and never opened
        # `sitemap-en.xml`. The per-URL filter downstream then saw a single
        # language and had nothing to choose between — which is why this has to
        # happen here, where the choice still exists.
        locs = _prefer_default_locale(locs)
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
            title, doc = _extract_page(link, fetcher, opts)
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


#: Strategies whose source states its own size, so `acquired == expected` is a
#: measurement rather than a tautology. A repository is enumerated through the
#: API; a spec or a single published document is one artifact that either
#: arrived whole or did not.
_KNOWS_ITS_SIZE = ("github", "openapi", "raw_text")


def _note_coverage(stats: dict | None, det: "Detection", docs: list,
                   found: "DocMap | None" = None) -> None:
    """Record whether this harvest actually got the whole documentation."""
    if stats is None:
        return
    if "whole" not in stats:
        if det.kind == "llms_txt":
            body = "\n".join(d.markdown for d in docs)[:200_000]
            is_unfetched_index = (
                len(docs) == 1
                and det.url.lower().endswith("llms.txt")
                and _NAMES_A_FULLER_FILE.search(body)
                and llmsfinder.classify_llms_shape(body, det.url) == "index"
            )
            stats["whole"] = not is_unfetched_index
            if not stats["whole"] and "reason" not in stats:
                missing = found.dump_bytes if found else 0
                stats["reason"] = (
                    "stored an llms.txt index that names a fuller file which could "
                    "not be fetched"
                    + (f" ({missing:,} characters of it)" if missing else ""))
        else:
            # Only where something independent stated how much there was.
            #
            # `True` was the blanket default, and it made completeness
            # definitionally true for any strategy that set nothing: a harvest
            # of one page recorded `expected 1, acquired 1, complete True`.
            # Measured 2026-09-10 — `langgraph` stored a 6,350-character README
            # and `tensorflow` a single rustdoc index page, and both are
            # presented by the store with more confidence than the 627-page
            # langchain corpus beside them.
            #
            # A GitHub repository is enumerated through the API, and a spec or
            # a single document is one artifact that either arrived or did not.
            # Those really do know their own size. Nothing else here does, and
            # `unknown` is the state the three-state design has always had for
            # exactly this — it was simply unreachable.
            stats["whole"] = True if det.kind in _KNOWS_ITS_SIZE else None

    if found is not None:
        stats["map"] = found.as_dict()
        stats.setdefault("discovered", found.expected or stats.get("expected") or len(docs))
        stats.setdefault("expected", found.expected or stats.get("expected") or len(docs))
    else:
        stats.setdefault("discovered", stats.get("expected") or len(docs))
        stats.setdefault("expected", len(docs))

    stats.setdefault("acquired", len(docs))
    stats.setdefault("fetched", len(docs))


def harvest(url: str, opts: Options | None = None, fetcher: Fetcher | None = None,
            stats: dict | None = None, sink=None) -> tuple[list[Doc], str]:
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
            # version. If the manifest itself can prove which of its entries
            # belong to that version, use just those; otherwise crawl the
            # version they named instead.
            pathway = _scope_site_wide_llms(url, det, fetcher, opts)
            restrict_links = pathway.restrict_links

            if pathway.skip:
                _log(opts, "  ignoring the site-wide llms.txt: it does not cover "
                           "the section this URL asks for")
            else:
                if restrict_links is not None:
                    _log(opts, f"  scoping the site-wide llms.txt to {docs_scope(url)} "
                               f"({len(restrict_links)} manifest page(s))")
                if pathway.restrict_body is not None:
                    # A dump narrowed to the section that was asked for. The
                    # handler reads `det.body` when it is set, so hand it the
                    # narrowed text rather than teaching it a second way in.
                    det = Detection(det.kind, det.url, pathway.restrict_body)
                _log(opts, f"  harvesting via {det.kind}")
                try:
                    if det.kind == "llms_txt":
                        docs = handle_llms_txt(det, fetcher, opts, stats=stats,
                                               restrict_links=restrict_links,
                                               drop_root=pathway.drop_root)
                    else:
                        docs = HANDLERS[det.kind](det, fetcher, opts)
                except ForgeError as e:
                    # The rung itself failed — an unreachable root document,
                    # most often. That is a failure of this strategy, not of
                    # the harvest: fall through to sitemap/crawl rather than
                    # reporting nothing, or crashing outright.
                    _log(opts, f"  {det.kind} acquisition failed ({e}); "
                               f"falling back to the next strategy")
                    docs = []

                if docs:
                    _note_coverage(stats, det, docs, found=None)

                    strategy_used = det.kind
                    if det.kind == "llms_txt":
                        if det.url.lower().endswith(("llms-full.txt", "llms-medium.txt")):
                            strategy_used = "llms-full.txt"
                        elif docs:
                            sample = docs[0].markdown if len(docs) == 1 else ""
                            shape = llmsfinder.classify_llms_shape(sample)
                            if len(docs) > 1 or shape == "index":
                                has_md = any(llmsfinder.is_markdown_link(d.url) for d in docs)
                                strategy_used = "llms.txt (md manifest)" if has_md else "llms.txt (html manifest)"
                            else:
                                strategy_used = "llms_txt"

                    return _drain(docs, sink), strategy_used

        prefix = docs_scope(url) if opts.scope in ("", "section", None) else (
            "/" if opts.scope == "host" else opts.scope)
        host = (urlparse(url).hostname or "").lower()

        # The site's own index first, sitemap second.
        links, index_kind = site_manifest(url, fetcher, opts)
        if len(links) < 3:
            index_kind = "sitemap"
            sitemap = find_sitemap(url, fetcher, opts)
            links = []
            if sitemap:
                try:
                    links = _sitemap_links(fetcher.text(sitemap), fetcher, opts)
                except ForgeError:
                    links = []
        if links:
            scoped = [l for l in dict.fromkeys(_normalize(l) for l in links)
                      if _crawlable(l, host, prefix)]
            before = len(scoped)
            scoped = _prefer_default_locale(_focus_on_docs(scoped, prefix))
            if len(scoped) != before:
                _log(opts, f"  narrowed {before} {index_kind} URLs to {len(scoped)} "
                           f"(documentation, default language)")
            scoped = _urls_for_release(scoped, _requested_release(url, opts),
                                       opts, stats)
            # One or two hits usually means the index does not really cover
            # the docs; a crawl will do better than a near-empty list.
            if len(scoped) >= 3:
                _log(opts, f"  harvesting {len(scoped)} pages from the {index_kind}")
                cap = opts.limit()
                if stats is not None:
                    over = 0 if cap is None else max(0, len(scoped) - cap)
                    stats["discovered"] = len(scoped)
                    stats["truncated"] = over > 0
                    stats["remaining"] = over
                    stats["index"] = index_kind
                out: list[Doc] = []
                unreadable: list[str] = []
                for link in (scoped if cap is None else scoped[:cap]):
                    try:
                        title, body = _extract_page(link, fetcher, opts)
                    except ForgeError as e:
                        # Reached, but nothing on it reads like documentation.
                        # Disclosed rather than quietly missing: a page that
                        # could not be extracted still counts against coverage,
                        # and pretending it was never there is the exact
                        # dishonesty the coverage figure exists to prevent.
                        unreadable.append(link)
                        _log(opts, f"  unextractable {link}: {e}")
                        continue
                    except Exception as e:
                        _log(opts, f"  skip {link}: {e}")
                        continue
                    if sink is not None:
                        if not sink.add(title, link, body):
                            continue
                        out.append(Doc(link, title, ""))
                    else:
                        out.append(Doc(link, title, body))
                    _log(opts, f"  [{len(out)}] {link}")
                    time.sleep(opts.delay)
                if stats is not None and unreadable:
                    stats["unextractable"] = unreadable
                if out:
                    # The index is the site's own list of what exists, so this
                    # is the one strategy that can measure completeness against
                    # something other than its own effort.
                    if stats is not None:
                        stats["whole"] = len(out) >= len(scoped)
                        if not stats["whole"]:
                            stats["reason"] = (
                                f"stored {len(out)} of the {len(scoped)} pages "
                                f"the {index_kind} lists"
                                + (f", {len(unreadable)} of them unextractable"
                                   if unreadable else ""))
                    return out, index_kind

        _log(opts, "  harvesting by crawl")
        crawl_opts = replace(opts, crawl=True)
        return _crawl_html(url, fetcher, crawl_opts, stats, sink=sink), "crawl"
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
def _forget(targets: list[str], assume_yes: bool = False) -> int:
    """Remove harvests from the knowledge base.

    A harvest can be wrong — the wrong project, a partial copy, a table of
    contents stored as though it were the documentation — and being unable to
    take one back out means the store only ever accumulates mistakes. The
    backends could always delete; nothing could ask them to.

    Each target is `name` (every version) or `name@version` (just that one).
    """
    # Imported here rather than at module scope: the extraction engine does not
    # otherwise know the store exists, and this is the one place it needs to.
    from docsforge.store.kb_store import StoreError, build_store

    store = build_store()
    plan: list[tuple[str, str | None, int, int]] = []
    for target in targets:
        name, _, version = target.partition("@")
        name, version = name.strip(), version.strip() or None
        try:
            rows = [v for v in store.versions(name)
                    if version is None or v["version"] == version]
        except StoreError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not rows:
            print(f"error: {name} has no version {version!r}", file=sys.stderr)
            return 1
        for row in rows:
            plan.append((name, row["version"], row["pages"], row["characters"]))

    print(f"About to remove {len(plan)} harvest(s) from {store.kind} "
          f"({store.location}):\n")
    for name, version, pages, chars in plan:
        print(f"  {name} {version} — {pages:,} pages, {chars:,} characters")
    print()

    if not assume_yes:
        # `isatty()` is not enough on its own: a pipe, a CI runner or a harness
        # can look like a terminal and still hand back EOF. Anything other than
        # a person typing "yes" means no, because the alternative is deleting
        # someone's corpus on the strength of a closed stdin.
        try:
            answer = input("Type 'yes' to delete: ") if sys.stdin.isatty() else ""
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() != "yes":
            print("Nothing was removed. Pass --yes to confirm without a prompt.")
            return 1

    removed = 0
    for target in targets:
        name, _, version = target.partition("@")
        removed += store.delete(name.strip(), version.strip() or None)
    print(f"Removed {removed} harvest(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Before parse_args: --help prints the module docstring, which contains
    # arrows, and argparse writes it straight to a cp1252 console.
    enable_utf8_console()

    ap = argparse.ArgumentParser(
        prog="docsforge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("url", nargs="?",
                    help="the documentation source to extract")
    ap.add_argument("--forget", metavar="NAME[@VERSION]", action="append",
                    help="remove a harvest from the knowledge base and exit. "
                         "NAME alone removes every version of it; NAME@VERSION "
                         "removes one. Repeatable.")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt for --forget")
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

    if args.forget:
        return _forget(args.forget, assume_yes=args.yes)
    if not args.url:
        ap.error("a URL is required (or use --forget to remove a harvest)")

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
