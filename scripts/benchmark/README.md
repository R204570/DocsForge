# The live benchmark suite

The offline suite in `tests/` asks whether the code does what its fixtures
expect. This asks a different question of a *running* DocsForge: what does
each tool actually answer, how long does it take, and is the deployment the
code in this checkout. Every case goes over MCP to a real server and reads
the real reply; nothing is mocked and nothing imports DocsForge (except one
case that compares the live tool surface with `forge_tools.TOOLS`, in a
subprocess with the store variables emptied).

Published runs live in [`benchmarks/`](../../benchmarks/README.md) as
`bench-N/`. This folder is the machinery.

```
python -m scripts.benchmark.run --offline                    # the usual run: everything, on this machine
python -m scripts.benchmark.run --offline --reset --publish  # fresh DB + caches, published as benchmarks/bench-<next>/
python -m scripts.benchmark.run                              # every read-only suite against the hosted server
python -m scripts.benchmark.run --suite guard                # one suite; repeatable
python -m scripts.benchmark.run --case redirect              # cases whose name contains this
python -m scripts.benchmark.run --repeat 3                   # median of three per case
python -m scripts.benchmark.run --list                       # what would run
python -m scripts.benchmark.run --diff a/results.json b/results.json
```

An unpublished run writes `offline/results/<timestamp>.json` and `.md`
(git ignores `offline/`). `--publish [N]` writes `benchmarks/bench-N/` —
`results.json` and a `README.md` that opens with the table and ends with an
*Issues faced* section listing every case that did not pass, to be
finished by hand with what each turned out to be.

## Offline: the whole thing on this machine

```
python -m scripts.benchmark.run --offline          # local DB + local server + every suite, writes and whole harvests included
python -m scripts.benchmark.run --offline --reset  # drop and recreate the offline database and caches first
python -m scripts.benchmark.offline                # just the offline MCP server, left running for Claude Code
python -m scripts.benchmark.offline --env          # the settings and the environment the server gets, secrets masked
```

[offline.py](offline.py) creates a `docsforge` database on the local
Postgres if it is missing, starts `python main.py --http` on
`127.0.0.1:8765` with every DocsForge variable set to an offline value, waits
for `/health` to say `postgres` and not degraded, runs the benchmarks with
`--allow-writes`, and stops the server. Nothing in `.env` reaches the
server: DocsForge loads `.env` on import but never overrides a variable that
is already set, and the runner sets every one it reads.

Offline has its own variable names, all optional:

| variable | default |
|---|---|
| `DOCSFORGE_OFFLINE_DB` | built from the five below |
| `DOCSFORGE_OFFLINE_PG_HOST` / `_PORT` / `_USER` | `127.0.0.1` / `5432` / `postgres` |
| `DOCSFORGE_OFFLINE_PG_PASSWORD` | the password on `.env`'s commented-out *local* DSN, if there is one |
| `DOCSFORGE_OFFLINE_PG_DATABASE` | `docsforge` — created if missing |
| `DOCSFORGE_OFFLINE_ROOT` | `./offline/` — logs, harvest records, caches, `save_docs` output, unpublished results |
| `DOCSFORGE_OFFLINE_PORT` | `8765` |
| `DOCSFORGE_OFFLINE_TOKEN` | none — loopback needs no bearer |
| `DOCSFORGE_OFFLINE_DEADLINE` | `25` s before a tool call hands back a harvest id; the harvest itself is never cut |
| `DOCSFORGE_OFFLINE_WHOLE` | `click` — the technology the offline suite harvests whole |

A database host that is not loopback is refused outright: offline that
reaches the shared store is the one mistake this exists to prevent.
`--reset` drops the database *and* the offline caches (`resolutions.json`,
`selection.json`, harvest records), so a reset run resolves and harvests
from nothing rather than from memory.

