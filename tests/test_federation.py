"""
Offline tests for Phase E federation — no network.

The defect this exists for: a technology whose documentation spans several
sites gets harvested as whichever one was reached first, agrees with that site's
sitemap, and reports `complete=True`. Whole corpora missing, total coverage
reported — the failure the coverage note exists to prevent, happening one level
above where it can see.

So the invariants get tests of their own. Invariant 8 (admitting a corpus never
changes another's completeness) and Invariant 9 (never `True` unless every
selected corpus is) are the two that make per-corpus accounting safe, and both
are easy to break by accident later.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core import federation as fed
from docsforge.core.federation import Corpus, Federation


def soup(html: str):
    return df._soup(html)


def links(*hrefs: str, where: str = "div") -> str:
    body = "".join(f"<a href='{h}'>x</a>" for h in hrefs)
    return f"<html><body><{where}>{body}</{where}></body></html>"


# ── the common case must stay free ───────────────────────
def test_a_single_corpus_federation_says_nothing():
    # Almost every technology is one crawlable tree, and that case must not pay
    # for the rare one — no extra requests, and no note to read.
    one = Federation.single("https://x.dev/docs/", technology="x")
    assert len(one.corpora) == 1
    assert one.note() == ""


# ── weighted evidence ────────────────────────────────────
def test_where_a_link_sits_decides_what_it_weighs():
    nav = soup("<nav><a href='/a'>x</a></nav>").find("a")
    foot = soup("<footer><a href='/a'>x</a></footer>").find("a")
    body = soup("<div><a href='/a'>x</a></div>").find("a")

    assert fed.link_weight(nav) == fed.WEIGHTS["nav"]
    assert fed.link_weight(foot) == fed.WEIGHTS["footer"]
    assert fed.link_weight(body) == fed.WEIGHTS["body"]
    # A hub page exists to enumerate the parts, so it triples its own weights.
    assert fed.link_weight(body, hub=True) == fed.WEIGHTS["hub"]


def test_a_sidebar_outvotes_a_footer():
    # Footer links repeat on every page and would otherwise win on multiplicity
    # alone. That is why position is weighted rather than counted.
    f = Federation(technology="x")
    for _ in range(10):
        f.record_page("https://x.dev/docs/p", soup(
            "<footer><a href='https://legal.x.dev/terms'>terms</a></footer>"
            "<nav><a href='https://api.x.dev/reference'>API</a></nav>"))

    votes = dict(f._votes)
    assert votes[("api.x.dev", "reference")] > votes[("legal.x.dev", "terms")]


def test_corpora_emerge_from_the_whole_crawl_not_from_a_hub_page():
    # Many projects never publish a page enumerating their parts; they express
    # the same thing as a sidebar, one page at a time.
    f = Federation(technology="x")
    for i in range(20):
        f.record_page(f"https://x.dev/docs/{i}", soup(
            "<nav><a href='https://api.x.dev/reference/'>API</a></nav>"))

    found = f.proposals("https://x.dev/docs/")
    assert [c.host for c in found] == ["api.x.dev"]


def test_a_passing_mention_is_not_a_corpus():
    f = Federation(technology="x")
    for i in range(40):
        f.record_page(f"https://x.dev/docs/{i}", soup(
            "<div><a href='https://x.dev/docs/next'>next</a></div>"))
    # One body link on one page, against a 40-page crawl.
    f.record_page("https://x.dev/docs/z", soup(
        "<div><a href='https://random.example/thing'>a thing</a></div>"))

    assert [c.host for c in f.proposals("https://x.dev/docs/")] == []


def test_the_corpus_being_crawled_is_not_proposed_as_a_new_one():
    f = Federation(technology="x")
    for i in range(20):
        f.record_page(f"https://x.dev/docs/{i}", soup(
            "<nav><a href='https://x.dev/docs/other'>other</a></nav>"))
    assert f.proposals("https://x.dev/docs/") == []


def test_the_threshold_rises_with_the_size_of_the_crawl():
    small, large = Federation(), Federation()
    small.pages_seen, large.pages_seen = 4, 400
    assert small.threshold() == fed.MIN_VOTES
    assert large.threshold() == pytest.approx(60.0)


# ── admission: Invariant 14 ──────────────────────────────
def test_a_host_enters_only_through_the_identity_gate():
    f = Federation(technology="effect")
    allowed = Corpus(url="https://api.effect.dev/")
    blocked = Corpus(url="https://unrelated.example/")

    def identify(url):
        return ("effect" in url, "names it 9 times" if "effect" in url else "never mentions it")

    assert f.admit(allowed, identify) is True
    assert f.admit(blocked, identify) is False
    assert blocked.status == "host not admitted"
    assert f.refused == [("unrelated.example", "never mentions it")]


def test_the_gate_is_run_once_per_host():
    f = Federation(technology="x")
    calls = []

    def identify(url):
        calls.append(url)
        return True, "ok"

    f.admit(Corpus(url="https://api.x.dev/a/"), identify)
    f.admit(Corpus(url="https://api.x.dev/b/"), identify)
    assert len(calls) == 1, "the identity gate should be cached per federation"


def test_a_refused_host_is_recorded_rather_than_dropped():
    f = Federation(technology="x")
    f.admit(Corpus(url="https://nope.example/"), lambda u: (False, "no evidence"))
    assert "nope.example" in f.note()
    assert "no evidence" in f.note()


# ── per-corpus accounting: Invariants 8 and 9 ────────────
def test_admitting_a_corpus_never_changes_another_ones_completeness():
    # Invariant 8. This is why the global scope-revision rule was withdrawn: it
    # invalidated `expected` for work already correctly finished.
    f = Federation(technology="x")
    done = f.add(Corpus(url="https://x.dev/docs/"))
    done.settle(stored=47, expected=47)
    assert done.complete is True

    late = f.add(Corpus(url="https://api.x.dev/"))
    late.settle(stored=0, expected=200)

    assert done.complete is True and done.expected == 47
    assert late.complete is False


def test_the_whole_is_never_complete_unless_every_selected_corpus_is():
    f = Federation(technology="x")
    a = f.add(Corpus(url="https://x.dev/docs/"))
    b = f.add(Corpus(url="https://api.x.dev/"))

    a.settle(10, 10)
    b.settle(5, 10)
    assert f.complete is False, "a measured shortfall makes the whole incomplete"

    b.settle(10, 10)
    assert f.complete is True

    b.settle(10, None)
    assert f.complete is None, "unmeasurable is not the same as incomplete"


def test_an_unselected_corpus_is_never_silently_absent():
    # Invariant 5: recorded, with its magnitude, and marked not requested.
    f = Federation(technology="x")
    f.add(Corpus(url="https://x.dev/docs/")).settle(10, 10)
    skipped = f.add(Corpus(url="https://api.x.dev/", magnitude=703, kind="api"))
    skipped.selected = False

    note = f.note()
    assert "not requested" in note
    assert "703" in note
    assert "api.x.dev" in note


def test_an_unselected_corpus_does_not_hold_back_completeness():
    f = Federation(technology="x")
    f.add(Corpus(url="https://x.dev/docs/")).settle(10, 10)
    skipped = f.add(Corpus(url="https://api.x.dev/"))
    skipped.selected = False
    assert f.complete is True


# ── filing ───────────────────────────────────────────────
def test_a_versionless_corpus_is_filed_under_undated():
    # One label across a federation files a versionless corpus under a version
    # it does not have, so the version lives on the corpus. A date would be a
    # claim about when the content is from, which an undated corpus cannot make.
    from docsforge.tools import forge_tools as ft
    assert ft.corpus_label(Corpus(url="https://x.dev/docs/"), []) == "undated"
    assert ft.corpus_label(Corpus(url="https://x.dev/docs/", version="v3"), []) == "v3"
    # A version the URL does name is still honoured.
    assert ft.corpus_label(Corpus(url="https://x.dev/docs/v2/"), []) == "v2"


def test_two_corpora_on_one_host_file_separately():
    a = Corpus(url="https://x.dev/docs/")
    b = Corpus(url="https://x.dev/reference/")
    assert a.slug != b.slug


# ── shape ────────────────────────────────────────────────
def test_one_enormous_self_referential_page_is_a_page_not_a_tree():
    assert fed.classify_shape(chars=60_000, anchors=120, median_chars=5_000) == "page"
    # Big but with no table of contents of its own: still an ordinary page.
    assert fed.classify_shape(chars=60_000, anchors=3, median_chars=5_000) == "tree"
    # Long relative to nothing: an absolute byte threshold would be meaningless
    # across corpora, so the comparison is to this corpus's own median.
    assert fed.classify_shape(chars=60_000, anchors=120, median_chars=30_000) == "tree"


def test_an_index_file_makes_it_an_api_corpus():
    assert fed.classify_shape(chars=1000, anchors=5, median_chars=1000,
                              has_index=True) == "api"


# ── kind ─────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://x.dev/reference/thing", "api"),
    ("https://x.dev/sdk/python/", "sdk"),
    ("https://x.dev/spec/v1/", "spec"),
    ("https://x.dev/guide/getting-started/", "guide"),
    ("https://x.dev/examples/basic/", "cookbook"),
    ("https://x.dev/changelog/", "changelog"),
    ("https://x.dev/deployment/", "operations"),
    ("https://x.dev/community/", "meta"),
])
def test_path_tokens_classify_a_corpus(url, expected):
    kind, confidence = fed.classify_kind(url)
    assert kind == expected
    assert confidence > 0.5


def test_a_generator_that_only_emits_references_settles_it():
    kind, confidence = fed.classify_kind("https://docs.rs/serde/", generator="rustdoc")
    assert kind == "api"
    assert confidence > 0.9


def test_an_unclassifiable_corpus_says_so_rather_than_guessing():
    # Low confidence on a mandatory kind is an escalation trigger in the layer
    # above. A classifier that guessed quietly would remove that signal.
    kind, confidence = fed.classify_kind("https://x.dev/misc/")
    assert kind == ""
    assert confidence == 0.0


# ── wiring: what a harvest actually reports ──────────────
def test_a_corpus_that_fails_the_gate_is_reported_as_refused(monkeypatch):
    # A refused host is never fetched, so this needs no store: the point is
    # that the refusal reaches the reader rather than a log.
    from docsforge.tools import forge_tools as ft

    monkeypatch.setattr(ft, "_identify_host",
                        lambda name, url: (False, "never mentions it"))
    stats = {"corpora": [{"url": "https://nope.example/docs/",
                          "host": "nope.example", "votes": 42.0}]}

    note = ft._federate("x", "https://x.dev/docs/", stats, intent="reference")

    assert "identity gate" in note
    assert "never mentions it" in note


def test_a_single_corpus_harvest_reports_nothing_extra():
    from docsforge.tools import forge_tools as ft
    assert ft._federate("x", "https://x.dev/docs/", {}) == ""
    assert ft._federate("x", "https://x.dev/docs/", {"corpora": []}) == ""


# ── harvesting every selected corpus ─────────────────────
@pytest.fixture
def kb(tmp_path):
    from docsforge.tools import forge_tools as ft
    from docsforge.store.kb_store import FileStore
    ft.reset_store(FileStore(tmp_path))
    yield tmp_path
    ft.reset_store(None)


def _two_page_harvest(url, opts, fetcher=None, stats=None, sink=None):
    from docsforge.tools import forge_tools as ft
    if stats is not None:
        stats["discovered"] = 2
        stats["whole"] = True
    docs = [ft.Doc(url + "a", "A", "body a"), ft.Doc(url + "b", "B", "body b")]
    # A real harvest streams each page into the sink as it is produced; a stub
    # that returns a list without doing so no longer models it.
    if sink is not None:
        for doc in docs:
            sink.add(doc.title, doc.url, doc.markdown)
    return docs, "sitemap"


def test_two_corpora_of_one_technology_file_under_different_keys():
    from docsforge.tools import forge_tools as ft
    a = Corpus(url="https://x.dev/docs/")
    b = Corpus(url="https://api.x.dev/reference/")
    assert ft.corpus_key("x", a) != ft.corpus_key("x", b)
    assert ft.corpus_key("x", b).startswith("x--")


def test_a_selected_corpus_is_actually_harvested_and_stored(kb, monkeypatch):
    from docsforge.tools import forge_tools as ft
    monkeypatch.setattr(ft, "_identify_host", lambda name, url: (True, "names it 9 times"))
    monkeypatch.setattr(ft, "harvest", _two_page_harvest)

    stats = {"corpora": [{"url": "https://api.x.dev/reference/",
                          "host": "api.x.dev", "votes": 42.0}],
             "fetched": 5, "discovered": 5, "whole": True}

    note = ft._federate("x", "https://x.dev/docs/", stats, intent="reference")

    assert "harvested separately" in note
    stored, _ = ft.store().technologies()
    names = [t["name"] for t in stored]
    assert any(n.startswith("x--api-x-dev") for n in names), names


def test_a_corpus_that_cannot_be_harvested_is_reported_not_dropped(kb, monkeypatch):
    from docsforge.tools import forge_tools as ft
    from docsforge.core.engine import ForgeError

    monkeypatch.setattr(ft, "_identify_host", lambda name, url: (True, "names it"))

    def explodes(url, opts, fetcher=None, stats=None, sink=None):
        raise ForgeError("the site went away")

    monkeypatch.setattr(ft, "harvest", explodes)
    stats = {"corpora": [{"url": "https://api.x.dev/reference/",
                          "host": "api.x.dev", "votes": 42.0}],
             "fetched": 5, "discovered": 5, "whole": True}

    note = ft._federate("x", "https://x.dev/docs/", stats, intent="reference")

    assert "could not be harvested" in note
    assert "the site went away" in note


def test_each_corpus_keeps_its_own_count(kb, monkeypatch):
    # Invariant 8, end to end: a corpus that comes back short must not make the
    # corpus that finished look short, and vice versa.
    from docsforge.tools import forge_tools as ft
    monkeypatch.setattr(ft, "_identify_host", lambda name, url: (True, "names it"))

    def short(url, opts, fetcher=None, stats=None, sink=None):
        if stats is not None:
            stats["discovered"] = 10       # ten exist, two came back
            stats["whole"] = False
        return _two_page_harvest(url, opts, fetcher, None, sink)[0], "sitemap"

    monkeypatch.setattr(ft, "harvest", short)
    stats = {"corpora": [{"url": "https://api.x.dev/reference/",
                          "host": "api.x.dev", "votes": 42.0}],
             "fetched": 5, "discovered": 5, "whole": True}

    note = ft._federate("x", "https://x.dev/docs/", stats, intent="reference")

    # The entry corpus is complete; the second is not. Both are stated.
    assert "2 of 10" in note or "INCOMPLETE" in note


def test_the_coverage_note_has_exactly_one_renderer():
    # _federate used to build its own, which is how its header came to say the
    # coverage described "only the corpus that was crawled" long after
    # selection had started harvesting the others. Two renderers of one fact
    # drift apart silently; this asserts there is one.
    from docsforge.tools import forge_tools
    source = open(forge_tools.__file__, encoding="utf-8").read()
    assert "sites.note(" in source, "_federate does not use the shared renderer"
    assert "**This technology documents itself" not in source,         "_federate is rendering a second coverage note of its own"
