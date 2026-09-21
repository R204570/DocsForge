# Benchmarks

The public record of DocsForge being measured. Each `bench-N/` is one run
of the live suite against a running DocsForge: every tool called over MCP,
every answer read, every call timed, and — at the end of the run's
`README.md` — the issues that run faced, what each one turned out to be,
and what was done about it. Nothing here is edited after the fact except
that last section, which is the point of the folder.

| bench | date | mode | build | result | headline |
|---|---|---|---|---|---|
| [bench-1](bench-1/README.md) | 2026-09-20 | offline | `1ce0c43` | 68 pass · 1 fail · 1 error · 2 known · 4 skipped | first published run; `click` harvested whole under PyPI's `8.5.0`; the two fails are the tanstack identity refusal and a connection reset on this network |
| [bench-2](bench-2/README.md) | 2026-09-21 | offline | `4cbdbb1` | 106 pass · 13 fail · 1 error · 2 known · 4 skipped | first run of the `versions` suite: five technologies at two releases each; an unpinned harvest of a site that files releases side by side takes the wrong one (V1), a pinned release against a scoped `llms.txt` is not honoured (V2), and `forget_documentation` counts one version for two (V4) |

## Every bench is an offline run

DocsForge is developed and tested on one machine, not on a fleet. The hosted
deployment (Vercel, a shared Aiven database) is stateless, ephemeral, and
cuts a harvest at its request deadline — the wrong place to measure whether a
harvest runs to the last page, and a place a benchmark must not write. So a
bench runs the whole of DocsForge on the developer's machine:

```
python -m scripts.benchmark.run --offline --reset --publish
```

That creates a `docsforge` database on the local Postgres (dropping any
previous one, and the offline caches with it), starts `python main.py --http`
on `127.0.0.1:8765` with every DocsForge variable pointed at an offline value
so `.env` never reaches it, runs all 126 cases with writes allowed, a
technology harvested whole and five more learned at two releases each,
stops the server, and writes `bench-<next>/`.
The runner and its cases are in [`scripts/benchmark/`](../scripts/benchmark/README.md).

What an offline bench cannot see, and says so by skipping: the bearer gate
(the server runs open on loopback), the serverless "a harvest is not
guaranteed to continue" disclosure, and what `scan_project` does over a
hosted connection. Those cases exist and run against the hosted server with
`python -m scripts.benchmark.run` (read-only), but that is not a bench.

## Reading a bench

Each case ends as one of:

- **PASS / FAIL** — the check agreed, or said what was wrong.
- **ERROR** — the case could not be judged: a transport failure, or a third
  party the case routes through not answering. Not DocsForge's failure.
- **KNOWN** — a gap already on record in `System Files/Issues.md`; the case
  names it. Not a regression.
- **FIXED** — a KNOWN case passed. The record needs updating.
- **SKIP** — a case that does not apply to this mode, with the reason.

**SLOW** marks a case over its latency budget; it never fails a case. The
`results.json` beside each README holds every row in full: the opening lines
of every answer, every timing, and what was stored when the run ended.

## What a bench is not

It is not a leaderboard, and the number of passes is not the score. A bench
that finds nothing has measured a build that was already measured; the ones
worth reading are the ones whose *Issues faced* section is long. bench-1's
predecessors — the unpublished runs that built the suite — found a crawl that
returned one page of forty, a search that answered a query no page contained,
a fonts stylesheet offered as documentation, a rate limit reported as
"nothing on these pages reads like documentation", and a resolver that stored
a Kubernetes CLI under `click`. Every one is a test now.
