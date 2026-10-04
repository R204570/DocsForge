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
from urllib.parse import quote, urlencode, urljoin, urlparse

from docsforge.core import languages
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
    #: What the registry entry this candidate was found in claims -- its
    #: homepage and repository -- for a candidate that did not come from the
    #: exact-name lap, which carries its facts another way (`_facts_for`).
    facts: dict = field(default_factory=dict)

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
    #: The registry the *caller* named, as distinct from `ecosystem`, which is
    #: also filled in with whichever registry answered first. Only a caller's
    #: choice narrows the search lap.
    asked_ecosystem: str = ""
    #: The programming language the caller asked for ("javascript"), and what
    #: resolution found about it -- see `resolve(language=)`.
    language: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ecosystem": self.ecosystem,
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "note": self.note, "resolved_via": self.resolved_via,
            "release": self.release, "language": self.language,
        }


def _same_page(url: str) -> str:
    """The key two URLs share when they are the same page.

    Scheme and host are case-insensitive by the URL spec; a path is not, and
    lowercasing one would merge `/API` with `/api` on the hosts where those
    differ. A trailing slash never does.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:                      # a registry's malformed address
        port = ""
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


def _declared(url: str) -> str:
    """A homepage as a registry or repository declared it, made fetchable.

    Scheme-less (`docs.rs/clap`, as GitHub stores it) gets one; `http://`
    becomes `https://`. `www.gradio.app` does not answer on port 80 at all:
    declared as `http://`, the site timed out, a timeout is "could not be
    read", and `gradio` stayed its repository (held-out round 2, 2026-09-24).
    Nothing that publishes documentation today serves it over plain HTTP only.
    """
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    if not text.startswith("https://"):
        return "https://" + text if "." in text.split("/", 1)[0] else ""
    return text


def _clean_repo(url: str) -> str:
    """A git remote as a browsable https URL."""
    text = (url or "").strip()
    text = re.sub(r"^git\+", "", text)
    text = re.sub(r"^git://", "https://", text)
    text = re.sub(r"^ssh://git@", "https://", text)
    text = re.sub(r"^git@([^:]+):", r"https://\1/", text)
    # `ssh://git@github.com:owner/repo` is scp syntax behind an ssh scheme:
    # the colon is a path separator, not a port. Left in, it crashed every
    # resolution that met it (`scikit-learn` on npm, held-out round 2).
    text = re.sub(r"^(https?://[^/:]+):(?!\d+(?:/|$))", r"\1/", text)
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
                     "vercel.app", "readthedocs.io", "hexdocs.pm", "surge.sh",
                     "codeberg.page", "sourceforge.io", "js.org",
                     "herokuapp.com", "web.app", "firebaseapp.com")

#: Registries that host somebody else's package under a path. The path names
#: the project and the host names nobody, exactly as on a forge — so a name in
#: the path here is not evidence of identity. `_owns_the_name` already refuses
#: them a hostname claim; `path-identity` has to refuse them a path claim.
PACKAGE_HOSTS = ("docs.rs", "pkg.go.dev", "npmjs.com", "pypi.org",
                 "crates.io", "rubygems.org", "packagist.org", "nuget.org",
                 "hex.pm", "hexdocs.pm", "metacpan.org", "godocs.io")

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
NAME_SUFFIXES = ("lang", "project", "js")


def _tld(host: str) -> str:
    return host.rsplit(".", 1)[-1] if "." in host else ""


def in_shared_namespace(url: str) -> str:
    """The namespace this URL is hosted under, or "" if it is a real domain."""
    host = _host(url)
    for space in SHARED_NAMESPACES:
        if host == space or host.endswith("." + space):
            return space
    return ""


#: Registries' own pages about a package: a listing, never its documentation.
INDEX_LISTINGS = ("pypi.org", "npmjs.com", "crates.io", "rubygems.org",
                  "packagist.org", "nuget.org", "libraries.io", "pkgs.org")


def _is_index_listing(url: str) -> bool:
    host = _host(url)
    return any(host == p or host.endswith("." + p) for p in INDEX_LISTINGS)


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
    r"""["']?[^"'>]*?url\s*=\s*([^"'\s>]+)""", re.I)
#: Quoted or not: `docs.serde.rs` writes `<meta http-equiv=refresh
#: content=0;url=serde/index.html>`, and a quotes-only pattern took the stub
#: for a page (final round-2 run).

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
                cand.url = _declared(cand.url) or cand.url
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
            landed = getattr(r, "url", "") or target
            first = lambda u: ([p for p in urlparse(u).path.lower().split("/") if p] or [""])[0]
            if first(landed) != first(target) and not _docs_path(landed):
                # `gohugo.io/docs/` redirects to `/about/`: the site keeps its
                # manual at the root, a section per directory, and taking the
                # page it landed on drew the harvest around one of them --
                # five pages of Hugo (held-out round 1, every run).
                out.append(Candidate(f"{origin}/", f"probe:{path}", 0.7,
                                     f"{target} redirects to {landed}, outside itself: "
                                     f"the documentation is the site"))
            else:
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
#:
#: Early in what a reader sees, that is, not in the bytes. A modern docs page
#: spends its first tens of kilobytes on inline scripts and styles:
#: `docs.langchain.com/oss/javascript/langgraph/overview` is 880 KB, its body
#: starts at byte 16,138, and the first 40,000 bytes held one mention of
#: "langgraph" -- so LangGraph's own documentation failed LangGraph's identity
#: check with "nothing on the page identifies it" (2026-09-24). The window is
#: raw bytes wide enough for such a page, and mentions are counted in its
#: visible text only (`identity_signals`), so a larger window buys no credit
#: from a script that happens to repeat a word.
VERIFY_WINDOW = 600_000
#: ...of which links are read only this far (`identity_signals`).
LINK_WINDOW = 40_000

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
    #
    # `js` on the same footing: a JavaScript project's own domain is very
    # often its name run into the word -- vuejs.org, nodejs.org,
    # expressjs.com, threejs.org, docs.solidjs.com -- and `solid-js` is
    # normalised to `solid`, so `docs.solidjs.com/llms.txt` earned neither
    # `own-domain` nor `docs-host` and failed on mentions alone (held-out
    # test, 2026-09-24).
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
    # What a reader sees: scripts and styles hold the page's words too -- in
    # route tables and JSON -- and counting them credited a page with
    # mentions no reader would find.
    text = _visible_text(body)
    # Links are judged over the opening of the page only, as they always
    # were. Every company site links its GitHub organisation from the footer,
    # and reading the whole page gave `pydantic.dev/` -- the company's front
    # page -- a `repo-identity` it never had, enough to outrank the
    # documentation (2026-09-24). The wider window is for mentions and
    # install lines, which live in the prose.
    head = body[:LINK_WINDOW]
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
        if path and path in head.lower() and candidate.url.rstrip("/") != repo:
            found.append("repo-backlink")

    home = facts.get("homepage") or ""
    if home and _host(home) and _host(home) == _host(candidate.url):
        found.append("registry-agreement")

    if _repo_identity(head, slug):
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


def _settle_held(held: "Candidate", candidates: list["Candidate"],
                 clients: frozenset = frozenset()) -> "Candidate":
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
              if c.verified and not is_forge(c.url) and not ownership_only(c.signals)
              and id(c) not in clients]
    return _docs_behind(max(better, key=evidence) if better else held, candidates)


def _docs_behind(picked: "Candidate", candidates: list["Candidate"]) -> "Candidate":
    """The site's own documentation host, when the answer is its front page.

    The front page settles *whose* site it is -- `streamlit.io` carries the
    install line and the repository -- and then that evidence belongs to the
    site, `docs.streamlit.io` included, which on its own stands on ownership
    and so could not win above. Held-out test, 2026-09-24: `streamlit` and
    `docker` both resolved to the marketing page with the verified docs host
    beside it. An answer that is already a documentation page stays.
    """
    if (picked is None or is_forge(picked.url) or "docs-host" in picked.signals
            or _looks_like_docs(picked.url) or in_shared_namespace(picked.url)):
        return picked
    site = _registrable(_host(picked.url))
    docs = [c for c in candidates
            if c.verified and "docs-host" in c.signals and not is_forge(c.url)
            and _registrable(_host(c.url)) == site]
    return max(docs, key=evidence) if docs else picked


#: Words a repository adds to a name when it is one language's binding for
#: the thing rather than the thing: `docker-py`, `redis-py`, `rust-docker`,
#: `go-redis`, `stripe-python`, `node-redis`. Not `js`: `three.js` and
#: `vue.js` are the projects themselves, by the same convention that names
#: their domains.
BINDING_MARKERS = frozenset({
    "py", "python", "rs", "rust", "go", "golang", "rb", "ruby", "php", "java",
    "net", "dotnet", "csharp", "sharp", "node", "client", "sdk", "driver",
    "bindings", "binding", "api"})


def _is_a_client(facts: dict, name: str) -> bool:
    """Is this registry entry's own repository a binding for `name`?

    `normalise` folds `docker-py` into `docker` on purpose -- the dressing is
    how a lookup spells the same library -- so the test is on the words the
    repository adds: all of them binding markers, none of them asked for.
    """
    found = _GITHUB_REPO.match((facts or {}).get("repository") or "")
    if not found:
        return False
    words = lambda text: {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}
    repo = words(re.sub(r"\.git$", "", found.group(2)))
    asked = words(name.split("/")[-1])
    extra = repo - asked
    return bool(extra) and extra <= BINDING_MARKERS and bool(repo - extra) \
        and (repo - extra) <= asked


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
    # A name that does not resolve is a finding too: the registry declared a
    # domain nobody holds any more. Taken for a network blip, an unrelated
    # crate's dead homepage (`www.fedfans.com`, for `koa`) was held to
    # outrank `koajs.com`, and `koa` and `diesel` both came back refused
    # (held-out round 2, 2026-09-24).
    if _DNS_MISS.search(str(e)):
        return False
    return "Request failed for" in str(e)


_DNS_MISS = re.compile(r"NameResolutionError|getaddrinfo failed|Name or service not known|"
                       r"nodename nor servname|No address associated with hostname|"
                       r"Failed to resolve", re.I)


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
    if _is_index_listing(candidate.url):
        # `uv` resolved to `pypi.org/project/uv/`: a registry's page about a
        # package names it constantly and documents nothing (held-out test,
        # 2026-09-24). docs.rs and pkg.go.dev *are* documentation, and stay.
        candidate.verified = False
        candidate.reason = "a package index's page about the package, not its documentation"
        return candidate
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
        home = _declared((data or {}).get("homepage") or "")
        if home:
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
#:   8  the search lap keeps to the registry the caller named (`langgraph` on
#:      npm had resolved to a Rust crate); a repository that wins is swapped
#:      for the documentation site it declares, when that passes the gate;
#:      and a resolution may be for one language's edition (`language=`),
#:      filed under its own key.
#:   9  what a name usually means, and what is not the thing: the most-starred
#:      repository named exactly that is consulted and its site can overrule
#:      a same-named package (`redis`, `helm`, `rails`, `hugo`); a registry
#:      package that is a language binding (`docker-py`) cannot unseat the
#:      project's own site; a package index's page is never documentation;
#:      a front page gives way to its own `docs.` host; `js` is a name suffix
#:      (`docs.solidjs.com`); crates and Go modules fall back to docs.rs and
#:      pkg.go.dev; and language editions are read from manifests, hub pages
#:      and sibling hosts, and refused where the site answers any path.
#:      Measured on 53 held-out names, 2026-09-24. Entries cached under 8
#:      include every one of the wrong answers that measurement found. Then,
#:      from round 2 of the same measurement (57 fresh names) before any of
#:      it was released: a front page gives way to the documentation it
#:      links; a repository's README names its docs when its homepage is a
#:      demo; declared homepages are fetched over https; a domain that no
#:      longer resolves is a finding, not a pause; and a language is named
#:      inside a compound segment (`python-api`). And from the final runs of
#:      2026-09-25, still unreleased: a docs host is refused when it is an
#:      alias, a stub, a preview or a generated reference; a hub's sibling
#:      `guides.` host is tried; a docs path that redirects out of itself
#:      means the site is the manual; and GitHub's search window is waited
#:      out rather than given up on.
RULES = 9


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


def _cache_key(name: str, language: str = "") -> str:
    """One entry per name, and per language asked for: "langgraph" for Python
    and for JavaScript are two different answers."""
    return normalise(name) + (f"@{language}" if language else "")


def recall(name: str, language: str = "") -> Resolution | None:
    """A remembered resolution for this name, or None. Costs no requests."""
    entry = _load_cache().get(_cache_key(name, language))
    if not entry:
        return None
    if entry.get("rules") != RULES:
        return None     # decided under rules this build no longer applies
    age = time.time() - entry.get("at", 0)
    if age > (CACHE_TTL if entry.get("url") else REJECT_TTL):
        return None

    result = Resolution(name=name, ecosystem=entry.get("ecosystem", ""),
                        language=entry.get("language", "") or "")
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


def remember(name: str, result: Resolution, language: str = "") -> None:
    """File what this resolution found, successful or not."""
    if learned_nothing(result):
        return          # nothing was learned, so there is nothing to file
    data = _load_cache()
    best = result.best
    data[_cache_key(name, language)] = {
        "language": result.language,
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
    # The name's own entry and every per-language one beside it.
    doomed = [k for k in data if k == slug or k.startswith(slug + "@")]
    if not doomed:
        return f"Nothing remembered for {name!r}."
    for key in doomed:
        data.pop(key, None)
    _save_cache(data)
    return f"Forgot the remembered resolution for {name!r}."


# ─────────────────────────────────────────────────────────────
# L5 — fuzzy search
# ─────────────────────────────────────────────────────────────
def from_search(name: str, fetcher: Fetcher, budget=None,
                ecosystem: str = "") -> list[Candidate]:
    """Registry fuzzy search, then an optional configured hook.

    Never a search engine's HTML endpoint: brittle, against their terms, and a
    poor look for a resolver whose entire pitch is trustworthiness. These are
    documented JSON APIs that the exact-name laps simply do not use.

    Only the registry the caller named, when they named one. Asked for
    `langgraph` on npm -- where the package is `@langchain/langgraph` and the
    exact lookup finds nothing -- this lap searched crates.io too, and
    `docs.rs/rust-langgraph`, a Rust crate, passed the identity gate and was
    the answer (2026-09-24). A caller who said npm did not mean Rust.
    """
    out: list[Candidate] = []
    query = quote(name.strip())
    if not query:
        return out
    if budget is not None and budget.exhausted:
        return out

    if ecosystem in ("", "npm"):
        npm = _json(fetcher, f"https://registry.npmjs.org/-/v1/search?text={query}&size=4")
        for obj in (npm or {}).get("objects", [])[:4]:
            pkg = obj.get("package") or {}
            links = pkg.get("links") or {}
            # What this package's own entry claims, for judging what it names:
            # a search hit is otherwise judged against nothing, and the
            # `@langchain/langgraph` README failed for "langgraph" on four
            # mentions alone while npm names it as that package's homepage.
            home = (links.get("homepage") or "").strip()
            repo = _clean_repo(links.get("repository") or "")
            # Only a package that *is* the name vouches for what it names:
            # `@langchain/langgraph` for "langgraph", never `langgraph-sdk`,
            # whose homepage would otherwise earn `registry-agreement` for a
            # project nobody asked about.
            exact = normalise(pkg.get("name", "")) == normalise(name)
            claims = ({"homepage": home, "repository": repo, "ecosystem": "npm"}
                      if exact else {})
            for field_name, url in (("homepage", home), ("repository", repo)):
                if url.startswith("http"):
                    cand = Candidate(url, f"search:npm/{pkg.get('name','')}",
                                     _score(url, field_name) * 0.8,
                                     f"npm search matched {pkg.get('name','')!r}")
                    cand.registry = "npm" if ecosystem else ""
                    cand.facts = {k: v for k, v in claims.items() if v}
                    out.append(cand)

    if ecosystem in ("", "crates"):
        crates = _json(fetcher, f"https://crates.io/api/v1/crates?q={query}&per_page=4")
        for crate in (crates or {}).get("crates", [])[:4]:
            for field_name in ("documentation", "homepage", "repository"):
                url = crate.get(field_name)
                if url:
                    cand = Candidate(url, f"search:crates/{crate.get('name','')}",
                                     _score(url, field_name) * 0.8,
                                     f"crates search matched {crate.get('name','')!r}")
                    cand.registry = "crates" if ecosystem else ""
                    out.append(cand)

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
        searched = from_search(name, fetcher, budget=budget,
                               ecosystem=result.asked_ecosystem
                               if result.asked_ecosystem in REGISTRIES else "")
        for cand in searched:
            if budget is not None:
                budget.charge()
            verify(cand, name, fetcher, dict(cand.facts), state=state)
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
            use_memory: bool = True, language: str = "") -> Resolution:
    """Find where `name` documents itself, consulting memory first.

    L0 is a wrapper rather than a lap inside the ladder so that a hit costs
    exactly zero HTTP requests — a cache that still opens a connection to check
    itself is not a cache. `forget_resolution()` clears it.

    `language` asks for the documentation of one language's edition --
    "langgraph" for JavaScript is `/oss/javascript/langgraph/`, not the
    Python pages the name resolves to by default. See `_for_language`.
    """
    lang = languages.canonical(language)
    said = lang.name if lang else ""
    if use_memory and verify_best:
        remembered = recall(name, said)
        if remembered is not None:
            return remembered

    own = fetcher is None
    fetcher = fetcher or Fetcher(Options(delay=0.0))
    try:
        result = _official_answer(name, ecosystem, fetcher) if verify_best else None
        if result is None:
            result = _resolve_uncached(name, ecosystem, fetcher, verify_best, limit)
        if verify_best and result.best is not None and is_forge(result.best.url):
            _prefer_docs_behind_repo(result, name, fetcher)
        if verify_best and not ecosystem and result.resolved_via != "official":
            # Not over a language's own manual: `TheAlgorithms/Python` has
            # more stars than anything else named "python" and is not it.
            _weigh_popularity(result, name, fetcher, lang.name if lang else "")
        if verify_best and result.best is not None and result.resolved_via != "official":
            # The site's own `docs.` host first: `ansible.com`'s front page
            # links "Documentation" to one product's readthedocs, while
            # `docs.ansible.com` is Ansible's (round 1 re-run).
            _docs_host_beside(result, name, fetcher)
            _front_page_docs(result, name, fetcher)
        if lang is not None and verify_best:
            result = _for_language(result, name, lang, ecosystem, fetcher, limit)
    finally:
        if own:
            fetcher.close()

    if use_memory and verify_best:
        remember(name, result, said)
    return result


def _official_answer(name: str, ecosystem: str, fetcher: Fetcher) -> Resolution | None:
    """A language's or runtime's own manual, when `name` is one -- see
    `languages.OFFICIAL_DOCS` -- and it passes the same gate as anything else.
    Not when the caller named a registry: then they meant a package."""
    where = "" if ecosystem else languages.official_docs(name)
    if not where:
        return None
    cand = Candidate(where, "official:language", 0.97,
                     f"{name} is a language, and this is its own manual")
    verify(cand, name, fetcher, {"via_domain": True})
    if not cand.verified:
        return None
    return Resolution(name=name, candidates=[cand], best=cand, resolved_via="official",
                      note=(f"{name!r} is a programming language or runtime, not a "
                            f"package: resolved to its own manual rather than to a "
                            f"registry entry that happens to share the word."))


def _page_key(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() + (parsed.path or "/").rstrip("/").lower()


#: What a front page calls the link to its documentation.
_DOCS_WORDS = ("docs", "documentation", "read the docs", "user guide", "guide",
               "guides", "manual", "reference", "api docs", "api reference")
_ANCHOR = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']?([^\"'\s>#]+)[^>]*>(.*?)</a>", re.I | re.S)


_DOCS_SEGMENTS = {"doc", "docs", "documentation", "guide", "guides", "manual"}


#: A site-root `llms.txt` shorter than this is an overview of the site, not a
#: list of its documentation.
FRONT_MANIFEST_MAX = 3_000
#: ...and a documentation link from a front page is at most this deep.
FRONT_LINK_DEPTH = 2
_MD_ANCHOR = re.compile(r"\[([^\]]+)\]\(\s*<?([^)\s>]+)")


_NOT_DOCS_WORDS = {"contribute", "contributing", "contributors", "community", "forum",
                   "forums", "discuss", "blog", "news", "careers", "jobs"}

_ROOT_TAIL = re.compile(r"^(?:v?\d[\w.]*|current|latest|stable|master|main|next|"
                        r"index\.html?)$", re.I)


def _docs_root(url: str) -> bool:
    """Is this the root of a site's documentation -- `/doc`, `/docs/latest/`,
    `/en/docs/`, `/guide/index.html` -- rather than a page inside it?"""
    parts = [p.lower() for p in urlparse(url).path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", parts[0]) and len(parts) > 1:
        parts = parts[1:]                                   # a locale: /en/docs/
    return (bool(parts) and parts[0] in _DOCS_SEGMENTS
            and all(_ROOT_TAIL.match(p) for p in parts[1:]))


def _docs_path(url: str) -> bool:
    """A documentation address: a docs host, or a path with a docs segment
    (`symfony.com/doc`, `numpy.org/doc/stable/`, `nginx.org/en/docs/`)."""
    segments = {re.sub(r"\.\w+$", "", p).lower()
                for p in urlparse(url).path.split("/") if p}
    labels = set(_host(url).split(".")[:-1])
    return (_looks_like_docs(url) or bool(segments & _DOCS_SEGMENTS)
            or bool(labels & {"docs", "doc"})
            or _host(url) in ("docs.rs", "hexdocs.pm", "pkg.go.dev")
            or _host(url).endswith(".hexdocs.pm"))


def _front_page_docs(result: Resolution, name: str, fetcher: Fetcher) -> None:
    """The documentation a project's front page leads to, when the answer is
    the front page.

    `mypy` resolved to `mypy-lang.org`, whose "Documentation" link is
    `mypy.readthedocs.io`; `symfony` to `symfony.com`, whose "Docs" is
    `/doc/current/`; `supabase` for Python to `supabase.com`, where no
    language section could be found because every one of them is under
    `/docs/` (held-out round 2, 2026-09-24). A front page proves whose site
    it is and documents very little; harvested whole, it is the marketing
    as much as the manual.

    A documentation page already verified on the same site is taken first,
    at no cost; failing that, the page's own link named for its docs -- on
    the same site, or on a host that carries the name -- and only if that
    page passes the gate too.
    """
    best = result.best
    if best is None or best.source == "evidence:docs-host":
        # Already moved to the site's documentation host: its own front page
        # links back to the hub it came from (`guides.rubyonrails.org`'s
        # "Docs" is `rubyonrails.org/docs`), and following it goes in a circle.
        return
    # A site's own short `llms.txt` is its front page for machines: `dapr.io`'s
    # lists Home, Community, Adopters and Enterprise, then "Official docs" at
    # `docs.dapr.io` (held-out round 3, 2026-09-25). A long one lists the
    # documentation itself and is the answer.
    manifest = urlparse(best.url).path.strip("/").lower() == "llms.txt"
    if (is_forge(best.url) or is_package_host(best.url)
            or (urlparse(best.url).path.strip("/") and not manifest)
            or (_docs_path(best.url) and not manifest) or "docs-host" in best.signals):
        return
    site = _registrable(_host(best.url))
    slug = normalise(name)
    ready = [c for c in result.candidates
             if c.verified and c is not best and not is_forge(c.url) and _docs_path(c.url)
             and _registrable(_host(c.url)) == site
             and (_is_docs_host(c.url) or _docs_root(c.url))]
    chosen = max(ready, key=evidence) if ready else None
    if chosen is None:
        try:
            r = fetcher.get(best.url, timeout=PROBE_TIMEOUT, allow_redirects=True)
            html = r.text if getattr(r, "status_code", 0) == 200 else ""
            base = getattr(r, "url", "") or best.url
        except ForgeError:
            html, base = "", best.url
        if manifest and len(html) > FRONT_MANIFEST_MAX:
            return
        links = _ANCHOR.findall(html[:VERIFY_WINDOW])
        if manifest:
            links = [(href, label) for label, href in _MD_ANCHOR.findall(html)]
        ranked: list[tuple[int, str]] = []
        own_docs = False
        for href, label in links:
            words = re.sub(r"<[^>]+>|\s+", " ", label).strip().lower()
            if words.endswith((" docs", " documentation")) and len(words) <= 40:
                words = "docs"                      # "Symfony Docs", "API documentation"
            if words not in _DOCS_WORDS:
                continue
            link = _declared(urljoin(base, href.strip()))
            if _host(link) == _host(base) and _page_key(link) != _page_key(base):
                own_docs = True     # labelled docs, on this very site, whatever its path
            if (not link.startswith(("http://", "https://")) or is_forge(link)
                    or not _docs_path(link) or _page_key(link) == _page_key(base)):
                continue
            # The contributors' guide, the forum and the blog are not the
            # documentation, whatever the link says: jQuery's "Documentation"
            # nav entry went to `contribute.jquery.org/documentation/`.
            words_in = set(_host(link).split(".")) | {
                p.lower() for p in urlparse(link).path.split("/") if p}
            if words_in & _NOT_DOCS_WORDS:
                continue
            # A front page's link to its documentation is to a section, not
            # to a page deep inside one: `fastapi.tiangolo.com` has a nav
            # entry "docs" at `/reference/openapi/docs/`, the module that
            # serves `/docs`, and following it moved FastAPI's answer into
            # its API reference.
            if len([p for p in urlparse(link).path.split("/") if p]) > FRONT_LINK_DEPTH:
                continue
            # On the site itself, the root of its documentation, not a page in
            # it: `biomejs.dev`'s "Get started" is `/guides/getting-started`,
            # one section of a manual that spans several, and taking it cut
            # Biome's harvest to eight pages (round 1 re-run).
            # (`doc.traefik.io` beside `traefik.io` is a host of its own.)
            if _host(link) == _host(base) and not _docs_root(link):
                continue
            host = _host(link)
            first = ([p for p in urlparse(link).path.split("/") if p] or [""])[0]
            named = _owns_the_name(link, slug) or (is_package_host(link)
                                                   and normalise(first) == slug)
            if _registrable(host) != site and not named:
                continue
            ranked.append((_DOCS_WORDS.index(words), link))
        if own_docs:
            # A front page that documents itself on its own host is a docs
            # site; the package reference it also links is the reference half.
            # `tokio.rs` links "Docs" to `/tokio/tutorial` and "API docs" to
            # `docs.rs/tokio`, and taking the second failed the benchmark's
            # `tokio_is_crates` (final run, 2026-09-25).
            ranked = [(r, l) for r, l in ranked
                      if _host(l) not in ("docs.rs", "pkg.go.dev", "godocs.io")]
        for _rank, link in sorted(dict.fromkeys(ranked))[:3]:
            # `phoenix.hexdocs.pm/` is ExDoc's refresh stub for `overview.html`:
            # judged as it stands, it names nothing.
            try:
                first_hop = fetcher.get(link, timeout=PROBE_TIMEOUT, allow_redirects=True)
                hop = _follow_client_redirect(first_hop, getattr(first_hop, "url", "") or link,
                                              fetcher)
                if hop is not None:
                    link = getattr(hop, "url", "") or link
            except ForgeError:
                pass
            cand = Candidate(link, "front-page:docs", best.confidence,
                             f"{best.url} links to {link} as its documentation")
            # What the name usually means supplies the repository to judge
            # a package docs page by: `hexdocs.pm/phoenix` carries no claim
            # on the name of its own.
            popular = next((v for (k, _l), v in _POPULAR.items() if k == slug and v), None)
            facts = {"homepage": best.url, "via_domain": _registrable(_host(link)) == site}
            if popular:
                facts["repository"] = f"https://github.com/{popular['repo']}"
            verify(cand, name, fetcher, facts)
            if cand.verified:
                chosen = cand
                break
    if chosen is None:
        return
    chosen.release = chosen.release or best.release
    chosen.registry = chosen.registry or best.registry
    result.candidates = dedupe([chosen] + result.candidates)
    result.note = (f"{result.note} {best.url} is the project's front page; its "
                   f"documentation is {chosen.url}.").strip()
    result.best = chosen
    result.resolved_via = f"{result.resolved_via or 'domain'}+docs"


_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
#: Generators whose output is an API reference built from the code.
_REFERENCE_GENERATORS = ("rustdoc", "typedoc", "javadoc", "doxygen", "godoc", "pdoc")


def _generated_reference(html: str) -> bool:
    for tag in _META_TAG.findall(html[:LINK_WINDOW]):
        if re.search(r"""name\s*=\s*["']?generator\b""", tag, re.I):
            content = re.search(r"""content\s*=\s*["']?([^"'>]+)""", tag, re.I)
            if content and content.group(1).strip().lower().startswith(_REFERENCE_GENERATORS):
                return True
    return False


