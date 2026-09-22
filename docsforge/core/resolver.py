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
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from docsforge.core.engine import Fetcher, ForgeError, Options
from docsforge.core.instrument import Budget, ResolveState

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
    #: What claim this host makes on the name — see `name_authority`. Set by
    #: `verify`, because it depends on the signals that verification produced.
    #: Recorded on the candidate rather than recomputed inside `evidence` so
    #: that a caller comparing two answers can see the grade that decided it.
    authority: int = 0
    #: The registry's current release, when the candidate came from one.
    #: What a site that publishes one version at a time is documenting.
    #: Last, so the positional construction the tests use stays valid.
    release: str = ""
    #: Which registry this candidate descends from, when any: set on what
    #: `from_registries` returns and inherited by a docs root probed beneath
    #: it. What a candidate is *judged against*. The same word exists on
    #: several registries, on different projects, and a PyPI project's page
    #: tested against npm's claims — npm's ecosystem, npm's repository —
    #: earns `install-mismatch` for its own `pip install` line and can never
    #: earn `repo-backlink` for linking to its own source. Carried as data
    #: rather than read off `source`, because a probed root's source must
    #: stay `probe:` — see `_path_identity`.
    registry: str = ""
    #: Set by `verify` when the page could not be fetched for a reason that
    #: says nothing about it -- the site rate limiting, down, or the request
    #: not completing. Such a candidate was not examined, and `unexamined_above`
    #: keeps a weaker one from being accepted in its place.
    unreachable: bool = False

    def as_dict(self) -> dict:
        return {
            "url": self.url, "source": self.source,
            "confidence": round(self.confidence, 2), "evidence": self.evidence,
            "verified": self.verified, "reason": self.reason,
            "signals": list(self.signals), "authority": self.authority,
            "release": self.release,
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
    #: The registry's current release of this name, if a registry answered.
    #: Not a claim about the documentation — the harvest decides what to do
    #: with it, and says so.
    release: str = ""
    #: A refusal that reflects a site that could not be read rather than a
    #: name nobody documents: not remembered, because it will resolve once
    #: the site answers again.
    unexamined: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ecosystem": self.ecosystem,
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "note": self.note, "resolved_via": self.resolved_via,
            "release": self.release,
        }


