"""What the live benchmarks ask, and what a correct answer looks like.

Every case names a tool call (or one of a few transport-level probes), the
arguments, and a `check` that reads the answer and says what is wrong with
it -- or nothing, when it is right. The runner adds the timing. A case is
one observation about the deployment; the list is the benchmark.

Three flags shape how a case is run and read:

* `known` names a gap already on record (`System Files/Issues.md`, by id). A known case that fails is reported as KNOWN, not as a
  regression; one that passes is reported as FIXED, which is the signal that
  the record needs updating.
* `writes` marks a case that changes the knowledge base. These run only with
  `--allow-writes`, because the store behind the hosted server is the real
  one, shared by everything that connects to it.
* `budget` is a latency expectation in seconds. Exceeding it is noted in the
  report as SLOW, never counted as a failure: a benchmark that fails on a slow
  network would be measuring the network.

The URLs below were chosen for being stable, public, and small, and for each
being a different *kind* of source. What each one is expected to be was
checked by hand on 2026-09-19 (svelte.dev publishes both an llms.txt index
and a 1.19 MB llms-full.txt; docs.python.org publishes neither; the petstore
spec is OpenAPI 3.0 JSON; httpbin.org answers `redirect-to` with a 302).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .live import Result

Check = Callable[[Result, "Context"], "str | None"]


@dataclass
class Context:
    """What earlier cases learned, for later ones to use.

    `techs` is the knowledge base as `list_knowledge_base` reported it, so the
    store cases can ask about whatever is actually stored rather than a name
    hard-wired here that may have been deleted since.
    """
    url: str = ""
    token: str = ""
    build: str = ""
    server: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    techs: list[dict] = field(default_factory=list)
    memo: dict = field(default_factory=dict)
    stale: bool = False          # a write happened since `techs` was read

    @property
    def local(self) -> bool:
        """Is the server on this machine? Then its disk is the caller's disk,
        it runs open on loopback, and a harvest is never cut short."""
        host = re.sub(r"^https?://", "", self.url).split("/")[0].split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    @property
    def largest_tech(self) -> dict | None:
        """The stored technology with the most pages: the best read/search subject."""
        if not self.techs:
            return None
        return max(self.techs, key=lambda t: (t["pages"], t["chars"]))

    def tech(self, name: str) -> dict | None:
        return next((t for t in self.techs if t["name"] == name), None)


@dataclass
class Case:
    suite: str
    name: str
    tool: str = ""
    kind: str = "tool"           # tool | health | unauthorized | initialize | tools | surface | build | concurrent
    args: Any = field(default_factory=dict)   # dict, or Context -> dict
    check: Check = lambda r, c: None
    known: str = ""
    writes: bool = False
    budget: float | None = None
    timeout: float | None = None
    n: int = 1                   # kind="concurrent": how many clients at once
    note: str = ""
    skip_if: Any = None          # Context -> reason to skip, or None to run

    def arguments(self, ctx: Context) -> dict:
        return self.args(ctx) if callable(self.args) else dict(self.args)


def hosted_only(ctx: Context) -> str | None:
    return "only meaningful against a hosted server" if ctx.local else None


def local_only(ctx: Context) -> str | None:
    return "only meaningful against a local server" if not ctx.local else None


def gated_only(ctx: Context) -> str | None:
    return None if ctx.token else "the server runs open on loopback"


def delete_only(ctx: Context) -> str | None:
    return None if ctx.memo.get("delete_enabled") else "forget_documentation is not enabled"


# -- reading answers ---------------------------------------------------------

LISTING = re.compile(
    r"^- \*\*(?P<name>[^*]+)\*\* [—-] (?P<pages>[\d,]+) pages across "
    r"(?P<nv>\d+) versions?, (?P<chars>[\d,]+) chars(?P<flags>.*)$")
VERSIONS = re.compile(r"^\s+versions: (?P<labels>.+)$")
BUILD = re.compile(r"DocsForge build `([0-9a-f]{4,40}|local)`")


def parse_listing(text: str) -> list[dict]:
    techs: list[dict] = []
    for line in text.splitlines():
        m = LISTING.match(line.strip("\r"))
        if m:
            techs.append({"name": m["name"], "pages": int(m["pages"].replace(",", "")),
                          "versions": int(m["nv"]), "chars": int(m["chars"].replace(",", "")),
                          "flags": m["flags"].strip(), "labels": ""})
            continue
        v = VERSIONS.match(line)
        if v and techs:
            techs[-1]["labels"] = v["labels"]
    return techs


def best_of(text: str) -> str:
    """The URL `find_docs` chose, or ''."""
    m = re.search(r"^Best: (\S+)", text, re.M)
    return m.group(1) if m else ""


def _has(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


# -- checks ------------------------------------------------------------------

class Unavailable(Exception):
    """The case could not be judged: something it depends on, other than
    DocsForge, did not answer. Reported as ERROR, never as FAIL."""


#: A tool error that is the network's, not DocsForge's: the site reset the
#: connection, the name did not resolve, the socket timed out. Judged as
#: ERROR (could not be measured), never as FAIL.
NETWORK_FAILURE = re.compile(
    r"Request failed for \S+: .*?(Connection aborted|ConnectionResetError|"
    r"Max retries exceeded|getaddrinfo failed|Name or service not known|timed out)")


def ok(*must: str, chars: int = 0, none_of: tuple[str, ...] = ()) -> Check:
    """The tool answered normally, said each of `must`, and none of `none_of`."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            if NETWORK_FAILURE.search(r.text):
                raise Unavailable(f"network: {r.text[:160]!r}")
            return f"tool error: {r.text[:160]!r}"
        for needle in must:
            if not _has(r.text, needle):
                return f"answer lacks {needle!r}: {r.text[:160]!r}"
        for needle in none_of:
            if _has(r.text, needle):
                return f"answer contains {needle!r}, which it must not"
        if chars and len(r.text) < chars:
            return f"only {len(r.text):,} chars, expected at least {chars:,}"
        return None
    return check


