"""
Federation: one technology is a set of documentation bodies, not one tree.

PROPOSAL-II calls the assumption behind `docs_scope()` the most serious defect
in the project. A technology of any size publishes a manual, a specification and
a generated API reference, frequently on different hosts. The pipeline harvests
whichever one it reached first, agrees with that site's sitemap, and records
`complete=True` — whole corpora missing, total coverage reported. It is the
failure `_coverage_note()` exists to prevent, happening one level above where
that function can see.

Two classifications, and conflating them is why the current design cannot serve
a caller with a purpose:

  * **shape** — how to acquire it: `tree`, `page`, `api`
  * **kind**  — what it is for: `spec`, `language`, `api`, `sdk`, `guide`,
    `cookbook`, `operations`, `changelog`, `meta`

Everything here is pure: no HTTP, no store. Host admission takes the identity
gate as a callback so this module can be tested offline and so the gate stays
the resolver's business.

**Known risk, recorded rather than hidden** (`ISSUES.md` R1): the identity gate
this admits hosts through was measured confirming a page is about something
*with that name*, not that it is the right project. On the Phase C numbers it
would admit a parked domain. `Federation.admit()` records the signals that let
each host in so a wrong admission can be seen rather than inferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

# ─────────────────────────────────────────────────────────────
# Vote weights — where a link sits says what it means
# ─────────────────────────────────────────────────────────────
#: A sidebar entry is information architecture; a footer link is boilerplate
#: repeated on every page and would otherwise out-vote everything by sheer
#: multiplicity. A hub page — one that exists to enumerate the parts — is an
#: accelerator that triples its own weights, never a prerequisite: plenty of
#: projects express the same thing as a grid, a sidebar, or not at all.
WEIGHTS = {"hub": 3.0, "nav": 2.0, "body": 1.0, "footer": 0.2}

#: A corpus needs real, repeated evidence: six votes, or 15% of the pages seen,
#: whichever is larger. The fraction is what stops a long crawl admitting
#: everything it ever glanced at.
MIN_VOTES = 6.0
VOTE_FRACTION = 0.15

_NAV_TAGS = {"nav", "aside", "header"}
_FOOTER_TAGS = {"footer"}
_NAV_HINTS = re.compile(r"\b(nav|sidebar|menu|toc|breadcrumb)\b", re.I)
_FOOTER_HINTS = re.compile(r"\bfooter\b", re.I)


def link_weight(anchor, hub: bool = False) -> float:
    """How much this anchor's position says it means something."""
    node = anchor
    for _ in range(6):                      # bounded walk; deep DOMs are common
        name = getattr(node, "name", None)
        if not name:
            break
        classes = " ".join(node.get("class") or []) if hasattr(node, "get") else ""
        ident = (node.get("id") or "") if hasattr(node, "get") else ""
        marker = f"{classes} {ident}"
        if name in _FOOTER_TAGS or _FOOTER_HINTS.search(marker):
            return WEIGHTS["footer"]
        if name in _NAV_TAGS or _NAV_HINTS.search(marker):
            return WEIGHTS["hub"] if hub else WEIGHTS["nav"]
        node = node.parent
    return WEIGHTS["hub"] if hub else WEIGHTS["body"]


