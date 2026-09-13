"""What the read cap bounds, and — more importantly — what it does not.

`MAX_CHARS` caps one tool result. It was 60,000, which cut real documentation
short: a single large specification page is bigger than that, and being handed
a third of one is a poor answer to a question about it.

The reason it cannot simply be removed is the *provider's* context window, not
DocsForge's storage. A result that overflows a window does not truncate
gracefully, it fails the turn — and this project speaks to six providers, some
with far smaller windows than Claude's.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.store import kb_store as kbs


def test_the_read_cap_is_generous_but_not_absent():
    assert ft.DEFAULT_MAX_CHARS == 200_000
    assert ft.DEFAULT_MAX_CHARS > 60_000, "the old cap cut real pages short"


def test_a_page_that_the_old_cap_would_have_cut_now_comes_back_whole():
    """The improvement, stated as the case that used to fail."""
    page = "x" * 150_000                     # over the old 60,000, under the new cap
    # An explicit limit, so this asserts the default rather than whatever the
    # machine's environment happens to set.
    assert ft._truncate(page, limit=ft.DEFAULT_MAX_CHARS) == page
    assert "truncated" not in ft._truncate(page, limit=ft.DEFAULT_MAX_CHARS)


def test_truncation_still_says_what_it_dropped():
    """Raising a cap must not quieten it. An undisclosed subset is the one
    thing this product refuses."""
    body = "y" * (ft.MAX_CHARS + 5_000)
    out = ft._truncate(body)
    assert len(out) < len(body)
    assert "truncated" in out
    assert "5,00" in out or "5,0" in out      # the count is named, not implied


def test_the_limit_is_per_call_so_a_caller_can_set_their_own():
    """Someone who knows their own context window should be able to say so."""
    assert ft._truncate("z" * 500, limit=1_000) == "z" * 500      # under: whole
    tight = ft._truncate("z" * 500, limit=100)                    # over: cut, and said
    assert len(tight) < 500
    assert "truncated" in tight


# ── the non-degradation guarantee ──────────────────────────────
#
# The cap is on one tool result. Everything durable stays whole, and these
# pin that: a raised read cap must never be mistaken for a storage limit,
# and a lowered one must never start truncating what is stored.

def test_the_store_keeps_a_page_far_larger_than_the_read_cap(tmp_path):
    store = kbs.FileStore(tmp_path)
    # Stripped, because the store strips a page before writing it and this
    # test is about size, not about whitespace.
    huge = ("# Spec\n\n" + "paragraph of specification text. " * 40_000).strip()
    assert len(huge) > ft.MAX_CHARS

    writer = store.writer("bigtech", "1.0", "https://x.dev/spec", "html", expected=1)
    writer.add("The Whole Spec", "https://x.dev/spec", huge)
    writer.settle(complete=True)

    page = store.page("bigtech", "1.0", 1)
    assert huge in page["content"], "storage must not be bounded by the read cap"


def test_lowering_the_read_cap_does_not_shrink_what_is_stored(tmp_path, monkeypatch):
    """The mirror image: the cap is applied on the way out, never on the way in."""
    monkeypatch.setattr(ft, "MAX_CHARS", 100)
    store = kbs.FileStore(tmp_path)
    body = ("# Doc\n\n" + "content " * 5_000).strip()

    writer = store.writer("small", "1.0", "https://x.dev/d", "html", expected=1)
    writer.add("Doc", "https://x.dev/d", body)
    writer.settle(complete=True)

    assert body in store.page("small", "1.0", 1)["content"]
