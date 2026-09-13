"""
Read-time relevance: return the passage, not the manual.

One page of a generated API reference can be 20,000 tokens spent answering a
one-line question. A documentation tool that costs more context than it saves
has inverted its own purpose.

The ordering here is the load-bearing part, and it is Invariant 6. Selection
decides which corpora are *stored*; relevance is applied on *retrieval*, never
on ingestion. Trimming at harvest time would turn every completeness claim into
a claim about an undisclosed subset — the stored corpus stays whole, and only
what is handed back is narrowed.

Chunking happens at read time rather than at save time. PROPOSAL-II §2.5 asks
for chunks written at save time into `(tech, corpus, kind, version, page_url,
heading_path, text)`, which needs a store schema change; doing it here gets the
same passages out of the data that already exists, and the schema change can
follow when federation actually writes multiple corpora (`ISSUES.md` F1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Markdown headings are emitted as ATX by `_html_to_md`, so this is reliable.
#: h2 and h3 only: h1 is the page title and h4 and below are usually parameter
#: lists, which split a signature away from the prose explaining it.
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)

#: The page's own h1. It is the page title, which every passage already carries
#: separately, so folding it into the heading path would repeat it in every
#: citation: "Retrying > Error Handling > Retrying".
_H1 = re.compile(r"^#\s+.+?$\n*", re.M)

#: Words too common to say anything about relevance.
_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "with", "you", "your",
}


@dataclass(frozen=True)
class Section:
    """One addressable piece of a page."""

    heading_path: str      # "Error Handling > Retrying"
    text: str
    ordinal: int
    page_title: str = ""
    page_url: str = ""

    @property
    def tokens(self) -> int:
        """A rough token count. Good enough to compare two answers."""
        return max(1, len(self.text) // 4)

    def render(self, width: int = 1200) -> str:
        body = self.text if len(self.text) <= width else self.text[:width] + " …"
        where = self.heading_path or self.page_title or "(untitled section)"
        return f"### {where}\n{body}"


def sections(markdown: str, page_title: str = "", page_url: str = "") -> list[Section]:
    """Split a page into sections on its headings, keeping the heading path.

    The heading path is what makes a passage quotable. "Retrying" on its own is
    ambiguous across a large manual; "Error Handling > Retrying" is not, and it
    is what lets a caller cite the answer rather than paraphrase it.
    """
    text = markdown or ""
    marks = list(_HEADING.finditer(text))
    if not marks:
        stripped = text.strip()
        return [Section(page_title, stripped, 0, page_title, page_url)] if stripped else []

    found: list[Section] = []
    # A preamble before the first heading is still content and still findable.
    lead = _H1.sub("", text[:marks[0].start()]).strip()
    if lead:
        found.append(Section(page_title, lead, 0, page_title, page_url))

    trail: list[str] = []
    for i, mark in enumerate(marks):
        level, heading = len(mark.group(1)), mark.group(2).strip()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()

        depth = level - 2                    # h2 is the top of a page's structure
        trail = trail[:max(0, depth)]
        while len(trail) < depth:
            trail.append("")
        trail.append(heading)
        path = " > ".join(part for part in trail if part)

        if body:
            found.append(Section(path, body, len(found), page_title, page_url))
    return found


def _terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9_.]+", (query or "").lower())
    return [w for w in words if w not in _STOP and len(w) > 1]


def score(section: Section, terms: list[str]) -> float:
    """How well this section answers the query.

    Heading matches count for more than body matches: a section *titled* after
    the thing you asked about is about it, whereas a body mention may be an
    aside. Long sections are lightly discounted so a whole page does not beat a
    precise answer merely by containing more words.
    """
    if not terms:
        return 0.0
    heading = section.heading_path.lower()
    body = section.text.lower()

    hits = 0.0
    for term in terms:
        if term in heading:
            hits += 3.0
        hits += min(body.count(term), 5) * 1.0
    if not hits:
        return 0.0
    # Mild length normalisation, not aggressive: a long section that genuinely
    # repeats the term should still win over a short one that mentions it once.
    return hits / (1.0 + section.tokens / 4000.0)


def rank(query: str, chunks: list[Section], limit: int = 5) -> list[Section]:
    """The best passages for a query, most relevant first."""
    terms = _terms(query)
    scored = [(score(chunk, terms), chunk) for chunk in chunks]
    keep = [(s, c) for s, c in scored if s > 0]
    keep.sort(key=lambda pair: -pair[0])
    return [chunk for _s, chunk in keep[:limit]]