def corpus_root(url: str) -> tuple[str, str]:
    """The `(host, first path segment)` a URL belongs to.

    Coarse on purpose. A corpus is "the API reference" or "the manual", not
    every directory under them, and grouping any finer proposes a corpus per
    page.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    segments = [s for s in (parsed.path or "/").split("/") if s]
    return host, (segments[0] if segments else "")


# ─────────────────────────────────────────────────────────────
# Corpus
# ─────────────────────────────────────────────────────────────
@dataclass
class Corpus:
    """One documentation body, with its own count and its own completeness."""

    url: str
    host: str = ""
    shape: str = "tree"            # tree | page | api
    kind: str = ""                 # what it is for; "" = not classified
    kind_confidence: float = 0.0
    version: str = ""              # "" files under `undated`
    magnitude: int = 0             # cheap estimate, feeds selection and breadth
    expected: int | None = None
    stored: int = 0
    complete: bool | None = None
    selected: bool = True
    #: The corpus the caller actually asked for, and the one already crawled by
    #: the time selection runs. It cannot be "not requested" — it was requested
    #: by name and it is already in the store (`ISSUES.md` W4).
    entry: bool = False
    status: str = ""               # "", "not requested", "host not admitted"
    evidence: str = ""
    votes: float = 0.0

    def __post_init__(self) -> None:
        if not self.host:
            self.host = (urlparse(self.url).hostname or "").lower()

    @property
    def slug(self) -> str:
        """A filing name for this corpus within its technology."""
        host, segment = corpus_root(self.url)
        stem = re.sub(r"[^a-z0-9]+", "-", f"{host}-{segment}".lower()).strip("-")
        return stem or "docs"

    def settle(self, stored: int, expected: int | None) -> None:
        """Record what this corpus actually got. Touches no other corpus."""
        self.stored = stored
        self.expected = expected
        self.complete = None if expected is None else stored >= expected

    def line(self) -> str:
        """One line for the coverage note."""
        where = f"{self.kind or 'unclassified'}/{self.shape}"
        if not self.selected:
            size = f"~{self.magnitude} pages" if self.magnitude else "size unknown"
            # A specific reason beats the generic one. "Not requested" is the
            # default because Invariant 5 requires an unselected corpus to be
            # recorded with its magnitude and never silently absent — but
            # saying *why* it was left out is strictly more useful.
            return f"- {self.url} ({where}, {size}) — {self.status or '**not requested**'}"
        # A status never replaces the count. For a corpus that was harvested,
        # how much of it came back IS the report — saying only "harvested
        # separately" would hide the one number this whole design exists to
        # produce.
        mark = {True: "complete", False: "INCOMPLETE",
                None: "coverage unknown"}[self.complete]
        of = f" of {self.expected}" if self.expected is not None else ""
        tail = f" — {self.status}" if self.status else ""
        return f"- {self.url} ({where}) — {self.stored}{of} pages, {mark}{tail}"


# ─────────────────────────────────────────────────────────────
# Federation
# ─────────────────────────────────────────────────────────────
@dataclass
class Federation:
    """Every documentation body belonging to one technology."""

    technology: str = ""
    corpora: list[Corpus] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    pages_seen: int = 0
    _votes: dict[tuple[str, str], float] = field(default_factory=dict)
    _example: dict[tuple[str, str], str] = field(default_factory=dict)
    _admitted: dict[str, bool] = field(default_factory=dict)

    # ── the common case ──
    @classmethod
    def single(cls, url: str, technology: str = "") -> "Federation":
        """One corpus, zero extra requests.

        Almost every technology is one crawlable tree, and that case must not
        pay for the rare one. Nothing in this constructor fetches anything.
        """
        return cls(technology=technology, corpora=[Corpus(url=url, entry=True)])

    # ── evidence ──
    def record_page(self, page_url: str, soup, hub: bool = False) -> None:
        """Accumulate weighted link evidence from one crawled page.

        Called with the soup *before* chrome is stripped, because the sidebar
        is where a project states its own structure. Summing across the whole
        crawl is what makes this work on projects that never publish a hub page
        at all — the sidebar says the same thing, just one page at a time.
        """
        self.pages_seen += 1
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, anchor.get("href") or "")
            if not href.startswith(("http://", "https://")):
                continue
            key = corpus_root(href)
            if not key[0]:
                continue
            self._votes[key] = self._votes.get(key, 0.0) + link_weight(anchor, hub)
            self._example.setdefault(key, href)

    def threshold(self) -> float:
        return max(MIN_VOTES, VOTE_FRACTION * self.pages_seen)

    def proposals(self, current_url: str) -> list[Corpus]:
        """Candidate corpora, best-evidenced first, excluding the current one."""
        here = corpus_root(current_url)
        floor = self.threshold()
        found = []
        for key, votes in self._votes.items():
            if key == here or votes < floor:
                continue
            root = self._example[key]
            found.append(Corpus(url=f"{urlparse(root).scheme}://{key[0]}/"
                                    + (f"{key[1]}/" if key[1] else ""),
                                host=key[0], votes=votes))
        return sorted(found, key=lambda c: -c.votes)

    # ── admission ──
    def admit(self, corpus: Corpus, identify) -> bool:
        """Let a host in only through the identity gate. Cached per federation.

        Invariant 14: federation is not permission to roam. Sibling-subdomain
        matching is withdrawn — it admits a project's own package host and
        rejects every documentation SaaS and every separate-domain API
        reference, which between them cover a large share of major ecosystems.

        `identify(url)` returns `(ok, evidence)`. It is passed in rather than
        imported so the gate stays the resolver's business and this stays
        testable without a network.
        """
        host = corpus.host
        if host in self._admitted:
            if not self._admitted[host]:
                corpus.status = "host not admitted"
            return self._admitted[host]

        ok, evidence = identify(corpus.url)
        self._admitted[host] = bool(ok)
        corpus.evidence = evidence
        if not ok:
            corpus.status = "host not admitted"
            self.refused.append((host, evidence or "failed the identity gate"))
        return bool(ok)

    def add(self, corpus: Corpus) -> Corpus:
        """Admit a corpus into the federation.

        Invariant 8: this never touches another corpus's `complete`. A corpus
        that finished at 47 of 47 stays complete forever, and a newly admitted
        one starts its own count from zero — which is why the global
        scope-revision rule from the earlier drafts is withdrawn. It invalidated
        `expected` for work that had already been done correctly.
        """
        self.corpora.append(corpus)
        return corpus

    # ── accounting ──
    @property
    def selected(self) -> list[Corpus]:
        return [c for c in self.corpora if c.selected]

    @property
    def complete(self) -> bool | None:
        """Invariant 9: never `True` unless every selected corpus is `True`.

        `False` and `None` stay distinct. A measured shortfall in any selected
        corpus makes the whole thing incomplete; an unmeasurable one makes it
        unknown. Collapsing those two would make unearned confidence
        expressible again, which is the thing the project exists not to do.
        """
        chosen = self.selected
        if not chosen:
            return None
        if any(c.complete is False for c in chosen):
            return False
        if any(c.complete is None for c in chosen):
            return None
        return True

    @property
    def stored(self) -> int:
        return sum(c.stored for c in self.selected)

    def note(self, intent: str = "") -> str:
        """The coverage note: every corpus, selected or not, with its reason.

        Invariant 5: an unselected corpus is recorded with its magnitude and is
        never silently absent. Invariant 11: every refusal is surfaced next to
        the coverage, not buried in a log nobody reads.

        This is the only place a coverage note is rendered. `_federate` used to
        build a second one inline, which is how its header came to claim the
        coverage described "only the corpus that was crawled" long after
        selection had started harvesting the others too. Two renderers of one
        fact drift; one cannot.
        """
        if len(self.corpora) <= 1 and not self.refused:
            return ""                       # single corpus: nothing to federate

        mark = {True: "complete", False: "INCOMPLETE",
                None: "coverage unknown"}[self.complete]
        why = f" (intent {intent!r})" if intent else ""
        lines = [f"**This technology documents itself in {len(self.corpora)} "
                 f"places{why} — {len(self.selected)} selected, overall "
                 f"{mark}.**", ""]
        lines += [c.line() for c in self.corpora]
        if self.refused:
            lines += ["", "Hosts refused by the identity gate:"]
            lines += [f"- {host} — {why}" for host, why in self.refused]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Shape — how to acquire it
# ─────────────────────────────────────────────────────────────
#: A single page six times the corpus median with its own table of contents is
#: a specification, not a tree. Relative, because an absolute byte threshold is
#: meaningless across corpora — the Phase B sample ranged 1,254 to 31,687
#: median chars, a 25x spread.
PAGE_MULTIPLE = 6
PAGE_ANCHORS = 25


def classify_shape(chars: int, anchors: int, median_chars: int,
                   has_index: bool = False, has_manifest: bool = False) -> str:
    """`page`, `api` or `tree`.

    Must run AFTER any render decision. A JS-driven API reference measures as
    2 KB of shell and would classify as a tree, which is the one mistake that
    turns an exact count into a guess.
    """
    if has_index:
        # An index file lists every entry: read it, fetch the entries, never
        # crawl. `expected` is the index length and it is exact.
        return "api"
    if has_manifest:
        # The site publishes a list of its own pages, and that list has more
        # than one entry on it. Whatever this is, it is not one document — so
        # the `page` test below cannot apply, however big the landing page is.
        # A specification hosted inside a MkDocs site is the case this catches:
        # long, heavily anchored, and still a tree.
        return "tree"
    if median_chars > 0 and chars >= PAGE_MULTIPLE * median_chars and anchors >= PAGE_ANCHORS:
        # One enormous self-referential page: fetch once, split on h2/h3.
        # `expected` is the section count and it is exact.
        return "page"
    return "tree"


# ─────────────────────────────────────────────────────────────
# Kind — what it is for
# ─────────────────────────────────────────────────────────────
#: Generator families that only ever emit API references.
API_GENERATORS = ("godoc", "javadoc", "docs.rs", "rustdoc", "hexdocs",
                  "doxygen", "sphinx-autodoc", "typedoc", "pdoc")

#: Path tokens, in rough order of how much they settle the question.
KIND_PATHS = (
    ("spec", ("/spec/", "/specification/", "/rfc/", "/standard/")),
    ("api", ("/api/", "/reference/", "/apidocs/", "/apiref/")),
    # PROPOSAL-II §8 asked whether `adk` is a kind of its own or a subtype of
    # `sdk`, and deferred it. It is its own: a caller resolving an import needs
    # the API reference and the client libraries, and an agent-development kit
    # is a third thing again. Folding it into `sdk` means an intent cannot ask
    # for one without dragging in the other.
    ("adk", ("/adk/", "/agent-kit/", "/agent-development-kit/", "/agents/")),
    ("sdk", ("/sdk/", "/client/", "/clients/", "/libraries/", "/bindings/")),
    ("cookbook", ("/examples/", "/recipes/", "/cookbook/", "/how-to/", "/howto/")),
    ("guide", ("/guide/", "/guides/", "/tutorial/", "/tutorials/",
               "/getting-started/", "/learn/", "/handbook/")),
    ("operations", ("/operations/", "/ops/", "/deploy/", "/deployment/",
                    "/admin/", "/install/", "/installation/", "/runbook/")),
    ("changelog", ("/changelog/", "/releases/", "/release-notes/",
                   "/whats-new/", "/blog/", "/news/")),
    ("language", ("/language/", "/syntax/", "/grammar/", "/lang/")),
    ("meta", ("/about/", "/community/", "/contributing/", "/governance/",
              "/roadmap/", "/team/")),
)


def classify_kind(url: str, title: str = "", generator: str = "",
                  code_ratio: float = 0.0, symbol_titles: float = 0.0
                  ) -> tuple[str, float]:
    """What this corpus is for, and how sure we are.

    Confidence is returned rather than swallowed because low confidence on a
    *mandatory* kind is one of the three escalation triggers in the layer
    above. A classifier that guesses quietly would remove the signal that layer
    needs to know it should ask.
    """
    low_gen = (generator or "").lower()
    for family in API_GENERATORS:
        if family in low_gen:
            return "api", 0.95

    path = (urlparse(url).path or "/").lower()
    if not path.endswith("/"):
        path += "/"
    for kind, tokens in KIND_PATHS:
        if any(token in path for token in tokens):
            return kind, 0.75

    low_title = (title or "").lower()
    for kind, tokens in KIND_PATHS:
        if any(token.strip("/").replace("-", " ") in low_title for token in tokens):
            return kind, 0.5

    # Pages named after identifiers rather than tasks, dense with signatures,
    # are a reference whatever their URL says.
    if symbol_titles >= 0.6 and code_ratio >= 0.3:
        return "api", 0.55
    if code_ratio >= 0.5:
        return "api", 0.4

    return "", 0.0