def _canonical_of(html: str, base: str) -> str:
    """The page's `rel=canonical` address, whatever order its attributes are in."""
    for tag in _LINK_TAG.findall(html[:LINK_WINDOW]):
        if re.search(r"""rel\s*=\s*["']?canonical\b""", tag, re.I):
            found = re.search(r"""href\s*=\s*["']?([^"'\s>]+)""", tag, re.I)
            if found:
                return urljoin(base, found.group(1))
    return ""


#: Host labels that mark a preview of the documentation, not the documentation.
_PRERELEASE_LABELS = {"alpha", "beta", "preview", "next", "staging", "canary", "nightly",
                      "edge", "dev", "rc"}

#: The first label of a sibling host a project keeps its manual on.
SIBLING_DOCS = ("docs", "guides", "guide", "learn", "manual", "developer")


def _docs_host_beside(result: Resolution, name: str, fetcher: Fetcher) -> None:
    """The site's documentation host, when the answer is elsewhere on that site.

    `sympy` resolved to `www.sympy.org/en/docs.html`, a page of links whose
    documentation is `docs.sympy.org`, and a harvest of it was nine pages of
    the project website (held-out round 3, 2026-09-25). `rails` and `ember`
    the same, one step further: `rubyonrails.org/docs` and `emberjs.com/docs`
    are hubs whose manuals are `guides.rubyonrails.org` and
    `guides.emberjs.com`, hosts that do not carry the bare name.

    Tried in order: `docs.<site>` when it carries the name, then the sibling
    documentation hosts the answer itself links to. Each must pass the gate;
    one request each, and `docs.` fails fast in DNS where there is none.
    """
    best = result.best
    if (best is None or is_forge(best.url) or is_package_host(best.url)
            or in_shared_namespace(best.url) or "docs-host" in best.signals
            or _is_docs_host(best.url)
            or re.search(r"/llms(?:-full)?\.txt$", urlparse(best.url).path)):
        # A manifest the site published is already its documentation, for
        # machines; a docs host is not a better answer than that.
        return
    site = _registrable(_host(best.url))
    slug = normalise(name)
    targets: list[tuple[str, str]] = []
    named = f"https://docs.{site}/"
    if _host(named) != _host(best.url) and _owns_the_name(named, slug):
        targets.append((named, f"docs.{site} is the documentation host of {_host(best.url)}"))
    if _docs_path(best.url):
        # A documentation hub: where it sends readers is the manual.
        targets += [(t, f"{best.url} links to {t} as its documentation")
                    for t in _sibling_docs(best.url, fetcher, site)
                    if _page_key(t) != _page_key(named)]
    popular = next((v for (k, _l), v in _POPULAR.items() if k == slug and v), None)
    for target, why in targets[:3]:
        cand = _docs_host_candidate(target, why, best, name, fetcher, popular)
        if cand is None:
            continue
        cand.release = cand.release or best.release
        cand.registry = cand.registry or best.registry
        result.candidates = dedupe([cand] + result.candidates)
        result.note = (f"{result.note} {best.url} is on the project's site; its "
                       f"documentation host is {cand.url}.").strip()
        result.best = cand
        result.resolved_via = f"{result.resolved_via or 'domain'}+docs-host"
        return


