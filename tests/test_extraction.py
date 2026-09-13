"""
Offline tests for Phase D adaptive extraction — no network.

Phase B measured that 2.8% of pages on correctly-resolved sites were silently
storing navigation as documentation, and 61% on wrongly-resolved ones. Phase D's
job is to stop guessing when it cannot tell, and to say so. So the tests that
matter here are the ones asserting a *refusal*, not the ones asserting success.
"""

import json
import os
import sys
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df

LONG = "Documentation body text. " * 20
NAV = "".join(f"<a href='/p{i}'>A navigation entry {i}</a>" for i in range(40))


def page(inner: str, head: str = "") -> str:
    return f"<html><head><title>T</title>{head}</head><body>{inner}</body></html>"


# ── density scoring ──────────────────────────────────────
def test_navigation_is_not_documentation():
    # The measured defect: an unrecognised template stored <body> whole, so a
    # sidebar full of links became a stored "page" of documentation.
    with pytest.raises(df.ForgeError, match="reads like documentation"):
        df._html_to_md(page(f"<div class='x'>{NAV}</div>"), "https://x.dev/a")


def test_prose_in_an_unrecognised_container_is_still_documentation():
    title, body = df._html_to_md(page(f"<div class='x'>{LONG}</div>"), "https://x.dev/a")
    assert title == "T"
    assert "Documentation body text" in body


def test_a_short_page_is_still_documentation():
    # No length floor. An API stub or a one-line note is brief, not chrome —
    # rejecting it for being short would discard real pages to catch navigation.
    _title, body = df._html_to_md(page("<p>hi</p>"), "https://x.dev/a")
    assert "hi" in body


def test_density_separates_prose_from_links():
    soup = df._soup(page(f"<div id='nav'>{NAV}</div><div id='doc'>{LONG}</div>"))
    nav = soup.select_one("#nav")
    doc = soup.select_one("#doc")
    assert df.density(doc) > df.DENSITY_FLOOR
    assert df.density(nav) < df.DENSITY_FLOOR


def test_a_recognised_container_still_wins_before_scoring():
    # Density is the fallback, not the policy. Where a CONTENT selector matches,
    # extraction must behave exactly as it always did.
    _el, selector = df.pick_main(df._soup(page(f"<main>{LONG}</main>")))
    assert selector == "main"


# ── JS shells ────────────────────────────────────────────
def test_an_empty_page_beside_a_bundle_is_a_shell():
    assert df._looks_like_shell(page("<div id='root'></div><script src='/b.js'></script>"))
    # No bundle: a thin page, not a shell — rendering it would not help.
    assert not df._looks_like_shell(page("<div id='root'></div>"))
    # Real content: not a shell however much JavaScript it also ships.
    assert not df._looks_like_shell(page(f"<main>{LONG}</main><script src='/b.js'></script>"))


class ShellFetcher:
    """Serves an empty shell over HTTP and real content when rendered."""

    def __init__(self):
        self.rendered = 0

    def html(self, url):
        return page("<div id='root'></div><script src='/bundle.js'></script>")

    def render(self, url):
        self.rendered += 1
        return page(f"<main>{LONG}</main>")


def test_a_shell_is_retried_rendered_exactly_once():
    fetcher = ShellFetcher()
    title, body = df._extract_page("https://x.dev/a", fetcher, df.Options())
    assert "Documentation body text" in body
    assert fetcher.rendered == 1, "one retry, not a retry loop"


def test_a_run_that_already_renders_does_not_retry():
    # --js already rendered it. If that still produced nothing, rendering again
    # is not going to help and the page is genuinely unextractable.
    fetcher = ShellFetcher()
    with pytest.raises(df.ForgeError):
        df._extract_page("https://x.dev/a", fetcher, df.Options(js=True))
    assert fetcher.rendered == 0


# ── the site's own index ─────────────────────────────────
def test_a_mkdocs_search_index_gives_the_sites_own_page_list():
    payload = json.dumps({"docs": [
        {"location": "intro/"}, {"location": "guide/"},
        {"location": "guide/#anchor"},          # same page, different anchor
        {"location": "api/"},
    ]})
    pages = df._mkdocs_pages(payload, "https://x.dev/docs/")
    assert pages == ["https://x.dev/docs/intro/", "https://x.dev/docs/guide/",
                     "https://x.dev/docs/api/"]


def test_a_broken_search_index_yields_nothing_rather_than_raising():
    assert df._mkdocs_pages("not json at all", "https://x.dev/") == []


