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
from docsforge.core import topics
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
#: `requests` allows thirty. A documentation site that needs more than ten
#: hops to reach a page is not documenting anything, and each hop is a guard
#: check and a request.
MAX_REDIRECTS = 10

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


class HTTPStatusError(ForgeError):
    """A page the server answered, with a status that is not a page.

    Carries the status because the callers that count failures need to tell
    them apart: a 404 is a page that is not there, a 429 is a site asking
    the crawl to stop. Both used to be one `ForgeError` string, and a crawl
    that met thirty-three 429s in a row filed every one of them as "reached
    but not extractable -- nothing on it reads like documentation" (offline
    benchmark, 2026-09-20), which is the opposite of what happened.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


#: A 429 is the site asking for a pause, and `Retry-After` says how long.
#: Waited for, up to this many seconds and this many times per request; a
#: site that is still saying no after that is not asking for patience.
RETRY_AFTER_CAP = 30.0
RETRIES_ON_429 = 2

#: This many 429s in a row and a harvest stops rather than keep asking. The
#: pages it did not fetch are reported as refused, not as missing.
RATE_LIMIT_STOP = 3

#: A connection reset mid-request is asked again this many times, after a
#: pause that grows with each attempt (see `Fetcher._reset_tolerant`).
RESET_RETRIES = 2
RESET_PAUSE = 1.0

#: What a reset looks like by the time `requests` has wrapped it.
_RESET_MARKS = ("ConnectionResetError", "RemoteDisconnected", "Connection aborted",
                "Connection reset", "forcibly closed", "EOF occurred in violation")


def _was_reset(error: BaseException) -> bool:
    """Was this connection cut, rather than never made?

    A DNS failure and a refused connection are `ConnectionError`s too, and
    they are answers; a reset is an interruption."""
    text = repr(error)
    if "NameResolutionError" in text or "getaddrinfo" in text or "Failed to resolve" in text:
        return False
    return any(mark in text for mark in _RESET_MARKS)


def _retry_after(r) -> float | None:
    """Seconds the response asks the client to wait, or None if it does not say."""
    raw = (r.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(raw)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


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
    #: The section a "section" harvest keeps to, once `harvest()` has worked
    #: it out from where the start page lands and what its own navigation
    #: covers. Empty means "derive it from the URL" (`docs_scope`), which is
    #: every caller that has not been through `harvest()`. Read it through
    #: `_scope_for`, never directly.
    section: str = ""
    #: What the harvest is for, in the caller's words -- "web development",
    #: "authentication". Empty means the whole section. See `topics`.
    topic: str = ""
    #: Which release the caller asked for, when they named one. This decides
    #: which of the two acquisition pathways a harvest takes, so it has to
    #: reach discovery rather than only the label at the end: a site
    #: publishes `llms.txt` for its *current* release, and answering "give me
    #: 1.10" with it stores the wrong documentation under the right name.
    #: Empty means "whatever is current", which is what that file is for.
    version: str = ""
    #: What the registry says is current, when the caller named nothing. Not
    #: a request -- the label still comes from what the pages show -- but
    #: the first thing consulted when a sitemap files several releases side
    #: by side and one of them has to be the current one (Issues.md V1).
    release_hint: str = ""
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


#: Rendering budget: how long a page may take to arrive, and how long it may
#: keep changing after that before it is taken as it is.
RENDER_TIMEOUT_MS = 30_000
RENDER_SETTLE_MS = 6_000
#: Subresources a rendered page is not allowed to load.
RENDER_SKIPS = frozenset(("image", "media", "font"))
#: Rendered pages per harvest whose collapsed navigation is opened up first.
RENDER_EXPANSIONS = 2


def _settle(page) -> None:
    """Wait, within `RENDER_SETTLE_MS`, for a rendered page to stop changing."""
    for state, ms in (("load", RENDER_SETTLE_MS), ("networkidle", 2_500)):
        try:
            page.wait_for_load_state(state, timeout=ms)
        except Exception:                           # noqa: BLE001 -- bounded, not required
            pass
    last, steady = -1, 0
    for _ in range(12):
        try:
            size = page.evaluate("() => document.body ? document.body.innerText.length : 0")
        except Exception:                           # noqa: BLE001 -- a page mid-navigation
            return
        steady = steady + 1 if size == last else 0
        if steady >= 2 and size > 0:
            return
        last = size
        page.wait_for_timeout(250)


#: Opens what a documentation sidebar keeps collapsed. Many list a section's
#: pages only once it is expanded -- the links are not in the page until then
#: -- so a crawl that reads the sidebar as loaded never learns they exist.
#: Only toggles: a link with somewhere to go is never clicked.
_EXPAND_JS = """
async (max) => {
  const roots = 'aside, nav, [role=navigation], [class*="sidebar"], [class*="Sidebar"], ' +
                '[class*="sidenav"], [class*="side-nav"], [class*="toctree"]';
  let clicked = 0;
  for (let round = 0; round < 4; round++) {
    let changed = 0;
    for (const root of document.querySelectorAll(roots)) {
      for (const d of root.querySelectorAll('details:not([open])')) { d.open = true; changed++; }
      for (const el of root.querySelectorAll('[aria-expanded="false"]')) {
        if (clicked >= max) break;
        const href = el.tagName === 'A' ? (el.getAttribute('href') || '') : '';
        if (href && !href.startsWith('#') && !href.startsWith('javascript:')) continue;
        try { el.click(); clicked++; changed++; } catch (e) {}
      }
    }
    if (!changed || clicked >= max) break;
    await new Promise(r => setTimeout(r, 350));
  }
  return clicked;
}
"""


def _expand_navigation(page, most: int = 150) -> None:
    try:
        opened = page.evaluate(_EXPAND_JS, most)
        if opened:
            page.wait_for_timeout(400)
    except Exception:                               # noqa: BLE001 -- a page that refuses is as it was
        pass


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
        self._context = None
        #: Rendered pages whose navigation has been expanded (`_expand_navigation`).
        self._expansions = 0
        #: How many times a site asked this fetcher to wait (HTTP 429) and
        #: it did. Read by a harvest that wants to say so.
        self.throttled = 0

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
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
        """One request, every hop of it guarded.

        `guard` used to see only the URL a caller passed in. `requests` then
        followed any `3xx` on its own — `allow_redirects` defaults on, and
        most callers here pass it explicitly — and the guard never saw where
        it went. So a page that answered `302 Location: http://127.0.0.1/...`
        walked straight past the check that refused `127.0.0.1` directly:
        reproduced 2026-09-17 with a loopback server behind a redirector,
        secret returned as documentation. Evaluation.md §2.1.

        Redirects are followed here instead, each target guarded before it is
        fetched, and the response that comes back is shaped as `requests`
        would have shaped it — `url` is where the request landed, `history`
        holds the hops — because the resolver reads `r.url` to learn that
        `terraform.io` lives on `developer.hashicorp.com` and must go on
        learning it. Every HTTP call in the package comes through here, so
        there is exactly one place for this to live.
        """
        self.guard(url)
        kw.setdefault("timeout", TIMEOUT)
        follow = kw.pop("allow_redirects", True)
        try:
            r = self._patient(url, **kw)
            history: list[requests.Response] = []
            while follow and r.is_redirect:
                if len(history) >= MAX_REDIRECTS:
                    raise ForgeError(f"Too many redirects fetching {url} "
                                     f"({MAX_REDIRECTS} followed)")
                location = r.headers.get("location")
                if not location:
                    break
                target = urljoin(r.url, location)
                self.guard(target)              # the whole point
                history.append(r)
                r.close()
                r = self._patient(target, **kw)
            r.history = history
            return r
        except requests.RequestException as e:
            raise ForgeError(f"Request failed for {url}: {e}") from e

    def _patient(self, url: str, **kw) -> requests.Response:
        """One GET, waited out when the site asks for a pause.

        A 429 with `Retry-After` is honoured, bounded by `RETRY_AFTER_CAP`
        and tried `RETRIES_ON_429` times; a 429 that names no wait gets a
        short one. Anything else, including a 503, comes straight back:
        a 503 is the server's own trouble, and guessing a wait for it would
        only make a harvest slower at reporting that it is down.
        """
        for attempt in range(RETRIES_ON_429 + 1):
            r = self._reset_tolerant(url, **kw)
            if r.status_code != 429 or attempt == RETRIES_ON_429:
                return r
            pause = min(_retry_after(r) or 5.0, RETRY_AFTER_CAP)
            self.throttled += 1
            r.close()
            time.sleep(pause)
        return r                                # unreachable; keeps the type-checker honest

    def _reset_tolerant(self, url: str, **kw) -> requests.Response:
        """One GET, asked again when the connection was cut under it.

        A reset says nothing about the page: the server, or something between
        it and us, dropped the socket mid-handshake. Measured 2026-09-24 on
        this network, `doc.rust-lang.org` reset one request in three, and
        `raw.githubusercontent.com` did the same to bench-1 (Issues.md N1) --
        each costing a page that was there all along. Only resets are asked
        again: a name that does not resolve, a refused connection or a
        timeout already said what they had to say, and the resolver probes
        guessed domains that mostly do not exist, where a retry would only
        double the wait.
        """
        for attempt in range(RESET_RETRIES + 1):
            try:
                return self.session.get(url, allow_redirects=False, **kw)
            except requests.ConnectionError as e:
                if attempt == RESET_RETRIES or not _was_reset(e):
                    raise
                time.sleep(RESET_PAUSE * (attempt + 1))
        raise AssertionError("unreachable")     # the loop returns or raises

    def text(self, url: str, **kw) -> str:
        r = self.get(url, **kw)
        if r.status_code >= 400:
            raise HTTPStatusError(r.status_code, f"HTTP {r.status_code} for {url}")
        return _decode(r)

    def html(self, url: str) -> str:
        """Fetch a page as HTML, rendering JS if the run asked for it."""
        return self.html_at(url)[0]

    def html_at(self, url: str) -> tuple[str, str]:
        """A page as HTML, and the URL it was finally served from.

        The second value is what a relative link on the page means. It
        differs from `url` whenever the server redirected, and the common
        redirect is `/docs` to `/docs/` -- which changes what `quickstart`
        resolves to. The crawler used to resolve against the URL it had
        *asked for*, with the trailing slash already normalised away, so a
        crawl from click.palletsprojects.com/en/stable/ asked for 38 pages
        under /en/ that do not exist, got 404 for every one, and returned
        the entry page alone.
        """
        if self.opts.js:
            return self._render(url)
        r = self.get(url)
        if r.status_code >= 400:
            raise HTTPStatusError(r.status_code, f"HTTP {r.status_code} for {url}")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or ctype.endswith(("xml", "json", "+xml"))):
            raise ForgeError(f"Not a text document ({ctype}) at {url}")
        return _decode(r), (r.url or url)

    def render(self, url: str) -> str:
        """Fetch with JavaScript executed, whatever the run asked for.

        `html()` renders only when the run opted in. This is the one-shot
        escape hatch for a page that turned out to be a shell, and it is what
        makes `--js` unnecessary on the sites that used to need it.
        """
        return self._render(url)[0]

    def render_at(self, url: str) -> tuple[str, str]:
        """`render`, plus the URL the browser ended up on -- see `html_at`."""
        return self._render(url)

    def _render(self, url: str) -> tuple[str, str]:
        self.guard(url)
        page = self._page()
        refused: list[str] = []

        def gate(route, request):
            # Every request the page makes -- the navigation, its redirects,
            # every subresource -- passes here before it leaves. `page.goto`
            # follows redirects on its own, HTTP and JavaScript alike, and
            # gave the guard no say in where they went; this does. A refused
            # one is aborted before a body is read, and the first refusal is
            # what the caller is told, since the page it gets back would
            # otherwise be silently missing whatever was blocked.
            try:
                self.guard(request.url)
            except ForgeError as e:
                if not refused:
                    refused.append(str(e))
                route.abort("blockedbyclient")
                return
            # Pixels, video and fonts put no words on a page, and they are
            # most of what a page downloads.
            if getattr(request, "resource_type", "") in RENDER_SKIPS:
                route.abort()
                return
            route.continue_()

        try:
            page.route("**/*", gate)
            # Not `networkidle`. A page that keeps an analytics beacon or a
            # websocket open is never idle, and `docs.langchain.com` failed
            # every render at the 30-second mark waiting for it (2026-09-24).
            # The document first, then a bounded wait for the content to stop
            # changing -- which is what "rendered" actually means here.
            page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            if refused:
                raise ForgeError(refused[0])
            _settle(page)
            landed = getattr(page, "url", "") or url
            if self._expansions < RENDER_EXPANSIONS:
                self._expansions += 1
                _expand_navigation(page)
                if (getattr(page, "url", "") or url) != landed:
                    # A toggle navigated. Take the page as it was loaded.
                    page.goto(landed, wait_until="domcontentloaded",
                              timeout=RENDER_TIMEOUT_MS)
                    _settle(page)
            if refused:
                raise ForgeError(refused[0])
            return page.content(), landed
        except ForgeError:
            raise
        except Exception as e:
            if refused:
                raise ForgeError(refused[0]) from e
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
        if self._context is None:
            # One context for the whole harvest: a context per page paid for
            # a fresh browser profile on every page.
            self._context = self._browser.new_context()
        return self._context.new_page()


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
    literal = _inet_aton(host)
    if literal is not None:
        return [literal]
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


_DIGITS = {10: "0123456789", 8: "01234567", 16: "0123456789abcdefABCDEF"}


def _inet_aton(host: str):
    """An IPv4 address written any way `inet_aton` accepts, or None.

    `ipaddress` knows the dotted quad. libc also takes `2130706433`,
    `0x7f000001`, `0177.0.0.1` and `127.1`, and on Linux so does the
    resolver -- which is what this guard used to lean on to see 127.0.0.1
    behind those spellings. Windows' resolver takes none of them, so there
    the same URL was not refused but "failed to resolve", after a
    sixteen-second DNS wait for a host called 2130706433 (measured by the
    offline benchmark, 2026-09-20). Reading the spelling here makes the
    answer the same on every platform, and immediate.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(parts):
        return None
    values: list[int] = []
    for part in parts:
        if part[:2].lower() == "0x":
            base, digits = 16, part[2:]
        elif len(part) > 1 and part[0] == "0":
            base, digits = 8, part[1:]
        else:
            base, digits = 10, part
        # `int()` alone would also accept `1_0` and ` 1`, which no resolver does.
        if not digits or any(c not in _DIGITS[base] for c in digits):
            return None
        values.append(int(digits, base))
    head, last = values[:-1], values[-1]
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        return None
    number = 0
    for v in head:
        number = (number << 8) | v
    number = (number << (8 * (4 - len(head)))) | last
    return ipaddress.IPv4Address(number)


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
def detect_source(url: str, fetcher: Fetcher, scope: str | None = None) -> Detection:
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

    # Probe for an LLM-native dump, whatever depth the URL is at. A single
    # docs page is rarely what someone wants when the whole site is published
    # as one file two directories up.
    #
    # Nearest first. The origin's file used to be the only one asked for, and
    # an origin often publishes one about the *company*: `resend.com/llms-full.txt`
    # is 8 KB of product overview, `resend.com/docs/llms-full.txt` is 2.2 MB
    # of documentation, and a harvest from `/docs/introduction` stored the
    # overview as the whole of Resend's docs. Prisma, Next.js and AWS the same
    # (field test, 2026-09-24).
    for probe in _llms_probes(url, scope):
        try:
            r = fetcher.get(probe, timeout=10, allow_redirects=True)
        except ForgeError:
            continue
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code == 200 and "html" not in ctype:
            body = _decode(r)
            # An HTML fallback served without saying so -- judged as a whole
            # document, not by a leading `<`: Svelte's dumps open with
            # `<SYSTEM>` and are exactly what this is looking for.
            if _is_html_document(body):
                continue
            return Detection("llms_txt", probe, body)

    return Detection("html", url)


