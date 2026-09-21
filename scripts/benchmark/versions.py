"""The versions benchmark: five technologies, each learned at two releases.

A library's versions contradict each other, and DocsForge stores them side
by side so that a model working against `django==4.2` is not answered from
the 6.x manual. This suite measures that contract end to end, by name alone,
the way a model would use it: `learn_technology(name)` for the current
documentation, `learn_technology(name, version=...)` for a release pinned
in a lockfile, and then every read and search that has to tell the two
apart.

The five were chosen for being five different *shapes* of versioned site,
each checked by hand on 2026-09-21:

* **django** (pypi) -- Sphinx behind a sitemap index whose English sitemap
  lists every release side by side, `/en/4.2/` next to `/en/6.1/` and
  `/en/dev/`: 11,209 URLs, so the current harvest is bounded by the cap.
* **poetry** (pypi) -- a Hugo site whose current docs live at `/docs/` with
  no version in the path, beside `/docs/1.8/` and `/docs/main/`; 1.8 is
  fifteen pages and comes in whole.
* **jest** (npm) -- Docusaurus, current at `/docs/` beside `/docs/29.7/`,
  `/docs/30.0/` and `/docs/next/`; found by its own domain before any
  registry is asked, so no registry release labels the current harvest.
* **sequelize** (npm) -- Docusaurus with *both* releases versioned in the
  path, `/docs/v6/` (stable) and `/docs/v7/` (alpha): the pinned release is
  the newer one, so the default read must prefer it.
* **pydantic** (pypi) -- MkDocs with versioned paths (`/docs/validation/1.10/`
  exists) but an `llms.txt` published only for `latest`, and no sitemap
  covering the docs: a release nothing indexes.

Each is harvested with a page cap (`DOCSFORGE_VERSIONS_PAGES`, default 40):
the question here is which release a page belongs to, not whether every
page arrived, and the offline suite already measures a harvest run to the
end. The cleanup suite removes all ten corpora afterwards, so a rerun
measures the harvests again rather than reading them back from the store.

Nothing here imports DocsForge. The expectations -- which path a release's
pages live under, which of the two the default read should prefer -- are
claims about the sites and about the version contract as documented in
`docsforge/core/versions.py`, written down so a run can disagree with them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from .cases import (Case, Context, Result, check_forgotten, check_listing,
                    delete_only, local_only, refused)

#: Pages per harvest. Enough for every site's first release-scoped section to
#: show which release it is; small enough for ten harvests to take minutes.
PAGES = int(os.environ.get("DOCSFORGE_VERSIONS_PAGES") or 40)

#: `Harvested **django** 4.2 — 40 pages, ...`
HARVESTED = re.compile(r"Harvested \*\*(?P<name>[^*]+)\*\* (?P<label>\S+) [—-] (?P<pages>[\d,]+) pages?")
#: `**django** 6.1.1 is already stored — 40 pages, harvested ...`
ALREADY = re.compile(r"\*\*(?P<name>[^*]+)\*\* (?P<label>\S+) is already stored")
#: `Resolved **django** to https://docs.djangoproject.com/`
RESOLVED = re.compile(r"Resolved \*\*(?P<name>[^*]+)\*\* to (?P<url>\S+)")
#: What `_harvest_now` appends when a pinned release could not be shown to
#: be the release the pages document.
CAVEAT = "is the label you asked for, not a finding"
#: One stored page in a `read_knowledge_base` answer.
SOURCE = re.compile(r"^Source: <(?P<url>[^>]+)>", re.M)
#: One passage in a `search_knowledge_base` answer: `- **poetry** 1.8 · Title`.
PASSAGE = re.compile(r"^- \*\*(?P<tech>[^*]+)\*\* (?P<label>\S+) · ", re.M)
#: The labels in a listing's `versions:` line: `2.5.1 (40 pages, ...), 1.8 (15 pages, ...)`.
LABELS = re.compile(r"(\S+) \(\d[\d,]* pages?,")


@dataclass(frozen=True)
class Versioned:
    """One technology and the two releases the suite asks for."""
    name: str
    host: str                   # where its documentation must resolve to
    pinned: str                 # the release asked for with `version=`
    pinned_path: str            # the path segment that release's pages live under
    current: Callable[[str, str], bool]   # (page url, current label) -> is this page the current release's?
    query: str                  # a phrase both releases document, on an early page
    newest: str = "current"     # which of the two the default read must prefer: "current" | "pinned"
    note: str = ""

    def key(self, which: str) -> str:
        return f"versions:{self.name}:{which}"


def _major_minor(label: str) -> str:
    m = re.match(r"v?(\d+\.\d+)", label)
    return m.group(1) if m else ""


TECHNOLOGIES: list[Versioned] = [
    Versioned(
        "django", "docs.djangoproject.com", pinned="4.2", pinned_path="/en/4.2/",
        current=lambda url, label: f"/en/{_major_minor(label)}/" in url,
        query="custom management command",
        note="the English sitemap lists every release; PyPI's release names the current one"),
    Versioned(
        "poetry", "python-poetry.org", pinned="1.8", pinned_path="/docs/1.8/",
        current=lambda url, label: bool(re.search(r"python-poetry\.org/docs/(?!\d|main/)", url)),
        query="poetry add",
        note="current docs are unversioned at /docs/, beside /docs/1.8/ and /docs/main/"),
    Versioned(
        "jest", "jestjs.io", pinned="29.7", pinned_path="/docs/29.7/",
        current=lambda url, label: bool(re.search(r"jestjs\.io/docs/(?!\d|next/)", url)),
        query="expect matchers",
        note="resolved by its own domain, so the current harvest is labelled by date, not npm's release"),
    Versioned(
        "sequelize", "sequelize.org", pinned="v7", pinned_path="/docs/v7/",
        current=lambda url, label: "/docs/v6/" in url,
        query="findAll where", newest="pinned",
        note="both releases are versioned in the path; v6 is npm's latest, v7 is newer"),
    Versioned(
        "pydantic", "pydantic.dev", pinned="1.10", pinned_path="/docs/validation/1.10/",
        current=lambda url, label: "/docs/validation/latest/" in url,
        query="BaseModel validation",
        note="/docs/validation/1.10/ exists, but only latest publishes an llms.txt and no sitemap covers the docs"),
]


# -- reading answers ---------------------------------------------------------

def _label_of(text: str) -> str:
    m = HARVESTED.search(text) or ALREADY.search(text)
    return m["label"] if m else ""


def _needs(ctx: Context, t: Versioned, which: str) -> str:
    label = ctx.memo.get(t.key(which))
    if not label:
        raise LookupError(f"the {which} harvest of {t.name} did not land")
    return label


# -- checks ------------------------------------------------------------------

def check_current(t: Versioned):
    """`learn_technology(name)`: the current documentation, honestly labelled."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        if "**failed**" in r.text or "**stopped reporting**" in r.text:
            return f"the harvest did not finish: {r.text[:200]!r}"
        m = RESOLVED.search(r.text)
        if m and t.host not in m["url"]:
            return f"resolved to {m['url']}, not {t.host}"
        label = _label_of(r.text)
        if not label:
            return f"unexpected shape: {r.text[:160]!r}"
        ctx.memo[t.key("current")] = label
        if CAVEAT in r.text:
            return "an unpinned harvest carries the unverified-version caveat"
        if label.lower() == t.pinned.lower():
            return f"the unpinned harvest was filed under the pinned release {label}"
        return None
    return check


