"""
Tests for PROPOSAL-3 Phase 2 — shape and magnitude, measured rather than assumed.

`classify_shape` and `Corpus.magnitude` were built and tested under PROPOSAL-II
and never called (`ISSUES.md` W2, W3). The consequence was not cosmetic: with
every corpus classified `tree`, the `page` branch of `_harvest_corpus` was
unreachable, so a specification published as one enormous document got crawled
as a site, found one page, and — being one 1.19 MB page — stored nothing. That
is S7, and it is the second half of the `go.dev` failure.

So these tests come in two halves, and the second half is the one that would
have caught the original bug:

  * that `classify_shape` decides correctly given measurements, and
  * that something actually measures, and actually calls it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core import federation as fed
from docsforge.tools import forge_tools as ft
from docsforge.core.federation import Corpus, Federation


# ── the decision, given measurements ─────────────────────
def test_a_manifest_rules_out_one_giant_page():
    # A specification hosted inside a MkDocs site is long, heavily anchored,
    # and still a tree: the site publishes a list of its own pages, so whatever
    # it is, it is not one document.
    assert fed.classify_shape(chars=60_000, anchors=120, median_chars=5_000,
                              has_manifest=True) == "tree"
    # Same numbers, no manifest: that is a specification.
    assert fed.classify_shape(chars=60_000, anchors=120,
                              median_chars=5_000) == "page"


def test_has_manifest_is_not_a_decorative_parameter():
    # It was accepted and never read for the whole of PROPOSAL-II. A parameter
    # that changes no outcome is a claim the code does not honour.
    a = fed.classify_shape(chars=60_000, anchors=120, median_chars=5_000,
                           has_manifest=False)
    b = fed.classify_shape(chars=60_000, anchors=120, median_chars=5_000,
                           has_manifest=True)
    assert a != b


# ── the measurement ──────────────────────────────────────
SPEC = """<html><body><nav>
  <a href="#intro">Introduction</a> <a href="#types">Types</a>
</nav><main><h1>The Language Specification</h1>
<p>%s</p>
%s
</main></body></html>""" % ("prose " * 400,
                            "\n".join(f'<h2 id="s{i}">Section {i}</h2><p>body</p>'
                                      for i in range(40)))

TREE = """<html><body><main><h1>Docs</h1><p>Welcome.</p>
<a href="/docs/a">A</a> <a href="/docs/b">B</a> <a href="/docs/c">C</a>
</main></body></html>"""


class FakeFetcher:
    """Serves canned HTML and refuses every manifest path."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.asked: list[str] = []

    def html(self, url):
        self.asked.append(url)
        if url not in self.pages:
            raise df.ForgeError(f"404 {url}")
        return self.pages[url]

    def render(self, url):          # pragma: no cover - not a JS shell here
        return self.html(url)

    def get(self, url, **kw):
        raise df.ForgeError("no manifest")

    def text(self, url, **kw):
        raise df.ForgeError("no robots")

    def close(self):
        pass


def test_a_probe_counts_anchors_links_and_size():
    f = FakeFetcher({"https://x.dev/ref/spec": SPEC})
    p = df.probe("https://x.dev/ref/spec", fetcher=f)

    assert p.failed == ""
    assert p.chars > 2_000, "the body should be measured, not the shell"
    assert p.anchors == 2, "in-page anchors are the page's own contents list"
    assert p.manifest == 0


def test_a_probe_that_fails_says_so_rather_than_raising():
    # A corpus that is merely hard to measure must not become a corpus that
    # cannot be harvested: `tree` is the safe default and the probe falls back
    # to it rather than taking the harvest down.
    f = FakeFetcher({})
    p = df.probe("https://x.dev/gone", fetcher=f)
    assert p.failed
    assert p.chars == 0


def test_magnitude_prefers_the_sites_own_count():
    assert df.Probe(links=12, manifest=340).magnitude == 340
    assert df.Probe(links=12).magnitude == 12


# ── wiring: something must actually call it ──────────────
def _source(module) -> str:
    return open(module.__file__, encoding="utf-8").read()


def test_classify_shape_has_a_production_caller():
    # The assertion that was missing for the whole of PROPOSAL-II.
    assert "classify_shape(" in _source(ft)