def _llms_dirs(url: str, scope: str | None = None) -> list[str]:
    """Where an llms file describing `url` could sit, nearest first: its docs
    section, each directory above that, and the origin."""
    scope = scope or docs_scope(url)
    parts = [p for p in scope.split("/") if p]
    dirs = ["/" + "/".join(parts[:i]) + "/" for i in range(len(parts), 0, -1)]
    return list(dict.fromkeys(dirs + ["/"]))


def _llms_probes(url: str, scope: str | None = None) -> list[str]:
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    return [origin + d + name for d in _llms_dirs(url, scope)
            for name in ("llms-full.txt", "llms.txt")]


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
    prefix = _scope_for(url, opts)
    host = (urlparse(url).hostname or "").lower()
    listed = _sitemap_urls(url, fetcher, opts,
                           keep=lambda l: _crawlable(l, host, prefix))
    scoped = [l for l in listed if _crawlable(l, host, prefix)]
    if scoped:
        found.sources.append("sitemap.xml")
        links += scoped

    found.urls = list(dict.fromkeys(_normalize(l) for l in links))
    return found


_SPA_ROOT = re.compile(
    r"""<(div|main|body)[^>]+id\s*=\s*["'](app|root|__next|__nuxt|svelte|___gatsby|"""
    r"""docs-root|main-app|application)["']""", re.I)


def _wants_render(html: str) -> bool:
    """Worth one rendered retry, now that static extraction found nothing.

    Wider than `_looks_like_shell`, which asks for almost no visible text: a
    client-rendered page often carries a `<noscript>` notice, a cookie banner
    and a footer, which is enough text to fail that test and none of it the
    documentation. `developer.apple.com/documentation/swiftui` is one --
    refused as unextractable, never rendered (field test, 2026-09-24). Only
    consulted after extraction has already failed, so a server-rendered page
    is never rendered for nothing.
    """
    if _looks_like_shell(html):
        return True
    low = (html or "").lower()
    scripted = bool(re.search(r"<script[^>]+src=", low))
    return scripted and bool(_SPA_ROOT.search(html or "") or "<noscript" in low)


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


#: A start URL that names a file is detected by its name, and landing it would
#: download the file to learn nothing.
_FILE_URL = re.compile(r"\.(txt|xml|json|ya?ml|md|markdown|rst|gz)$", re.I)


#: The start page is read whole when it is HTML -- its navigation is evidence
#: of where the manual's edges are (`_nav_scope`) -- up to this size.
LAND_MAX_BYTES = 4_000_000


def _land(url: str, fetcher) -> tuple[str, str]:
    """Where a start URL actually is, and its HTML when it is a page.

    Past HTTP redirects and a client-side redirect stub. Everything a harvest
    derives -- the docs section, which llms files are probed for, which
    sitemap entries are in scope -- was derived from the URL as given.
    `laravel.com/docs` is a 301 to `/framework/docs`, so the section was
    `/docs/`, nothing the site links to is under it, and the harvest stored
    one page (field test, 2026-09-24). The page itself comes back too, because
    its sidebar says where the manual's edges are (`_resolve_section`).
    """
    if _FILE_URL.search(urlparse(url).path) or \
            (urlparse(url).hostname or "").lower() in ("github.com", "www.github.com"):
        return url, ""
    landed, html = url, ""
    for _ in range(REDIRECT_HOPS):
        try:
            r = fetcher.get(landed, timeout=MAP_TIMEOUT, allow_redirects=True, stream=True)
        except (ForgeError, TypeError):
            return landed, html
        try:
            status = getattr(r, "status_code", 0) or 0
            if not 200 <= status < 400:
                return landed, html
            where = getattr(r, "url", "") or landed
            headers = getattr(r, "headers", None) or {}
            ctype = (headers.get("content-type") or "").lower()
            size = headers.get("content-length") or ""
            html = ""
            if "html" in ctype and (not size.isdigit() or int(size) <= LAND_MAX_BYTES):
                html = _decode(r)
        finally:
            closer = getattr(r, "close", None)
            if callable(closer):
                closer()
        target = _redirect_target(html, where) if html else ""
        if not target or _normalize(target) == _normalize(where):
            return where, html
        landed = target
    return landed, html


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
    roots = list(dict.fromkeys([origin + _scope_for(url, opts), origin + "/"]))

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
        scope = _scope_for(url, opts)
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
    """The first sitemap `find_sitemaps` knows of, or None."""
    found = find_sitemaps(url, fetcher, opts)
    return found[0] if found else None


def _is_sitemap_body(text: str, ctype: str = "") -> bool:
    """An XML sitemap or index, or a plain-text list of URLs -- not an HTML
    page a server answers every path with."""
    head = (text or "").lstrip()[:400].lower()
    if head.startswith(("<?xml", "<urlset", "<sitemapindex")):
        return True
    if head.startswith("<"):
        return False
    first = head.split("\n", 1)[0].strip()
    return first.startswith(("http://", "https://")) and "html" not in ctype


def find_sitemaps(url: str, fetcher: Fetcher, opts: Options) -> list[str]:
    """Every sitemap that could list this documentation, declared ones first.

    robots.txt is the declared location, and it may declare several: one per
    product, one per language, one for the docs. The first was the only one
    ever read, so a site that lists its documentation in the second was
    harvested by crawl or not at all. A sitemap beside the docs section itself
    (`/lambda/latest/dg/sitemap.xml`) is asked for as well, because the
    site-wide one often leaves the docs to a sitemap of their own. The
    conventional root paths are the fallback when nothing else answered.
    """
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    found: list[str] = []

    try:
        robots = fetcher.text(origin + "/robots.txt", timeout=10)
    except ForgeError:
        robots = ""
    for line in robots.splitlines():
        if line.lower().startswith("sitemap:"):
            declared = line.split(":", 1)[1].strip()
            if declared and declared not in found:
                _log(opts, f"  sitemap from robots.txt: {declared}")
                found.append(declared)

    def answers(probe: str) -> bool:
        try:
            r = fetcher.get(probe, timeout=10, allow_redirects=True)
        except ForgeError:
            return False
        ctype = (r.headers.get("content-type", "") or "").lower()
        return r.status_code == 200 and ("xml" in ctype or _is_sitemap_body(r.text, ctype))

    # The section's own sitemap, when it is not the root's.
    for where in [d for d in _llms_dirs(url, _scope_for(url, opts)) if d != "/"][:2]:
        probe = origin + where + "sitemap.xml"
        if probe not in found and answers(probe):
            _log(opts, f"  sitemap found beside the docs at {probe}")
            found.append(probe)

    if not found:
        for candidate in SITEMAP_CANDIDATES:
            probe = origin + candidate
            if answers(probe):
                _log(opts, f"  sitemap found at {probe}")
                found.append(probe)
                break
    return found