def _sibling_docs(url: str, fetcher: Fetcher, site: str) -> list[str]:
    """Roots of the documentation hosts beside `url` that its page links to,
    most-linked first."""
    try:
        r = fetcher.get(url, timeout=PROBE_TIMEOUT, allow_redirects=True)
    except ForgeError:
        return []
    if getattr(r, "status_code", 0) != 200:
        return []
    here = _host(getattr(r, "url", "") or url)
    counts: dict[str, int] = {}
    for match in _ANY_LINK.finditer(r.text or ""):
        link = urljoin(url, (match.group(1) or match.group(2) or match.group(3) or "").strip())
        host = _host(link)
        if (not host or host == here or _registrable(host) != site
                or host.split(".")[0] not in SIBLING_DOCS):
            continue
        root = f"https://{host}/"
        counts[root] = counts.get(root, 0) + 1
    return sorted(counts, key=lambda t: (SIBLING_DOCS.index(_host(t).split(".")[0]),
                                         -counts[t]))


def _docs_host_candidate(target: str, why: str, best: Candidate, name: str,
                         fetcher: Fetcher, popular: dict | None) -> Candidate | None:
    """`target` as the answer's documentation host, if it is one."""
    try:
        r = fetcher.get(target, timeout=PROBE_TIMEOUT, allow_redirects=True)
    except ForgeError:
        return None
    landed = getattr(r, "url", "") or target
    if getattr(r, "status_code", 0) != 200:
        return None
    # `docs.diesel.rs` is a refresh stub for `diesel.rs/docs`, and
    # `docs.serde.rs` one for Serde's rustdoc index: judged as served, both
    # replaced the project's own guide site (final round-2 run).
    hop = _follow_client_redirect(r, landed, fetcher)
    if hop is not None:
        r, landed = hop, getattr(hop, "url", "") or landed
    else:
        stub = getattr(r, "text", "") or ""
        if len(stub) < 4_000 and (_META_REFRESH.search(stub) or _JS_REDIRECT.search(stub)):
            return None             # a signpost to somewhere that could not be read
    if _host(landed) == _host(best.url):
        return None                 # an alias for the site already found
    if set(_host(landed).split(".")[:-2]) & _PRERELEASE_LABELS:
        # A preview of the next documentation is not the documentation of
        # record: `docs.jenkins.io` redirects to `alpha.docs.jenkins.io`,
        # while Jenkins documents itself at `jenkins.io/doc` (final round 3).
        return None
    if _generated_reference(getattr(r, "text", "") or ""):
        # An API reference generated from the code is the half of the
        # documentation a guide site links to, not a better answer than it.
        return None
    canonical = _canonical_of(getattr(r, "text", "") or "", landed)
    if canonical and _host(canonical) == _host(best.url):
        # The same site under a second name: `docs.helm.sh` serves Helm's
        # front page ("Artboard", canonical `https://helm.sh/`), and it
        # replaced `helm.sh/docs/` in the final round-1 run.
        return None
    cand = Candidate(landed, "evidence:docs-host", best.confidence, why)
    facts = {"homepage": best.url, "via_domain": True}
    if popular:
        facts["repository"] = f"https://github.com/{popular['repo']}"
    verify(cand, name, fetcher, facts)
    if not cand.verified:
        return None
    if "docs-host" not in cand.signals and "repo-backlink" not in cand.signals:
        # A sibling that does not carry the name must at least link the
        # project's repository: `guides.rubyonrails.org` links `rails/rails`.
        return None
    return cand


