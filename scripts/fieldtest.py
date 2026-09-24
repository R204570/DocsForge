#!/usr/bin/env python3
"""
Field test: harvest real documentation sites and read what came back.

The offline suite asks whether the code does what its fixtures expect, and the
benchmark asks what a running server answers for a handful of technologies.
Neither asks the question this project is judged by: pointed at an arbitrary
documentation site -- Docusaurus, Sphinx, Mintlify, a hand-built React app, a
page that renders nothing without JavaScript -- does a harvest come back whole,
and is what it stored the documentation rather than the chrome around it?

So this runs `engine.harvest()` against a spread of real sites chosen for the
generator that built them, one subprocess per site (Playwright's sync API is
bound to its thread, and a crash on one site must not end the run), and
measures every page it stored:

  * how it was acquired, and what the harvest claimed about coverage
  * extraction quality, from the Markdown alone: code blocks whose lines were
    collapsed onto one, relative links left unresolved, UI chrome that
    survived ("Copy", "Edit this page", "On this page"), heading permalink
    marks, empty or untitled pages

It stores nothing in the knowledge base. Output lands in
`measurements/fieldtest/<run>/`, one folder per site with every page as a
file, so a number that looks wrong can be read.

    python scripts/fieldtest.py                      # every site, 15 pages each
    python scripts/fieldtest.py docusaurus mintlify  # labels or generators to run
    python scripts/fieldtest.py --pages 40 --jobs 4
    python scripts/fieldtest.py --list

Network required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: (label, generator, start URL). Chosen to spread across the generators and
#: hosting shapes real documentation is built with, not to be easy.
SITES: list[tuple[str, str, str]] = [
    ("docusaurus", "docusaurus", "https://docusaurus.io/docs"),
    ("react-native", "docusaurus", "https://reactnative.dev/docs/getting-started"),
    ("jest", "docusaurus", "https://jestjs.io/docs/getting-started"),
    ("playwright-node", "docusaurus", "https://playwright.dev/docs/intro"),
    ("playwright-python", "docusaurus", "https://playwright.dev/python/docs/intro"),
    ("mkdocs-material", "mkdocs", "https://squidfunk.github.io/mkdocs-material/getting-started/"),
    ("fastapi", "mkdocs", "https://fastapi.tiangolo.com/"),
    ("pydantic", "mkdocs", "https://docs.pydantic.dev/latest/"),
    ("python-stdlib", "sphinx", "https://docs.python.org/3/library/"),
    ("requests", "sphinx-rtd", "https://requests.readthedocs.io/en/latest/"),
    ("flask", "sphinx", "https://flask.palletsprojects.com/en/stable/"),
    ("django", "sphinx-custom", "https://docs.djangoproject.com/en/stable/"),
    ("godot", "sphinx-rtd", "https://docs.godotengine.org/en/stable/"),
    ("vite", "vitepress", "https://vite.dev/guide/"),
    ("vue", "vitepress", "https://vuejs.org/guide/introduction.html"),
    ("vitest", "vitepress", "https://vitest.dev/guide/"),
    ("astro", "starlight", "https://docs.astro.build/en/getting-started/"),
    ("cloudflare-workers", "starlight", "https://developers.cloudflare.com/workers/"),
    ("nextra", "nextra", "https://nextra.site/docs"),
    ("swr", "nextra", "https://swr.vercel.app/docs/getting-started"),
    ("langgraph-js", "mintlify", "https://docs.langchain.com/oss/javascript/langgraph/overview"),
    ("resend", "mintlify", "https://resend.com/docs/introduction"),
    ("gitbook", "gitbook", "https://gitbook.com/docs"),
    ("readme", "readme.io", "https://docs.readme.com/main/docs"),
    ("nextjs", "custom-next", "https://nextjs.org/docs"),
    ("react", "custom-next", "https://react.dev/learn"),
    ("tailwind", "custom-next", "https://tailwindcss.com/docs/installation"),
    ("stripe", "custom", "https://docs.stripe.com/payments"),
    ("supabase", "custom-next", "https://supabase.com/docs/guides/getting-started"),
    ("mdn", "custom", "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API"),
    ("github-docs", "custom-next", "https://docs.github.com/en/actions"),
    ("ms-learn", "custom", "https://learn.microsoft.com/en-us/dotnet/csharp/"),
    ("angular", "spa", "https://angular.dev/overview"),
    ("svelte", "custom-sveltekit", "https://svelte.dev/docs/svelte/overview"),
    ("pkg-go-dev", "godoc", "https://pkg.go.dev/net/http"),
    ("go-dev", "custom", "https://go.dev/doc/"),
    ("docs-rs", "rustdoc", "https://docs.rs/serde/latest/serde/"),
    ("rust-book", "mdbook", "https://doc.rust-lang.org/book/"),
    ("kubernetes", "hugo-docsy", "https://kubernetes.io/docs/concepts/"),
    ("docsify", "docsify-spa", "https://docsify.js.org/#/"),
    ("apple-swiftui", "spa-json", "https://developer.apple.com/documentation/swiftui"),
    ("aws-lambda", "custom-js", "https://docs.aws.amazon.com/lambda/latest/dg/welcome.html"),
    ("firebase", "custom", "https://firebase.google.com/docs/firestore"),
    ("hono", "vitepress", "https://hono.dev/docs/"),
    ("bun", "custom", "https://bun.sh/docs"),
    ("deno", "custom-lume", "https://docs.deno.com/runtime/"),
    ("prisma", "custom-next", "https://www.prisma.io/docs"),
    ("laravel", "custom", "https://laravel.com/docs/"),
    ("spring-boot", "antora", "https://docs.spring.io/spring-boot/index.html"),
    ("terraform", "custom-next", "https://developer.hashicorp.com/terraform/docs"),
    ("electron", "docusaurus", "https://www.electronjs.org/docs/latest/"),
    ("expo", "custom-next", "https://docs.expo.dev/"),
    ("postgres", "custom-sgml", "https://www.postgresql.org/docs/current/"),
]

OUT = ROOT / "measurements" / "fieldtest"

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


# ── one site, in its own process ────────────────────────────────────────────
def run_one(label: str, url: str, pages: int, where: Path) -> dict:
    from docsforge.core import engine

    where.mkdir(parents=True, exist_ok=True)
    stats: dict = {}
    opts = engine.Options(max_pages=pages, delay=0.25, workers=4, verbose=True)
    started = time.time()
    result: dict = {"label": label, "url": url}
    try:
        docs, strategy = engine.harvest(url, opts, stats=stats)
        result["strategy"] = strategy
    except Exception as e:                       # noqa: BLE001 -- measured, not handled
        docs = []
        result["error"] = f"{type(e).__name__}: {e}"
    result["seconds"] = round(time.time() - started, 1)

    per_page = []
    for i, doc in enumerate(docs, 1):
        quality = assess(doc.markdown)
        per_page.append({"url": doc.url, "title": doc.title, **quality})
        name = f"{i:03d}.md"
        (where / name).write_text(
            f"<!-- {doc.url} -->\n# {doc.title}\n\n{doc.markdown}", encoding="utf-8")

    keep = ("expected", "discovered", "acquired", "fetched", "whole", "reason",
            "truncated", "remaining", "index", "current_release", "revisions",
            "unextractable", "refused", "rate_limited", "corpora", "failed")
    result["stats"] = {k: stats[k] for k in keep if k in stats}
    result["pages"] = per_page
    totals = {k: sum(p[k] for p in per_page) for k in
              ("chars", "fences", "collapsed_fences", "relative_links",
               "chrome_lines", "permalink_marks")}
    totals["pages"] = len(per_page)
    totals["thin_pages"] = sum(1 for p in per_page if p["chars"] < 400)
    totals["untitled"] = sum(1 for p in per_page if (p["title"] or "") in ("", "Untitled"))
    result["totals"] = totals
    (where / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# ── the run ─────────────────────────────────────────────────────────────────
def _spawn(label: str, url: str, pages: int, run_dir: Path, timeout: int) -> dict:
    where = run_dir / label
    where.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--one", label,
           "--url", url, "--pages", str(pages), "--dir", str(where)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", DOCSFORGE_DB="", DATABASE_URL="")
    started = time.time()
    try:
        with open(where / "log.txt", "w", encoding="utf-8") as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=timeout, env=env, cwd=str(ROOT))
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    path = where / "result.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"label": label, "url": url, "error": f"process ended ({code}) with no result",
            "seconds": round(time.time() - started, 1), "totals": {}, "stats": {}}


def summarise(results: list[dict]) -> str:
    head = ("| site | strategy | pages | expected | whole | s | thin | collapsed | "
            "rel links | chrome | ¶ | note |")
    rows = [head, "|" + "---|" * 12]
    for r in sorted(results, key=lambda r: r["label"]):
        t, s = r.get("totals") or {}, r.get("stats") or {}
        note = r.get("error") or s.get("reason") or ""
        rows.append("| {label} | {strategy} | {pages} | {expected} | {whole} | {secs} | "
                    "{thin} | {coll} | {rel} | {chrome} | {perm} | {note} |".format(
                        label=r["label"], strategy=r.get("strategy", "—"),
                        pages=t.get("pages", 0), expected=s.get("expected", ""),
                        whole=s.get("whole", ""), secs=r.get("seconds", ""),
                        thin=t.get("thin_pages", ""), coll=t.get("collapsed_fences", ""),
                        rel=t.get("relative_links", ""), chrome=t.get("chrome_lines", ""),
                        perm=t.get("permalink_marks", ""),
                        note=str(note).replace("|", "/")[:110]))
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("only", nargs="*", help="labels or generators to run")
    ap.add_argument("--pages", type=int, default=15)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--run", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    ap.add_argument("--url", help=argparse.SUPPRESS)
    ap.add_argument("--dir", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.one:
        run_one(args.one, args.url, args.pages, Path(args.dir))
        return 0
    if args.list:
        for label, gen, url in SITES:
            print(f"{label:20} {gen:16} {url}")
        return 0

    chosen = [s for s in SITES if not args.only or s[0] in args.only or s[1] in args.only]
    run_dir = OUT / args.run
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(chosen)} site(s), {args.pages} pages each -> {run_dir}", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(_spawn, label, url, args.pages, run_dir, args.timeout): label
                   for label, _gen, url in chosen}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            t = r.get("totals") or {}
            print(f"  {r['label']:20} {r.get('strategy', 'ERROR'):24} "
                  f"{t.get('pages', 0):3} pages  {r.get('seconds', '?')}s"
                  + (f"  {r['error'][:80]}" if r.get("error") else ""), flush=True)

    (run_dir / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    table = summarise(results)
    (run_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print("\n" + table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