def _sitemap_body(url: str, fetcher) -> str:
    """A sitemap's text, gunzipped when it is served compressed.

    `sitemap.xml.gz` is as valid as `sitemap.xml`, and a server that sends it
    as `application/gzip` gets no help from `requests`, which only inflates
    a `Content-Encoding`. Read as text, it was bytes of noise with no `<loc>`.
    """
    if not urlparse(url).path.lower().endswith(".gz") or not hasattr(fetcher, "get"):
        return fetcher.text(url)
    r = fetcher.get(url, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise HTTPStatusError(r.status_code, f"HTTP {r.status_code} for {url}")
    raw = r.content or b""
    if raw[:2] == b"\x1f\x8b":
        import gzip
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            raise ForgeError(f"Could not inflate {url}: {e}") from e
    return raw.decode("utf-8", "replace")


#: How much sitemap a harvest reads before it stops opening more. A site-wide
#: index can name thousands of files -- every AWS service, every Go module,
#: every Microsoft Learn locale -- and reading them all to find one guide's
#: pages timed three harvests out at seven minutes without a page stored
#: (field test, 2026-09-24). Children most likely to hold the section are
#: opened first (`_sitemap_links`), so a budget costs the least likely ones.
SITEMAP_FILES = 40
SITEMAP_SECONDS = 90.0


class _SitemapAllowance:
    def __init__(self) -> None:
        self.files = 0
        self.deadline = time.monotonic() + SITEMAP_SECONDS
        self.cut = False

    def spend(self) -> bool:
        """Take one more file, or say the budget is gone."""
        if self.files >= SITEMAP_FILES or time.monotonic() > self.deadline:
            self.cut = True
            return False
        self.files += 1
        return True


def _sitemap_urls(url: str, fetcher, opts: Options, keep=None) -> list[str]:
    """Every URL the site's sitemaps list, in their order, each once.

    A sitemap beside the section is read first, and when it lists the section
    the site-wide ones are not opened at all: `/lambda/latest/dg/sitemap.xml`
    is the Lambda guide, and `sitemap_index.xml` is all of AWS.
    """
    listed: list[str] = []
    budget = _SitemapAllowance()
    here = _scope_for(url, opts)
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    found = find_sitemaps(url, fetcher, opts)
    local = [s for s in found if s.startswith(origin) and here != "/" and
             urlparse(s).path.startswith(here)]
    for group in ([local, [s for s in found if s not in local]] if local else [found]):
        for sitemap in group:
            if not budget.spend():
                break
            try:
                listed += _sitemap_links(_sitemap_body(sitemap, fetcher), fetcher, opts,
                                         keep=keep, hint=here, budget=budget)
            except ForgeError as e:
                _log(opts, f"  skip sitemap {sitemap}: {e}")
        if group is local and len([u for u in listed if keep is None or keep(u)]) >= 3:
            break
    if budget.cut:
        _log(opts, f"  stopped reading sitemaps after {budget.files} file(s): the site "
                   f"publishes more than one harvest should open to find a section")
    return list(dict.fromkeys(listed))


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

    fences = _fence_spans(text)
    levels: list[tuple[str, list]] = []
    for prefix in ("#", "##", "###"):
        hits = [h for h in re.finditer(rf"^{prefix}[ \t]+(\S[^\n]*)$", text, re.M)
                if not _inside(h.start(), fences)]
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
                docs.append(Doc(link_url, title, _meta_header(link_url, "llms_txt")
                                 + _tidy_markdown(text, link_url)))
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
        links = _topic_prefilter(links, opts, stats, titled=True)

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
            root_doc = Doc(det.url, "llms.txt Overview",
                           _meta_header(det.url, "llms.txt") + _tidy_markdown(body, det.url))
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
    pages = _dump_pages(body)
    if pages:
        # Cut where the dump says its pages are, each under its own address.
        docs = [Doc(where or det.url, title,
                    _meta_header(where or det.url, "llms.txt")
                    + _tidy_markdown(chunk, where or det.url))
                for where, title, chunk in pages]
        _log(opts, f"  cut the dump into the {len(docs)} pages it says it holds")
    else:
        parts = _split_dump(body)
        docs = [Doc(f"{det.url}#{_anchor(title)}", title,
                    _meta_header(det.url, "llms.txt") + _tidy_markdown(chunk, det.url))
                for title, chunk in parts]
        if not docs:
            docs = [Doc(det.url, "llms.txt",
                        _meta_header(det.url, "llms.txt") + _tidy_markdown(body, det.url))]
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


#: Path segments a site uses for a release line rather than a number. The
#: first group names the current release; the second names one nobody should
#: be handed when they asked for nothing in particular.
_CURRENT_LINES = ("stable", "latest", "current")
_DEV_LINES = frozenset(("dev", "next", "main", "master", "canary", "nightly",
                        "unstable", "edge", "alpha", "beta", "rc"))
#: A numbered release line as a path segment: `v6`, `4.2`, `1.10.4`. Stricter
#: than `_VERSION`, which a caller's own request is matched against: a bare
#: integer here is a chapter, a page of a blog archive or a year, and
#: `docs.python.org/3/` is the current documentation, filed beside `/3.12/`.
_RELEASE_LINE = re.compile(
    r"^(v\d+(\.(\d+|x))*|\d+\.(\d+|x)(\.(\d+|x))*)([-.]?(snapshot|m\d+|rc\.?\d*|"
    r"beta\.?\d*|alpha\.?\d*|pre\.?\d*|preview\.?\d*|dev\d*|next))?$", re.I)
#: (`28.x` is Docusaurus's name for a release line: jestjs.io files
#: `/docs/28.x/` beside its current `/docs/`.)
#: A release line that is not a release: `4.2-SNAPSHOT`, `3.0.0-rc.1`. Spring
#: Boot files `/spring-boot/4.2-SNAPSHOT/` beside its current pages, and a
#: pattern that did not know the suffix took the snapshot for current pages
#: filed under no release at all (field test, 2026-09-24). Found like a
#: release, chosen like `dev` -- only when the site points at it.
_PRERELEASE = re.compile(r"[-.]?(snapshot|m\d+|rc\.?\d*|beta\.?\d*|alpha\.?\d*|"
                         r"pre\.?\d*|preview\.?\d*|dev\d*|next)$", re.I)


def _release_segment(url: str) -> tuple[int, str] | None:
    """`(depth, segment)` of the first path part that names a release line,
    or None."""
    for depth, part in enumerate(p for p in urlparse(url).path.split("/") if p):
        low = part.lower()
        if low in _CURRENT_LINES or low in _DEV_LINES or _RELEASE_LINE.match(part):
            return depth, low
    return None


def _release_groups(urls: list[str]) -> dict[str, list[str]]:
    """The URLs by the release line each is filed under, `""` for none.

    Judged at the shallowest depth any URL names a release, so
    `/en/dev/releases/5.2/` is filed under `dev`, and `/en/5.2/releases/4.2/`
    under `5.2` -- a release that scopes a path comes early in it.
    """
    found = [(_release_segment(u), u) for u in urls]
    depths = [seg[0] for seg, _ in found if seg is not None]
    if not depths:
        return {"": list(urls)}
    shallowest = min(depths)
    groups: dict[str, list[str]] = {}
    for seg, url in found:
        key = seg[1] if seg is not None and seg[0] == shallowest else ""
        groups.setdefault(key, []).append(url)
    return groups


def _entry_page(url: str, fetcher: Fetcher) -> tuple[str, list[str]]:
    """Where the start URL lands, and the same-host links on that page.

    One request, made only when a sitemap lists several releases and nothing
    cheaper says which is current: the page a site sends its readers to is
    the site's own answer.
    """
    try:
        r = fetcher.get(url, timeout=10, allow_redirects=True)
    except ForgeError:
        return "", []
    landed = getattr(r, "url", "") or ""
    host = (urlparse(url).hostname or "").lower()
    links: list[str] = []
    if "html" in (r.headers.get("content-type") or "").lower():
        try:
            soup = _soup(_decode(r), "html.parser")
        except Exception:  # noqa: BLE001 -- a page that will not parse is no evidence
            return landed, []
        for a in soup.find_all("a", href=True):
            link = urljoin(landed or url, a["href"])
            if (urlparse(link).hostname or "").lower() == host:
                links.append(link)
    return landed, links


def _prefer_current_release(urls: list[str], opts: Options, hint: str = "",
                            entry=None) -> tuple[list[str], str]:
    """One release, not all of them, when the caller asked for nothing else.

    A sitemap that files every release side by side -- `docs.djangoproject.com`
    lists `/en/4.2/` beside `/en/6.1/` and `/en/dev/`; `python-poetry.org`
    lists `/docs/1.8/` and `/docs/main/` beside `/docs/` -- is sorted however
    the site sorts it, so a capped harvest of Django returned forty pages of
    `/en/dev/` labelled with PyPI's 6.1.1, Poetry's current release came back
    three releases mixed, and an uncapped harvest would have stored every
    release under one label. Measured 2026-09-21, `benchmarks/bench-2`
    (Issues.md V1); the named-release path had been fixed for this two days
    earlier and this is its twin.

    The current release is, in order: the one the registry says is current
    (`hint`); the pages filed under no release at all, when there are enough
    of them to be the documentation rather than a stray index; the release
    the entry page redirects to, then the one it links to most (one request,
    paid only when the cheaper signals are silent); the line the site calls
    `stable`, `latest` or `current`; the highest-numbered release. A
    development line is never chosen by number, only by the site pointing
    at it. Returns the URLs kept and the release they were kept for, `""`
    when the list was left alone.
    """
    groups = _release_groups(urls)
    if len(groups) < 2:
        return urls, ""
    named = [k for k in groups if k]

    def choose(key: str, why: str) -> tuple[list[str], str]:
        _log(opts, f"  the sitemap files {len(named)} releases side by side "
                   f"({', '.join(sorted(named))}); taking {key or 'the unversioned pages'} "
                   f"as current: {why}")
        return groups[key], key

    if hint:
        agreeing = [k for k in named
                    if versions.same_release(k, hint) or versions.same_release(hint, k)]
        if agreeing:
            return choose(max(agreeing, key=versions.sort_key), f"the registry's release is {hint}")
    if len(groups.get("", [])) >= 3:
        return choose("", "the current documentation is filed under no release")
    landed, links = entry() if entry is not None else ("", [])
    seg = _release_segment(landed) if landed else None
    if seg is not None and seg[1] in groups:
        return choose(seg[1], f"the site redirects its front page there")
    tally: dict[str, int] = {}
    for link in links:
        s = _release_segment(link)
        key = s[1] if s is not None else ""
        if key in groups:
            tally[key] = tally.get(key, 0) + 1
    if tally:
        most = max(tally, key=lambda k: (tally[k], k != ""))
        if most:
            return choose(most, f"the front page links there {tally[most]} times")
    for line in _CURRENT_LINES:
        if line in groups:
            return choose(line, f"the site calls it {line}")
    numbered = [k for k in named if k not in _DEV_LINES and not _PRERELEASE.search(k)
                and versions.release_parts(k)]
    if numbered:
        return choose(max(numbered, key=versions.sort_key), "the highest-numbered release")
    return urls, ""


def _url_for_release(url: str, asked: str, fetcher: Fetcher, opts: Options,
                     stats: dict | None = None) -> str:
    """The start URL with the release asked for in place of the one it names.

    `pydantic` resolves to `/docs/validation/latest/llms.txt`, and a request
    for 1.10 was answered with that file: a manifest scoped below the site
    root skips the site-wide release check, so the 2.x dump was stored under
    **1.10** with a caveat appended (bench-2, Issues.md V2). The site
    publishes `/docs/validation/1.10/llms-full.txt`, one path segment away.

    So when the URL names a release line -- `latest`, `v6`, `2.9` -- that is
    not the one asked for, ask the site for the same path under the release
    asked for. One request; the harvest starts there if the site answers
    under that release, and where it started is left alone otherwise: a URL
    that names no release, one that already names the right one, and a site
    that has no such path all go on as before.
    """
    seg = _release_segment(url)
    if seg is None:
        return url
    depth, current = seg
    if versions.same_release(asked, current):
        return url
    wanted = asked.strip()
    if current.startswith("v") and versions.release_parts(current) and \
            versions.release_parts(wanted) and not wanted.lower().startswith("v"):
        wanted = "v" + wanted           # the site's spelling: /docs/v6/ asks for /docs/v7/
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    seen = -1
    for i, part in enumerate(parts):
        if part:
            seen += 1
            if seen == depth:
                parts[i] = wanted
                break
    candidate = parsed._replace(path="/".join(parts)).geturl()
    try:
        r = fetcher.get(candidate, timeout=10, allow_redirects=True)
    except ForgeError:
        return url
    if getattr(r, "status_code", 0) != 200:
        return url
    landed = _release_segment(getattr(r, "url", "") or candidate)
    if landed is None or not versions.same_release(asked, landed[1]):
        return url                      # sent back to the release it already had
    _log(opts, f"  {url} is the {current} documentation; {asked} was asked for, "
               f"and the site answers for it at {candidate}")
    if stats is not None:
        stats["release_url"] = candidate
        # The site filing the pages under the release asked for is what
        # makes the label a finding rather than the request repeated back.
        stats["release_confirmed"] = True
    return candidate


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
_DUMP_SOURCE = re.compile(r"^(Source|URL):[ \t]*<?(https?://[^\s>]+)>?[ \t]*$", re.M)


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """Where the fenced code blocks are, as `[(start, end)]`.

    A `#` at the start of a line inside one is a shell comment, not a
    heading: `docs.deno.com/llms-full.txt` split on them made pages called
    "For entire app" and "Variables" out of Terraform snippets -- 254 of its
    837 pages under 400 characters (field test, 2026-09-24).
    """
    # Any indentation: a fence inside a list item is indented with it, and
    # reading only the CommonMark three spaces left react-native's dump with
    # 1,165 fence marks -- an odd number -- so everything after the stray one
    # counted as code, and none of it was tidied (uncapped field test,
    # 2026-09-24). A fence left open at the end is a malformed document, not
    # a code block running to the end of it, and is ignored.
    #
    # And as CommonMark reads them: an opener carries at most an info string
    # (`jsx title="App.js"`), a closer is the bare fence alone on its line.
    # The same dump puts code in table cells -- `| ```jsx` opens mid-line and
    # `` ``` | ![image](…) `` closes a line that goes on being a table -- and
    # counting every line that merely starts with a fence paired the rest of
    # the file wrongly.
    spans, open_at, fence = [], None, ""
    for m in re.finditer(r"^[ \t]*(`{3,}|~{3,})([^\n]*)$", text, re.M):
        mark, rest = m.group(1), m.group(2).strip()
        if open_at is None:
            if rest.startswith("|") or ("`" in rest and mark[0] == "`") or len(rest) > 80:
                continue                    # not a fence: a table cell, inline code
            open_at, fence = m.start(), mark
        elif not rest and mark[0] == fence[0] and len(mark) >= len(fence):
            spans.append((open_at, m.end()))
            open_at = None
    return spans


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    import bisect
    i = bisect.bisect_right(spans, (pos, float("inf"))) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _dump_sections(text: str) -> list[tuple[str, int, int]]:
    """`[(source_url, start, end)]` for a dump that states each page's origin.

    Two spellings are in use: `Source: <url>` under the page's heading
    (Mintlify, `docs.langchain.com`) and `URL: <url>` a line or two below it
    (`docs.deno.com`). A bare `URL:` line with no heading above it is prose
    about some URL, not a page boundary, and neither counts inside a code
    block.
    """
    fences = _fence_spans(text)
    starts: list[tuple[int, str]] = []
    for mark in _DUMP_SOURCE.finditer(text):
        if _inside(mark.start(), fences):
            continue
        line_start = text.rfind("\n", 0, mark.start()) + 1
        # The heading above it introduces the page, so the section starts
        # there -- looking past blank lines and the one-line summary some
        # dumps put between the two (`docs.deno.com`: "# Config files",
        # "> How Deno projects are configured...", "URL: ..."), a few lines
        # at most.
        heading, cursor = None, line_start
        for _ in range(8):
            if cursor <= 0:
                break
            prev_start = text.rfind("\n", 0, cursor - 1) + 1
            line = text[prev_start:cursor - 1]
            if not line.strip() or line.lstrip().startswith(">"):
                cursor = prev_start
                continue
            if line.lstrip().startswith("#"):
                heading = prev_start
            break
        if mark.group(1) != "Source" and heading is None:
            continue
        starts.append((heading if heading is not None else line_start, mark.group(2)))

    # A third spelling: a page's heading *is* a link to the page --
    # `# [Aliases](https://pydantic.dev/docs/validation/latest/api/pydantic/aliases/)`
    # opens each of the 396 pages in Pydantic's dump, which states no
    # `Source:` at all and was cut on its headings into 214 fragments under
    # 400 characters (field test, 2026-09-24). Taken when it outnumbers the
    # other spellings.
    linked = [(m.start(), m.group(1)) for m in _DUMP_HEADING_LINK.finditer(text)
              if not _inside(m.start(), fences)]
    if len(linked) > len(starts):
        starts = linked

    out = []
    for i, (start, source) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        out.append((source, start, end))
    return out


_DUMP_HEADING_LINK = re.compile(r"^#{1,2}[ \t]*\[[^\]\n]+\]\((https?://[^)\s]+)\)[ \t]*$", re.M)


def _dump_pages(text: str) -> list[tuple[str, str, str]]:
    """A dump cut into the pages it says it holds: `[(url, title, chunk)]`.

    Where the dump states each page's origin, that is where its pages begin
    and end -- a better cut than any heading level, and each page keeps the
    address it was published at instead of `llms-full.txt#some-anchor`, so a
    citation points somewhere a reader can go. Text above the first page is
    the dump's own preamble and is kept as a page of its own. `[]` when the
    dump states too few origins to be cut this way.
    """
    sections = _dump_sections(text)
    if len(sections) < SPLIT_MIN_PARTS:
        return []
    pages: list[tuple[str, str, str]] = []
    head = text[:sections[0][1]].strip()
    if len(head) >= MIN_MAIN_CHARS:
        first = re.sub(r"<[^>]*>", " ", head.split("\n", 1)[0]).lstrip("# ").strip()
        pages.append(("", first[:90].rstrip() or "Overview", head))
    seen: dict[str, int] = {}
    for source, start, end in sections:
        chunk = text[start:end].strip()
        if not chunk:
            continue
        line = chunk.split("\n", 1)[0]
        title = line.lstrip("# ").strip() if line.lstrip().startswith("#") else ""
        linked_title = re.match(r"^\[(.+?)\]\([^)]*\)$", title)
        if linked_title:
            title = linked_title.group(1).strip()
        title = title or urlparse(source).path.rstrip("/").rsplit("/", 1)[-1] or source
        # The same page twice -- split across two entries -- stays two pages.
        seen[source] = seen.get(source, 0) + 1
        url = source if seen[source] == 1 else f"{source}#part-{seen[source]}"
        pages.append((url, title[:200], chunk))
    return pages


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
    if not _broader_than_request(det, url, opts):
        return Pathway(False, None, False)   # already scoped to what was asked for

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
    if not asked and _scope_for(url, opts) == "/":
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
    prefix = _scope_for(url, opts)
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


def _one_real_sentence(el) -> bool:
    """Does this container say something -- a paragraph of prose, not a
    heading, a spinner or a line of links?"""
    for p in el.find_all("p"):
        text = p.get_text(" ", strip=True)
        linked = sum(len(a.get_text(" ", strip=True)) for a in p.find_all("a"))
        if len(text) >= 30 and linked < len(text) / 2 and " " in text:
            return True
    return False


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
    short, short_selector = None, ""
    for sel in CONTENT:
        found = soup.select_one(sel)
        if found and len(found.get_text(strip=True)) > MIN_MAIN_CHARS:
            chosen, selector = found, sel
            break
        if found is not None and short is None and _one_real_sentence(found):
            short, short_selector = found, sel

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
    if short is not None:
        # A page with one thing to say. Flask's `deploying/eventlet` is
        # "Eventlet is no longer maintained. Use gevent instead." inside
        # Sphinx's `role=main`, and the 200-character floor refused it as a
        # stub (uncapped field test, 2026-09-24). A shell's container holds no
        # paragraph once `<noscript>` is stripped, so it still fails.
        return short, short_selector
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


# ─────────────────────────────────────────────────────────────
# Tidying what was chosen, before it becomes Markdown
# ─────────────────────────────────────────────────────────────
# Measured across 53 real sites on 2026-09-24 (`scripts/fieldtest.py`): every
# heading on Docusaurus sites carried `[​](#x "Direct link to X")`, Sphinx and
# pkg.go.dev a `¶`; Tailwind's code came out one line per block because each
# line is a `<span class="line">` with no newline between them; relative links
# pointed nowhere once the page was stored; and "Copy", "Edit this page" and
# "Last updated" were filed as documentation. None of it changes what the
# publisher wrote -- it is the page's furniture, not its words.

def _unhide(el) -> None:
    for attr in ("hidden", "aria-hidden"):
        if el.has_attr(attr):
            del el[attr]
    style = el.get("style") or ""
    if "display" in style and "none" in style:
        del el["style"]


def _label_before(soup, panel, label: str) -> None:
    if not label:
        return
    p = soup.new_tag("p")
    strong = soup.new_tag("strong")
    strong.string = label
    p.append(strong)
    panel.insert(0, p)


def _flatten_tabs(soup) -> None:
    """Every tab's content, each under its own label, none of it hidden.

    A tab set shows one panel and hides the rest -- `hidden`, `aria-hidden`
    -- and `strip_chrome` removes whatever is `aria-hidden`, so the Python
    tab of a Python/JavaScript example was stored and the JavaScript one
    silently was not. Run before any stripping.
    """
    for tablist in soup.select("[role=tablist]"):
        tabs = tablist.select("[role=tab]")
        if not tabs:
            continue
        panels = []
        for tab in tabs:
            target = tab.get("aria-controls")
            panels.append(soup.find(id=target) if target else None)
        if not all(panels):
            container = tablist.parent
            around = container.select("[role=tabpanel]") if container is not None else []
            if len(around) == len(tabs):
                panels = around
        for tab, panel in zip(tabs, panels):
            if panel is None:
                continue
            _unhide(panel)
            _label_before(soup, panel, tab.get_text(" ", strip=True))
        if all(panels):
            tablist.decompose()
    # Generators that build tabs from radio buttons and labels, pairing the
    # n-th label with the n-th block: MkDocs Material, VitePress code groups.
    for group, labels_sel, blocks_sel in ((".tabbed-set", ".tabbed-labels > label, :scope > label",
                                           ".tabbed-content > .tabbed-block"),
                                          (".vp-code-group", ".tabs label", ".blocks > *"),
                                          (".code-group", ".tabs label", ".blocks > *")):
        for box in soup.select(group):
            try:
                labels = box.select(labels_sel)
                blocks = box.select(blocks_sel)
            except Exception:                   # noqa: BLE001 -- a selector bs4 rejects
                continue
            if not labels or len(labels) != len(blocks):
                continue
            for label, block in zip(labels, blocks):
                _unhide(block)
                _label_before(soup, block, label.get_text(" ", strip=True))
            for label in labels:
                label.decompose()
            for radio in box.select("input"):
                radio.decompose()
    for panel in soup.select("[role=tabpanel]"):
        _unhide(panel)


_PERMALINK_WORDS = {"", "#", "¶", "§", "🔗", "link", "permalink", "anchor", "#️⃣"}
_PERMALINK_CLASSES = ("headerlink", "hash-link", "anchor", "anchorjs", "header-anchor",
                      "permalink", "heading-link", "idlink", "autolink", "deep-link")


def _drop_permalinks(main) -> None:
    """A heading's link to itself is not part of the heading."""
    for a in main.select("h1 a, h2 a, h3 a, h4 a, h5 a, h6 a, dt a, a.headerlink"):
        if getattr(a, "decomposed", False) or a.parent is None:
            continue
        href = (a.get("href") or "").strip()
        if href and not href.startswith("#"):
            continue
        words = a.get_text("", strip=True).replace("​", "").strip().lower()
        label = " ".join([a.get("aria-label") or "", a.get("title") or ""]).lower()
        classes = " ".join(a.get("class") or []).lower()
        if (words in _PERMALINK_WORDS
                or any(k in label for k in ("direct link", "permalink", "link to", "go to "))
                or any(k in classes for k in _PERMALINK_CLASSES)):
            if a.find_parent(["h1", "h2", "h3", "h4", "h5", "h6", "dt"]) is not None or \
                    words in _PERMALINK_WORDS:
                a.decompose()
                continue
        if a.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"]) is not None:
            a.unwrap()          # a heading whose words are its own anchor keeps its words
    # And anywhere else: Playwright marks every API parameter `[#](#option-x)`
    # and pkg.go.dev every example `Example [¶](#example-Handle)`. A link to
    # this page whose whole text is a symbol is a permalink, wherever it sits.
    for a in main.find_all("a", href=True):
        if getattr(a, "decomposed", False) or a.parent is None:
            continue
        if a["href"].strip().startswith("#") and \
                a.get_text("", strip=True).replace("​", "").strip().lower() in _PERMALINK_WORDS:
            a.decompose()


_UI_CHROME = re.compile(
    r"^(copy|clipboard)|copy-?(button|btn|code|icon)|edit-?(this-)?page|theme-edit|"
    r"last-?updated|feedback|was-?this|helpful|page-?rating|pagination|prev-?next|"
    r"breadcrumbs?$|table-of-contents|on-this-page|^sr-only$|visually-?hidden|"
    r"skip-?(to|link)|theme-doc-footer|doc-?footer|docs-footer|article-footer|"
    r"^toc$|^toc-|-toc$", re.I)
#: A button that says one of these is furniture; any other button keeps its words.
_BUTTON_WORDS = re.compile(
    r"^(copy|copied!?|copy code|copy to clipboard|copy page|copy as markdown|"
    r"view as markdown|open in \w+|ask ai|expand|collapse|show more|show less|"
    r"toggle.*|close|menu|share|feedback|yes|no|edit|)$", re.I)


def _drop_ui_chrome(main) -> None:
    for el in list(main.find_all(True)):
        if getattr(el, "decomposed", False) or el.parent is None:
            continue
        marks = list(el.get("class") or []) + ([el.get("id")] if el.get("id") else [])
        if any(_UI_CHROME.search(m) for m in marks if m):
            el.decompose()
            continue
        if el.name == "button":
            if _BUTTON_WORDS.match(el.get_text(" ", strip=True)):
                el.decompose()
            else:
                el.unwrap()


#: Elements that hold one line of a highlighted code block each.
_CODE_LINES = (".line", ".token-line", ".code-line", ".cm-line", ".ec-line",
               "[data-line]", ".highlight-line", ".react-syntax-highlighter-line")
_CODE_GUTTER = (".linenos", ".lineno", ".line-number", ".line-numbers",
                ".line-numbers-rows", ".react-syntax-highlighter-line-number",
                ".gutter", "button", ".copy", "[class*=copy]")
_LANG_CLASS = re.compile(r"^(?:language|lang|highlight|hljs|brush)-([\w+#.-]+)$", re.I)


def _code_language(pre) -> str:
    for node in [pre, pre.find("code")] + list(pre.parents)[:3]:
        if node is None or not hasattr(node, "get"):
            continue
        for attr in ("data-language", "data-lang", "data-code-lang"):
            if node.get(attr):
                return str(node.get(attr)).strip().lower()
        for cls in node.get("class") or []:
            m = _LANG_CLASS.match(cls)
            if m and m.group(1).lower() not in ("none", "default", "plaintext"):
                return m.group(1).lower()
    return ""


def _clean_code_blocks(main) -> None:
    """One newline per line of code, whatever markup the highlighter used."""
    for pre in main.find_all("pre"):
        if getattr(pre, "decomposed", False) or pre.parent is None:
            continue
        for junk in pre.select(", ".join(_CODE_GUTTER)):
            junk.decompose()
        lang = _code_language(pre)
        lines = pre.select(", ".join(_CODE_LINES))
        # Only the outermost line elements: a highlighter may nest them.
        ids = {id(l) for l in lines}
        lines = [l for l in lines if not any(id(p) in ids for p in l.parents)]
        if len(lines) >= 2:
            text = "\n".join(l.get_text().rstrip("\n") for l in lines)
        else:
            for br in pre.find_all("br"):
                br.replace_with("\n")
            text = pre.get_text()
        pre.clear()
        code = _soup_of(pre).new_tag("code")
        code.string = text.strip("\n")
        pre.append(code)
        if lang:
            pre["data-df-lang"] = lang


def _soup_of(el):
    """The document an element belongs to -- the only thing that makes tags."""
    root = el
    while getattr(root, "parent", None) is not None:
        root = root.parent
    if hasattr(root, "new_tag"):
        return root
    from bs4 import BeautifulSoup
    return BeautifulSoup("", "html.parser")


def _absolutize(main, base: str) -> None:
    """Links and images that still point somewhere once the page is stored."""
    for a in main.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("javascript:"):
            a.unwrap()
        elif href and not href.startswith(("#", "mailto:", "tel:", "data:")):
            a["href"] = urljoin(base, href)
    for img in main.find_all("img"):
        src = (img.get("src") or "").strip()
        lazy = (img.get("data-src") or img.get("data-lazy-src") or "").strip()
        if (not src or src.startswith("data:")) and lazy:
            src = lazy
        if src.startswith("data:"):
            img.decompose()             # inline pixels, kilobytes of base64, no words
        elif src:
            img["src"] = urljoin(base, src)


_CHROME_LINE = re.compile(
    r"^\s*(copy|copied!?|copy code|copy page|copy to clipboard|copy as markdown|"
    r"view as markdown|open in (chatgpt|claude|cursor)|ask ai|edit this page|edit on github|"
    r"edit page|on this page|in this article|table of contents|skip to (main )?content|"
    r"was this (page|article)? ?helpful\??|thank you for your feedback[.!]?|"
    r"previous|next|previous page|next page)\s*$", re.I)
_LAST_UPDATED = re.compile(r"^\s*last (updated|modified)\b.{0,80}$", re.I)


def _drop_chrome_lines(body: str) -> str:
    out, fence = [], False
    for line in body.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and (_CHROME_LINE.match(line) or _LAST_UPDATED.match(line)):
            continue
        out.append(line)
    return "\n".join(out)


def _markdown(main) -> str:
    """The chosen element as Markdown, fences sized to the code inside them."""
    from markdownify import MarkdownConverter

    class _Converter(MarkdownConverter):
        def convert_pre(self, el, text, parent_tags):
            if not text:
                return ""
            lang = el.get("data-df-lang") or ""
            fence = "```"
            while fence in text:
                fence += "`"
            return f"\n\n{fence}{lang}\n{text.strip(chr(10))}\n{fence}\n\n"

    return _Converter(heading_style="ATX", bullets="-",
                      table_infer_header=True).convert_soup(main)


def _html_to_md(html: str, url: str, soup=None, plan=None,
                report: dict | None = None) -> tuple[str, str]:
    try:
        import markdownify  # noqa: F401 -- the converter is built in `_markdown`
    except ImportError as e:
        raise ForgeError("HTML extraction needs: pip install markdownify") from e

    soup = soup if soup is not None else _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    title = title or "Untitled"
    base = _document_base(soup, url)

    _flatten_tabs(soup)
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

    _drop_permalinks(main)
    _drop_ui_chrome(main)
    _clean_code_blocks(main)
    _absolutize(main, base)
    body = _drop_chrome_lines(_markdown(main))
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

    # No recognisable docs root: stay in the start page's own folder. A path
    # that ends in a slash *is* a folder -- `/en/stable/` is the stable
    # manual, and taking its last segment for a page widened the scope to
    # `/en/`, every release the site keeps side by side.
    if urlparse(url).path.endswith("/"):
        folder = parts
    else:
        folder = parts[:-1] if "." in parts[-1] or len(parts) > 1 else parts
    return "/" + "/".join(folder) + "/" if folder else "/"


def _scope_for(url: str, opts: "Options | None" = None) -> str:
    """The path prefix a harvest of `url` keeps to.

    What the caller typed wins (`scope="host"`, or a literal prefix); then the
    section `harvest()` resolved from the site itself (`Options.section`);
    then the URL's shape (`docs_scope`). One function, because the llms
    probe, the manifest lookup, the sitemap filter, the llms narrowing and the
    crawl each used to derive it from the URL on their own, and a boundary
    four stages agree on and one does not is not a boundary.
    """
    scope = getattr(opts, "scope", "section") if opts is not None else "section"
    if scope == "host":
        return "/"
    if scope not in ("", "section", None):
        return scope if scope.endswith("/") else scope + "/"
    section = getattr(opts, "section", "") if opts is not None else ""
    return section or docs_scope(url)


#: Where a documentation site keeps its own table of contents. Order does not
#: matter: every match is measured and the one holding the most links wins.
_SIDEBARS = ("aside", "nav", "[role=navigation]", "[class*=sidebar]",
             "[class*=Sidebar]", "[class*=side-bar]", "[class*=sidenav]",
             "[class*=SideNav]", "[class*=side-nav]", "[id*=sidebar]",
             "[class*=docs-nav]", "[class*=book-nav]", "[class*=menu]")
#: ...and what looks like one but is the site's, not the manual's: the top bar
#: links to other products, a footer to the company, an in-page contents list
#: to anchors, a pager to two neighbours, and the mobile drawer that repeats
#: the top bar (`go.dev`'s is an `<aside>` outside its `<header>`).
_NOT_SIDEBAR = re.compile(
    r"navbar|topnav|top-nav|topbar|masthead|header|footer|breadcrumb|pagination|"
    r"pager|page-nav|toc|table-of-contents|on-this-page|tabs?-nav|language|locale|"
    r"version|drawer|mobile|hamburger|offcanvas|off-canvas|global-?nav|site-?nav|"
    r"primary-?nav|main-?nav|mega", re.I)
#: What a manual calls its own table of contents. Outweighs every mark above:
#: HashiCorp's sidebar sits in a `mobile-menu-container ... sidebarContainer`.
_IS_SIDEBAR = re.compile(
    r"sidebar|side-bar|sidenav|side-nav|docs-?nav|doc-nav|book-nav|toctree|"
    r"nav-?tree|menu__list|docs-navigation", re.I)


def _marks(el) -> str:
    return " ".join([el.get("id") or ""] + list(el.get("class") or []))


def _sidebar_standing(el) -> int:
    """1 for a manual's own sidebar, -1 for the site's navigation, 0 for
    neither -- judged on the element and the few containers around it."""
    node, depth = el, 0
    negative = False
    while node is not None and depth < 8 and getattr(node, "name", None):
        marks = _marks(node) if hasattr(node, "get") else ""
        if marks.strip():
            if _IS_SIDEBAR.search(marks):
                return 1
            if _NOT_SIDEBAR.search(marks):
                negative = True
        node, depth = node.parent, depth + 1
    return -1 if negative else 0
#: A sidebar with fewer links than this is a pager or a stub, and says nothing
#: about where the manual's edges are.
NAV_MIN_LINKS = 8
#: The share of sidebar links a section has to hold to be the manual's.
NAV_COVERAGE = 0.8


def _sidebar_links(soup, base: str) -> list[str]:
    """The same-host page links of the page's table of contents.

    The one that lists the page itself, when one does -- a manual's sidebar
    marks where the reader is, a site's menu does not -- and the largest
    among those; the largest of all otherwise (Next.js's sidebar links
    `/docs/app/...` from `/docs` and never `/docs` itself).
    """
    host = (urlparse(base).hostname or "").lower()
    here = _normalize(base)
    ranked: list[tuple[tuple, list[str]]] = []
    try:
        candidates = soup.select(", ".join(_SIDEBARS))
    except Exception:                               # noqa: BLE001 -- a selector bs4 rejects
        return []
    for el in candidates:
        standing = _sidebar_standing(el)
        if standing < 0:
            continue
        if standing == 0 and (el.find_parent(["header", "footer"]) is not None
                              or el.name in ("header", "footer")):
            continue
        links = []
        for a in el.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full = urldefrag(urljoin(base, href))[0]
            parsed = urlparse(full)
            if (parsed.hostname or "").lower() != host or parsed.path.lower().endswith(SKIP_EXT):
                continue
            links.append(full)
        links = list(dict.fromkeys(links))
        if len(links) < NAV_MIN_LINKS:
            continue
        lists_here = any(_normalize(l) == here for l in links)
        ranked.append(((lists_here, standing, len(links)), links))
    if not ranked:
        return []
    return max(ranked, key=lambda row: row[0])[1]


def _nav_scope(soup, base: str) -> str:
    """The section the page's own navigation covers, or "" when it does not say.

    A URL's shape is a guess at where a manual's edges are; its sidebar is
    the manual stating them. `angular.dev/overview` looks like a folder called
    `/overview/`, and the harvest kept to it stored one page, while the
    sidebar on that page covers all of `angular.dev`. `firebase.google.com/
    docs/firestore` looks like part of `/docs/`, and a harvest of Firestore
    was headed for 13,704 pages of every Firebase product, while the sidebar
    covers `/docs/firestore/`. Terraform's covers `/terraform/`, not the
    `/terraform/docs/` its URL suggests (field test, 2026-09-24).

    The answer is the deepest directory holding `NAV_COVERAGE` of the
    sidebar's links, widened if need be to include the page itself.
    """
    return _covering_section(_sidebar_links(soup, base), base)


def _under(links: list[str], prefix: str) -> list[str]:
    """Those links at or below `prefix` (`/docs` counts as inside `/docs/`)."""
    return [l for l in links
            if ((urlparse(l).path or "/").rstrip("/") + "/").startswith(prefix)]


def _covering_section(links: list[str], base: str) -> str:
    """The deepest directory holding `NAV_COVERAGE` of `links` and `base`."""
    if len(links) < NAV_MIN_LINKS:
        return ""
    counts: dict[str, int] = {}
    for link in links:
        path = urlparse(link).path or "/"
        parts = [p for p in path.split("/") if p]
        dirs = {"/"} | {"/" + "/".join(parts[:i]) + "/" for i in range(1, len(parts))}
        if path.endswith("/") and parts:
            dirs.add("/" + "/".join(parts) + "/")
        for d in dirs:
            counts[d] = counts.get(d, 0) + 1
    need = NAV_COVERAGE * len(links)
    covering = [d for d, n in counts.items() if n >= need]
    best = max(covering, key=lambda d: d.count("/")) if covering else "/"
    # The page has to be inside what it is harvested as. A section's own root
    # page -- `/docs` for `/docs/` -- is inside it: measuring the page by its
    # parent directory widened Next.js and Prisma to their whole sites.
    here = (urlparse(base).path or "/").rstrip("/") + "/"
    while not here.startswith(best) and best != "/":
        best = best[: best[:-1].rfind("/") + 1] or "/"
    return best


def _resolve_section(url: str, html: str) -> str:
    """The section a harvest starting at `url` should keep to.

    The sidebar's answer (`_nav_scope`) where it gives one, the URL's shape
    otherwise -- with one exception: a URL that names a release keeps it. A
    sidebar spanning `/docs/` does not license mixing `/docs/v3/` with every
    other release the site files beside it.
    """
    shaped = docs_scope(url)
    if not html:
        return shaped
    try:
        soup = _soup(html)
    except Exception:                               # noqa: BLE001 -- no evidence, keep the guess
        return shaped
    base = _document_base(soup, url)
    links = _sidebar_links(soup, base)
    nav = _covering_section(links, base)
    if not nav or nav == shaped:
        return shaped
    if shaped.startswith(nav):
        # Wider than the URL suggests. Right when the URL's shape misled --
        # `/overview` is a page, not a folder, and nothing lives under it --
        # and wrong when the URL named a real section: `docs.stripe.com/
        # payments` has its own subtree in the sidebar, and widening it to
        # the whole of Stripe answered a question nobody asked. Nor across a
        # release the URL names.
        if len(_under(links, shaped)) >= NAV_MIN_LINKS:
            return shaped
        if _release_segment(url) is not None:
            return shaped
    return nav


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


def _dir_of(url: str) -> str:
    """The directory a URL's file sits in: `/docs/llms.txt` -> `/docs/`."""
    path = urlparse(url).path or "/"
    return path[: path.rfind("/") + 1] or "/"


def _broader_than_request(det: Detection, url: str, opts: "Options | None" = None) -> bool:
    """Does this published file describe more than the section asked for?

    The site root always does -- see `_at_the_site_root`, which this extends.
    Since detection probes nearest first (`_llms_probes`), a file can also be
    found part-way up: asked for `/docs/orm/`, it may be `/docs/llms.txt`,
    which describes all of `/docs/` and has to be narrowed exactly as a root
    file would be. A file in the section's own directory, or below it, is
    the one that was asked for and is taken as published.
    """
    if _at_the_site_root(det):
        return True
    where = _dir_of(det.url)
    prefix = _scope_for(url, opts)
    return prefix != where and prefix.startswith(where)


def _fetch_at(fetcher, url: str, render: bool = False) -> tuple[str, str]:
    """`(html, landed)` from any fetcher.

    A real `Fetcher` says where the page was finally served from. A fetcher
    that only knows `html()` -- every fake in the test suite -- is taken at
    its word that it was `url`, which is what the crawl assumed of everything
    until 2026-09-19.
    """
    at = getattr(fetcher, "render_at" if render else "html_at", None)
    if at is not None:
        html, landed = at(url)
        return html, (landed or url)
    return (fetcher.render(url) if render else fetcher.html(url)), url


def _document_base(soup, landed: str) -> str:
    """What relative links on this page resolve against.

    A `<base href>` when the page declares one, else the URL the page was
    served from -- the rule a browser applies, and the only one under which
    `quickstart/` on click.palletsprojects.com/en/stable/ is the quickstart.
    """
    base = soup.find("base", href=True) if soup is not None else None
    href = (base.get("href") or "").strip() if base else ""
    return urljoin(landed, href) if href else landed


def _normalize(url: str) -> str:
    """Drop the fragment and a trailing slash so `/intro` and `/intro/` are
    one page, not two fetches of the same content.

    A key, never an address: see `_fetchable`."""
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        url = url.replace(path, path[:-1], 1)
    return url


def _fetchable(url: str) -> str:
    """The URL to ask for: as the site wrote it, minus the fragment.

    The crawl used to fetch `_normalize`'s spelling, trailing slash removed,
    and a server is under no obligation to answer `/book` the way it answers
    `/book/`. `doc.rust-lang.org` answers `/book/` with the Rust book and
    `/book` with a 302 to `/stable/book/` -- outside the scope the crawl was
    keeping to -- so every chapter link fell out of scope and a harvest of the
    book stored its title page (field test, 2026-09-24). Where a server does
    redirect `/x` to `/x/`, asking for the slashed spelling saves that hop on
    every page. Pages are still *keyed* by `_normalize`, so the two spellings
    remain one page.
    """
    return urldefrag(url)[0]


#: A site's machine-readable files: read by the ladder when it wants them,
#: never harvested as pages. Angular's sitemap lists its own `llms.txt`, and an
#: uncapped harvest counted it as a page it could not read (2026-09-24).
_MACHINE_FILES = ("llms.txt", "llms-full.txt", "llms-medium.txt", "llms-small.txt",
                  "robots.txt", "sitemap.xml", "sitemap.txt", "sitemap_index.xml")


def _crawlable(link: str, host: str, prefix: str = "/") -> bool:
    p = urlparse(link)
    if p.scheme not in ("http", "https"):
        return False
    if (p.hostname or "").lower() != host:
        return False
    if p.path.lower().endswith(SKIP_EXT):
        return False
    if p.path.lower().rsplit("/", 1)[-1] in _MACHINE_FILES:
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

    def __init__(self, start: str | None, seeds: list[str] | None = None) -> None:
        self._heap: list[tuple[tuple, str]] = []
        #: Keyed by `_page_key` -- `/x`, `/x/` and `/x.md` are one page; what is queued is the URL as the site wrote
        #: it, because that is what gets fetched (see `_fetchable`).
        self._seen: set[str] = set()
        self._yield: dict[str, float] = {}
        #: URLs the site itself listed -- a sitemap, a manifest -- in the
        #: order it listed them. They go first, in that order: they are the
        #: statement of what exists, and what a link merely points at is
        #: found by fetching them.
        self._order: dict[str, int] = {}
        for i, url in enumerate(seeds or []):
            self._order.setdefault(_page_key(url), i)
            self.append(url)
        if start:
            self.append(start)

    def _rank(self, url: str) -> tuple:
        seeded = self._order.get(_page_key(url))
        if seeded is not None:
            return (-1, seeded, 0, 0, url)
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
        key = _page_key(url)
        if key in self._seen:
            return
        self._seen.add(key)
        heapq.heappush(self._heap, (self._rank(url), url))

    def popleft(self) -> str:
        return heapq.heappop(self._heap)[1]

    def __contains__(self, url: str) -> bool:
        return _page_key(url) in self._seen

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


#: A refused page whose text is at least this much link text is a table of
#: contents (`_crawl_html`), not documentation that could not be read.
INDEX_LINK_SHARE = 0.5

#: A Markdown link's target: `[text](target)`, `[text](<target>)`, with or
#: without a title after it.
_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^)]*[\"'])?\s*\)")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


