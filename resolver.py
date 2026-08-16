"""
Turn a technology name into documentation URLs.

This is the hop DocsForge was missing. Every tool that acquires documentation
needed a URL, but the caller is a model that has just met a library it does not
know — so the one thing it cannot supply is where that library's documentation
lives. Left with no tool, it guesses a URL from the same stale training data
the product exists to bypass, and a guess can resolve to a real, *wrong* page
and be harvested and summarised with complete confidence.

The chain, cheapest first:

    1. package registry   name -> homepage / documentation / repository
    2. convention probe   host -> llms.txt, sitemap, a /docs root
    3. verification       does that page actually document this package?

Only a verified candidate is worth harvesting. A resolver that is merely
*usually* right recreates the guessing bug with extra steps, so "I could not
resolve this, give me a URL" is a supported and preferred outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from docsforge import Fetcher, ForgeError, Options

#: Registries are keyless and public, but not unlimited: keep the timeout tight
#: and never make more than a couple of calls per resolution.
REGISTRY_TIMEOUT = 12
PROBE_TIMEOUT = 10

#: Paths worth trying on a candidate host when the registry only gave us a
#: marketing homepage. Ordered by how likely each is to *be* the docs root.
DOC_PATHS = ("/llms.txt", "/docs/", "/docs", "/documentation/", "/guide/",
             "/en/latest/", "/latest/")


@dataclass
class Candidate:
    """A possible home for a technology's documentation."""

    url: str
    source: str                  # where the suggestion came from
    confidence: float            # 0..1, before verification
    evidence: str = ""
    verified: bool | None = None  # None = not checked yet
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "url": self.url, "source": self.source,
            "confidence": round(self.confidence, 2), "evidence": self.evidence,
            "verified": self.verified, "reason": self.reason,
        }


@dataclass
class Resolution:
    name: str
    ecosystem: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    best: Candidate | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ecosystem": self.ecosystem,
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "note": self.note,
        }


# ─────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────
#: Suffixes people attach to a library's name in prose but never in its
#: package name: "Effect.ts", "Vue.js", "pydantic-py".
_DRESSING = re.compile(r"(\.|-)(js|ts|py|rs|go|dev|io)$", re.I)


def normalise(name: str) -> str:
    """A name reduced to the form two spellings of it can be compared in.

    `Effect.ts`, `effect-ts` and `effect` are the same library; a lookup that
    only matches the exact stored slug makes the caller guess our filing
    convention, which it has no way to know.
    """
    text = (name or "").strip().lower()
    if text.startswith("@") and "/" in text:      # @scope/pkg -> pkg
        text = text.split("/", 1)[1]
    text = _DRESSING.sub("", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def guess_ecosystem(name: str) -> str:
    """The registry a name most likely belongs to, or "" for unknown."""
    if name.startswith("@") and "/" in name:
        return "npm"
    if "::" in name or name.endswith("-rs"):
        return "crates"
    return ""


# ─────────────────────────────────────────────────────────────
# Registries
# ─────────────────────────────────────────────────────────────
def _json(fetcher: Fetcher, url: str) -> dict | None:
    try:
        r = fetcher.get(url, timeout=REGISTRY_TIMEOUT)
    except ForgeError:
        return None
    if r.status_code != 200:
        return None
    try:
        data = json.loads(r.text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_repo(url: str) -> str:
    """A git remote as a browsable https URL."""
    text = (url or "").strip()
    text = re.sub(r"^git\+", "", text)
    text = re.sub(r"^git://", "https://", text)
    text = re.sub(r"^ssh://git@", "https://", text)
    text = re.sub(r"^git@([^:]+):", r"https://\1/", text)
    text = re.sub(r"\.git$", "", text)
    return text if text.startswith("http") else ""


#: Code hosts. Their origin belongs to the forge, not to any project on it, so
#: probing `github.com/llms.txt` finds GitHub's own file and offers it as the
#: documentation for whatever package happened to be asked about.
FORGES = ("github.com", "gitlab.com", "bitbucket.org", "sourceforge.net",
          "codeberg.org", "git.sr.ht")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_forge(url: str) -> bool:
    return _host(url) in FORGES


def _looks_like_docs(url: str) -> bool:
    host = _host(url)
    path = urlparse(url).path.lower()
    return (host.startswith("docs.") or ".readthedocs." in host
            or "/docs" in path or "/documentation" in path or "/guide" in path)


def _score(url: str, field_name: str) -> float:
    """How much to trust a URL, by which field it came out of.

    An explicit `documentation` field is the package author saying where the
    docs are. A homepage is a guess that often lands on marketing. And a field
    of any name pointing at a code host is a *repository* — some registries
    put a GitHub link in `documentation`, and taking that at face value ranks a
    repo above the project's actual documentation site.
    """
    if not url:
        return 0.0
    if is_forge(url):
        return 0.35
    if field_name == "documentation":
        return 0.92
    if field_name == "homepage":
        return 0.78 if _looks_like_docs(url) else 0.55
    return 0.35        # repository


def _npm(name: str, fetcher: Fetcher) -> list[Candidate]:
    data = _json(fetcher, f"https://registry.npmjs.org/{name}")
    if not data:
        return []
    out = []
    home = (data.get("homepage") or "").strip()
    if home.startswith("http"):
        out.append(Candidate(home, "npm:homepage", _score(home, "homepage"),
                             f"npm registry homepage for {name}"))
    repo = data.get("repository")
    repo_url = _clean_repo(repo.get("url") if isinstance(repo, dict) else repo or "")
    if repo_url:
        out.append(Candidate(repo_url, "npm:repository", _score(repo_url, "repository"),
                             f"npm registry repository for {name}"))
    return out


def _pypi(name: str, fetcher: Fetcher) -> list[Candidate]:
    data = _json(fetcher, f"https://pypi.org/pypi/{name}/json")
    if not data:
        return []
    info = data.get("info") or {}
    out = []

    # project_urls is where modern packages actually declare their docs.
    for label, url in (info.get("project_urls") or {}).items():
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        low = label.lower()
        if "doc" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "documentation"),
                                 f"PyPI project_urls[{label}] for {name}"))
        elif "home" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "homepage"),
                                 f"PyPI project_urls[{label}] for {name}"))
        elif "source" in low or "repo" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "repository"),
                                 f"PyPI project_urls[{label}] for {name}"))

    home = (info.get("home_page") or "").strip()
    if home.startswith("http"):
        out.append(Candidate(home, "pypi:home_page", _score(home, "homepage"),
                             f"PyPI home_page for {name}"))
    return out