def _objects_inv(entries: list[str]) -> bytes:
    head = (b"# Sphinx inventory version 2\n# Project: x\n# Version: 1\n"
            b"# The remainder of this file is compressed using zlib.\n")
    return head + zlib.compress("\n".join(entries).encode("utf-8"))


def test_a_sphinx_inventory_gives_the_sites_own_page_list():
    raw = _objects_inv([
        "json.loads py:function 1 library/json.html#json.loads -",
        "json.dumps py:function 1 library/json.html#json.dumps -",
        "os.path py:module 1 library/os.path.html#$ -",     # $ = anchor is the name
    ])
    pages = df._sphinx_pages(raw, "https://docs.x.org/3/")
    assert pages == ["https://docs.x.org/3/library/json.html",
                     "https://docs.x.org/3/library/os.path.html"]


def test_a_corrupt_inventory_yields_nothing_rather_than_raising():
    assert df._sphinx_pages(b"nonsense with no zlib marker", "https://x.dev/") == []


# ── the frontier ─────────────────────────────────────────
def test_the_frontier_reorders_but_never_drops():
    # Invariant 7. Ordering decides which pages a truncated harvest gets; it
    # must never decide which pages are eligible at all.
    urls = ["https://x.dev/docs/a", "https://x.dev/blog/post",
            "https://x.dev/docs/deep/b/c", "https://x.dev/changelog/2024",
            "https://x.dev/guide/start"]
    frontier = df._Frontier(urls[0])
    for u in urls[1:]:
        frontier.append(u)

    drained = []
    while len(frontier):
        drained.append(frontier.popleft())

    assert set(drained) == set(urls), "the frontier dropped a page"
    assert len(drained) == len(urls)


def test_documentation_is_crawled_before_the_changelog():
    frontier = df._Frontier("https://x.dev/changelog/2024")
    frontier.append("https://x.dev/docs/intro")
    assert frontier.popleft() == "https://x.dev/docs/intro"


def test_shallow_pages_come_before_deep_ones():
    frontier = df._Frontier("https://x.dev/docs/a/b/c/d")
    frontier.append("https://x.dev/docs/intro")
    assert frontier.popleft() == "https://x.dev/docs/intro"


def test_the_frontier_never_queues_the_same_page_twice():
    frontier = df._Frontier("https://x.dev/docs/a")
    frontier.append("https://x.dev/docs/a")
    assert len(frontier) == 1
    assert "https://x.dev/docs/a" in frontier


# ── the closed loop: the plan revises itself mid-crawl ────
from docsforge.core.observation import Ledger, Observation


def obs(**kw) -> Observation:
    kw.setdefault("url", "https://x.dev/docs/a")
    return Observation(**kw)


def ledger_of(*observations) -> Ledger:
    led = Ledger()
    for o in observations:
        led.record(o)
    return led


def test_r1_a_hypothesis_that_stops_fitting_is_withdrawn():
    # The entry page is the least representative page on any docs site. A plan
    # seeded from it has to be able to be wrong.
    plan = df.Plan(generator="mkdocs")
    led = ledger_of(*[obs(selector="", signature="s1") for _ in range(6)])

    changed = plan.revise(led)

    assert plan.generator == ""
    assert any("withdrew" in line for line in changed)


def test_r1_leaves_a_hypothesis_that_is_still_fitting():
    plan = df.Plan(generator="mkdocs")
    led = ledger_of(*[obs(selector="main", signature="s1", chars=3000)
                      for _ in range(6)])
    plan.revise(led)
    assert plan.generator == "mkdocs"


def test_r2_a_selector_that_keeps_winning_is_pinned_to_its_template():
    plan = df.Plan()
    led = ledger_of(*[obs(selector=".doc-content", signature="body>div>main",
                          chars=3000) for _ in range(4)])

    changed = plan.revise(led)

    assert plan.pinned["body>div>main"] == ".doc-content"
    assert any("pinned" in line for line in changed)


def test_r3_a_low_scoring_unrecognised_template_is_routed_to_density():
    plan = df.Plan()
    # Nearly all link text, barely any prose: this template is not being read.
    led = ledger_of(*[obs(selector="", signature="s-nav", chars=100,
                          link_text_ratio=0.95) for _ in range(4)])

    changed = plan.revise(led)

    assert "s-nav" in plan.density_clusters
    assert any("density" in line for line in changed)


def test_r4_a_run_of_shells_switches_the_crawl_to_rendered_fetching():
    plan = df.Plan()
    led = ledger_of(*([obs(selector="", shell=True, signature="s1")] * 5
                      + [obs(selector="main", chars=3000, signature="s2")] * 5))

    changed = plan.revise(led)

    assert plan.render is True
    assert any("rendered" in line for line in changed)


