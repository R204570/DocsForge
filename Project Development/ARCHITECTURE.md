# Architecture

Recorded from the build, not from intention. Where the code and this file
disagree, the code is right and this file is stale.

Written against 860 passing tests, 59 skipped behind opt-in gates — live
network, Postgres, browser rendering.

---

## 1. One engine, three surfaces

The extraction engine is a library. Nothing about it knows whether a CLI, an
MCP client or a browser is asking, which is what keeps the three from drifting
apart — an MCP client and the chat panel return byte-identical results because
they call the same function with the same arguments.

```mermaid
graph LR
  CLI["CLI<br/>docsforge"]
  MCP["MCP server<br/>docsforge-mcp"]
  WEB["Web panel<br/>server/app.py + static/"]

  TOOLS["tools/forge_tools.py<br/>the twelve tools"]
  ENGINE["core/engine.py<br/>detect · fetch · extract · harvest"]
  STORE["store/kb_store.py<br/>FileStore | PostgresStore"]

  CLI --> ENGINE
  MCP --> TOOLS
  WEB --> TOOLS
  TOOLS --> ENGINE
  TOOLS --> STORE
  ENGINE --> STORE
```

`tools/forge_tools.py` is the seam. It owns argument shaping, the trace wrapper, and
the human-readable result strings; `core/engine.py` owns the web and knows
nothing about tools.

---

## 2. Module map

Everything is one package, `docsforge/`, with a subpackage per responsibility;
the only script outside it is `main.py`, which starts the MCP server. Paths
below are relative to `docsforge/`.

```
main.py                  python main.py [--http]      -> the MCP server
docsforge/__main__.py    python -m docsforge <URL>    -> the CLI
docsforge/core/          acquisition, identity and scope
docsforge/store/         kb_store
docsforge/tools/         the tool layer, background jobs, tracing, logs
docsforge/server/        mcp_server (+ site/: the public pages it serves over HTTP),
                         app (+ static/: the local web chat — never hosted)
docsforge/providers/     model backends for the web chat
scripts/                 bench harnesses and live smoke drivers
```

Hosted, the process is `main.py --http` in the `Containerfile`: `/` , `/tools`
and `/connect` are public pages, `/health` is for the platform, and `/mcp` is
the tool surface behind a bearer token (`DOCSFORGE_MCP_TOKEN`). The gate is a
small ASGI layer in `mcp_server.py` rather than the SDK's OAuth machinery,
because that has to be configured when the server object is built — which
would put a token requirement on every local `--http` run too. A non-loopback
bind without a token is refused. The web chat is not in the hosted process.

```mermaid
graph TD
  subgraph Surfaces
    APP["server/app.py<br/>HTTP + SSE"]
    MCPS["server/mcp_server.py<br/>started by main.py"]
  end

  subgraph Tools
    FT["tools/forge_tools.py"]
    HJ["tools/harvest_jobs.py<br/>background harvests"]
  end

  subgraph Acquisition
    DF["core/engine.py<br/>detect · crawl · harvest"]
    LF["core/llmsfinder.py<br/>shape · links · density"]
    MAN["core/manifests.py<br/>generator manifests"]
    VER["core/versions.py<br/>release ordering"]
  end

  subgraph Identity
    RES["core/resolver.py<br/>name to URL"]
    INST["core/instrument.py<br/>what a probe revealed"]
    REA["core/reasoning.py<br/>optional model veto"]
  end

  subgraph Scope
    FED["core/federation.py<br/>a technology is many corpora"]
    SEL["core/selection.py<br/>ask, never guess"]
    OBS["core/observation.py"]
  end

  subgraph Storage
    KB["store/kb_store.py"]
    PAS["core/passages.py<br/>read-time relevance"]
  end

  subgraph Observability
    TR["tools/tracing.py<br/>event log"]
    AL["tools/applog.py<br/>JSONL request log"]
  end

  APP --> FT
  MCPS --> FT
  FT --> HJ
  FT --> DF
  FT --> RES
  FT --> TR
  APP --> TR
  APP --> AL
  HJ --> DF
  DF --> LF
  DF --> MAN
  DF --> VER
  DF --> FED
  RES --> INST
  RES --> REA
  RES --> VER
  FED --> SEL
  FED --> OBS
  DF --> KB
  KB --> PAS
```

`scripts/measure.py` sits outside this — it is a bench harness, not a runtime path.

---

## 3. A chat turn, end to end

The server is stateless. The browser holds the conversation and posts it back
every turn, so a reload loses nothing the browser still has and the server
never has to reconcile two ideas of the same chat.