def check_pinned(t: Versioned):
    """`learn_technology(name, version=pinned)`: that release, confirmed."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        if "**failed**" in r.text or "**stopped reporting**" in r.text:
            return f"the harvest did not finish: {r.text[:200]!r}"
        m = RESOLVED.search(r.text)
        if m and t.host not in m["url"]:
            return f"resolved to {m['url']}, not {t.host}"
        label = _label_of(r.text)
        if not label:
            return f"unexpected shape: {r.text[:160]!r}"
        if label.lower() != t.pinned.lower():
            return f"filed under {label!r}, not the {t.pinned!r} that was asked for"
        ctx.memo[t.key("pinned")] = label
        if CAVEAT in r.text:
            return (f"{t.pinned} was stored but not confirmed: "
                    f"'Version {t.pinned!r} {CAVEAT}'")
        return None
    return check


def check_listed(t: Versioned):
    """The listing shows the technology with exactly the two releases."""
    def check(r: Result, ctx: Context) -> str | None:
        why = check_listing(r, ctx)
        if why:
            return why
        ctx.stale = False
        tech = ctx.tech(t.name)
        if tech is None:
            return f"{t.name} is not in the listing"
        labels = LABELS.findall(tech["labels"])
        want = {ctx.memo.get(t.key("current"), "?"), ctx.memo.get(t.key("pinned"), "?")}
        if tech["versions"] != 2 or set(labels) != want:
            return f"listing shows versions {labels}, expected {sorted(want)}"
        return None
    return check


def check_pages(t: Versioned, which: str):
    """Every page read back under a label belongs to that release.

    Judged by the page URLs the store kept: a release's pages live under its
    path on every one of these sites. `read_knowledge_base` caps a whole
    read at 300,000 characters, so this sees the first pages, which is
    where a harvest that took the wrong release shows it first.
    """
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        # The site's front page is where a domain-resolved harvest starts;
        # it belongs to no release and is not what this asks about.
        urls = [u for u in SOURCE.findall(r.text) if urlsplit(u).path not in ("", "/")]
        if not urls:
            return "no pages came back"
        label = ctx.memo.get(t.key(which), "")
        if which == "pinned":
            bad = [u for u in urls if t.pinned_path not in u + "/"]
            what = f"not under {t.pinned_path}"
        else:
            bad = [u for u in urls if not t.current(u + "/", label)]
            what = "not the current release's"
        if bad:
            return (f"{len(bad)} of the {len(urls)} pages shown under {label!r} are "
                    f"{what}: {bad[0]}")
        return None
    return check


def check_default(t: Versioned):
    """`learn_technology(name)` on a stored technology names the release a
    versionless read would give: the newest by the store's own ordering."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        m = ALREADY.search(r.text)
        if not m:
            return f"a stored technology was not reported as stored: {r.text[:160]!r}"
        if "Nothing was fetched" not in r.text:
            return "says already stored but does not say nothing was fetched"
        expected = ctx.memo.get(t.key(t.newest), "?")
        if m["label"] != expected:
            other = ctx.memo.get(t.key("pinned" if t.newest == "current" else "current"), "?")
            return (f"the default version is {m['label']!r}; expected {expected!r} "
                    f"(the {t.newest} release) over {other!r}")
        return None
    return check