#: A link to this page whose text is only a permalink mark, as it survives in
#: Markdown a generator exported: `[​](#highlights "Direct link to Highlights")`
#: -- 2,665 of them in react-native's `llms-full.txt` (field test, 2026-09-24).
_MD_PERMALINK = re.compile(r"\[(?:​|¶|§|#|🔗|\s)*\]\(#[^)\s]*(?:\s+\"[^\n]*?\")?\)")


def _tidy_markdown(text: str, base: str) -> str:
    """Published Markdown, with links that still point somewhere once stored.

    A dump or a page's Markdown twin is kept as the publisher wrote it, save
    two things that only work on the site it came from: permalink marks, and
    relative links -- `[Routing](/docs/app/routing)` in Next.js's dump, 4,224
    of them, which lead nowhere from a knowledge base. Code blocks are left
    exactly as they are.
    """
    if not text:
        return text
    # A permalink mark is never code, so it goes wherever it is.
    text = _MD_PERMALINK.sub("", text)
    if not base:
        return text
    fences = _fence_spans(text)

    def absolute(m: re.Match) -> str:
        target = m.group(2)
        if _inside(m.start(), fences) or target.startswith(
                ("#", "http://", "https://", "mailto:", "tel:", "data:", "//")):
            return m.group(0)
        return f"{m.group(1)}({urljoin(base, target)}{m.group(3) or ''})"

    return _MD_TARGET.sub(absolute, text)


