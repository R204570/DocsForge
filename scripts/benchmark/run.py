"""Run the live benchmarks against a DocsForge deployment and write the numbers.

    python -m scripts.benchmark.run --offline            # the whole thing on this machine (the usual run)
    python -m scripts.benchmark.run --offline --publish  # ... and publish it as benchmarks/bench-<next>/
    python -m scripts.benchmark.run                      # every read-only suite against the hosted server
    python -m scripts.benchmark.run --suite guard        # one suite (repeatable)
    python -m scripts.benchmark.run --case redirect      # cases whose name contains this
    python -m scripts.benchmark.run --repeat 3           # median of three, per case
    python -m scripts.benchmark.run --list               # what would run, and nothing else
    python -m scripts.benchmark.run --diff a.json b.json # what changed between two runs

Where the server is and how to authenticate: `--url` / `--token`, else
`DOCSFORGE_MCP_URL` / `DOCSFORGE_MCP_TOKEN`, else the registration in
`~/.claude.json`. The token is never written to the results.

An unpublished run lands in `offline/results/` (ignored by git) as
`<timestamp>.json` and `.md`. `--publish` writes it as `benchmarks/bench-N/`
instead -- `results.json`, and a `README.md` that opens with the table and
ends with an "Issues faced" section listing every case that did not pass,
to be finished by hand with what was found. The exit code is 1 when any
case FAILs or ERRORs; KNOWN gaps and SLOW timings do not fail the run, they
are what the report is for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import cases as C
from .live import CALL_TIMEOUT, Live, Result, fan_out, resolve_target

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
#: Unpublished runs. Under the offline root, which git ignores.
RESULTS = ROOT / "offline" / "results"
#: Published runs, one folder each, numbered in order: the public record.
PUBLISHED = ROOT / "benchmarks"

PASS, FAIL, ERROR, KNOWN, FIXED, SKIP = "PASS", "FAIL", "ERROR", "KNOWN", "FIXED", "SKIP"
BAD = {FAIL, ERROR}


# -- one case ----------------------------------------------------------------

async def perform(case: C.Case, live: Live, ctx: C.Context, allow_writes: bool,
                  timeout: float) -> tuple[Result, str, str]:
    """`(result, verdict, why)` for one case, dispatching on its kind."""
    if case.writes and not allow_writes:
        return Result(), SKIP, "changes the store; pass --allow-writes"
    if case.skip_if is not None:
        reason = case.skip_if(ctx)
        if reason:
            return Result(), SKIP, reason
    # A write since the listing was read means the store cases would be
    # asking about a knowledge base that no longer exists in that shape.
    if ctx.stale and callable(case.args):
        await refresh_listing(live, ctx, timeout)
    try:
        args = case.arguments(ctx)
    except LookupError as e:
        return Result(), SKIP, str(e)

    kind = case.kind
    if kind == "tool":
        r = await live.call(case.tool, args, timeout=case.timeout or timeout)
    elif kind == "poll":
        # The same call again and again until the answer stops saying
        # "running": how a harvest that outlives one tool call is waited for.
        started = time.perf_counter()
        limit = case.timeout or 900.0
        polls = 0
        while True:
            before = time.perf_counter()
            r = await live.call(case.tool, args, timeout=timeout)
            polls += 1
            if r.error or not r.ok or C.settled(r.text):
                break
            if time.perf_counter() - started > limit:
                break
            # The server waits up to `wait` seconds itself; an instant
            # answer means it had nothing to wait on, so pace the next ask.
            if time.perf_counter() - before < 2.0:
                await asyncio.sleep(2.0)
        r.seconds = time.perf_counter() - started
        r.meta["polls"] = polls
    elif kind == "health":
        r = await live.health()
    elif kind == "unauthorized":
        r = await live.unauthorized(token=args.get("token", ""))
    elif kind == "initialize":
        started = time.perf_counter()
        try:
            async with Live(live.url, live.token, timeout=timeout) as fresh:
                r = Result(ok=True, seconds=time.perf_counter() - started,
                           meta=dict(fresh.server))
                ctx.server = dict(fresh.server)
        except Exception as e:  # noqa: BLE001
            r = Result(error=f"{type(e).__name__}: {e}"[:400],
                       seconds=time.perf_counter() - started)
    elif kind == "tools":
        started = time.perf_counter()
        try:
            tools = await live.tools()
            ctx.tools = tools
            r = Result(ok=True, seconds=time.perf_counter() - started,
                       text=", ".join(t["name"] for t in tools), meta={"tools": tools})
        except Exception as e:  # noqa: BLE001
            r = Result(error=f"{type(e).__name__}: {e}"[:400],
                       seconds=time.perf_counter() - started)
    elif kind == "surface":
        r = surface_drift(ctx)
        if r.meta.get("skipped"):
            return r, SKIP, r.meta["skipped"]
    elif kind == "build":
        r = build_lineage(ctx)
        if r.meta.get("skipped"):
            return r, SKIP, r.meta["skipped"]
    elif kind == "concurrent":
        started = time.perf_counter()
        results = await fan_out(live.url, live.token, case.tool, args, case.n,
                                timeout=case.timeout or timeout)
        r = Result(ok=all(x.ok and not x.error for x in results),
                   seconds=time.perf_counter() - started,
                   text=f"{sum(1 for x in results if x.ok)} of {len(results)} ok; "
                        f"per-call max {max(x.seconds for x in results):.1f}s",
                   meta={"results": [{"ok": x.ok, "seconds": round(x.seconds, 3),
                                      "error": x.error, "text": x.text[:200]}
                                     for x in results]})
    else:
        return Result(), ERROR, f"unknown case kind {kind!r}"

    if r.error:
        return r, ERROR, r.error
    try:
        why = case.check(r, ctx)
    except C.Unavailable as e:
        return r, ERROR, str(e)
    if case.writes and not r.error:
        ctx.stale = True
    if why is None:
        return r, (FIXED if case.known else PASS), ""
    return r, (KNOWN if case.known else FAIL), why


async def refresh_listing(live: Live, ctx: C.Context, timeout: float) -> None:
    """Re-read what is stored, quietly, after something changed it."""
    listing = next(c for c in C.CASES if c.name == "listing")
    r = await live.call(listing.tool, {}, timeout=timeout)
    if not r.error:
        listing.check(r, ctx)
    ctx.stale = False


# -- the two cases that look at this checkout ------------------------------

def surface_drift(ctx: C.Context) -> Result:
    """The live tool surface against `forge_tools.TOOLS` in this checkout.

    Run in a subprocess with the store variables set empty -- never popped,
    which would let `.env` refill them -- so importing DocsForge here cannot
    open a database. `DOCSFORGE_ALLOW_DELETE` is cleared too, so the local
    list is the thirteen the live one is compared against.
    """
    if not ctx.tools:
        return Result(meta={"skipped": "tools_list did not run"})
    env = dict(os.environ, DOCSFORGE_DB="", DATABASE_URL="", DOCSFORGE_ALLOW_DELETE="",
               PYTHONIOENCODING="utf-8")
    code = ("import json; from docsforge.tools import forge_tools as f; "
            "print(json.dumps([{'name': t.name, "
            "'properties': sorted((t.schema.get('properties') or {}).keys()), "
            "'required': sorted(t.schema.get('required') or [])} for t in f.TOOLS]))")
    started = time.perf_counter()
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return Result(meta={"skipped": f"could not import this checkout: {e}"})
    if out.returncode != 0:
        return Result(meta={"skipped": "could not import this checkout: "
                                       + out.stderr.strip().splitlines()[-1][:200]})
    local = {t["name"]: t for t in json.loads(out.stdout)}
    live = {t["name"]: {"properties": sorted((t["schema"].get("properties") or {}).keys()),
                        "required": sorted(t["schema"].get("required") or [])}
            for t in ctx.tools if t["name"] != "forget_documentation"}
    drift: list[str] = []
    for name in sorted(set(local) | set(live)):
        if name not in live:
            drift.append(f"{name}: in checkout, not live")
        elif name not in local:
            drift.append(f"{name}: live, not in checkout")
        else:
            for key in ("properties", "required"):
                a, b = local[name][key], live[name][key]
                if a != b:
                    drift.append(f"{name}.{key}: checkout {a} vs live {b}")
    return Result(ok=True, seconds=time.perf_counter() - started,
                  text="no drift" if not drift else "\n".join(drift),
                  meta={"drift": drift, "compared": len(live)})


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def build_lineage(ctx: C.Context) -> Result:
    """Is the sha the live build reports a commit this repository has?"""
    if not ctx.build:
        return Result(ok=True, meta={"in_history": None})
    if ctx.build == "local":
        return Result(ok=True, text="live build is 'local' (no VERCEL_GIT_COMMIT_SHA)",
                      meta={"in_history": None})
    if not _git("rev-parse", "--is-inside-work-tree"):
        return Result(meta={"skipped": "not a git checkout"})
    kind = _git("cat-file", "-t", ctx.build)
    head = _git("rev-parse", "--short=7", "HEAD")
    main = _git("rev-parse", "--short=7", "origin/main")
    where = []
    if kind == "commit":
        subject = _git("log", "-1", "--format=%s", ctx.build)
        where.append(f"live {ctx.build} = {subject!r}")
    where.append(f"HEAD {head}; origin/main {main}")
    return Result(ok=True, text="; ".join(where),
                  meta={"in_history": kind == "commit", "head": head, "main": main})


# -- the run -----------------------------------------------------------------

#: With writes allowed, the suites that fill the store run before the ones
#: that read it, so a fresh offline database has a corpus by the time the
#: store cases ask for the largest one. Cleanup is last whatever else runs.
WRITING_ORDER = ["transport", "detect", "fetch", "guard", "resolve",
                 "offline", "writes", "store", "concurrency", "cleanup"]


def select(suites: list[str], names: list[str], allow_writes: bool) -> list[C.Case]:
    chosen = [c for c in C.CASES
              if (not suites or c.suite in suites)
              and (not names or any(n in c.name for n in names))]
    if not allow_writes and not suites:
        chosen = [c for c in chosen if not c.writes]
    if allow_writes:
        rank = {suite: i for i, suite in enumerate(WRITING_ORDER)}
        chosen.sort(key=lambda c: rank.get(c.suite, len(rank)))   # stable: cases keep their order
    return chosen


def _loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/")[0].split(":")[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


async def run(args) -> int:
    chosen = select(args.suite, args.case, args.allow_writes)
    if args.list:
        for c in chosen:
            flag = " [writes]" if c.writes else (" [known gap]" if c.known else "")
            print(f"{c.suite:12s} {c.name:40s} {c.tool or c.kind}{flag}")
        print(f"\n{len(chosen)} cases")
        return 0

    url, token, source = resolve_target(args.url, args.token)
    if not token and not _loopback(url):
        print("no token: pass --token, set DOCSFORGE_MCP_TOKEN, or `claude mcp add` "
              "the server with an Authorization header", file=sys.stderr)
        return 2
    if not token:
        source = "none needed on loopback"

    # The listing is what tells the store cases what to ask about, and the
    # tool list is what says whether deletion is enabled, so both run first
    # whether or not they were selected -- recorded only when they were.
    prerequisites = [c for c in C.CASES
                     if c.name in ("tools_list", "listing") and c not in chosen]

    stamp = datetime.now(timezone.utc)
    ctx = C.Context(url=url, token=token)
    print(f"DocsForge live benchmarks -> {url}  (token from {source})")
    print(f"{len(chosen)} cases x {args.repeat} run(s); timeout {args.timeout:.0f}s per call\n")

    rows: list[dict] = []
    started = time.perf_counter()
    async with Live(url, token, timeout=args.timeout) as live:
        ctx.server = dict(live.server)
        print(f"connected: {live.server['name']} v{live.server['version']} "
              f"in {live.server['seconds']:.2f}s\n")
        print(f"{'':7s} {'suite':12s} {'case':40s} {'seconds':>8s}  detail")
        for case in prerequisites + chosen:
            recorded = case in chosen
            runs: list[dict] = []
            for _ in range(args.repeat if recorded else 1):
                r, verdict, why = await perform(case, live, ctx, args.allow_writes, args.timeout)
                runs.append({"verdict": verdict, "why": why, "seconds": round(r.seconds, 3),
                             "ok": r.ok, "text": r.text[:600], "meta": _jsonable(r.meta)})
                if verdict in BAD and args.repeat > 1:
                    break                       # one failure is the answer
            if not recorded:
                continue
            row = summarize(case, runs)
            rows.append(row)
            print(format_row(row), flush=True)

    elapsed = time.perf_counter() - started
    counts = {v: sum(1 for r in rows if r["verdict"] == v)
              for v in (PASS, FAIL, ERROR, KNOWN, FIXED, SKIP)}
    slow = sum(1 for r in rows if r["slow"])
    report = {
        "at": stamp.isoformat(timespec="seconds"),
        "url": url,
        "server": ctx.server,
        "build": ctx.build,
        "checkout": {"head": _git("rev-parse", "--short=7", "HEAD"),
                     "branch": _git("rev-parse", "--abbrev-ref", "HEAD")},
        "python": sys.version.split()[0],
        "repeat": args.repeat,
        "timeout": args.timeout,
        "elapsed": round(elapsed, 1),
        "counts": counts,
        "slow": slow,
        "stored": ctx.techs,
        "rows": rows,
    }

    if getattr(args, "publish", None):
        number = next_bench() if args.publish is True else int(args.publish)
        folder = PUBLISHED / f"bench-{number}"
        folder.mkdir(parents=True, exist_ok=True)
        report["bench"] = number
        (folder / "results.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        written = folder / "README.md"
        written.write_text(markdown(report, published=True), encoding="utf-8")
    else:
        RESULTS.mkdir(parents=True, exist_ok=True)
        base = RESULTS / stamp.strftime("%Y%m%d-%H%M%S")
        base.with_suffix(".json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        written = base.with_suffix(".md")
        written.write_text(markdown(report), encoding="utf-8")

    print(f"\n{counts[PASS]} pass, {counts[FAIL]} fail, {counts[ERROR]} error, "
          f"{counts[KNOWN]} known gap(s), {counts[FIXED]} fixed, {counts[SKIP]} skipped; "
          f"{slow} over budget; {elapsed:.0f}s in all")
    print(f"build {ctx.build or '?'} -> {written}")
    return 1 if counts[FAIL] or counts[ERROR] else 0


def next_bench() -> int:
    """One above the highest `benchmarks/bench-N` that exists."""
    taken = []
    if PUBLISHED.exists():
        for entry in PUBLISHED.iterdir():
            m = re.fullmatch(r"bench-(\d+)", entry.name)
            if m and entry.is_dir():
                taken.append(int(m.group(1)))
    return max(taken, default=0) + 1


def summarize(case: C.Case, runs: list[dict]) -> dict:
    """One row from one or more runs: the worst verdict and the median time."""
    order = [ERROR, FAIL, KNOWN, SKIP, FIXED, PASS]
    worst = min(runs, key=lambda r: order.index(r["verdict"]))
    seconds = [r["seconds"] for r in runs if r["verdict"] != SKIP]
    median = statistics.median(seconds) if seconds else 0.0
    return {
        "suite": case.suite, "name": case.name, "tool": case.tool or case.kind,
        "verdict": worst["verdict"], "why": worst["why"],
        "seconds": round(median, 3), "max_seconds": round(max(seconds), 3) if seconds else 0.0,
        "budget": case.budget,
        "slow": bool(case.budget and median > case.budget),
        "known": case.known, "note": case.note, "writes": case.writes,
        "runs": runs,
    }


def _first_line(row: dict) -> str:
    text = row["runs"][-1]["text"] if row["runs"] else ""
    return text.splitlines()[0] if text.strip() else ""


def format_row(row: dict) -> str:
    detail = row["why"] or _first_line(row)[:70]
    if row["slow"]:
        detail = f"SLOW (budget {row['budget']:.0f}s) " + detail
    if row["known"] and row["verdict"] == KNOWN:
        detail = f"[{row['known']}] " + detail
    return f"{row['verdict']:7s} {row['suite']:12s} {row['name']:40s} {row['seconds']:8.2f}  {detail}"


def markdown(report: dict, published: bool = False) -> str:
    c = report["counts"]
    number = report.get("bench")
    mode = "offline" if _loopback(report["url"]) else "hosted"
    lines = [
        (f"# bench-{number} -- {report['at'][:10]}, {mode}" if published
         else f"# DocsForge live benchmarks -- {report['at']}"),
        "",
        f"Server: `{report['url']}` -- build `{report['build'] or '?'}` "
        f"(v{report['server'].get('version', '?')}). "
        f"Checkout: `{report['checkout'].get('head', '?')}` on "
        f"`{report['checkout'].get('branch', '?')}`. "
        f"{report['repeat']} run(s) per case, {report['elapsed']}s in all.",
        "",
        f"**{c[PASS]} pass · {c[FAIL]} fail · {c[ERROR]} error · {c[KNOWN]} known · "
        f"{c[FIXED]} fixed · {c[SKIP]} skipped · {report['slow']} over budget**",
        "",
        "| verdict | suite | case | s | budget | detail |",
        "|---|---|---|---:|---:|---|",
    ]
    for r in report["rows"]:
        detail = r["why"] or _first_line(r)
        detail = detail.replace("|", "\\|")[:140]
        if r["known"]:
            detail = f"*{r['known']}* {detail}"
        budget = f"{r['budget']:.0f}" if r["budget"] else ""
        flag = " **SLOW**" if r["slow"] else ""
        lines.append(f"| {r['verdict']} | {r['suite']} | `{r['name']}` | "
                     f"{r['seconds']:.2f}{flag} | {budget} | {detail} |")
    if report.get("stored"):
        lines += ["", "## Stored at the end", ""]
        for t in report["stored"]:
            lines.append(f"- **{t['name']}** -- {t['pages']} pages, {t['chars']:,} chars"
                         f"{' ' + t['flags'] if t['flags'] else ''}; {t['labels']}")
    if published:
        # Every case that did not pass, with what the server actually said,
        # as the skeleton of the section a person finishes: what was wrong,
        # whether it is DocsForge's, and what was done about it.
        lines += ["", "## Issues faced", ""]
        found = [r for r in report["rows"] if r["verdict"] in (FAIL, ERROR, KNOWN)]
        if not found:
            lines.append("None: every case passed or was skipped for a stated reason.")
        for i, r in enumerate(found, 1):
            lines += [f"### {i}. `{r['suite']}/{r['name']}` -- {r['verdict']}", ""]
            if r["note"]:
                lines.append(f"*{r['note']}*")
                lines.append("")
            lines.append(f"**What the server said:** {r['why'] or _first_line(r)}")
            lines.append("")
            lines.append("**What it means:** _(to be written)_")
            lines.append("")
    return "\n".join(lines) + "\n"


def diff(a_path: str, b_path: str) -> int:
    a = json.loads(Path(a_path).read_text(encoding="utf-8"))
    b = json.loads(Path(b_path).read_text(encoding="utf-8"))
    ra = {(r["suite"], r["name"]): r for r in a["rows"]}
    rb = {(r["suite"], r["name"]): r for r in b["rows"]}
    print(f"{a['at']} build {a['build']}  ->  {b['at']} build {b['build']}\n")
    changed = 0
    for key in sorted(set(ra) | set(rb)):
        x, y = ra.get(key), rb.get(key)
        if x is None or y is None:
            print(f"{'added' if x is None else 'removed':8s} {key[0]:12s} {key[1]}")
            changed += 1
            continue
        verdict = f"{x['verdict']} -> {y['verdict']}" if x["verdict"] != y["verdict"] else ""
        delta = y["seconds"] - x["seconds"]
        timing = (f"{x['seconds']:.2f}s -> {y['seconds']:.2f}s ({delta:+.2f})"
                  if x["seconds"] and abs(delta) > max(0.5, 0.25 * x["seconds"]) else "")
        if verdict or timing:
            changed += 1
            print(f"{'':8s} {key[0]:12s} {key[1]:40s} {verdict:16s} {timing}")
    print(f"\n{changed} case(s) changed" if changed else "\nno change")
    return 0


def _jsonable(meta: dict) -> dict:
    try:
        json.dumps(meta)
        return meta
    except (TypeError, ValueError):
        return {k: str(v) for k, v in meta.items()}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m scripts.benchmark.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="the /mcp endpoint (default: env, ~/.claude.json, or the hosted server)")
    ap.add_argument("--token", help="bearer token (default: env or ~/.claude.json)")
    ap.add_argument("--offline", action="store_true",
                    help="run everything against a DocsForge started on this machine, "
                         "on a local database, writes and whole harvests included "
                         "(see scripts/benchmark/offline.py)")
    ap.add_argument("--reset", action="store_true",
                    help="with --offline: drop and recreate the offline database first")
    ap.add_argument("--publish", nargs="?", const=True, default=None, metavar="N",
                    help="write the run as benchmarks/bench-N/ (next number when N is omitted)")
    ap.add_argument("--suite", action="append", default=[], choices=C.SUITES,
                    help="run only this suite; repeatable")
    ap.add_argument("--case", action="append", default=[],
                    help="run only cases whose name contains this; repeatable")
    ap.add_argument("--allow-writes", action="store_true",
                    help="include cases that change the knowledge base")
    ap.add_argument("--repeat", type=int, default=1, help="runs per case; the median is reported")
    ap.add_argument("--timeout", type=float, default=CALL_TIMEOUT, help="seconds per call")
    ap.add_argument("--list", action="store_true", help="print the selected cases and exit")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="compare two result files instead of running")
    return ap


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    return parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.diff:
        return diff(*args.diff)
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")
    if args.offline:
        from . import offline
        if args.list:
            args.allow_writes = True
            return asyncio.run(run(args))
        return offline.bench(offline.settings(), args.reset, args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
