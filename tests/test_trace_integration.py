"""
Integration tests for execution-trace observability: forge_tools.run_tool()
through to the real tool functions, and app.py's SSE surface.

Unlike tests/test_tracing.py (the trace log in isolation), these drive the
actual instrumented call paths -- tool_learn_technology, tool_harvest_docs,
run_tool's dispatch -- with the network stubbed out, the same way
tests/test_learn.py already does for the tools themselves.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.tools import harvest_jobs
from docsforge.core import resolver
from docsforge.tools import tracing as tr
from docsforge.store.kb_store import FileStore

PAGES = [("Intro", "https://x.dev/docs/intro", "welcome")]


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    ft.reset_store(FileStore(tmp_path))
    tr.clear()
    harvest_jobs.clear()
    yield
    ft.reset_store(None)
    tr.clear()
    harvest_jobs.clear()


def resolution(url="https://x.dev/docs/", verified=True, name="effect"):
    got = resolver.Resolution(name=name, ecosystem="npm")
    cand = resolver.Candidate(url, "npm:homepage", 0.8, "stubbed", verified,
                              "names it 9 times" if verified else "never mentions it")
    got.candidates = [cand]
    got.best = cand if verified else None
    got.resolved_via = "registry" if verified else ""
    if not verified:
        got.note = "Found 1 candidate(s) but none could be confirmed to document it."
    return got


def last_trace():
    tid = ft.last_trace_id()
    assert tid, "run_tool() should have minted a trace id"
    trace = tr.get(tid)
    assert trace is not None
    return trace


def by_name(trace, name):
    matches = [e for e in trace.events() if e.name == name]
    assert matches, f"no event named {name!r} in {[e.name for e in trace.events()]}"
    return matches[-1]


# ── run_tool(): EVERY call records what ran and what came back ──
def test_an_uninstrumented_tool_still_records_its_arguments_and_output(monkeypatch):
    from docsforge.core import engine as df

    """The gap this fixes: a tool nobody wrote internal stages for used to
    produce an empty trace, so opening its row in the UI showed nothing.
    Every call now records the invocation itself."""
    monkeypatch.setattr(ft, "detect_source",
                        lambda url, fetcher: df.Detection("llms_txt", url))

    out = ft.run_tool("detect_source_type", {"url": "https://x.dev/llms.txt"})
    trace = last_trace()
    assert trace.finished is not None, "an ordinary tool call's trace closes immediately"

    call = by_name(trace, "detect_source_type")
    assert call.state == tr.COMPLETED
    assert call.metadata == {"url": "https://x.dev/llms.txt"}, "what was executed"
    assert call.output == out, "what came back"
    assert call.target == "https://x.dev/llms.txt"


def test_a_failed_call_records_the_failure_and_the_text_the_model_was_given():
    out = ft.run_tool("read_knowledge_base", {"name": "nothing-stored-under-this"})
    trace = last_trace()
    call = by_name(trace, "read_knowledge_base")
    assert call.state == tr.FAILED
    assert call.error
    # A failed call still produced a result; the detail view must be able to
    # show exactly what the model received.
    assert call.output == out


def test_arguments_are_sanitized_before_reaching_the_browser(monkeypatch):
    """Tools take URLs and names today, but the boundary has to hold for
    whatever a future tool accepts."""
    captured = {}

    def fake_tool(url, api_key=None, trace=None):
        captured["ran"] = True
        return "ok"

    tool = ft.Tool("fake_secret_tool", "d",
                   {"type": "object",
                    "properties": {"url": {"type": "string"},
                                  "api_key": {"type": "string"}}},
                   fake_tool)
    monkeypatch.setitem(ft.BY_NAME, "fake_secret_tool", tool)

    ft.run_tool("fake_secret_tool", {"url": "https://x.dev", "api_key": "sk-live-secret"})
    assert captured["ran"], "the real argument must still reach the tool"

    call = by_name(last_trace(), "fake_secret_tool")
    assert call.metadata["api_key"] == "[redacted]"
    assert call.metadata["url"] == "https://x.dev"


def test_a_large_output_is_bounded_and_the_omission_disclosed(monkeypatch):
    big = "x" * (tr.MAX_OUTPUT + 5_000)

    def fake_tool(url, trace=None):
        return big

    tool = ft.Tool("fake_big_tool", "d",
                   {"type": "object", "properties": {"url": {"type": "string"}}},
                   fake_tool)
    monkeypatch.setitem(ft.BY_NAME, "fake_big_tool", tool)

    out = ft.run_tool("fake_big_tool", {"url": "https://x.dev"})
    assert len(out) == len(big), "the model still gets the whole result"

    call = by_name(last_trace(), "fake_big_tool")
    assert len(call.output) == tr.MAX_OUTPUT
    assert call.omitted == 5_000, "what was left out is counted, not hidden"


def test_internal_stages_nest_under_the_tool_call(monkeypatch):
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    monkeypatch.setattr(
        ft, "tool_harvest_docs",
        lambda url, name=None, max_pages=0, js=False, version=None, **kw: "Harvested — 2 pages")

    ft.run_tool("learn_technology", {"name": "effect"})
    trace = last_trace()
    call = by_name(trace, "learn_technology")
    resolving = by_name(trace, "resolving identity")

    assert call.parent_id is None, "the tool call is the root of its own trace"
    assert resolving.parent_id == call.id, "its stages hang beneath it"


# ── tool_learn_technology: resolving -> harvesting stages ────
def test_learn_technology_traces_resolution_and_harvest_stages(monkeypatch):
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    monkeypatch.setattr(
        ft, "tool_harvest_docs",
        lambda url, name=None, max_pages=0, js=False, version=None, **kw: (
            kw["trace"].event("stub harvest", message="2 pages") or "Harvested **effect** — 2 pages"
        ))

    out = ft.run_tool("learn_technology", {"name": "effect"})
    assert "Resolved" in out
    trace = last_trace()

    resolving = by_name(trace, "resolving identity")
    assert resolving.state == tr.COMPLETED
    assert resolving.result["best"]["url"] == "https://x.dev/docs/"
    assert resolving.result["resolved_via"] == "registry"

    stub = by_name(trace, "stub harvest")
    # tool_harvest_docs was handed the SAME trace context learn_technology
    # opened, not a disconnected one of its own.
    assert stub.trace_id == trace.id


def test_failed_resolution_is_traced_with_every_candidate_considered(monkeypatch):
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution(verified=False))

    out = ft.run_tool("learn_technology", {"name": "ghost"})
    assert out.startswith("Error:")
    trace = last_trace()

    resolving = by_name(trace, "resolving identity")
    assert resolving.state == tr.FAILED
    assert len(resolving.result["candidates"]) == 1
    assert resolving.result["candidates"][0]["verified"] is False

    # The tool call itself is ALSO recorded as failed, without erasing the
    # more specific resolution failure nested beneath it.
    call = by_name(trace, "learn_technology")
    assert call.state == tr.FAILED
    assert resolving.state == tr.FAILED  # still true; not overwritten
    assert resolving.parent_id == call.id


def test_already_stored_technology_is_traced_without_a_harvest(monkeypatch):
    ft.store().save("effect", "v3", "https://x.dev/docs/v3/", "crawl", PAGES, complete=True)
    monkeypatch.setattr(ft, "_resolve",
                        lambda *a, **k: pytest.fail("must not resolve when stored"))

    ft.run_tool("learn_technology", {"name": "effect"})
    trace = last_trace()
    already = by_name(trace, "already stored")
    assert already.result["technology"] == "effect"


# ── tool_harvest_docs: harvesting / storing / corpus selection ──
def test_harvest_docs_traces_its_three_stages(monkeypatch):
    from docsforge.core import engine as df

    def fake_harvest(url, opts=None, stats=None, sink=None, fetcher=None):
        stats["discovered"] = 1
        stats["whole"] = True
        doc = df.Doc(url, "Intro", "# Intro\nwelcome")
        if sink is not None:
            sink.add(doc.title, doc.url, doc.markdown)
        return [doc], "crawl"

    monkeypatch.setattr(ft, "harvest", fake_harvest)
    monkeypatch.setattr(ft, "_federate", lambda *a, **k: "")

    out = ft.run_tool("harvest_docs", {"url": "https://x.dev/docs/"})
    assert "Harvested" in out
    trace = last_trace()

    harvesting = by_name(trace, "harvesting")
    assert harvesting.state == tr.COMPLETED
    assert harvesting.result["pages"] == 1
    assert harvesting.result["strategy"] == "crawl"

    storing = by_name(trace, "storing")
    assert storing.state == tr.COMPLETED

    corpus = by_name(trace, "corpus selection")
    assert corpus.state == tr.COMPLETED

    completed = by_name(trace, "harvest_docs completed")
    assert completed.result["pages"] == 1
    assert completed.result["complete"] is True


def test_harvest_docs_traces_a_failed_fetch(monkeypatch):
    def fake_harvest(url, opts=None, stats=None, sink=None, fetcher=None):
        raise ft.ForgeError("connection refused")

    monkeypatch.setattr(ft, "harvest", fake_harvest)

    out = ft.run_tool("harvest_docs", {"url": "https://x.dev/docs/"})
    assert out.startswith("Error:")
    trace = last_trace()
    harvesting = by_name(trace, "harvesting")
    assert harvesting.state == tr.FAILED
    assert "connection refused" in harvesting.error


# ── page-fetch progress ticks: aggregated, no fake percentage ──
def test_counting_fetcher_ticks_report_bare_counts_without_a_total(monkeypatch):
    from docsforge.core import engine as df

    monkeypatch.setattr(df.Fetcher, "html", lambda self, url: "<html><body>ok</body></html>")

    ctx = tr.start("x")
    stage = ctx.stage("harvesting")
    stage.start()
    progress = harvest_jobs.Progress()
    stats = {}  # no "discovered" key: the total is genuinely unknown
    fetcher = ft._CountingFetcher(df.Options(), progress, stats, stage=stage)
    for i in range(ft.TICK_EVERY_PAGES):
        fetcher.html(f"https://x.dev/{i}")

    ev = tr.get(ctx.trace_id).events()[-1]
    assert ev.counters.get("pages") == ft.TICK_EVERY_PAGES
    assert "expected" not in ev.counters
    assert "%" not in ev.message


def test_counting_fetcher_ticks_include_the_total_once_known(monkeypatch):
    from docsforge.core import engine as df

    monkeypatch.setattr(df.Fetcher, "html", lambda self, url: "<html><body>ok</body></html>")

    ctx = tr.start("x")
    stage = ctx.stage("harvesting")
    stage.start()
    progress = harvest_jobs.Progress()
    stats = {"discovered": 50}
    fetcher = ft._CountingFetcher(df.Options(), progress, stats, stage=stage)
    for i in range(ft.TICK_EVERY_PAGES):
        fetcher.html(f"https://x.dev/{i}")

    ev = tr.get(ctx.trace_id).events()[-1]
    assert ev.counters == {"pages": ft.TICK_EVERY_PAGES, "expected": 50}


# ── the still-running / detach path ───────────────────────
def test_a_harvest_past_the_deadline_keeps_tracing_after_run_tool_returns(monkeypatch):
    """The scenario the whole background-trace design exists for: run_tool()
    returns "still running" while `work()` keeps executing (and tracing) on
    harvest_jobs' own thread."""
    release = __import__("threading").Event()

    def slow_resolve(*a, **k):
        release.wait(timeout=2)
        return resolution()

    monkeypatch.setattr(ft, "_resolve", slow_resolve)
    monkeypatch.setattr(
        ft, "tool_harvest_docs",
        lambda url, name=None, max_pages=0, js=False, version=None, **kw: "Harvested — 1 page")
    # Force the "did not finish in time" branch without a real 25s wait.
    monkeypatch.setattr(harvest_jobs, "wait", lambda job, seconds=None: False)

    out = ft.run_tool("learn_technology", {"name": "effect"})
    assert "still running" in out
    trace = last_trace()

    # run_tool() must NOT have closed this trace -- the real work is still
    # in flight on harvest_jobs' thread.
    assert trace.finished is None
    assert trace.keep_open is True

    release.set()
    for _ in range(100):
        if trace.finished is not None:
            break
        time.sleep(0.02)
    assert trace.finished is not None, "the background thread should close its own trace"
    resolving = by_name(trace, "resolving identity")
    assert resolving.state == tr.COMPLETED


