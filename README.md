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

Results handed to a model are capped at `DOCSFORGE_MAX_CHARS` (60k default) with an explicit truncation marker.

## 3. Web chat

```bash
cp .env.example .env      # then put your GROQ_API_KEY in it
python app.py             # http://127.0.0.1:8000
```

A single-page chat with the composer pinned to the bottom. You type a question, the model fetches whatever docs it needs through the tools above, and the reply is rendered as Markdown. Every reply carries **View Markdown / Copy .md / Download .md** actions, so you can take the raw `.md` straight out of the page.

Tool calls appear inline as they run, so you can see what was fetched and how much came back.

### Configuration

All optional except the key — defaults shown.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | **Required.** From https://console.groq.com/keys |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Any Groq model with tool-calling support. |
| `GROQ_TEMPERATURE` | `1` | |
| `GROQ_MAX_TOKENS` | `2048` | Per-turn completion cap. |
| `GROQ_TOP_P` | `1` | |
| `GITHUB_TOKEN` | — | Raises the GitHub API rate limit. |
| `DOCSFORGE_MAX_CHARS` | `60000` | Largest tool result returned to a model. |
| `DOCSFORGE_OUT_ROOT` | `./docs_md` | Directory `save_docs` may write into. |
| `DOCSFORGE_ALLOW_PRIVATE` | unset | Allow fetching private/loopback addresses. |

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The chat UI. |
| `GET` | `/api/config` | Model name, key presence, tool list. |
| `POST` | `/api/chat` | SSE stream: `token`, `tool`, `done`, `error` events. |
| `POST` | `/api/render` | Markdown → sanitized HTML. |

The server is stateless — the browser holds the conversation and posts it back each turn.

## Security

DocsForge fetches URLs chosen by whoever is talking to it, which in the MCP and web paths can be a language model. Two guards apply there:

- **SSRF** — requests to private, loopback, link-local, and reserved addresses are refused. Set `DOCSFORGE_ALLOW_PRIVATE=1` (or pass `--allow-private`) to scrape docs on your own network.
- **Path traversal** — `save_docs` cannot write outside `DOCSFORGE_OUT_ROOT`.

Rendered Markdown is sanitized with `nh3` before it reaches the page, since it mixes model output with scraped HTML. Bind `app.py` to `127.0.0.1` (the default) unless you have put authentication in front of it.

## Tests

```bash
python -m pytest tests/ -q          # 62 offline unit tests, no network
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