def _same_page(url: str) -> str:
    """The key two URLs share when they are the same page.

    Scheme and host are case-insensitive by the URL spec; a path is not, and
    lowercasing one would merge `/API` with `/api` on the hosts where those
    differ. A trailing slash never does.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    return (f"{parts.scheme.lower()}://{host}{port}"
            f"{parts.path.rstrip('/')}"
            + (f"?{parts.query}" if parts.query else ""))


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """One entry per page, keeping the first and merging what the rest knew.

    Every lap contributes candidates and several of them reach the same page
    by different routes — a registry homepage and a domain guess, or two names
    that redirect onto one site. Concatenating without merging cost twice:
    `find_docs("pydantic")` printed each candidate twice with identical
    confidence and evidence, and `verify` fetched each copy separately.

    First wins, because callers concatenate in order of preference. What the
    duplicate knew is not thrown away: a verdict beats "not checked yet",
    signals are unioned, and confidence takes the higher of the two — so
    merging can only ever sharpen the entry that survives, never blunt it.
    """
    merged: dict[str, Candidate] = {}
    for cand in candidates:
        key = _same_page(cand.url)
        held = merged.get(key)
        if held is None:
            merged[key] = cand
            continue
        if held.verified is None and cand.verified is not None:
            held.verified = cand.verified
            held.reason = cand.reason or held.reason
            held.evidence = cand.evidence or held.evidence
            held.authority = max(held.authority, cand.authority)
        for signal in cand.signals:
            if signal not in held.signals:
                held.signals.append(signal)
        held.confidence = max(held.confidence, cand.confidence)
        held.release = held.release or cand.release
    return list(merged.values())


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


#: Hosting namespaces where the leading label is claimed first-come by whoever
#: registered an account, and therefore says nothing about who owns the *name*.
#:
#: `tensorflow.github.io` is not `github.com`, so `is_forge` did not recognise
#: it and `_owns_the_name` read the label `tensorflow` as the project's own
#: domain. That made `tensorflow.github.io/rust/tensorflow` — the Rust
#: bindings crate, whose own description reads "Rust bindings for the
#: TensorFlow machine learning library" — outrank `www.tensorflow.org`, which
#: had been fetched and verified. Two strong signals against one, and the one
#: was the project's actual documentation.
#:
#: These are still admissible: plenty of real projects publish their docs on
#: `<name>.github.io`, and refusing them outright would lose that
#: documentation. What they must not do is outrank the project's own domain,
#: which is what `name_authority` below is for.
SHARED_NAMESPACES = ("github.io", "gitlab.io", "pages.dev", "netlify.app",
                     "vercel.app", "readthedocs.io", "surge.sh",
                     "codeberg.page", "sourceforge.io", "js.org",
                     "herokuapp.com", "web.app", "firebaseapp.com")

#: Registries that host somebody else's package under a path. The path names
#: the project and the host names nobody, exactly as on a forge — so a name in
#: the path here is not evidence of identity. `_owns_the_name` already refuses
#: them a hostname claim; `path-identity` has to refuse them a path claim.
PACKAGE_HOSTS = ("docs.rs", "pkg.go.dev", "npmjs.com", "pypi.org",
                 "crates.io", "rubygems.org", "packagist.org", "nuget.org",
                 "hex.pm", "metacpan.org", "godocs.io")

#: TLDs a global project actually publishes on. Everything else that is two
#: letters is a country code, and a country-code host carrying a global
#: project's name is a local community: `pytorch.kr` is the Korean PyTorch
#: user group, and it beat `pytorch.org` because it happens to carry a `pip
#: install` line and pytorch.org's own page did not.
#:
#: Curated rather than "not two letters", for the same reason the locale list
#: is curated: `.io`, `.dev`, `.ai`, `.sh` and `.co` are country codes on
#: paper and generic in practice, and demoting them would lose real projects.
GENERIC_TLDS = {"com", "org", "net", "io", "dev", "ai", "app", "sh", "co",
                "rs", "build", "website", "tools", "cloud", "tech", "software",
                "run", "page", "xyz", "info", "team", "systems", "codes"}

#: Suffixes a project appends to its own name when the bare name is taken.
#: `lang` was already accepted here — golang.org, ziglang.org, nim-lang.org —
#: on the reasoning that a language whose name is a common word takes the
#: suffix for exactly that reason. `project` is the same convention:
#: `djangoproject.com` is Django's own domain, and refusing it the claim sent
#: a request for Django 5.2 to `www.djangoproject.com`'s weblog instead of
#: `docs.djangoproject.com`. Fixed and short on purpose — `mojoportal.org` is
#: still refused, because `portal` is not on this list and never will be.
NAME_SUFFIXES = ("lang", "project")


def _tld(host: str) -> str:
    return host.rsplit(".", 1)[-1] if "." in host else ""


def in_shared_namespace(url: str) -> str:
    """The namespace this URL is hosted under, or "" if it is a real domain."""
    host = _host(url)
    for space in SHARED_NAMESPACES:
        if host == space or host.endswith("." + space):
            return space
    return ""


def is_package_host(url: str) -> bool:
    host = _host(url)
    return any(host == p or host.endswith("." + p) for p in PACKAGE_HOSTS)


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


def _is_docs_host(url: str) -> bool:
    """Is this a host dedicated to documentation, like `docs.astro.build`?

    Such a host is exempt from the content floor. Astro's docs root renders
    entirely client-side and serves an empty shell, so measuring its text
    rejects it — and rejecting it hands the harvest to `astro.build`, whose
    sitemap is mostly blog posts. Pointing `/docs` at `docs.<project>` is an
    explicit statement about where the documentation lives, and it outranks
    what the index page happens to render without JavaScript.
    """
    host = _host(url)
    return host.startswith(("docs.", "developer.", "devdocs.")) or ".readthedocs." in host


#: Hosts and paths that publish a *generated symbol reference* rather than
#: documentation to learn from. Both are real documentation and neither is
#: refused — a signature is exactly what you want when you need a signature —
#: but asked for "the documentation" they are the wrong half.
_REFERENCE_HOSTS = ("reference.", "api.", "apidocs.", "apiref.",
                    "javadoc.", "rustdoc.", "godoc.", "pkg.")
_REFERENCE_PATHS = ("/reference", "/api-reference", "/apiref", "/javadoc",
                    "/godoc", "/api/reference")


def is_reference_site(url: str) -> bool:
    """Is this a generated API reference rather than prose documentation?

    Recognised by name, which is free and needs no fetch. `langchain` resolved
    to `reference.langchain.com` and harvested 560 pages averaging 490
    characters — one attribute per page:

        # action > **Attribute** in `langchain`
        ## Signature
        action: Literal['accept']

    Measuring instead of naming was tried and does not separate them at the
    point the decision is made: sampled at their roots, that reference manifest
    runs to a median of 1,774 characters against the docs site's 5,023 — close
    enough that any threshold would be a coin toss. The thinness only shows up
    two levels down, long after a URL has been chosen. The subdomain says it
    outright and says it for free.

    `docs.` beats `reference.` on its own via `docs-host`, so this matters for
    the case that signal cannot reach: a project whose reference lives on a
    subdomain and whose guides live on the bare domain.
    """
    host = _host(url)
    if host.startswith(_REFERENCE_HOSTS):
        return True
    path = (urlparse(url).path or "").lower().rstrip("/")
    return path.startswith(_REFERENCE_PATHS)


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
    release = str(((data.get("dist-tags") or {}).get("latest")) or "")
    home = (data.get("homepage") or "").strip()
    if home.startswith("http"):
        out.append(Candidate(home, "npm:homepage", _score(home, "homepage"),
                             f"npm registry homepage for {name}", release=release))
    repo = data.get("repository")
    repo_url = _clean_repo(repo.get("url") if isinstance(repo, dict) else repo or "")
    if repo_url:
        out.append(Candidate(repo_url, "npm:repository", _score(repo_url, "repository"),
                             f"npm registry repository for {name}", release=release))
    return out


def _pypi(name: str, fetcher: Fetcher) -> list[Candidate]:
    data = _json(fetcher, f"https://pypi.org/pypi/{name}/json")
    if not data:
        return []
    info = data.get("info") or {}
    out = []
    release = str(info.get("version") or "")

    # project_urls is where modern packages actually declare their docs.
    for label, url in (info.get("project_urls") or {}).items():
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        low = label.lower()
        if "doc" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "documentation"),
                                 f"PyPI project_urls[{label}] for {name}", release=release))
        elif "home" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "homepage"),
                                 f"PyPI project_urls[{label}] for {name}", release=release))
        elif "source" in low or "repo" in low:
            out.append(Candidate(url, f"pypi:{label}", _score(url, "repository"),
                                 f"PyPI project_urls[{label}] for {name}", release=release))

    home = (info.get("home_page") or "").strip()
    if home.startswith("http"):
        out.append(Candidate(home, "pypi:home_page", _score(home, "homepage"),
                             f"PyPI home_page for {name}", release=release))
    return out


def _crates(name: str, fetcher: Fetcher) -> list[Candidate]:
    data = _json(fetcher, f"https://crates.io/api/v1/crates/{name}")
    crate = (data or {}).get("crate") or {}
    out = []
    release = str(crate.get("max_stable_version") or crate.get("max_version") or "")
    for key, kind in (("documentation", "documentation"),
                      ("homepage", "homepage"),
                      ("repository", "repository")):
        url = (crate.get(key) or "").strip()
        if url.startswith("http"):
            out.append(Candidate(url, f"crates:{key}", _score(url, kind),
                                 f"crates.io {key} for {name}", release=release))
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


def _facts_for(candidate: Candidate, found: list[Candidate], pooled: dict) -> dict:
    """What this candidate is judged against: its own registry's claims.

    A candidate that came from a registry is tested against that registry —
    its ecosystem, the repository and homepage it declared — and nothing
    another registry said about a different project with the same word. One
    that came from none (the project's own domain, a shape guess) is tested
    against the pool, as before.

    Named rather than inlined because it decides which URL comes back, and
    the tripwire in `test_rules_stamp` can only watch what it can name.
    """
    if not candidate.registry:
        return pooled
    return _facts_from([c for c in found if c.registry == candidate.registry],
                       candidate.registry)


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
            for cand in got:
                cand.registry = eco
            found += got
            if ecosystem:
                break
    return found, hit


def release_from(found: list[Candidate], ecosystem: str) -> str:
    """The current release according to one registry, or "" when it is not
    among `found` — never another registry's for the same word."""
    if ecosystem not in REGISTRIES:
        return ""
    return next((c.release for c in found
                 if c.release and c.registry == ecosystem), "")


# ─────────────────────────────────────────────────────────────
# Probing
# ─────────────────────────────────────────────────────────────
#: Below this many links a manifest is too small to judge: a handful of
#: entries that happen to be dated is not evidence of a newsroom.
_JUDGE_MANIFEST_ABOVE = 5

#: Above this share of article links, a manifest is a newsroom rather than a
#: documentation root. Measured against the manifests actually in use rather
#: than picked:
#:
#:      article  docsy  links  manifest
#:         0.52   0.00     60  pytorch.org          <- the newsroom
#:         0.14   0.00     14  prisma.io
#:         0.08   0.20     71  pydantic.dev
#:         0.03   0.00    176  docs.langchain.com
#:         0.01   0.94    101  mojolang.org
#:         0.00   0.57      7  svelte.dev
#:
#: Note the second column, which is why the obvious test is the wrong one:
#: asking whether a manifest links to `/docs/` paths rejects Prisma and
#: LangChain, both of which are already *on* a documentation host and name
#: their pages `/orm/overview`. What the newsroom has and the others do not is
#: articles. 0.3 sits in the middle of a 3.7x gap.
_ARTICLE_SHARE = 0.3


def _indexes_only_articles(body: str, url: str) -> bool:
    """Is this `llms.txt` a list of posts rather than of documentation?"""
    from docsforge.core import llmsfinder                                   # local: avoids a cycle
    from docsforge.core import engine

    links = [target for _title, target in llmsfinder.parse_llms_links(body, url)]
    if len(links) < _JUDGE_MANIFEST_ABOVE:
        return False
    articles = sum(1 for link in links if engine.looks_like_article(link))
    return articles / len(links) >= _ARTICLE_SHARE


#: Subdomains a project puts its documentation on, best first. Kept to two:
#: each costs a request on every candidate host, and between them they cover
#: what the convention actually is. `developer.` is the one large vendors use
#: where `docs.` would have meant the company's own internal handbook.
DOCS_SUBDOMAINS = ("docs", "developer")


def _docs_subdomain(origin: str, fetcher: Fetcher) -> list[Candidate]:
    """The project's documentation subdomain, if it publishes on one.

    Asked before the apex's own paths, and only for a corpus it actually
    serves: a `docs.` host that 404s, or that redirects straight back to the
    apex, produces nothing and costs one request. What it must not do is
    *lower* the bar — a subdomain is admitted on exactly the evidence an apex
    path is, `_indexes_only_articles` included, because "the documentation
    lives here" is a claim about location and not about quality.
    """
    host = _host(origin)
    if not host or host.count(".") < 1:
        return []
    scheme = urlparse(origin).scheme or "https"

    out: list[Candidate] = []
    for label in DOCS_SUBDOMAINS:
        if host.startswith(f"{label}."):
            continue
        target = f"{scheme}://{label}.{host}/llms.txt"
        try:
            r = fetcher.get(target, timeout=PROBE_TIMEOUT, allow_redirects=True)
        except ForgeError:
            continue
        if r.status_code != 200:
            continue
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" in ctype:
            continue
        body = getattr(r, "text", "") or ""
        if _indexes_only_articles(body, r.url):
            continue
        # Above the apex's own 0.95, and deliberately: both are the site
        # describing itself for machines, and when a project publishes two the
        # subdomain is the one scoped to the thing that was asked for.
        out.append(Candidate(r.url, f"probe:{label}.llms.txt", 0.96,
                             f"{target} exists and is not HTML — "
                             f"{label}.{host} is where this project says its "
                             f"documentation lives"))
        break
    return out


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

    # The documentation subdomain first, because on a company that ships more
    # than one product the apex is the company and only the subdomain is the
    # library. Measured 2026-09-16: `pydantic.dev/llms.txt` answers 200, this
    # loop took it at 0.95 and broke, and `learn_technology("pydantic")`
    # stored 24 pages of Pydantic Logfire — pricing, customer evidence, a
    # competition winner — under the name of the validation library, at 0.97
    # confidence with nothing in the result suggesting anything was wrong.
    # `docs.pydantic.dev` carries the library: 2,003,603 characters against
    # the apex dump's 684,669, and a different corpus rather than a longer one.
    #
    # One extra request, and only where it can change the answer: a host that
    # already *is* the documentation host is not asked for a second one.
    out: list[Candidate] = []
    if not _is_docs_host(origin):
        out += _docs_subdomain(origin, fetcher)
    if out and out[-1].confidence >= 0.95:
        return out

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
            # An HTTP 200 is not a documentation root: a stub that redirects
            # with a meta tag or a line of script arrives as a perfectly good
            # empty page, and accepting one costs the correct answer.
            hop = _follow_client_redirect(r, target, fetcher)
            if hop is not None:
                r = hop
            landed = getattr(r, "url", "") or target
            if (len(_visible_text(getattr(r, "text", ""))) < MIN_PROBE_TEXT
                    and not _is_docs_host(landed)):
                continue
        if path.endswith(".txt"):
            if "html" in ctype:
                continue
            # A published llms.txt is the site describing itself for machines;
            # nothing beats it -- provided it is describing its documentation.
            # `pytorch.org/llms.txt` is 8 KB of conference announcements and
            # blog posts under a `## Posts` heading, and taking it as the docs
            # root both stopped `/docs/` ever being probed (this loop breaks on
            # a 0.95) and pointed a harvest at the newsroom. Same material as
            # Django's weblog, arriving by a different rung.
            if _indexes_only_articles(getattr(r, "text", "") or "", r.url):
                continue
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

#: A language whose bare name is a common word publishes on a `lang` domain
#: for exactly that reason: ziglang.org, nim-lang.org, golang.org,
#: rust-lang.org, julialang.org, kotlinlang.org, crystal-lang.org,
#: elixir-lang.org, scala-lang.org, dlang.org, vlang.io, mojolang.org.
#: `_owns_the_name` accepts these hosts, but nothing ever tried them, so the
#: highest-signal source we have never saw the site: `zig` resolved to an npm
#: templating library and `nim` to an unrelated repository -- both *verified*,
#: each on a registry package that agreed with itself. Nearly all of them sit
#: on .org, so this costs four probes rather than the full NAME_TLDS spread.
LANG_TLDS = ("org", "io")

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


def _probe_origins(origins: list[tuple[str, str]], slug: str, fetcher: Fetcher,
                   explore: int = DOMAINS_EXPLORED,
                   state=None) -> list[Candidate]:
    """Try a list of `(label, url)` guesses and return what survives.

    Split out of `from_domains` so the name-shape lap can reuse it verbatim.
    That reuse is the point rather than a convenience: a shape hit must meet
    exactly the standard a domain hit meets — same software gate, same text
    floor, same docs-root probing, same scoring — or L3 becomes a second, softer
    way in, and the ladder's whole claim is that more laps never mean a lower
    bar.
    """
    live: list[tuple[int, int, str, str, str, str]] = []
    for label, origin in origins:
        try:
            r = fetcher.get(origin, timeout=DOMAIN_TIMEOUT, allow_redirects=True)
        except ForgeError:
            continue
        if r.status_code != 200 or "html" not in (
                r.headers.get("content-type") or "").lower():
            continue
        html = getattr(r, "text", "")
        if state is not None:
            # Before the floors, not after. "Every candidate, passed or failed,
            # deposits what its fetch revealed" is the whole point of the
            # hinge — and a page that fails the software gate is exactly the
            # kind that still carries a link to where the real docs are.
            state.record(getattr(r, "url", "") or origin, html)
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
        live.append((_domain_score(origin, landed, html, slug), text, label,
                     landed, why, origin))

    # A project can own several of these and put different things on them:
    # `kubernetes.dev` is the contributor portal and `kubernetes.io` the
    # documentation, while `terraform.com` is a different company entirely.
    # Rank on deliberate evidence first and volume of text only as a tiebreak.
    live.sort(reverse=True)

    # Several of them can also be the *same* site. `pydantic.io` 301s to
    # `pydantic.dev`, so two origins landed on one page and each was explored
    # whole: `find_docs("pydantic")` probed `/llms.txt` twice, verified both
    # copies, and printed every candidate twice with identical confidence and
    # evidence. Collapse on where the request actually *landed*, not on the
    # name that was guessed, and keep the names that redirect into it as what
    # they are — further evidence of ownership. `explore` now bounds distinct
    # destinations rather than guesses, so the same budget reaches further.
    #
    # Which of the collapsed rows is kept cannot be left to the sort. Two rows
    # for one page score identically and hold identical text, so the tiebreak
    # fell through to the *label*, compared descending: `pydantic.io` beat
    # `pydantic.dev` on the letter `i`, and the candidate came back sourced
    # `domain:io` and claiming that pydantic.dev redirects onto itself. The
    # origin that did not redirect is the canonical one, and it wins.
    def _canonical(row) -> bool:
        return _host(row[5]) == _host(row[3])

    by_landing: dict[str, tuple] = {}
    aliases: dict[str, list[str]] = {}
    for row in live:
        key = f"{_host(row[3])}{urlparse(row[3]).path.rstrip('/')}"
        held = by_landing.get(key)
        if held is None:
            by_landing[key] = row
            aliases[key] = []
            continue
        if _canonical(row) and not _canonical(held):
            by_landing[key] = row            # the redirect target, not the alias
            aliases[key].append(held[2])
        else:
            aliases[key].append(row[2])

    out: list[Candidate] = []
    for key, (_score, _text, label, landed, why, _origin) in list(by_landing.items())[:explore]:
        # The homepage is usually marketing with the docs one click away, so
        # the docs root under it outranks it.
        for found in probe_docs_root(landed, fetcher):
            found.source = f"domain:{label}/{found.source}"
            found.confidence = min(0.97, found.confidence + 0.02)
            out.append(found)
        # Say truthfully where we ended up. A redirect onto a code host does
        # not make that host the project's; the repository under it may well
        # be the right one, but that is a claim about the path, not the name.
        claim = (f"{_host(landed)} is the project's own domain and carries {why}"
                 if not is_forge(landed) else
                 f"{slug}.{label} redirects onto {_host(landed)}, a code host "
                 f"owned by no one project; the page there carries {why}")
        also = aliases.get(key) or []
        if also:
            claim += (f"; {', '.join(f'{slug}.{a}' for a in also)} "
                      f"redirect{'s' if len(also) == 1 else ''} onto it")
        out.append(Candidate(landed, f"domain:{label}", 0.75, claim))
    return out


def from_domains(name: str, fetcher: Fetcher, state=None) -> list[Candidate]:
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
    origins = [(tld, f"https://{slug}.{tld}") for tld in NAME_TLDS]
    if not slug.endswith("lang"):        # `golang` would give golanglang.org
        origins += [(f"lang.{tld}", f"https://{slug}{sep}lang.{tld}")
                    for sep in ("", "-") for tld in LANG_TLDS]
    return _probe_origins(origins, slug, fetcher, state=state)


# ─────────────────────────────────────────────────────────────
# L3 — name shapes
# ─────────────────────────────────────────────────────────────
#: Concatenation gets one extra TLD because it is the highest-yield shape and
#: several large projects sit on a country-code domain (huggingface.co).
SHAPE_TLDS = NAME_TLDS + ("co",)

#: Tokens that are grammar rather than name. "ruby on rails" is published at
#: rubyonrails.org so they are kept when concatenating, but a preposition is
#: never a vendor or a product on its own.
_GLUE = {"on", "of", "for", "the", "and", "in"}

#: Subdomains vendors put developer documentation behind.
DEV_HOSTS = ("docs", "developer", "developers")


def name_shapes(name: str) -> list[tuple[str, str]]:
    """Origins to try for a multi-word name, most likely first.

    A multi-word name carries structure that one slug throws away: the first
    token is usually the vendor and the rest the product, and vendors publish
    on a small set of predictable shapes. `from_domains` already tries the
    hyphenated slug — which is why `react hook form` resolves today and
    `apache airflow` does not. Nobody registered apache-airflow.org; they put
    it at airflow.apache.org.

    Returns `(label, url)` pairs; the label ends up in `resolved_via`.
    """
    slug = normalise(name)
    tokens = [t for t in slug.split("-") if t]
    if len(tokens) < 2:
        return []          # single-word names are from_domains' job, already done

    concat = "".join(tokens)
    first, last = tokens[0], tokens[-1]
    rest = "".join(tokens[:-1])           # everything before the final token
    tail_hyphen = "-".join(tokens[1:])    # everything after the first

    shapes: list[tuple[str, str]] = []

    # 1. Everything run together — opentelemetry.io, rubyonrails.org,
    #    godotengine.org, unrealengine.com. The highest-yield shape by far.
    shapes += [(f"concat.{tld}", f"https://{concat}.{tld}") for tld in SHAPE_TLDS]

    # 2. Product as a subdomain of vendor — airflow.apache.org, ui.shadcn.com,
    #    code.visualstudio.com.
    if rest and last not in _GLUE:
        shapes += [(f"product.vendor.{tld}", f"https://{last}.{rest}.{tld}")
                   for tld in NAME_TLDS]

    # 3. Product as a path under the vendor — tanstack.com/query.
    if tail_hyphen and first not in _GLUE:
        shapes += [(f"vendor.{tld}/product", f"https://{first}.{tld}/{tail_hyphen}")
                   for tld in NAME_TLDS]

    # 4. A vendor's developer portal — docs.github.com/actions,
    #    developer.hashicorp.com/terraform.
    if tail_hyphen and first not in _GLUE:
        for host in DEV_HOSTS:
            shapes += [(f"{host}.vendor.{tld}/product",
                        f"https://{host}.{first}.{tld}/{tail_hyphen}")
                       for tld in ("io", "com", "org")]
        # Some vendors repeat the vendor in the path rather than dropping it:
        # docs.spring.io/spring-boot, not docs.spring.io/boot.
        shapes += [(f"docs.vendor.{tld}/name",
                    f"https://docs.{first}.{tld}/{slug}") for tld in ("io", "com")]

    return shapes


def from_name_shapes(name: str, fetcher: Fetcher, state=None,
                     budget=None) -> list[Candidate]:
    """L3. Multi-word names, through exactly the gate everything else uses."""
    shapes = name_shapes(name)
    if not shapes:
        return []
    if budget is not None:
        # Never spend the whole allowance on guesses: the laps after this are
        # cheaper per candidate and likelier to be right.
        shapes = shapes[:max(0, budget.requests_left - 8)]
        budget.charge(len(shapes))
    if not shapes:
        return []
    return _probe_origins(shapes, normalise(name), fetcher, state=state)


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
    if any(label == slug or label.replace("-", "") == flat for label in labels):
        return True

    # A language whose bare name is a common word takes a `lang` suffix on its
    # domain for exactly that reason: golang.org, rust-lang.org, julialang.org,
    # nim-lang.org, crystal-lang.org, mojolang.org. Refusing the suffix denied
    # every one of them a claim on its own name — Mojo's real docs sat
    # unverified at 0.92 while an npm package called `mojo` was resolved
    # instead. The suffix is fixed, so `mojoportal.org` is still refused.
    #
    # `project` joined it for the same reason and on the same evidence:
    # `djangoproject.com` is Django's own domain, and refusing it the claim
    # meant `docs.djangoproject.com` earned neither `own-domain` nor
    # `docs-host`, so a request for Django 5.2 resolved to
    # `www.djangoproject.com` and harvested 214 weblog posts.
    if any(label.replace("-", "") == flat + suffix
           for label in labels for suffix in NAME_SUFFIXES):
        return True

    # A multi-word name may be spread across labels rather than run into one:
    # `apache airflow` lives at airflow.apache.org and `visual studio code` at
    # code.visualstudio.com. Requiring EVERY token to appear in the hostname is
    # a stricter test than the single-label match above, not a looser one — it
    # is what makes L3's shapes reachable without softening the bar for
    # anything that already resolves.
    tokens = [t for t in slug.split("-") if t]
    if len(tokens) < 2:
        return False
    host_labels = [l for l in labels if l not in ("www", "com", "org", "io",
                                                  "dev", "net", "co")]
    joined = "".join(host_labels)
    return all(token in joined for token in tokens)


def _install_line(body: str, slug: str) -> str:
    """The ecosystem an install command on this page names, or ""."""
    for eco, prefix in _INSTALL:
        # The package may be scoped or path-qualified; anchor on the bare name.
        if re.search(prefix + r"[\"'@\w./\-]*\b" + re.escape(slug) + r"\b",
                     body, re.I):
            return eco
    return ""


_REPO_PATH = re.compile(
    r"https?://(?:www\.)?(?:github|gitlab|bitbucket)\.com/([\w.\-]+)/([\w.\-]+)", re.I)


def _repo_identity(body: str, slug: str) -> str:
    """A source repository on this page whose own path names this project.

    The strongest identity claim a page can make, and the one nothing read
    before. `tanstack.com/query` links to `github.com/TanStack/query`: owner
    and repository together account for every token in the name, which is a
    statement by the code owner and not a coincidence of hostnames.

    Deliberately segment-exact. Substring matching would let an unrelated
    project called Flask Lists claim `flask` through `github.com/flaskio/web`,
    which is the measured failure this is meant to close rather than widen —
    9 of 20 names resolved to the wrong project, and every one of them owned a
    hostname while none of them owned a matching repository.
    """
    tokens = {t for t in slug.split("-") if t}
    if not tokens:
        return ""

    def words(text: str) -> set[str]:
        return {s for s in re.split(r"[-_.]+", text.lower()) if s}

    for owner, repo in _REPO_PATH.findall(body or ""):
        repo = re.sub(r"\.git$", "", repo)
        repo_words, owner_words = words(repo), words(owner)

        # Containment has to hold BOTH ways, and the outward direction is the
        # one that matters. Requiring only that the repository covers the name
        # lets any longer repository claim it: npm search for "aws lambda"
        # turns up `awslabs/aws-lambda-invoke-store`, whose name contains both
        # tokens and is not AWS Lambda's documentation. Measured — it is
        # exactly how the search lap produced three confident wrong answers the
        # moment it was switched on.
        if not repo_words <= tokens:
            continue
        if tokens <= (repo_words | owner_words | {owner.lower(), repo.lower()}):
            return f"{owner}/{repo}"
    return ""


#: Registry fields that are a nomination rather than a mention: the registry
#: was asked where this package documents itself and answered with this URL.
_NOMINATING = ("pypi:", "npm:", "crates:")


def _path_identity(candidate: "Candidate", slug: str) -> str:
    """A registry-nominated URL whose path is scoped to this project.

    The gate asks whether the *host* carries the name, and for a sub-project
    that host belongs to the parent. LangGraph documents itself at
    `docs.langchain.com/oss/python/langgraph/overview`; the host carries
    `langchain`, so the real documentation earned neither `own-domain` nor
    `docs-host` and was refused with "only registry-agreement", while the
    source tree passed on `repo-identity` and the harvest stored a README.

    Two independent things have to hold, which is what keeps this a strong
    signal rather than a looser one:

      * a **registry nominated this exact URL** — PyPI was asked where
        langgraph documents itself and said this — and
      * the asked-for name is a **whole path segment** of it.

    Neither alone is worth anything. A registry nomination on its own is
    already `registry-agreement`; a name in a path on its own is what makes
    `github.com/sintaxi/terraform` look like Terraform. Together they are two
    sources agreeing, which is the standard `is_identified` has always held.

    Refused on package hosts. `docs.rs/htmx` and `pkg.go.dev/x` also carry the
    name in the path, and there the host is a registry serving somebody else's
    package — the same reason `_owns_the_name` refuses them a hostname claim.
    """
    if not slug or is_forge(candidate.url) or is_package_host(candidate.url):
        return ""
    if not candidate.source.startswith(_NOMINATING):
        return ""
    flat = slug.replace("-", "")
    segments = [s.lower() for s in urlparse(candidate.url).path.split("/") if s]
    for segment in segments:
        bare = segment.removesuffix(".html").removesuffix(".htm")
        if bare == slug or bare.replace("-", "") == flat:
            return segment
    return ""


def _scope_identity(candidate: "Candidate", name: str) -> str:
    """An npm scope that the registry-nominated host carries as its own.

    `@tanstack/react-query` documents itself at `tanstack.com/query`. The
    gate normalises the name to `react-query`, which no hostname, repository
    or path carries — the pages say "TanStack Query" — so the real
    documentation stood on `registry-agreement` alone and was refused
    (`Issues.md` R7, every run, hosted and offline).

    What the gate was missing is that a scope is not a bare name. npm binds a
    scope to one account or organisation; nobody can publish under
    `@tanstack/` but TanStack. So when npm, asked about this scoped package,
    nominates this exact URL, *and* the host carries the scope as a whole
    label, the publisher's registry entry points at the publisher's own
    domain. Two facts, from the registry and from DNS, agreeing.

    Deliberately narrow, so it cannot become the loosening R7 warned about:

      * the name must be scoped — a bare `click` gets nothing from this;
      * the URL must be an npm nomination for that scoped name, so
        `tanstack.com/router` earns nothing for `@tanstack/react-query`;
      * forges and package hosts are refused, as `_owns_the_name` refuses
        them: `github.com/tanstack` is an account on somebody else's host.

    And it never lets a page about `click` identify `@anyone/click`: the
    signal is about the scope's domain, not the package's bare tail.
    """
    text = (name or "").strip().lower()
    if not (text.startswith("@") and "/" in text):
        return ""
    scope = text[1:].split("/", 1)[0]
    if not scope or not candidate.source.startswith("npm:"):
        return ""
    if is_forge(candidate.url) or is_package_host(candidate.url):
        return ""
    flat = scope.replace("-", "")
    for label in _host(candidate.url).split("."):
        if label == scope or label.replace("-", "") == flat:
            return scope
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
    # Not onto a forge, though. There the path names the project and the
    # host names nobody, which is why `_owns_the_name` refuses forges
    # outright — and arriving by redirect does not change who owns the host.
    # `mojo.dev` redirects to an unrelated repository, and crediting that
    # arrival made `github.com/gdejohn/procrastination` a *verified* answer
    # for Mojo, off two structural signals and not one mention of the name.
    owns = _owns_the_name(candidate.url, slug)
    arrived_on_its_own = facts.get("via_domain") and not is_forge(candidate.url)
    if arrived_on_its_own or owns:
        found.append("own-domain")

    # `docs.astro.build` is the project's own documentation host, and that is
    # a statement about identity that survives the page rendering nothing
    # without JavaScript. Deliberately conditional on owning the name:
    # `docs.rs/htmx` is also a "docs." host, and it is a Rust crate registry
    # hosting somebody else's package, not htmx's documentation.
    if owns and _is_docs_host(candidate.url):
        found.append("docs-host")

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

    if _repo_identity(body, slug):
        found.append("repo-identity")

    if _path_identity(candidate, slug):
        found.append("path-identity")

    if _scope_identity(candidate, name):
        found.append("scope-domain")

    hay = normalise(text)
    hits = hay.count(slug) if slug else 0
    flat = slug.replace("-", "")
    if slug and flat != slug:
        # A multi-word project almost always writes its own name run together —
        # "OpenTelemetry", "RubyOnRails" — which normalises to the flat form and
        # never matches the hyphenated slug. Counting only the slug therefore
        # missed every mention on the project's own front page, which is exactly
        # where the name is stated most often. Taking the better of the two
        # counts corrects an undercount; it does not lower the bar, because
        # MIN_MENTIONS and the STRONG requirement are both untouched.
        hits = max(hits, hay.count(flat))
    if hits >= MIN_MENTIONS:
        found.append(f"names-it:{hits}")
    return found


#: Signals that identify a project rather than merely describe one. Mention
#: counts are deliberately excluded: they are corroboration, never proof.
STRONG = ("own-domain", "docs-host", "install:", "repo-backlink",
          "registry-agreement", "repo-identity", "path-identity",
          "scope-domain")


#: The strong signals that come from owning the name — the host carries it,
#: or is the docs host of a domain that does. Everything else in STRONG says
#: something about the *project*: how it installs, where its source lives,
#: what a registry nominated.
OWNERSHIP = ("own-domain", "docs-host")


def ownership_only(signals: list[str]) -> bool:
    """Identified on owning the name and saying it, and nothing else.

    The one path a name-squatter satisfies (`Issues.md` R10): `flask` reached
    a to-do app at flask.io and `polars` a third-party site, each on
    `own-domain` plus mentions. Not refused for it — plenty of genuine sites
    publish nothing but prose — but not final either: see `_resolve_uncached`.
    """
    strong = [s for s in signals if s.startswith(STRONG)]
    return bool(strong) and all(s.startswith(OWNERSHIP) for s in strong)


def _settle_held(held: "Candidate", candidates: list["Candidate"]) -> "Candidate":
    """Decide a domain answer that was held for standing on ownership alone.

    It loses only to a verified page that shows something about the *project*
    — an install line, its source repository, a registry's nomination — and
    is not the source tree itself. Ranking the two by `evidence` could not
    do this: `evidence` asks who owns the name before it asks anything else,
    so `polars.dev`, a third-party guide that owns the word, kept beating
    `docs.pola.rs`, which carries Polars' repository and does not own
    "polars" as a label. Measured offline, 2026-09-22.

    Where no such page exists the held answer stands, exactly as it would
    have before it was held: `pydantic.dev/docs/validation/latest/` and
    `docs.pydantic.dev` both stand on ownership alone, and swapping one
    official page for the other on the `docs-host` bit is not what holding
    is for.
    """
    # The held page is in `candidates` too, re-verified against the
    # registry's facts; if that gave it evidence of its own, it competes.
    better = [c for c in candidates
              if c.verified and not is_forge(c.url) and not ownership_only(c.signals)]
    return max(better, key=evidence) if better else held


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


#: How strong a claim a host makes on the name. Not a count of anything —
#: a statement about what kind of address this is.
APEX_DOMAIN = 3        # the project's own domain, or one it redirected onto
LOCAL_DOMAIN = 2       # the same name on a country-code TLD: a local mirror
SHARED_LABEL = 1       # the name as a first-come label on a hosting namespace
NO_CLAIM = 0


def name_authority(candidate: "Candidate") -> int:
    """What kind of claim this host makes on the name.

    `own-domain` is one bit, and three very different things were setting it:
    `tensorflow.org` (the project), `tensorflow.github.io` (whoever registered
    that GitHub organisation) and `pytorch.kr` (the Korean user group). One
    bit cannot rank them, so ranking fell through to counting strong signals —
    and there a rustdoc page with `repo-identity` outscored `www.tensorflow.org`,
    and a community mirror carrying a `pip install` line outscored
    `pytorch.org`. Both were fetched, both verified, both lost by one unit of
    arithmetic to a page that is not the project's documentation.

    So the claim is graded rather than counted:

      * **apex** — the name is a whole label of a real domain on a TLD the
        world publishes on, or a host that the name's own domain deliberately
        redirected onto (`terraform.io` → `developer.hashicorp.com`, which is
        somebody consolidating their documentation and is the strongest thing
        a redirect can mean).
      * **local** — the same, on a country code. A global project's canonical
        site is not a ccTLD; a ccTLD carrying its name is a national community.
        Admissible, and never above the project's own domain.
      * **shared** — the name as a label under `github.io` and friends, where
        labels are claimed first-come. Real projects publish there and are not
        refused; they simply do not outrank a project's own domain.

    Graded, not gated: every one of these still passes `is_identified` on
    exactly the evidence it did before. This decides which of two verified
    answers is the better one, which is a question that only arises after
    both have already been believed.

    Read off the candidate alone, deliberately. Taking the name as a second
    argument meant only `verify` could compute this, and `evidence` then read
    a field that anything constructing a candidate by hand would leave at
    zero — ranking that silently degrades when a caller does not know to
    populate it. `own-domain` already means "the host carries the name, or the
    name's own domain sent us here", which is precisely the input needed.
    """
    if is_forge(candidate.url) or is_package_host(candidate.url):
        return NO_CLAIM

    # Nothing claimed the name here at all.
    if "own-domain" not in candidate.signals:
        return NO_CLAIM

    if in_shared_namespace(candidate.url):
        return SHARED_LABEL

    return (APEX_DOMAIN if _tld(_host(candidate.url)) in GENERIC_TLDS
            else LOCAL_DOMAIN)


def evidence(candidate: Candidate) -> tuple:
    """How good an answer this verified candidate is. Higher is better.

    Ranked on what the signals *mean*, not on how many there are. Counting
    them was tried first and was worse than what it replaced: a GitHub
    repository page is dense with the project's name and carries a backlink
    and a registry agreement, so `langchain` ranked
    `github.com/langchain-ai/langchainjs/tree/main/libs/langchain/` above
    `docs.langchain.com`. Two strong signals and eighteen mentions, and it is
    a source tree.

    So the order asks four questions before it counts anything:

      1. **Is this source or documentation?** A forge hosts the code. It can
         still win when it is all there is -- many small libraries really do
         document themselves in a README -- but never over a real docs site.
      2. **What claim does the host make on the name?** See `name_authority`.
         This used to be the single bit `own-domain`, and collapsing a
         project's own domain, a first-come `github.io` label and a national
         mirror into one value is what let the count below decide between
         them.
      3. **Is it the project's own documentation host?** That is exactly what
         `docs-host` means: owns the name *and* is a docs host.
      4. **Is it prose, or a generated symbol reference?**

    Only then strong-signal count, mentions as corroboration, and the
    source-type prior last -- purely to break a tie, since `confidence` is a
    guess about the *kind* of source made before anything was read.
    """
    signals = set(candidate.signals)
    strong = sum(1 for s in candidate.signals if s.startswith(STRONG))
    named = 0
    for signal in candidate.signals:
        if signal.startswith("names-it:"):
            named = int(signal.split(":", 1)[1])
    return (
        0 if is_forge(candidate.url) else 1,
        name_authority(candidate),
        1 if "docs-host" in signals else 0,
        # A generated symbol reference is documentation, and it is the wrong
        # half of it for someone who asked how to use the thing. Below
        # `docs-host`, which already separates `docs.` from `reference.`, so
        # this only decides what that signal cannot reach: a project whose
        # reference sits on a subdomain and whose guides sit on the bare name.
        0 if is_reference_site(candidate.url) else 1,
        strong,
        named,
        candidate.confidence,
    )


def _transient(e: ForgeError) -> bool:
    """Did the fetch fail for a reason that says nothing about the page?

    A 429 is the site asking for a pause, a 5xx is the site's own trouble,
    and a request that never completed is the network's. A 404 is not: the
    registry named a page that is not there, and that is a finding.
    """
    status = getattr(e, "status", None)
    if status is not None:
        return status in (408, 425, 429) or status >= 500
    return "Request failed for" in str(e)


def unexamined_above(picked: Candidate | None,
                     candidates: list[Candidate]) -> Candidate | None:
    """The strongest registry-nominated candidate that could not be examined
    and outranks `picked` -- the reason `picked` must not win.

    A weaker candidate is allowed to win only when every stronger one was
    read and found wanting. One that could not be read because the site was
    rate limiting or down says nothing about itself, and accepting a
    same-named project from another registry or a code host in its place is
    a guess dressed as a resolution. Measured 2026-09-20, with
    click.palletsprojects.com answering HTTP 429: `click` resolved to
    github.com/databricks/click/wiki -- a Kubernetes CLI -- and was harvested
    and stored under `click`; `requests` resolved to docs.rs/requests, a
    Rust crate. Both passed verification, because both do document a
    project called that. Neither is the project that was asked for.

    Only candidates a registry nominated (or the docs root probed beneath
    one, which inherits its registry) can block: a guessed domain that is
    down is a guess that is down. And a candidate from the *same* registry
    as `picked` does not block it -- a homepage that verified is the same
    project as the documentation URL beside it that did not answer -- unless
    `picked` is the project's code host: the same project, but a README in
    place of the documentation that could not be read is not the answer to
    "where does it document itself", and it would be stored as though it were.
    """
    blockers = [
        c for c in candidates
        if c.unreachable and c.registry in REGISTRIES
        and (picked is None
             or (c.confidence >= picked.confidence
                 and (c.registry != picked.registry or is_forge(picked.url))))
    ]
    return max(blockers, key=lambda c: c.confidence) if blockers else None


def best_verified(candidates: list[Candidate]) -> Candidate | None:
    """The best-evidenced verified candidate, or None if none passed.

    The loop used to stop at the *first* candidate that verified, walking in
    confidence order — and confidence there is a prior about the **source
    type**, not evidence about the page. `pypi:Documentation` outranks
    `pypi:Homepage` before either has been read, so `langchain` resolved to
    `reference.langchain.com` (an API symbol index, one attribute per page,
    median 490 characters) while `docs.langchain.com` sat second at 0.78 and
    was never even checked. The winner was not better, it was earlier.

    Reading them all costs more requests, which the ladder's own budget still
    bounds. It buys the ability to compare, and a resolution that cannot
    compare cannot be said to have chosen.
    """
    passed = [c for c in candidates if c.verified]
    return max(passed, key=evidence) if passed else None


def verify(candidate: Candidate, name: str, fetcher: Fetcher,
           facts: dict | None = None, state=None) -> Candidate:
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
        candidate.unreachable = _transient(e)
        return candidate

    if state is not None:
        # The hinge, and the reason it is here rather than at the call sites:
        # this function is the only place that holds a candidate's page, and
        # it used to read it once and throw it away. A candidate that fails is
        # exactly the one whose page is worth keeping — it frequently links
        # straight to the documentation that would have passed.
        state.record(candidate.url, body)

    slug = normalise(name)
    if not slug:
        candidate.verified = False
        candidate.reason = "no usable name to check against"
        return candidate

    candidate.signals = identity_signals(candidate, name, body, facts)
    candidate.authority = name_authority(candidate)
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
# ─────────────────────────────────────────────────────────────
# L4 — evidence
# ─────────────────────────────────────────────────────────────
#: Keyless, rate-limited, and the only forge with a stable public field saying
#: where a project's documentation lives. GitLab's API exposes no equivalent
#: `homepage`, so a repository there contributes its backlink but not this.
_GITHUB_REPO = re.compile(r"https?://(?:www\.)?github\.com/([\w.\-]+)/([\w.\-]+)", re.I)

#: How much of the accumulated evidence to spend requests on. Each of these is
#: a fetch, and the lap after this one is cheaper per candidate.
MAX_EVIDENCE_REPOS = 3
MAX_EVIDENCE_LINKS = 4


def from_evidence(name: str, state, fetcher: Fetcher, budget=None) -> list[Candidate]:
    """L4. Read back what the candidates that already failed gave away.

    This is the lap that makes the ladder a loop rather than a list. `verify()`
    fetches a candidate, fails it, and would otherwise discard the page —
    including the repository backlink whose `homepage` field is the code owner
    stating, in public, where the documentation lives. `ResolveState` has been
    recording that all along; nothing read it back until here.

    Three kinds of evidence, in descending order of how much they claim:

    1. A repository's declared homepage. The strongest: it is the project
       saying where its own documentation is.
    2. An outbound link that leaves the candidate's host and looks like
       documentation — often a project pointing at its own docs SaaS.
    3. A canonical URL that points somewhere else, which is a site saying "the
       real version of this page lives there".
    """
    if state is None:
        return []
    out: list[Candidate] = []
    slug = normalise(name)

    for repo in list(state.repos_seen)[:MAX_EVIDENCE_REPOS]:
        if budget is not None and budget.exhausted:
            break
        found = _GITHUB_REPO.match(repo)
        if not found:
            continue
        owner, project = found.group(1), re.sub(r"\.git$", "", found.group(2))
        # Only ask about repositories whose own name relates to what was asked.
        # Every docs page links to something on GitHub; most of it is unrelated.
        if slug and slug.replace("-", "") not in f"{owner}{project}".lower().replace("-", ""):
            continue
        if budget is not None:
            budget.charge()
        data = _json(fetcher, f"https://api.github.com/repos/{owner}/{project}")
        home = ((data or {}).get("homepage") or "").strip()
        if home.startswith(("http://", "https://")):
            out.append(Candidate(
                home, "evidence:repo-homepage", 0.88,
                f"github.com/{owner}/{project} declares its homepage as {home}"))

    for href in list(state.outbound)[:MAX_EVIDENCE_LINKS]:
        out.append(Candidate(href, "evidence:outbound", 0.6,
                             "linked as documentation from a candidate page"))

    for source, canonical in list(state.canonical.items())[:MAX_EVIDENCE_LINKS]:
        if _host(canonical) and _host(canonical) != _host(source):
            out.append(Candidate(
                canonical, "evidence:canonical", 0.7,
                f"{_host(source)} names {canonical} as the canonical location"))

    seen, unique = set(), []
    for cand in out:
        key = cand.url.rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(cand)
    return unique


# ─────────────────────────────────────────────────────────────
# L0 — memory
# ─────────────────────────────────────────────────────────────
#: A resolution is a fact about where a project publishes, and projects move
#: rarely. A refusal is a fact about what we could not find today, which is a
#: much weaker claim with a much shorter life — a site can add the evidence
#: tomorrow, so refusals are retried an order of magnitude sooner.
CACHE_TTL = 30 * 86400
REJECT_TTL = 7 * 86400

#: Which set of identity rules decided an entry. Bump it whenever they
#: change. An answer reached under superseded rules is evidence about the
#: code that wrote it, not about the name, so `recall` discards it and the
#: next resolution earns its answer again.
#:
#: Written because a fix does not reach a cache. The run that resolved `mojo`
#: to a Java library filed that as a success, and `CACHE_TTL` would have
#: served it for thirty days -- outliving the fix by four weeks, on the one
#: name whose failure prompted the fix. Entries written before this stamp
#: existed carry no `rules` key and are discarded on sight, which is the
#: intended effect: they were all decided under rules that have since moved.
#:
#: History, because the bump is the part that gets forgotten:
#:
#:   1  the stamp itself, and the forge/`lang`-domain ownership rules
#:   2  `best_verified` — read every candidate and compare, ranked by what the
#:      signals mean. Missing this bump is why the fix did not reach anyone:
#:      a cache holding `langchain -> reference.langchain.com` at `rules: 1`
#:      still matched, so `recall` served the old answer in one second and the
#:      new ranking never ran. The mechanism worked; nobody turned the handle.
#:      `test_rules_is_bumped_when_the_decision_logic_changes` now fails when
#:      the deciding functions change without this number moving.
#:   3  `is_reference_site` — a generated symbol reference ranks below prose
#:      documentation. Bumped because the tripwire from 2 demanded it, which
#:      is the whole of what that test is for.
#:   4  `name_authority` — the claim a host makes on the name is graded rather
#:      than counted, so a first-come `github.io` label and a country-code
#:      mirror stop outranking the project's own domain; `path-identity`, so a
#:      sub-project documented on its parent's domain can be identified at
#:      all; and `project` as a name suffix, so `djangoproject.com` is
#:      Django's. Every wrong answer of 2026-09-10 was cached under rules 3
#:      with a 30-day TTL, and this is what stops them being served until
#:      October.
#:   7  `scope-domain` — a scoped npm name is identified on its scope's own
#:      domain when npm nominated the URL (R7); and a domain answer that
#:      stands on owning the name alone no longer pre-empts the registries
#:      (R10). Entries cached under 6 include refusals of every scoped
#:      package and squatters resolved without a registry being asked.
RULES = 7


def _cache_file() -> Path:
    return Path(os.environ.get("DOCSFORGE_RESOLVE_CACHE")
                or Path.home() / ".docsforge" / "resolutions.json")


def _load_cache() -> dict:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        # A corrupt or unreadable cache must never be the reason a resolution
        # fails; it is an optimisation, not a source of truth.
        return {}


def _save_cache(data: dict) -> None:
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def recall(name: str) -> Resolution | None:
    """A remembered resolution for this name, or None. Costs no requests."""
    entry = _load_cache().get(normalise(name))
    if not entry:
        return None
    if entry.get("rules") != RULES:
        return None     # decided under rules this build no longer applies
    age = time.time() - entry.get("at", 0)
    if age > (CACHE_TTL if entry.get("url") else REJECT_TTL):
        return None

    result = Resolution(name=name, ecosystem=entry.get("ecosystem", ""))
    result.resolved_via = entry.get("resolved_via", "") or "memory"
    if not entry.get("url"):
        result.note = entry.get("note") or f"Recently could not resolve {name!r}."
        return result
    cand = Candidate(entry["url"], "memory", 0.95, entry.get("evidence", ""),
                     True, entry.get("reason", "remembered from an earlier run"),
                     release=entry.get("release", "") or "")
    cand.signals = list(entry.get("signals") or [])
    result.candidates = [cand]
    result.best = cand
    result.note = entry.get("note", "")
    # Remembered with the rest, since 2026-09-17. It was not, so the second
    # `learn_technology` for any name — the one served from memory — filed
    # its harvest under the date where the first had filed it under the
    # release. A cache that changes the answer is not a cache.
    result.release = entry.get("release", "") or ""
    return result


#: What `verify()` writes when the request itself failed, as opposed to
#: succeeding and finding the page documents something else.
_UNREACHABLE = "could not be read"


def learned_nothing(result: Resolution) -> bool:
    """True when a refusal reflects this machine rather than the name.

    Candidates that could not be *fetched* say nothing about where a project
    documents itself — they say the network could not answer. Filing that
    for `REJECT_TTL` turns a passing outage into a week of confident wrong
    answers, which is exactly what happened to `mojo`: on a NAT64 network
    every candidate was refused as a "private address", six were found, none
    could be read, and the refusal was cached for seven days while the cause
    was fixed within one.

    A refusal reached by actually reading the candidates is a real finding
    and is still remembered.
    """
    if result.best is not None or not result.candidates:
        return False
    if result.unexamined:
        return True
    return all(_UNREACHABLE in (c.reason or "") for c in result.candidates)


def remember(name: str, result: Resolution) -> None:
    """File what this resolution found, successful or not."""
    if learned_nothing(result):
        return          # nothing was learned, so there is nothing to file
    data = _load_cache()
    best = result.best
    data[normalise(name)] = {
        "at": time.time(),
        "rules": RULES,
        "url": best.url if best else "",
        "evidence": best.evidence if best else "",
        "reason": best.reason if best else "",
        "signals": list(best.signals) if best else [],
        "ecosystem": result.ecosystem,
        "resolved_via": result.resolved_via,
        "release": result.release,
        "note": result.note,
    }
    _save_cache(data)


def forget_resolution(name: str = "") -> str:
    """Drop one remembered resolution, or all of them.

    Ships with the cache rather than after it. A cache the user cannot clear is
    a trap: the one moment you need it gone is when it is confidently wrong.
    """
    data = _load_cache()
    if not name:
        _save_cache({})
        return f"Forgot {len(data)} remembered resolution(s)."
    slug = normalise(name)
    if data.pop(slug, None) is None:
        return f"Nothing remembered for {name!r}."
    _save_cache(data)
    return f"Forgot the remembered resolution for {name!r}."


# ─────────────────────────────────────────────────────────────
# L5 — fuzzy search
# ─────────────────────────────────────────────────────────────
def from_search(name: str, fetcher: Fetcher, budget=None) -> list[Candidate]:
    """Registry fuzzy search, then an optional configured hook.

    Never a search engine's HTML endpoint: brittle, against their terms, and a
    poor look for a resolver whose entire pitch is trustworthiness. These are
    documented JSON APIs that the exact-name laps simply do not use.
    """
    out: list[Candidate] = []
    query = quote(name.strip())
    if not query:
        return out
    if budget is not None and budget.exhausted:
        return out

    npm = _json(fetcher, f"https://registry.npmjs.org/-/v1/search?text={query}&size=4")
    for obj in (npm or {}).get("objects", [])[:4]:
        pkg = obj.get("package") or {}
        links = pkg.get("links") or {}
        for field_name in ("homepage", "repository"):
            url = links.get(field_name) or pkg.get(field_name)
            if url:
                out.append(Candidate(url, f"search:npm/{pkg.get('name','')}",
                                     _score(url, field_name) * 0.8,
                                     f"npm search matched {pkg.get('name','')!r}"))

    crates = _json(fetcher, f"https://crates.io/api/v1/crates?q={query}&per_page=4")
    for crate in (crates or {}).get("crates", [])[:4]:
        for field_name in ("documentation", "homepage", "repository"):
            url = crate.get(field_name)
            if url:
                out.append(Candidate(url, f"search:crates/{crate.get('name','')}",
                                     _score(url, field_name) * 0.8,
                                     f"crates search matched {crate.get('name','')!r}"))

    hook = os.environ.get("DOCSFORGE_SEARCH", "").strip()
    if hook:
        try:
            payload = _json(fetcher, hook.replace("{query}", query))
        except ForgeError:
            payload = None
        items = payload if isinstance(payload, list) else (payload or {}).get("results", [])
        for item in (items or [])[:4]:
            url = item if isinstance(item, str) else (item or {}).get("url", "")
            if url:
                out.append(Candidate(url, "search:hook", 0.6,
                                     "returned by DOCSFORGE_SEARCH"))
    return out


def _ladder_tail(result: Resolution, name: str, fetcher: Fetcher, state, budget,
                 verify_best: bool, limit: int) -> Resolution:
    """The laps that run only once domains and registries have both failed.

    Kept separate because there are two ways to arrive here — no registry knew
    the name at all, or every candidate failed verification — and both deserve
    the rest of the ladder rather than one of them being a dead end. That
    asymmetry is what made F6 look like a resolution problem when it was
    partly a control-flow one.
    """
    if not verify_best:
        return result

    # ── L3: the shape of the name ──
    if budget is None or not budget.exhausted:
        shaped = from_name_shapes(name, fetcher, state=state, budget=budget)
        for cand in shaped:
            # No `via_domain` here. Whether a shape host really owns the name
            # is precisely the question, so `_owns_the_name` answers it rather
            # than the lap asserting it on the way in.
            verify(cand, name, fetcher, {}, state=state)
            if cand.verified:
                result.candidates = dedupe(shaped + result.candidates)[:limit]
                result.best = cand
                result.resolved_via = f"shape:{cand.source}"
                result.note = (
                    f"Resolved {name!r} from the shape of its name: vendors "
                    f"publish on a small set of predictable hosts, and no "
                    f"registry lists this one."
                )
                return result
        if shaped:
            result.candidates = dedupe(result.candidates + shaped)[:limit]

    # ── L4: evidence the failed candidates already gave away ──
    if budget is None or not budget.exhausted:
        evidence = from_evidence(name, state, fetcher, budget=budget)
        # A declared homepage is usually marketing with the docs one click
        # away, so the docs root beneath it outranks it — the same treatment a
        # registry homepage gets.
        probed: list[Candidate] = []
        for cand in evidence:
            if cand.source == "evidence:repo-homepage" and not _looks_like_docs(cand.url):
                for found in probe_docs_root(cand.url, fetcher):
                    found.source = f"evidence:repo-homepage/{found.source}"
                    found.confidence = min(0.95, found.confidence + 0.02)
                    probed.append(found)
        evidence = probed + evidence

        for cand in evidence:
            verify(cand, name, fetcher, {}, state=state)
            if state is not None:
                state.record(cand.url)
            if cand.verified:
                result.candidates = dedupe(evidence + result.candidates)[:limit]
                result.best = cand
                result.resolved_via = f"evidence:{cand.source}"
                result.note = (
                    f"Resolved {name!r} from evidence left by candidates that "
                    f"themselves failed — the page that could not be verified "
                    f"still said where to look next."
                )
                return result
        if evidence:
            result.candidates = dedupe(result.candidates + evidence)[:limit]

    # ── L5: fuzzy search, the last and least certain lap ──
    if budget is None or not budget.exhausted:
        searched = from_search(name, fetcher, budget=budget)
        for cand in searched:
            if budget is not None:
                budget.charge()
            verify(cand, name, fetcher, {}, state=state)
            if state is not None:
                state.record(cand.url)
            if cand.verified:
                result.candidates = dedupe(searched + result.candidates)[:limit]
                result.best = cand
                result.resolved_via = "search"
                result.note = (
                    f"Resolved {name!r} through registry search rather than an "
                    f"exact name match. The identity gate is the same one every "
                    f"other lap uses."
                )
                return result
        if searched:
            result.candidates = dedupe(result.candidates + searched)[:limit]

    if result.best is None and budget is not None and budget.exhausted:
        result.note = f"{result.note} Resolution {budget.why()}.".strip()
    return result


def resolve(name: str, ecosystem: str = "", fetcher: Fetcher | None = None,
            verify_best: bool = True, limit: int = 6,
            use_memory: bool = True) -> Resolution:
    """Find where `name` documents itself, consulting memory first.

    L0 is a wrapper rather than a lap inside the ladder so that a hit costs
    exactly zero HTTP requests — a cache that still opens a connection to check
    itself is not a cache. `forget_resolution()` clears it.
    """
    if use_memory and verify_best:
        remembered = recall(name)
        if remembered is not None:
            return remembered

    result = _resolve_uncached(name, ecosystem, fetcher, verify_best, limit)

    if use_memory and verify_best:
        remember(name, result)
    return result


def _resolve_uncached(name: str, ecosystem: str = "", fetcher: Fetcher | None = None,
                      verify_best: bool = True, limit: int = 6) -> Resolution:
    """Find where `name` documents itself.

    Returns every candidate with its evidence rather than silently picking one,
    so a caller that disagrees can see why and choose differently.
    """
    result = Resolution(name=name, ecosystem=ecosystem or guess_ecosystem(name))
    own = fetcher is None
    fetcher = fetcher or Fetcher(Options(delay=0.0))
    # The hinge: every candidate, passed or failed, deposits what its fetch
    # revealed where a later lap can read it. And a bound, so a pathological
    # name refuses within a stated budget rather than wandering.
    state = ResolveState(name=name)
    budget = Budget()

    try:
        # 1. The project's own domain. Checked first because the data says so:
        #    it produced every correct answer and none of the wrong ones.
        # Deduped before anything reads it, not after: `verify` fetches every
        # candidate it is handed, so a page left in the list twice is a second
        # request as well as a second line of output.
        domain = dedupe(from_domains(name, fetcher, state=state))
        if verify_best:
            # Read them all, then compare. Stopping at the first to pass made
            # the ordering the decision -- see `best_verified`.
            for cand in domain[:limit]:
                verify(cand, name, fetcher, {"via_domain": True}, state=state)
            picked = best_verified(domain[:limit])
            if picked is not None and not ownership_only(picked.signals):
                result.candidates = domain[:limit]
                result.best = picked
                result.resolved_via = "domain"
                result.note = (
                    f"Resolved from {name!r}'s own domain. Registries were not "
                    f"consulted: owning the name is the stronger claim, and "
                    f"where the two disagree the registry is usually a "
                    f"different project that shares the word.")
                return result
            # Owning the name and saying it is all this page showed — no
            # install line, no source repository, nothing a registry said.
            # That is the one path a squatter satisfies (`Issues.md` R10), so
            # it is held rather than returned: if no registry knows the name
            # it stands, and if one does, it competes with what the registry
            # nominated instead of pre-empting it.
            provisional = picked
        else:
            provisional = None

        # 2. Registries, as the fallback.
        found, hit = from_registries(name, result.ecosystem, fetcher)
        if hit:
            result.ecosystem = result.ecosystem or hit
            # Provisional, like the ecosystem beside it, and scoped to it. It
            # used to be the release of the first candidate in query order
            # with any release at all — npm's, always, for any name that also
            # exists on npm — and it was never revisited once verification
            # chose a winner. So `click` resolved to click.palletsprojects.com
            # via PyPI and was stored as version 0.1.0: the `dist-tags.latest`
            # of an unrelated npm package that shares the word. Measured live,
            # 2026-09-16, twice, with `markdownify` the second. The ecosystem
            # got the correction below the day this bug was found for it;
            # the release never did.
            result.release = release_from(found, result.ecosystem)
        if not found and provisional is not None:
            # Nothing contradicts the domain: it stands, as it always did.
            result.candidates = domain[:limit]
            result.best = provisional
            result.resolved_via = "domain"
            result.note = (
                f"Resolved from {name!r}'s own domain, on owning the name "
                f"alone: no registry knows {name!r}, so nothing could confirm "
                f"or contradict it.")
            return result
        if not found:
            # No registry knows it. That used to end the search, which is what
            # made every multi-word name unreachable — no registry knows
            # "apache airflow" either, and it is very much a real technology.
            # Fall through to the shape lap instead.
            result.candidates = domain[:limit]
            result.note = (
                f"No registry knows {name!r}. If it is private or internal, "
                f"pass the documentation URL directly to harvest_docs."
            )
            return _ladder_tail(result, name, fetcher, state, budget,
                                verify_best, limit)
        found += domain

        # A homepage is worth one round of convention-guessing before use.
        extra: list[Candidate] = []
        for cand in list(found):
            if cand.confidence < 0.9 and not _looks_like_docs(cand.url):
                for probed in probe_docs_root(cand.url, fetcher):
                    # The docs root under PyPI's homepage is PyPI's project,
                    # and it carries that registry's release. Carried as data
                    # rather than by re-tagging `source`: a `pypi:` prefix
                    # means "a registry nominated this exact URL" to
                    # `_path_identity`, and a guessed path is not that.
                    probed.release = probed.release or cand.release
                    probed.registry = probed.registry or cand.registry
                    extra.append(probed)

        ranked = dedupe(sorted(found + extra,
                               key=lambda c: c.confidence, reverse=True))
        result.candidates = ranked[:limit]

        # What the registries claimed — per registry, not pooled. Pooled and
        # first-wins, the facts were always the first registry to answer:
        # `click` on PyPI was judged against npm's ecosystem and npm's
        # repository, so its own `pip install` line counted *against* it as
        # `install-mismatch` — a veto, below two strong signals — and a link
        # to its own source could never be `repo-backlink`. A candidate is
        # judged against the registry it came from; one that came from none
        # is judged against the pool, as before.
        facts = _facts_from(found, result.ecosystem)
        if verify_best:
            for cand in result.candidates:
                verify(cand, name, fetcher,
                       dict(_facts_for(cand, found, facts),
                            via_domain=cand.source.startswith("domain:")),
                       state=state)
            picked = best_verified(result.candidates)
            if provisional is not None:
                picked = _settle_held(provisional, result.candidates)
            blocker = unexamined_above(picked, result.candidates)
            if blocker is not None:
                # Not the ladder tail either: its laps -- name shapes,
                # evidence, search -- can only produce weaker candidates
                # than the one that could not be read.
                why = blocker.reason.removeprefix(_UNREACHABLE + ": ")
                result.best = None
                result.unexamined = True
                result.note = (
                    f"{blocker.url} could not be read ({why}) and outranks every "
                    f"candidate that could. Nothing weaker was accepted in its "
                    f"place: a same-named project elsewhere is not this one. "
                    f"Try again later, or pass the documentation URL directly to "
                    f"harvest_docs."
                )
                return result
            if picked is not None:
                result.best = picked
                result.resolved_via = ("domain" if picked.source.startswith("domain:")
                                       else "registry")
                # The ecosystem is whichever registry actually produced the
                # answer, not whichever one happened to reply first: the
                # same name often exists in several, on different projects.
                won = picked.registry
                if won in REGISTRIES:
                    result.ecosystem = won
                # And the release is the winner's own — the same principle,
                # applied to the field that was left out of it. A winner that
                # carries none (the project's own domain, verified here on
                # registry agreement) takes its ecosystem's, which is the
                # most that can honestly be said about it.
                result.release = picked.release or release_from(found, result.ecosystem)
            if result.best is None:
                result.note = (
                    f"Found {len(result.candidates)} candidate(s) for {name!r} but none "
                    f"could be confirmed to document it. Harvesting an unverified page "
                    f"risks storing the wrong project — pass a URL directly if you know it."
                )
                return _ladder_tail(result, name, fetcher, state, budget,
                                    verify_best, limit)
        elif result.candidates:
            result.best = result.candidates[0]
            result.release = result.best.release or result.release
        return result
    finally:
        if own:
            fetcher.close()