def refused(*any_of: str, none_of: tuple[str, ...] = (), via: str = "") -> Check:
    """The tool refused (an `isError` result) and said why in one of these ways.

    `via` names a third-party host the case routes through; a 5xx from it
    means the case could not be judged, which is not DocsForge's failure.
    """
    def check(r: Result, ctx: Context) -> str | None:
        if r.ok:
            return f"was not refused; answered {len(r.text):,} chars: {r.text[:120]!r}"
        if via and re.search(rf"HTTP 5\d\d for https?://{re.escape(via)}", r.text):
            raise Unavailable(f"{via} answered 5xx; the redirect never happened")
        if any_of and not any(_has(r.text, s) for s in any_of):
            return f"refused for an unexpected reason: {r.text[:160]!r}"
        for needle in none_of:
            if _has(r.text, needle):
                return f"the refusal leaks {needle!r}: {r.text[:160]!r}"
        return None
    return check


def exact(value: str) -> Check:
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        if r.text.strip() != value:
            return f"expected {value!r}, got {r.text.strip()[:120]!r}"
        return None
    return check


def starts(value: str, *must: str) -> Check:
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        if not r.text.strip().startswith(value):
            return f"expected to start with {value!r}, got {r.text.strip()[:120]!r}"
        for needle in must:
            if not _has(r.text, needle):
                return f"answer lacks {needle!r}: {r.text[:160]!r}"
        return None
    return check


def resolves_to(host: str, ecosystem: str = "", verified: bool = True) -> Check:
    """`find_docs` picked a URL on `host`, and (optionally) said which registry won."""
    def check(r: Result, ctx: Context) -> str | None:
        if not r.ok:
            return f"tool error: {r.text[:160]!r}"
        best = best_of(r.text)
        if not best:
            return f"no Best: line -- {r.text.splitlines()[-1][:160]!r}"
        if host not in best:
            return f"best is {best}, expected {host}"
        if ecosystem:
            head = r.text.splitlines()[0]
            if f"({ecosystem})" not in head:
                return f"header says {head!r}, expected ({ecosystem})"
        if verified:
            # The winning URL's own evidence line must say it was confirmed:
            # the mark is the first token of the line under the URL, and
            # "unverified" contains "verified", so compare the whole token.
            lines = r.text.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == f"- {best}" and i + 1 < len(lines):
                    mark = lines[i + 1].strip().split(" · ", 1)[0]
                    if mark != "verified":
                        return f"best {best} is {mark!r}, not verified"
                    break
        return None
    return check


def all_of(*checks: Check) -> Check:
    def check(r: Result, ctx: Context) -> str | None:
        for c in checks:
            why = c(r, ctx)
            if why:
                return why
        return None
    return check


# -- transport ---------------------------------------------------------------

def check_health(r: Result, ctx: Context) -> str | None:
    body = r.meta.get("json") or {}
    if r.meta.get("status") != 200:
        return f"HTTP {r.meta.get('status')}: {r.text[:120]!r}"
    if body.get("status") != "ok":
        return f"status is {body.get('status')!r}, degraded={body.get('degraded')!r}"
    if body.get("store") != "postgres":
        return f"store is {body.get('store')!r}; a hosted instance must be on postgres"
    ctx.memo["health"] = body
    return None


def check_unauthorized(r: Result, ctx: Context) -> str | None:
    if r.meta.get("status") != 401:
        return f"HTTP {r.meta.get('status')}, expected 401"
    challenge = r.meta.get("www_authenticate", "")
    if not challenge.lower().startswith("bearer"):
        return f"WWW-Authenticate is {challenge!r}, expected a Bearer challenge"
    return None


