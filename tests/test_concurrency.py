"""
Tests for PROPOSAL-3 Phase 3 — bounded concurrency.

Fetching is almost all of a crawl's wall-clock time and almost none of its CPU.
`go.dev` spent sixteen minutes on roughly 1,200 pages, which is most of a second
per page spent waiting on the network with one connection open.

The phase's acceptance criterion is deliberately two-sided, because either half
alone is easy and worthless: **at least 2x faster, and the same page set as the
sequential run.** A crawler that is fast because it drops pages is not faster.
"""

import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core.engine import Options, _Pace

HOST = "https://x.dev"
BODY = ("<p>" + ("Documentation prose about the subject at hand. " * 25) + "</p>")


def _site(leaves: int = 11) -> dict[str, str]:
    """A hub and its leaves.

    A chain would test nothing: page N's links are not known until page N has
    been parsed, so a chain is inherently sequential however many workers there
    are. Real documentation is a hub with a sidebar, which is exactly the shape
    that can overlap.
    """
    links = " ".join(f'<a href="/docs/p{i}">Page {i}</a>' for i in range(leaves))
    pages = {f"{HOST}/docs/": f"<html><head><title>Docs</title></head><body><main>"
                              f"<h1>Docs</h1>{BODY}{links}</main></body></html>"}
    for i in range(leaves):
        pages[f"{HOST}/docs/p{i}"] = (
            f"<html><head><title>Page {i}</title></head><body><main>"
            f"<h1>Page {i}</h1>{BODY}</main></body></html>")
    return pages


class SlowFetcher:
    """Serves the canned site, taking `latency` seconds over each page."""

    def __init__(self, pages: dict[str, str], latency: float = 0.0):
        self.pages = pages
        self.latency = latency
        self.calls: list[str] = []

    def html(self, url: str) -> str:
        self.calls.append(url)
        if self.latency:
            time.sleep(self.latency)
        # The crawler normalises a trailing slash away before it fetches, so a
        # canned site keyed on the URL as written would 404 on its own hub.
        for key in (url, url + "/", url.rstrip("/")):
            if key in self.pages:
                return self.pages[key]
        raise df.ForgeError(f"HTTP 404 for {url}")

    def render(self, url: str) -> str:
        return self.html(url)

    def close(self) -> None:
        pass


def _crawl(pages, workers, latency=0.0, max_pages=0, delay=0.0):
    opts = Options(crawl=True, max_pages=max_pages, delay=delay,
                   workers=workers, verbose=False)
    stats: dict = {}
    docs = df._crawl_html(f"{HOST}/docs/", SlowFetcher(pages, latency), opts,
                          stats=stats)
    return docs, stats


# ── the same pages, in the same order ────────────────────
def test_a_concurrent_crawl_returns_the_same_pages():
    pages = _site()
    one, _ = _crawl(pages, workers=1)
    many, _ = _crawl(pages, workers=4)

    assert {d.url for d in one} == {d.url for d in many}
    assert len(one) == len(pages)


def test_a_concurrent_crawl_returns_them_in_the_same_order():
    # Dispatch order is queue order and results are consumed in dispatch order,
    # so concurrency reorders nothing. Worth pinning: an out-of-order crawl
    # would still pass the set comparison above while changing which pages
    # survive `max_pages`.
    pages = _site()
    one, _ = _crawl(pages, workers=1)
    many, _ = _crawl(pages, workers=4)

    assert [d.url for d in one] == [d.url for d in many]


def test_a_truncated_concurrent_crawl_keeps_the_same_prefix():
    pages = _site(leaves=20)
    one, s1 = _crawl(pages, workers=1, max_pages=6)
    many, s2 = _crawl(pages, workers=4, max_pages=6)

    assert [d.url for d in one] == [d.url for d in many]
    assert len(many) == 6
    assert s1["truncated"] and s2["truncated"]


def test_prefetched_pages_are_counted_as_remaining():
    # Pages taken off the queue into the window but never processed are still
    # outstanding. Counting only the queue would understate a shortfall — and
    # report `whole` for a crawl that stopped with pages in hand.
    pages = _site(leaves=20)
    _, stats = _crawl(pages, workers=4, max_pages=6)

    assert stats["whole"] is False
    assert stats["remaining"] >= 14


# ── and faster ───────────────────────────────────────────
@pytest.mark.parametrize("latency", [0.05])
def test_concurrency_is_at_least_twice_as_fast(latency):
    pages = _site(leaves=11)

    start = time.monotonic()
    one, _ = _crawl(pages, workers=1, latency=latency)
    sequential = time.monotonic() - start

    start = time.monotonic()
    many, _ = _crawl(pages, workers=4, latency=latency)
    concurrent = time.monotonic() - start

    assert len(one) == len(many) == 12
    assert concurrent * 2 <= sequential, (
        f"sequential {sequential:.2f}s vs concurrent {concurrent:.2f}s")


