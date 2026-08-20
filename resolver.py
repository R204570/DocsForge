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
    #: Which identity checks fired. `verified: true` on its own is what made
    #: the wrong answers dangerous — a caller shown the reasons can disagree.
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "url": self.url, "source": self.source,
            "confidence": round(self.confidence, 2), "evidence": self.evidence,
            "verified": self.verified, "reason": self.reason,
            "signals": list(self.signals),
        }


@dataclass
class Resolution:
    name: str
    ecosystem: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    best: Candidate | None = None
    note: str = ""
    #: "domain", "registry", or "" when nothing resolved. Part of the honesty
    #: contract: how an answer was reached bears on how much to trust it.
    resolved_via: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ecosystem": self.ecosystem,
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "note": self.note, "resolved_via": self.resolved_via,
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
          "codeberg.org", "git.sr.ht", "githubusercontent.com")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_forge(url: str) -> bool:
    """Is this URL on a code host rather than a project's own site?

    Matched by suffix, not equality. Exact matching let `gist.github.com` and
    `raw.githubusercontent.com` through, and probing them offered GitHub's own
    `llms.txt` as the documentation for whatever had been asked about — it
    entered one live resolution at 0.95, the top-scoring candidate of the run,
    and lost only because that file happened not to contain the word.
    """
    host = _host(url)
    return any(host == forge or host.endswith("." + forge) for forge in FORGES)


#: How much readable text a page must carry before it counts as documentation.
#: Tuned against the measured failure: the real Astro docs root returns 3
#: characters, the marketing homepage 6,448.
MIN_PROBE_TEXT = 200