def check_initialize(r: Result, ctx: Context) -> str | None:
    if r.meta.get("name") != "docsforge":
        return f"server calls itself {r.meta.get('name')!r}"
    return None


#: The surface `forge_tools.TOOLS` defines. `forget_documentation` is added
#: only when DOCSFORGE_ALLOW_DELETE is on, and a hosted instance offering it
#: is worth knowing about -- see check_tools.
EXPECTED_TOOLS = {
    "detect_source_type", "fetch_docs", "save_docs", "harvest_docs",
    "learn_technology", "harvest_status", "find_docs", "search_knowledge_base",
    "scan_project", "list_knowledge_base", "read_knowledge_base",
    "forget_resolution", "forget_selection",
}


def check_tools(r: Result, ctx: Context) -> str | None:
    names = {t["name"] for t in r.meta.get("tools", [])}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS - {"forget_documentation"}
    if missing:
        return f"missing tools: {sorted(missing)}"
    if extra:
        return f"unexpected tools: {sorted(extra)}"
    if "forget_documentation" in names:
        ctx.memo["delete_enabled"] = True
    for tool in r.meta.get("tools", []):
        if not tool["description"].strip():
            return f"{tool['name']} has no description"
    return None


def check_surface(r: Result, ctx: Context) -> str | None:
    """Filled in by the runner: the live schemas against this checkout's."""
    drift = r.meta.get("drift") or []
    if r.meta.get("skipped"):
        return None
    if drift:
        return "; ".join(drift[:4]) + (" ..." if len(drift) > 4 else "")
    return None


def check_build(r: Result, ctx: Context) -> str | None:
    if not ctx.build:
        return "list_knowledge_base did not name a build"
    if r.meta.get("in_history") is False:
        return f"live build {ctx.build} is not a commit this checkout knows"
    return None


def check_listing(r: Result, ctx: Context) -> str | None:
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    m = BUILD.search(r.text)
    if m:
        ctx.build = m.group(1)
    if "stored in postgres" not in r.text and "nothing is stored yet" not in r.text:
        return f"unexpected listing shape: {r.text[:160]!r}"
    ctx.techs = parse_listing(r.text)
    if "technolog" in r.text and "stored in" in r.text and not ctx.techs:
        return "listing names technologies but none could be parsed"
    if "Storage:" not in r.text and ctx.techs:
        return "listing has no capacity note"
    return None


# -- the store: arguments that depend on what is stored ----------------------

def _needs_tech(ctx: Context) -> dict:
    tech = ctx.largest_tech
    if tech is None:
        raise LookupError("nothing is stored; run list_knowledge_base first")
    return tech


def args_read(ctx: Context) -> dict:
    return {"name": _needs_tech(ctx)["name"]}


def args_read_section(ctx: Context) -> dict:
    """A section phrase the corpus is known to contain: a word from the first
    page title `read_whole` returned, else the technology's own name."""
    tech = _needs_tech(ctx)
    word = ctx.memo.get("section_word") or tech["name"].split("-")[0]
    return {"name": tech["name"], "section": word}


def args_read_bad_version(ctx: Context) -> dict:
    return {"name": _needs_tech(ctx)["name"], "version": "no-such-version-zz"}


def args_search(ctx: Context) -> dict:
    tech = _needs_tech(ctx)
    return {"query": tech["name"].split("-")[0], "technology": tech["name"]}


def args_search_api(ctx: Context) -> dict:
    return dict(args_search(ctx), kind="api")


def args_search_one(ctx: Context) -> dict:
    return dict(args_search(ctx), limit=1)


def args_search_all(ctx: Context) -> dict:
    return {"query": _needs_tech(ctx)["name"].split("-")[0]}


def check_read(r: Result, ctx: Context) -> str | None:
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    tech = ctx.largest_tech or {}
    if len(r.text) < 500:
        return f"only {len(r.text):,} chars back for {tech.get('name')}"
    # A read bigger than the cap must say what it dropped, not just stop.
    if tech.get("chars", 0) > 200_000 and "showing the first" not in r.text \
            and "truncated:" not in r.text:
        return "the read was cut without disclosing what was omitted"
    # A word the corpus is known to contain, for `read_section` to ask for.
    for line in r.text.splitlines():
        if line.startswith("#"):
            words = [w for w in re.findall(r"[A-Za-z]{4,}", line) if w.lower() != "docsforge"]
            if words:
                ctx.memo["section_word"] = words[0]
                break
    return None