def check_scoped_search(t: Versioned):
    """A search scoped to the pinned release answers only from it."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        labels = [m["label"] for m in PASSAGE.finditer(r.text)]
        if not labels:
            return f"no passage for {t.query!r} in {t.name} {t.pinned}: {r.text[:120]!r}"
        wrong = [l for l in labels if l != t.pinned]
        if wrong:
            return f"{len(wrong)} of {len(labels)} passages come from {sorted(set(wrong))}, not {t.pinned}"
        return None
    return check


def check_search_labels(t: Versioned):
    """A search across both releases names, on every passage, which one it
    came from -- and only labels that are stored. Ranking decides which
    release fills the ten slots, so both appearing is not required."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        head = r.text.splitlines()[0] if r.text.strip() else ""
        m = re.match(r"^(\d+) passage", head)
        if not m:
            return f"no passages for {t.query!r} in {t.name}: {r.text[:120]!r}"
        labels = [x["label"] for x in PASSAGE.finditer(r.text)]
        if len(labels) != int(m.group(1)):
            return f"{m.group(1)} passages but {len(labels)} carry a version label"
        stored = {ctx.memo.get(t.key("current")), ctx.memo.get(t.key("pinned"))}
        foreign = sorted(set(labels) - stored)
        if foreign:
            return f"passages labelled {foreign}, which is not a stored version"
        return None
    return check


