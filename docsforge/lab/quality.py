"""
What the lab can judge by itself, before a person looks.

`assess` reads a page of stored Markdown for what extraction left behind; it
is the field test's measure (`scripts/fieldtest.py` imports it from here, so
the lab and the field test cannot disagree about what "clean" means).
`matches` is the held-out measure of whether a resolved URL is one of the
documentation locations written down for a name.

These are the *automatic* half of a verdict. The lab shows them beside every
result and asks a tester for the other half; neither replaces the other.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ── quality, read off the Markdown ──────────────────────────────────────────
_FENCE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.M | re.S)
_REL_LINK = re.compile(r"\]\((?!https?://|mailto:|#|data:)([^)\s]+)\)")
_CHROME = re.compile(
    r"^\s*(copy|copied!?|copy code|copy to clipboard|edit this page|edit on github|"
    r"on this page|table of contents|skip to (main )?content|was this (page )?helpful\??|"
    r"previous|next|ask ai|search\.\.\.|toggle (navigation|sidebar|theme)|"
    r"last updated.*|thank you for your feedback.*)\s*$",
    re.I | re.M)
_PERMALINK = re.compile(r"(¶|​|\[#\]\(#|\[​?\]\(#)")
_META = re.compile(r"^<!-- source:[^\n]*-->\s*", re.M)

#: Under this many characters a stored page is "thin" -- a stub, a redirect
#: notice, or a navigation page mistaken for content.
THIN = 400


def assess(markdown: str) -> dict:
    """What a page of stored Markdown says about how it was extracted."""
    body = _META.sub("", markdown or "")
    fences = _FENCE.findall(body)
    collapsed = 0
    for _mark, code in fences:
        lines = [l for l in code.splitlines() if l.strip()]
        # One very long line that plainly holds several statements was several
        # lines in the page: the line breaks lived in markup and were lost.
        if len(lines) == 1 and len(lines[0]) > 120 and re.search(
                r"(;\s*\S|\)\s+\w+\s*[(=]|\}\s+\w|import .* import |\s{4,}\S)", lines[0]):
            collapsed += 1
    return {
        "chars": len(body),
        "fences": len(fences),
        "collapsed_fences": collapsed,
        "relative_links": len(_REL_LINK.findall(body)),
        "chrome_lines": len(_CHROME.findall(body)),
        "permalink_marks": len(_PERMALINK.findall(body)),
        "headings": len(re.findall(r"^#{1,6} ", body, re.M)),
    }


def clean(metrics: dict) -> bool:
    """A page with none of the residue `assess` counts."""
    return not (metrics.get("collapsed_fences") or metrics.get("chrome_lines")
                or metrics.get("permalink_marks") or metrics.get("relative_links"))


def page_flags(metrics: dict) -> list[str]:
    """The residue on one page, in words a tester can scan."""
    flags = []
    if metrics.get("chars", 0) < THIN:
        flags.append("thin")
    if metrics.get("collapsed_fences"):
        flags.append("collapsed code")
    if metrics.get("chrome_lines"):
        flags.append("UI chrome")
    if metrics.get("permalink_marks"):
        flags.append("permalink marks")
    if metrics.get("relative_links"):
        flags.append("relative links")
    return flags


# ── where a name should resolve ─────────────────────────────────────────────
def parse_expected(text) -> list[str]:
    """`"zod.dev, https://docs.x.io/api/"` -> `["zod.dev", "docs.x.io/api"]`."""
    if isinstance(text, (list, tuple)):
        items = [str(t) for t in text]
    else:
        items = re.split(r"[\s,]+", str(text or ""))
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        item = re.sub(r"^[a-z][a-z0-9+.-]*://", "", item, flags=re.I)
        item = item.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if item and item.lower() not in (o.lower() for o in out):
            out.append(item)
    return out


def _plain_match(url: str, want: str) -> bool:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "/").lstrip("/").lower()
    want_host, _, want_path = want.partition("/")
    return host == want_host.lower().removeprefix("www.") and \
        path.startswith(want_path.lower())


def matches(url: str, expected: list[str], follow=None) -> bool:
    """Whether `url` is on one of the `expected` documentation locations.

    `follow(address) -> "host/path"` says where an address lands today, for an
    expectation written before a site moved (`turbo.build` is `turborepo.com`
    now); None skips that and compares the addresses as written.
    """
    if not url or not expected:
        return False
    if any(_plain_match(url, want) for want in expected):
        return True
    if follow is None:
        return False
    for want in expected:
        moved = follow(want)
        if moved and moved != want and _plain_match(url, moved):
            return True
    answer = (urlparse(url).hostname or "") + urlparse(url).path
    moved = follow(answer)
    return bool(moved) and any(_plain_match(moved, want) for want in expected)


def lands_on(address: str) -> str:
    """Where `https://<address>` lands today, as `host/path`, or "".

    Through DocsForge's own fetcher, so an address typed into the lab is held
    to the same guard as any other fetch.
    """
    from docsforge.core.engine import Fetcher, Options
    try:
        with Fetcher(Options(delay=0.0)) as fetcher:
            r = fetcher.get(f"https://{address}")
            if r.status_code >= 400:
                return ""
            p = urlparse(r.url)
            return (p.hostname or "").lower().removeprefix("www.") + p.path.rstrip("/")
    except Exception:                                   # noqa: BLE001 -- unreachable
        return ""


# ── a harvest, judged ───────────────────────────────────────────────────────
def harvest_verdict(summary: dict) -> dict:
    """The held-out bar for a harvest: no error, ten pages or everything the
    site listed, fewer than half of them thin. Plus how clean the pages are."""
    pages = summary.get("pages") or []
    stored = len(pages)
    listed = summary.get("expected") or 0
    thin = sum(1 for p in pages if p.get("chars", 0) < THIN)
    tidy = sum(1 for p in pages if clean(p))
    enough = stored >= 10 or bool(listed and stored >= listed) or summary.get("complete") is True
    ok = bool(stored) and enough and thin < stored / 2 and not summary.get("error")
    reasons = []
    if summary.get("error"):
        reasons.append("the harvest failed")
    if not stored:
        reasons.append("nothing was stored")
    elif not enough:
        reasons.append(f"only {stored} page(s)" + (f" of {listed} listed" if listed else ""))
    if stored and thin >= stored / 2:
        reasons.append(f"{thin} of {stored} pages are thin")
    return {"pass": ok, "pages": stored, "listed": listed, "thin": thin, "clean": tidy,
            "reason": "; ".join(reasons) or "enough pages, mostly substantial"}