def _crates(name: str, fetcher: Fetcher) -> list[Candidate]:
    data = _json(fetcher, f"https://crates.io/api/v1/crates/{name}")
    crate = (data or {}).get("crate") or {}
    out = []
    for key, kind in (("documentation", "documentation"),
                      ("homepage", "homepage"),
                      ("repository", "repository")):
        url = (crate.get(key) or "").strip()
        if url.startswith("http"):
            out.append(Candidate(url, f"crates:{key}", _score(url, kind),
                                 f"crates.io {key} for {name}"))
    return out


REGISTRIES = {"npm": _npm, "pypi": _pypi, "crates": _crates}


def from_registries(name: str, ecosystem: str, fetcher: Fetcher) -> tuple[list[Candidate], str]:
    """Ask the package registries where this library documents itself.

    With no ecosystem hint every registry is tried, because the same name can
    exist in several and the caller usually does not know which one it meant.
    """
    order = [ecosystem] if ecosystem in REGISTRIES else list(REGISTRIES)
    found: list[Candidate] = []
    hit = ""
    for eco in order:
        got = REGISTRIES[eco](name, fetcher)
        if got:
            hit = hit or eco
            found += got
            if ecosystem:
                break
    return found, hit


# ─────────────────────────────────────────────────────────────
# Probing
# ─────────────────────────────────────────────────────────────
def probe_docs_root(url: str, fetcher: Fetcher) -> list[Candidate]:
    """Look for a documentation root on a host the registry pointed at.

    A registry homepage is very often a marketing page with the docs one click
    away, so a bare homepage is worth one cheap round of convention-guessing
    before it is either used or discarded.
    """
    if not urlparse(url).netloc or is_forge(url):
        # A repository's origin is the code host, not the project. Probing it
        # offers GitHub's own llms.txt as the docs for whatever was asked for.
        return []
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    out: list[Candidate] = []
    for path in DOC_PATHS:
        target = urljoin(origin + "/", path.lstrip("/"))
        try:
            r = fetcher.get(target, timeout=PROBE_TIMEOUT, allow_redirects=True)
        except ForgeError:
            continue
        if r.status_code != 200:
            continue
        ctype = (r.headers.get("content-type") or "").lower()
        if path.endswith(".txt"):
            if "html" in ctype:
                continue
            # A published llms.txt is the site describing itself for machines;
            # nothing beats it.
            out.append(Candidate(r.url, "probe:llms.txt", 0.95,
                                 f"{target} exists and is not HTML"))
        elif "html" in ctype:
            out.append(Candidate(r.url, f"probe:{path}", 0.7,
                                 f"{target} returned a page"))
        if out and out[-1].confidence >= 0.95:
            break
    return out