def test_the_federation_measures_before_it_selects():
    source = _source(ft)
    assert "_measure_corpora(" in source
    # Ordering matters: magnitude feeds the escalation question, so it has to
    # be known before selection runs, not after.
    assert source.index("_measure_corpora(sites") < source.index("sel.select(")


def test_measuring_sets_shape_and_magnitude(monkeypatch):
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    spec = Corpus(url="https://x.dev/ref/spec")
    sites.add(spec)

    fakes = {
        "https://x.dev/ref/spec": df.Probe(chars=60_000, anchors=120, links=4),
    }
    monkeypatch.setattr(ft, "probe", lambda url, **kw: fakes[url])

    ft._measure_corpora(sites, entry, {"discovered": 90, "fetched": 88})

    assert entry.magnitude == 90, "the entry corpus is measured from its crawl"
    assert spec.magnitude == 4
    # One probed corpus, so it is its own median and cannot be six times it.
    assert spec.shape == "tree"


def test_a_document_six_times_the_median_is_a_page(monkeypatch):
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    spec = Corpus(url="https://x.dev/ref/spec")
    guide = Corpus(url="https://x.dev/learn/")
    sites.add(spec)
    sites.add(guide)

    fakes = {
        "https://x.dev/ref/spec": df.Probe(chars=60_000, anchors=120, links=6),
        "https://x.dev/learn/": df.Probe(chars=4_000, anchors=2, links=30),
    }
    monkeypatch.setattr(ft, "probe", lambda url, **kw: fakes[url])
    ft._measure_corpora(sites, entry, {"discovered": 20})

    assert spec.shape == "page", "a specification is fetched once and split"
    assert guide.shape == "tree"


def test_the_entry_corpus_is_never_reclassified(monkeypatch):
    # It has already been crawled as a tree by the time this runs. Relabelling
    # it would describe the harvest that happened as something it was not.
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    monkeypatch.setattr(ft, "probe",
                        lambda url, **kw: pytest.fail(f"re-fetched {url}"))

    ft._measure_corpora(sites, entry, {"discovered": 12})
    assert entry.shape == "tree"


def test_an_unmeasurable_corpus_stays_a_tree(monkeypatch):
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    broken = Corpus(url="https://x.dev/ref/spec")
    sites.add(broken)

    monkeypatch.setattr(ft, "probe",
                        lambda url, **kw: df.Probe(failed="connection reset"))
    ft._measure_corpora(sites, entry, {})

    assert broken.shape == "tree", "claim nothing when nothing was measured"


def test_only_an_api_corpus_with_a_manifest_is_index_shaped(monkeypatch):
    # A MkDocs guide publishes a page list too; calling that "api" would file a
    # tutorial under a shape it does not have.
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    guide = Corpus(url="https://x.dev/guide/")
    guide.kind = "guide"
    api = Corpus(url="https://x.dev/api/")
    api.kind = "api"
    sites.add(guide)
    sites.add(api)

    monkeypatch.setattr(ft, "probe",
                        lambda url, **kw: df.Probe(chars=3_000, links=5, manifest=210))
    ft._measure_corpora(sites, entry, {})

    assert api.shape == "api"
    assert guide.shape == "tree"
    assert guide.magnitude == 210


def test_a_corpus_is_not_compared_against_its_own_size(monkeypatch):
    # Found while building this: with the corpus included in its own median,
    # the one enormous document is compared against a median it has just
    # defined, so it can never be six times it — and the `page` branch stays
    # unreachable in exactly the case it exists for. Two corpora is enough to
    # show it, which is the common federation size.
    sites = Federation(technology="x")
    entry = Corpus(url="https://x.dev/docs/")
    sites.add(entry)
    spec = Corpus(url="https://x.dev/ref/spec")
    small = Corpus(url="https://x.dev/faq/")
    sites.add(spec)
    sites.add(small)

    fakes = {
        "https://x.dev/ref/spec": df.Probe(chars=120_000, anchors=200, links=3),
        "https://x.dev/faq/": df.Probe(chars=2_000, anchors=1, links=8),
    }
    monkeypatch.setattr(ft, "probe", lambda url, **kw: fakes[url])
    ft._measure_corpora(sites, entry, {})

    assert spec.shape == "page"
    assert small.shape == "tree"