#: A repository needs this many stars to say what a name usually means...
POPULAR_MIN_STARS = 500
#: ...and this many to overrule an answer the ladder already reached.
POPULAR_OVERRIDE_STARS = 2000
_POPULAR: dict[tuple[str, str], dict | None] = {}
#: The longest wait for GitHub's search window to reopen, in seconds.
SEARCH_WAIT_MAX = 65
#: GitHub's names for the languages `languages` knows, where they differ.
_GH_LANGUAGE = {"python": "python", "go": "go", "rust": "rust", "java": "java",
                "kotlin": "kotlin", "csharp": "c#", "ruby": "ruby", "php": "php",
                "swift": "swift", "dart": "dart", "elixir": "elixir", "cpp": "c++"}


def _popular_repo(name: str, fetcher: Fetcher, language: str = "") -> dict | None:
    """The most-starred repository whose name *is* this name, if one stands out.

    What a name usually means is public evidence: `redis` is `redis/redis`
    (76,000 stars, redis.io), not the Python client PyPI files under the same
    word; `helm` is `helm/helm` (helm.sh), not a Rust crate; `rails` is
    `rails/rails`. Measured on 53 held-out names, 2026-09-24: the ladder sent
    eight of them to a same-named package or a squatter, and the most-starred
    repository of exactly that name pointed at the right site for all of them
    but `docker`, which has none. One search request, cached for the process.
    """
    slug = normalise(name)
    if len(slug) < 2:
        return None
    key = (slug, language)
    if key in _POPULAR:
        return _POPULAR[key]
    term = name.strip().split("/")[-1]
    query = f"{term} in:name"
    if _GH_LANGUAGE.get(language):
        query += f" language:{_GH_LANGUAGE[language]}"
    url = "https://api.github.com/search/repositories?" + urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": "10"})
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    for attempt in range(2):
        try:
            r = fetcher.get(url, timeout=REGISTRY_TIMEOUT, headers=headers)
        except (ForgeError, TypeError):
            break
        if r.status_code == 200:
            try:
                data = json.loads(r.text)
            except ValueError:
                data = None
            break
        # The search API allows ten requests a minute without a token, and its
        # window is a minute: wait it out once rather than learn nothing. A
        # 20-second cap gave up on most refusals, and without this evidence a
        # same-named stranger stands -- `kafka` became the Rust crate, `fiber`
        # Uber's library, `ember` a renamed programming language, all right
        # again a minute later (final round-2 run, 2026-09-25). Learning ten
        # dependencies at once is exactly the burst that meets the limit.
        headers = getattr(r, "headers", {}) or {}
        reset = headers.get("x-ratelimit-reset", "") or headers.get("X-RateLimit-Reset", "")
        wait = (int(reset) - time.time()) if str(reset).isdigit() else -1
        if attempt == 0 and r.status_code in (403, 429) and 0 < wait <= SEARCH_WAIT_MAX:
            time.sleep(wait + 1)
            continue
        break
    if not isinstance(data, dict):
        # Not remembered: a refusal says nothing about the name, and the
        # next resolution may be allowed to ask.
        return None
    flat = lambda text: re.sub(r"[^a-z0-9]", "", (text or "").lower())
    best = None
    for item in data.get("items") or []:
        called = item.get("name", "")
        if item.get("fork") or normalise(called) != slug:
            continue
        # `normalise` folds `redis-py` into `redis`, which is right for a
        # lookup and wrong here: the binding is exactly what this is for
        # telling apart. `three.js` for `three` is the same project.
        if flat(called) != flat(term) and _is_a_client(
                {"repository": f"https://github.com/{item.get('full_name', '')}"}, term):
            continue
        if best is None or item.get("stargazers_count", 0) > best["stars"]:
            home = _declared(item.get("homepage") or "")
            if not home:
                # No site declared: a Rust crate is documented on docs.rs and a
                # Go module on pkg.go.dev whatever its repository says --
                # `hyperium/tonic` declares nothing, and `tonic` went to a
                # Python project's readthedocs (held-out round 3, 2026-09-25).
                language = (item.get("language") or "").lower()
                if language == "rust":
                    home = f"https://docs.rs/{term.lower()}"
                elif language == "go":
                    home = f"https://pkg.go.dev/github.com/{item.get('full_name', '')}"
            best = {"repo": item.get("full_name", ""), "stars": item.get("stargazers_count", 0),
                    "homepage": home}
    if best is not None and best["stars"] < POPULAR_MIN_STARS:
        best = None
    # And the name's first meaning, not merely its best exact match: asked
    # for `nestjs`, the search's top result is `nestjs/nest` (76,726 stars,
    # matched through its owner), and the most-starred repository *called*
    # `nestjs` is a third party's monorepo of Nest modules -- which replaced
    # `docs.nestjs.com` in the round-1 re-run (2026-09-24). Where something
    # else carries the name further, it is not what the name means.
    top = max((i.get("stargazers_count", 0) for i in data.get("items") or []
               if not i.get("fork")), default=0)
    if best is not None and best["stars"] < top:
        best = None
    _POPULAR[key] = best
    return best