#: `<meta http-equiv="refresh" content="0; url=…">`, the usual shape of the
#: stub that sits where a docs root used to be.
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*"""
    r"""["'][^"']*url\s*=\s*([^"'\s>]+)""", re.I)

#: A stub that redirects with a script instead: `location.href = "..."`.
_JS_REDIRECT = re.compile(
    r"""location(?:\.href|\.replace\()?\s*=?\s*\(?\s*["']([^"']+)["']""", re.I)


def _visible_text(html: str) -> str:
    """Roughly what a reader would see, for measuring whether a page is empty."""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


def _follow_client_redirect(response, base: str, fetcher: Fetcher):
    """Follow one hop of a redirect the HTTP layer cannot see.

    `requests` follows 3xx, but a page that redirects with a meta tag or a line
    of JavaScript arrives as a perfectly good 200 holding nothing. That is not
    a page, it is a signpost, and the thing it points at is the answer.
    """
    html = getattr(response, "text", "") or ""
    if len(html) > 4_000:            # a real page, not a signpost
        return None
    match = _META_REFRESH.search(html) or _JS_REDIRECT.search(html)
    if not match:
        return None
    target = urljoin(base, match.group(1).strip())
    if target.rstrip("/") == base.rstrip("/"):
        return None
    try:
        hop = fetcher.get(target, timeout=PROBE_TIMEOUT, allow_redirects=True)
    except ForgeError:
        return None
    return hop if hop.status_code == 200 else None


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


def _facts_from(found: list[Candidate], ecosystem: str) -> dict:
    """What the registries claimed, for the identity checks to test against.

    These are the independent statements a candidate page can agree with: the
    repository the package declares, and the homepage it declares. Agreement
    between two sources that never consulted each other is the evidence a
    mention count cannot provide.
    """
    facts: dict = {"ecosystem": ecosystem}
    for cand in found:
        tail = cand.source.rsplit(":", 1)[-1].lower()
        if "repo" in tail or "source" in tail:
            facts.setdefault("repository", cand.url)
        elif "home" in tail:
            facts.setdefault("homepage", cand.url)
    return facts


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
        if "html" in ctype and not path.endswith(".txt"):
            # An HTTP 200 is not a documentation root. `docs.astro.build`
            # answers 200 with an 80-byte client-side redirect shell holding
            # three characters of text, and accepting it cost the *correct*
            # answer: it entered the pool, failed verification, and handed the
            # win to the marketing homepage.
            hop = _follow_client_redirect(r, target, fetcher)
            if hop is not None:
                r = hop
            if len(_visible_text(getattr(r, "text", ""))) < MIN_PROBE_TEXT:
                continue
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
# The project's own domain
# ─────────────────────────────────────────────────────────────
#: Tried in order, and kept short because each one costs a request. `.com`
#: is last: it is the most heavily squatted, so it is the least trustworthy
#: evidence that the project owns the name.
NAME_TLDS = ("dev", "io", "org", "com")

#: A missing domain fails fast at DNS, so this can be tight.
DOMAIN_TIMEOUT = 6

#: How many live domains get the full docs-root treatment. Bounded because
#: each one costs a handful of requests, and past the second the returns are
#: not worth the latency.
DOMAINS_EXPLORED = 2


#: Evidence that a site is about software at all, rather than merely owning
#: the word. Without this gate `astro` resolves to an astrology site: it owns
#: astro.com, it is enormous, and it says "astro" constantly — which is every
#: signal a name-and-size check has, and none of the ones that matter.
_FORGE_LINK = re.compile(r"https?://(?:www\.)?(?:github|gitlab|bitbucket)\.com/[\w.\-]+/", re.I)
_CODE_BLOCK = re.compile(r"<(?:code|pre)[\s>]", re.I)


def _looks_like_software(body: str, slug: str) -> str:
    """Why this page appears to be a software project's, or "" if it does not."""
    if _install_line(re.sub(r"<[^>]+>", " ", body), slug):
        return "an install command"
    if _FORGE_LINK.search(body):
        return "a link to its source repository"
    if len(_CODE_BLOCK.findall(body)) >= 3:
        return "code samples"
    return ""


def _domain_score(origin: str, landed: str, html: str, slug: str) -> int:
    """How strongly this domain looks like *the* home of the technology.

    Two live domains can both pass the software gate — `terraform.io` and
    `terraform.com` did, and page size preferred the wrong one. What separates
    them is deliberateness: `terraform.io` redirects to
    `developer.hashicorp.com/terraform`, and a name-domain pointed at a
    project-specific path somewhere else is somebody consolidating their
    documentation. A site that simply serves itself has made no such claim.
    """
    score = 0
    if _host(landed) != _host(origin):
        score += 2
        if slug in urlparse(landed).path.lower():
            score += 3
    if _install_line(re.sub(r"<[^>]+>", " ", html), slug):
        score += 2
    if _FORGE_LINK.search(html):
        score += 1
    return score


def from_domains(name: str, fetcher: Fetcher) -> list[Candidate]:
    """Look for the project's own site before asking anyone else.

    Measured, this is the whole ballgame: every correct resolution in the audit
    came from the project's own domain, and every wrong one came through a
    registry. Registries answer "what package is named X", which is a different
    question from "what is the technology X" — and when the two disagree,
    `terraform` is an unrelated static-site tool and `kubernetes` is a Python
    client library.
    """
    slug = normalise(name)
    if not slug or len(slug) < 2:
        return []

    live: list[tuple[int, str, str]] = []
    for tld in NAME_TLDS:
        origin = f"https://{slug}.{tld}"
        try:
            r = fetcher.get(origin, timeout=DOMAIN_TIMEOUT, allow_redirects=True)
        except ForgeError:
            continue
        if r.status_code != 200 or "html" not in (
                r.headers.get("content-type") or "").lower():
            continue
        html = getattr(r, "text", "")
        text = len(_visible_text(html))
        if text < MIN_PROBE_TEXT:
            continue
        # Owning the word is not the same as being the software. Something on
        # the page has to say "this is a code project" before the domain counts
        # as the project's, or any common noun resolves to whoever bought it.
        why = _looks_like_software(html, slug)
        if not why:
            continue
        landed = getattr(r, "url", "") or origin
        live.append((_domain_score(origin, landed, html, slug), text, tld, landed, why))

    # A project can own several of these and put different things on them:
    # `kubernetes.dev` is the contributor portal and `kubernetes.io` the
    # documentation, while `terraform.com` is a different company entirely.
    # Rank on deliberate evidence first and volume of text only as a tiebreak.
    live.sort(reverse=True)

    out: list[Candidate] = []
    for _score, text, tld, landed, why in live[:DOMAINS_EXPLORED]:
        # The homepage is usually marketing with the docs one click away, so
        # the docs root under it outranks it.
        for found in probe_docs_root(landed, fetcher):
            found.source = f"domain:{tld}/{found.source}"
            found.confidence = min(0.97, found.confidence + 0.02)
            out.append(found)
        out.append(Candidate(landed, f"domain:{tld}", 0.75,
                             f"{slug}.{tld} is the project's own domain and "
                             f"carries {why}"))
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


#: Install commands, per ecosystem. A page that tells you how to install the
#: package is a page about that package, in that ecosystem — which is the
#: distinction `htmx` needed: `npm i htmx` and `cargo add htmx` are different
#: projects that happen to share a word.
_INSTALL = (
    ("npm", r"(?:npm|pnpm|bun)\s+(?:i|add|install|create)\s+(?:-\w+\s+)*"),
    ("npm", r"yarn\s+add\s+"),
    ("pypi", r"(?:pip|pip3|uv pip|poetry add|conda install)\s+(?:install\s+)?"),
    ("crates", r"cargo\s+add\s+"),
    ("go", r"go\s+get\s+(?:[\w.\-/]+/)?"),
)


def _owns_the_name(url: str, slug: str) -> bool:
    """Is the name a whole label of this host?

    `htmx.org`, `kubernetes.io`, `docs.pydantic.dev`, `fastapi.tiangolo.com` —
    all the project itself. `github.com/sintaxi/terraform` and `docs.rs/htmx`
    are not, and that single distinction separates every correct answer in the
    audit from every wrong one. Owning a label in the hostname is a far
    stronger claim on a bare name than being one package in one namespaced,
    first-come registry.
    """
    if not slug or is_forge(url):
        return False
    labels = _host(url).split(".")
    flat = slug.replace("-", "")
    return any(label == slug or label.replace("-", "") == flat for label in labels)


def _install_line(body: str, slug: str) -> str:
    """The ecosystem an install command on this page names, or ""."""
    for eco, prefix in _INSTALL:
        # The package may be scoped or path-qualified; anchor on the bare name.
        if re.search(prefix + r"[\"'@\w./\-]*\b" + re.escape(slug) + r"\b",
                     body, re.I):
            return eco
    return ""


def identity_signals(candidate: Candidate, name: str, body: str,
                     facts: dict | None = None) -> list[str]:
    """Independent reasons to believe this page documents *this* project.

    Counting how often a page says a word measures its topic, not its
    identity: a page about any project called terraform says "terraform"
    constantly. What distinguishes projects is agreement between sources that
    did not consult each other — the host owning the name, an install line in
    the right ecosystem, a link back to the repository the registry declared.
    """
    slug = normalise(name)
    facts = facts or {}
    text = re.sub(r"<[^>]+>", " ", body)
    found: list[str] = []

    # A project's domain may redirect off itself — terraform.io lands on
    # developer.hashicorp.com — so how we arrived counts, not just where.
    if facts.get("via_domain") or _owns_the_name(candidate.url, slug):
        found.append("own-domain")

    eco = _install_line(text, slug)
    if eco:
        wanted = facts.get("ecosystem")
        found.append(f"install:{eco}" if not wanted or wanted == eco
                     else f"install-mismatch:{eco}")

    repo = (facts.get("repository") or "").rstrip("/")
    if repo:
        path = urlparse(repo).path.strip("/").lower()
        if path and path in body.lower() and candidate.url.rstrip("/") != repo:
            found.append("repo-backlink")

    home = facts.get("homepage") or ""
    if home and _host(home) and _host(home) == _host(candidate.url):
        found.append("registry-agreement")

    hits = normalise(text).count(slug) if slug else 0
    if hits >= MIN_MENTIONS:
        found.append(f"names-it:{hits}")
    return found


#: Signals that identify a project rather than merely describe one. Mention
#: counts are deliberately excluded: they are corroboration, never proof.
STRONG = ("own-domain", "install:", "repo-backlink", "registry-agreement")


def is_identified(signals: list[str]) -> bool:
    """Two independent sources agreeing, or one strong source plus the name.

    A wrong answer is survivable. A wrong answer stamped `verified` is not,
    because the caller has been given a reason to stop checking — so the bar
    is agreement, not familiarity.
    """
    strong = [s for s in signals if s.startswith(STRONG)]
    named = any(s.startswith("names-it") for s in signals)
    if any(s.startswith("install-mismatch") for s in signals) and len(strong) < 2:
        return False
    return len(strong) >= 2 or (len(strong) == 1 and named)


def verify(candidate: Candidate, name: str, fetcher: Fetcher,
           facts: dict | None = None) -> Candidate:
    """Confirm a page documents the project that was asked for.

    Without this the resolver is a more elaborate guess: a plausible-looking
    URL gets harvested and summarised, and nobody finds out it was the wrong
    project.
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

    candidate.signals = identity_signals(candidate, name, body, facts)
    candidate.verified = is_identified(candidate.signals)
    if candidate.verified:
        candidate.reason = "identified by " + ", ".join(candidate.signals)
    elif candidate.signals:
        candidate.reason = (
            f"not enough to identify {name!r} — only {', '.join(candidate.signals)}. "
            f"Naming a project is not the same as being it.")
    else:
        candidate.reason = f"nothing on the page identifies it as {name!r}"
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
        # 1. The project's own domain. Checked first because the data says so:
        #    it produced every correct answer and none of the wrong ones.
        domain = from_domains(name, fetcher)
        if verify_best:
            for cand in domain:
                verify(cand, name, fetcher, {"via_domain": True})
                if cand.verified:
                    result.candidates = domain[:limit]
                    result.best = cand
                    result.resolved_via = "domain"
                    result.note = (
                        f"Resolved from {name!r}'s own domain. Registries were not "
                        f"consulted: owning the name is the stronger claim, and "
                        f"where the two disagree the registry is usually a "
                        f"different project that shares the word.")
                    return result

        # 2. Registries, as the fallback.
        found, hit = from_registries(name, result.ecosystem, fetcher)
        if hit:
            result.ecosystem = result.ecosystem or hit
        if not found:
            result.candidates = domain[:limit]
            result.note = (
                f"No registry knows {name!r}. If it is private or internal, "
                f"pass the documentation URL directly to harvest_docs."
            )
            return result
        found += domain

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

        facts = _facts_from(found, result.ecosystem)
        if verify_best:
            for cand in result.candidates:
                verify(cand, name, fetcher,
                       dict(facts, via_domain=cand.source.startswith("domain:")))
                if cand.verified:
                    result.best = cand
                    result.resolved_via = ("domain" if cand.source.startswith("domain:")
                                           else "registry")
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
