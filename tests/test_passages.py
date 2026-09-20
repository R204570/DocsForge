"""
Offline tests for Phase G read-time relevance — no network.

Invariant 6 is the one to hold: relevance is applied on retrieval, never on
ingestion. The stored corpus stays whole; only what is handed back is narrowed.
Trimming at harvest time would turn every completeness claim into a claim about
an undisclosed subset, which is the thing the coverage figure exists to prevent.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.core import passages as psg
from docsforge.store.kb_store import FileStore

PAGE = """# Retrying

Some preamble about the module.

## Error Handling

Errors are values here, not exceptions.

### Retrying

Use exponential backoff with a jitter. The retry policy is configurable.

## Layers

Layers wire services together with npm install.
"""


@pytest.fixture
def kb(tmp_path):
    ft.reset_store(FileStore(tmp_path))
    yield tmp_path
    ft.reset_store(None)


# ── chunking ─────────────────────────────────────────────
def test_a_page_splits_on_its_headings():
    found = psg.sections(PAGE, page_title="Retrying")
    paths = [s.heading_path for s in found]
    assert "Error Handling" in paths
    assert "Layers" in paths


def test_a_nested_heading_keeps_the_path_that_makes_it_quotable():
    # "Retrying" alone is ambiguous across a large manual; the path is what
    # lets a caller cite the answer rather than paraphrase it.
    found = psg.sections(PAGE)
    nested = [s for s in found if s.heading_path.endswith("> Retrying")]
    assert nested and nested[0].heading_path == "Error Handling > Retrying"


def test_content_before_the_first_heading_is_not_lost():
    found = psg.sections("Some intro text with no heading at all.\n\n## Later\n\nmore")
    assert any("intro text" in s.text for s in found)


def test_a_page_with_no_headings_is_one_section():
    found = psg.sections("Just a paragraph.", page_title="T")
    assert len(found) == 1 and found[0].text == "Just a paragraph."


def test_an_empty_page_yields_nothing():
    assert psg.sections("") == []


# ── ranking ──────────────────────────────────────────────
def test_a_section_titled_after_the_question_wins():
    found = psg.sections(PAGE, page_title="Retrying")
    best = psg.rank("retrying backoff", found, limit=1)
    assert best and "Retrying" in best[0].heading_path


def test_an_irrelevant_query_returns_nothing_rather_than_the_first_section():
    found = psg.sections(PAGE)
    assert psg.rank("kubernetes ingress controller", found) == []


def test_ranking_returns_passages_not_whole_pages():
    found = psg.sections(PAGE, page_title="Retrying")
    best = psg.rank("exponential backoff", found, limit=1)
    assert "Layers wire services" not in best[0].text


# ── the point of the exercise ────────────────────────────
def test_a_passage_costs_far_fewer_tokens_than_its_page():
    # The measurement, not an assertion of intent: one page of a generated API
    # reference can be 20k tokens spent answering a one-line question.
    big = PAGE + ("\n\n## Filler\n\n" + "unrelated prose. " * 2000)
    whole = psg.Section("", big, 0).tokens
    best = psg.rank("exponential backoff", psg.sections(big), limit=1)

    assert best
    assert best[0].tokens < whole / 10, (
        f"passage {best[0].tokens} tokens vs whole page {whole}")


# ── wiring ───────────────────────────────────────────────
def _store(kb):
    return ft.store().save(
        "effect", "v3", "https://x.dev/docs/", "crawl",
        [("Retrying", "https://x.dev/docs/reference/retry", PAGE),
         ("Tutorial", "https://x.dev/docs/guide/start",
          "## Getting Started\n\nInstall it and write your first exponential program.")],
        complete=True)


def test_search_returns_passages_with_heading_paths(kb):
    _store(kb)
    out = ft.tool_search_knowledge_base("exponential backoff")
    assert "Error Handling > Retrying" in out
    assert "passage" in out


def test_search_says_which_technology_answered(kb):
    # A search across the whole store that does not say which manual answered
    # is unciteable.
    _store(kb)
    out = ft.tool_search_knowledge_base("exponential backoff")
    assert "effect" in out and "v3" in out


def test_kind_filtering_keeps_tutorial_prose_out_of_an_api_answer(kb):
    _store(kb)
    out = ft.tool_search_knowledge_base("exponential", kind="api")
    assert "reference/retry" in out
    assert "guide/start" not in out


def test_a_kind_with_no_matching_pages_says_so_rather_than_guessing(kb):
    _store(kb)
    out = ft.tool_search_knowledge_base("exponential", kind="operations")
    assert "not of kind" in out or "No passage" in out


def test_retrieval_narrows_what_is_returned_not_what_is_stored(kb):
    # Invariant 6. Searching must never change the store.
    _store(kb)
    before = ft.store().entry("effect", "v3")["pages"]
    ft.tool_search_knowledge_base("exponential backoff", kind="api")
    assert ft.store().entry("effect", "v3")["pages"] == before


def test_a_partial_match_says_which_words_no_passage_contains(kb):
    # Measured live 2026-09-19 against the hosted store: searching for
    # 'zzqx-token-that-no-page-contains-9182' returned "10 passage(s) for
    # 'zzqx-token-that-no-page-contains-9182'" -- ten ranked passages about
    # Angular's injection tokens, because both stores fall back from every
    # word to any word, and the fallback said nothing about it. A model
    # reading that header cites a passage as the answer to a question it
    # never addressed. The passages may stay; the header must own up.
    _store(kb)
    out = ft.tool_search_knowledge_base("zzqx-exponential-backoff-9182")
    assert "passage(s) for" in out
    assert "none of them contains 'zzqx', '9182'" in out, out.splitlines()[0]
    assert "they match the rest of the query" in out


def test_a_full_match_carries_no_caveat(kb):
    _store(kb)
    out = ft.tool_search_knowledge_base("exponential backoff")
    assert "none of them contains" not in out
