"""
Measuring a page from raw HTML, and what a resolution attempt revealed.

Phase B of PROPOSAL-II. Every adaptive rule the crawler applies is a threshold
over a number, and writing those rules first would mean guessing the constants
and then discovering, expensively, which guesses were wrong. So this measures.

The record types themselves live in `observation.py`, which imports nothing
from the pipeline — that is what lets the crawler adapt on them. This module
needs `docsforge` to do the measuring, so anything the crawler must reach
cannot live here.

`Budget` and `ResolveState` are Layer 1's, and the resolver consumes them
directly. `observe()` is the driver's, and the pipeline has its own cheaper
path that reports from the parse it already did rather than re-parsing.

    from docsforge.core.instrument import observe
    from docsforge.core.observation import Ledger
    ledger = Ledger()
    ledger.record(observe(html, url, name="fastapi"))
    print(ledger.summary())
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from docsforge.core.engine import MIN_MAIN_CHARS, _soup, density, pick_main, strip_chrome
from docsforge.core.observation import Ledger, Observation, ancestry, bucket

__all__ = ["Observation", "Ledger", "observe", "ResolveState", "Budget",
           "to_json"]

_GENERATOR = re.compile(r"""name=["']generator["'][^>]*content=["']([^"']+)""", re.I)
_CANONICAL = re.compile(r"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)""", re.I)
_HREF = re.compile(r"""href=["'](https?://[^"']+)""", re.I)
_REPO = re.compile(r"https?://(?:www\.)?(?:github|gitlab)\.com/"
                   r"[\w.\-]+/[\w.\-]+", re.I)

_DOCSY_WORDS = ("docs", "documentation", "reference", "api", "guide", "manual")


def _docsy(url: str) -> bool:
    low = url.lower()
    return any(word in low for word in _DOCSY_WORDS)


def observe(html: str, url: str, name: str = "", fetched_ms: int = 0) -> Observation:
    """Measure one page exactly as the extractor sees it.

    Two parses, on purpose: `strip_chrome` removes `<script>`, so shell
    detection has to read the document before stripping. Measuring the stripped
    copy for everything else is what keeps these numbers honest — they describe
    the text that would actually be stored, not the raw page.

    The crawler does *not* call this. It reports from the parse it already did
    (`docsforge._html_to_md(report=...)`), because paying for a second parse of
    every page in a 700-page harvest to learn what the first parse already knew
    would be a strange way to make a crawl faster.
    """
    raw = _soup(html)
    scripts = len([s for s in raw.select("script") if s.get("src")])
    found = _GENERATOR.search(html or "")
    generator = found.group(1).strip() if found else ""

    soup = _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    strip_chrome(soup)
    main, selector = pick_main(soup)
    extractable = main is not None
    if main is None:
        # Measure it anyway, from <body>. The extractor refuses this page, but
        # instrumentation still has to describe what was refused — otherwise a
        # rise in refusals could not be told from a rise in bad pages.
        main = soup.body or soup

    text = main.get_text(" ", strip=True) if main is not None else ""
    anchors = main.select("a") if main is not None else []
    anchor_chars = sum(len(a.get_text(" ", strip=True)) for a in anchors)
    headings = len(main.select("h1,h2,h3,h4,h5,h6")) if main is not None else 0
    code = len(main.select("pre")) if main is not None else 0

    return Observation(
        url=url,
        title=title or "Untitled",
        selector=selector,
        signature=ancestry(main),
        shape=f"h{bucket(headings)}|c{bucket(code)}|a{bucket(len(anchors))}",
        chars=len(text),
        links=len(anchors),
        link_text_ratio=round(anchor_chars / len(text), 3) if text else 0.0,
        code_blocks=code,
        headings=headings,
        scripts=scripts,
        shell=len(text) < MIN_MAIN_CHARS and scripts > 0,
        extractable=extractable,
        density_score=round(density(main), 4) if main is not None else 0.0,
        generator=generator,
        mentions=len(re.findall(re.escape(name), text, re.I)) if name else 0,
        fetched_ms=fetched_ms,
    )


def to_json(ledger: Ledger, indent: int = 2) -> str:
    return json.dumps({"summary": ledger.summary(),
                       "observations": [asdict(o) for o in ledger.observations]},
                      indent=indent)


# ─────────────────────────────────────────────────────────────
# ResolveState — what each resolution attempt revealed
# ─────────────────────────────────────────────────────────────
@dataclass
class ResolveState:
    """Evidence dropped by every candidate a resolution touched.

    `verify()` fetches a candidate, fails it, and would otherwise throw the
    page away — including the repository backlink whose `homepage` field is the
    code owner stating where the documentation lives. Layer 1's evidence lap
    reads this back.
    """

    name: str = ""
    tried: list[str] = field(default_factory=list)
    repos_seen: list[str] = field(default_factory=list)
    outbound: list[str] = field(default_factory=list)
    canonical: dict[str, str] = field(default_factory=dict)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def record(self, url: str, html: str = "") -> None:
        """File one candidate and everything its page gave away."""
        if url not in self.tried:
            self.tried.append(url)
        if not html:
            return

        host = (urlparse(url).hostname or "").lower()
        for repo in _REPO.findall(html):
            if repo not in self.repos_seen:
                self.repos_seen.append(repo)

        found = _CANONICAL.search(html)
        if found:
            self.canonical[url] = found.group(1)

        for href in _HREF.findall(html):
            other = (urlparse(href).hostname or "").lower()
            # Only links that leave this host and look like documentation: a
            # candidate pointing at its own pages says nothing new.
            if other and other != host and _docsy(href) and href not in self.outbound:
                self.outbound.append(href)

    def reject(self, url: str, why: str) -> None:
        self.rejected.append((url, why))

    def summary(self) -> dict:
        return {"name": self.name, "tried": len(self.tried),
                "repos_seen": self.repos_seen, "outbound": self.outbound[:20],
                "canonical": self.canonical, "rejected": self.rejected}


# ─────────────────────────────────────────────────────────────
# Budget — what a lap is allowed to spend
# ─────────────────────────────────────────────────────────────
@dataclass
class Budget:
    """A spend limit, so a pathological name refuses instead of wandering."""

    requests: int = 40             # the proposal's resolution budget
    seconds: float = 20.0
    spent: int = 0
    started: float = field(default_factory=time.time)

    def charge(self, n: int = 1) -> int:
        self.spent += n
        return self.spent

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def requests_left(self) -> int:
        return max(0, self.requests - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.requests or self.elapsed >= self.seconds

    def why(self) -> str:
        """The refusal Layer 1 gives when it runs out."""
        if self.spent >= self.requests:
            return f"gave up after {self.spent} requests"
        if self.elapsed >= self.seconds:
            return f"gave up after {self.elapsed:.1f}s"
        return ""

    def summary(self) -> dict:
        return {"spent": self.spent, "requests_left": self.requests_left,
                "elapsed": round(self.elapsed, 2), "exhausted": self.exhausted}