def _registrable(host: str) -> str:
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


#: Shared hosts whose subdomain is an owner, not a project.
_OWNER_PAGES = ("github.io", "gitlab.io", "codeberg.page")


def _same_project_site(url: str, popular: dict) -> bool:
    """Is `url` on the popular repository's own site, or the repository?"""
    if not url:
        return False
    if popular["repo"] and f"github.com/{popular['repo']}".lower() in url.lower():
        return True
    home = popular.get("homepage") or ""
    if not home:
        return False
    here, there = _host(url), _host(home)
    first = lambda u: ([p for p in urlparse(u).path.split("/") if p] or [""])[0].lower()
    if is_package_host(url):
        # docs.rs/<crate>: the host is shared, the first segment is the project.
        return here == there and first(url) == first(home)
    if in_shared_namespace(url):
        # On <owner>.github.io/<repo> the repository is the first segment;
        # <project>.readthedocs.io is the project's own host, whatever the
        # path. Compared by segment everywhere, `requests.readthedocs.io`
        # and `requests.readthedocs.io/en/latest/` were two projects, and
        # Requests was "overruled" by itself, losing its ecosystem (offline
        # benchmark `requests_is_pypi`, final run).
        if not here.endswith(_OWNER_PAGES):
            return here == there
        return here == there and (not first(home) or first(url) == first(home))
    return here == there or _registrable(here) == _registrable(there)


