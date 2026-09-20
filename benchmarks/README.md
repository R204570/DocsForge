# Live benchmarks

The offline suite in `tests/` asks whether the code does what its fixtures
expect. This directory asks a different question of a *running* DocsForge:
what does each tool actually answer, how long does it take, and is the
deployment the code in this checkout. Every case here goes over MCP to a real
server and reads the real reply; nothing is mocked and nothing imports
DocsForge (except one case that compares the live tool surface with
`forge_tools.TOOLS`, in a subprocess with the store variables emptied).

```
python -m benchmarks.run                  # every read-only suite, ~5 minutes
python -m benchmarks.run --suite guard    # one suite; repeatable
python -m benchmarks.run --case redirect  # cases whose name contains this
python -m benchmarks.run --repeat 3       # median of three per case
python -m benchmarks.run --list           # what would run
python -m benchmarks.run --diff results/a.json results/b.json
```

## Offline: the whole thing on this machine

```
python -m benchmarks.run --offline          # local DB + local server + every suite, writes and whole harvests included
python -m benchmarks.run --offline --reset  # drop and recreate the offline database first
python -m benchmarks.offline                # just the offline MCP server, left running for Claude Code
python -m benchmarks.offline --env          # the settings and the environment the server gets, secrets masked
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
| `DOCSFORGE_OFFLINE_ROOT` | `./offline/` — logs, harvest records, caches, `save_docs` output |
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
an hour (HTTP 429). A harvest that meets it now waits out `Retry-After`,
stops after three refusals in a row, and reports the pages as *refused by
the site*, not as missing — and the benchmark reports that harvest as a
FAIL with the site's reason. Set `DOCSFORGE_OFFLINE_WHOLE` to another
technology, or wait, rather than re-running against the same site.

Offline adds the `offline` suite: `scan_project` over this repository (the
caller's disk *is* the server's), `learn_technology("click")` with no page
limit, a poll of `harvest_status` until the harvest is **done**, then the
listing must show every page Sphinx lists under pypi's 8.x release, the
quickstart (a relative link off `/en/stable/`) must read back, and a search
must answer from it. The `cleanup` suite removes `benchmark-petstore`
afterwards — `forget_documentation` is enabled offline because the store is
disposable. The `click` corpus is kept: it is the offline knowledge base.

## Reaching the server

`--url` and `--token`, else `DOCSFORGE_MCP_URL` and `DOCSFORGE_MCP_TOKEN`,
else the `docsforge` registration in `~/.claude.json` — whatever
`claude mcp add --transport http docsforge <url> --header "Authorization: Bearer …"`
wrote. The default URL is the hosted server. The token is never written to
a result file.

## What is measured

| suite | asks | touches |
|---|---|---|
| `transport` | `/health`; `/mcp` refuses no token and a wrong token; a fresh session; all thirteen tools listed; live schemas equal this checkout's; the build sha the server reports is a commit here | nothing |
| `detect` | `detect_source_type` on one URL of each kind: OpenAPI JSON, a GitHub repo, raw Markdown, a sitemap, an `llms.txt` index beside a full dump, a deep page on a site that publishes one, a plain HTML page, and two non-URLs | the web |
| `fetch` | `fetch_docs`: endpoint tables from OpenAPI, `force=`, a 1.19 MB dump cut at the cap *and saying so*, passthrough, GitHub, HTML, a 3-page crawl, a bounded sitemap, and two errors | the web |
| `guard` | every SSRF spelling of loopback and the metadata address, a hostname that resolves to loopback, a 302 to each of them, `file://`, and `save_docs` inside and outside its root | the web; `/tmp` on the server |
| `resolve` | `find_docs` for names with one right answer, names that collide across registries, a scoped npm name, and a name that does not exist | registries and the web |
| `store` | `list_`, `read_`, `search_knowledge_base` against whatever is stored (the largest corpus), their error paths, `harvest_status`, `forget_*` on unknown names, and `scan_project` over a hosted connection | the store, read-only |
| `concurrency` | six clients listing at once, six searching at once | the store, read-only |
| `writes` | `harvest_docs` of a one-page corpus named `benchmark-petstore`, read and searched back; `learn_technology` on an already-stored name | **the store** |

The `writes` suite runs only with `--allow-writes`. The store behind the
hosted server is the real knowledge base, and a benchmark should not leave
`benchmark-petstore` in it without being asked to. Removing it afterwards
needs `DOCSFORGE_ALLOW_DELETE`.

## Reading a result

Each case ends as one of:

- **PASS** / **FAIL** — the check agreed, or said what was wrong.
- **ERROR** — the call could not be judged: a transport failure, a timeout,
  or a third party the case routes through (httpbin.org) not answering.
- **KNOWN** — a gap already recorded in `Project Development/Evaluation.md`
  (the case names the section) still reproduces. Not a regression.
- **FIXED** — a KNOWN case passed. The record needs updating.
- **SKIP** — `writes` without `--allow-writes`, or a store case with nothing stored.

A case over its latency `budget` is marked **SLOW** in the detail column.
It never fails: a benchmark that failed on a slow network would be
measuring the network. The exit code is 1 on any FAIL or ERROR.

Results land in `results/<timestamp>.json` (every answer's opening lines and
every timing) and `results/<timestamp>.md` (the table). `--diff` compares two
JSON files: verdicts that changed and timings that moved by more than a
quarter.

## Expectations are checked by hand

A case's expected answer is a claim about the world on the day it was
written, and the world moves: `pydantic` documents itself at
`pydantic.dev/docs/validation/latest` now, not `docs.pydantic.dev`, and the
first run of this suite "failed" the resolver for being right. When a
`resolve` or `detect` case fails, open the URL before opening the code.