# ── progress on the pathway that fetches pages as text ──────────────
# `_CountingFetcher` hooks `html()`, which a crawl and a sitemap use. A
# Markdown-twin manifest fetches its pages with `text()` instead, so the
# design doc's own headline case -- adk.dev, 229 links, every one `.md` --
# counted zero pages and reported "starting" for its entire run.
def _serve(pages, monkeypatch):
    from docsforge.core import engine as df

    class Resp:
        def __init__(self, text, status=200):
            self.text, self.status_code = text, status
            self.headers = {"content-type": "text/plain"}

        @property
        def content(self):
            return self.text.encode("utf-8")

    def fake_get(self, url, **kw):
        body = pages.get(url.rstrip("/"), pages.get(url))
        return Resp(body) if body is not None else Resp("not found", 404)

    monkeypatch.setattr(df.Fetcher, "get", fake_get)


def test_a_markdown_twin_manifest_counts_the_pages_it_fetches(monkeypatch):
    from docsforge.core import engine as df

    pages = {"https://x.dev/llms.txt":
             "# Index\n\n" + "\n".join(f"- [P{i}](https://x.dev/p{i}.md)"
                                       for i in range(6))}
    for i in range(6):
        pages[f"https://x.dev/p{i}.md"] = f"# P{i}\n\nbody"
    _serve(pages, monkeypatch)

    progress = harvest_jobs.Progress()
    stats = {}
    ctx = tr.start("x")
    stage = ctx.stage("harvesting")
    stage.start()

    opts = df.Options(verbose=False, delay=0.0)
    with ft._CountingFetcher(opts, progress, stats, stage=stage) as fetcher:
        docs, strategy = df.harvest("https://x.dev/llms.txt", opts,
                                    fetcher=fetcher, stats=stats)

    assert strategy == "llms.txt (md manifest)"
    assert len(docs) == 6
    assert progress.pages == 6, "every page fetched is a page counted"
    assert progress.expected == 6

    ticked = [e for e in tr.get(ctx.trace_id).events() if e.counters.get("pages")]
    assert ticked, "the live trace must see the progress too"