```mermaid
sequenceDiagram
  participant B as Browser
  participant A as server/app.py
  participant P as provider
  participant T as forge_tools.run_tool
  participant X as Trace

  B->>A: POST /api/chat  (whole conversation)
  A->>X: create trace, id returned in the first SSE frame
  B->>A: GET /api/trace/{id}   (second connection, SSE)
  A->>P: stream the turn
  P-->>A: tool_start(name, args)
  A->>T: run_tool
  T->>X: Stage opens, args sanitized
  T-->>X: nested stages as work proceeds
  X-->>B: trace / trace_end frames, incrementally
  T-->>A: result text
  A-->>P: tool result
  P-->>A: tokens
  A-->>B: answer frames
```

Two connections, deliberately. The answer stream and the trace stream have
different lifetimes: a harvest that outlives the 25-second deadline keeps
emitting trace events long after the answer has been handed back, so binding
them to one connection would either truncate the trace or hold the answer
hostage to it.

`run_tool` therefore **detaches** rather than closes a trace whose work moved
to a background thread. Closing it on return raced `harvest_jobs` and cut the
event log off mid-harvest.

---

## 4. Two pathways into a harvest

This is the shape of the whole acquisition problem, and the distinction the
system gets wrong most easily.

`llms.txt` and `llms-full.txt` are published for the **current** release. A
site publishes one of them, at its origin, describing what it ships today.
That makes them the best possible answer to "give me the latest docs" and a
poor answer to "give me 0.9" — so the request decides the pathway, not the
file.

```mermaid
flowchart TD
  START["harvest(url, version?)"] --> ASK{"was a release named?<br/>opts.version, or a version in the path"}

  ASK -->|no| SANITY["sanity check:<br/>does the site publish llms.txt?"]
  SANITY -->|yes| USE["use it — it is the current release,<br/>which is what was asked for"]
  SANITY -->|no| CRAWL1["fall down the ladder<br/>sitemap, then crawl"]

  ASK -->|yes| ROOT{"is the published file<br/>at the site root?"}
  ROOT -->|no| ASPUB["already scoped below the root —<br/>use as published"]
  ROOT -->|yes| DECL{"does it declare a version,<br/>and does it match?"}
  DECL -->|matches| WHOLE["take it whole"]
  DECL -->|"no, but it files links<br/>under that release"| NARROW["narrow to those links,<br/>drop the root prose"]
  DECL -->|"cannot show it documents<br/>the release asked for"| CRAWL2["ignore it — crawl that<br/>version's own docs instead"]
```

Three details that were each a defect before they were a rule:

- **Never broaden a scoped request.** `docs.modular.com` publishes one
  `llms.txt` for Modular Cloud, so a request for `/mojo/` came back as API-key
  and billing documentation. No release was involved — just a file about a
  different product on the same host.
- **Narrowing for a release drops the root prose; narrowing for a section keeps
  it.** The file's own prose belongs to the release it was published for. When
  the reason for narrowing is a *section*, the file already showed it covers
  that section and its overview is the same site's words about it. Conflating
  the two threw away 1.1 MB of real documentation on mojolang.org.
- **Where the file sits decides this, not how we got its URL.** The gate used
  to also require that *we* had probed for the file, on the reasoning that a
  URL the caller typed is a URL the caller meant. The caller stopped typing
  it — resolution now hands `learn_technology` whatever it found — and asking
  for Mojo 0.9 went straight past the release check.

---

## 5. The acquisition ladder

Tried in order, stopping at the first rung that answers. The ladder's claim is
that more laps never mean a lower bar.

```mermaid
flowchart TD
  L1["1 · llms-full.txt<br/>one request, stored whole"]
  L2["2 · llms.txt with Markdown links<br/>one request per listed page, no HTML"]
  L3["3 · llms.txt with HTML links<br/>the site's own page list, extracted as usual"]
  L4["4 · generator manifest<br/>search_index.json, objects.inv"]
  L5["5 · sitemap.xml, filtered to the docs section"]
  L6["6 · scoped crawl"]

  L1 -->|absent| L2 -->|absent| L3 -->|absent| L4 -->|absent| L5 -->|absent| L6
```

**A file the site published about itself outranks anything inferred about it.**
A sitemap is a hint addressed to crawlers; `llms.txt` is a statement addressed
to us.

**An index is not documentation.** A file of 229 links is a table of contents.
Storing it *as* the corpus is the failure the README warns about, so shape is
classified by link density before anything is stored: an index is almost
entirely link characters, a dump almost entirely prose, and a hybrid is
treated as both — store the prose, follow the links, count both separately.

Every rung on this ladder now pays the same politeness. `_crawl_html` has
spaced its requests per host since the beginning; the manifest path ignored
`opts.delay` outright and sent 211 back-to-back requests at mojolang.org,
which is how a small documentation host learns to refuse us.

### Coverage is three separate claims

Root acquisition, manifest acquisition and corpus completeness are recorded
apart, because one succeeding says nothing about the others.