# ─────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────
#: How much of a page to read when checking it is the right library. The name
#: should appear early — in the title, a heading, or the first code sample.
VERIFY_WINDOW = 40_000

#: How many times a page must name the package before it counts as documenting
#: it. One mention is noise; a real docs page repeats it constantly.
MIN_MENTIONS = 3


def verify(candidate: Candidate, name: str, fetcher: Fetcher) -> Candidate:
    """Confirm a page actually documents the package that was asked for.

    Without this the resolver is a more elaborate guess: a plausible-looking
    URL gets harvested and summarised, and nobody finds out it was the wrong
    project. Cheap to do, and the difference between a loud failure and a
    silent wrong answer.
    """
    try:
        body = fetcher.text(candidate.url, timeout=PROBE_TIMEOUT)[:VERIFY_WINDOW]
    except ForgeError as e:
        candidate.verified = False
        candidate.reason = f"could not be read: {e}"
        return candidate

    slug = normalise(name)
    if not slug:
        candidate.verified = False
        candidate.reason = "no usable name to check against"
        return candidate

    haystack = normalise(re.sub(r"<[^>]+>", " ", body))
    # A single stray occurrence proves nothing — every page on the internet
    # mentions "effect" once. A page that documents a library says its name
    # repeatedly: in the title, the install line, the imports.
    hits = haystack.count(slug)
    if hits >= MIN_MENTIONS:
        candidate.verified = True
        candidate.reason = f"names {name!r} {hits} times"
    elif hits:
        candidate.verified = False
        candidate.reason = (f"mentions {name!r} only {hits} time"
                            f"{'s' if hits != 1 else ''} — too weak to trust")
    else:
        candidate.verified = False
        candidate.reason = f"never mentions {name!r}"
    return candidate


# ─────────────────────────────────────────────────────────────
# The chain
# ─────────────────────────────────────────────────────────────
def resolve(name: str, ecosystem: str = "", fetcher: Fetcher | None = None,
            verify_best: bool = True, limit: int = 6) -> Resolution:
    """Find where `name` documents itself.

    Returns every candidate with its evidence rather than silently picking one,
    so a caller that disagrees can see why and choose differently.
    """
    result = Resolution(name=name, ecosystem=ecosystem or guess_ecosystem(name))
    own = fetcher is None
    fetcher = fetcher or Fetcher(Options(delay=0.0))

    try:
        found, hit = from_registries(name, result.ecosystem, fetcher)
        if hit:
            result.ecosystem = result.ecosystem or hit
        if not found:
            result.note = (
                f"No registry knows {name!r}. If it is private or internal, "
                f"pass the documentation URL directly to harvest_docs."
            )
            return result

        # A homepage is worth one round of convention-guessing before use.
        extra: list[Candidate] = []
        for cand in list(found):
            if cand.confidence < 0.9 and not _looks_like_docs(cand.url):
                extra += probe_docs_root(cand.url, fetcher)

        seen, ranked = set(), []
        for cand in sorted(found + extra, key=lambda c: c.confidence, reverse=True):
            key = cand.url.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            ranked.append(cand)
        result.candidates = ranked[:limit]

        if verify_best:
            for cand in result.candidates:
                verify(cand, name, fetcher)
                if cand.verified:
                    result.best = cand
                    # The ecosystem is whichever registry actually produced the
                    # answer, not whichever one happened to reply first: the
                    # same name often exists in several, on different projects.
                    won = cand.source.split(":", 1)[0]
                    if won in REGISTRIES:
                        result.ecosystem = won
                    break
            if result.best is None:
                result.note = (
                    f"Found {len(result.candidates)} candidate(s) for {name!r} but none "
                    f"could be confirmed to document it. Harvesting an unverified page "
                    f"risks storing the wrong project — pass a URL directly if you know it."
                )
        elif result.candidates:
            result.best = result.candidates[0]
        return result
    finally:
        if own:
            fetcher.close()
