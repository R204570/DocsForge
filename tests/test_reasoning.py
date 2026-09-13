"""
Tests for PROPOSAL-3 Phase 4 — bounded reasoning.

The rule this phase must not break is the one that makes it affordable: **no
model call per page.** A call per page on a 1,200-page site is the bill this
whole project exists not to send. So the tests that matter most here are the
ones about *not* calling: the cache, the budget, and the fact that everything
still works with reasoning off.

That last one is the phase's acceptance criterion — "a harvest with reasoning
disabled behaves exactly as Phase 3 did" — and it is checked by the other 611
tests in this suite, every one of which runs with reasoning off and none of
which mentions it. What is checked here is that turning it *on* stays bounded.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.tools import forge_tools as ft
from docsforge.core import reasoning
from docsforge.core.reasoning import Budget, Reasoner


class Model:
    """A provider that answers from a script and counts what it was asked."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else "unsure"


def _on(model, calls: int = 12) -> Reasoner:
    return Reasoner(budget=Budget(calls=calls), provider=model, on=True)


# ── off by default, and off means untouched ──────────────
def test_reasoning_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("DOCSFORGE_REASONING", raising=False)
    assert reasoning.configured() is False
    assert reasoning.current().enabled() is False


def test_an_env_flag_alone_is_not_enough(monkeypatch):
    # Two independent switches: the operator says whether to spend anything,
    # the provider layer says whether there is anything to spend it on.
    monkeypatch.setenv("DOCSFORGE_REASONING", "on")
    monkeypatch.setattr("docsforge.providers.PROVIDERS", [])
    assert reasoning.configured() is False


def test_asking_while_off_costs_nothing_and_returns_the_fallback():
    model = Model("div.content")
    off = Reasoner(provider=model, on=False)

    assert off.ask("m", "k", "q?", fallback="algorithmic") == "algorithmic"
    assert model.asked == []
    assert off.log == [], "an unasked question is not a consultation"


# ── bounded ──────────────────────────────────────────────
def test_the_budget_is_a_hard_cap():
    model = Model(*[f"answer {i}" for i in range(20)])
    r = _on(model, calls=3)

    got = [r.ask("m", f"key{i}", "q?", fallback="fell back") for i in range(6)]

    assert model.asked and len(model.asked) == 3
    assert got[:3] == ["answer 0", "answer 1", "answer 2"]
    assert got[3:] == ["fell back"] * 3, "past the cap, fall back — never stall"


def test_an_exhausted_budget_is_recorded_rather_than_silent():
    r = _on(Model("one"), calls=1)
    r.ask("m", "a", "q?")
    r.ask("m", "b", "q?", fallback="x")

    assert "budget" in r.log[-1].failed
    assert "budget" in r.note()


# ── cached by cluster and by host, never by page ─────────
def test_one_call_per_key_however_many_pages():
    model = Model("div.content")
    r = _on(model)

    for _ in range(500):
        assert r.ask("unrecognised template", "layout-A", "q?") == "div.content"

    assert len(model.asked) == 1, "a 500-page site must cost one call, not 500"
    assert r.budget.spent == 1


def test_a_second_template_costs_a_second_call():
    model = Model("div.content", "main.doc")
    r = _on(model)

    assert r.ask("unrecognised template", "layout-A", "q?") == "div.content"
    assert r.ask("unrecognised template", "layout-B", "q?") == "main.doc"
    assert r.budget.spent == 2


def test_a_cache_hit_is_recorded_as_one():
    r = _on(Model("div.content"))
    r.ask("m", "k", "q?")
    r.ask("m", "k", "q?")

    assert [c.cached for c in r.log] == [False, True]


# ── the model proposes, the code disposes ────────────────
def test_an_answer_that_does_not_validate_falls_back():
    r = _on(Model("nonsense"))
    got = r.ask("m", "k", "q?", fallback="algorithmic",
                check=lambda a: a == "acceptable")

    assert got == "algorithmic"
    assert r.log[-1].failed == "answer did not validate"


def test_a_rejected_answer_is_not_cached():
    # Otherwise one bad answer is reused for every page of that template.
    model = Model("nonsense", "good")
    r = _on(model)
    check = lambda a: a == "good"

    assert r.ask("m", "k", "q?", fallback="f", check=check) == "f"
    assert r.ask("m", "k", "q?", fallback="f", check=check) == "good"
    assert len(model.asked) == 2


def test_a_provider_that_raises_costs_a_fallback_not_a_harvest():
    def explodes(question):
        raise RuntimeError("rate limited")

    r = _on(explodes)
    assert r.ask("m", "k", "q?", fallback="algorithmic") == "algorithmic"
    assert "rate limited" in r.log[-1].failed


# ── recorded ─────────────────────────────────────────────
def test_every_consultation_is_auditable():
    r = _on(Model("api"))
    r.ask("corpus kind", "https://x.dev/", "what kind?")

    entry = r.record()[0]
    assert entry["moment"] == "corpus kind"
    assert entry["answer"] == "api"
    assert "what kind?" in entry["question"]
    assert "corpus kind" in r.note()


