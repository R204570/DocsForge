# DocsForge

**Universal software documentation → clean Markdown for LLMs.**

## 🚀 In production — feedback and contributors can support

**Found a bug, a bad extraction, or something that could work better?**
[Open an issue](https://github.com/R204570/DocsForge/issues) with as much detail
as you can. Every report makes it more reliable.

**Want to contribute?** Email **rajpatel9408019@gmail.com** with the subject
**DocsForge Contribution Proposal**, and include:

- Your **résumé/CV** and **GitHub** — this is what I read to get a picture of your work
- Your experience with crawlers, MCP tools or AI agents
- What you'd like to work on, and any ideas you already have

Useful ground: crawling and scraping, documentation extraction, MCP tools, AI
agents, backend and developer tooling. Bug fixes and documentation count as much
as new crawling strategies.

— **Raj Patel**, creator and maintainer

---

DocsForge points at any documentation source, figures out *what kind* of source it is, and extracts it into tidy, LLM-ready Markdown. Feed it a docs site, an OpenAPI spec, a GitHub repo, a sitemap, or a raw Markdown file — it detects the format and handles each one appropriately.

It ships in three forms, all sharing one extraction engine:

| Surface | File | What it's for |
|---|---|---|
| **CLI** | `python -m docsforge` (`docsforge/core/engine.py`) | One-shot scraping into `.md` files. |
| **MCP server** | `main.py` (`docsforge/server/mcp_server.py`) | Give any MCP client (Claude Code, Claude Desktop, your agent) live docs-fetching tools. |
| **Web chat** | `python -m docsforge.server.app` + `docsforge/server/static/` | A chat UI over any of six providers that fetches docs and answers in rendered Markdown, plus DocsStore for browsing what has been harvested. |

```
                   ┌─────────────────┐
   CLI ───────────▶│                 │
                   │  core/engine.py │  detect → extract → Markdown
   MCP client ────▶│ tools/forge_tools│
                   │                 │
   Web chat ──────▶│                 │
                   └─────────────────┘
```

`docsforge/tools/forge_tools.py` defines each tool exactly once. `docsforge/server/mcp_server.py` generates the MCP surface from those definitions; `docsforge/server/app.py` hands the same schemas to whichever provider is selected. An MCP client and the web chat therefore run identical code, and neither can drift from the other.

```
main.py                 the only script in the root: starts the MCP server
docsforge/              the package
  core/                 engine (fetch/extract/crawl), resolver, llmsfinder, versions,
                        manifests, federation, selection, passages, reasoning,
                        observation, instrument
  store/                kb_store — Markdown files or Postgres
  tools/                forge_tools, harvest_jobs, tracing, applog
  server/               mcp_server, app (+ static/)
  providers/            one model backend per file
scripts/                measurement harnesses and live smoke drivers
benchmarks/             the live suite: every tool measured over MCP against a
                        running DocsForge — the hosted one, or `--offline` on a
                        local database with harvests run to the last page
tests/                  the offline suite
```

## Features

- **Automatic source detection** — probes the URL (and content, when needed) to pick the right extraction strategy.
- **Supported sources:**
  - `llms.txt` / `llms-full.txt` — the LLM-native docs standard (passthrough).
  - **OpenAPI / Swagger** (JSON or YAML) → readable API reference with endpoint tables, params, request bodies, and response codes. Local `$ref`s are resolved and path-level parameters are applied to every operation.
  - **sitemap.xml** → structured crawl of every listed page, including sitemap indexes.
  - **GitHub repos** → README + all Markdown under `/docs` via the GitHub API.
  - **Generic HTML docs sites** → readability-style extraction (strips nav/footer/ads, keeps main content).
  - **Raw Markdown / plaintext** → passthrough with cleanup.
- **Bare-domain probing** — auto-checks for `llms.txt` at the root before falling back to HTML.
- **Optional site crawling** (`--crawl`) with same-host link following, page limits, and asset filtering.
- **JS rendering** (`--js`) via Playwright, reusing a single browser across the whole run.
- **Single-file output** (`--single-file`) to concatenate everything into one `.md`.
- **Provenance headers** — every output file records its source URL, type, and scrape time.
- **Durable as it goes** — a page is stored before the next is fetched, so an interrupted crawl keeps what it had, and a page the store refuses costs that page rather than the whole harvest.
- **Concurrent fetching** — pages overlap within a per-host politeness cap, so a crawl waits once instead of once per page, and returns the same pages in the same order a sequential run would.

## Installation

```bash
pipx install docsforge          # the `docsforge` CLI and `docsforge-mcp` server
```

Or from a clone, which is what you want if you intend to change anything:

```bash
git clone https://github.com/R204570/DocsForge.git
cd DocsForge
pip install -e .                # or: pip install -r requirements.txt
```

Optional extras, each pulling only what it needs:

```bash
pip install -e ".[web]"         # the chat panel (FastAPI, sanitiser, renderer)
pip install -e ".[postgres]"    # store harvests in Postgres with a text index
pip install -e ".[js]"          # then: playwright install chromium
pip install -e ".[providers]"   # every model SDK; or pick one: [claude], [groq]
pip install -e ".[dev]"         # everything, plus pytest
```

`requirements.txt` is the exact, reproducible set used by CI. `pyproject.toml`
states the version *range* each dependency is known to work across. Four
packages are pinned outright — `beautifulsoup4`, `markdownify`, `mcp` and
`nh3` — because a float in any of them changes extraction, output or
sanitising with no code change of ours.

## 1. CLI

```bash
python -m docsforge <URL> [options]
```

```bash
# A docs site (auto-detected)
python -m docsforge https://docs.stripe.com

# An OpenAPI / Swagger spec → API reference tables
python -m docsforge https://petstore3.swagger.io/api/v3/openapi.json

# A GitHub repo → README + /docs
python -m docsforge https://github.com/tiangolo/fastapi

# Crawl a docs site, up to 50 pages
python -m docsforge https://docs.example.com --crawl --max-pages 50

# JS-rendered site
python -m docsforge https://site.com --js

# Combine everything into one Markdown file
python -m docsforge https://site.com --single-file
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-o`, `--out` | `./docs_md` | Output directory. |
| `--crawl` | off | Follow same-host links from the start URL. |
| `--max-pages` | `25` | Max pages to fetch (crawl / sitemap / repo docs). |
| `--js` | off | Render JavaScript with Playwright. |
| `--delay` | `0.4` | Seconds to wait between requests when crawling. |
| `--single-file` | off | Write one combined `.md` instead of per-page files. |
| `--force` | — | Skip detection and force a strategy: `llms_txt`, `openapi`, `sitemap`, `github`, `raw_text`, `html`. |
| `--allow-private` | off | Permit private/loopback hosts (see [Security](#security)). |
| `-q`, `--quiet` | off | Suppress progress output. |

### As a library

```python
from docsforge.core.engine import forge, Options

docs = forge("https://docs.example.com", Options(crawl=True, max_pages=10))
for d in docs:
    print(d.title, len(d.markdown))
```

## 2. MCP server

```bash
python main.py                       # at a terminal: the site + /mcp on http://127.0.0.1:8765
DOCSFORGE_MCP_TOKEN=… python main.py --http --host 0.0.0.0   # shared: token required
```

One command, two callers. Typed at a terminal, `main.py` serves HTTP: the
public site — `/` (what DocsForge is), `/tools` (the reference), `/connect`
(client setup) — plus `/health` and the MCP endpoint at `/mcp`. Launched by an
MCP client, which always talks over pipes, the same command speaks MCP on
stdin/stdout instead; `--http` and `--stdio` force either (a service with no
terminal, like the Containerfile, passes `--http`). Only `/mcp` is gated: clients send `Authorization: Bearer <DOCSFORGE_MCP_TOKEN>`,
and without it the server answers 401. Binding a non-loopback address with no
token is refused outright (`DOCSFORGE_MCP_INSECURE=1` overrides, for a network
you own).

**OAuth, for clients that cannot send a token.** ChatGPT's connectors
authenticate with OAuth or not at all, so with a token configured the server
is also an OAuth 2.1 authorization server (`docsforge/server/oauth.py`) —
discovery at `/.well-known/…`, dynamic client registration, PKCE — whose
**login is the DocsForge token**: the client opens a DocsForge sign-in page,
the person pastes the token once, and the client walks away with an access
token that `/mcp` accepts beside the bearer header. Everything the flow needs
to remember is carried in signed, expiring blobs rather than stored, which is
what lets it run on a stateless host and off the database; rotating the token
revokes every client and token at once. Discovery names absolute URLs, so the
server has to know where it lives: `DOCSFORGE_PUBLIC_URL`, or on Vercel the
production domain the platform provides. Without an HTTPS (or loopback)
address OAuth is not offered and the bearer token still works.

The web chat in `docsforge/server/app.py` is not part of this
process and nothing routes to it. See [Deploying](#deploying).

Register with Claude Code:

```bash
claude mcp add docsforge -- python /absolute/path/to/DocsForge/main.py
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "docsforge": {
      "command": "python",
      "args": ["E:/DocsForge/main.py"]
    }
  }
}
```

### Tools exposed

| Tool | Arguments | Returns |
|---|---|---|
| `detect_source_type` | `url` | Which strategy the URL would use — a cheap probe. |
| `fetch_docs` | `url`, `crawl`, `max_pages`, `js`, `force` | The extracted Markdown. |
| `save_docs` | `url`, `out_dir`, `crawl`, `max_pages`, `js`, `force`, `single_file` | Paths written to disk. |
| `harvest_docs` | `url`, `name`, `max_pages`, `js`, `scope`, `version` | Learns a whole technology from one URL and stores it. Unlimited by default. Returns a summary. |
| `learn_technology` | `name`, `version`, `ecosystem`, `max_pages`, `js`, `intent`, `corpora`, `strict` | **Learns a library from its name alone — no URL.** Resolves, verifies, harvests, stores. |
| `harvest_status` | `harvest`, `wait` | How a harvest is getting on — phase, pages of how many, elapsed — and how it ended. Omit `harvest` for every harvest in flight; `wait` lets it run up to the deadline before answering. |
| `find_docs` | `name`, `ecosystem` | Where a name resolves to, with evidence. Harvests nothing. |
| `scan_project` | `path`, `unknown_only` | A project's dependencies, versions, and which are documented. |
| `search_knowledge_base` | `query`, `technology`, `version`, `limit` | Ranked search across every stored page at once. |
| `list_knowledge_base` | — | What has already been harvested, and which versions of each. |
| `read_knowledge_base` | `name`, `section`, `version` | Reads stored docs back, optionally only matching pages. Defaults to the newest version. |
| `forget_resolution` | `name` | Forgets where a name previously resolved to, so the next lookup starts over. Deletes no documentation. Omit `name` to clear all. |
| `forget_selection` | `name` | Forgets which corpora were chosen for a technology, so it asks again. Deletes no documentation. |

`learn_technology` also takes `intent`, `corpora` and `strict`; `search_knowledge_base`
takes `kind`. See **Serving a purpose** below.

`forget_documentation` also exists but is opt-in behind `DOCSFORGE_ALLOW_DELETE=1`:
deleting a harvest is the one irreversible thing here, and a model that has just
mis-resolved a name is not the caller you want holding that lever.

Results handed to a model are capped at `DOCSFORGE_MAX_CHARS` (60k default) with an explicit truncation marker.

## 3. Web chat

```bash
cp .env.example .env      # add a key for ONE provider — or none at all
python -m docsforge.server.app   # http://127.0.0.1:8000
```

Three surfaces, one window: the chat at `/`, **DocsStore** at `/library`, and
everything that would otherwise need explaining at `/docs`. Charcoal on black
with a single accent; nothing on the home screen but the input.

The sidebar opens with the panel button or `Ctrl`+`B`, and holds your past
conversations. They live in your browser — forty most recent, Markdown only,
nothing uploaded — so **New chat** no longer throws work away.

Every tool call the model makes is listed above the answer it produced — what
was fetched, what kind of source it was, and how much came back — so an answer
built from a page that was actually read does not look like one that was not.
**Click any of those rows** to open its execution trace: the exact input the
tool was given, the stages it went through, and what each returned, nested and
arriving live rather than assembled at the end. A three-minute harvest is
watchable for three minutes, a failure is attached to the operation that caused
it, and where a page had to be clipped the row says how much it left out. There
are no invented percentages — a progress figure appears only where there is a
real denominator to divide by, such as a manifest's own page count.
A harvest too big to finish inside the request keeps going in the background,
and a card in the corner shows what it is doing — the phase, the page count,
and how long it has been going — on the chat and in DocsStore alike. Where the
source states how many pages it has, the card fills a bar; where it does not —
a crawl discovers its frontier as it walks it — you get elapsed time and a page
count and no invented percentage. DocsStore refreshes itself the moment one
lands. The same transitions go to `logs/docsforge.log` and to the console, so
a harvest is watchable without a browser open.

Each answer carries **Copy / Download .md / Edit**, because the Markdown is
something you keep, not just something you read. The send button becomes a stop
button while an answer streams, and stopping keeps what already arrived.

### Providers

Pick one from the model control at the top right; unconfigured ones are
disabled and say why. The choice rides on each request, so when one provider
hits its daily cap you switch and keep going mid-conversation.

| Provider | Key | Default model | Notes |
|---|---|---|---|
| **Claude Code** | *none* | your CLI default | Runs the local `claude` CLI against your existing login. **No API key and no per-token bill.** |
| **Ollama** | *none* | best installed | Models running on your own machine. **No key, no quota, works offline.** |
| **Claude** | `ANTHROPIC_API_KEY` | `claude-opus-5` | Strongest on long documents and tool use. |
| **Groq** | `GROQ_API_KEY` | `gpt-oss-120b` | Fast and cheap; free tier caps at 100k tokens/day. |
| **ChatGPT** | `OPENAI_API_KEY` | `gpt-4.1` | Billed per token, no free tier. |
| **Gemini** | `GEMINI_API_KEY` | `gemini-2.5-flash` | Large free tier. |

Each provider is one file in `docsforge/providers/`, and they all speak the same small
event stream (`text`, `tool_start`, `tool_end`, `notice`) so `app.py` never
learns which one is running. What differs is the tool-calling shape, which is
why each owns its own loop:

- `groq.py` and `chatgpt.py` share `_openai_shape.py` — `tool_calls` deltas
  stitched by index, answered with `role: "tool"` messages.
- `claude.py` — `tool_use` blocks answered by `tool_result` blocks in one user
  turn. Sends **no** `temperature`/`top_p`/`top_k`: they were removed on Opus 5
  and return a 400. Refusal fallbacks are on by default (`ANTHROPIC_FALLBACKS=off`).
- `gemini.py` — `functionCall` / `functionResponse` parts, automatic function
  calling disabled so the JSON-Schema tool definitions stay shared.
- `ollama.py` — reuses the same OpenAI-shaped loop (Ollama serves an
  OpenAI-compatible endpoint), but probes the daemon instead of checking for a
  key, and auto-picks the best tool-capable model you have pulled. Models that
  cannot call tools — embeddings, `phi3`, vision-only — are filtered out, since
  one that silently answers from memory looks like DocsForge being broken.
- `claudecode.py` — the odd one out: it shells out to the `claude` CLI with
  **DocsForge's own MCP server attached**, so the tools run out of process over
  real MCP. `--strict-mcp-config` keeps your other MCP servers out of the session.

Adding a provider means one file and one line in `docsforge/providers/__init__.py`.

### Configuration

Everything is optional; see `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `DOCSFORGE_PROVIDER` | first configured | Which provider to start on. |
| `<NAME>_MODEL` | per provider | Override a provider's model, e.g. `CLAUDE_MODEL`. |
| `ANTHROPIC_FALLBACKS` | `default` | `off` disables Claude's server-side refusal fallbacks. |
| `OPENAI_BASE_URL` | — | Point ChatGPT at Azure or an OpenAI-compatible host. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Where the Ollama daemon listens. |
| `OLLAMA_MODEL` | auto | Pin a local model; otherwise the best tool-capable one installed. |
| `GITHUB_TOKEN` | — | Raises the GitHub API rate limit. |
| `DOCSFORGE_MAX_CHARS` | `200000` | Largest tool result returned to a model. Not a storage limit — files and the knowledge base keep every page whole. Raise it if your provider's context window allows; the cap exists because a result that overflows the window fails the turn rather than truncating. |
| `DOCSFORGE_OUT_ROOT` | `./docs_md` | Directory `save_docs` may write into. |
| `DOCSFORGE_ALLOW_PRIVATE` | unset | Allow fetching private/loopback addresses. |
| `DOCSFORGE_ALLOW_DELETE` | unset | Let the **model** delete stored documentation. Off by default — see below. |
| `DOCSFORGE_REASONING` | `off` | Allow a bounded number of model calls at four decision points — see below. |

### Removing a harvest

A harvest can be wrong: the wrong project, a partial copy, a table of contents
stored as though it were the documentation. Three ways to take one back out.

**In DocsStore.** Open a technology at `/library` and each version has a delete
control, with a second one below for the whole technology. It asks once before
acting.

**From the command line**, which is the one to reach for when clearing several:

```bash
python -m docsforge --forget pydantic@1.10     # one version
python -m docsforge --forget astro             # every version of it
python -m docsforge --forget a --forget b --yes  # several, no prompt
```

It prints what it is about to remove — pages and characters — and does nothing
unless you type `yes` or pass `--yes`.

**Over HTTP**, which is what the UI uses:

```bash
curl -X DELETE http://127.0.0.1:8000/api/library/astro
curl -X DELETE http://127.0.0.1:8000/api/library/pydantic/1.10
```

**Can the model delete?** Not unless you say so. Deleting is the one
irreversible thing DocsForge does, and a model that has just mis-resolved a
name is the last caller who should hold that lever — 703 pages of Effect are
one confident hallucination away. Set `DOCSFORGE_ALLOW_DELETE=1` to add a
`forget_documentation` tool to the surface. Note that you do not need it to
re-harvest: harvesting the same name again replaces that version on its own.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The chat UI. |
| `GET` | `/docs` | How it all works. (Swagger UI is disabled so this URL is the product's; the schema is still at `/openapi.json`.) |
| `GET` | `/library` | DocsStore: everything harvested so far. |
| `GET` | `/api/config` | Provider catalog, current default, tool list. |
| `POST` | `/api/chat` | SSE stream: `token`, `tool`, `notice`, `done`, `error`. Takes an optional `provider`. |
| `POST` | `/api/render` | Markdown → sanitized HTML. |
| `GET` | `/api/library` | One page of technologies. `?page=N&q=filter`. |
| `GET` | `/api/library/{tech}` | Every stored version of one technology. |
| `GET` | `/api/library/{tech}/{version}` | That version's page index. |
| `GET` | `/api/library/{tech}/{version}/page/{n}` | One page, as Markdown and sanitized HTML. |
| `GET` | `/api/library-search` | Ranked search. `?q=&tech=&version=&limit=`. |

The server is stateless — the browser holds the conversation and posts it back each turn.

## DocsStore

`/library` is the box: every documentation set that has ever been harvested,
three levels deep.

```
effect                  a divider in the box
  v3, v2                every crawled version of it
    703 pages           the pages of the version you opened
```

The technology list is **paged** (12 at a time) rather than loaded whole,
because the box grows every time anyone harvests and there is no natural
ceiling. Opening a divider lists every version with its page count, size,
harvest date and strategy; opening a version puts its page index beside the
page you are reading. The search box inside a version runs the ranked
full-text search over that version only, and marks what matched.

Every view is addressable, so a link goes exactly where you meant:
`/library#/effect/v3/41`.

**Right-click a technology** to open or to delete it outright — every version of
it, in one action. Removing a language's documentation one version at a time is
not a route once it has eight of them. The delete arms before it fires: it asks
once, and needs a second click.

The menu bar's right-hand corner names the backend that answered. That is not
decoration: a Postgres box ranks search and a file box cannot, and reading
unranked results while believing they are ranked is worse than knowing.

## Learning something by name

The caller usually does not have a documentation URL — it is a model that has
just met a library it does not know, so *where the docs live* is exactly the
knowledge it is missing. It does not need one:

```
learn_technology(name="effect")
learn_technology(name="pydantic", version="1.10")
  -> resolved to https://docs.pydantic.dev (names 'pydantic' 214 times)
  -> harvested 85 pages, stored as pydantic 1.10
```

It asks the package registries — npm, PyPI, crates.io, all keyless — where the
library declares its documentation, probes the site for an `llms.txt` or a docs
root, and then **confirms the page actually documents that package** before
harvesting anything.

### How a name is resolved

A bounded ladder, cheapest lap first. Every lap ends at the same identity gate,
so reaching further never means believing more:

| Lap | What it tries |
|---|---|
| **memory** | A resolution seen in the last 30 days. Costs zero requests. Refusals are re-tried after 7 days, because a site can add the evidence later. |
| **domain** | `{name}.dev` / `.io` / `.org` / `.com`, and the docs root beneath whichever answers. |
| **registries** | Exact-name lookup in npm, PyPI and crates.io. |
| **name shapes** | For multi-word names: `opentelemetry.io`, `airflow.apache.org`, `tanstack.com/query`, `docs.spring.io/spring-boot`. |
| **search** | npm and crates.io fuzzy search, then `DOCSFORGE_SEARCH` if you configure one. Never a search engine's HTML. |

The whole ladder is capped at 40 requests and 20 seconds, so a name that does
not exist refuses promptly instead of wandering.

```bash
forget_resolution(name="polars")   # resolved to the wrong site? start over
forget_resolution()                # clear every remembered lookup
```

### Verification is the point

A resolver that is merely usually right is a slower guess. Names collide: the
npm package `fastapi` is unrelated to the Python framework, and the `effect`
crate is not the TypeScript library. Harvesting an unverified page stores the
wrong project and then answers from it confidently.

So a page must name the package repeatedly before it is trusted — one passing
mention is noise. If nothing verifies, DocsForge reports that and asks for a
URL instead of picking something plausible. `find_docs` shows the candidates
and the evidence without fetching the documentation.

### Reading the project

`scan_project` reads `package.json`, `pyproject.toml`, `requirements.txt`,
`Cargo.toml` and `go.mod`, and reports each dependency, its version, and
whether it is documented here yet.

The manifest is the only place the **correct version** can be read from. A bare
name cannot tell you this project is pinned to Pydantic 1.10, and 1.10 and 2.11
contradict each other — which is what the versioned store exists to prevent,
and what a name-only lookup would defeat.

### Names are matched, not memorised

`Effect.ts`, `effect-ts` and `effect` all reach the same stored copy, and
`@scope/pkg` files under `pkg`. Everything is stored under one canonical name,
so a library cannot end up saved twice under two spellings. Genuinely ambiguous
names are refused rather than guessed.

## Learning a whole technology

`fetch_docs` answers "extract this URL". `harvest_docs` answers "extract this
technology" — the question you actually have when a model does not know a stack:

```
harvest_docs(url="https://www.effect.website/docs/v3/getting-started/introduction/")
  -> 200 pages, 2,668,643 characters, stored as knowledge_base/effect.md
```

Give it any page of a docs site and it finds the rest, best strategy first:

1. **`llms.txt` / `llms-full.txt`** — the site already published itself for machines.
2. **`sitemap.xml`**, filtered to the docs section — complete, cheap, and it finds
   pages nothing links to. Located via `robots.txt` first, then the usual paths.
3. **A scoped crawl** — works anywhere, but only reaches what is linked.

Everything lands as one Markdown file with a contents index, and
`read_knowledge_base(name, section=...)` reads it back so a stored technology is
never re-scraped to answer a question. `section` matches page titles first and
falls back to searching the text, so a specific question pulls the handful of
relevant pages instead of a whole manual.

### How much it crawls

`harvest_docs` has **no page limit by default** — it keeps going until the
documentation section is exhausted. A page count is a guess at how big someone
else's manual is; the scope prefix above is the boundary that actually means
something. The Effect v3 docs come to 703 pages, and nothing in DocsForge knew
that in advance.

Pass `max_pages=N` only to deliberately cut a harvest short. `fetch_docs` keeps
its own 200-page ceiling, so a single page fetch can never turn into an
open-ended crawl by accident.

### A long harvest does not block

The Effect v3 docs take around twelve minutes to harvest. Every MCP client
gives up long before that, so `learn_technology` used to look broken on exactly
the technologies it was most needed for.

It now waits up to 25 seconds (`DOCSFORGE_HARVEST_DEADLINE`) and then hands
back a harvest id while the crawl carries on in the background:

```
**Learning effect from https://effect.website/docs/ — still running.** Harvest id `effect-1`.

Currently harvesting 118/703 pages, 25s elapsed.
```

`harvest_status(harvest="effect-1")` reports how it is getting on — phase,
pages of how many, elapsed time, and once it ends whether it stored, failed or
stopped — and `wait=20` lets one call watch it finish. Every running status
also says, in so many words, that the pages are landing in DocsForge's own
store rather than in the model's context: a model told "still running" tends
to decide the harvest is now its job — to fetch the pages itself, or to cut
the harvest short — and neither is wanted. `list_knowledge_base` reports
every harvest in flight too, and any that failed — a background failure
leaves no other trace, so it is surfaced there rather than lost. Anything
that finishes inside the deadline returns exactly what it always returned;
short harvests are unaffected.

Jobs live in the server process and are not persisted. The durable record of a
harvest is the knowledge-base entry it writes, so a harvest interrupted by a
restart simply did not happen.

### A short harvest says so

When you *do* set a limit and hit it, that is reported — in the harvest result,
in `list_knowledge_base`, and again every time the content is read:

```
**INCOMPLETE — stopped at the 200-page limit, 400+ pages still queued.**
```

That matters more than the limit itself: a third of a manual that looks whole
produces confident, wrong answers.

### Versions are kept apart

A library's v2 and v3 docs contradict each other, and a model handed both will
quote the wrong one. So a harvest is filed under a **version**, not just a
name, and re-harvesting one version leaves the others alone:

```
pydantic
  2.11   85 pages   1.2 MB   harvested 2026-08-14
  1.10   24 pages   198 KB   harvested 2026-08-14
```

The label comes from the URL — `/docs/v3/`, `/docs/validation/2.11/` — and is
then **checked against what came back**. A site publishes one `llms.txt` for
its current release, so if the harvest served that instead of crawling the
version you asked for, the label falls back to the harvest date rather than
claiming a precision the content does not have. Sites that publish one version
at a time are always dated: the date says which snapshot this is.

`read_knowledge_base(name, version=...)` picks one; without it you get the most
recently harvested.

For the same reason the crawl scope follows the version down the path.
Pydantic keeps versions at `/docs/validation/2.11/`, and stopping at `/docs/`
there crawls every version of the manual at once and calls the result one
harvest.

### Where harvested docs live

Two backends, chosen automatically:

| | When | What you get |
|---|---|---|
| **Markdown files** | the default | `knowledge_base/<tech>/<version>.md`, one file per version, plus `index.json`. Zero setup, and the file is a deliverable you can hand to anyone. |
| **PostgreSQL** | `DOCSFORGE_DB` set and reachable | A row per page with a GIN-indexed `tsvector`. Section lookups are ranked and stay fast as the store grows, and several DocsForge instances can share one store. |

```bash
DOCSFORGE_DB=postgresql://postgres:password@127.0.0.1:5432/DocsForge
```

The schema is created on first use. If the database is unreachable DocsForge
falls back to files rather than losing a harvest — losing ten minutes of
crawling to a database outage would be the worse failure.

The file path is anchored to the package directory rather than the working
directory: keying it off `cwd` meant launching the app from elsewhere silently
produced a second, empty knowledge base. Override with `DOCSFORGE_KB_ROOT`.
Both `knowledge_base/` and `.env` are gitignored.

Postgres is what makes `section=` worth using. The file backend answers it by
regex over one large string, which cannot rank; Postgres ranks, with page
titles weighted above body text. Over 703 harvested Effect pages (6.3 MB):

```
'error handling'                  0.20s   1 page   (by title)
'retry with exponential backoff'  0.11s   6 pages  (by content, ranked)
```

Already have a file store? `scripts/migrate_kb.py` reads the combined Markdown
back into Postgres, so a site that took ten minutes to crawl is not crawled
again.

To read what is stored, open [DocsStore](#docsstore) at `/library`.

### Why the crawl is scoped

Documentation shares a domain with marketing, blogs and changelogs. Crawling by
host from an Effect docs page reached `/podcast` and `/community-hub` within four
pages, and those pages then dominated the truncated result the model saw — so it
summarised a podcast feed instead of the library.

`harvest_docs` therefore stays inside the *documentation root* of the start URL
(`/docs/v3/getting-started/introduction/` -> `/docs/v3/`), which took that same
crawl from 7/12 relevant pages to 200/200. Pass `scope="host"` for the old
behaviour, or a literal prefix like `scope="/docs/v3/"` to pin it exactly.

## Measuring the crawler

`docsforge/core/instrument.py` measures what extraction actually does — which `CONTENT`
selector won on each page, how often none did and `<body>` was stored whole,
how many distinct templates a site really has, how much of the "documentation"
is link text, and how many pages are JS shells. It decides nothing and no
shipping module imports it; a test enforces both.

`scripts/measure.py` drives it across real technologies and writes the numbers out:

```bash
python scripts/measure.py             # the built-in 20, ~40 pages each
python scripts/measure.py fastapi --pages 6   # one technology, quickly
```

Results land in `measurements/` as JSON plus a readable table, and the run is
resumable. This exists because every adaptive rule the crawler runs is a
threshold over a number, and thresholds picked before the numbers are guesses.

## Measuring whether any of it helps

`scripts/measure_answers.py` asks the only question that matters: does a harvested
corpus change what a model can answer? The same questions are put three ways —
the model alone, the model with the documentation retrieved for it, and the
model given DocsForge's tools and left to use them.

```bash
python scripts/measure_answers.py --provider ollama --model llama3.2:latest
python scripts/measure_answers.py --phases closed,passages --out answers.json
```

Every expected answer is checked against the stored corpus before a question is
asked, so a fixture whose answer is not in the documentation fails the run
rather than quietly measuring nothing. Grading is a string match on an exact
identifier, not one model judging another.

Measured 2026-09-10, twelve questions across six technologies:

```
phase             all   unknown tech   known tech
closed           0/12            0/7          0/5     llama3.2 (3B), alone
passages         8/12            6/7          2/5     with the corpus retrieved
tools            1/12            1/7          0/5     llama3.2 driving the tools
tools             3/4            3/4            -     qwen3.5 (9B), 4 questions
```

Closed-book the model answered none of the twelve and said so only once —
`useEffect` for Effect, `agentkit` for `google-adk`, `hydrate` for
`model_validate`. Confident inventions, which is the failure this exists to
prevent. With the documentation in front of it: 6 of 7 on technologies it did
not know. A model too small to call tools cannot reach the corpus by itself; one
that can, does.

## Serving a purpose

A technology of any size documents itself in more than one place: a manual, a
specification, a generated API reference, often on different hosts. DocsForge
finds those from the link evidence it gathers while crawling, confirms each new
host through the same identity gate it uses for resolution, and classifies them
by **shape** (how to fetch it) and **kind** (what it is for).

`intent` then decides which enter scope:

| Intent | Takes | Leaves |
|---|---|---|
| `resolve-import` | `api`, `sdk` | guides, cookbooks, changelogs |
| `implement` | `api`, plus guides and examples | changelogs, meta |
| `learn` | `guide`, plus language and spec | changelogs, meta |
| `operate` | `operations`, plus guides and API | meta |
| `reference` *(default)* | everything except `meta` | — |

Two rules keep this honest:

- **Corpora left out are declared, never dropped.** Each is listed with its
  size and marked `not requested`.
- **Choosing a corpus is scope; dropping pages inside one is filtering, and is
  forbidden.** A selected corpus is harvested whole or reported partial.

When several corpora could be what you meant, it does not guess — it returns
`NEEDS SELECTION` with the options, and remembers your answer so the question is
asked once. `forget_selection` clears it.

```python
learn_technology(name="stripe", intent="resolve-import", strict=True)
```

`strict=True` adds `usable_for_planning`, which is `true` only when every corpus
your intent requires came back complete. It is the figure a downstream system
can refuse to act on.

## Answering from what is stored

`search_knowledge_base` returns **passages**, not pages. Results are chunked on
`h2`/`h3` and ranked as sections, each carrying its heading path so you can cite
it — `Error Handling > Retrying` rather than "somewhere on the retry page". One
page of a generated API reference can be 20,000 tokens spent answering a
one-line question.

```python
search_knowledge_base(query="exponential backoff", kind="api")
```

`kind` keeps tutorial prose out of an answer that wants a signature. Relevance
is applied at read time only: what is stored stays whole, and only what is
handed back is narrowed.

## Reasoning, if you want it

Four moments decide whether a harvest is correct, and all four are settled by
arithmetic at exactly the point the arithmetic is weakest: a template none of
the nine selectors recognise, a corpus whose kind the URL does not state, a new
host that may be a different project sharing a name, and a page answering 200
while rendering an error.

`DOCSFORGE_REASONING=on` lets a model settle those instead — under a budget
that is the whole point of the feature:

- **Twelve calls per harvest.** A hard cap, not a target. Spent means fall back,
  never stall.
- **Cached per template and per host, never per page.** A 700-page site with
  three templates costs at most three calls. There is no per-page model call
  anywhere, by construction — that is the bill this design exists to avoid.
- **Validated before trusted.** A proposed selector that matches nothing on the
  page is discarded, and the algorithmic answer stands.
- **Only ever stricter at the identity gate.** A host the gate refused is never
  re-admitted by asking; reasoning can veto a wrong admission and cannot create
  one.
- **Recorded.** Every consultation, its question and its answer appear in the
  result beside the coverage note. A judgement nobody can audit is worse than a
  heuristic.

It needs a configured provider as well as the flag, and with it off every path
behaves exactly as it does without it — which is what the test suite runs.

## Known limits

Kept here rather than only in `Project Development/AUDIT.md`, because a tool
whose pitch is calibrated confidence cannot be selective about its own.

- **Multi-word names resolve about 8 times in 20**, up from 1 before the name-shape
  lap. `apache airflow`, `ruby on rails`, `open telemetry`, `shadcn ui` and
  `visual studio code` now work. `spring boot`, `next auth`, `framer motion`,
  `aws lambda` and the cloud-provider names still refuse — several for honest
  reasons, such as documentation living under a deep path on an enormous vendor
  portal that no name shape reaches. Refusing beats guessing.
- **A resolved name is not always the right project.** Verification confirms a
  page is *about something with that name*, which is not the same as confirming
  the project. Measured over 25 judged names, cold: **23 correct**, up from 19.
  Check the URL in the result before trusting a harvest, pass `ecosystem=` when
  you know it, and use `forget_resolution` when it is wrong.

  What still fails: `flask` reaches an unrelated to-do app at flask.io and
  `polars` a third-party site. Both are the same remaining case — a domain that
  genuinely owns the word and repeats it — and closing it means raising the
  identity gate itself, which is a decision about a stated invariant rather than
  a fix.

  What was closed, each after being caught live: a domain probe no longer keeps
  its ownership claim when it redirects onto a code host (`mojo.dev` lands on a
  Java library); a language can claim the `lang` domain it publishes from
  (`ziglang.org`, `nim-lang.org`); the claim a host makes on a name is now
  *graded* rather than counted, so a first-come `<name>.github.io` label and a
  country-code community mirror no longer outrank the project's own domain
  (`tensorflow` reached the Rust bindings' rustdoc and `pytorch` the Korean user
  group before this); a sub-project documented on its parent's host can be
  identified at all, through a registry-nominated URL whose path names it
  (`langgraph`); and `djangoproject.com` counts as Django's own domain, so
  `django` reaches `docs.djangoproject.com` rather than the weblog.
  See `Project Development/Resolved.md`, `FINDINGS-B.md` and `FINDINGS-C.md`.
- **A page under an unrecognised template is refused, not stored.** Extraction
  tries nine selectors and then scores the page by text-to-link density; where
  nothing reads like documentation it stores nothing and says so, rather than
  silently keeping the navigation. That is the right default — it is what
  stopped a sidebar being filed as a manual — but it costs a real page whenever
  an API reference is mostly method links. Bounded reasoning can propose a
  selector for such a template, validated against the page before it is used.
- **A page that answers 200 while rendering an error can still be stored.** A
  4xx or 5xx is refused outright, but a *soft* 404 — a genuine 200 whose body
  reads "Page not found" — is indistinguishable from documentation without
  reading it. Bounded reasoning catches these when it is switched on; with it
  off, they are stored.
- **An interrupted harvest leaves nothing readable.** Pages are durable as they
  are fetched, and whatever was already stored is untouched — but a harvest
  that never finished stays invisible until it does, rather than being served
  as a partial copy. That is deliberate: a partial corpus presented as a whole
  one is the failure this project exists to refuse. Whether it should be
  readable behind an explicit opt-in is an open question.
- **Background harvests do not survive a restart.** The pages do — they are
  written as they are fetched — and the *report* of one is now visible from any
  process, so a harvest started by the MCP subprocess shows up in the panel and
  in `list_knowledge_base`. What still does not survive is the harvest itself:
  it runs on a daemon thread and dies with its process. When that happens the
  tracker says the harvest **stopped reporting** rather than leaving it
  spinning, because a dead process and a slow one differ only in their silence.

`Project Development/` holds the design record: three proposals, an audit, and
the measurements behind each threshold. `PROPOSAL-II.md` (federation, intent,
passages) and `PROPOSAL-3.md` (streaming storage, measured shape, concurrency,
bounded reasoning) are both implemented; `ISSUES.md` is the live list of what is
still open, including every limit above.

Start with **`ARCHITECTURE.md`** — how the pieces fit, in diagrams, including
the two pathways a harvest can take and what the identity gate will and will not
believe. **`PRODUCT.md`** is what this is for and who for; **`DESIGN.md`** is the
interface, recorded from the built pages rather than from intention;
**`llmsfinder.md`** is the acquisition ladder, with a §10 recording what the
build learned that the proposal did not anticipate.

## Security

DocsForge fetches URLs chosen by whoever is talking to it, which in the MCP and web paths can be a language model. Two guards apply there:

- **SSRF** — requests to private, loopback, link-local, and reserved addresses are refused. Set `DOCSFORGE_ALLOW_PRIVATE=1` (or pass `--allow-private`) to scrape docs on your own network.
- **Path traversal** — `save_docs` cannot write outside `DOCSFORGE_OUT_ROOT`.

Rendered Markdown is sanitized with `nh3` before it reaches the page, since it mixes model output with scraped HTML. Bind the web chat to `127.0.0.1` (the default) unless you have put authentication in front of it.

## Deploying

Two shapes. Both put the corpus in Postgres and serve the same routes: the
site at `/`, `/tools`, `/connect`, a `/health` check, and the MCP endpoint at
`/mcp` behind a bearer token. The web chat, tests, scripts and `.env` are
never shipped.

| Variable | Required | What it does |
|---|---|---|
| `DOCSFORGE_MCP_TOKEN` | yes | Clients send it as `Authorization: Bearer …`. Generate it (`openssl rand -hex 32`); never reuse a database password. |
| `DOCSFORGE_DB` or `DATABASE_URL` | yes | The Postgres DSN, `?sslmode=require` on Aiven. On a host with no persistent disk a file store would vanish. |
| `DOCSFORGE_HARVEST_DEADLINE` | no | Seconds a harvest may take inside one tool call before it goes to the background (25). |
| `DOCSFORGE_PUBLIC_URL` | no | The https address OAuth discovery advertises. Vercel's production domain is used when unset. |
| `PORT` | no | Container only: what the platform routes to (8765). |

### Vercel (serverless)

`docsforge/server/vercel.py` exposes the app; `pyproject.toml` names it under
`[tool.vercel]` and `vercel.json` sets the function's `maxDuration`. Import
the repository in Vercel, set the variables above in the project's
Environment Variables (the token as sensitive), deploy. Then:

```bash
claude mcp add --transport http docsforge https://<project>.vercel.app/mcp   --header "Authorization: Bearer <token>"
```

What a serverless host changes, and the code says so rather than pretending:

- **Stateless.** `/mcp` runs the SDK's stateless mode — every request carries
  everything, answers are plain JSON. Nothing lives between requests.
- **Ephemeral.** Nothing runs after a response. A harvest that finishes
  inside `DOCSFORGE_HARVEST_DEADLINE` is stored as usual; one that does not
  is *discarded*, and the result says exactly that instead of "it continues
  in the background". Nothing partial is ever published. **Large harvests are
  done from a long-lived DocsForge** — `python main.py` on your machine with
  the same `DOCSFORGE_DB` — and are readable through Vercel at once. Raise the
  deadline (to below `maxDuration`, 300 s by default here) if your client
  waits that long on a tool call.
- **Only `/tmp` is writable, per instance.** Caches (resolution cache, logs)
  go there and are lost between instances; that costs a repeat lookup,
  nothing more. Harvest status is kept in the database as well, so
  `harvest_status` answers the same from whichever instance takes the call.
- **Misconfiguration is visible.** With no database or no token the site
  still serves and `/mcp` answers `503` with the reason; `/health` reports
  `degraded` when the configured database could not be reached.

### A container (long-lived)

For a host that keeps a process alive — where background harvests work as
designed — the `Containerfile` builds `main.py --http` from `pip install .`:

```bash
podman build -t docsforge -f Containerfile .
podman run -p 8765:8765 -e DOCSFORGE_MCP_TOKEN=… -e DOCSFORGE_DB=… docsforge
```

Run one replica: streamable-HTTP sessions and running harvests live in the
process. Redeploy when nothing is harvesting — a redeploy kills a harvest in
flight and its status record with it.

The site pages under `docsforge/server/site/` were designed in Stitch from the
web chat's palette. `scripts/site_exports/` holds Stitch's raw exports and
`python scripts/build_site.py scripts/site_exports` turns them into the served
pages — one nav, real links, Tailwind compiled to a static stylesheet, the
version chip filled in from the package at request time. Needs Node for the
compiler. Edit in Stitch, download, re-run.

## Tests

```bash
python -m pytest tests/ -q          # 860 offline unit tests, no network

# The 37 Postgres tests skip unless you point them at a throwaway database,
# which makes a green run look more complete than it is — set this before
# trusting one. The variable is deliberately NOT DOCSFORGE_DB, and the database
# it names must not be the one holding real harvests: these tests create and
# drop technologies.
DOCSFORGE_TEST_DB=postgresql://postgres:pw@127.0.0.1:5432/DocsForgeTest python -m pytest tests/ -q
```

The live checks need the network, and the last two need `GROQ_API_KEY`:

```bash
python scripts/smoke_mcp.py         # spawns main.py, the MCP server, over stdio
python -m docsforge.server.app --port 8123 &
python scripts/smoke_web.py 8123    # one real Groq turn, end to end
python scripts/smoke_multiturn.py 8123
python scripts/shoot_ui.py 8123     # screenshots every UI state
```

`shoot_ui.py` stubs the model stream by default, so it costs no tokens and is
deterministic; pass `--live` to drive a real turn instead.

## Output

By default, each source produces its own Markdown file in the output directory, named from the host, path, and a short hash of the URL — so pages from different sites never overwrite each other. Every file starts with a comment header noting the source URL, detected type, and timestamp. Use `--single-file` to merge all documents into one file separated by horizontal rules.

## License

MIT © 2026 Raj Patel