Read the Docs rate-limits an address that harvests `click` three times in
an hour (HTTP 429). A harvest that meets it waits out `Retry-After`, stops
after three refusals in a row, and reports the pages as *refused by the
site*, not as missing — and the benchmark reports that harvest as a FAIL
with the site's reason. Set `DOCSFORGE_OFFLINE_WHOLE` to another technology
(`httpx` is known to work), or wait, rather than re-running against the
same site.

The offline suite adds: `scan_project` over this repository (the caller's
disk *is* the server's), `learn_technology` with no page limit, a poll of
`harvest_status` by the returned id until the harvest is **done**, then the
listing must show every page the site lists (for `click`: under PyPI's 8.x
release), a deep page must read back, and a search must answer from it. The
`cleanup` suite removes `benchmark-petstore` afterwards —
`forget_documentation` is enabled offline because the store is disposable.
The whole-technology corpus is kept: it is the offline knowledge base.

## Reaching a hosted server

`--url` and `--token`, else `DOCSFORGE_MCP_URL` and `DOCSFORGE_MCP_TOKEN`,
else the `docsforge` registration in `~/.claude.json` — whatever
`claude mcp add --transport http docsforge <url> --header "Authorization: Bearer …"`
wrote. The default URL is the hosted server. The token is never written to
a result file. Against a hosted server the `writes`, `offline` and `cleanup`
suites are skipped unless `--allow-writes` is passed, and it should not be:
the store behind it is the real one.

## What is measured

| suite | asks | touches |
|---|---|---|
| `transport` | `/health`; `/mcp` refuses no token and a wrong token (hosted); a fresh session; all thirteen tools listed; live schemas equal this checkout's; the build sha the server reports is a commit here | nothing |
| `detect` | `detect_source_type` on one URL of each kind, and two non-URLs | the web |
| `fetch` | `fetch_docs`: endpoint tables, `force=`, a 1.19 MB dump cut at the cap *and saying so*, passthrough, GitHub, HTML, a 3-page crawl, a bounded sitemap, two errors | the web |
| `guard` | every SSRF spelling of loopback and the metadata address, a hostname that resolves to loopback, a 302 to each, `file://`, `save_docs` inside and outside its root | the web; the server's scratch |
| `resolve` | `find_docs` for names with one right answer, names that collide across registries, a scoped npm name, a name that does not exist | registries and the web |
| `store` | `list_`, `read_`, `search_knowledge_base` against the largest stored corpus, their error paths, `harvest_status`, `forget_*` on unknown names, `scan_project` hosted | the store, read-only |
| `concurrency` | six clients listing at once, six searching at once | the store, read-only |
| `offline` | `scan_project` on this repo; a technology harvested whole and read back | **the offline store** |
| `writes` | `harvest_docs` of a one-page corpus, read and searched back; `learn_technology` on a stored name | **the store** |
| `cleanup` | remove `benchmark-petstore` | **the store**, where deletion is enabled |

## Reading a result

- **PASS** / **FAIL** — the check agreed, or said what was wrong.
- **ERROR** — the call could not be judged: a transport failure, a timeout,
  the network between the server and a site, or a third party the case
  routes through (httpbin.org) not answering.
- **KNOWN** — a gap already recorded in `System Files/Issues.md` (the case
  names the id) still reproduces. Not a regression.
- **FIXED** — a KNOWN case passed. The record needs updating.
- **SKIP** — a case that does not apply to this mode (the bearer gate on an
  open loopback server; the hosted `scan_project` gap on a local one), or a
  store case with nothing stored.

A case over its latency `budget` is marked **SLOW** in the detail column.
It never fails: a benchmark that failed on a slow network would be
measuring the network. The exit code is 1 on any FAIL or ERROR.

## Expectations are checked by hand

A case's expected answer is a claim about the world on the day it was
written, and the world moves: `pydantic` documents itself at
`pydantic.dev/docs/validation/latest` now, not `docs.pydantic.dev`, and the
first run of this suite "failed" the resolver for being right. When a
`resolve` or `detect` case fails, open the URL before opening the code.
