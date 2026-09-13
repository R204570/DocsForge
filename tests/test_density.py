"""Telling a set of stubs from documentation, by arithmetic.

The threshold is not tuned against the example that raised the question. It
was measured over every corpus in a real store:

     median  <500ch  pages   corpus
        169     65%   1799   google-adk   split dump fragments
        490     53%    560   langchain    one API symbol per page
      2,112      0%     87   gin-gonic
      3,896     11%    385   astro
      3,973      5%    703   effect
      4,411      0%     13   mojo
      6,879      1%     85   pydantic
     17,870      0%     16   go-net-http

Everything genuine sits at 2,112 or above; both stub corpora at 490 or below.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import llmsfinder as lf


def _corpus(median: int, n: int = 60) -> list[int]:
    """n pages scattered either side of `median`, so the median is real."""
    return [median + (i % 11 - 5) * (median // 20 or 1) for i in range(n)]


# ── the measured corpora, as cases ──────────────────────────────

def test_the_two_stub_corpora_are_recognised():
    assert lf.reads_as_stubs(_corpus(169, 1799)), "google-adk: split dump fragments"
    assert lf.reads_as_stubs(_corpus(490, 560)), "langchain: one API symbol per page"


def test_every_genuine_corpus_is_left_alone():
    for median, pages, name in ((2_112, 87, "gin-gonic"), (3_896, 385, "astro"),
                                (3_973, 703, "effect"), (6_879, 85, "pydantic"),
                                (17_870, 16, "go-net-http")):
        if pages < lf.STUB_MIN_PAGES:
            continue
        assert not lf.reads_as_stubs(_corpus(median, pages)), name


def test_the_threshold_sits_in_the_gap_not_on_an_edge():
    """1,200 has better than 2x clearance on both sides of a 4.3x gap."""
    assert 490 * 2 < lf.STUB_MEDIAN < 2_112 / 1.7


# ── it must not fire on things that are merely small ────────────

def test_a_handful_of_short_pages_is_not_a_claim():
    """A README and three guides is an ordinary corpus, not a symbol index."""
    assert not lf.reads_as_stubs([300, 420, 380, 510])


def test_one_enormous_page_is_never_stubs():
    assert not lf.reads_as_stubs([3_400_000])


def test_an_empty_corpus_says_nothing():
    assert lf.reads_as_stubs([]) is False


# ── what it says when it fires ──────────────────────────────────

def test_the_note_names_the_numbers_it_judged_on():
    note = lf.density_note(_corpus(490, 560))
    assert note
    assert "560 pages" in note
    assert "under 500" in note
    # It must not read as a refusal: a symbol index is real documentation.
    assert "useful for looking up" in note


def test_a_healthy_corpus_gets_no_note():
    assert lf.density_note(_corpus(4_411, 200)) == ""


def test_the_note_suggests_the_next_step_rather_than_deciding_it():
    """Reporting, not acting. Refusing would discard a real corpus over a
    threshold; switching sites silently would be guessing at the intent."""
    note = lf.density_note(_corpus(169, 1799))
    assert "check whether the project publishes them separately" in note
