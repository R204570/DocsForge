# Architecture

Recorded from the build, not from intention. Where the code and this file
disagree, the code is right and this file is stale.

Written on 2026-09-21 against 1,076 passing offline tests (61 skipped behind
opt-in gates: live network, a throwaway Postgres, browser rendering) and the
live benchmark suite in `scripts/benchmark/` (76 cases, published as
`benchmarks/bench-N/`).

Revised 2026-09-24 for the real-world acquisition work — §2, §5, §6, §7, §8
and §12 — after `scripts/fieldtest.py` harvested 53 documentation sites and
found where real sites defeat the harvester (`Issues.md` A1–A12, L1–L6).

---

## 1. One engine, three surfaces

The extraction engine is a library. Nothing in it knows whether a CLI, an MCP
client or a browser is asking, which is what keeps the three from drifting
apart: an MCP client and the chat panel return byte-identical results because
they call the same function with the same arguments.

```mermaid
graph LR
  CLI["CLI<br/>python -m docsforge"]
  MCP["MCP server<br/>main.py"]
  WEB["Web panel<br/>server/app.py + static/<br/>(local only, never hosted)"]

  TOOLS["tools/forge_tools.py<br/>the thirteen tools, defined once"]
  ENGINE["core/engine.py<br/>detect · fetch · extract · harvest"]
  STORE["store/kb_store.py<br/>FileStore | PostgresStore"]

  CLI --> ENGINE
  MCP --> TOOLS
  WEB --> TOOLS
  TOOLS --> ENGINE
  TOOLS --> STORE
  ENGINE --> STORE
```

`tools/forge_tools.py` is the seam. It owns argument shaping, the trace
wrapper, and the human-readable result strings; `core/engine.py` owns the web
and knows nothing about tools. `server/mcp_server.py` *generates* the MCP
surface from `forge_tools.TOOLS` — a JSON schema becomes a typed signature —
so the two cannot drift, and the benchmark's `surface_matches_checkout` case
confirms live that they have not.

---

## 2. Module map

Everything is one package, `docsforge/`, with a subpackage per
responsibility. `main.py` is the only script at the root: it starts the MCP
server, and being at the root is what makes it launchable by path from any
MCP client's config.

```
main.py                      python main.py [--http|--stdio]   -> the MCP server
docsforge/__main__.py        python -m docsforge <URL>          -> the CLI
docsforge/core/              acquisition, identity and scope
  engine.py                    Fetcher (the SSRF guard, redirects, 429), detection,
                               extraction, the acquisition ladder, the crawl
  resolver.py                  name -> where it documents itself; the identity gate;
                               one language's edition (`language=`)
  languages.py                 language aliases, editions, code evidence, official manuals
  topics.py                    a topic as a rule for which pages belong
  llmsfinder.py                llms.txt shape, links, density
  manifests.py                 generator manifests (Sphinx objects.inv, MkDocs search index)
                               and project manifests (package.json, pyproject, ...)
  versions.py                  release ordering, "asked is a prefix of found"
  federation.py                a technology is several corpora; page kinds
  selection.py                 which corpora an intent takes; ask, never guess
  passages.py                  read-time relevance: sections, ranking
  reasoning.py                 optional model veto (DOCSFORGE_REASONING), off by default
  observation.py / instrument.py  what a page and a probe revealed; the crawl's plan
docsforge/store/kb_store.py  FileStore and PostgresStore behind one protocol
docsforge/tools/
  forge_tools.py               the tool layer: every tool, exactly once
  harvest_jobs.py              harvests that outlive the call: deadline, records, heartbeat
  tracing.py                   the nested execution trace a panel renders
  applog.py                    rotating JSONL request/tool/harvest log
docsforge/server/
  mcp_server.py                the MCP surface, the public site, /health, the bearer gate
  oauth.py                     an OAuth server whose login is the bearer token (for ChatGPT)
  vercel.py                    the same server as a stateless, ephemeral Vercel Function
  site/                        three self-contained public pages: /, /tools, /connect
  app.py + static/             the web chat and DocsStore browser -- local testing only
docsforge/providers/         one model backend per file, for the web chat
scripts/
  benchmark/                   the live suite: run.py, cases.py, live.py, offline.py
  fieldtest.py                 53 real documentation sites, every page measured
  measure*.py, smoke_*.py      the older harnesses and smoke drivers
tests/                       the offline suite
benchmarks/                  published benchmark runs, bench-1 onward
System Files/                this folder
```

