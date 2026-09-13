"""
LLMSFinder: Acquisition ladder engine for DocsForge.

Implements published-first documentation acquisition as specified in `llmsfinder.md`.
Prefers published machine-readable files (llms-full.txt, llms.txt index/manifest)
over inferred sitemaps and deep crawling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


#: Markdown link pattern: [title](url)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

#: A `Version:` line, as sites actually write it in an `llms.txt` header.
#: The convention has no schema — it fixes the title and the `>` summary and
#: leaves everything else to the publisher — but where a site states which
#: release the file documents, this is the shape it states it in.
_VERSION_LINE = re.compile(r"^[ \t]*version[ \t]*:[ \t]*(\S+)[ \t]*$", re.I | re.M)

#: How far into a file the header is still the header, for sites that put no
#: `##` section in at all.
_HEADER_CHARS = 2_000


def declared_version(text: str) -> str:
    """The version an `llms.txt` states about itself, or `""`.

    Read only from the header — everything above the first `##` section —
    because further down a `Version:` line is documentation *about* versions
    (a changelog entry, an installation transcript, a CLI example) rather
    than a claim this file makes about itself.

    Returns the raw token and judges nothing: whether it is a release number
    worth filing a harvest under is a question `versions.py` already answers,
    and answering it twice is how two rankings drift apart.
    """
    body = (text or "").strip()
    if not body:
        return ""
    head = re.split(r"^##[ \t]", body, maxsplit=1, flags=re.M)[0][:_HEADER_CHARS]
    match = _VERSION_LINE.search(head)
    return match.group(1).strip() if match else ""


#: A manifest entry is a line whose content *begins* with a link — `- [Title](url)`
#: — because that is what a table of contents is made of. A link buried inside a
#: sentence is a cross-reference, and counting the whole line it sits on as
#: "link" made prose look like a manifest: `docs.langchain.com/llms-full.txt` is
#: 6.7 MB of documentation with 9,542 citations threaded through it, and the
#: old measure read that as 20.7% links against 4.3% for this one.
_MANIFEST_LINE = re.compile(r"^\s*(?:[-*+]\s*)?\[[^\]]+\]\([^)\s]+\)")

#: A file published under one of these names *is* the full text. That is what
#: the convention means, and it is the publisher saying so about their own file.
_FULL_FILE = re.compile(r"llms-(?:full|medium)\.txt$", re.I)

#: A file that points at a fuller version of itself is an index, however much
#: prose it carries — unless it is the fuller version, where the mention is
#: either itself or one of its own sub-corpora.
_NAMES_FULLER = re.compile(r"llms-(?:full|medium)\.txt", re.I)

#: Almost entirely a list of links.
INDEX_DENSITY = 0.5
#: Enough manifest lines to be a manifest rather than a few citations.
HYBRID_DENSITY = 0.03


def classify_llms_shape(text: str, url: str = "") -> str:
    """Classify an `llms.txt` document into Shape A (index), Shape B (dump), or Shape C (hybrid).

    - **Shape A (index)**: High ratio of manifest-line characters to total
      characters. A table of contents / list of links.
    - **Shape B (dump)**: Low link density, predominantly prose documentation.
    - **Shape C (hybrid)**: Substantial prose *and* multiple manifest links.

    Uses arithmetic character and link density ratios without model calls.

    `url` is the address the body came from, and it settles the one case
    arithmetic cannot. The rule that a file naming `llms-full.txt` is an index
    exists so a stub that points at the fuller file sends us to the fuller
    file. Applied to that fuller file it is self-defeating, and measurably so:
    `docs.langchain.com/llms-full.txt` mentions `llms-full.txt` twice — both
    times pointing at its own Python and TypeScript sub-corpora — so 6.7 MB of
    documentation already in hand was classified as a table of contents,
    discarded, and replaced by 813 individual HTML fetches of which 186 failed.
    Twelve and a half minutes and a 23% loss, to re-acquire what one request
    had already returned whole.

    Naming the file is the publisher's own statement about what it contains,
    which is the same principle that puts `llms.txt` above a sitemap: a file a
    site published about itself outranks anything inferred about it. A stub
    that merely *calls itself* full is still caught, because a body that really
    is almost all links is an index whatever it is named.
    """
    body = (text or "").strip()
    if not body:
        return "index"

    links = _LINK_RE.findall(body)
    if not links:
        return "dump"

    lines = body.splitlines()
    non_empty_lines = [l.strip() for l in lines if l.strip()]
    if not non_empty_lines:
        return "index"

    manifest_lines = [l for l in non_empty_lines if _MANIFEST_LINE.match(l)]
    total_chars = len(body)
    density = sum(len(l) for l in manifest_lines) / max(1, total_chars)

    is_full_file = bool(_FULL_FILE.search(urlparse(url).path)) if url else False

    # Almost entirely links: an index, whatever the file is called.
    if density >= INDEX_DENSITY:
        return "index"

    # Points at a fuller file than itself.
    if not is_full_file and _NAMES_FULLER.search(body):
        return "index"

    # Published as the full text, and its body does not contradict that.
    if is_full_file:
        return "dump"

    # Hybrid has substantial prose AND multiple manifest links.
    if len(manifest_lines) >= 3 and HYBRID_DENSITY <= density < INDEX_DENSITY \
            and total_chars >= 300:
        return "hybrid"

    return "dump"


#: Below this median page size a corpus is a set of stubs, not prose
#: documentation. Measured over every corpus in a real store rather than
#: tuned against the one example that raised the question:
#:
#:      median  <500ch  pages   corpus
#:         169     65%   1799   google-adk      split dump fragments
#:         490     53%    560   langchain       one API symbol per page
#:       2,112      0%     87   gin-gonic
#:       3,896     11%    385   astro
#:       3,973      5%    703   effect
#:       4,411      0%     13   mojo
#:       6,879      1%     85   pydantic
#:      17,870      0%     16   go-net-http
#:
#: Everything genuine sits at 2,112 or above; both stub corpora sit at 490 or
#: below. 1,200 is the midpoint of a 4.3x gap, so it is not a knife edge.
STUB_MEDIAN = 1_200

#: A median over three pages is noise, and a small corpus of short pages is a
#: perfectly ordinary thing — a handful of guides, a README. The claim needs
#: enough pages to be a claim.
STUB_MIN_PAGES = 20


def reads_as_stubs(sizes: list[int]) -> bool:
    """Is this a set of stubs rather than documentation to read?

    Not a judgement about worth: an API symbol index is real documentation and
    is exactly what you want when you need a signature. It is a statement about
    *shape*, and the shape matters because a caller who asked for "the
    documentation" and received 560 pages averaging 490 characters — one
    attribute each — has been handed something that cannot answer the question
    they were going to ask.

    Arithmetic, deliberately, like every other decision in this module. No
    model call, no heuristic about wording.
    """
    if len(sizes) < STUB_MIN_PAGES:
        return False
    ordered = sorted(sizes)
    median = ordered[len(ordered) // 2]
    return median < STUB_MEDIAN


def density_note(sizes: list[int]) -> str:
    """One line for the caller when a corpus reads as stubs, else "".

    Said rather than acted on. Refusing the harvest would throw away a real
    corpus over a threshold, and silently switching to a different site would
    be guessing at what was wanted — so this reports, and the caller decides.
    """
    if not reads_as_stubs(sizes):
        return ""
    ordered = sorted(sizes)
    median = ordered[len(ordered) // 2]
    tiny = sum(1 for s in sizes if s < 500)
    return (
        f"NOTE: these {len(sizes)} pages have a median of {median:,} characters "
        f"and {tiny} of them are under 500. That is the shape of an API symbol "
        f"index or a split dump, not prose documentation — useful for looking up "
        f"a signature, thin for learning how something works. If you wanted "
        f"guides and tutorials, check whether the project publishes them "
        f"separately."
    )


def parse_llms_links(text: str, base_url: str) -> list[tuple[str, str]]:
    """Extract (title, absolute_url) from an llms.txt index."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for title, target in _LINK_RE.findall(text or ""):
        target = target.strip()
        if not target or target.startswith(("#", "mailto:", "javascript:")):
            continue
        try:
            full_url = urljoin(base_url, target)
            p = urlparse(full_url)
            if p.scheme not in ("http", "https") or not p.netloc:
                continue
        except Exception:
            continue

        # Strip fragment for document fetching deduplication
        clean_url = full_url.split("#")[0]
        if clean_url in seen:
            continue
        seen.add(clean_url)
        clean_title = title.strip() or "Untitled"
        found.append((clean_title, clean_url))

    return found


def is_markdown_link(url: str) -> bool:
    """Check if a URL points directly to a Markdown document twin."""
    path = urlparse(url).path.lower()
    return path.endswith((".md", ".markdown", ".raw.txt")) or "/raw/" in path