#: `](target)` or `](target "title")` -- where a Markdown link points.
_MD_TARGET = re.compile(r"(\]\s*)\(\s*<?([^)\s>]+)>?(\s+\"[^\"]*\")?\s*\)")


def _is_html_document(text: str) -> bool:
    head = (text or "").lstrip()[:600].lower()
    return head.startswith(("<!doctype html", "<html")) or "<html" in head[:200]


def _markdown_page(text: str, url: str, fallback_title: str = "") -> tuple[str, str]:
    """A Markdown twin as a stored page: its title, and its text.

    Front matter is the page's metadata, not its prose; its `title` is kept
    as the page's title and the block itself is dropped."""
    body = text.lstrip("﻿")
    title = ""
    front = _FRONTMATTER.match(body)
    if front:
        m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", front.group(1), re.M)
        title = m.group(1).strip() if m else ""
        body = body[front.end():]
    if not title:
        m = re.search(r"^#\s+(.+?)\s*#*\s*$", body, re.M)
        title = m.group(1).strip() if m else ""
    title = title or fallback_title or \
        urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0] or url
    return title[:200], _meta_header(url, "markdown") + _tidy_markdown(body.strip(), url)


def _lives_at(soup, url: str, landed: str, base: str) -> bool:
    """Is `base` where this page's documentation actually is?

    Yes when the page was redirected there. When only a `<base href>` says
    so, yes if the page's links agree -- Click's `/en/stable/` declares
    `/en/8.5.x/` and its relative links all resolve there. Apple's app shell
    declares `/tutorials/` on every documentation page and links nowhere
    under it, and taking its word moved a harvest of SwiftUI into the
    tutorials (field test, 2026-09-24).
    """
    if _page_key(landed) != _page_key(url):
        return True
    host = (urlparse(base).hostname or "").lower()
    there = docs_scope(base)
    links = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)
             if not a["href"].startswith(("#", "mailto:", "javascript:"))]
    links = [l for l in links if (urlparse(l).hostname or "").lower() == host]
    if len(links) < 3:
        return False
    return sum(1 for l in links if _crawlable(l, host, there)) >= len(links) / 2