def check_search(r: Result, ctx: Context) -> str | None:
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    if re.match(r"^\d+ passage\(s\)", r.text):
        if "http" not in r.text:
            return "passages carry no source URL"
        if "ranked search" not in r.text:
            return "search on postgres should say it is ranked"
        return None
    if r.text.startswith(("No passage", "Nothing stored")):
        return None   # an honest miss is a correct answer
    return f"unexpected shape: {r.text[:160]!r}"


def check_search_one(r: Result, ctx: Context) -> str | None:
    why = check_search(r, ctx)
    if why:
        return why
    if r.text.startswith("1 passage(s)") or r.text.startswith(("No passage", "Nothing stored")):
        return None
    return f"limit=1 returned {r.text.split(' ', 1)[0]} passages"


def check_search_miss(r: Result, ctx: Context) -> str | None:
    """A query no page fully contains is either a clean miss or a disclosed
    partial match -- never a ranked answer presented as the whole thing."""
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    if r.text.startswith(("No passage", "Nothing stored")):
        return None
    if re.match(r"^\d+ passage\(s\)", r.text):
        head = r.text.splitlines()[0]
        if "none of them contains" in head and "'zzqx'" in head:
            return None
        return f"ranked passages pose as an answer: {head[:160]!r}"
    return f"unexpected shape: {r.text[:160]!r}"


#: Harvested whole by the offline suite. Small enough to finish in minutes,
#: real enough to prove three things at once: it has no sitemap, so the
#: Sphinx manifest and the crawl do the work; its pages are relative links
#: off `/en/stable/`, the shape the crawl used to resolve wrongly; and its
#: name also exists on npm, so the version label shows which registry the
#: resolver believed. Read the Docs rate-limits an address that harvests it
#: three times in an hour (HTTP 429), so a run that must not wait for that
#: to lift can name another technology: DOCSFORGE_OFFLINE_WHOLE=attrs. The
#: click-specific assertions (release 8.x, a quickstart page) then stand
#: down and only the general ones -- whole, not cut short -- are made.
WHOLE = (os.environ.get("DOCSFORGE_OFFLINE_WHOLE") or "click").strip()
CLICK = WHOLE == "click"


def check_learn(r: Result, ctx: Context) -> str | None:
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    # The id is what the tool tells a model to poll with, and it is what the
    # benchmark polls with. Polling by *name* once matched two records -- a
    # finished harvest from an earlier run and the live one -- and the
    # two-record summary carries no per-harvest verdict, so the poll spun
    # for fifteen minutes until the old record aged out (2026-09-20).
    m = re.search(r"[Hh]arvest id `([^`]+)`", r.text)
    ctx.memo["harvest_id"] = m.group(1) if m else ""
    if m or "Harvested" in r.text or "already" in r.text:
        return None
    return f"unexpected shape: {r.text[:160]!r}"


def args_poll(ctx: Context) -> dict:
    return {"harvest": ctx.memo.get("harvest_id") or WHOLE, "wait": 25}


SETTLED = ("**done**", "**failed**", "**stopped reporting**", "already stored",
           "No harvest called", "Nothing is stored")


def settled(text: str) -> bool:
    """Has harvest_status stopped saying 'running'?"""
    return any(s in text for s in SETTLED) or "running" not in text


def check_settled(r: Result, ctx: Context) -> str | None:
    if not r.ok:
        return f"tool error: {r.text[:160]!r}"
    if "**failed**" in r.text or "**stopped reporting**" in r.text:
        return f"the harvest did not finish: {r.text[:200]!r}"
    if "**done**" in r.text or "already stored" in r.text:
        ctx.memo["harvest_report"] = r.text
        ctx.stale = True
        return None
    if "**running**" in r.text:
        return "still running when the poll gave up"
    return f"unexpected shape: {r.text[:160]!r}"


def check_whole(r: Result, ctx: Context) -> str | None:
    why = check_listing(r, ctx)
    if why:
        return why
    ctx.stale = False
    tech = ctx.tech(WHOLE)
    if tech is None:
        return f"{WHOLE} is not in the listing"
    report = ctx.memo.get("harvest_report", "")
    if "page limit" in report or "still queued" in report:
        return f"a limit cut the harvest short: {report[report.find('INCOMPLETE'):][:160]!r}"
    if "INCOMPLETE" in tech["flags"] and "not extractable" not in report:
        # Honest when the shortfall is index pages nothing could be read
        # from (click lists genindex and search); a defect otherwise.
        return f"{WHOLE} is flagged INCOMPLETE for a reason other than unextractable pages"
    if CLICK and tech["pages"] < 30:
        return f"{WHOLE} has only {tech['pages']} pages; the site links about 40"
    if CLICK and not tech["labels"].startswith("8."):
        return f"{WHOLE} is filed under {tech['labels'].split(' ')[0]!r}, not pypi's 8.x release"
    if tech["pages"] < 2:
        return f"{WHOLE} has only {tech['pages']} page(s); a whole technology is more than that"
    return None