| Claim | Means |
|---|---|
| `expected` | unique **actionable** pages the manifest lists — not its raw link count |
| `acquired` | how many of those came back |
| `whole` | `acquired == expected`, and never true of a partial corpus |
| `failed_urls` | each failure with a normalized URL and a coarse category, so a retry can operate on the failed subset |

`expected` is measured against what the site says exists, not against the slice
a page limit left behind — otherwise cutting a harvest short would make it
*look* complete.

---

## 6. Name resolution

The hard part is not finding a candidate. It is refusing a plausible wrong one:
a harvest of the wrong project, stamped `verified`, is worse than no harvest,
because the caller has been given a reason to stop checking.

```mermaid
flowchart TD
  N["name"] --> MEM{"remembered?"}
  MEM -->|"yes, and written<br/>under current RULES"| DONE["done, no requests"]
  MEM -->|no| L1["L1 · the project's own domain<br/>name.dev/io/org/com, plus namelang.org and name-lang.org"]
  L1 --> L2["L2 · package registries<br/>PyPI, npm, crates.io"]
  L2 --> L3["L3 · name shapes<br/>concat, product.vendor, vendor/product, docs portals"]
  L3 --> L4["L4 · registry search"]
  L1 --> GATE
  L2 --> GATE
  L3 --> GATE
  L4 --> GATE
  GATE{"identity gate<br/>two strong signals,<br/>or one strong plus the name"}
  GATE -->|passes| VERIFIED["verified"]
  GATE -->|fails| CANDS["returned as an unconfirmed candidate"]
```

The whole ladder is capped at 40 requests.

**Strong signals** identify a project rather than describe one: `own-domain`,
`docs-host`, `install:`, `repo-backlink`, `registry-agreement`,
`repo-identity`. Mention counts are deliberately excluded — corroboration,
never proof.

Three rules the gate learned the hard way:

- **A code host is nobody's own domain.** `mojo.dev` redirects onto
  `github.com/gdejohn/procrastination`, a Java library — Maven plugins are also
  called "mojos". Crediting the arrival made that a *verified* answer for Mojo,
  off two structural signals and not one mention of the name.
- **A language owns its `lang` domain.** Names that are common words take the
  suffix for exactly that reason. Nothing probed them, so the highest-signal
  source never saw the site: `zig` resolved to an npm templating library and
  `nim` to an unrelated repository.
- **Asked must be a prefix of found.** Asking for `2` is answered by `2.11`;
  asking for `2.5` is **not** answered by `2`. A loose comparison is how a
  request for one release gets handed another's documentation.

### The cache is evidence, and evidence goes stale

```mermaid
flowchart LR
  R["resolution"] --> W{"was anything<br/>actually learned?"}
  W -->|"every candidate refused<br/>with 'could not be read'"| DROP["file nothing —<br/>that is the network talking,<br/>not the name"]
  W -->|yes| FILE["file it, stamped with<br/>the RULES that decided it"]
  FILE --> READ{"recall"}
  READ -->|"stamp matches, within TTL"| SERVE["serve it"]
  READ -->|"stamp differs"| REDO["discard — decided under<br/>rules this build no longer applies"]
```

Both halves were written after being burned. A NAT64 outage refused every
candidate as a "private address"; six were found, none could be read, and the
refusal was cached for seven days while the cause was fixed within one. Later
the *wrong* answer was filed as a success, where a 30-day TTL would have
outlived its own fix by four weeks.

---

## 7. Storage

```mermaid
graph TD
  T["technology<br/>e.g. mojo"] --> V1["version 1.0.0"]
  T --> V2["version 0.9"]
  V1 --> P["pages, in order<br/>url · title · content"]
  P --> S["sections<br/>split on h2/h3, heading paths kept"]
  V1 --> M["coverage<br/>expected · acquired · whole · strategy"]
```

Two versions of one library are kept side by side rather than one overwriting
the other, because they contradict each other. `core/versions.py` orders labels so
"latest" means newest rather than most recently fetched — a release number
always outranks a harvest date, because the date only ever appears when a
harvest failed to find a number.

Two backends behind one protocol:

| | `FileStore` | `PostgresStore` |
|---|---|---|
| Default | yes, zero keys | set `DOCSFORGE_DB` |
| Search | substring | full-text, ranked |
| Pages | Markdown files | rows, with `page.search` and `section.search` |

Pages stream into a `.partial` file that no reader can see; the finished file
is assembled at settle time. A crash leaves the `.partial` on disk rather than
a half-written corpus that looks whole.

The storage chip in the UI names which backend answered, because Postgres ranks
search and a folder of files cannot — hiding which one you are reading would be
a lie about the quality of the answer.

---

## 8. Observability

Two independent records, for two different readers.

