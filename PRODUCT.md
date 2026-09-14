# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: a developer who has hit the wall where their AI model does not know
the technology they are working with — a library, framework or API released
or updated after the model's training, or too niche to have been learned
well. They are mid-task. They know the technology's name; they do not
necessarily know where it documents itself, and a URL the model "remembers"
comes from the same training data that did not include it.

On the public site (`docsforge/server/site/`) that developer arrives from a
link or a search, already using an MCP-capable client — Claude Code, Cursor,
ChatGPT, Codex, Windsurf, Antigravity, Gemini CLI, VS Code, Claude Desktop —
and decides in about a minute whether to connect DocsForge to it. Confirmed
2026-09-14: this is the site's primary audience, and *connecting* is the
action; contributors and evaluators are a clear second door (GitHub).

Secondary (product, not site): the same person using DocsForge standalone
(CLI or MCP over stdio), and the embedded chat panel documented in
`Project Development/PRODUCT.md`.

## Product Purpose

DocsForge gives a model documentation for technologies it was not trained
on, and tells the truth about how much of it there is.

Name a library and it works out where that library documents itself,
confirms the page really documents it rather than merely mentioning it,
harvests the whole set, and stores it. Point it at a URL instead and it
identifies what kind of source the URL is and extracts accordingly. Either
way the output is Markdown a model can read and a coverage figure a caller
can act on.

Success is the wall coming down: the model answers correctly about a
technology it did not know a minute ago — and where the harvest was
partial, says so instead of guessing.

## Positioning

Two things a scraper with a nice wrapper cannot truthfully claim:

- **Name-addressable.** The caller needs no URL and is discouraged from
  inventing one. Resolution asks the package registries (npm, PyPI,
  crates.io — keyless), probes the site, and **verifies** the page names
  the package repeatedly before trusting it. If nothing verifies it says so
  and asks for a URL rather than picking something plausible.
- **Measured coverage.** `complete`, `incomplete` and `unknown` are distinct
  states; `unknown` is never rendered as success. A partial harvest says so
  in the harvest result, in `list_knowledge_base`, and again every time the
  content is read back.

One extraction engine backs every surface, so an MCP client and the chat
panel return byte-identical results. Six source shapes behind one URL
field: `llms.txt`/`llms-full.txt`, OpenAPI/Swagger (JSON or YAML),
`sitemap.xml`, GitHub repositories, HTML documentation sites, raw Markdown.

## Operating Context

- The public site is served by the MCP server process itself (`/`, `/tools`,
  `/connect`) from Vercel at https://temp-repo-murex.vercel.app, beside the
  gated `/mcp` endpoint. Pages are self-contained HTML built by
  `scripts/build_site.py`; the server fills `{{VERSION}}`, `{{BASE_URL}}`
  at request time. No external scripts; fonts from Google Fonts with a
  system fallback.
- Connecting requires a bearer token the operator issues (or OAuth via the
  same token for ChatGPT). The site never contains the token; `/connect`
  splices a pasted token into copyable snippets client-side.
- The corpus lives in Postgres (Aiven); large harvests are run from a
  long-lived `python main.py` on the operator's machine and are readable
  through the hosted endpoint at once.

## Capabilities and Constraints

- Twelve tools over MCP: `learn_technology`, `harvest_docs`, `fetch_docs`,
  `save_docs`, `find_docs`, `scan_project`, `search_knowledge_base`,
  `read_knowledge_base`, `list_knowledge_base`, `detect_source_type`,
  `forget_resolution`, `forget_selection` (`forget_documentation` opt-in).
- Results handed to a model are capped at 60k characters with an explicit
  truncation marker.
- A harvest past the deadline (25 s) continues in the background on a
  long-lived host; on the serverless host it is reported as discarded.
- Versions are stored side by side; an unversioned site is labelled with
  the registry's current release and the result says so.
- Constraint: the site's pages must keep working with no script (content
  visible by default) and at 400px; nothing from the local web chat is
  served.

## Brand Commitments

- Name: **DocsForge**. Wordmark: the name beside a small lavender square.
- **The dark lavender theme is pinned by the user (2026-09-14): keep it.**
  Tokens from the web chat and the current site: page `#0b0b0d`, soft
  `#121215`, surface `#17171b`, hairlines `#2a2a31` / `#202027`, text
  `#ececee`, secondary `#a9a9b3`, tertiary `#7c7c88`, accent `#cf9fff`
  (ink on accent `#170b24`, tint `rgba(207,159,255,.13)`, line
  `rgba(207,159,255,.34)`), danger `#ff9a9a`; 14px radii on cards and
  buttons, 8px on chips; Inter for text, JetBrains Mono for code, tool
  names and figures. There is no light mode.
- Voice: plain, precise, unhyped; states limits as readily as strengths.
  No emoji, no stock illustration.
- Copy and sections of the current site are kept as they stand (confirmed
  2026-09-14); layout, hierarchy and motion may be redone.
- Home page composition (standing preference, 2026-09-14): the product-led
  landing page done at the craft level of Vercel and Linear — hero, the
  system shown in animated SVG diagrams, feature rows, architecture,
  close. Chosen by the user over the seed-assigned stacked deck
  (93c9c310), which was built, reviewed, and rejected as a form. No
  raster imagery is available; diagrams are authored SVG in the theme.

## Evidence on Hand

- Measured, quotable: 703 Effect pages in 6.3 MB; `'retry with
  exponential backoff'` → 6 pages ranked in 0.11 s; `'error handling'` →
  1 page by title in 0.20 s; langchain 627 pages / 8.9 MB streamed to
  Postgres; a real incompleteness line: *"INCOMPLETE — stopped at the
  200-page limit, 400+ pages still queued."*
- A real transcript shape: `learn_technology(name="pydantic",
  version="1.10")` → resolved to https://docs.pydantic.dev (names
  'pydantic' 214 times) → harvested 85 pages, stored as pydantic 1.10.
- The thirteen tools with arguments and returns (README "Tools exposed").
- Per-client connect snippets verified against each client's docs
  (`scripts/site_exports/connect.html`).
- Absent, never to be invented: customers, testimonials, logos, download
  counts, pricing, benchmarks against other tools.

## Product Principles

1. Truth over reassurance: coverage, versions and errors are reported as
   measured, and the site claims nothing the README cannot back.
2. The name is the interface: a developer should never need a URL to start.
3. One engine, identical answers on every surface.
4. The site exists to get a developer connected in a minute; everything on
   it serves that decision or the contributor's second door.
5. Keep working without help: no external scripts, content visible before
   any script runs, honours reduced motion.

## Accessibility & Inclusion

Keyboard-reachable client picker and copy control; visible focus; contrast
at or above 4.5:1 on body text against `#0b0b0d`/`#17171b`; `prefers-reduced-motion`
disables scroll-linked and entrance motion without hiding content.