def test_infrastructure_fetches_are_not_counted_as_pages(monkeypatch):
    """`text()` also fetches the manifest itself, robots.txt and sitemaps.
    Counting those would inflate the number the coverage claim rests on,
    which is why the acquisition loop reports pages rather than the
    transport guessing."""
    from docsforge.core import engine as df

    pages = {"https://x.dev/llms.txt":
             "# Index\n\n" + "\n".join(f"- [P{i}](https://x.dev/p{i}.md)"
                                       for i in range(3))}
    for i in range(3):
        pages[f"https://x.dev/p{i}.md"] = f"# P{i}\n\nbody"
    _serve(pages, monkeypatch)

    progress = harvest_jobs.Progress()
    stats = {}
    opts = df.Options(verbose=False, delay=0.0)
    with ft._CountingFetcher(opts, progress, stats) as fetcher:
        docs, _ = df.harvest("https://x.dev/llms.txt", opts,
                             fetcher=fetcher, stats=stats)

    assert len(docs) == 3
    assert progress.pages == 3, "the manifest fetch itself is not a page"


def test_the_exact_denominator_is_known_before_the_fetching_starts(monkeypatch):
    """A published manifest knows how many pages it promises up front —
    that is why its coverage claim beats a sitemap's. Publishing it only
    after the loop finished meant a 229-page harvest read "fetched 40
    pages" for the whole ten minutes it could have read "40/229"."""
    from docsforge.core import engine as df

    pages = {"https://x.dev/llms.txt":
             "# Index\n\n" + "\n".join(f"- [P{i}](https://x.dev/p{i}.md)"
                                       for i in range(8))}
    for i in range(8):
        pages[f"https://x.dev/p{i}.md"] = f"# P{i}\n\nbody"
    _serve(pages, monkeypatch)

    progress = harvest_jobs.Progress()
    progress.phase = "harvesting"      # what learn_technology sets before this
    stats = {}
    seen: list[str] = []

    opts = df.Options(verbose=False, delay=0.0)
    with ft._CountingFetcher(opts, progress, stats) as fetcher:
        real = fetcher.page_fetched

        def spy():
            real()
            seen.append(progress.line())

        fetcher.page_fetched = spy
        df.harvest("https://x.dev/llms.txt", opts, fetcher=fetcher, stats=stats)

    assert seen[0] == "harvesting 1/8 pages", "the total is known from the first page"
    assert seen[-1] == "harvesting 8/8 pages"
    assert "so far" not in " ".join(seen), "an exact count never says 'so far'"


def test_a_hybrid_root_counts_as_a_page_it_already_holds(monkeypatch):
    """The root arrives with the manifest rather than through the fetch
    loop. Counting it keeps the progress figure and the denominator
    describing the same set of documents."""
    from docsforge.core import engine as df

    body = ("# Docs\n\n> summary\n\n" + ("Root prose. " * 80) + "\n\n"
            + "\n".join(f"- [P{i}](https://x.dev/p{i}.md)" for i in range(4)))
    pages = {"https://x.dev/llms.txt": body}
    for i in range(4):
        pages[f"https://x.dev/p{i}.md"] = f"# P{i}\n\nbody"
    _serve(pages, monkeypatch)

    progress = harvest_jobs.Progress()
    stats = {}
    opts = df.Options(verbose=False, delay=0.0)
    with ft._CountingFetcher(opts, progress, stats) as fetcher:
        docs, _ = df.harvest("https://x.dev/llms.txt", opts,
                             fetcher=fetcher, stats=stats)

    assert len(docs) == 5, "four linked pages plus the root"
    assert stats["discovered"] == 5
    assert progress.pages == 5, "the root is a document obtained, so it counts"
    assert progress.expected == 5