```mermaid
graph TD
  subgraph Surfaces
    MCPS["server/mcp_server.py<br/>stdio or HTTP"]
    VER["server/vercel.py<br/>stateless Function"]
    APP["server/app.py<br/>local chat"]
  end
  subgraph Tools
    FT["tools/forge_tools.py"]
    HJ["tools/harvest_jobs.py"]
  end
  subgraph Acquisition
    DF["core/engine.py"]
    LF["core/llmsfinder.py"]
    MAN["core/manifests.py"]
    VS["core/versions.py"]
  end
  subgraph Identity
    RES["core/resolver.py"]
    LANG["core/languages.py"]
    INST["core/instrument.py"]
    REA["core/reasoning.py"]
  end
  subgraph Scope
    FED["core/federation.py"]
    SEL["core/selection.py"]
    TOP["core/topics.py"]
  end
  subgraph Storage
    KB["store/kb_store.py"]
    PAS["core/passages.py"]
  end
  subgraph Observability
    TR["tools/tracing.py"]
    AL["tools/applog.py"]
  end
  VER --> MCPS
  MCPS --> FT
  APP --> FT
  FT --> HJ
  FT --> DF
  FT --> RES
  FT --> TR
  FT --> AL
  HJ --> DF
  DF --> LF
  DF --> MAN
  DF --> VS
  DF --> FED
  RES --> INST
  RES --> REA
  RES --> VS
  RES --> LANG
  FT --> LANG
  DF --> TOP
  FED --> SEL
  DF --> KB
  KB --> PAS
```

---

## 3. Where it runs

Three shapes of process serve the same tools.

| | stdio | `--http` (long-lived) | Vercel |
|---|---|---|---|
| Started by | an MCP client, per turn | a person, or the Containerfile | the platform, per request |
| Store | files or `DOCSFORGE_DB` | files or `DOCSFORGE_DB` | Postgres, required |
| `/mcp` gate | none (pipes) | bearer token; refused on a non-loopback bind without one | bearer token, required |
| A harvest past the deadline | continues; the process waits for it before exiting | continues in the background | **discarded**, and the answer says so |
| Session state | per process | per process | none: every request carries everything |

**Hosted** is `docsforge/server/vercel.py`: the site at `/`, `/tools`,
`/connect`, `/health` for the platform, `/mcp` behind `DOCSFORGE_MCP_TOKEN`
(with an OAuth layer whose login *is* that token, so a client that cannot
send a bearer header — ChatGPT — can still get in). The corpus lives in an
Aiven PostgreSQL. Only `/tmp` is writable, so the caches DocsForge keeps on
disk go there, and harvest records go to the database as well, because two
requests are not promised the same instance.

**Offline** is `scripts/benchmark/offline.py`: `python main.py --http` on
loopback against a local `docsforge` database, with every variable DocsForge
reads set to an offline value before the process starts. It is the shape the
benchmarks run in, and the shape a person uses to harvest something large:
nothing cuts a harvest short there.

---

## 4. A tool call, end to end

```mermaid
sequenceDiagram
  participant C as MCP client
  participant S as mcp_server
  participant T as forge_tools.run_tool_checked
  participant E as engine / resolver
  participant K as store

  C->>S: tools/call learn_technology(name)
  S->>T: run on a worker thread
  T->>T: open a trace; sanitise args
  T->>E: resolve(name)
  E-->>T: Resolution (best, candidates, note)
  T->>E: harvest(best.url) -- on a job thread
  E->>K: sink.add(page) per page, durable as it goes
  Note over T: deadline (25 s) reached
  T-->>S: "still running, harvest id click-1"
  S-->>C: text result
  E->>K: settle(complete, expected, version)
  Note over K: job record: done
```