def test_r4_does_not_fire_on_an_occasional_shell():
    plan = df.Plan()
    led = ledger_of(*([obs(selector="", shell=True, signature="s1")]
                      + [obs(selector="main", chars=3000, signature="s2")] * 9))
    plan.revise(led)
    assert plan.render is False


def test_r5_the_yield_map_is_per_path_neighbourhood():
    led = ledger_of(
        obs(url="https://x.dev/reference/a", selector="main", chars=6000),
        obs(url="https://x.dev/reference/b", selector="main", chars=6000),
        obs(url="https://x.dev/blog/c", selector="", chars=80,
            link_text_ratio=0.95),
    )
    found = df.Plan.refresh_yield(led)
    assert found["/reference"] > found["/blog"]


def test_a_revision_is_recorded_not_just_applied():
    # Invariant 11. A crawler that changes its plan can change what it was
    # measuring, and the defence is that every change is disclosed.
    plan = df.Plan(generator="mkdocs")
    plan.revise(ledger_of(*[obs(selector="", signature="s1") for _ in range(6)]))
    assert plan.revisions and "withdrew" in plan.revisions[0]


def test_too_few_pages_to_judge_changes_nothing():
    plan = df.Plan(generator="mkdocs")
    assert plan.revise(ledger_of(obs(selector=""), obs(selector=""))) == []
    assert plan.generator == "mkdocs"


# ── the frontier acts on what the plan learned ───────────
def test_reprioritise_reorders_without_dropping_anything():
    urls = [f"https://x.dev/docs/p{i}" for i in range(5)] + \
           ["https://x.dev/reference/deep/a"]
    frontier = df._Frontier(urls[0])
    for u in urls[1:]:
        frontier.append(u)

    frontier.reprioritise({"/reference/deep": 1.0, "/docs/p1": 0.0})

    drained = []
    while len(frontier):
        drained.append(frontier.popleft())
    assert set(drained) == set(urls), "reprioritising dropped a page"


def test_a_high_yield_neighbourhood_is_crawled_first():
    frontier = df._Frontier("https://x.dev/docs/misc/b")
    frontier.append("https://x.dev/docs/reference/a")
    frontier.reprioritise({"/docs/reference": 1.0, "/docs/misc": 0.0})
    assert frontier.popleft() == "https://x.dev/docs/reference/a"


def test_yield_never_promotes_a_changelog_above_the_manual():
    # Bounded on purpose: a couple of well-written release notes must not
    # outrank the documentation.
    frontier = df._Frontier("https://x.dev/changelog/2024/a")
    frontier.append("https://x.dev/docs/intro/b")
    frontier.reprioritise({"/changelog/2024": 1.0, "/docs/intro": 0.0})
    assert frontier.popleft() == "https://x.dev/docs/intro/b"


# ── re-extraction: the same page, read differently ───────
def test_a_pinned_selector_overrides_the_content_order():
    # `main` matches first and holds navigation; `.doc-content` is the real
    # body. Twelve sibling pages are what teach the crawl that.
    html = page(f"<main>{NAV}</main><div class='doc-content'>{LONG}</div>")
    soup = df._soup(html)
    df.strip_chrome(soup)
    plain, selector = df.pick_main(soup)
    assert selector == "main"

    plan = df.Plan()
    plan.pinned[df_ancestry(plain)] = ".doc-content"

    soup2 = df._soup(html)
    df.strip_chrome(soup2)
    _el, pinned_selector = df.pick_main(soup2, plan=plan)
    assert pinned_selector == ".doc-content"


def test_a_template_routed_to_density_stops_using_the_selector_list():
    html = page(f"<main>{NAV}</main><div class='doc-content'>{LONG}</div>")
    soup = df._soup(html)
    df.strip_chrome(soup)
    plain, _ = df.pick_main(soup)

    plan = df.Plan()
    plan.density_clusters.add(df_ancestry(plain))

    soup2 = df._soup(html)
    df.strip_chrome(soup2)
    _el, selector = df.pick_main(soup2, plan=plan)
    assert selector == "density"


def df_ancestry(el):
    from docsforge.core.observation import ancestry
    return ancestry(el)


# ── R6: the density floor is learned, not assumed ────────
def stripped(html):
    soup = df._soup(html)
    df.strip_chrome(soup)
    return soup