def check_forgotten(r: Result, ctx: Context) -> str | None:
    if r.ok or "Nothing stored" in r.text:
        ctx.stale = True
        return None
    return f"tool error: {r.text[:160]!r}"


def check_status_unknown(r: Result, ctx: Context) -> str | None:
    why = ok("No harvest called")(r, ctx)
    if why:
        return why
    hosted = not ctx.local
    if hosted and "does not guarantee" not in r.text:
        return "a serverless host did not disclose that a background harvest is not guaranteed"
    if not hosted and "does not guarantee" in r.text:
        return "a long-lived local server claims the serverless limitation"
    return None


def check_concurrent(r: Result, ctx: Context) -> str | None:
    results = r.meta.get("results", [])
    bad = [x for x in results if x.get("error") or not x.get("ok")]
    if bad:
        first = bad[0]
        return (f"{len(bad)} of {len(results)} calls failed; first: "
                f"{(first.get('error') or first.get('text', ''))[:160]!r}")
    return None


# -- the list ----------------------------------------------------------------

PETSTORE = "https://petstore3.swagger.io/api/v3/openapi.json"
SVELTE_LLMS = "https://svelte.dev/llms.txt"
SVELTE_PAGE = "https://svelte.dev/docs/svelte/overview"
CLICK_README = "https://raw.githubusercontent.com/pallets/click/main/README.md"
CLICK_REPO = "https://github.com/pallets/click"
CLICK_DOCS = "https://click.palletsprojects.com/en/stable/"
FASTAPI_SITEMAP = "https://fastapi.tiangolo.com/sitemap.xml"
PY_ASYNCIO = "https://docs.python.org/3/library/asyncio.html"
PY_MISSING = "https://docs.python.org/3/library/this-page-does-not-exist.html"
REDIRECT_TO_LOOPBACK = "https://httpbin.org/redirect-to?url=http%3A%2F%2F127.0.0.1%2F"
REDIRECT_TO_METADATA = ("https://httpbin.org/redirect-to?url="
                        "http%3A%2F%2F169.254.169.254%2Flatest%2Fmeta-data%2F")

PRIVATE = ("private/loopback", "private", "loopback")