def _markdown_alternate(soup, base: str) -> str:
    """The Markdown copy a page says it has, or "".

    `<link rel="alternate" type="text/markdown" href="…">` is how a growing
    number of documentation platforms publish each page's source beside it
    -- Apple's, Cloudflare's, Mintlify's and Fern's among them -- and it is
    the page without the JavaScript that renders it or the chrome around it.
    """
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower() if isinstance(link.get("rel"), list) \
            else str(link.get("rel") or "").lower()
        kind = (link.get("type") or "").lower()
        if "alternate" in rel and "markdown" in kind:
            return urljoin(base, link["href"].strip())
    return ""


def _crawl_html(start: str, fetcher: Fetcher, opts: Options,
                stats: dict | None = None, sink=None, *,
                seeds: list[str] | None = None, admit=None,
                statement: str = "", known: set[str] | None = None) -> list[Doc]:
    """Fetch and extract every page of a documentation section.

    With `seeds`, this is the acquisition loop for a site that stated what
    exists -- its sitemap, its generator's manifest -- and `statement` names
    that source. The seeds go first, in the site's order; links found on
    them are followed too, because a sitemap is a hint and not always a
    complete one; and coverage is measured against everything known to
    exist, which a plain crawl cannot do. `start` is then fetched only if it
    is itself a seed or linked from one.

    `admit`, when given, is the caller's rule for which in-scope URLs belong
    to this harvest at all -- one language, one release -- applied alike to
    seeds and to every link found. `known` is pages this harvest already has
    from elsewhere, as `_page_key`s; a link to one of them is not followed.
    """
    seen: set[str] = set()
    out: list[Doc] = []
    #: Reached, fetched, and nothing on them read like documentation. Kept
    #: apart from pages that simply failed to fetch, because "we could not read
    #: this" and "this was not there" are different claims about coverage.
    unreadable: list[str] = []
    #: Asked for and refused with a 429: the site rate-limiting this crawl.
    #: Not `unreadable` -- nothing was read -- and after `RATE_LIMIT_STOP` of
    #: them in a row the crawl stops asking, because a crawler that answers a
    #: site's "slow down" with thirty more requests is the crawler that gets
    #: the site's IP block, and the corpus it stores in the meantime is a
    #: handful of pages labelled as though the rest did not exist.
    refused: list[str] = []
    refused_in_a_row = 0
    rate_limited = False
    #: Asked for and answered 404 or 410: listed or linked, and not there.
    #: Reported, never counted as a page this copy lacks.
    dead: list[str] = []
    #: Reached, and nothing but a table of contents: followed, not stored,
    #: not a gap.
    index_pages: list[str] = []
    #: Out-of-scope link evidence, summed across the whole crawl. Costs no
    #: requests: it reads soup that link discovery has already parsed.
    sites = Federation()
    #: What this crawl has seen, and the hypothesis it is currently working to.
    #: The plan starts empty and is re-derived every twelfth page from the last
    #: twelve — the entry page no longer decides how the rest is read.
    ledger = Ledger()
    plan = Plan()
    last_revised = 0
    admit = admit or (lambda _url: True)
    if known:
        allowed = admit
        admit = lambda link: _page_key(link) not in known and allowed(link)  # noqa: E731
    seeded = [_fetchable(u) for u in (seeds or [])]
    statement_keys = {_page_key(u) for u in seeded}
    queue = _Frontier(None if seeded else _fetchable(start), seeds=seeded)
    host = (urlparse(start).hostname or "").lower()
    #: Whether the boundary is still ours to move: a derived section, not a
    #: prefix the caller typed, and not one a site's own list already fixed.
    #: See the first-page check below.
    derived_scope = opts.scope in ("", "section", None) and not seeded

    # "Same host" is not the right boundary for a docs site — see docs_scope.
    prefix = _scope_for(start, opts)
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
    first_page = True

    def _fetch(link: str, render: bool) -> tuple[str, str]:
        # A Markdown file is never rendered: a browser would wrap it in a
        # `<pre>` and hand back a page about a text file.
        render = render and not llmsfinder.is_markdown_link(link)
        pace.wait(link)
        with pace.host(link):
            html, landed = _fetch_at(fetcher, link, render)
        # A client-side redirect stub is followed here, as `_extract_page`
        # follows it for a single page: `docs.pytorch.org/docs/stable/…` is a
        # 1,400-byte `location.replace` to the release it aliases, on every
        # page, and extraction finds nothing in the stub itself.
        hops = {_page_key(link), _page_key(landed)}
        for _ in range(REDIRECT_HOPS):
            target = _redirect_target(html, landed)
            if not target or _page_key(target) in hops:
                break
            hops.add(_page_key(target))
            pace.wait(target)
            with pace.host(target):
                html, landed = _fetch_at(fetcher, target, render)
        return html, landed

    def _fetch_twin(soup, base: str) -> tuple[str, str] | None:
        """The page's declared Markdown copy, fetched, or None."""
        alt = _markdown_alternate(soup, base)
        if not alt or (urlparse(alt).hostname or "").lower() != (urlparse(base).hostname or "").lower():
            return None
        try:
            pace.wait(alt)
            with pace.host(alt):
                text = fetcher.text(alt)
        except Exception:                           # noqa: BLE001 -- no copy is no copy
            return None
        if _is_html_document(text) or len(text.strip()) < 40:
            return None
        seen.add(_page_key(alt))
        return alt, text

    def _fill() -> None:
        """Top the window up, in queue order.

        Dispatch order is queue order and results are consumed in dispatch
        order, so a crawl returns its pages in the sequence it would have
        sequentially. One real difference, worth stating rather than leaving to
        be discovered: a plan revision rescores what is still *queued*, and the
        window has already been taken off the queue, so a revision reaches the
        crawl up to `workers` pages later than it otherwise would.
        """
        while (pool is not None and not plan.render and len(window) < workers
               and queue and (limit is None or len(out) + len(window) < limit)):
            # Not once the plan renders: Playwright's sync API belongs to the
            # thread that started it, and a rendered page fetched from a
            # worker thread fails every time. Rendered pages are fetched one
            # at a time on this thread, below.
            nxt = queue.popleft()
            if _page_key(nxt) in seen:
                continue
            seen.add(_page_key(nxt))
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
                if _page_key(url) in seen:
                    continue
                seen.add(_page_key(url))
            else:
                break

            report: dict = {}
            html = ""
            needed_help = False
            # Links this page leads to, queued once the page has been judged:
            # a harvest kept to a topic follows only the pages it kept.
            found: list[str] = []

            def release() -> None:
                for link in found:
                    if _page_key(link) not in seen and link not in queue:
                        queue.append(link)
                found.clear()

            try:
                if pending is not None:
                    html, landed = pending.result()
                else:
                    html, landed = _fetch(url, plan.render)
                # Where the page was served from is a page too: a redirect
                # that lands here again from another link is not a new page.
                seen.add(_page_key(landed))

                if llmsfinder.is_markdown_link(landed) and not _is_html_document(html):
                    # A page's Markdown twin, reached by a link or listed by the
                    # site. Its words are the page; its links are Markdown.
                    title, doc = _markdown_page(html, url)
                    first_page = False
                    fences = _fence_spans(html)
                    for m in _MD_LINK.finditer(html):
                        if _inside(m.start(), fences):
                            continue        # an example in a code block, not a link
                        link = _fetchable(urljoin(landed, m.group(1)))
                        if (_page_key(link) not in seen and link not in queue
                                and _crawlable(link, host, prefix) and admit(link)):
                            found.append(link)
                    refused_in_a_row = 0
                    if sink is not None:
                        if not sink.add(title, url, doc):
                            if not getattr(sink, "off_topic", False):
                                release()
                            continue
                        out.append(Doc(url, title, ""))
                    else:
                        out.append(Doc(url, title, doc))
                    release()
                    _log(opts, f"  [{len(out)}] {url}")
                    continue

                # Parse once: link discovery needs the nav _html_to_md strips out.
                soup = _soup(html)
                # Same soup, same reason. The sidebar is where a project states its
                # own structure, so out-of-scope evidence is collected here, before
                # extraction strips the chrome away.
                sites.record_page(url, soup)
                # A relative link means what a browser would make of it: it
                # resolves against the page's declared base or the URL it was
                # served from, never against the normalised spelling this crawl
                # keys pages by. Under `/en/stable`, `quickstart` is `/en/quickstart`.
                base = _document_base(soup, landed)

                if first_page and derived_scope and not _crawlable(base, host, prefix) \
                        and _lives_at(soup, url, landed, base):
                    # The start page lives somewhere other than the URL it was
                    # asked for at -- a redirect, or a declared base -- and the
                    # section derived from the request is not where its
                    # documentation is. `laravel.com/docs` answers with a 301
                    # to `/framework/docs`; a crawl kept to `/docs/` stored the
                    # landing page and found every link on it out of scope
                    # (field test, 2026-09-24).
                    host = (urlparse(base).hostname or host).lower()
                    prefix = docs_scope(base)
                    _log(opts, f"  {url} lives at {base}; crawling within {prefix} there")
                first_page = False
                for a in soup.find_all("a", href=True):
                    link = _fetchable(urljoin(base, a["href"]))
                    if (_page_key(link) not in seen and link not in queue
                            and _crawlable(link, host, prefix) and admit(link)):
                        found.append(link)

                try:
                    title, doc = _html_to_md(html, url, soup=soup, plan=plan,
                                             report=report)
                except ForgeError:
                    # Nothing readable in the HTML as served. The page's own
                    # Markdown copy first -- no browser, no chrome -- then one
                    # rendered retry if the page ships the scripts that would
                    # draw it.
                    twin = _fetch_twin(soup, base)
                    if twin is not None:
                        title, doc = _markdown_page(twin[1], url,
                                                    soup.title.get_text(strip=True)
                                                    if soup.title else "")
                        _log(opts, f"  {url} has no readable HTML; took its Markdown "
                                   f"copy at {twin[0]}")
                        # The copy links where the page does, and it is the
                        # only place those links are readable.
                        fences = _fence_spans(twin[1])
                        for m in _MD_LINK.finditer(twin[1]):
                            if _inside(m.start(), fences):
                                continue
                            link = _fetchable(urljoin(twin[0], m.group(1)))
                            if (_page_key(link) not in seen and link not in queue
                                    and _crawlable(link, host, prefix) and admit(link)):
                                found.append(link)
                    elif opts.js or plan.render or not _wants_render(html):
                        raise
                    else:
                        _log(opts, f"  {url} renders client-side; retrying rendered")
                        rendered = fetcher.render(url)
                        needed_help = True
                        rsoup = _soup(rendered)
                        for a in rsoup.find_all("a", href=True):
                            link = _fetchable(urljoin(base, a["href"]))
                            if (_page_key(link) not in seen and link not in queue
                                    and _crawlable(link, host, prefix) and admit(link)):
                                found.append(link)
                        title, doc = _html_to_md(rendered, url, soup=rsoup, plan=plan,
                                                 report=report)
            except ForgeError as e:
                if getattr(e, "status", None) == 429:
                    refused.append(url)
                    refused_in_a_row += 1
                    _log(opts, f"  refused {url}: {e}")
                    if refused_in_a_row >= RATE_LIMIT_STOP:
                        rate_limited = True
                        _log(opts, f"  {RATE_LIMIT_STOP} refusals in a row: the site is "
                                   f"rate-limiting this crawl; stopping rather than "
                                   f"keep asking")
                        break
                    continue
                if getattr(e, "status", None) in (404, 410):
                    # Not there. A dead entry in the site's own list or a dead
                    # link, and not a page this copy is missing: FastAPI's
                    # sitemap names twelve pages it has since removed, and
                    # counting them made a harvest of every page that exists
                    # INCOMPLETE (uncapped field test, 2026-09-24).
                    dead.append(url)
                    _log(opts, f"  not on the site {url}: {e}")
                    continue
                if report:
                    # A refused page is still evidence — it is what tells the plan
                    # that a template is not being read well.
                    report["shell"] = _looks_like_shell(html)
                    ledger.record(Observation(**report))
                if report.get("link_text_ratio", 0) >= INDEX_LINK_SHARE:
                    # A table of contents -- Sphinx's module index, a category
                    # page of cards -- refused because it is links, which is
                    # what it is for. Not documentation, so not a gap.
                    index_pages.append(url)
                    _log(opts, f"  index page {url}: its links are followed, it is not stored")
                else:
                    unreadable.append(url)
                    _log(opts, f"  unextractable {url}: {e}")
                # A page of nothing but links is still a way to the pages it
                # lists: an index that reads as navigation is exactly that.
                release()
                continue
            except Exception as e:  # one broken page must not end the crawl
                # Counted, not only logged: a page that broke the parser is a
                # page this harvest does not have.
                unreadable.append(url)
                _log(opts, f"  skip {url}: {type(e).__name__}: {e}")
                continue

            refused_in_a_row = 0
            # A page that had to be rendered to be read counts as a shell for
            # the plan (R4), whatever the static test said of it.
            report["shell"] = _looks_like_shell(html) or needed_help
            ledger.record(Observation(**report))

            # Decision point 4. A page that answers 200 while rendering "Page
            # not found" is invisible to every status check, and it is stored
            # as documentation. Only pages that are both short and error-shaped
            # are worth asking about, so this costs at most one call per
            # template however many such pages a site has.
            if _error_shaped(title, doc) and _is_error_page(title, doc, url):
                unreadable.append(url)
                _log(opts, f"  {url} answers 200 but renders an error")
                release()
                continue

            if sink is not None:
                # Durable before the next page is *stored* (Invariant 16, as
                # amended for Phase 3: up to `workers` pages are now in flight
                # ahead of this one, so an interruption can lose that much
                # fetching — never anything already stored). The body is
                # released rather than carried to the end of the harvest, and a
                # page the store refuses costs that page and nothing else.
                if not sink.add(title, url, doc):
                    # Off the topic this harvest keeps to: not stored, and not
                    # a way in either. Refused by the store for another reason
                    # (too large): its links still lead to real pages.
                    if not getattr(sink, "off_topic", False):
                        release()
                    continue
                out.append(Doc(url, title, ""))
            else:
                out.append(Doc(url, title, doc))
            release()
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
        # `truncated` is the page cap's word and keeps that meaning: a crawl
        # the site stopped is incomplete for a reason of its own, below.
        stats["truncated"] = bool(left) and not rate_limited
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
        stats["frontier_drained"] = not left and not rate_limited
        stats["whole"] = False if (left or rate_limited) else None
        if rate_limited:
            stats["rate_limited"] = True
            stats["reason"] = (
                f"the site answered HTTP 429 (rate limited) to {len(refused)} "
                f"request(s) and the crawl stopped after {len(out)} page(s) rather "
                f"than keep asking; {left + len(refused)} discovered page(s) were "
                f"not fetched. This is the site's pace, not its size: harvest "
                f"again later")
        elif not left and not seeded:
            stats.setdefault("reason", (
                f"a crawl reached every page linked under {prefix} and stopped. "
                f"Nothing stated how many pages exist, so a page nothing links "
                f"to would not have been seen"))
        if seeded:
            # The site said what exists, so coverage is a measurement: every
            # page it listed, and every page those pages link to inside the
            # section, either stored or accounted for.
            known = len(out) + len(unreadable) + len(refused) + left
            found_by_links = max(0, known - len(statement_keys))
            stats["discovered"] = known
            stats["index"] = statement or "sitemap"
            stats["listed"] = len(statement_keys)
            stats["found_by_links"] = found_by_links
            if not left and not rate_limited:
                stats["whole"] = not unreadable and not refused
            if stats["whole"] is False and not rate_limited:
                stats["reason"] = (
                    f"stored {len(out)} of the {known} pages the "
                    f"{statement or 'sitemap'} lists"
                    + (f" or links to ({found_by_links} found only by following links)"
                       if found_by_links else "")
                    + (f", {len(unreadable)} of them unextractable" if unreadable else "")
                    + (f", {len(refused)} of them refused with HTTP 429" if refused else ""))
        stats.setdefault("discovered", len(out) + len(queue) + len(refused))
        if unreadable:
            stats["unextractable"] = unreadable
        if refused:
            stats["refused"] = refused
        if dead:
            stats["dead"] = list(stats.get("dead") or []) + dead
        if index_pages:
            stats["index_pages"] = list(stats.get("index_pages") or []) + index_pages
        if getattr(fetcher, "throttled", 0):
            stats["throttled"] = fetcher.throttled
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
        if rate_limited or (refused and not unreadable):
            raise ForgeError(
                f"The site answered HTTP 429 (rate limited) to every request and "
                f"nothing was fetched from {start}. Its pace, not its size: "
                f"harvest again later.")
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