def check_unknown_version(t: Versioned):
    """A version that is not stored is refused, and both that are are named."""
    def check(r: Result, ctx: Context) -> str | None:
        why = refused("has no version", "Stored versions")(r, ctx)
        if why:
            return why
        for which in ("current", "pinned"):
            label = ctx.memo.get(t.key(which), "")
            if label and label not in r.text:
                return f"the refusal does not name the stored {which} release {label!r}"
        return None
    return check


# -- the cases ---------------------------------------------------------------

def _cases(t: Versioned) -> list[Case]:
    n = t.name
    harvest_budget = 90.0
    return [
        Case("versions", f"{n}_current", "learn_technology", kind="harvest",
             args={"name": n, "max_pages": PAGES}, check=check_current(t),
             writes=True, skip_if=local_only, budget=harvest_budget, timeout=900.0,
             note=f"{t.note}; the label must be a release or a date, never the pinned one"),
        Case("versions", f"{n}_pinned", "learn_technology", kind="harvest",
             args={"name": n, "version": t.pinned, "max_pages": PAGES},
             check=check_pinned(t), writes=True, skip_if=local_only,
             budget=harvest_budget, timeout=900.0,
             note=f"version={t.pinned!r}: stored under that label, and confirmed by the pages, not just repeated back"),
        Case("versions", f"{n}_two_versions_listed", "list_knowledge_base",
             check=check_listed(t), writes=True, skip_if=local_only, budget=5.0),
        Case("versions", f"{n}_pinned_pages_are_{t.pinned}", "read_knowledge_base",
             args=lambda ctx, t=t: {"name": t.name, "version": _needs(ctx, t, "pinned")},
             check=check_pages(t, "pinned"), writes=True, skip_if=local_only, budget=10.0,
             note=f"every page stored under {t.pinned} lives under {t.pinned_path}"),
        Case("versions", f"{n}_current_pages_are_current", "read_knowledge_base",
             args=lambda ctx, t=t: {"name": t.name, "version": _needs(ctx, t, "current")},
             check=check_pages(t, "current"), writes=True, skip_if=local_only, budget=10.0,
             note="no page stored as the current release comes from another release's path"),
        Case("versions", f"{n}_default_is_{t.newest}", "learn_technology",
             args={"name": n}, check=check_default(t), writes=True, skip_if=local_only,
             budget=15.0,
             note=f"with both stored, a versionless call names the {t.newest} release and fetches nothing"),
        Case("versions", f"{n}_search_scoped_to_{t.pinned}", "search_knowledge_base",
             args={"query": t.query, "technology": n, "version": t.pinned, "limit": 10},
             check=check_scoped_search(t), writes=True, skip_if=local_only, budget=10.0),
        Case("versions", f"{n}_search_names_the_version", "search_knowledge_base",
             args={"query": t.query, "technology": n, "limit": 10},
             check=check_search_labels(t), writes=True, skip_if=local_only, budget=10.0,
             note="unscoped: every passage says which of the two releases it came from"),
        Case("versions", f"{n}_unknown_version_names_both", "read_knowledge_base",
             args={"name": n, "version": "0.0.0-zz"}, check=check_unknown_version(t),
             writes=True, skip_if=local_only, budget=5.0),
    ]


def check_forgot_both(r: Result, ctx: Context) -> str | None:
    """Both releases went, and the report counted them."""
    why = check_forgotten(r, ctx)
    if why:
        return why
    if r.ok and "2 version(s)" not in r.text:
        return f"two versions were stored but the report says: {r.text.splitlines()[0][:120]!r}"
    return None


def _cleanup(t: Versioned) -> Case:
    return Case("cleanup", f"forget_{t.name}", "forget_documentation",
                args={"name": t.name}, check=check_forgot_both, writes=True,
                skip_if=delete_only, budget=5.0,
                note="removes both releases and says it removed two; a rerun then measures the harvests again")


CASES: list[Case] = [c for t in TECHNOLOGIES for c in _cases(t)] + [_cleanup(t) for t in TECHNOLOGIES]
