# DocsForge

**Universal software documentation → clean Markdown for LLMs.**

DocsForge points at any documentation source, figures out *what kind* of source it is, and extracts it into tidy, LLM-ready Markdown. Feed it a docs site, an OpenAPI spec, a GitHub repo, a sitemap, or a raw Markdown file — it detects the format and handles each one appropriately.

## Features

- **Automatic source detection** — probes the URL (and content, when needed) to pick the right extraction strategy.
- **Supported sources:**
  - `llms.txt` / `llms-full.txt` — the LLM-native docs standard (passthrough).
  - **OpenAPI / Swagger** (JSON or YAML) → readable API reference with endpoint tables, params, request bodies, and response codes.
  - **sitemap.xml** → structured crawl of every listed page.
  - **GitHub repos** → README + all Markdown under `/docs` via the GitHub API.
  - **Generic HTML docs sites** → readability-style extraction (strips nav/footer/ads, keeps main content).
  - **Raw Markdown / plaintext** → passthrough with cleanup.
- **Bare-domain probing** — auto-checks for `llms.txt` at the root before falling back to HTML.
- **Optional site crawling** (`--crawl`) with same-host link following and page limits.
- **JS rendering** (`--js`) via Playwright for client-rendered sites.
- **Single-file output** (`--single-file`) to concatenate everything into one `.md`.
- **Provenance headers** — every output file records its source URL, type, and scrape time.

## Installation

```bash
git clone https://github.com/R204570/DocsForge.git
cd DocsForge
pip install requests beautifulsoup4 markdownify
```

Optional extras, installed only if you use the relevant flags/sources:

```bash
pip install pyyaml       # for YAML OpenAPI specs
pip install lxml         # recommended for sitemap XML parsing
pip install playwright   # for --js rendering
playwright install chromium
```

## Usage

```bash
python docsforge.py <URL> [options]
```

### Examples

```bash
# A docs site (auto-detected)
python docsforge.py https://docs.stripe.com

# An OpenAPI / Swagger spec → API reference tables
python docsforge.py https://api.example.com/openapi.json

# A GitHub repo → README + /docs
python docsforge.py https://github.com/tiangolo/fastapi

# Crawl a docs site, up to 50 pages
python docsforge.py https://docs.example.com --crawl --max-pages 50

# JS-rendered site
python docsforge.py https://site.com --js

# Combine everything into one Markdown file
python docsforge.py https://site.com --single-file
```

## Options

| Flag | Default | Description |
|---|---|---|
| `-o`, `--out` | `./docs_md` | Output directory. |
| `--crawl` | off | Follow same-host links from the start URL. |
| `--max-pages` | `25` | Max pages to fetch (crawl / sitemap / repo docs). |
| `--js` | off | Render JavaScript with Playwright. |
| `--delay` | `0.4` | Seconds to wait between requests when crawling. |
| `--single-file` | off | Write one combined `.md` instead of per-page files. |
| `--force` | — | Skip detection and force a strategy: `llms_txt`, `openapi`, `github`, `raw_text`, `html`, `sitemap`. |

## Output

By default, each source produces its own Markdown file (named from a slug of the URL) in the output directory. Every file starts with a comment header noting the source URL, detected type, and timestamp. Use `--single-file` to merge all documents into one file separated by horizontal rules.

## License

MIT © 2026 Raj Patel