def _weigh_popularity(result: Resolution, name: str, fetcher: Fetcher,
                      language: str = "") -> None:
    """Check the answer against what the name usually means, and defer to it
    when the two disagree and the popular project is clearly dominant.

    The popular project's site goes through the same gate as anything else,
    judged against its repository; an answer is only replaced by one that
    passes. Not consulted when the caller named a registry: then the package
    in that registry is what was asked for.
    """
    # Narrowed to the language asked for first (`gin` for Go), then not: what
    # a name means does not change with the edition wanted, and
    # `elasticsearch` for Python has no Python repository of that name.
    popular = _popular_repo(name, fetcher, language)
    if popular is None and language:
        popular = _popular_repo(name, fetcher, "")
    if popular is None:
        return
    if result.unexamined and popular["stars"] < POPULAR_OVERRIDE_STARS:
        # A refusal because the strongest candidate could not be read is not
        # an absence for just anything to fill: the answer may be that very
        # page. Only what the name clearly means may answer it, through the
        # same gate -- and if the unreadable page *is* its site, the gate
        # cannot read it either and the refusal stands. `tonic` was refused
        # over a Python project's rate-limited readthedocs while
        # `hyperium/tonic` (docs.rs/tonic) was the meaning (round 3).
        return
    best = result.best
    own_repo = False
    if best is not None and _same_project_site(best.url, popular):
        if not (is_forge(best.url) and popular.get("homepage")):
            return
        # The answer is the project's repository and it declares a site --
        # what `_prefer_docs_behind_repo` does from the REST API, done from
        # what the search already said: that API allows sixty requests an
        # hour without a token, and `gradio` stayed a repository when they
        # ran out (held-out round 2).
        own_repo = True
    elif best is not None and popular["stars"] < POPULAR_OVERRIDE_STARS:
        return
    home = popular.get("homepage") or ""
    if not home or is_forge(home) or _is_index_listing(home):
        return
    repo = f"https://github.com/{popular['repo']}"
    cand = Candidate(home, "popular:github", 0.9,
                     f"the most-starred repository named {name!r} is {popular['repo']} "
                     f"({popular['stars']:,} stars), and it declares {home} as its site")
    options = [cand]
    if not _looks_like_docs(home) and not is_package_host(home):
        options = _with_roots(probe_docs_root(home, fetcher)) + options
    facts = {"repository": repo, "homepage": home}
    chosen = None
    for option in options:
        verify(option, name, fetcher, facts)
        if option.verified:
            chosen = option
            break
    if chosen is None:
        return
    chosen.evidence = chosen.evidence or cand.evidence
    was = best.url if best is not None else ""
    result.candidates = dedupe([chosen] + result.candidates)
    result.best = chosen
    result.unexamined = False
    result.resolved_via = (f"{result.resolved_via}+popular" if result.resolved_via
                           else "popular")
    if not own_repo:
        # A registry's release and ecosystem belonged to the package it
        # answered with, which this is not.
        result.release = ""
        if best is not None and best.registry:
            result.ecosystem = ""
    if own_repo:
        result.note = ((f"{result.note} " if result.note else "")
                       + f"The repository {was} declares {home} as its site, and "
                       f"{chosen.url} is its documentation.")
        return
    result.note = ((f"{result.note} " if result.note else "")
                   + f"The most-starred repository named {name!r} is {popular['repo']} "
                   f"({popular['stars']:,} stars), whose site is {chosen.url}"
                   + (f"; {was} documents a different project that shares the name"
                      if was else "") + ".")


_PAGE_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?:/[^\s\"'<>\)\]]*)?")


def _readme_docs(repo_url: str, name: str, fetcher: Fetcher, have: set,
                 most: int = 2) -> list[Candidate]:
    """Documentation sites a repository's own page links to, most-linked first.

    Only on a host that carries the name, and only at a documentation
    address: a README links to badges, sponsors and chat servers as well.
    """
    try:
        r = fetcher.get(repo_url, timeout=PROBE_TIMEOUT, allow_redirects=True)
    except ForgeError:
        return []
    if getattr(r, "status_code", 0) != 200:
        return []
    slug = normalise(name)
    counts: dict[str, int] = {}
    for link in _PAGE_URL.findall(r.text or ""):
        root = f"{urlparse(link).scheme}://{urlparse(link).netloc}/"
        if (is_forge(root) or is_package_host(root) or _is_index_listing(root)
                or not _owns_the_name(root, slug) or not _docs_path(root)
                or _page_key(root) in have):
            continue
        counts[root] = counts.get(root, 0) + 1
    ranked = sorted(counts, key=counts.get, reverse=True)[:most]
    return [Candidate(u, "evidence:readme-docs", 0.85,
                      f"{repo_url} links to {u} as documentation ({counts[u]} times)")
            for u in ranked]


def _with_roots(options: list[Candidate]) -> list[Candidate]:
    """Each `llms.txt` option followed by the page it sits in.

    A manifest can fail the gate where its site passes -- `docs.solidjs.com/
    llms.txt` names Solid 309 times and links nothing, while `docs.solidjs.com/`
    links the repository -- and one miss should not cost the site.
    """
    out: list[Candidate] = []
    for option in options:
        out.append(option)
        if urlparse(option.url).path.lower().endswith("/llms.txt"):
            root = option.url.rsplit("/", 1)[0] + "/"
            out.append(Candidate(root, option.source.replace("llms.txt", "root"),
                                 option.confidence - 0.05,
                                 f"{root} publishes {option.url}"))
    return out


def _prefer_docs_behind_repo(result: Resolution, name: str, fetcher: Fetcher) -> None:
    """A repository that won, swapped for the documentation site it declares.

    npm nominates `github.com/langchain-ai/langgraphjs#readme` for
    `@langchain/langgraph`, the repository passes the gate -- it is that
    project -- and a harvest of it is a README and whatever Markdown sits in
    `docs/`, while the project's documentation is a site of its own. The
    repository says where: its `homepage`, the owner's public statement. That
    site goes through the same gate as everything else, and wins only if it
    passes; otherwise the repository stands.
    """
    found = _GITHUB_REPO.match(result.best.url)
    if not found:
        return
    owner, project = found.group(1), re.sub(r"\.git$", "", found.group(2))
    data = _json(fetcher, f"https://api.github.com/repos/{owner}/{project}")
    home = _declared((data or {}).get("homepage") or "")
    probed: list[Candidate] = []
    if home and not is_forge(home) and not _is_index_listing(home):
        cand = Candidate(home, "evidence:repo-homepage", 0.9,
                         f"github.com/{owner}/{project} declares its homepage as {home}",
                         release=result.best.release)
        cand.registry = result.best.registry
        # The documentation root under a declared homepage before the
        # homepage itself, which is usually the project's front page.
        probed = ([cand] if _looks_like_docs(home) or is_package_host(home)
                  else _with_roots(probe_docs_root(home, fetcher)) + [cand])
    # Every crate is documented on docs.rs and every Go module on pkg.go.dev,
    # whether or not the repository says so: `tokio-rs/axum` declares no
    # homepage, and its documentation is `docs.rs/axum`.
    registry = result.best.registry or result.ecosystem
    if registry == "crates":
        crate = name.strip().lower()
        probed.append(Candidate(f"https://docs.rs/{crate}", "evidence:docs.rs", 0.85,
                                f"{crate} is a crate, and docs.rs builds every crate's "
                                f"documentation"))
    elif registry == "go":
        probed.append(Candidate(f"https://pkg.go.dev/github.com/{owner}/{project}",
                                "evidence:pkg.go.dev", 0.85,
                                f"github.com/{owner}/{project} is a Go module, and "
                                f"pkg.go.dev documents every one"))
    # The repository's own statement of where it lives counts as a registry's
    # does: `date-fns.org` and `www.gradio.app` render their pages in the
    # browser, and judged on the repository link alone neither passed.
    # Last, the documentation its README links to: `pmndrs/zustand` declares
    # a demo as its homepage and documents itself at `zustand.docs.pmnd.rs`,
    # which only the README names (held-out round 2).
    probed += _readme_docs(result.best.url, name, fetcher,
                           {_page_key(c.url) for c in probed})
    facts = {"repository": result.best.url, **({"homepage": home} if home else {})}
    for option in probed:
        verify(option, name, fetcher, facts)
        if option.verified:
            option.registry = option.registry or result.best.registry
            option.release = option.release or result.best.release
            result.candidates = dedupe([option] + result.candidates)
            result.note = (f"{result.note} The repository {result.best.url} won the "
                           f"registry lap; {option.url} is its documentation "
                           f"({option.evidence}), and it passed the same checks.").strip()
            result.best = option
            result.resolved_via = f"{result.resolved_via}+repo-homepage"
            return


#: How many alternative URLs a language variant may cost.
VARIANT_TRIES = 8


