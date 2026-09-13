#!/usr/bin/env python3
"""
Phase B measurement driver — read the numbers before writing any rule.

PROPOSAL-II asks for exactly this before Phase D: "Run against twenty real
technologies and read the numbers before writing a single revision rule or
threshold." Two of the five proposed revision rules are expected to prove
worthless and one unanticipated rule to prove essential, and the cheapest place
to learn that is here rather than in a crawler that already depends on them.

What it measures, per technology:

  * resolution — did `resolve()` find it at all, from which lap, at what cost.
    The success rate over a list like this is the direct measurement of F6.
  * extraction — for a sample of pages: which CONTENT selector won, how often
    nothing won and <body> was taken whole, how many templates a site really
    has, how much of the "documentation" is link text, and how many pages are
    JS shells.

It stores nothing in the knowledge base and changes nothing in the pipeline.
It fetches pages, measures them, and writes JSON.

    python scripts/measure.py                        # the 20 default technologies
    python scripts/measure.py fastapi pydantic       # only these
    python scripts/measure.py --pages 60             # sample size per technology
    python scripts/measure.py --out measurements     # where the JSON lands

Runs are resumable: a technology whose JSON already exists is skipped unless
--force is passed. Network required, and a full run takes a while — it is
meant to be run once and read, not run often.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from docsforge.core import engine as df  # noqa: E402
from docsforge.core import resolver  # noqa: E402
from docsforge.core import instrument  # noqa: E402
from docsforge.core.instrument import Budget, ResolveState, observe  # noqa: E402
from docsforge.core.observation import Ledger  # noqa: E402

# Twenty, chosen to spread across documentation generators and ecosystems
# rather than to be easy: MkDocs Material, Sphinx, VitePress, Docusaurus,
# Starlight, mdBook, Hugo/Docsy and several bespoke React sites are all here,
# because a rule learned from one generator is not a rule.
TECHNOLOGIES = [
    "fastapi", "pydantic", "django", "flask", "requests", "sqlalchemy",
    "numpy", "polars", "react", "vue", "svelte", "astro", "vite",
    "tailwindcss", "tokio", "serde", "kubernetes", "terraform", "prisma",
    "effect",
]

# F6 is about names that are not one token. Measuring it with the list above
# proves nothing, because a single-token name is exactly the case that already
# works: `{slug}.dev` or an exact registry lookup finds it. These are the names
# the resolution ladder is being built for, and today's expected result is a
# refusal for most of them.
MULTIWORD = [
    "google cloud storage", "azure blob storage", "aws lambda",
    "spring boot", "ruby on rails", "next auth", "tanstack query",
    "shadcn ui", "react hook form", "framer motion", "apache airflow",
    "hugging face transformers", "elastic search", "github actions",
    "visual studio code", "unreal engine", "godot engine",
    "postgres full text search", "open telemetry", "argo cd",
]


def sample(urls: list[str], n: int) -> list[str]:
    """Up to `n` URLs spread evenly across the list, not the first `n`.

    Taking the first n is what the crawler does when it truncates, and the
    proposal names that as a defect: nav order puts every index and landing
    page first, so the first n pages are the least representative sample of a
    documentation site that exists.
    """
    if n <= 0 or len(urls) <= n:
        return list(urls)
    step = len(urls) / n
    return [urls[int(i * step)] for i in range(n)]


def candidate_pages(url: str, fetcher, opts) -> tuple[list[str], str]:
    """Every page under this docs root, by the cheapest route that works."""
    prefix = df.docs_scope(url)
    host = (urlparse(url).hostname or "").lower()

    sitemap = df.find_sitemap(url, fetcher, opts)
    if sitemap:
        try:
            links = df._sitemap_links(fetcher.text(sitemap), fetcher, opts)
        except Exception:
            links = []
        scoped = [l for l in dict.fromkeys(df._normalize(l) for l in links)
                  if df._crawlable(l, host, prefix)]
        scoped = df._prefer_default_locale(df._focus_on_docs(scoped, prefix))
        if len(scoped) >= 3:
            return scoped, "sitemap"

    # No usable sitemap: take what the entry page links to, which is also what
    # the crawler would have to do.
    soup = df._soup(fetcher.html(url))
    links = []
    for anchor in soup.select("a[href]"):
        full = df._normalize(urljoin(url, anchor.get("href") or ""))
        if df._crawlable(full, host, prefix):
            links.append(full)
    return list(dict.fromkeys([url] + links)), "links"


def measure_one(name: str, pages: int, opts, resolve_only: bool = False) -> dict:
    """Resolve one technology, sample its pages, and measure every one."""
    state = ResolveState(name=name)
    budget = Budget()
    started = time.time()

    with df.Fetcher(opts) as fetcher:
        # ── resolution ──
        try:
            found = resolver.resolve(name, fetcher=fetcher)
        except Exception as e:
            return {"name": name, "resolved": False,
                    "error": f"{type(e).__name__}: {e}",
                    "budget": budget.summary()}

        for cand in getattr(found, "candidates", []) or []:
            state.record(cand.url)
            budget.charge()
            if not cand.verified:
                state.reject(cand.url, cand.reason or "unverified")

        if not found.best:
            return {"name": name, "resolved": False,
                    "note": getattr(found, "note", "") or "no candidate verified",
                    "resolve": state.summary(), "budget": budget.summary()}

        url = found.best.url
        if resolve_only:
            # F6 runs care only whether a name reaches a candidate at all;
            # fetching forty pages per name would cost hours and answer nothing.
            return {"name": name, "resolved": True, "url": url,
                    "evidence": found.best.evidence,
                    "seconds": round(time.time() - started, 1),
                    "resolve": state.summary(), "budget": budget.summary()}

        # ── enumeration and extraction ──
        try:
            urls, how = candidate_pages(url, fetcher, opts)
        except Exception as e:
            return {"name": name, "resolved": True, "url": url,
                    "error": f"enumeration failed: {type(e).__name__}: {e}",
                    "resolve": state.summary(), "budget": budget.summary()}

        chosen = sample(urls, pages)
        ledger = Ledger()
        failures = 0
        for link in chosen:
            began = time.time()
            try:
                html = fetcher.html(link)
            except Exception:
                failures += 1
                continue
            budget.charge()
            ledger.record(observe(html, link, name=name,
                                  fetched_ms=int((time.time() - began) * 1000)))
            time.sleep(opts.delay)

    return {
        "name": name,
        "resolved": True,
        "url": url,
        "evidence": found.best.evidence,
        "enumerated_via": how,
        "available_pages": len(urls),
        "sampled_pages": len(chosen),
        "fetch_failures": failures,
        "seconds": round(time.time() - started, 1),
        "resolve": state.summary(),
        "budget": budget.summary(),
        "extraction": ledger.summary(),
        "observations": json.loads(instrument.to_json(ledger))["observations"],
    }


def report(results: list[dict]) -> str:
    """The table to actually read. Everything else is backing detail."""
    lines = []
    resolved = [r for r in results if r.get("resolved")]
    measured = [r for r in resolved if r.get("extraction", {}).get("pages")]

    lines.append(f"resolved {len(resolved)}/{len(results)} technologies")
    lines.append("")
    lines.append("CAUTION: 'resolved' means a candidate passed the identity gate, NOT")
    lines.append("that it is the right documentation. The first run of this driver")
    lines.append("returned 20/20 while six URLs were a marketing homepage, an llms.txt")
    lines.append("or another project entirely. Read the url column by hand.")
    lines.append("")
    if not measured:
        # A --resolve-only run: what each name reached is the whole result.
        for r in sorted(resolved, key=lambda r: r["name"]):
            lines.append(f"{r['name']:<26} -> {str(r.get('url',''))[:60]}")
            lines.append(f"{'':<26}    {str(r.get('evidence',''))[:60]}")
        for r in results:
            if not r.get("resolved"):
                why = r.get("note") or r.get("error") or "unresolved"
                lines.append(f"{r['name']:<26} REFUSED — {why[:52]}")
        return "\n".join(lines)

    head = (f"{'technology':<14} {'pages':>6} {'fell':>6} {'unext':>6} {'shell':>6} "
            f"{'tmpl':>5} {'chars':>7} {'link%':>6}  generator")
    lines.append(head)
    lines.append("-" * len(head))

    for r in sorted(measured, key=lambda r: r["name"]):
        e = r["extraction"]
        gens = e.get("generators") or {}
        gen = next(iter(gens), "") if gens else ""
        lines.append(
            f"{r['name']:<14} {e['pages']:>6} {e['fell_through_pct']:>5.1f}% "
            f"{e.get('unextractable_pct', 0):>5.1f}% {e['shells_pct']:>5.1f}% "
            f"{e['templates']:>5} {e['median_chars']:>7} "
            f"{e['median_link_text_ratio']*100:>5.1f}%  {gen[:28]}"
        )

    for r in results:
        if not r.get("resolved"):
            why = r.get("note") or r.get("error") or "unresolved"
            lines.append(f"{r['name']:<14} UNRESOLVED — {why[:70]}")

    if measured:
        total = sum(r["extraction"]["pages"] for r in measured)
        fell = sum(r["extraction"]["fell_through"] for r in measured)
        shells = sum(r["extraction"]["shells"] for r in measured)
        lines += ["", f"across {total} pages: {fell} fell through to <body> "
                      f"({100*fell/total:.1f}%), {shells} JS shells "
                      f"({100*shells/total:.1f}%)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="measure", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="technologies (default: the built-in 20)")
    ap.add_argument("--multiword", action="store_true",
                    help="use the multi-word list instead: the F6 measurement")
    ap.add_argument("--resolve-only", action="store_true",
                    help="resolve and stop; do not fetch or measure pages")
    ap.add_argument("--pages", type=int, default=40,
                    help="pages to sample per technology (default 40)")
    ap.add_argument("--out", default="measurements", help="output directory")
    ap.add_argument("--delay", type=float, default=0.2, help="politeness delay")
    ap.add_argument("--force", action="store_true", help="re-measure what already exists")
    args = ap.parse_args(argv)

    names = args.names or (MULTIWORD if args.multiword else TECHNOLOGIES)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    opts = df.Options(crawl=True, max_pages=0, delay=args.delay, verbose=False)

    results = []
    for i, name in enumerate(names, 1):
        target = out / f"{name}.json"
        if target.exists() and not args.force:
            print(f"[{i}/{len(names)}] {name}: already measured, skipping", file=sys.stderr)
            results.append(json.loads(target.read_text(encoding="utf-8")))
            continue

        print(f"[{i}/{len(names)}] {name}: measuring…", file=sys.stderr)
        try:
            result = measure_one(name, args.pages, opts,
                                 resolve_only=args.resolve_only)
        except KeyboardInterrupt:
            print("interrupted; what finished is already on disk", file=sys.stderr)
            break
        except Exception as e:                                  # noqa: BLE001
            # One unreachable site must never end a twenty-technology run.
            result = {"name": name, "resolved": False,
                      "error": f"{type(e).__name__}: {e}"}
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
        results.append(result)

    text = report(results)
    (out / "summary.txt").write_text(text, encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "observations"}
                    for r in results], indent=2), encoding="utf-8")
    print()
    print(text)
    print(f"\nwritten to {out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
