"""
Offline tests for Phase F selection — no network.

Two properties carry this layer, and both are easy to erode later:

  * **Invariant 4.** Choosing which corpora enter scope is a declaration that
    gets recorded. Dropping pages inside a selected corpus is filtering and is
    forbidden. Once `intent` exists, every future request to "just skip the
    irrelevant pages" will sound reasonable.
  * **Invariant 10.** When it cannot tell what is needed it asks, and if it
    cannot ask it refuses and returns the options. It never guesses, because a
    silently truncated harvest that looks successful is worse than a refusal.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import selection as sel
from docsforge.core.federation import Corpus
from docsforge.core.selection import Selection, select, usable_for_planning


def corpus(kind: str, url: str = "", confidence: float = 0.8,
           magnitude: int = 100) -> Corpus:
    return Corpus(url=url or f"https://x.dev/{kind}/", kind=kind,
                  kind_confidence=confidence, magnitude=magnitude)


# ── intent decides scope ─────────────────────────────────
def test_resolve_import_takes_the_reference_and_leaves_the_tutorial():
    corpora = [corpus("api"), corpus("sdk"), corpus("guide"),
               corpus("cookbook"), corpus("changelog")]

    got = select(corpora, intent="resolve-import")

    assert not got.needs_selection
    assert {c.kind for c in got.selected} == {"api", "sdk"}


def test_an_unselected_corpus_is_declared_not_dropped():
    # Invariant 5. The guide is not harvested, and is not silently absent
    # either — it is listed with its magnitude and marked.
    corpora = [corpus("api"), corpus("guide", magnitude=703)]
    select(corpora, intent="resolve-import")

    guide = [c for c in corpora if c.kind == "guide"][0]
    assert guide.selected is False
    assert "not requested" in guide.line()
    assert "703" in guide.line()


def test_the_default_intent_authorises_nothing_to_be_left_out():
    # A caller who has not said what they are doing has not asked for a subset.
    corpora = [corpus("api"), corpus("guide"), corpus("cookbook"), corpus("spec")]
    got = select(corpora)
    assert got.intent == "reference"
    assert len(got.selected) == 4


def test_selection_has_no_way_to_drop_pages_inside_a_corpus():
    # Invariant 4, enforced structurally. If a page-level filter ever appears
    # in this module's surface, this test is the thing that should stop it.
    source = open(sel.__file__, encoding="utf-8").read()
    for forbidden in ("def filter_pages", "max_pages", "page_limit", "skip_page"):
        assert forbidden not in source, f"selection.py grew {forbidden}"


# ── escalation is rare by construction ───────────────────
def test_a_single_corpus_never_escalates():
    got = select([corpus("api")], intent="resolve-import")
    assert not got.needs_selection
    assert len(got.selected) == 1


def test_an_unambiguous_technology_never_escalates():
    # A manual, a specification and one API reference: each mandatory kind is
    # matched exactly once, so there is nothing to ask about.
    corpora = [corpus("guide"), corpus("spec"), corpus("api"), corpus("sdk")]
    for intent in ("resolve-import", "implement", "learn", "reference"):
        assert not select(corpora, intent=intent).needs_selection, intent


def test_a_tie_for_a_mandatory_kind_escalates():
    corpora = [corpus("api", "https://x.dev/rest/"),
               corpus("api", "https://x.dev/graphql/"),
               corpus("sdk")]

    got = select(corpora, intent="resolve-import")

    assert got.needs_selection and got.trigger == "tie"
    assert len(got.options) == 2


def test_low_confidence_on_a_mandatory_kind_escalates():
    corpora = [corpus("api", confidence=0.2), corpus("sdk"), corpus("guide")]
    got = select(corpora, intent="resolve-import")
    assert got.needs_selection and got.trigger == "confidence"


def test_a_platform_scale_name_escalates_on_breadth():
    corpora = [corpus("guide", f"https://x.dev/svc{i}/") for i in range(12)]
    got = select(corpora, intent="reference")
    assert got.needs_selection and got.trigger == "breadth"
    # Options ordered by magnitude, so a caller can see what is big.
    assert got.options == sorted(got.options, key=lambda c: -c.magnitude)


def test_the_question_names_kinds_and_sizes_not_just_urls():
    corpora = [corpus("api", "https://x.dev/rest/", magnitude=400),
               corpus("api", "https://x.dev/graphql/", magnitude=90),
               corpus("sdk")]
    got = select(corpora, intent="resolve-import")
    question = got.question()
    assert "https://x.dev/rest/" in question and "api" in question
    assert "400" in question


# ── the caller decides ───────────────────────────────────
def test_an_explicit_choice_is_never_second_guessed():
    corpora = [corpus("api"), corpus("guide"), corpus("changelog")]
    got = select(corpora, intent="resolve-import",
                 explicit=["https://x.dev/changelog/"])
    assert [c.kind for c in got.selected] == ["changelog"]
    assert not got.needs_selection


def test_a_stored_policy_answers_the_question_once():
    corpora = [corpus("api", "https://x.dev/rest/"),
               corpus("api", "https://x.dev/graphql/"), corpus("sdk")]
    assert select(corpora, intent="resolve-import").needs_selection

    # The policy records what existed as well as what was chosen, so graphql
    # is understood as rejected rather than as a surprise.
    got = select(corpora, intent="resolve-import",
                 policy={"chosen": ["https://x.dev/rest/", "https://x.dev/sdk/"],
                         "known": [c.url for c in corpora]})
    assert not got.needs_selection and got.from_policy
    assert {c.kind for c in got.selected} == {"api", "sdk"}


def test_a_stored_policy_must_not_silently_exclude_a_new_corpus():
    # PROPOSAL-II's open question, answered: it must not. A corpus of a
    # mandatory kind that appeared after the choice was made re-triggers it.
    corpora = [corpus("api", "https://x.dev/rest/"), corpus("sdk"),
               corpus("api", "https://x.dev/brand-new/")]
    got = select(corpora, intent="resolve-import",
                 policy={"chosen": ["https://x.dev/rest/", "https://x.dev/sdk/"],
                         "known": ["https://x.dev/rest/", "https://x.dev/sdk/"]})
    assert got.needs_selection
    assert "appeared since" in got.reason


# ── the FlowIT contract ──────────────────────────────────
def test_a_complete_mandatory_corpus_is_usable_for_planning():
    api, sdk = corpus("api"), corpus("sdk")
    got = select([api, sdk], intent="resolve-import")
    api.settle(10, 10)
    sdk.settle(4, 4)

    ok, why = usable_for_planning(got, [api, sdk])
    assert ok is True and "complete" in why


def test_an_incomplete_mandatory_corpus_is_not_usable_for_planning():
    api, sdk = corpus("api"), corpus("sdk")
    got = select([api, sdk], intent="resolve-import")
    api.settle(3, 10)
    sdk.settle(4, 4)

    ok, why = usable_for_planning(got, [api, sdk])
    assert ok is False and "INCOMPLETE" in why


def test_unknown_coverage_is_not_usable_for_planning():
    # `unknown` is not `complete`. A plan map generated from a harvest that
    # never established how much existed is exactly the unearned confidence
    # this project refuses to express.
    api, sdk = corpus("api"), corpus("sdk")
    got = select([api, sdk], intent="resolve-import")
    api.settle(10, None)
    sdk.settle(4, 4)

    ok, why = usable_for_planning(got, [api, sdk])
    assert ok is False and "unknown" in why


def test_a_missing_mandatory_kind_is_not_usable_for_planning():
    guide = corpus("guide")
    got = select([guide], intent="resolve-import")
    ok, why = usable_for_planning(got, [guide])
    assert ok is False


def test_an_unresolved_selection_is_never_usable_for_planning():
    corpora = [corpus("api", "https://x.dev/a/"), corpus("api", "https://x.dev/b/"),
               corpus("sdk")]
    got = select(corpora, intent="resolve-import")
    ok, why = usable_for_planning(got, corpora)
    assert got.needs_selection and ok is False


# ── remembering ──────────────────────────────────────────
def test_a_remembered_policy_survives_and_can_be_cleared():
    sel.remember_policy("effect", ["https://x.dev/api/"],
                        known=["https://x.dev/api/", "https://x.dev/guide/"])
    stored = sel.recall_policy("effect")
    assert stored["chosen"] == ["https://x.dev/api/"]
    assert "https://x.dev/guide/" in stored["known"]

    said = sel.forget_selection("effect")

    assert "effect" in said
    assert sel.recall_policy("effect") is None


def test_forgetting_everything_empties_the_policy_store():
    sel.remember_policy("a", ["https://a/"])
    sel.remember_policy("b", ["https://b/"])
    assert "2" in sel.forget_selection()
    assert sel.recall_policy("a") is None


# ── asking, and refusing only when there is nobody to ask ──
def test_an_installed_channel_is_used_before_refusing():
    # Invariant 10 in the order it is stated: ask first, refuse second.
    corpora = [corpus("api", "https://x.dev/rest/"),
               corpus("api", "https://x.dev/graphql/"), corpus("sdk")]
    got = select(corpora, intent="resolve-import")
    assert got.needs_selection

    asked = []

    def channel(selection):
        asked.append(selection)
        return ["https://x.dev/rest/"]

    sel.set_asker(channel)
    try:
        answer = sel.ask(got)
    finally:
        sel.set_asker(None)

    assert answer == ["https://x.dev/rest/"]
    assert asked and asked[0].trigger == "tie"


def test_no_channel_means_no_answer_rather_than_a_guess():
    corpora = [corpus("api", "https://x.dev/rest/"),
               corpus("api", "https://x.dev/graphql/"), corpus("sdk")]
    got = select(corpora, intent="resolve-import")

    sel.set_asker(None)
    # Nothing is attached and the suite has no tty, so there is nobody to ask.
    assert sel.ask(got) is None


def test_a_channel_that_fails_refuses_rather_than_guesses():
    corpora = [corpus("api", "https://x.dev/a/"), corpus("api", "https://x.dev/b/"),
               corpus("sdk")]
    got = select(corpora, intent="resolve-import")

    def broken(selection):
        raise RuntimeError("the client went away")

    sel.set_asker(broken)
    try:
        assert sel.ask(got) is None
    finally:
        sel.set_asker(None)


def test_nothing_to_decide_is_never_asked_about():
    got = select([corpus("api")], intent="resolve-import")
    calls = []
    sel.set_asker(lambda s: calls.append(s) or [])
    try:
        assert sel.ask(got) is None
    finally:
        sel.set_asker(None)
    assert calls == [], "asked a question that had already been answered"


def test_the_refusal_carries_everything_needed_to_answer_it():
    # Over MCP the channel is the model: it relays the question and calls back
    # with `corpora=`. That round trip only works if the refusal says how.
    corpora = [corpus("api", "https://x.dev/rest/", magnitude=400),
               corpus("api", "https://x.dev/graphql/", magnitude=90),
               corpus("sdk")]
    question = select(corpora, intent="resolve-import").question()

    assert "corpora=" in question
    assert "https://x.dev/rest/" in question
    assert "400" in question


def test_adk_is_a_kind_of_its_own():
    from docsforge.core import federation as fed
    kind, confidence = fed.classify_kind("https://x.dev/adk/python/")
    assert kind == "adk" and confidence > 0.5
    # And an intent can take the API and SDK without dragging it in.
    got = select([corpus("api"), corpus("sdk"), corpus("adk")],
                 intent="resolve-import")
    assert {c.kind for c in got.selected} == {"api", "sdk", "adk"}
