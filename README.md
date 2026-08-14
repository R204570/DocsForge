# DocsForge

**Universal software documentation → clean Markdown for LLMs.**

DocsForge points at any documentation source, figures out *what kind* of source it is, and extracts it into tidy, LLM-ready Markdown. Feed it a docs site, an OpenAPI spec, a GitHub repo, a sitemap, or a raw Markdown file — it detects the format and handles each one appropriately.

It ships in three forms, all sharing one extraction engine:

| Surface | File | What it's for |
|---|---|---|
| **CLI** | `docsforge.py` | One-shot scraping into `.md` files. |
| **MCP server** | `mcp_server.py` | Give any MCP client (Claude Code, Claude Desktop, your agent) live docs-fetching tools. |
| **Web chat** | `app.py` + `static/` | A chat UI backed by Groq that fetches docs and answers in rendered Markdown. |

```
                   ┌─────────────────┐
   CLI ───────────▶│                 │
                   │   docsforge.py  │  detect → extract → Markdown
   MCP client ────▶│   forge_tools   │
                   │                 │
   Web chat ──────▶│                 │
                   └─────────────────┘
```

`forge_tools.py` defines each tool exactly once. `mcp_server.py` exposes those definitions over MCP; `app.py` hands the same schemas to Groq for tool calling. An MCP client and the web chat therefore run identical code.

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

## Installation

```bash
git clone https://github.com/R204570/DocsForge.git
cd DocsForge
pip install -r requirements.txt
```

Optional extras:

```bash
pip install playwright && playwright install chromium   # for --js / js:true
pip install pytest                                       # to run the tests
```

## 1. CLI

```bash
python docsforge.py <URL> [options]
```

```bash
# A docs site (auto-detected)
python docsforge.py https://docs.stripe.com

# An OpenAPI / Swagger spec → API reference tables
python docsforge.py https://petstore3.swagger.io/api/v3/openapi.json

# A GitHub repo → README + /docs
python docsforge.py https://github.com/tiangolo/fastapi

# Crawl a docs site, up to 50 pages
python docsforge.py https://docs.example.com --crawl --max-pages 50

# JS-rendered site
python docsforge.py https://site.com --js

# Combine everything into one Markdown file
python docsforge.py https://site.com --single-file
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
from docsforge import forge, Options

docs = forge("https://docs.example.com", Options(crawl=True, max_pages=10))
for d in docs:
    print(d.title, len(d.markdown))
```

## 2. MCP server

```bash
python mcp_server.py                 # stdio — what MCP clients launch
python mcp_server.py --http          # streamable HTTP on 127.0.0.1:8765
```

Register with Claude Code:

```bash
claude mcp add docsforge -- python /absolute/path/to/DocsForge/mcp_server.py
```

Or in an MCP client config file:

```json
{
  "mcpServers": {
    "docsforge": {
      "command": "python",
      "args": ["E:/DocsForge/mcp_server.py"]
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
| `list_knowledge_base` | — | What has already been harvested, and which versions of each. |
| `read_knowledge_base` | `name`, `section`, `version` | Reads stored docs back, optionally only matching pages. Defaults to the newest version. |

Results handed to a model are capped at `DOCSFORGE_MAX_CHARS` (60k default) with an explicit truncation marker.

## 3. Web chat

```bash
cp .env.example .env      # add a key for ONE provider — or none at all
python app.py             # http://127.0.0.1:8000
```

A single-page chat built as a HyperCard card stack: every answer is a card, the
shoebox index runs down the left, and the current card holds the document. Each
card carries **Edit This Card / Copy / Download .md**, so the Markdown is
something you keep, not just something you read. Tool calls appear inline as
they run, with the source type each card was forged from.

### Providers

Pick one from the **Model** menu; unconfigured ones are greyed out. The choice
rides on each request, so when one provider hits its daily cap you switch and
keep going — the stack can mix providers.

| Provider | Key | Default model | Notes |
|---|---|---|---|
| **Claude Code** | *none* | your CLI default | Runs the local `claude` CLI against your existing login. **No API key and no per-token bill.** |
| **Ollama** | *none* | best installed | Models running on your own machine. **No key, no quota, works offline.** |
| **Claude** | `ANTHROPIC_API_KEY` | `claude-opus-5` | Strongest on long documents and tool use. |
| **Groq** | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Fast and cheap; free tier caps at 100k tokens/day. |
| **ChatGPT** | `OPENAI_API_KEY` | `gpt-4.1` | Billed per token, no free tier. |
| **Gemini** | `GEMINI_API_KEY` | `gemini-2.5-flash` | Large free tier. |

Each provider is one file in `providers/`, and they all speak the same small
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

Adding a provider means one file and one line in `providers/__init__.py`.

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
| `DOCSFORGE_MAX_CHARS` | `60000` | Largest tool result returned to a model. |
| `DOCSFORGE_OUT_ROOT` | `./docs_md` | Directory `save_docs` may write into. |
| `DOCSFORGE_ALLOW_PRIVATE` | unset | Allow fetching private/loopback addresses. |

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The chat UI. |
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

The menu bar's right-hand corner names the backend that answered. That is not
decoration: a Postgres box ranks search and a file box cannot, and reading
unranked results while believing they are ranked is worse than knowing.

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

Already have a file store? `tests/migrate_kb.py` reads the combined Markdown
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

## Security

DocsForge fetches URLs chosen by whoever is talking to it, which in the MCP and web paths can be a language model. Two guards apply there:

- **SSRF** — requests to private, loopback, link-local, and reserved addresses are refused. Set `DOCSFORGE_ALLOW_PRIVATE=1` (or pass `--allow-private`) to scrape docs on your own network.
- **Path traversal** — `save_docs` cannot write outside `DOCSFORGE_OUT_ROOT`.

Rendered Markdown is sanitized with `nh3` before it reaches the page, since it mixes model output with scraped HTML. Bind `app.py` to `127.0.0.1` (the default) unless you have put authentication in front of it.

## Tests

```bash
python -m pytest tests/ -q          # 165 offline unit tests, no network

# The Postgres backend is skipped unless you point it at a throwaway database.
# It is deliberately NOT DOCSFORGE_DB, so tests can never touch real harvests.
DOCSFORGE_TEST_DB=postgresql://postgres:pw@127.0.0.1:5432/DocsForge python -m pytest tests/ -q
```

The live checks need the network, and the last two need `GROQ_API_KEY`:

```bash
python tests/smoke_mcp.py           # spawns the MCP server over stdio
python app.py --port 8123 &
python tests/smoke_web.py 8123      # one real Groq turn, end to end
python tests/smoke_multiturn.py 8123
python tests/shoot_ui.py 8123       # screenshots every UI state
```

`shoot_ui.py` stubs the model stream by default, so it costs no tokens and is
deterministic; pass `--live` to drive a real turn instead.

## Output

By default, each source produces its own Markdown file in the output directory, named from the host, path, and a short hash of the URL — so pages from different sites never overwrite each other. Every file starts with a comment header noting the source URL, detected type, and timestamp. Use `--single-file` to merge all documents into one file separated by horizontal rules.

## License

MIT © 2026 Raj Patel