def _admission(url: str, prefix: str, seeds: list[str] | None = None):
    """Which in-scope links belong to this harvest, as a predicate.

    The sitemap path filters what the site lists -- one language
    (`_prefer_default_locale`), one release (`_prefer_current_release`), no
    blog posts on a marketing host (`_focus_on_docs`). A crawl that follows
    links needs the same rule for what it *finds*, or the language switcher
    and the version picker on every page walk it straight back into the
    translations and releases the list was narrowed away from.

    The harvest's own language and release are read off what it starts from:
    the seeds when the site listed them, the start URL otherwise.
    """
    pool = list(seeds or []) or [url]
    locales = {_locale_of(u) for u in pool}
    if locales <= {"", "en"}:
        locales = {"", "en"}

    # The release the harvest is filed under, if most of its pages name one;
    # if most name none, the current documentation is the unversioned pages.
    # By majority, not unanimity: docusaurus.io lists a handful of pages whose
    # paths merely contain a release-like word (a migration guide filed under
    # `v3`), that switched the unversioned rule off, and an uncapped harvest
    # followed the version picker into 311 pages of `/docs/3.3.2/` and
    # `/docs/next/` (field test, 2026-09-24).
    found = [s for s in (_release_segment(u) for u in pool) if s is not None]
    release = None
    unversioned_below = None
    if found and len(found) >= len(pool) / 2:
        depth = min(d for d, _ in found)
        at_depth = {k for d, k in found if d == depth}
        if len(at_depth) == 1:
            release = (depth, at_depth.pop())
    elif seeds:
        unversioned_below = len([p for p in prefix.split("/") if p]) + 1

    articles_out = prefix in ("", "/") and not is_documentation_host(url)

    def admit(link: str) -> bool:
        if _locale_of(link) not in locales:
            return False
        seg = _release_segment(link)
        if seg is not None:
            if release is not None and seg[0] == release[0] and seg[1] != release[1]:
                return False
            if unversioned_below is not None and seg[0] < unversioned_below:
                # The current docs are filed under no release, and this link
                # is a release's copy of them (`/docs/1.8/` beside `/docs/`).
                return False
        if articles_out and looks_like_article(link):
            return False
        return True

    return admit


def _xml_soup(text: str):
    """Prefer a real XML parser, but degrade instead of exploding when lxml
    is not installed — the README used to call it optional."""
    for parser in ("lxml-xml", "xml", "html.parser"):
        try:
            return _soup(text, parser)
        except Exception:
            continue
    raise ForgeError("Could not parse sitemap XML")


def _sitemap_links(text: str, fetcher: Fetcher, opts: Options, depth: int = 0,
                   keep=None, hint: str = "", budget=None) -> list[str]:
    """Every page URL a sitemap lists, following an index down.

    `keep`, when given, says which URLs the caller will actually use; a page
    cap is then counted in those, not in whatever the first children listed.
    `hint` is the path the caller is interested in, used only to decide which
    children of an index to open first.
    """
    stripped = (text or "").lstrip()
    if stripped and not stripped.startswith("<"):
        # The sitemaps protocol allows a plain list, one URL per line, and
        # `doc.rust-lang.org/robots.txt` points at exactly that. Parsed as XML
        # it had no `<loc>` in it, and the Rust book fell back to a crawl.
        return [line.strip() for line in stripped.splitlines()
                if line.strip().startswith(("http://", "https://"))]

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
        # Children that name the section being harvested go first. Order
        # only: every child is still read unless a page cap is already met.
        words = [w.lower() for w in hint.split("/") if w and len(w) > 2]
        if words:
            locs.sort(key=lambda sm: not any(w in sm.lower() for w in words))
        for sm in locs:
            if cap is not None:
                wanted = [u for u in nested if keep is None or keep(u)]
                if len(wanted) >= cap:
                    break
            if budget is not None and not budget.spend():
                break
            try:
                nested += _sitemap_links(_sitemap_body(sm, fetcher), fetcher, opts,
                                         depth + 1, keep=keep, hint=hint, budget=budget)
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


