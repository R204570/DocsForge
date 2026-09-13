"""
Tests for PROPOSAL-3 Phase 2 — the coverage a reader actually sees.

Two wiring defects with the same shape: PROPOSAL-II built the honest answer,
tested it, and then showed the reader a different one.

  * **W1** — `Federation.complete` implements Invariant 9 and nothing called it.
    A harvest that got all of one corpus and half of another announced itself
    complete in its headline, with the shortfall visible only to a reader who
    scrolled to the note at the bottom.
  * **W4** — the entry corpus, already crawled and already stored, could be
    reported **"not requested"**, because `classify_kind` returns `""` for a
    docs root with no kind token in its path and an unclassified corpus matches
    no kind-specific intent.

Both are about the same failure: the summary contradicting the detail. A reader
who trusts the headline is the reader this product is for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import selection as sel
from docsforge.core.federation import Corpus, Federation


# ── W4: the entry corpus is never "not requested" ────────
def test_the_entry_corpus_is_never_marked_not_requested():
    # `resolve-import` mandates `api` and `sdk` and excludes `guide`, so the
    # unclassified entry corpus matches nothing — which is the W4 setup exactly.
    entry = Corpus(url="https://x.dev/docs/", entry=True)      # kind "" — typical
    api = Corpus(url="https://api.x.dev/", kind="api", kind_confidence=0.9)
    sdk = Corpus(url="https://x.dev/sdk/", kind="sdk", kind_confidence=0.9)

    result = sel.select([entry, api, sdk], intent="resolve-import")

    assert entry.selected, "the URL the caller handed in was requested by name"
    assert "not requested" not in entry.status
    assert entry in result.selected


def test_a_corpus_that_really_was_not_requested_still_says_so():
    # The fix must not turn Invariant 5 off for everything else.
    entry = Corpus(url="https://x.dev/docs/", entry=True)
    api = Corpus(url="https://api.x.dev/", kind="api", kind_confidence=0.9)
    sdk = Corpus(url="https://x.dev/sdk/", kind="sdk", kind_confidence=0.9)
    blog = Corpus(url="https://x.dev/blog/", kind="guide", kind_confidence=0.9,
                  magnitude=42)

    sel.select([entry, api, sdk, blog], intent="resolve-import")

    assert not blog.selected
    assert "not requested" in blog.status


def test_the_selection_result_agrees_with_the_marks():
    # If `result.selected` and `corpus.selected` disagree, the coverage note and
    # the harvest loop are working from different lists.
    entry = Corpus(url="https://x.dev/docs/", entry=True)
    api = Corpus(url="https://api.x.dev/", kind="api", kind_confidence=0.9)
    sdk = Corpus(url="https://x.dev/sdk/", kind="sdk", kind_confidence=0.9)
    blog = Corpus(url="https://x.dev/blog/", kind="guide", kind_confidence=0.9)
    every = [entry, api, sdk, blog]

    result = sel.select(every, intent="resolve-import")

    assert {c.url for c in result.selected} == \
           {c.url for c in every if c.selected}


# ── W1: the roll-up is the headline ──────────────────────
def _source() -> str:
    from docsforge.tools import forge_tools
    return open(forge_tools.__file__, encoding="utf-8").read()


def test_federation_completeness_reaches_the_headline():
    source = _source()
    assert 'stats["federation"]' in source, "the roll-up is never recorded"
    assert 'stats.get("federation")' in source, "the headline never reads it"


def test_the_federation_runs_before_the_headline_is_written():
    # Ordering is the whole fix: until `_federate` has run there is no
    # federation-level completeness to report, so a headline written before it
    # can only ever describe the entry corpus.
    source = _source()
    assert source.index("note = _federate(") < source.index('warning = ""')


def test_an_incomplete_peer_makes_the_federation_incomplete():
    # Invariant 9's roll-up, which is what the headline now reports.
    f = Federation(technology="x")
    entry = f.add(Corpus(url="https://x.dev/docs/", entry=True))
    entry.settle(10, 10)
    peer = f.add(Corpus(url="https://api.x.dev/"))
    peer.settle(5, 40)

    assert entry.complete is True
    assert f.complete is False, "one short corpus makes the federation short"


def test_an_unselected_corpus_does_not_drag_the_headline_down():
    # Invariant 5 records it; Invariant 9 does not count it against coverage.
    # Otherwise every harvest of a large platform reports INCOMPLETE forever.
    f = Federation(technology="x")
    entry = f.add(Corpus(url="https://x.dev/docs/", entry=True))
    entry.settle(10, 10)
    skipped = f.add(Corpus(url="https://api.x.dev/", magnitude=703))
    skipped.selected = False

    assert f.complete is True