CASES: list[Case] = [
    # -- transport: is the deployment there, gated, and this code? ---------
    Case("transport", "health", kind="health", check=check_health, budget=2.0,
         note="/health answers 200, ok, on postgres, not degraded"),
    Case("transport", "mcp_needs_token", kind="unauthorized", args={"token": ""},
         check=check_unauthorized, budget=2.0, skip_if=gated_only,
         note="/mcp without a bearer is a 401 with a Bearer challenge"),
    Case("transport", "mcp_rejects_wrong_token", kind="unauthorized",
         args={"token": "not-the-token-" + "0" * 50}, check=check_unauthorized, budget=2.0,
         skip_if=gated_only),
    Case("transport", "initialize", kind="initialize", check=check_initialize, budget=3.0,
         note="a fresh session: connect + initialize"),
    Case("transport", "tools_list", kind="tools", check=check_tools, budget=3.0,
         note="all thirteen tools, each with a description"),
    Case("transport", "surface_matches_checkout", kind="surface", check=check_surface,
         note="live tool names and parameters against forge_tools.TOOLS here"),
    Case("transport", "listing", tool="list_knowledge_base", check=check_listing, budget=5.0,
         note="also learns the build sha and what is stored, for later cases"),
    Case("transport", "build_is_known", kind="build", check=check_build,
         note="the sha the live build reports is a commit in this repository"),

    # -- detect_source_type: one probe, right kind ------------------------
    Case("detect", "openapi_json", "detect_source_type", args={"url": PETSTORE},
         check=exact("openapi"), budget=5.0),
    Case("detect", "github_repo", "detect_source_type", args={"url": CLICK_REPO},
         check=exact("github"), budget=3.0),
    Case("detect", "raw_markdown", "detect_source_type", args={"url": CLICK_README},
         check=exact("raw_text"), budget=3.0),
    Case("detect", "sitemap", "detect_source_type", args={"url": FASTAPI_SITEMAP},
         check=exact("sitemap"), budget=3.0),
    Case("detect", "llms_index_prefers_full_dump", "detect_source_type",
         args={"url": SVELTE_LLMS}, check=starts("llms_txt", "llms-full.txt"), budget=8.0,
         note="an llms.txt index beside an llms-full.txt is resolved to the full dump"),
    Case("detect", "deep_page_finds_site_dump", "detect_source_type",
         args={"url": SVELTE_PAGE}, check=starts("llms_txt", "svelte.dev/llms-full.txt"),
         budget=8.0, note="a docs page on a site that publishes llms-full.txt is that dump"),
    Case("detect", "html_page", "detect_source_type", args={"url": PY_ASYNCIO},
         check=exact("html"), budget=8.0,
         note="docs.python.org publishes no llms.txt; both probes 404 first"),
    Case("detect", "empty_url_is_an_error", "detect_source_type", args={"url": ""},
         check=refused("URL", "http"), known="Issues.md D1",
         note="today '' is classified html rather than refused"),
    Case("detect", "garbage_is_an_error", "detect_source_type", args={"url": "not a url"},
         check=refused("URL", "http"), known="Issues.md D1"),

    # -- fetch_docs: extraction, per source kind --------------------------
    Case("fetch", "openapi_renders_endpoints", "fetch_docs", args={"url": PETSTORE},
         check=ok("/pet/{petId}", "findByStatus", "GET", chars=5_000), budget=10.0),
    Case("fetch", "force_raw_skips_rendering", "fetch_docs",
         args={"url": PETSTORE, "force": "raw_text"},
         check=ok('"openapi"', chars=5_000, none_of=("| Parameter",)), budget=10.0,
         note="force=raw_text hands back the JSON, not endpoint tables"),
    Case("fetch", "llms_full_dump_is_capped_and_says_so", "fetch_docs",
         args={"url": SVELTE_LLMS},
         check=ok("svelte", "truncated:", "more characters omitted", chars=150_000),
         budget=20.0, note="1.19 MB dump; the 200k cap must disclose the cut"),
    Case("fetch", "raw_markdown_passthrough", "fetch_docs", args={"url": CLICK_README},
         check=ok("click", chars=800), budget=5.0),
    Case("fetch", "github_readme_and_docs", "fetch_docs", args={"url": CLICK_REPO},
         check=ok("click", chars=800), budget=15.0,
         note="GitHub API, unauthenticated: rate limits on a shared egress show here"),
    Case("fetch", "html_page_extracted", "fetch_docs", args={"url": PY_ASYNCIO},
         check=ok("asyncio", chars=1_000), budget=10.0),
    Case("fetch", "crawl_three_pages", "fetch_docs",
         args={"url": CLICK_DOCS, "crawl": True, "max_pages": 3},
         check=ok("# Extracted 3 documents", chars=5_000), budget=30.0),
    Case("fetch", "sitemap_bounded", "fetch_docs",
         args={"url": FASTAPI_SITEMAP, "max_pages": 2},
         check=ok("# Extracted 2 documents", chars=2_000), budget=30.0),
    Case("fetch", "bad_url_is_an_error", "fetch_docs", args={"url": "not-a-url"},
         check=refused("http", "URL"), budget=3.0),
    Case("fetch", "missing_page_is_an_error", "fetch_docs", args={"url": PY_MISSING},
         check=refused("404", "not found"), budget=8.0),

    # -- the SSRF and path guards, live ----------------------------------
    Case("guard", "loopback", "fetch_docs", args={"url": "http://127.0.0.1:8765/"},
         check=refused(*PRIVATE), budget=3.0),
    Case("guard", "cloud_metadata", "fetch_docs",
         args={"url": "http://169.254.169.254/latest/meta-data/"},
         check=refused(*PRIVATE), budget=3.0),
    Case("guard", "decimal_ip", "fetch_docs", args={"url": "http://2130706433/"},
         check=refused(*PRIVATE), budget=3.0),
    Case("guard", "octal_ip", "fetch_docs", args={"url": "http://0177.0.0.1/"},
         check=refused(*PRIVATE), budget=3.0),
    Case("guard", "ipv6_loopback", "fetch_docs", args={"url": "http://[::1]/"},
         check=refused(*PRIVATE), budget=3.0),
    Case("guard", "file_scheme", "fetch_docs", args={"url": "file:///etc/passwd"},
         check=refused("http"), budget=3.0),
    Case("guard", "dns_to_loopback", "fetch_docs", args={"url": "http://127.0.0.1.nip.io/"},
         check=refused(*PRIVATE), budget=3.0,
         note="a public hostname that resolves to 127.0.0.1 is judged by its address"),
    Case("guard", "redirect_to_loopback", "fetch_docs", args={"url": REDIRECT_TO_LOOPBACK},
         check=refused(*PRIVATE, via="httpbin.org"), budget=8.0,
         note="Issues.md X1: a 302 to 127.0.0.1 must be refused at the hop"),
    Case("guard", "redirect_to_metadata", "fetch_docs", args={"url": REDIRECT_TO_METADATA},
         check=refused(*PRIVATE, via="httpbin.org"), budget=8.0),
    Case("guard", "save_outside_root", "save_docs",
         args={"url": PETSTORE, "out_dir": "../../../../tmp/benchmark-traversal"},
         check=refused("Refusing to write outside"), budget=3.0),
    Case("guard", "save_inside_root", "save_docs",
         args={"url": PETSTORE, "out_dir": "benchmark-scratch"},
         check=ok(".md"), budget=10.0,
         note="writes to the server's own scratch (/tmp on Vercel), not the store"),

    # -- find_docs: name -> official documentation, nothing harvested -----
    Case("resolve", "fastapi", "find_docs", args={"name": "fastapi"},
         check=resolves_to("fastapi.tiangolo.com"), budget=30.0),
    Case("resolve", "pydantic", "find_docs", args={"name": "pydantic"},
         check=resolves_to("pydantic.dev"), budget=30.0,
         note="pydantic.dev/docs/validation/latest since 2026; docs.pydantic.dev redirects there"),
    Case("resolve", "click_is_pypi_not_npm", "find_docs", args={"name": "click"},
         check=resolves_to("click.palletsprojects.com", "pypi"), budget=30.0,
         note="Issues.md R6: npm also has a 'click'; the pypi one must win and be labelled so"),
    Case("resolve", "requests_is_pypi", "find_docs", args={"name": "requests"},
         check=resolves_to("requests.readthedocs.io", "pypi"), budget=30.0),
    Case("resolve", "tokio_is_crates", "find_docs", args={"name": "tokio"},
         check=resolves_to("tokio.rs", "crates"), budget=30.0),
    Case("resolve", "svelte", "find_docs", args={"name": "svelte"},
         check=resolves_to("svelte.dev"), budget=30.0,
         note="found by its own domain before any registry is asked, so no ecosystem is claimed"),
    Case("resolve", "django", "find_docs", args={"name": "django"},
         check=resolves_to("docs.djangoproject.com"), budget=30.0),
    Case("resolve", "scoped_npm_name", "find_docs", args={"name": "@tanstack/react-query"},
         check=resolves_to("tanstack.com", "npm"), budget=30.0,
         note="the site says 'TanStack Query', never the scoped name; identified on "
              "`scope-domain`, npm's nomination on the scope's own domain (Issues.md R7)"),
    Case("resolve", "squatted_name_flask", "find_docs", args={"name": "flask"},
         check=resolves_to("flask.palletsprojects.com", "pypi"), budget=45.0,
         note="flask.io owns the name and says it, and nothing else; it must not "
              "pre-empt PyPI's nomination (Issues.md R10)"),
    Case("resolve", "squatted_name_polars", "find_docs", args={"name": "polars"},
         check=resolves_to("pola.rs"), budget=45.0,
         note="a third-party site once won on own-domain plus mentions (Issues.md R10)"),
    Case("resolve", "ecosystem_pin", "find_docs", args={"name": "click", "ecosystem": "pypi"},
         check=resolves_to("click.palletsprojects.com", "pypi"), budget=30.0),
    Case("resolve", "unknown_name_is_honest", "find_docs",
         args={"name": "zzqx-no-such-package-9182"},
         check=lambda r, c: (None if (not best_of(r.text)) else
                             f"invented {best_of(r.text)} for a name that does not exist"),
         budget=30.0, note="no Best: for a name no registry has"),

    # -- the store, read-only --------------------------------------------
    Case("store", "read_whole", "read_knowledge_base", args=args_read, check=check_read,
         budget=10.0, note="the largest stored technology, default version"),
    Case("store", "read_section", "read_knowledge_base", args=args_read_section,
         check=ok(chars=200), budget=10.0),
    Case("store", "read_unknown_name", "read_knowledge_base",
         args={"name": "zzqx-not-stored-9182"},
         check=refused("No stored documentation", "Available"), budget=5.0),
    Case("store", "read_unknown_version", "read_knowledge_base",
         args=args_read_bad_version, check=refused("has no version"), budget=5.0),
    Case("store", "search_scoped", "search_knowledge_base", args=args_search,
         check=check_search, budget=10.0),
    Case("store", "search_everything", "search_knowledge_base", args=args_search_all,
         check=check_search, budget=10.0),
    Case("store", "search_kind_api", "search_knowledge_base", args=args_search_api,
         check=check_search, budget=10.0),
    Case("store", "search_limit_one", "search_knowledge_base", args=args_search_one,
         check=check_search_one, budget=10.0),
    Case("store", "search_miss_is_honest", "search_knowledge_base",
         args={"query": "zzqx-token-that-no-page-contains-9182"},
         check=check_search_miss, budget=10.0,
         note="no page has 'zzqx'; passages for the other words must say so, not pose as the answer"),
    Case("store", "harvest_status_idle", "harvest_status", args={},
         check=ok("harvest"), budget=5.0),
    Case("store", "harvest_status_unknown_id", "harvest_status",
         args={"harvest": "zzqx-no-such-harvest-9182"},
         check=check_status_unknown, budget=5.0,
         note="a serverless host must say it cannot promise a background harvest; a local one must not"),
    Case("store", "forget_resolution_unknown", "forget_resolution",
         args={"name": "zzqx-never-resolved-9182"}, check=ok("Nothing remembered"), budget=3.0),
    Case("store", "forget_selection_unknown", "forget_selection",
         args={"name": "zzqx-never-selected-9182"}, check=ok(), budget=3.0),
    Case("store", "scan_project_remote_dot", "scan_project", args={"path": "."},
         check=refused("cannot see", "remote", "client"), known="Issues.md H2",
         skip_if=hosted_only,
         note="over a hosted connection '.' is the server's own tree; today it is scanned and reported as yours"),
    Case("store", "scan_project_foreign_path", "scan_project",
         args={"path": "C:\\Users\\nobody\\project"},
         check=refused(none_of=("/var/task", "/tmp/")), known="Issues.md H2",
         skip_if=hosted_only,
         note="an absolute client path; the refusal must not leak the server's working directory"),

    # -- many clients at once ----------------------------------------------
    Case("concurrency", "listing_x6", "list_knowledge_base", kind="concurrent", n=6,
         check=check_concurrent, budget=15.0,
         note="six sessions listing at once: one connection each against a 15-backend plan"),
    Case("concurrency", "search_x6", "search_knowledge_base", kind="concurrent", n=6,
         args=args_search, check=check_concurrent, budget=15.0),

    # -- offline: a local server, a local store, and a harvest run to the end --
    Case("offline", "scan_this_repo", "scan_project", args={"path": "."},
         check=ok("pyproject.toml", "docsforge"), skip_if=local_only, budget=10.0,
         note="on a local server '.' is this repository: the caller's disk is the server's"),
    Case("offline", "learn_whole_technology", "learn_technology", args={"name": WHOLE},
         check=check_learn, writes=True, skip_if=local_only, budget=30.0,
         note="no max_pages: the tool hands back a harvest id at the deadline and the crawl runs on"),
    Case("offline", "harvest_runs_to_the_end", "harvest_status", kind="poll",
         args=args_poll, check=check_settled, writes=True,
         skip_if=local_only, timeout=1800.0,
         note="polls harvest_status until the harvest is done; nothing cuts it short"),
    Case("offline", "whole_corpus_stored", "list_knowledge_base", check=check_whole,
         writes=True, skip_if=local_only, budget=5.0,
         note=f"{WHOLE}: every page the site links, no INCOMPLETE flag, and the pypi release as its version"),
    Case("offline", "deep_page_was_reached", "read_knowledge_base",
         args={"name": WHOLE, "section": "quickstart" if CLICK else WHOLE},
         check=ok(chars=1_000), writes=True, skip_if=local_only, budget=5.0,
         note="the quickstart is a relative link from the entry page: the crawl-base fix, live"),
    Case("offline", "search_whole_corpus", "search_knowledge_base",
         args={"query": "click.option decorator" if CLICK else WHOLE, "technology": WHOLE},
         check=check_search, writes=True, skip_if=local_only, budget=5.0),

    # -- writes: only with --allow-writes; these change the shared store --
    Case("writes", "harvest_docs_small", "harvest_docs",
         args={"url": PETSTORE, "name": "benchmark-petstore", "max_pages": 1},
         check=ok("benchmark-petstore"), writes=True, budget=25.0,
         note="stores a one-page corpus named benchmark-petstore; removal needs DOCSFORGE_ALLOW_DELETE"),
    Case("writes", "read_back_harvest", "read_knowledge_base",
         args={"name": "benchmark-petstore"}, check=ok("/pet/{petId}", chars=5_000),
         writes=True, budget=10.0),
    Case("writes", "search_harvest", "search_knowledge_base",
         args={"query": "findByStatus", "technology": "benchmark-petstore"},
         check=check_search, writes=True, budget=10.0),
    Case("writes", "learn_already_stored_fetches_nothing", "learn_technology",
         args=lambda ctx: {"name": _needs_tech(ctx)["name"]},
         check=ok("already stored", "Nothing was fetched"), writes=True, budget=15.0,
         note="learn_technology on a stored name says so and does not crawl"),

    # -- cleanup: only where deletion is enabled, which offline turns on ---
    Case("cleanup", "forget_benchmark_corpus", "forget_documentation",
         args={"name": "benchmark-petstore"}, check=check_forgotten, writes=True,
         skip_if=delete_only, budget=5.0,
         note="removes what harvest_docs_small stored; the whole-technology corpus is kept"),
]

SUITES: list[str] = list(dict.fromkeys(c.suite for c in CASES))
