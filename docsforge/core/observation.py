"""
What a page looked like, and what a crawl has seen so far.

Pure data. This module imports nothing from the pipeline, which is the only
reason the pipeline can import *it* — `instrument.py` needs `docsforge` to do
the measuring, so `Observation` living there made it unreachable from the
crawler that now has to adapt on it.

The split is along a real line rather than a convenient one:

  * here — the record of a page and the ledger of a crawl, with no opinion
  * `instrument.observe()` — measuring a page from raw HTML, for the driver
  * `docsforge.Plan` — deciding what to do differently, for the crawler

A measurement that steers the crawl is adaptation, and it has to be recorded
(Invariant 11) and must never drop a page (Invariant 7). Those two rules are
what keep this from becoming a filter with a nicer name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: CSS-module and build-hash suffixes: `sidebar-sidecar-layout_main__0SN51`.
#: Stable within one crawl and different after the next deploy, so leaving them
#: in makes a signature that cannot be compared across runs.
_CLASS_HASH = re.compile(
    # CSS-module suffixes: `sidebar-sidecar-layout_main__0SN51`.
    r"__[A-Za-z0-9_]{4,}\b"
    # Scoped-style classes the frameworks emit per build, seen live on the
    # Astro docs as `main.astro-5rh3l5p7`. Anchored to the known prefixes
    # rather than "any dash and some characters", which would eat real class
    # names like `doc-content`.
    r"|\b(?:astro|svelte|jsx|css|v)-[a-z0-9]{5,}\b")


def bucket(n: int) -> str:
    """Coarse magnitude. Exact counts would make every page its own template."""
    if n == 0:
        return "0"
    if n <= 2:
        return "1-2"
    if n <= 5:
        return "3-5"
    if n <= 15:
        return "6-15"
    return "16+"


def ancestry(el, depth: int = 3) -> str:
    """`tag.firstclass` for the element and its parents, outermost first.

    Three levels is a deliberate compromise: one level cannot tell a docs
    template from a blog template, and the whole path makes every page unique.
    Only the first class is kept because utility-class frameworks emit dozens
    per element, nearly all layout noise.
    """
    parts: list[str] = []
    node = el
    for _ in range(depth):
        name = getattr(node, "name", None)
        if not name or name == "[document]":
            break
        classes = node.get("class") or [] if hasattr(node, "get") else []
        parts.append(f"{name}.{classes[0]}" if classes else name)
        node = node.parent
    path = _CLASS_HASH.sub("", ">".join(reversed(parts)))
    # A class that was nothing but a build hash leaves a dangling dot.
    return path.replace(".>", ">").rstrip(".")


@dataclass(frozen=True)
class Observation:
    """What one page looked like when it was extracted.

    Deliberately free of judgement: no page is "good" or "bad" here, it just
    has numbers. Whether a link ratio of 0.6 means navigation is a question for
    whatever reads this, and it should be answered from a distribution.
    """

    url: str
    title: str = ""
    selector: str = ""             # winning CONTENT selector; "density"; "" = refused
    signature: str = ""            # template fingerprint: the layout ancestry
    shape: str = ""                # coarse content shape; NOT part of the template
    chars: int = 0
    links: int = 0
    link_text_ratio: float = 0.0
    code_blocks: int = 0
    headings: int = 0
    scripts: int = 0
    shell: bool = False
    extractable: bool = True
    #: The container's density, from the same `docsforge.density()` the floor is
    #: compared against. Recorded rather than recomputed because a learned floor
    #: must be fitted to the exact quantity it will be checked against — two
    #: nearly-identical scoring functions is how a threshold quietly stops
    #: meaning what it was fitted to mean.
    density_score: float = 0.0
    generator: str = ""
    mentions: int = 0
    fetched_ms: int = 0

    @property
    def fell_through(self) -> bool:
        """True when none of the nine CONTENT selectors recognised the page."""
        return self.selector in ("", "density")

    def score(self) -> float:
        """How readable this page's content was, in roughly 0..1.

        Prefers the density actually measured during extraction. The fallback
        below only runs for an `Observation` built by hand — a test fixture, or
        a record from before `density_score` existed.
        """
        if self.density_score:
            return self.density_score
        if not self.chars:
            return 0.0
        length = min(self.chars / 3000.0, 1.0)
        prose = 1.0 - min(self.link_text_ratio, 1.0)
        structure = min((self.headings + self.code_blocks) / 10.0, 1.0)
        return round(0.5 * prose + 0.3 * length + 0.2 * structure, 3)


class Ledger:
    """Every observation from one corpus, plus a rolling window over them.

    The plan is re-derived from the last WINDOW pages rather than from all of
    them, because a site that changes template halfway should be noticed
    halfway, not averaged into silence.
    """

    WINDOW = 12

    def __init__(self) -> None:
        self.observations: list[Observation] = []

    def __len__(self) -> int:
        return len(self.observations)

    def record(self, obs: Observation) -> Observation:
        self.observations.append(obs)
        return obs

    def recent(self, n: int | None = None) -> list[Observation]:
        return self.observations[-(n or self.WINDOW):]

    def by_signature(self) -> dict[str, list[Observation]]:
        """Observations grouped by template — the unit rules are learned on."""
        clusters: dict[str, list[Observation]] = {}
        for obs in self.observations:
            clusters.setdefault(obs.signature, []).append(obs)
        return clusters

    def summary(self) -> dict:
        """The numbers to read before writing a single threshold."""
        obs = self.observations
        if not obs:
            return {"pages": 0}

        chars = sorted(o.chars for o in obs)
        ratios = sorted(o.link_text_ratio for o in obs)
        selectors: dict[str, int] = {}
        generators: dict[str, int] = {}
        for o in obs:
            key = o.selector or "(refused)"
            selectors[key] = selectors.get(key, 0) + 1
            if o.generator:
                generators[o.generator] = generators.get(o.generator, 0) + 1

        def median(xs):
            return xs[len(xs) // 2] if xs else 0

        fell = sum(1 for o in obs if o.fell_through)
        shells = sum(1 for o in obs if o.shell)
        refused = sum(1 for o in obs if not o.extractable)
        return {
            "pages": len(obs),
            "fell_through": fell,
            "fell_through_pct": round(100 * fell / len(obs), 1),
            "shells": shells,
            "shells_pct": round(100 * shells / len(obs), 1),
            "unextractable": refused,
            "unextractable_pct": round(100 * refused / len(obs), 1),
            "templates": len(self.by_signature()),
            "median_chars": median(chars),
            "min_chars": chars[0],
            "max_chars": chars[-1],
            "median_link_text_ratio": median(ratios),
            "selectors": dict(sorted(selectors.items(), key=lambda kv: -kv[1])),
            "generators": dict(sorted(generators.items(), key=lambda kv: -kv[1])),
        }
