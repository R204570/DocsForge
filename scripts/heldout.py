#!/usr/bin/env python3
"""
Held-out accuracy: technologies never used while DocsForge was being fixed.

The field test (`scripts/fieldtest.py`) is where failures were found and
fixed, so its numbers are the ones the code was tuned to. This measures what
the build does on names it has not seen: every case below was chosen, and its
right answer written down, before the first run.

Two stages, both offline (nothing is stored anywhere shared):

  1. **Resolution** -- `resolver.resolve(name, language=)` from the name
     alone, cache off. Correct when the answer is on one of the expected
     documentation locations.
  2. **Harvest** -- the resolved URL harvested (`--pages` per site, one
     subprocess each, as the field test does), and every stored page measured
     for what extraction left behind.

Scores:
  resolution  correct / cases
  harvest     cases that stored real documentation (no error; at least ten
              pages or everything listed; under half of them thin) / resolved
  pages clean pages with no collapsed code, chrome, permalink marks or dead
              relative links / pages stored

    python scripts/heldout.py                  # everything, 40 pages a site
    python scripts/heldout.py --pages 0 zod    # one case, uncapped
    python scripts/heldout.py --resolve-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# (name, language, expected documentation locations as "host" or "host/path").
# Written before the first run; not edited to match what came back.
CASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("zod", "", ("zod.dev",)),
    ("trpc", "", ("trpc.io",)),
    ("drizzle-orm", "", ("orm.drizzle.team",)),
    ("elysia", "", ("elysiajs.com",)),
    ("solid-js", "", ("docs.solidjs.com", "solidjs.com")),
    ("qwik", "", ("qwik.dev", "qwik.builder.io")),
    ("nuxt", "", ("nuxt.com",)),
    ("pinia", "", ("pinia.vuejs.org",)),
    ("turborepo", "", ("turborepo.com", "turbo.build")),
    ("biome", "", ("biomejs.dev",)),
    ("esbuild", "", ("esbuild.github.io",)),
    ("webpack", "", ("webpack.js.org",)),
    ("express", "", ("expressjs.com",)),
    ("nestjs", "", ("docs.nestjs.com", "nestjs.com")),
    ("mongoose", "", ("mongoosejs.com",)),
    ("socket.io", "", ("socket.io",)),
    ("three.js", "", ("threejs.org",)),
    ("axios", "", ("axios-http.com", "axios.rest")),
    ("redux", "", ("redux.js.org",)),
    ("react-hook-form", "", ("react-hook-form.com",)),
    ("htmx", "", ("htmx.org",)),
    ("vitepress", "", ("vitepress.dev",)),
    ("alpinejs", "", ("alpinejs.dev",)),
    ("httpx", "", ("python-httpx.org", "www.python-httpx.org")),
    ("pandas", "", ("pandas.pydata.org",)),
    ("sqlalchemy", "", ("docs.sqlalchemy.org", "sqlalchemy.org")),
    ("celery", "", ("docs.celeryq.dev",)),
    ("pytest", "", ("docs.pytest.org",)),
    ("typer", "", ("typer.tiangolo.com",)),
    ("rich", "", ("rich.readthedocs.io",)),
    ("textual", "", ("textual.textualize.io",)),
    ("streamlit", "", ("docs.streamlit.io",)),
    ("ruff", "", ("docs.astral.sh/ruff",)),
    ("uv", "", ("docs.astral.sh/uv",)),
    ("tokio", "", ("tokio.rs", "docs.rs/tokio")),
    ("axum", "", ("docs.rs/axum",)),
    ("clap", "", ("docs.rs/clap",)),
    ("bevy", "", ("bevy.org", "bevyengine.org", "docs.rs/bevy")),
    ("tauri", "", ("tauri.app", "v2.tauri.app")),
    ("gin", "go", ("gin-gonic.com", "pkg.go.dev/github.com/gin-gonic/gin")),
    ("cobra", "go", ("cobra.dev", "pkg.go.dev/github.com/spf13/cobra")),
    ("docker", "", ("docs.docker.com",)),
    ("redis", "", ("redis.io",)),
    ("ansible", "", ("docs.ansible.com",)),
    ("helm", "", ("helm.sh",)),
    ("prometheus", "", ("prometheus.io",)),
    ("rails", "", ("guides.rubyonrails.org", "api.rubyonrails.org", "rubyonrails.org")),
    ("hugo", "", ("gohugo.io",)),
    # One language's edition of something documented per language.
    ("playwright", "dotnet", ("playwright.dev/dotnet",)),
    ("grpc", "go", ("grpc.io/docs/languages/go",)),
    ("temporal", "typescript", ("docs.temporal.io/develop/typescript", "typescript.temporal.io")),
    ("sentry", "python", ("docs.sentry.io/platforms/python",)),
    ("opentelemetry", "go", ("opentelemetry.io/docs/languages/go",)),
]

#: Round 2: written 2026-09-24 after round 1's failures were fixed, before any
#: of these was run, to measure whether those fixes generalise or were fitted
#: to round 1. None of these is in round 1, the field test or the offline
#: benchmark. Not edited to match what came back.
ROUND2: list[tuple[str, str, tuple[str, ...]]] = [
    ("fastify", "", ("fastify.dev", "fastify.io")),
    ("koa", "", ("koajs.com",)),
    ("lodash", "", ("lodash.com",)),
    ("date-fns", "", ("date-fns.org",)),
    ("dayjs", "", ("day.js.org",)),
    ("chart.js", "", ("chartjs.org",)),
    ("d3", "", ("d3js.org",)),
    ("jquery", "", ("api.jquery.com", "jquery.com", "learn.jquery.com")),
    ("ember", "", ("guides.emberjs.com", "emberjs.com", "api.emberjs.com")),
    ("preact", "", ("preactjs.com",)),
    ("lit", "", ("lit.dev",)),
    ("gatsby", "", ("gatsbyjs.com",)),
    ("storybook", "", ("storybook.js.org",)),
    ("cypress", "", ("docs.cypress.io",)),
    ("puppeteer", "", ("pptr.dev",)),
    ("rollup", "", ("rollupjs.org",)),
    ("eslint", "", ("eslint.org",)),
    ("prettier", "", ("prettier.io",)),
    ("pnpm", "", ("pnpm.io",)),
    ("sequelize", "", ("sequelize.org",)),
    ("typeorm", "", ("typeorm.io",)),
    ("knex", "", ("knexjs.org",)),
    ("zustand", "", ("zustand.docs.pmnd.rs", "docs.pmnd.rs/zustand")),
    ("mobx", "", ("mobx.js.org",)),
    ("valibot", "", ("valibot.dev",)),
    ("numpy", "", ("numpy.org",)),
    ("matplotlib", "", ("matplotlib.org",)),
    ("scikit-learn", "", ("scikit-learn.org",)),
    ("scrapy", "", ("docs.scrapy.org",)),
    ("aiohttp", "", ("docs.aiohttp.org",)),
    ("starlette", "", ("starlette.dev", "starlette.io")),
    ("sqlmodel", "", ("sqlmodel.tiangolo.com",)),
    ("alembic", "", ("alembic.sqlalchemy.org",)),
    ("mypy", "", ("mypy.readthedocs.io",)),
    ("black", "", ("black.readthedocs.io",)),
    ("gradio", "", ("gradio.app",)),
    ("serde", "", ("serde.rs", "docs.rs/serde")),
    ("actix-web", "", ("actix.rs", "docs.rs/actix-web")),
    ("rocket", "", ("rocket.rs",)),
    ("diesel", "", ("diesel.rs",)),
    ("reqwest", "", ("docs.rs/reqwest",)),
    ("echo", "go", ("echo.labstack.com",)),
    ("fiber", "go", ("docs.gofiber.io", "gofiber.io")),
    ("gorm", "go", ("gorm.io",)),
    ("kafka", "", ("kafka.apache.org",)),
    ("nginx", "", ("nginx.org",)),
    ("istio", "", ("istio.io",)),
    ("flutter", "", ("docs.flutter.dev",)),
    ("symfony", "", ("symfony.com/doc",)),
    ("ktor", "", ("ktor.io",)),
    ("strapi", "", ("docs.strapi.io",)),
    ("pocketbase", "", ("pocketbase.io",)),
    # One language's edition of something documented per language.
    ("mongodb", "python", ("mongodb.com/docs/languages/python", "mongodb.com/docs/drivers/pymongo",
                           "pymongo.readthedocs.io")),
    ("mongodb", "go", ("mongodb.com/docs/drivers/go", "pkg.go.dev/go.mongodb.org/mongo-driver")),
    ("pulumi", "python", ("pulumi.com/docs/iac/languages-sdks/python",
                          "pulumi.com/docs/languages-sdks/python")),
    ("supabase", "python", ("supabase.com/docs/reference/python",)),
    ("elasticsearch", "python", ("elastic.co/docs/reference/elasticsearch/clients/python",
                                 "elastic.co/guide/en/elasticsearch/client/python-api",
                                 "elasticsearch-py.readthedocs.io")),
]

#: Round 3: written 2026-09-25, after round 2's failures were fixed and before
#: any of these was run -- round 2 stopped being unseen the moment its
#: failures were worked on, so this is the set that says whether they
#: generalise. None is in rounds 1-2, the field test or the benchmark; names
#: whose common meaning is itself contested (`warp`: the terminal or the Rust
#: library?) were left out rather than guessed. Not edited to match results.
ROUND3: list[tuple[str, str, tuple[str, ...]]] = [
    ("jotai", "", ("jotai.org",)),
    ("immer", "", ("immerjs.github.io",)),
    ("formik", "", ("formik.org",)),
    ("ramda", "", ("ramdajs.com",)),
    ("rxjs", "", ("rxjs.dev",)),
    ("handlebars", "", ("handlebarsjs.com",)),
    ("pug", "", ("pugjs.org",)),
    ("mocha", "", ("mochajs.org",)),
    ("jasmine", "", ("jasmine.github.io",)),
    ("webdriverio", "", ("webdriver.io",)),
    ("nodemon", "", ("nodemon.io", "github.com/remy/nodemon")),
    ("pm2", "", ("pm2.keymetrics.io", "pm2.io")),
    ("hapi", "", ("hapi.dev",)),
    ("adonisjs", "", ("docs.adonisjs.com", "adonisjs.com")),
    ("meteor", "", ("docs.meteor.com", "meteor.com", "v3-docs.meteor.com")),
    ("eleventy", "", ("11ty.dev",)),
    ("hexo", "", ("hexo.io",)),
    ("jekyll", "", ("jekyllrb.com",)),
    ("quasar", "", ("quasar.dev",)),
    ("vuetify", "", ("vuetifyjs.com",)),
    ("chakra-ui", "", ("chakra-ui.com", "v2.chakra-ui.com")),
    ("mantine", "", ("mantine.dev",)),
    ("django-ninja", "", ("django-ninja.dev",)),
    ("tornado", "", ("tornadoweb.org",)),
    ("sanic", "", ("sanic.dev",)),
    ("peewee", "", ("docs.peewee-orm.com",)),
    ("attrs", "", ("attrs.org",)),
    ("loguru", "", ("loguru.readthedocs.io",)),
    ("dask", "", ("docs.dask.org",)),
    ("xarray", "", ("docs.xarray.dev",)),
    ("seaborn", "", ("seaborn.pydata.org",)),
    ("bokeh", "", ("docs.bokeh.org",)),
    ("networkx", "", ("networkx.org",)),
    ("sympy", "", ("docs.sympy.org", "sympy.org")),
    ("nltk", "", ("nltk.org",)),
    ("spacy", "", ("spacy.io",)),
    ("tonic", "", ("docs.rs/tonic", "github.com/hyperium/tonic")),
    ("sqlx", "", ("docs.rs/sqlx", "github.com/launchbadge/sqlx")),
    ("egui", "", ("docs.rs/egui", "egui.rs")),
    ("nom", "", ("docs.rs/nom",)),
    ("zap", "go", ("pkg.go.dev/go.uber.org/zap",)),
    ("testify", "go", ("pkg.go.dev/github.com/stretchr/testify", "github.com/stretchr/testify")),
    ("buffalo", "go", ("gobuffalo.io",)),
    ("consul", "", ("developer.hashicorp.com/consul",)),
    ("rabbitmq", "", ("rabbitmq.com/docs",)),
    ("cassandra", "", ("cassandra.apache.org",)),
    ("jenkins", "", ("jenkins.io/doc",)),
    ("traefik", "", ("doc.traefik.io",)),
    ("caddy", "", ("caddyserver.com/docs",)),
    ("sass", "", ("sass-lang.com",)),
    ("phoenix", "", ("hexdocs.pm/phoenix", "phoenixframework.org")),
    ("sinatra", "", ("sinatrarb.com",)),
    ("vapor", "", ("docs.vapor.codes",)),
    # One language's edition of something documented per language.
    ("redis", "python", ("redis.io/docs/latest/develop/clients/redis-py", "redis-py.readthedocs.io",
                         "redis.readthedocs.io")),
    ("dapr", "python", ("docs.dapr.io/developing-applications/sdks/python",)),
    ("selenium", "python", ("selenium.dev/documentation", "selenium-python.readthedocs.io")),
]

SETS = {"round1": CASES, "round2": ROUND2, "round3": ROUND3}

OUT = ROOT / "measurements" / "heldout"


def matches(url: str, expected: tuple[str, ...]) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or "/"
    for want in expected:
        want_host, _, want_path = want.partition("/")
        if host == want_host.removeprefix("www.") and \
                path.lstrip("/").lower().startswith(want_path.lower()):
            return True
    # An expected address that now redirects: the site moved, the answer did
    # not. `turborepo.com` and `turbo.build` both land on `turborepo.dev`.
    # The expectations stay as written; only where they lead today is asked.
    for want in expected:
        moved = _lands_on(want)
        if moved and moved != want and matches(url, (moved,)):
            return True
    # And the answer's own redirect: `mypy.readthedocs.org` is served from
    # `mypy.readthedocs.io`.
    answer = (parsed.hostname or "") + parsed.path
    moved = _lands_on(answer) if url else ""
    if moved and moved != answer.lower().removeprefix("www.").rstrip("/"):
        return any(_plain_match(moved, want) for want in expected)
    return False


def _plain_match(landed: str, want: str) -> bool:
    host, _, path = landed.partition("/")
    want_host, _, want_path = want.partition("/")
    return host == want_host.removeprefix("www.") and \
        path.lower().startswith(want_path.lower())


_LANDS: dict[str, str] = {}


def _lands_on(want: str) -> str:
    """Where `https://<want>` lands today, as `host/path`, or ""."""
    if want not in _LANDS:
        import requests
        try:
            r = requests.get(f"https://{want}", timeout=15, allow_redirects=True,
                             headers={"User-Agent": "DocsForge-heldout/1"})
            p = urlparse(r.url)
            _LANDS[want] = ((p.hostname or "").lower().removeprefix("www.")
                            + p.path.rstrip("/")) if r.ok else ""
        except Exception:                           # noqa: BLE001 -- unreachable
            _LANDS[want] = ""
    return _LANDS[want]


def _label(row: dict) -> str:
    return row["name"] + (f"-{row['language']}" if row["language"] else "")


def resolve_one(case) -> dict:
    from docsforge.core import resolver
    name, language, expected = case
    started = time.time()
    try:
        got = resolver.resolve(name, use_memory=False, language=language)
        url = got.best.url if got.best else ""
        row = {"url": url, "via": got.resolved_via, "note": (got.note or "")[:300]}
    except Exception as e:                          # noqa: BLE001 -- measured
        row = {"url": "", "via": "", "error": f"{type(e).__name__}: {e}"}
    row.update({"name": name, "language": language, "expected": list(expected),
                "correct": matches(row["url"], expected),
                "seconds": round(time.time() - started, 1)})
    return row


def harvest_ok(result: dict) -> bool:
    if result.get("error"):
        return False
    t = result.get("totals") or {}
    pages = t.get("pages", 0)
    listed = (result.get("stats") or {}).get("expected") or 0
    enough = pages >= 10 or (listed and pages >= listed)
    return bool(pages) and enough and t.get("thin_pages", 0) < pages / 2


def clean_pages(result: dict) -> tuple[int, int]:
    rows = result.get("pages") or []
    clean = sum(1 for p in rows if not (p["collapsed_fences"] or p["chrome_lines"]
                                        or p["permalink_marks"] or p["relative_links"]))
    return clean, len(rows)


def main(argv=None) -> int:
    import fieldtest

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("only", nargs="*", help="case names to run")
    ap.add_argument("--pages", type=int, default=40)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--run", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--set", default="round1", choices=sorted(SETS))
    args = ap.parse_args(argv)

    run_dir = OUT / args.run
    run_dir.mkdir(parents=True, exist_ok=True)
    # Resolution remembers nothing between runs and touches no shared cache.
    os.environ["DOCSFORGE_RESOLVE_CACHE"] = str(run_dir / "resolutions.json")
    os.environ.setdefault("DOCSFORGE_DB", "")
    cases = [c for c in SETS[args.set] if not args.only or c[0] in args.only]
    print(f"{len(cases)} held-out case(s) -> {run_dir}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        resolved = list(pool.map(resolve_one, cases))
    for row in resolved:
        mark = "ok " if row["correct"] else "BAD"
        print(f"  {mark} {row['name']:16} {row['language'] or '-':10} {row['url'] or row.get('error', '')}",
              flush=True)

    harvested: dict[str, dict] = {}
    if not args.resolve_only:
        todo = [r for r in resolved if r["correct"]]

        def go(row):
            label = _label(row)
            return label, fieldtest._spawn(label, row["url"], args.pages, run_dir,
                                           args.timeout)

        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for name, result in pool.map(go, todo):
                harvested[name] = result
                t = result.get("totals") or {}
                print(f"  harvest {name:20} {result.get('strategy', 'ERROR'):28} "
                      f"{t.get('pages', 0):4} pages  {result.get('seconds', '?')}s"
                      + (f"  {result['error'][:80]}" if result.get("error") else ""), flush=True)

    correct = sum(r["correct"] for r in resolved)
    good = sum(harvest_ok(harvested[n]) for n in harvested)
    clean = total = 0
    for result in harvested.values():
        c, n = clean_pages(result)
        clean, total = clean + c, total + n
    lines = [f"# Held-out run {args.run}", "",
             f"- resolution: **{correct}/{len(resolved)}** "
             f"({100 * correct / max(1, len(resolved)):.0f}%)"]
    if harvested:
        lines.append(f"- harvest: **{good}/{len(harvested)}** of the correctly resolved "
                     f"({100 * good / max(1, len(harvested)):.0f}%)")
        lines.append(f"- pages clean: **{clean}/{total}** "
                     f"({100 * clean / max(1, total):.0f}%)")
    lines += ["", "| case | lang | resolved to | ok | harvest | pages | listed | note |",
              "|---|---|---|---|---|---|---|---|"]
    for row in resolved:
        result = harvested.get(_label(row), {})
        t, s = result.get("totals") or {}, result.get("stats") or {}
        lines.append("| {n} | {l} | {u} | {ok} | {h} | {p} | {e} | {note} |".format(
            n=row["name"], l=row["language"] or "", u=row["url"] or "—",
            ok="yes" if row["correct"] else "**no**",
            h=("yes" if harvest_ok(result) else "**no**") if result else "",
            p=t.get("pages", ""), e=s.get("expected", ""),
            note=str(result.get("error") or s.get("reason") or "").replace("|", "/")[:90]))
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / "results.json").write_text(json.dumps(
        {"resolved": resolved, "harvested": harvested}, indent=2), encoding="utf-8")
    print("\n" + "\n".join(lines[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