def test_the_floor_is_the_constant_until_a_template_has_a_distribution():
    plan = df.Plan()
    assert plan.floor_for("body>div>main") == df.DENSITY_FLOOR
    # Four pages is not a distribution.
    plan.revise(ledger_of(*[obs(signature="s1", density_score=0.2) for _ in range(4)]))
    assert plan.floor_for("s1") == df.DENSITY_FLOOR


def test_a_uniformly_dense_template_lowers_its_own_floor():
    # An API reference where every page is mostly signatures and anchor links
    # scores low throughout. One global floor refuses the whole corpus.
    plan = df.Plan()
    scores = [0.18, 0.20, 0.19, 0.22, 0.21, 0.20]
    changed = plan.revise(ledger_of(*[obs(signature="ref", density_score=s)
                                      for s in scores]))

    assert plan.floor_for("ref") < df.DENSITY_FLOOR
    assert any("density floor" in line for line in changed)


def test_a_uniformly_wordy_template_raises_its_own_floor():
    # A tutorial site scores high throughout, so the constant never catches its
    # navigation. The floor has to be able to move up as well as down.
    plan = df.Plan()
    scores = [0.74, 0.76, 0.75, 0.78, 0.73, 0.75]
    plan.revise(ledger_of(*[obs(signature="prose", density_score=s)
                            for s in scores]))
    assert plan.floor_for("prose") > df.DENSITY_FLOOR


def test_a_fitted_floor_is_clamped_to_something_sane():
    plan = df.Plan()
    low, high = df.Plan.FLOOR_RANGE
    plan.revise(ledger_of(*[obs(signature="empty", density_score=0.0)
                            for _ in range(6)]))
    plan.revise(ledger_of(*[obs(signature="perfect", density_score=1.0)
                            for _ in range(6)]))
    assert plan.floor_for("empty") >= low
    assert plan.floor_for("perfect") <= high


def test_fitting_a_floor_is_recorded_like_any_other_revision():
    # Invariant 11. A threshold that moved silently is a threshold nobody can
    # audit when a harvest later looks wrong.
    plan = df.Plan()
    plan.revise(ledger_of(*[obs(signature="ref", density_score=0.2)
                            for _ in range(6)]))
    assert any("density floor" in line for line in plan.revisions)


def test_each_template_gets_its_own_floor():
    plan = df.Plan()
    plan.revise(ledger_of(
        *[obs(signature="ref", density_score=0.2) for _ in range(6)],
        *[obs(signature="prose", density_score=0.75) for _ in range(6)]))
    assert plan.floor_for("ref") < plan.floor_for("prose")


def test_a_learned_floor_actually_changes_what_is_extracted():
    # The whole point: a container the constant refuses is accepted once the
    # site has shown that this is simply how its pages score.
    html = page("<div class='ref'>" + NAV + "<p>sig(x) returns y. " * 12 + "</p></div>")
    assert df.pick_main(stripped(html))[1] == "", "fixture must fail the constant"

    best, best_score = None, 0.0
    for el in df._density_candidates(stripped(html)):
        scored = df.density(el)
        if scored > best_score:
            best, best_score = el, scored

    from docsforge.core.observation import ancestry
    plan = df.Plan()
    plan.floors[ancestry(best)] = round(best_score - 0.01, 3)

    assert df.pick_main(stripped(html), plan=plan)[1] == "density"


def test_the_score_a_floor_is_fitted_to_is_the_one_it_is_checked_against():
    # Two nearly-identical scoring functions is how a fitted threshold quietly
    # stops meaning what it was fitted to mean.
    soup = stripped(page(f"<main>{LONG}</main>"))
    main = soup.select_one("main")
    report = df._measure(main, "main", "https://x.dev/a", "T")
    assert report["density_score"] == pytest.approx(df.density(main), abs=1e-4)
    assert Observation(**report).score() == pytest.approx(df.density(main), abs=1e-4)


def test_a_tight_distribution_does_not_refuse_half_the_site():
    # The bug this margin exists for, caught on docs.astro.build: `median -
    # k*MAD` collapses onto the median whenever a template scores consistently,
    # so the floor landed at 0.60 against a median of ~0.75 and would have
    # refused the bottom half of the site's own documentation.
    plan = df.Plan()
    tight = [0.74, 0.75, 0.75, 0.76, 0.75, 0.75]
    plan.revise(ledger_of(*[obs(signature="prose", density_score=s) for s in tight]))

    fitted = plan.floor_for("prose")
    median = 0.75
    assert fitted <= median * df.Plan.FLOOR_MARGIN + 1e-9, (
        f"floor {fitted} sits too close to the median {median}")
    # A page has to be unusually poor for its own site, not merely below average.
    assert fitted < min(tight)