class _TopicSink:
    """Stores only what belongs to the harvest's topic, and keeps the count.

    Wraps the real sink, or collects the documents itself when there is none
    (a library caller), so a topic filters every acquisition path alike -- a
    published file, a listed page, a crawled one -- at the one place every page
    passes through. `off_topic` says why the last page was not stored, which
    the crawl reads to decide whether to follow its links.
    """

    def __init__(self, inner, selector) -> None:
        self.inner = inner
        self.selector = selector
        self.off_topic = False
        self.stored = 0
        self.docs: list[Doc] = []

    def add(self, title: str, url: str, body: str) -> bool:
        self.off_topic = False
        if not self.selector.wants(title, url, body):
            self.off_topic = True
            return False
        if self.inner is None:
            self.docs.append(Doc(url, title, body))
            self.stored += 1
            return True
        kept = self.inner.add(title, url, body)
        if kept:
            self.stored += 1
        return kept


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

    With `opts.topic`, the whole of the part of it that is about the topic
    (`topics.Selector`): every page judged, the ones that belong stored, and
    what was left out accounted for in `stats["topic"]`.

    Returns the documents and the name of the strategy that produced them.
    """
    opts = opts or Options()
    selector = topics.Selector(opts.topic) if (opts.topic or "").strip() else None
    if selector is None or not selector.active:
        return _harvest(url, opts, fetcher, stats, sink)
    topical = _TopicSink(sink, selector)
    _log(opts, f"  keeping to the topic {opts.topic!r}: "
               f"{', '.join(selector.vocabulary[:14])}"
               + (" ..." if len(selector.vocabulary) > 14 else ""))
    docs, strategy = _harvest(url, opts, fetcher, stats, topical)
    for skipped in (stats or {}).pop("topic_skipped", []) if stats is not None else []:
        selector.left.append((skipped, ""))
    if stats is not None:
        # Measured against what the topic selected: a page judged not to be
        # about it was never expected, and one that failed to arrive was.
        failures = (len(stats.get("unextractable") or []) + len(stats.get("refused") or [])
                    + len(stats.get("failed_urls") or []))
        stats["acquired"] = stats["fetched"] = topical.stored
        stats["expected"] = stats["discovered"] = (topical.stored + failures
                                                   + (stats.get("remaining") or 0))
        stats["topic"] = {"topic": opts.topic, "terms": selector.vocabulary,
                          "kept": topical.stored, "left": len(selector.left),
                          "left_by_section": selector.left_by_section()}
    if sink is None:
        docs = topical.docs
    return docs, strategy


def _harvest(url: str, opts: Options, fetcher: Fetcher | None,
             stats: dict | None, sink) -> tuple[list[Doc], str]:
    """`harvest`, for one topic or none -- see there."""
    own = fetcher is None
    fetcher = fetcher or Fetcher(opts)
    try:
        if opts.scope in ("", "section", None) and not opts.section:
            # Everything below is derived from this URL -- the section, the
            # llms files probed for, the sitemap filter -- so it has to be the
            # URL the documentation is actually at, not the one that redirects
            # there, and the section has to be the one the site's own
            # navigation draws rather than the one the URL's shape suggests.
            landed, page = _land(url, fetcher)
            if landed != url:
                _log(opts, f"  {url} redirects to {landed}; harvesting from there")
                url = landed
            if (opts.version or "").strip():
                # A release was asked for and the URL may name another: start
                # where the site files the one asked for, if it does, and draw
                # the section there.
                moved = _url_for_release(url, opts.version.strip(), fetcher, opts, stats)
                if moved != url:
                    url, page = moved, ""
            if page and _DOCSIFY.search(page):
                files = _docsify_pages(url, page, fetcher)
                if len(files) >= 2:
                    where = _dir_of(files[0])
                    _log(opts, f"  a docsify site: its sidebar lists {len(files)} Markdown "
                               f"page(s) under {where}; reading them directly")
                    md_opts = replace(opts, crawl=True, section=where)
                    return (_crawl_html(files[0], fetcher, md_opts, stats, sink=sink,
                                        seeds=files, statement="docsify sidebar",
                                        admit=llmsfinder.is_markdown_link), "docsify")
            section = _resolve_section(url, page)
            if section != docs_scope(url):
                _log(opts, f"  the site's own navigation puts this page in {section} "
                           f"(its URL alone suggests {docs_scope(url)})")
            opts = replace(opts, section=section)
        elif (opts.version or "").strip():
            # A release was asked for and the URL may name another: start
            # where the site files the one asked for, if it does.
            url = _url_for_release(url, opts.version.strip(), fetcher, opts, stats)
        det = detect_source(url, fetcher, scope=_scope_for(url, opts))
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
                    _log(opts, f"  scoping the site-wide llms.txt to {_scope_for(url, opts)} "
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
                        whole_text = det.body is not None and llmsfinder.classify_llms_shape(
                            det.body.strip(), det.url) not in ("index", "hybrid")
                        if det.url.lower().endswith(("llms-full.txt", "llms-medium.txt")):
                            strategy_used = "llms-full.txt"
                        elif whole_text:
                            # An `llms.txt` that is the documentation itself --
                            # svelte.dev's per-section files are -- cut into
                            # pages, not a list of links fetched one by one.
                            strategy_used = "llms.txt (full text)"
                        elif docs:
                            sample = docs[0].markdown if len(docs) == 1 else ""
                            shape = llmsfinder.classify_llms_shape(sample)
                            if len(docs) > 1 or shape == "index":
                                has_md = any(llmsfinder.is_markdown_link(d.url) for d in docs)
                                strategy_used = "llms.txt (md manifest)" if has_md else "llms.txt (html manifest)"
                            else:
                                strategy_used = "llms_txt"

                    kept = _drain(docs, sink)
                    if det.kind == "llms_txt":
                        covered = _published_pages(docs, stats)
                        if covered is None:
                            _log(opts, "  the published file names no pages, so it cannot "
                                       "be checked against the sitemap")
                        else:
                            extra, suffix = _complete_from_listing(
                                url, fetcher, opts, stats, sink, covered,
                                det.url.rsplit("/", 1)[-1], already=len(kept),
                                docs=docs)
                            kept += extra
                            strategy_used += suffix
                    return kept, strategy_used

        prefix = _scope_for(url, opts)

        scoped, index_kind = _listed_pages(url, fetcher, opts, stats)
        scoped = _topic_prefilter(scoped, opts, stats)
        if scoped:
            # One or two hits usually means the index does not really cover
            # the docs; a crawl will do better than a near-empty list.
            if len(scoped) >= 3:
                _log(opts, f"  harvesting {len(scoped)} pages from the {index_kind}, "
                           f"and whatever they link to in scope")
                if stats is not None:
                    # The denominator before the first page, for progress.
                    stats["discovered"] = len(scoped)
                    stats["index"] = index_kind
                crawl_opts = replace(opts, crawl=True)
                try:
                    out = _crawl_html(url, fetcher, crawl_opts, stats, sink=sink,
                                      seeds=scoped, statement=index_kind,
                                      admit=_admission(url, prefix, seeds=scoped))
                    return out, index_kind
                except ForgeError:
                    if stats is not None and stats.get("rate_limited"):
                        raise
                    # Every listed page failed to read. The list was the
                    # site's word; the crawl below is a second opinion.
                    _log(opts, f"  nothing the {index_kind} lists could be read; "
                               f"falling back to a crawl")
                    if stats is not None:
                        for key in ("index", "listed", "found_by_links", "whole",
                                    "reason", "unextractable", "discovered"):
                            stats.pop(key, None)

        _log(opts, "  harvesting by crawl")
        crawl_opts = replace(opts, crawl=True)
        return (_crawl_html(url, fetcher, crawl_opts, stats, sink=sink,
                            admit=_admission(url, prefix)), "crawl")
    finally:
        if own:
            fetcher.close()


_DOCSIFY = re.compile(r"window\.\$docsify\s*=")


def _docsify_pages(landed: str, html: str, fetcher) -> list[str]:
    """The Markdown files a docsify site is made of, from its own sidebar.

    Docsify ships one HTML page and fetches each route's Markdown in the
    browser: `#/quickstart` is `quickstart.md` beside `index.html`. A crawl
    sees one page whose every link is a fragment, and stored one page of
    docsify's own documentation (field test, 2026-09-24). The sidebar the
    site configures -- `_sidebar.md` -- lists the pages, and each is a file
    that can be read directly, with no browser at all.
    """
    base = landed.split("#", 1)[0]
    base = base if base.endswith("/") else base[: base.rfind("/") + 1]
    configured = re.search(r"basePath\s*:\s*['\"]([^'\"]+)['\"]", html)
    if configured and not configured.group(1).startswith(("http://", "https://")):
        base = urljoin(base, configured.group(1).rstrip("/") + "/")
    host = (urlparse(base).hostname or "").lower()
    # `alias: {'.*?/changelog': 'https://raw.githubusercontent.com/…/CHANGELOG.md'}`
    # -- a route the site serves from somewhere else, so the file is there.
    aliases = [(k, v) for k, v in re.findall(
        r"""['"]([^'"]+)['"]\s*:\s*['"]([^'"]+\.md)['"]""",
        (re.search(r"alias\s*:\s*\{(.*?)\}", html, re.S) or re.match("", "")).group(1) or "")]

    def as_file(target: str) -> str:
        target = target.strip()
        if target.startswith(("http://", "https://")):
            return target if (urlparse(target).hostname or "").lower() == host else ""
        target = target.lstrip("#").split("?", 1)[0].lstrip("/")
        if not target or target.endswith("/"):
            target += "README.md"
        route = "/" + target[:-3] if target.lower().endswith(".md") else "/" + target
        for pattern, source in aliases:
            try:
                if re.fullmatch(pattern, route):
                    moved = re.sub(pattern, source.replace("$", "\\"), route)
                    return urljoin(base, moved)
            except re.error:
                continue
        if not target.lower().endswith(".md"):
            target += ".md"
        return urljoin(base, target)

    pages = [urljoin(base, "README.md")]
    try:
        sidebar = fetcher.text(urljoin(base, "_sidebar.md"))
    except Exception:                               # noqa: BLE001 -- no sidebar, the home page alone
        sidebar = ""
    if sidebar and not _is_html_document(sidebar):
        for m in _MD_LINK.finditer(sidebar):
            page = as_file(m.group(1))
            if page:
                pages.append(page)
    return list(dict.fromkeys(pages))


#: A listing longer than this is narrowed to a topic before its pages are
#: fetched, by title and address; a shorter one is fetched whole and judged
#: page by page, which is slower and misses nothing.
TOPIC_PREFILTER_ABOVE = 40


def _topic_prefilter(entries: list, opts: Options, stats: dict | None,
                     titled: bool = False) -> list:
    """Drop the entries a topic plainly does not want, before fetching them.

    `entries` are URLs, or `(title, url)` pairs when `titled`. What is dropped
    is recorded in `stats["topic_skipped"]`, so the account of what the topic
    left out includes the pages it never had to fetch to know it.
    """
    if not (opts.topic or "").strip() or len(entries) <= TOPIC_PREFILTER_ABOVE:
        return entries
    sel = topics.Selector(opts.topic)
    if not sel.active:
        return entries
    kept, dropped = [], []
    for entry in entries:
        title, link = entry if titled else ("", entry)
        (kept if sel.may_want(title, link) else dropped).append(entry)
    if dropped:
        _log(opts, f"  the topic {opts.topic!r} rules out {len(dropped)} of "
                   f"{len(entries)} listed pages by title and address")
        if stats is not None:
            stats.setdefault("topic_skipped", []).extend(
                e[1] if titled else e for e in dropped)
    return kept


def _listed_pages(url: str, fetcher, opts: Options,
                  stats: dict | None = None) -> tuple[list[str], str]:
    """The pages the site says exist in this harvest's section, and who said so.

    Its generator's own manifest and its sitemaps, together. Either alone was
    taken as the whole truth, and neither is: FastAPI publishes an
    `objects.inv` -- mkdocstrings writes one -- that lists its 22 API
    reference pages and none of its ~150 guide pages, and it was taken as the
    site's own count (field test, 2026-09-24). The manifest still names the
    source when it has something to say; the sitemap fills in what it left
    out. Then the same narrowing every listed page goes through: the section,
    one language, one release.
    """
    prefix = _scope_for(url, opts)
    host = (urlparse(url).hostname or "").lower()
    manifest, index_kind = site_manifest(url, fetcher, opts)
    if len(manifest) < 3:
        manifest, index_kind = [], "sitemap"
    mapped = _sitemap_urls(url, fetcher, opts,
                           keep=lambda l: _crawlable(l, host, prefix))
    links = manifest + mapped
    if manifest and mapped:
        listed = {_normalize(l) for l in manifest}
        extra = {_normalize(l) for l in mapped if _crawlable(l, host, prefix)} - listed
        if extra:
            _log(opts, f"  the {index_kind} manifest lists {len(listed)} pages; the "
                       f"sitemap names {len(extra)} more in scope, taken too")
    if not links:
        return [], index_kind

    # One entry per page, fetched as the index spells it: the listed URL is
    # the canonical one, and stripping its slash costs a redirect per page
    # where it does not cost the page (`_fetchable`).
    keys: set[str] = set()
    scoped = []
    for link in links:
        key = _normalize(link)
        if key not in keys:
            keys.add(key)
            if _crawlable(link, host, prefix):
                scoped.append(_fetchable(link))
    before = len(scoped)
    scoped = _prefer_default_locale(_focus_on_docs(scoped, prefix))
    if len(scoped) != before:
        _log(opts, f"  narrowed {before} {index_kind} URLs to {len(scoped)} "
                   f"(documentation, default language)")
    asked = _requested_release(url, opts)
    if not asked:
        # Nothing asked for means the current release, not every release the
        # site has ever published under one label.
        scoped, current = _prefer_current_release(
            scoped, opts, hint=opts.release_hint,
            entry=lambda: _entry_page(url, fetcher))
        if current and stats is not None:
            stats["current_release"] = current
    scoped = _urls_for_release(scoped, asked, opts, stats)
    return scoped, index_kind


#: Endings a page's address takes in one listing and not in another: an
#: `llms.txt` links `/guides/auth.md` where the sitemap lists `/guides/auth`.
_PAGE_SUFFIXES = ("/index.html.md", "/index.md", "/index.mdx", ".html.md", ".md",
                  ".mdx", "/index.html", ".html", "/index")


def _page_key(url: str) -> str:
    """One page, however a listing spells it -- for comparing listings only."""
    parsed = urlparse(_normalize(url))
    path = parsed.path or "/"
    for suffix in _PAGE_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)] or "/"
            break
    return (parsed.hostname or "").lower() + path.rstrip("/").lower()


def _published_pages(docs: list[Doc], stats: dict | None) -> set[str] | None:
    """The pages a published file accounted for, as page keys; None when the
    file names none (a dump with no per-page markers cannot be checked)."""
    named = [d.url for d in docs if "#" not in d.url and not
             d.url.lower().endswith(("llms.txt", "llms-full.txt", "llms-medium.txt"))]
    failed = [f.get("url", "") for f in ((stats or {}).get("failed_urls") or [])]
    if not named and not failed:
        return None
    return {_page_key(u) for u in named + failed if u}


def _linked_from(docs: list[Doc], url: str, opts: Options) -> list[str]:
    """In-section pages the given documents link to, in the order met.

    What a published file's own pages point at is the site's word too: a
    curated `llms.txt` lists seven pages of GitHub's Actions docs, and those
    seven link to the other three hundred (field test, 2026-09-24).
    """
    host = (urlparse(url).hostname or "").lower()
    prefix = _scope_for(url, opts)
    found: list[str] = []
    for doc in docs:
        text = doc.markdown or ""
        fences = _fence_spans(text)
        where = doc.url.split("#", 1)[0]
        for m in _MD_LINK.finditer(text):
            if _inside(m.start(), fences):
                continue
            link = _fetchable(urljoin(where, m.group(1)))
            if _crawlable(link, host, prefix) and not looks_like_article(link):
                found.append(link)
    return list(dict.fromkeys(found))


def _complete_from_listing(url: str, fetcher, opts: Options, stats: dict | None,
                           sink, covered: set[str], source: str,
                           already: int, docs: list[Doc] | None = None
                           ) -> tuple[list[Doc], str]:
    """Fetch what the site lists in scope and a published file left out.

    An `llms.txt` is the site's word to us and is read first -- its pages
    are the cleanest copy there is -- but it is not always all of the
    documentation. Angular's lists 85 curated pages; AWS's lists 2 of the
    Lambda guide's hundreds; each was stored as complete (field test,
    2026-09-24). The sitemap and the generator's manifest are the site's word
    too, so what they list in the same section and the file does not is
    fetched as well, and coverage is measured against both.

    Returns the extra documents and a suffix for the strategy name.
    """
    listed, index_kind = _listed_pages(url, fetcher, opts, {})
    prefix = _scope_for(url, opts)
    admit = _admission(url, prefix, seeds=listed or None)
    linked = [u for u in _linked_from(docs or [], url, opts) if admit(u)]
    if linked and not listed:
        index_kind = "pages it links to"
    missing = [u for u in list(dict.fromkeys(listed + linked)) if _page_key(u) not in covered]
    missing = _topic_prefilter(missing, opts, stats)
    if not missing:
        if listed:
            _log(opts, f"  the {index_kind} agrees with {source}: nothing in scope is missing")
        return [], ""
    cap = opts.limit()
    room = None if cap is None else max(0, cap - already)
    _log(opts, f"  the {index_kind} lists {len(missing)} page(s) in scope that {source} "
               f"does not; fetching them too")
    if room == 0:
        if stats is not None:
            stats["truncated"] = True
            stats["remaining"] = (stats.get("remaining") or 0) + len(missing)
            stats["discovered"] = (stats.get("discovered") or 0) + len(missing)
        return [], f" + {index_kind}"
    sub: dict = {}
    sub_opts = replace(opts, crawl=True, max_pages=room or 0)
    try:
        extra = _crawl_html(url, fetcher, sub_opts, sub, sink=sink, seeds=missing,
                            statement=index_kind, known=covered, admit=admit)
    except ForgeError as e:
        _log(opts, f"  none of them could be read: {e}")
        extra = []
    if stats is not None:
        known = sub.get("discovered", len(missing))
        for key in ("expected", "discovered"):
            stats[key] = (stats.get(key) or 0) + known
        for key in ("acquired", "fetched"):
            stats[key] = (stats.get(key) or 0) + len(extra)
        for key in ("unextractable", "refused"):
            if sub.get(key):
                stats[key] = list(stats.get(key) or []) + list(sub[key])
        if sub.get("truncated"):
            stats["truncated"] = True
            stats["remaining"] = (stats.get("remaining") or 0) + (sub.get("remaining") or 0)
        if sub.get("rate_limited"):
            stats["rate_limited"] = True
        stats["supplemented"] = {"from": index_kind, "missing": len(missing),
                                 "known": known, "stored": len(extra)}
        if stats.get("whole") is not False and sub.get("whole") is not True:
            stats["whole"] = False if sub.get("whole") is False else None
        note = (f"{source} left out {known} page(s) the {index_kind} lists or links to "
                f"in scope; {len(extra)} of them were fetched and stored")
        stats["reason"] = (f"{stats['reason']}; {note}" if stats.get("reason") else note)
    return extra, f" + {index_kind}"


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