def _variant_urls(url: str, lang: "languages.Language") -> list[str]:
    """Where the `lang` edition of the page at `url` would be, most likely first.

    A language segment swapped (`/oss/python/langgraph/` -> `/oss/javascript/
    langgraph/`), a host label swapped (`python.langchain.com` ->
    `js.langchain.com`), or a language prefix put in front of a path that
    names none (`playwright.dev/docs/intro` -> `playwright.dev/python/docs/intro`).
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    trailing = parsed.path.endswith("/")
    out: list[str] = []

    def path_of(segs: list[str]) -> str:
        return "/" + "/".join(segs) + ("/" if trailing and segs else "")

    swapped = False
    for i, seg in enumerate(parts):
        other = languages.segment_language(seg)
        if other is not None and other.name != lang.name:
            swapped = True
            for token in lang.segments:
                out.append(parsed._replace(path=path_of(parts[:i] + [token] + parts[i + 1:])).geturl())
    labels = (parsed.hostname or "").split(".")
    if labels and languages.segment_language(labels[0]) not in (None, lang):
        swapped = True
        for token in lang.segments:
            host = ".".join([token] + labels[1:])
            out.append(parsed._replace(netloc=host).geturl())
    if not swapped:
        for token in lang.segments[:2]:
            out.append(parsed._replace(path=path_of([token] + parts)).geturl())
            if parts:
                out.append(parsed._replace(path=path_of(parts[:1] + [token] + parts[1:])).geturl())
    return list(dict.fromkeys(out))


def _content_of(body: str, url: str) -> str:
    """A fetched page's documentation, not its scripts: what `languages`
    measures has to be the words and code a reader sees (see
    `languages.evidence`)."""
    if not body:
        return ""
    head = body.lstrip()[:600].lower()
    if not (head.startswith(("<!doctype", "<html")) or "<html" in head[:200]):
        return body                                 # Markdown, text: already content
    try:
        from docsforge.core import engine
        return engine._html_to_md(body, url)[1]
    except Exception:                               # noqa: BLE001 -- unreadable is empty
        return _visible_text(body)


#: Links in HTML, and in the Markdown an `llms.txt` is written in.
#: Unquoted too: a minified Hugo site writes `href=/docs/languages/`, and
#: `grpc.io/docs/` offered no link at all to a quoted-only pattern.
_ANY_LINK = re.compile(r"""href\s*=\s*(?:["']([^"'#]+)|([^\s>"'#]+))|\]\(\s*<?([^)\s>#]+)""")


def _language_links(html: str, base: str, lang: "languages.Language", name: str,
                    most: int = 3) -> list[str]:
    """Where a page that serves several languages sends readers of one.

    `docs.langchain.com/` is one front page for Python and JavaScript alike,
    and links to `/oss/python/...` and `/oss/javascript/...`; there is no
    segment in its own URL to swap. Links are grouped by the path through the
    language's segment -- `/develop/typescript/`, `/platforms/python/`,
    `/docs/languages/go/` -- and, where the next segment names the project
    (`/oss/javascript/langgraph/`), by that. Each group offers its section
    root, then the shallowest page in it; the group that names the project
    comes first, then the largest.

    Read from Markdown links as well as `href`s, because the page found is
    often an `llms.txt`, and from the site's sibling hosts: `temporal.io`
    files its per-language guides on `docs.temporal.io` (held-out test,
    2026-09-24: all four such cases missed their edition for one of these).
    """
    if not html:
        return []
    site = _registrable((urlparse(base).hostname or "").lower())
    slug = normalise(name)
    groups: dict[str, list[str]] = {}
    for match in _ANY_LINK.finditer(html):
        href = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not href:
            continue
        link = urljoin(base, href)
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if not host or _registrable(host) != site or is_forge(link):
            continue
        parts = [_PAGE_FILE.sub("", p) for p in parsed.path.split("/") if p]
        parts = [p for p in parts if p]
        for i, part in enumerate(parts):
            if _segment_names(part, lang, slug):
                depth = i + 1
                if i + 1 < len(parts) - 1 and slug and slug in normalise(parts[i + 1]):
                    depth = i + 2
                key = f"{parsed.scheme}://{host}/" + "/".join(parts[:depth]) + "/"
                groups.setdefault(key, []).append(link)
                break
    if not groups:
        return []
    # The project's own section first; a guide before an API reference --
    # from `pulumi.com/docs`, `/docs/reference/pkg/python/` outnumbers
    # `/docs/iac/languages-sdks/python/` and is the reference half; then
    # the largest.
    reference = lambda key: bool({p.lower() for p in urlparse(key).path.split("/")}
                                 & {"reference", "api", "apidocs", "pkg", "packages"})
    ranked = sorted(groups.items(),
                    key=lambda kv: (bool(slug) and slug in normalise(urlparse(kv[0]).path),
                                    not reference(kv[0]), len(kv[1])),
                    reverse=True)
    out: list[str] = []
    for key, links in ranked[:most]:
        shallowest = min(links, key=lambda u: (urlparse(u).path.count("/"), len(u)))
        out += [key, shallowest]
    return list(dict.fromkeys(out))


#: A page's own file name, which says nothing about where it is filed:
#: `platforms/python.md` is the Python section, `languages/index.md` the hub.
_PAGE_FILE = re.compile(r"(?:^index)?\.(?:md|mdx|html?|txt)$", re.I)

#: The page a multi-language site lists its languages on.
_HUB_WORDS = ("languages", "language", "sdks", "sdk", "platforms", "libraries",
              "client-libraries", "clients", "client", "integrations")


def _language_hubs(html: str, base: str, most: int = 2, name: str = "") -> list[str]:
    """Links to the page that lists a site's languages, nearest first.

    `grpc.io/docs/` names no language; `grpc.io/docs/languages/` names all of
    them. One hop, taken only when the page itself offered nothing.
    """
    site = _registrable((urlparse(base).hostname or "").lower())
    hubs: list[str] = []
    for match in _ANY_LINK.finditer(html or ""):
        link = urljoin(base, (match.group(1) or match.group(2) or match.group(3) or "").strip())
        parsed = urlparse(link)
        if _registrable((parsed.hostname or "").lower()) != site or is_forge(link):
            continue
        parts = [_PAGE_FILE.sub("", p) for p in parsed.path.split("/") if p]
        parts = [p for p in parts if p]
        # Whole, or as the last word of a compound: Elastic's
        # `/client/index.html` and `/docs/reference/elasticsearch-clients`.
        if parts and (parts[-1].lower() in _HUB_WORDS
                      or parts[-1].lower().rsplit("-", 1)[-1] in _HUB_WORDS):
            hubs.append(link)
    # The hub for the project asked about before a sibling product's:
    # `elastic.co/guide/` lists `enterprise-search-clients/` above
    # `elasticsearch/client/`, and the first led to the wrong product's
    # Python client.
    slug = normalise(name) if name else ""
    return sorted(dict.fromkeys(hubs),
                  key=lambda u: (not (slug and slug in normalise(urlparse(u).path)),
                                 urlparse(u).path.count("/")))[:most]


#: A redirect onto one of these is a site turning a reader away, not an edition.
_TURNED_AWAY = re.compile(r"/(?:auth|login|log-in|signin|sign-in|signup|sign-up|account|"
                          r"register|sso)(?:/|$)", re.I)


def _answers_anything(option: str, lang: "languages.Language", fetcher: Fetcher) -> bool:
    """Does the site answer 200 where no edition could be?

    `sentry.io/python/` lands on a login page that repeats the path it was
    given, so the path "names" Python and the site "answers" it. The same
    address with the language's segment replaced by a word nothing publishes
    tells the two apart: one request, spent only when the path is the only
    evidence.
    """
    parsed = urlparse(option)
    parts = [p for p in parsed.path.split("/") if p]
    swapped = [("zq-docsforge-none" if p.lower() in lang.segments else p) for p in parts]
    if swapped == parts:
        return False
    control = parsed._replace(path="/" + "/".join(swapped) + "/").geturl()
    try:
        r = fetcher.get(control, timeout=PROBE_TIMEOUT, allow_redirects=True)
    except ForgeError:
        return False
    return getattr(r, "status_code", 0) == 200


def _same_site_root(a: str, b: str) -> bool:
    """One page, or a site and a page at its root: `playwright.dev` and
    `playwright.dev/`, or `x.dev` and `x.dev/docs/` when nothing narrower
    separates them."""
    if _page_key(a) == _page_key(b):
        return True
    pa, pb = urlparse(a), urlparse(b)
    if (pa.hostname or "").lower().removeprefix("www.") != \
            (pb.hostname or "").lower().removeprefix("www."):
        return False
    return (pa.path or "/").strip("/") == "" or (pb.path or "/").strip("/") == ""


def _segment_names(segment: str, lang: "languages.Language", slug: str = "") -> bool:
    """Does one path segment name `lang`?

    Whole, as `/python/` does; or as one word of a compound, as Elastic's
    `/client/python-api/` and a `/ruby-sdk/` do -- but only for a word of four
    letters or more: `go-live` is not Go, and `net-core` is not .NET.
    """
    low = segment.lower()
    if low in lang.segments:
        return True
    # A language and the word for what is published for it, nothing else:
    # `event-history-typescript` is one page of Temporal's encyclopedia, and
    # read as a section it replaced `/develop/typescript/` (round 1 re-run).
    words = [w for w in re.split(r"[-_.]", low) if w]
    if len(words) != 2:
        return False
    first, second = words
    # The project and its language, however short: `redis-py`, `docker-py`,
    # `go-redis`. Redis files its Python client at
    # `/develop/clients/redis-py/`, and with only four-letter words trusted a
    # group of FastAPI tutorials was taken for the Python edition instead.
    if slug and ((first == slug and second in lang.segments)
                 or (second == slug and first in lang.segments)):
        return True
    return ((len(first) >= 4 and first in lang.segments and second in _EDITION_WORDS)
            or (len(second) >= 4 and second in lang.segments and first in _EDITION_WORDS))


#: What a site calls one language's edition of itself, beside the language.
_EDITION_WORDS = {"api", "sdk", "client", "driver", "lib", "library", "bindings",
                  "docs", "guide", "reference", "quickstart"}


def _names_language(url: str, lang: "languages.Language", slug: str = "") -> bool:
    parsed = urlparse(url)
    words = [p.lower() for p in parsed.path.split("/") if p]
    host = ((parsed.hostname or "").split(".") or [""])[0].lower()
    return host in lang.segments or any(_segment_names(w, lang, slug) for w in words)


def _switch_to_language(result: Resolution, name: str, lang: "languages.Language",
                        fetcher: Fetcher, limit: int) -> bool:
    """Make `result.best` the `lang` edition, if the site publishes one.

    True when it already is -- the path names the language, the page it lands
    on does, or its content is written for it and nothing more specific
    exists -- or when an edition beside it was found and put in its place.
    Worked from where the page *lands*: LangChain's repository declares
    `docs.langchain.com/langchain/`, which lands on the Python edition, and
    the JavaScript one is a segment away from there, not from the address
    that redirected.
    """
    best = result.best
    if best is None:
        return False
    if _names_language(best.url, lang, normalise(name)):
        return True
    try:
        r = fetcher.get(best.url, timeout=PROBE_TIMEOUT, allow_redirects=True)
        raw = r.text if getattr(r, "status_code", 0) == 200 else ""
        here = (getattr(r, "url", "") or best.url) if raw else best.url
    except ForgeError:
        raw, here = "", best.url
    if _names_language(here, lang, normalise(name)):
        best.url = here
        return True
    page = _content_of(raw, here)
    shows = bool(page) and languages.written_for(page, lang) is True
    if shows and languages.dominant(page) == lang.name:
        result.note = (f"{result.note} The documentation found is written for "
                       f"{lang.name}.").strip()
        return True
    # A manifest is a file in a directory; its editions are the directory's.
    # `opentelemetry.io/go/` redirects to `/docs/languages/go/`, and
    # `opentelemetry.io/llms.txt/go` is nothing.
    base = (here.rsplit("/", 1)[0] + "/"
            if re.search(r"/llms(?:-full)?\.txt$", urlparse(here).path) else here)
    links = _language_links(raw, here, lang, name)
    if not links:
        for hub in _language_hubs(raw, here, name=name):
            try:
                r = fetcher.get(hub, timeout=PROBE_TIMEOUT, allow_redirects=True)
            except ForgeError:
                continue
            if getattr(r, "status_code", 0) == 200:
                links = _language_links(r.text or "", getattr(r, "url", "") or hub,
                                        lang, name)
            if links:
                break
    options = _variant_urls(base, lang)[:VARIANT_TRIES] + links
    for option in list(dict.fromkeys(options)):
        try:
            r = fetcher.get(option, timeout=PROBE_TIMEOUT, allow_redirects=True)
        except ForgeError:
            continue
        if getattr(r, "status_code", 0) != 200:
            continue
        landed = getattr(r, "url", "") or option
        if _page_key(landed) in (_page_key(best.url), _page_key(here)):
            continue                                # sent back where it started
        if _TURNED_AWAY.search(urlparse(landed).path) and not _TURNED_AWAY.search(
                urlparse(option).path):
            continue                                # a login page, not an edition
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "markdown" not in ctype and "text/plain" not in ctype:
            continue
        body = _content_of(r.text or "", landed)
        fits = languages.written_for(body, lang)
        if fits is False or (fits is None and not _names_language(landed, lang, normalise(name))):
            continue
        if fits is None and _answers_anything(option, lang, fetcher):
            continue                                # the path is all it had
        cand = Candidate(landed, f"variant:{lang.name}", best.confidence,
                         f"the {lang.name} edition of {here}", True,
                         f"the {lang.name} edition the site publishes beside the "
                         f"verified page {here}",
                         release="")
        cand.signals = list(best.signals) + [f"language:{lang.name}"]
        cand.authority = best.authority
        result.candidates = dedupe([cand] + result.candidates)[:limit]
        was = languages.dominant(page) or "another language"
        result.note = (f"{result.note} {here} documents {name} for {was}; the site "
                       f"publishes the {lang.name} edition at {landed}, and that is "
                       f"what was asked for.").strip()
        result.best = cand
        result.resolved_via = f"{result.resolved_via or 'registry'}+{lang.name}"
        # The registry release was the default edition's package, not
        # necessarily this one's; the harvest reads the pages instead.
        result.release = ""
        return True
    if shows:
        # A page that shows this language among others, and no edition of
        # its own to be found: a Stripe page with a tab per SDK.
        result.note = (f"{result.note} The documentation found shows {lang.name} "
                       f"among the languages it covers.").strip()
        return True
    return False


def _for_language(result: Resolution, name: str, lang: "languages.Language",
                  ecosystem: str, fetcher: Fetcher, limit: int) -> Resolution:
    """The `lang` edition of what resolution found, or an honest note.

    Three ways a project answers "which language": its pages already serve
    it; it files one edition per language and the found page belongs to
    another (LangGraph, Playwright) -- then the same path under the asked
    language is tried, and taken only if the site answers there and the page
    is written for that language or filed under its name; or it publishes
    that language's package separately, to be found through that language's
    registry and then, if need be, switched the same way.
    """
    result.language = lang.name
    best = result.best
    if best is not None and _switch_to_language(result, name, lang, fetcher, limit):
        return result

    # No edition beside it: the language's own registry.
    if lang.ecosystem in REGISTRIES and lang.ecosystem != (ecosystem or ""):
        other = _resolve_uncached(name, lang.ecosystem, fetcher, True, limit)
        if other.best is not None and is_forge(other.best.url):
            _prefer_docs_behind_repo(other, name, fetcher)
        if other.best is not None:
            other.language = lang.name
            if _switch_to_language(other, name, lang, fetcher, limit):
                other.note = (f"{other.note} Found through {lang.ecosystem}, the "
                              f"{lang.name} registry.").strip()
                return other
            if best is not None and _same_site_root(other.best.url, best.url):
                # The language's own registry nominates what was already
                # found: `playwright` on npm points at playwright.dev, which
                # is the Node edition, whatever its landing page shows of it.
                result.note = (f"{result.note} {lang.ecosystem}, the {lang.name} "
                               f"registry, points at this same documentation.").strip()
                return result

    if best is not None:
        result.note = (f"{result.note} No {lang.name} edition of this documentation "
                       f"was found; what was found may be written for another "
                       f"language -- check before relying on its examples.").strip()
    return result


def _resolve_uncached(name: str, ecosystem: str = "", fetcher: Fetcher | None = None,
                      verify_best: bool = True, limit: int = 6) -> Resolution:
    """Find where `name` documents itself.

    Returns every candidate with its evidence rather than silently picking one,
    so a caller that disagrees can see why and choose differently.
    """
    result = Resolution(name=name, ecosystem=ecosystem or guess_ecosystem(name),
                        asked_ecosystem=ecosystem)
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
        if provisional is not None:
            # The held page competes, so it is never trimmed away. Cut by
            # confidence, it could fall past `limit`, miss the re-verification
            # below, and still come back from `_settle_held` as a `best` that
            # `candidates` does not list — `find_docs` printed a "Best:" that
            # was none of the candidates above it.
            key = _same_page(provisional.url)
            if all(_same_page(c.url) != key for c in result.candidates):
                result.candidates.append(
                    next(c for c in ranked if _same_page(c.url) == key))

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
            # A package whose own repository is named for something else is a
            # client for the thing asked about, not the thing: PyPI's `redis`
            # lives at `redis/redis-py`, its `docker` at `docker/docker-py`.
            # Such a package cannot unseat the project's own site (R10's hold
            # was written for a squatter against the real package, where the
            # repository does carry the name: `pallets/flask`).
            clients = frozenset(
                id(c) for c in result.candidates
                if c.registry and _is_a_client(_facts_for(c, found, facts), name))
            picked = (_settle_held(provisional, result.candidates, clients)
                      if provisional is not None
                      else best_verified(result.candidates))
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
