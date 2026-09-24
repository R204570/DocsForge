# Product Requirements

DocsForge gives a model documentation for technologies it was not trained
on, and tells the truth about how much of it there is.

Updated 2026-09-21. The earlier `PRODUCT.md` described a chat panel to be
embedded in FlowIT; that destination stands, but the product as it exists
and is measured today is the **MCP server** — hosted at
`docsforge.vercel.app/mcp` and runnable on any machine — used from Claude
Code and any other MCP client. The web panel is a local testing surface.

---

## 1. Users

**Primary: a developer, mid-task, whose model does not know the technology
in front of them** — a library released or changed after the model's
training, or too niche to have been learned well. They know the *name*. They
do not necessarily know where it documents itself, and a URL the model
remembers comes from the same training data that did not include the
technology.

**Secondary:** the same person operating DocsForge for themselves — running
it offline to build a knowledge base of what their projects depend on — and
automated callers (an agent at import time, a planner before it writes code)
that need to know whether the documentation they are about to reason from is
whole.

**Not a user:** anyone who needs a hosted service with an SLA. There is one
developer, one machine, and a free-tier deployment.

## 2. The problem

A model asked about an unknown library does one of three things: refuses,
guesses, or fetches a URL it invented. Scrapers exist, but they assume one
shape of input, take a URL, and report what they got as if it were everything
there was. The failure that matters is not "no documentation"; it is
**documentation that looks complete and is not**, or documents the wrong
project — because the model then stops checking.

## 3. What it must do

### Functional requirements

| # | Requirement | Where it lives | Measured by |
|---|---|---|---|
| F1 | **A name is enough.** `learn_technology(name)` finds where the technology documents itself, confirms the page documents *that* project, harvests the whole set, stores it. | `resolver.py`, `engine.harvest` | `resolve/*`, `offline/learn_whole_technology` |
| F2 | **Detect before extracting.** One URL field accepts `llms.txt`, OpenAPI (JSON/YAML), `sitemap.xml`, a GitHub repository, a plain HTML docs site, raw Markdown. | `engine.detect_source` | `detect/*` |
| F3 | **Refuse a name it cannot confirm.** An unconfirmed resolution returns its candidates and says why each failed; a candidate that could not be read blocks anything weaker. | `resolver.verify`, `unexamined_above` | `resolve/unknown_name_is_honest`, the 2026-09-20 regression tests |
| F4 | **Measure coverage; never invent it.** `complete`, `incomplete`, `unknown`, `refused` are distinct, disclosed in the harvest's answer and the listing. | `engine.harvest` stats, `forge_tools._coverage` | `offline/whole_corpus_stored` |
| F5 | **Store whole; narrow at read time.** Pages are stored as published; `read_knowledge_base(section=)` and `search_knowledge_base` return passages with heading paths and citations. | `kb_store`, `passages.py` | `store/*` |
| F6 | **Versions are kept apart.** Two releases of one library are two corpora; "latest" is the highest release, not the newest download; the version label is the winning registry's release, never another registry's. | `versions.py`, `resolver.release_from` | `whole_corpus_stored` (8.x) |
| F7 | **A harvest outlives the call.** Past the deadline the tool returns a harvest id and the harvest continues; `harvest_status` reports it from any process. | `harvest_jobs.py` | `offline/harvest_runs_to_the_end` |
| F8 | **Every tool exists exactly once**, on every surface, with the same schema. | `forge_tools.TOOLS` → `mcp_server.register` | `transport/surface_matches_checkout` |
| F9 | **Know a project's dependencies** and which are already documented (`scan_project`), from its manifests, at the versions it pins. | `manifests.py` | `offline/scan_this_repo` |
| F10 | **Say what it did.** Every answer names its source URLs, page counts, characters, strategy, truncation, and anything it could not fetch or read. | tool result strings | every case's check reads the text |

### Non-functional requirements

| # | Requirement | Today |
|---|---|---|
| N1 | **No fetch reaches a private address**, by any spelling or redirect. | Held live: 10 guard cases, all refused. |
| N2 | **A hosted `/mcp` is never open.** Bearer required; a non-loopback bind without one is refused at start. | Held live (401 + Bearer challenge). |
| N3 | **A read-only tool answers within 10 s hosted, 1 s offline.** | Offline: every store call under 1 s. Hosted: 4–15 s — the function runs in `iad1`, the database in Singapore (Issues H1). |
| N4 | **A harvest is polite**: per-host delay and concurrency cap, `Retry-After` honoured, stop after three refusals. | Held (`test_rate_limit.py`); measured live on Read the Docs. |
| N5 | **Nothing destructive by default.** Deletion opt-in; benchmarks write only offline. | Held. |
| N6 | **Zero-key path works**: `pipx install docsforge`, files as the store, no model key needed for the MCP server. | Held. |
| N7 | **Every defect found live becomes a test that names the real case.** | 1,165 tests; the 2026-09 fixes each carry one, the field-test ones in `tests/test_realworld.py`. |

## 4. What it must not do

- Invent a URL, or a version, or a coverage figure.
- Harvest into the shared store from a benchmark or a test.
- Restructure what a publisher wrote.
- Present an any-word search fallback as a match for the whole query.
- Report a rate-limited page as one with nothing on it.

## 5. Surfaces

| Surface | Status | For |
|---|---|---|
| MCP over HTTP, hosted (Vercel + Aiven) | in production, free tier | Claude Code / any MCP client, anywhere |
| MCP over HTTP, offline (`scripts/benchmark/offline.py`) | the development and benchmark shape | whole-site harvests, the public benches |
| MCP over stdio (`main.py` launched by a client) | works | a client that spawns its servers |
| CLI (`python -m docsforge <URL>`) | works | one-shot extraction to files |
| Web panel (`server/app.py`) | local only, never hosted | trying providers and tools by hand |

## 6. Constraints

- **One developer, one machine.** Development, testing and benchmarking are
  done offline; the hosted deployment is exercised read-only.
- **Free tiers.** Vercel Hobby (one region, 300 s per request, no background
  work), Aiven's smallest Postgres (15 connections). A harvest larger than
  the request deadline cannot complete hosted.
- **Third parties set the pace.** Read the Docs rate-limits an address that
  harvests one of its sites three times in an hour; GitHub's unauthenticated
  API allows 60 requests an hour per address. The product must survive both
  and say which it met.
- **The store behind the hosted server is real.** Nothing automated writes
  to it.

## 7. Success

- A model that did not know the technology answers correctly from what was
  stored — and where the harvest was partial, says so instead of guessing.
  (`scripts/measure_answers.py` is the only benchmark that speaks to this
  directly; its numbers are the only ones that may be cited about it.)
- A published bench whose *Issues faced* section shrinks from one run to the
  next for the same build, and grows only when a new build is measured.
- Zero cases where DocsForge stored the wrong project under a name, across
  all published benches. bench-1's predecessors found one; it is fixed and
  tested.

## 8. Out of scope, for now

- Hosting the web chat.
- Authentication beyond one bearer token (and the OAuth shim over it).
- A job queue: harvests are not resumed, only observed.
- Rendering JavaScript by default (opt-in, slow, Playwright).
- Multi-tenancy: one store, one owner.

## 9. Open decisions

Listed in `Issues.md` and marked **DECISION**: whether to raise the identity
gate to close the name-squatter path (`flask`, `polars`), whether a scoped
npm name (`@tanstack/react-query`) may be identified by its unscoped tail,
and whether to move the hosted function to the database's region.