# ── politeness is per host, and spaces starts ────────────
def test_pacing_spaces_requests_to_one_host():
    pace = _Pace(0.05)
    start = time.monotonic()
    for _ in range(4):
        pace.wait("https://x.dev/a")
    # Four slots at 50ms: the first is free, so three gaps.
    assert time.monotonic() - start >= 0.15 - 0.02


def test_pacing_does_not_make_one_host_wait_for_another():
    # Politeness is a promise to a host, not a global throttle. A federated
    # harvest touching three hosts should not run three times slower for it.
    pace = _Pace(0.05)
    pace.wait("https://a.dev/1")
    start = time.monotonic()
    pace.wait("https://b.dev/1")
    assert time.monotonic() - start < 0.03


def test_pacing_with_no_delay_costs_nothing():
    pace = _Pace(0.0)
    start = time.monotonic()
    for i in range(50):
        pace.wait(f"https://x.dev/{i}")
    assert time.monotonic() - start < 0.05


# ── the constraint that cannot be threaded away ──────────
def test_a_rendered_crawl_stays_sequential():
    # Playwright's sync API is bound to the thread that created the browser and
    # a Fetcher keeps exactly one, so a rendered crawl must not be pooled — a
    # correctness constraint, not a tuning choice.
    source = open(df.__file__, encoding="utf-8").read()
    assert re.search(r"workers\s*=\s*1 if \(opts\.js", source), \
        "a JS crawl must fall back to one worker"


def test_js_options_force_one_worker():
    pages = _site(leaves=4)
    opts = Options(crawl=True, max_pages=0, delay=0.0, workers=8, js=True,
                   verbose=False)
    fetcher = SlowFetcher(pages)
    docs = df._crawl_html(f"{HOST}/docs/", fetcher, opts, stats={})
    assert len(docs) == 5


def test_the_pool_is_shut_down_even_when_a_crawl_raises(monkeypatch):
    # A leaked pool holds non-daemon threads for the life of the process, which
    # in an MCP server is the life of the session.
    import concurrent.futures as cf

    made: list = []
    real = cf.ThreadPoolExecutor

    class Tracked(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(df, "ThreadPoolExecutor", Tracked)
    monkeypatch.setattr(df, "_soup", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        _crawl(_site(leaves=4), workers=4)

    assert made, "the crawl never opened a pool"
    assert made[0]._shutdown


# ── never more than HOST_CONCURRENCY open to one host ────
def test_no_more_than_the_cap_is_ever_open_to_one_host():
    # A rate limit is not a concurrency limit. Four requests spaced 0.4s apart
    # are still four open sockets if each takes two seconds, and it is open
    # sockets rather than request frequency that a small documentation host
    # notices. §6 asks for this asserted, so the pace records a high-water mark
    # rather than merely promising one.
    pages = _site(leaves=30)
    opts = Options(crawl=True, max_pages=0, delay=0.0, workers=8, verbose=False)
    stats: dict = {}
    df._crawl_html(f"{HOST}/docs/", SlowFetcher(pages, latency=0.01), opts,
                   stats=stats)

    peak = stats["host_peak"]
    assert peak, "nothing recorded how many requests were open"
    assert max(peak.values()) <= df.HOST_CONCURRENCY, peak


def test_the_cap_is_per_host_not_global():
    pace = df._Pace(0.0, concurrency=1)
    with pace.host("https://a.dev/1"):
        # Another host is unaffected: this would deadlock on a global lock.
        with pace.host("https://b.dev/1"):
            pass
    assert pace.peak == {"a.dev": 1, "b.dev": 1}


# ── one connection per read tool call ─────────────────────

def test_the_read_tools_run_inside_one_store_session(monkeypatch):
    """Measured against Postgres: `list_knowledge_base()` opened four
    connections and `read_knowledge_base()` three, one per operation. Through
    `run_tool` they now open one each."""
    from docsforge.tools import forge_tools as ft
    opened = []

    class Store:
        kind, location = "postgres", "test"

        def session(self):
            import contextlib

            @contextlib.contextmanager
            def held():
                opened.append("session")
                yield
            return held()

        def technologies(self, *a, **kw):
            return [], 0

    monkeypatch.setattr(ft, "store", lambda: Store())
    ft.run_tool("list_knowledge_base", {})
    assert opened == ["session"]


def test_a_harvest_does_not_hold_a_connection_while_it_crawls(monkeypatch):
    """The whole point of naming the pooled tools rather than inferring them:
    a harvest spends its call fetching pages, and holding a connection across
    that is the problem, not a smaller version of it."""
    from docsforge.tools import forge_tools as ft
    assert "learn_technology" not in ft._POOLED
    assert "harvest_docs" not in ft._POOLED
    assert "list_knowledge_base" in ft._POOLED