# ── decision point 1: an unrecognised template ───────────
#: An API reference under an unrecognised template: real prose, and a great
#: many method links. The link ratio is what makes density refuse it, and
#: refusing is usually right — that ratio is what stops a sidebar being stored
#: as documentation. Here it is wrong, and the page is lost. This is decision
#: point 1's actual shape, not a contrived one: it was `pick_main` returning
#: `(None, "")` on pages like this that made a wrongly-resolved site store
#: navigation on 61% of its pages.
# Tuned deliberately: at this length density refuses the page (link ratio)
# while div.xyzzy still holds 677 characters, comfortably over
# MIN_MAIN_CHARS. Longer prose and density accepts <body> instead, and the
# decision point is never reached.
_PROSE = "Real documentation prose about the subject at hand. " * 4
_METHODS = " ".join(f'<a href="/m{i}">method{i}</a>' for i in range(60))
ODD = f"""<html><head><title>Odd</title></head><body>
<div class="nav"><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a></div>
<div class="xyzzy"><p>{_PROSE}</p>{_METHODS}</div>
</body></html>"""


def test_an_unrecognised_template_is_refused_when_reasoning_is_off():
    # Today's behaviour, and it stays the fallback.
    soup = df._soup(ODD)
    assert df.pick_main(soup) == (None, "")


def test_a_proposed_selector_is_used_when_it_validates():
    soup = df._soup(ODD)
    with reasoning.active(_on(Model("div.xyzzy"))):
        found, how = df.pick_main(soup)

    assert found is not None
    assert how == "reasoned:div.xyzzy"
    assert "Real documentation prose" in found.get_text()


def test_a_proposed_selector_that_matches_nothing_is_refused():
    soup = df._soup(ODD)
    with reasoning.active(_on(Model("div.does-not-exist"))):
        assert df.pick_main(soup) == (None, "")


def test_two_pages_of_one_template_ask_once():
    model = Model("div.xyzzy")
    with reasoning.active(_on(model)):
        for _ in range(20):
            df.pick_main(df._soup(ODD))
    assert len(model.asked) == 1


# ── decision point 4: a soft 404 ─────────────────────────
def test_a_short_error_shaped_page_is_worth_asking_about():
    assert df._error_shaped("Page not found", "404 - this page does not exist")
    assert not df._error_shaped("Error handling", "x" * 2000)
    assert not df._error_shaped("Retries", "how to retry a failed request")


def test_a_soft_404_is_stored_when_reasoning_is_off():
    # Today's behaviour, stated so the change is visible: a 200 that renders an
    # error is indistinguishable from documentation without reading it.
    assert df._is_error_page("Page not found", "404", "https://x.dev/a") is False


def test_a_soft_404_is_caught_when_reasoning_is_on():
    with reasoning.active(_on(Model("ERROR"))):
        assert df._is_error_page("Page not found", "404 not found",
                                 "https://x.dev/a") is True


def test_real_documentation_is_not_discarded_as_an_error():
    with reasoning.active(_on(Model("DOCUMENTATION"))):
        assert df._is_error_page("Error handling", "404 responses are retried",
                                 "https://x.dev/a") is False


# ── decision point 3: the identity gate may only veto ────
def test_reasoning_can_refuse_a_host_the_gate_admitted():
    with reasoning.active(_on(Model("DIFFERENT a Java build tool, not the "
                                    "Python package"))):
        veto = ft._reason_about_identity("maven", "https://maven.example/",
                                         "Maven is a build tool for Java.",
                                         "names it 9 times")
    assert veto
    assert "Java build tool" in veto


def test_reasoning_confirming_the_gate_changes_nothing():
    with reasoning.active(_on(Model("SAME it is the project's own docs"))):
        assert ft._reason_about_identity("effect", "https://effect.website/",
                                         "Effect is a TypeScript library.",
                                         "names it 12 times") == ""


def test_the_identity_gate_is_asked_once_per_host():
    model = Model("SAME it is the project's own docs")
    with reasoning.active(_on(model)):
        for _ in range(30):
            ft._reason_about_identity("effect", "https://effect.website/docs/",
                                      "Effect is a TypeScript library.", "r")
    assert len(model.asked) == 1


def test_an_unreadable_page_is_never_vetoed():
    # No evidence is not evidence of a different project. Vetoing on an empty
    # body would refuse every host whose landing page failed to extract.
    model = Model("DIFFERENT")
    with reasoning.active(_on(model)):
        assert ft._reason_about_identity("x", "https://x.dev/", "", "r") == ""
    assert model.asked == []


# ── the wiring ───────────────────────────────────────────
def _source(module) -> str:
    return open(module.__file__, encoding="utf-8").read()


def test_all_four_decision_points_are_reached():
    tools, crawler = _source(ft), _source(df)
    assert "_ask_for_selector(" in crawler          # 1
    assert "_reason_about_kind(" in tools           # 2
    assert "_reason_about_identity(" in tools       # 3
    assert "_is_error_page(" in crawler             # 4


def test_a_harvest_opens_exactly_one_budget():
    # Twelve calls per harvest, not per corpus — a federation must not multiply
    # the cap by the thing it is meant to bound.
    source = _source(ft)
    assert source.count("reasoning.Reasoner()") == 1
    assert "reasoning.active(reasoner)" in source


def test_consultations_reach_the_stats():
    assert 'stats["reasoning"]' in _source(ft)