`run_tool_checked` wraps every tool in one root trace stage recording the
arguments (sanitised) and the returned text (bounded), and turns a
`ForgeError` into an `isError` result whose text a model can act on. Read
tools (`list_`, `read_`, `search_knowledge_base`, `harvest_status`,
`scan_project`) run inside one store session — one connection for the call,
not one per operation, which mattered the day ten concurrent searches were
forty connections against a plan allowing twenty. The harvesting tools are
deliberately *not* pooled: they spend their call fetching pages, and holding a
connection open across that is the problem, not a smaller version of it.

---

## 5. The Fetcher: every request through one door

`engine.Fetcher.get` is the only place an HTTP request leaves the package, and
it does five things on every one:

1. **Guard the URL**: scheme, host, and the address the host resolves to —
   private, loopback, link-local, reserved, multicast and unspecified ranges
   are refused, NAT64-mapped addresses are unwrapped first, and every
   `inet_aton` spelling (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`)
   is read as the address it is, without asking the platform's resolver.
2. **Follow redirects itself**, one hop at a time, guarding each target.
   `requests` used to follow them alone, and a `302 Location:
   http://169.254.169.254/` walked past the guard that refused the same
   address typed directly (`Evaluation` §2.1, fixed 2026-09-17).
3. **Honour a 429**: `Retry-After` is waited for, bounded by 30 s and two
   retries; a 429 that names no wait gets five seconds.
4. **Report where it landed**: `html_at` returns `(html, landed_url)`, and a
   crawl resolves relative links against that, or a declared `<base href>`.
   Resolving against the normalised spelling it had asked for turned
   `quickstart/` under `/en/stable` into `/en/quickstart`, 38 times.
5. **Ask again after a reset**: a connection cut mid-request is retried
   twice, after a growing pause (`_reset_tolerant`). Only a reset — a name
   that does not resolve, a refused connection and a timeout are answers,
   and the resolver probes guessed domains that mostly do not exist
   (`Issues.md` N1).

A URL is fetched as the site spells it (`_fetchable`) and keyed by
`_page_key`, under which `/x`, `/x/`, `/x.md` and `/x/index.html` are one
page: a server owes `/book` nothing it owes `/book/` (A1).

An HTTP failure is an `HTTPStatusError` carrying its status, so a caller can
tell a 404 (the page is not there) from a 429 (the site said not now).

**Rendering** (`_render`) is the one other door, and the same guard stands in
it: every request the page makes passes `page.route`, which also turns away
images, media and fonts. It waits for the document and then a bounded settle
(`_settle`: load, a short network-idle window, visible text unchanged across
checks), never for `networkidle` alone, which a page holding an analytics
connection never reaches. One browser context serves the whole harvest, and
the first rendered pages have their collapsed sidebar sections opened
(`_expand_navigation`) so the links behind them are found. Playwright's sync
API belongs to one thread, so rendered pages are fetched on the crawl's own
thread, never from its pool.

---

## 6. The acquisition ladder

*Revised 2026-09-24 after the field test (`Issues.md` A1–A12).* Before any
rung: the start URL is landed (redirects, client-side stubs), and the section
is read from the page's own sidebar (`_resolve_section`) into
`Options.section`, which every rung below reads through `_scope_for`.

```mermaid
flowchart TD
  L0["0 · land the start URL; read the section from its sidebar"]
  L1["1 · llms-full.txt / llms.txt, nearest the section first<br/>a dump cut into the pages it names; an index fetched"]
  S["then: the site's lists and the delivered pages' links<br/>what they name in the section and the file left out is fetched too"]
  L2["2 · the site's lists, together<br/>generator manifest + every sitemap (section's own first, budgeted)"]
  L3["3 · crawl seeded with those lists, following in-section links<br/>(or from the start page when nothing is listed)"]
  L0 --> L1
  L1 -->|delivered| S
  L1 -->|absent or refused| L2 --> L3
```

The crawl is one loop for every HTML page, listed or found: concurrent over
plain HTTP, adaptive (the Plan), a Markdown twin taken when a page declares one
and its HTML is unreadable, one rendered retry for a page that ships the
scripts to draw itself, and a switch to rendering (on the main thread) once a
site proves mostly client-side. `_admission` keeps what it finds to the
harvest's language and release; a topic (`Options.topic`, `topics.Selector`)
judges every page at the sink and follows only the kept ones. docsify sites
bypass it all: their `_sidebar.md` names the Markdown files.

The rungs as they stood before, kept for the incidents that shaped them:

```mermaid
flowchart TD
  L1["1 · llms-full.txt<br/>one request, stored whole"]
  L2["2 · llms.txt with Markdown links<br/>one request per listed page"]
  L3["3 · llms.txt with HTML links<br/>the site's own page list, extracted"]
  L4["4 · generator manifest<br/>Sphinx objects.inv, MkDocs search index"]
  L5["5 · sitemap.xml, filtered to the docs section"]
  L6["6 · scoped crawl"]
  L1 -->|absent| L2 -->|absent| L3 -->|absent| L4 -->|absent| L5 -->|absent| L6
```

**A file the site published about itself outranks anything inferred about
it — and does not exhaust it.** A sitemap is a hint addressed to crawlers;
`llms.txt` is a statement addressed to us, and is read first. It is then
checked against the site's other statements (A6): the sitemap and manifest
for the same section, and the links on the pages it delivered. What they name
and it left out is fetched too; when they agree, nothing more is fetched.
**An index is not documentation**: shape is classified by link density before
anything is stored, and an index of 229 links is a table of contents, not a
corpus. **A dump is cut where it says its pages are** (`Source:`, `URL:`, a
heading that links to the page), each page under its own address.

**The request decides the pathway, not the file.** `llms.txt` describes the
current release. Asked for a named release, the harvest checks whether the
file declares one and whether it matches, narrows to the links filed under
that release when it does not, and ignores the file when it cannot show it
documents the release asked for.

**Every rung pays the same politeness**: a per-host delay, a per-host
concurrency cap, and — new this week — a stop after three 429s in a row. A
harvest the site stopped reports the pages it did not fetch as *refused by
the site*, a different fact from *reached and unreadable*, and the corpus is
`INCOMPLETE` for that stated reason rather than `COVERAGE UNKNOWN`.

### Coverage is three separate claims

| Claim | Means |
|---|---|
| `expected` | every page known to exist in the section: what the published file, manifest and sitemaps list, plus what their pages link to inside it |
| `acquired` | how many of those came back |
| `whole` | `acquired == expected` with nothing left queued; `False` for a partial corpus; `None` when nothing established a denominator (a crawl from one page, no list anywhere) |

`expected` is measured against what the site says exists, never against the
slice a page limit left behind. `listed` and `found_by_links` say how much of
it each source contributed, and a published file checked against the site's
lists records what it left out in `supplemented`. `unextractable` (reached,
nothing on it read like documentation) and `refused` (asked for, HTTP 429)
are listed by URL in the harvest's coverage note, because a hole nobody is
told about is indistinguishable from a page that never existed. Two things
are listed and *not* counted as holes (A13): `dead` pages, listed or linked
and answering 404 or 410 — the site's list is stale, the copy is not short —
and `index_pages`, tables of contents that are all links, followed to what
they list and not stored.

A topic (`topics.Selector`) changes what is expected, not what is claimed: a
page judged outside the topic is not expected, a page judged inside it that
failed to arrive still counts against it, and `stats["topic"]` lists the
terms used and what was left out, by section.

---

## 7. Name resolution and the identity gate

The hard part is not finding a candidate. It is refusing a plausible wrong
one: a harvest of the wrong project, stamped `verified`, is worse than no
harvest, because the caller has been given a reason to stop checking.

```mermaid
flowchart TD
  N["name (+ language)"] --> MEM{"remembered for this name and language,<br/>under the current RULES?"}
  MEM -->|yes| DONE["done, no requests"]
  MEM -->|no| L0["L0 · a language or runtime?<br/>its own manual (go.dev/doc/), verified"]
  L0 -->|verified| OK
  L0 -->|not one| L1["L1 · the project's own domain<br/>name.dev/io/org/com, namelang.org, name-lang.org"]
  L1 -->|"verified, and more than ownership"| DONE2["resolved via domain;<br/>registries not consulted"]
  L1 -->|"not, or ownership only (held)"| L2["L2 · registries: PyPI, npm, crates.io<br/>each candidate judged against its own registry"]
  L2 --> GATE{"identity gate"}
  GATE -->|"a stronger candidate<br/>could not be read"| BLOCK["refuse: nothing weaker<br/>is accepted in its place"]
  GATE -->|passes| OK["verified; ecosystem and release<br/>are the winner's own"]
  GATE -->|"none"| L3["L3 · name shapes<br/>L4 · evidence the failed pages gave away<br/>L5 · search, the named registry only"]
  L3 --> GATE
  OK --> REPO["a winning repository gives way<br/>to the docs site it declares, if that passes"]
  REPO --> ED{"language asked for?"}
  ED -->|yes| EDN["that language's edition:<br/>segment / prefix / host swapped, or the<br/>front page's language section, or its registry"]
```

The whole ladder is capped at 40 requests. **Strong signals** identify a
project rather than describe one: `own-domain`, `docs-host`, `install:`,
`repo-backlink`, `registry-agreement`, `repo-identity`. Mention counts are
corroboration, never proof. Mentions and install lines are counted in a
page's visible text over its first 600 KB; links are judged over its first
40 KB only (L5 — a footer's GitHub link is not identity).

**An edition is found, never assumed** (`_switch_to_language`). From where the
resolved page *lands*, the asked language's edition is tried beside it —
`/oss/python/` → `/oss/javascript/`, `playwright.dev/docs/` →
`playwright.dev/python/docs/`, `python.x.com` → `js.x.com`, or the section a
many-language front page links to — and taken only when its own code is in
that language (`languages.written_for`, measured on extracted content, never
raw HTML) or the site files it under that language's name. Failing that, the
language's registry is resolved and switched the same way. Otherwise the
answer says no edition was found.

Rules the gate learned the hard way, each after being caught live:

- **A code host is nobody's own domain** (`mojo.dev` → a Java library called
  procrastination).
- **A language owns its `lang` domain** (`zig`, `nim`).
- **A first-come `<name>.github.io` and a country-code mirror do not outrank
  the project's own domain** (`tensorflow`, `pytorch`).
- **A candidate is judged against its own registry**: `click` on PyPI used to
  be tested against npm's ecosystem and npm's repository, and stored under
  npm's version, `0.1.0` (fixed 2026-09-17; `System Files/Issues.md` R6).
- **A candidate that could not be read blocks anything weaker.** With
  click.palletsprojects.com answering 429, `click` resolved to a Kubernetes
  CLI's wiki and `requests` to a Rust crate — both verified, because both do
  document a project of that name. A registry-nominated candidate whose fetch
  failed for a reason that says nothing about it (429, 5xx, no answer) is
  *unexamined*, and the ladder stops there with a refusal that is not cached
  (fixed 2026-09-20).
- **Evidence links are anchors only, decoded, and judged by their own
  words**: `fonts.googleapis.com` is not an API reference.
- **A language is not a package** (L4): `go` reached `docs.rs/go`, a Rust
  crate, because `go.dev` stood on ownership alone and the only registry
  that knew the word answered. Languages and runtimes resolve to their own
  manuals first.
- **Search keeps to the registry named** (L2): `langgraph` on npm reached
  `docs.rs/rust-langgraph`. A search hit is judged against its own registry
  entry only when its package *is* the name.
- **A repository is where the code is, not the documentation** (L3): a
  winning GitHub repository gives way to the site it declares as its
  homepage, if that site passes the gate.

### The cache is evidence, and evidence goes stale

A resolution is filed for 30 days, a refusal for 7, both stamped with the
`RULES` revision that decided them (8 since 2026-09-24) and discarded on
recall if the stamp no longer matches; `tests/test_rules_stamp.py`
fingerprints the functions that decide an answer so the stamp cannot be
forgotten. Each language asked for is filed under its own key
(`langgraph@javascript`), and `forget_resolution` clears them all. Two things are never filed: a refusal reached without reading
the candidates (the network talking, not the name), and a refusal for want of
examination (a rate limit talking).

---

## 8. Storage

```mermaid
graph TD
  T["technology, e.g. click"] --> V1["version 8.5.0"]
  T --> V2["version 8.1"]
  V1 --> P["pages, in order: url · title · content"]
  P --> S["sections: split on h2/h3, heading paths kept"]
  V1 --> M["coverage: expected · acquired · whole · strategy"]
```

Two versions of one library are kept side by side, because they contradict
each other. `core/versions.py` orders labels so "latest" means newest rather
than most recently fetched; a release number outranks a harvest date, and a
date only appears when a harvest failed to find a number.

Two other things that are not the whole manual are filed as technologies of
their own, so a later "already stored" can never hand one back for the other:
a language's edition, when the site files it apart, as `<name>-<language>`
(`langgraph-javascript`), and a harvest kept to a topic as `<name>-<topic>`
(`go-web-development`). Name matching (`stored_name`) will not stretch a
prefix across the hyphen, so `langgraph` never answers with
`langgraph-javascript`.

| | `FileStore` | `PostgresStore` |
|---|---|---|
| When | default; `DOCSFORGE_DB` unset or unreachable (and it says so) | `DOCSFORGE_DB` set |
| Search | substring, then any-term | full-text, ranked; `page.search` over the first 300,000 characters, `section.search` over the rest |
| Durability | `.partial` file until settle | a row per page, written as fetched |

Both stores search for every word first and fall back to any of them. The
fallback used to come back dressed as a match; the tool's header now names
the words no returned passage contains.

---

## 9. A harvest outlives the call that started it

A tool call returns at the deadline (25 s) with a harvest id; the harvest
runs on in the process that started it. Its status is *published*, not held:

```mermaid
flowchart LR
  subgraph A["process that started it"]
    J["job thread"] --> H["heartbeat, 2 s"]
  end
  H --> F["harvest record<br/>~/.docsforge/harvests/id.json<br/>and the database"]
  F --> LKB["list_knowledge_base / harvest_status,<br/>from any process"]
```

This is not a job queue. Nothing is resumed from a record; a harvest still
dies with its process, and a record whose heartbeat stopped is reported
**stalled**, never running. Hosted, the process is the request, so a harvest
past the deadline is *lost* and the answer says so; a long harvest is done
from a long-lived DocsForge pointed at the same database, and is readable
hosted at once. Offline, `DOCSFORGE_HARVEST_LINGER=0` makes the process wait
for every harvest it started before exiting.

---

## 10. Observability

Two records for two readers. `tools/tracing.py` is a nested, incrementally
emitted event log — every tool call is a root stage; resolution, harvesting
and each page are stages beneath it; events are keyed by id so 230 progress
ticks are one row counting up. `tools/applog.py` is rotating JSONL at
`logs/docsforge.log` (offline: `offline/logs/`): one line per request, tool
call, trace event, harvest transition and error. Neither fabricates a
percentage where the backend has no denominator.

---

## 11. Boundaries

- Fetches to private, loopback, link-local and reserved addresses are
  refused, in every spelling, at every redirect hop, and inside the browser
  when rendering (`page.route` interception).
- `save_docs` cannot write outside `DOCSFORGE_OUT_ROOT`.
- Tool results are capped (`DOCSFORGE_MAX_CHARS`, 200,000 by default) with a
  marker naming what was omitted; storage is unbounded.
- Deletion is opt-in (`DOCSFORGE_ALLOW_DELETE`); offline turns it on because
  the store is disposable.
- Rendered Markdown in the local panel is sanitised with `nh3`.

## 12. What this architecture refuses

- **Guessing a URL for a name it could not confirm**, and accepting a weaker
  candidate for one it could not read.
- **Reporting coverage it did not measure.** `complete`, `incomplete`,
  `unknown` and now `refused` are distinct, and none is rendered as success.
- **Restructuring what a publisher wrote.** Relevance is applied at read
  time; ingestion never reshapes what was published. It does drop the page's
  furniture — permalink marks, copy buttons, "Edit this page" — and resolve
  links that would point nowhere once stored, because none of that is what
  was published (`Design.md` §16).
- **Taking one list for the whole.** A curated `llms.txt`, a manifest that
  covers the API and not the guides, a stale sitemap: each is checked against
  the others and the links on the pages themselves.
- **Broadening a scoped request, or narrowing one silently.** A section, an
  edition and a topic are each kept to exactly, and whatever a topic left out
  is listed.
- **Letting one dead page end a run** — or one 429 pretend the page was empty.