```mermaid
graph TD
  TURN["Turn"] --> TC["tool call<br/>root Stage, args sanitized"]
  TC --> ST1["stage · resolving"]
  TC --> ST2["stage · harvesting"]
  ST2 --> E1["event · page fetched"]
  ST2 --> E2["event · page fetched"]
  ST2 --> E3["event · failed, with the error<br/>attached to the operation that caused it"]
```

**`tools/tracing.py`** is what the user sees: a nested, incrementally emitted event
log rendered under each tool row in the panel, expandable by clicking the row.
Every event carries an id, a parent id, a lifecycle state — `queued`,
`running`, `completed`, `failed`, `skipped`, `cancelled` — a timestamp, and
optional input, result and error.

Design constraints that are load-bearing:

- **Events are keyed by id, not appended.** A stage re-emitted with the same id
  updates in place, so 230 progress ticks are one row that counts up rather
  than 230 rows.
- **Emission is incremental, never buffered to the end.** A harvest that takes
  three minutes is watchable for three minutes.
- **No fabricated percentages.** A percentage is shown only where the backend
  has a defensible denominator — a manifest length is one; a crawl frontier is
  not.
- **Sanitized at the boundary**, with strings clipped at 4,000 characters and
  outputs at 20,000, and the omission stated rather than silent.
- Low-level events are **not** persisted into chat history. The trace explains a
  turn while you are looking at it; the durable record of a harvest is the
  knowledge-base entry it wrote.

**`tools/applog.py`** is what a developer reads: rotating JSONL at `logs/docsforge.log`
— one line per HTTP request, one per tool call, per turn, per trace event, per
harvest transition, per error. Gitignored, and independent of whether a browser
was watching.

### A harvest outlives the turn that started it

The trace explains one tool call while you are looking at it. A harvest past
the 25-second deadline is a different problem: it outlives the call, and with
the `claudecode` provider it outlives the *process*, because the CLI launches
`main.py` — the MCP server — itself and every turn gets a fresh subprocess.

So job status is published rather than held in memory:

```mermaid
flowchart LR
  subgraph P1["process A · started the harvest"]
    J["job thread"] --> H["heartbeat, 2s"]
  end
  H --> F["~/.docsforge/harvests/&lt;id&gt;.json<br/>phase · pages · expected · state"]
  J -->|"phase change"| F

  subgraph P2["process B · never heard of it"]
    LKB["list_knowledge_base"]
    API["GET /api/harvests"]
  end
  F --> LKB
  F --> API
  API --> UI["tracker card,<br/>on every page"]
```

**This is not a job queue and must not become one.** Nothing is ever resumed
from a record. A harvest still runs on a daemon thread and still dies with its
process — what changed is that we notice. A record still claiming to run whose
heartbeat stopped is reported **stalled**, never "running": a dead process
cannot be distinguished from a slow one except by its silence, and continuing
to call silence "working" is the failure this exists to prevent.

Three things fall out of the record being shared:

| | Before | Now |
|---|---|---|
| `list_knowledge_base` | promised a progress line that a different process could not produce | reports every harvest in flight, whoever started it |
| A duplicate call | `_still_harvesting` *asked* the model not to; langchain was crawled twice | `learn_technology` checks, and returns the running job's status |
| A harvest id | a per-process counter, so two harvests were both `langchain-1` | numbered above whatever is already on disk |

A phase change publishes immediately; page counts wait for the beat. A phase
shorter than the heartbeat was otherwise invisible — a two-second `resolving`
appeared nowhere at all — while page counts tick hundreds of times and the
beat is what bounds that to one write every couple of seconds.

---

## 9. Boundaries

- **Fetches to private, loopback, link-local and reserved addresses are
  refused**, with NAT64-mapped addresses unwrapped before the check —
  `64:ff9b::/96` is a public address wearing an IPv6 costume, and refusing it
  broke every harvest on a NAT64 network.
- `save_docs` cannot write outside its output root.
- Rendered Markdown is sanitized with `nh3`, because it mixes model output with
  scraped HTML.
- Tool results are capped at 60,000 characters with an explicit truncation
  marker.
- JS rendering is opt-in and slow; crawling is bounded by a per-host delay, a
  per-host concurrency cap, and a page cap.

---

## 10. What this architecture refuses

- **Guessing a URL for a name it could not confirm.** An unconfirmed resolution
  returns its candidates and says why each failed.
- **Reporting coverage it did not measure.** `complete`, `incomplete` and
  `unknown` are distinct, and `unknown` is never rendered as success.
- **Restructuring what a publisher wrote.** A document published whole is
  stored whole. Read-time relevance narrows what is handed back; ingestion
  never reshapes what was published.
- **Letting one dead page end a run.** Partial failure is an operating
  condition, recorded per URL, not an exception.
