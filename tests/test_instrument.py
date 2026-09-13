"""
Offline tests for Phase B instrumentation — no network.

Phase B's whole claim is that it changes nothing. That claim is load-bearing:
everything after it reads these numbers and trusts that measuring the pipeline
did not disturb the pipeline. So inertness is tested first and hardest, and the
primitives second.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core import instrument
from docsforge.core.instrument import Budget, Ledger, Observation, ResolveState, observe

LONG = "Documentation body text. " * 20          # comfortably over MIN_MAIN_CHARS
SHORT = "tiny"


def page(inner: str, title: str = "T", head: str = "") -> str:
    return f"<html><head><title>{title}</title>{head}</head><body>{inner}</body></html>"


# ── Phase B changes no behaviour ─────────────────────────
def test_the_crawler_adapts_on_its_own_measurements():
    # This test used to assert the opposite. That was right for Phase B, whose
    # whole claim was that measuring changed nothing — and wrong from Phase D
    # on, because a crawl that cannot read its own measurements cannot revise
    # its plan, which is the defect the project exists to fix.
    #
    # What must stay true is the pair of rules that keep adaptation honest:
    # every revision is recorded (Invariant 11), and nothing is ever dropped
    # (Invariant 7).
    crawler = open(df.__file__, encoding="utf-8").read()

    assert "Ledger" in crawler and "Observation" in crawler
    assert "plan.revise(" in crawler, "the crawler never re-derives its plan"
    assert "reprioritise(" in crawler, "the frontier is never rescored"
    assert 'stats["revisions"]' in crawler, "revisions are not surfaced"

    # The measuring half is still the driver's, and still re-parses. If the
    # crawler ever imports it, every page is parsed twice for no gain.
    assert not re.search(r"^\s*(import|from)\s+instrument\b", crawler, re.M), \
        "the crawler should report from its own parse, not re-measure"


@pytest.mark.parametrize("inner,expected", [
    (f"<main>{LONG}</main>", "main"),
    (f"<article>{LONG}</article>", "article"),
    (f"<div role='main'>{LONG}</div>", "[role=main]"),
    (f"<div class='markdown-body'>{LONG}</div>", ".markdown-body"),
    (f"<div class='prose'>{LONG}</div>", ".prose"),
    (f"<div id='content'>{LONG}</div>", "#content"),
    # Nothing in CONTENT matches, so the container is chosen by density
    # instead of <body> being taken whole and unexamined.
    (f"<div class='nope'>{LONG}</div>", "density"),
    # A matching selector below the threshold is not believed.
    (f"<main>{SHORT}</main><div class='nope'>{LONG}</div>", "density"),
])
def test_pick_main_reports_the_selector_the_extractor_actually_used(inner, expected):
    soup = df._soup(page(inner))
    df.strip_chrome(soup)
    _, selector = df.pick_main(soup)
    assert selector == expected


def test_extraction_still_agrees_with_pick_main():
    # The refactor's only risk: _html_to_md and pick_main drifting apart. If
    # they ever disagree, every recorded selector becomes a lie.
    html = page(f"<nav>skip me</nav><main>{LONG}</main>")
    _, body = df._html_to_md(html, "https://x.dev/a")

    soup = df._soup(html)
    df.strip_chrome(soup)
    main, selector = df.pick_main(soup)

    assert selector == "main"
    assert "skip me" not in body
    assert main.get_text(strip=True) in body.replace("\n", " ") or "Documentation" in body


def test_observing_a_page_does_not_disturb_it():
    html = page(f"<main>{LONG}</main>")
    before = html
    observe(html, "https://x.dev/a", name="docs")
    assert html == before


# ── Observation ──────────────────────────────────────────
def test_an_unrecognised_template_goes_to_density_not_to_body():
    # The defect Phase B counted and Phase D fixed: an unrecognised template
    # used to store <body> whole and say nothing about it.
    obs = observe(page(f"<div class='nope'>{LONG}</div>"), "https://x.dev/a")
    assert obs.fell_through is True          # none of the nine selectors matched
    assert obs.selector == "density"         # but it was still chosen on merit
    assert obs.extractable is True

    obs = observe(page(f"<main>{LONG}</main>"), "https://x.dev/b")
    assert obs.fell_through is False
    assert obs.selector == "main"


def test_a_js_shell_is_diagnosed_from_emptiness_beside_a_bundle():
    shell = page("<div id='root'></div><script src='/bundle.js'></script>")
    assert observe(shell, "https://x.dev/a").shell is True

    # Same empty container, no bundle: not a shell, just a thin page.
    assert observe(page("<div id='root'></div>"), "https://x.dev/b").shell is False

    # A bundle beside real content is an ordinary page.
    full = page(f"<main>{LONG}</main><script src='/bundle.js'></script>")
    assert observe(full, "https://x.dev/c").shell is False


def test_the_generator_is_read_when_the_site_declares_one():
    head = "<meta name='generator' content='MkDocs 1.5.3'>"
    assert observe(page(f"<main>{LONG}</main>", head=head), "u").generator == "MkDocs 1.5.3"
    assert observe(page(f"<main>{LONG}</main>"), "u").generator == ""


def test_mentions_count_the_technology_naming_itself():
    body = "<main>FastAPI is fast. fastapi routes. " + LONG + "</main>"
    assert observe(page(body), "u", name="fastapi").mentions == 2
    # No name asked for, nothing counted.
    assert observe(page(body), "u").mentions == 0


def test_link_heavy_pages_are_distinguishable_from_prose():
    nav = "<div class='nope'>" + "".join(
        f"<a href='/p{i}'>A navigation entry {i}</a>" for i in range(40)) + "</div>"
    prose = f"<main>{LONG}</main>"

    seen = observe(page(nav), "u")
    assert seen.link_text_ratio > 0.9
    # And it is refused rather than stored: nearly all of its text is link text.
    assert seen.extractable is False
    assert observe(page(prose), "u").link_text_ratio == 0.0


def test_the_score_is_provisional_but_ordered_sensibly():
    prose = observe(page(f"<main><h2>H</h2><pre>code</pre>{LONG}</main>"), "u")
    nav = observe(page("<div class='nope'>" + "".join(
        f"<a href='/p{i}'>entry {i}</a>" for i in range(40)) + "</div>"), "u")
    assert prose.score() > nav.score()


# ── Ledger ───────────────────────────────────────────────
def _obs(**kw) -> Observation:
    return Observation(url=kw.pop("url", "https://x.dev/a"), **kw)


def test_the_ledger_window_is_the_last_twelve_pages():
    # Phase D re-derives its plan from recent pages, so a site that changes
    # template halfway is noticed halfway rather than averaged into silence.
    led = Ledger()
    for i in range(30):
        led.record(_obs(url=f"https://x.dev/{i}"))
    assert len(led) == 30
    assert len(led.recent()) == 12
    assert led.recent()[-1].url == "https://x.dev/29"
    assert led.recent(3)[0].url == "https://x.dev/27"


def test_a_template_is_its_layout_and_not_its_content():
    # Measured: folding heading and code counts into the signature gave 0.46
    # templates per page — 22 signatures across 40 Terraform pages that all
    # shared one identical ancestry. Two pages built the same way are the same
    # template however much they each happen to say.
    long_prose = f"<main><p>{LONG}</p></main>"
    rich = ("<main><h2>a</h2><h3>b</h3><pre>x</pre><pre>y</pre>"
            f"<p>{LONG}</p><a href='/1'>one</a></main>")

    plain = observe(page(long_prose), "https://x.dev/a")
    busy = observe(page(rich), "https://x.dev/b")

    assert plain.signature == busy.signature, "same layout, same template"
    assert plain.shape != busy.shape, "the content shape is still recorded"


def test_a_build_hash_does_not_create_a_new_template():
    # Next-style CSS modules emit `main_content__0SN51`, which changes on every
    # deploy. Leaving it in makes signatures incomparable across crawls.
    one = observe(page(f"<main class='layout_main__0SN51'>{LONG}</main>"), "u")
    two = observe(page(f"<main class='layout_main__9XyZ2'>{LONG}</main>"), "u")
    assert one.signature == two.signature


def test_pages_sharing_a_template_cluster_together():
    led = Ledger()
    led.record(_obs(url="a", signature="body>div>main|h3-5|c0|a1-2"))
    led.record(_obs(url="b", signature="body>div>main|h3-5|c0|a1-2"))
    led.record(_obs(url="c", signature="body>div>article|h0|c0|a16+"))

    clusters = led.by_signature()
    assert len(clusters) == 2
    assert len(clusters["body>div>main|h3-5|c0|a1-2"]) == 2


def test_the_summary_counts_what_phase_d_needs_to_read():
    led = Ledger()
    led.record(_obs(url="a", selector="main", chars=3000, signature="s1"))
    led.record(_obs(url="b", selector="", chars=1000, signature="s2"))       # fell through
    led.record(_obs(url="c", selector="", chars=10, shell=True, scripts=2, signature="s2"))

    s = led.summary()
    assert s["pages"] == 3
    assert s["fell_through"] == 2
    assert s["fell_through_pct"] == 66.7
    assert s["shells"] == 1
    assert s["templates"] == 2
    # Since Phase D an empty selector means refused, not "body taken whole".
    assert s["selectors"]["(refused)"] == 2


def test_an_empty_ledger_summarises_without_dividing_by_zero():
    assert Ledger().summary() == {"pages": 0}


# ── ResolveState ─────────────────────────────────────────
def test_a_rejected_candidate_still_leaves_its_evidence_behind():
    # The point of Layer 1's evidence lap: verify() currently fetches a page,
    # fails it, and discards the repository backlink that says where the docs
    # actually live.
    state = ResolveState(name="effect")
    html = ("<html><head><link rel='canonical' href='https://x.dev/docs/'></head>"
            "<body><a href='https://github.com/Effect-TS/effect'>source</a>"
            "<a href='https://other.io/docs/api'>API reference</a>"
            "<a href='https://x.dev/docs/self'>own page</a></body></html>")
    state.record("https://x.dev/", html)
    state.reject("https://x.dev/", "never mentions it")

    assert state.tried == ["https://x.dev/"]
    assert "https://github.com/Effect-TS/effect" in state.repos_seen
    assert state.canonical["https://x.dev/"] == "https://x.dev/docs/"
    # Leaves the host and looks like documentation.
    assert "https://other.io/docs/api" in state.outbound
    # Same host: says nothing the resolver did not already know.
    assert "https://x.dev/docs/self" not in state.outbound
    assert state.rejected == [("https://x.dev/", "never mentions it")]


def test_the_same_candidate_is_only_recorded_once():
    state = ResolveState()
    state.record("https://x.dev/")
    state.record("https://x.dev/")
    assert state.tried == ["https://x.dev/"]


# ── Budget ───────────────────────────────────────────────
def test_a_budget_is_exhausted_by_requests():
    b = Budget(requests=3, seconds=999)
    assert not b.exhausted
    b.charge()
    b.charge(2)
    assert b.spent == 3 and b.requests_left == 0
    assert b.exhausted
    assert "3 requests" in b.why()


def test_a_budget_is_exhausted_by_time():
    b = Budget(requests=999, seconds=0)
    assert b.exhausted
    assert b.why().startswith("gave up after")


def test_an_unspent_budget_has_no_refusal_to_give():
    assert Budget(requests=40, seconds=999).why() == ""


def test_only_the_resolver_spends_a_budget():
    # Layer 1 is bounded; nothing else has a lap structure to bound. If the
    # crawler ever grows one it needs its own budget with its own numbers, not
    # a borrowed resolution allowance.
    from docsforge.core import resolver
    from docsforge.tools import forge_tools, harvest_jobs
    for mod in (df, forge_tools, harvest_jobs):
        text = open(mod.__file__, encoding="utf-8").read()
        assert "Budget(" not in text, f"{mod.__name__} constructs a Budget"
    resolver_text = open(resolver.__file__, encoding="utf-8").read()
    assert "Budget()" in resolver_text, "Layer 1 must be bounded"
