# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a developer working inside **FlowIT** who has hit the wall where the AI model does not know the technology they are working with — a library, framework, or API released or updated after the model's training, or simply too niche to have been learned well.

They are mid-task, not researching for its own sake. They know which technology is missing; they do not necessarily know where it documents itself, and a URL remembered by the model comes from the same training data that did not include the technology. Supplying a name is the ordinary case and supplying a URL is the shortcut.

Secondary: the same person using DocsForge standalone (CLI or MCP server) outside FlowIT, and automated callers — FlowIT itself at import and function-trigger time — that need to know whether the documentation they are about to reason from is complete.

## Product Purpose

DocsForge gives a model documentation for technologies it was not trained on, and tells the truth about how much of it there is.

Name a library and it works out where that library documents itself, confirms the page really documents it rather than merely mentioning it, harvests the whole set, and stores it. Point it at a URL instead and it identifies what kind of source the URL is and extracts accordingly. Either way the output is Markdown a model can read and a coverage figure a caller can act on.

Success is the wall coming down: the model, which a minute ago did not know the technology, now answers about it correctly — and where the harvest was partial, says so instead of guessing.

## Positioning

Most scrapers assume one shape of input. DocsForge detects the shape first and then extracts accordingly, which is why one URL field accepts six materially different kinds of source:

`llms.txt` · OpenAPI/Swagger (JSON or YAML) · sitemap.xml · GitHub repos · generic HTML docs sites · raw Markdown

Two things separate it from a scraper with a nice wrapper:

- **It is name-addressable.** The caller does not need a URL, and is discouraged from inventing one.
- **It reports coverage it has actually measured.** `complete`, `incomplete` and `unknown` are distinct states, and `unknown` is never rendered as success. A harvest that could not establish how much documentation existed says exactly that.

One extraction engine backs three surfaces — CLI, MCP server, and this chat panel — so an MCP client and the panel return byte-identical results.

## Operating Context

This surface is an **embedded panel inside FlowIT**, not a standalone site. It has to survive narrow widths and sit beside a host UI that owns the surrounding chrome.

The working loop: the user arrives already blocked, supplies a name or a question, watches the fetch happen, and leaves with documentation the model can use. Fetches take seconds to minutes, not milliseconds, and can partially fail — a page 404s, a crawl truncates, a rate limit hits. Those states are normal operating conditions, not exceptions.

Harvests of any size outlive a client's patience, so a harvest that passes the deadline returns a harvest id and continues in the background; the caller is told it is running rather than left holding an open connection.

The user's stated goal for this build remains experimental: to find out whether feeding harvested docs to a model actually clears the unknown-technology wall.

## Capabilities and Constraints

- Chat backed by any of six providers — Claude, Claude Code CLI, Ollama, Groq, ChatGPT, Gemini — streaming, with tool calling. Multiple providers is the point: when one provider's daily quota is gone, the others still work.
- Twelve tools, shared by the MCP server and every provider: `detect_source_type`, `fetch_docs`, `save_docs`, `harvest_docs`, `learn_technology`, `find_docs`, `search_knowledge_base`, `scan_project`, `list_knowledge_base`, `read_knowledge_base`, `forget_resolution`, `forget_selection`. A thirteenth, `forget_documentation`, is opt-in behind `DOCSFORGE_ALLOW_DELETE` because deletion is the one irreversible thing here.
- A technology is treated as a set of documentation bodies, not one tree. Corpora are discovered from link evidence gathered while crawling, admitted only through the identity gate, and classified by kind (what it is for). An unselected corpus is listed and marked `not requested`, never silently absent. Shape classification and magnitude estimation are implemented but not yet wired, so every corpus currently reports as a `tree` of unknown size (`ISSUES.md` W2, W3).
- `intent` decides which corpora enter scope; `strict` adds `usable_for_planning`, true only when every corpus the intent requires came back complete. Where the choice is genuinely ambiguous the answer is a `NEEDS SELECTION` refusal carrying the options, never a guess.
- Retrieval returns passages chunked on `h2`/`h3` with their heading paths, filterable by kind. Relevance applies at read time only; what is stored stays whole.
- Name resolution climbs a bounded ladder: memory, the project's own domain, package registries, the shape of a multi-word name, then registry search. Every lap ends at the same identity gate, and the whole ladder is capped at 40 requests. The domain lap also probes the `lang` shape a language whose bare name is a common word publishes on — `ziglang.org`, `nim-lang.org`, `rust-lang.org` — because for those names the project's own site is otherwise unreachable.
- A resolution is remembered for 30 days, a refusal for 7, and both are clearable with `forget_resolution`. Two things are never filed: an answer nothing could be read to reach — that is the network talking, not the name — and an entry decided under identity rules this build no longer applies, which is discarded on recall so a fix reaches the cache instead of being outlived by it.
- Harvests are stored and versioned. Two versions of one library are kept side by side rather than one overwriting the other, because they contradict each other.
- Storage is Markdown files by default; setting `DOCSFORGE_DB` moves it to Postgres with a full-text index. The zero-key, one-command path stays the default.
- Long harvests run in the background and report progress through `list_knowledge_base`, a tracker card in the panel, `GET /api/harvests`, and a line in the log and on the console at every phase change. Each job publishes a small status record under `~/.docsforge/harvests/` so a harvest started in one process is visible from another — which matters because the `claudecode` provider runs each turn's tools in a subprocess of its own. It is not a queue: nothing is resumed from a record, a harvest still dies with its process, and a record whose heartbeat stopped is reported *stalled* rather than left claiming to work. The durable record of a harvest is still the knowledge-base entry it writes.
- Every tool call is inspectable. Clicking its row in the panel opens a nested, live execution trace — what was run, with what input, and what came back — streamed incrementally over its own connection so a three-minute harvest is watchable for three minutes rather than explained after the fact. A parallel JSONL log at `logs/docsforge.log` records the same run for a developer who was not watching.
- Replies are always Markdown; the panel renders it and offers raw `.md`, copy, and download.
- Tool results are capped at 200,000 characters with an explicit truncation marker naming the sections omitted, and `read_knowledge_base` takes a `section` so any omitted part can be asked for directly. This is a read cap, not a storage one: files on disk and pages in the store are unbounded. It exists because the binding constraint is the provider's context window — and a result that overflows one fails the turn rather than truncating — so it is tunable by anyone who knows their own.
- Stateless server; the browser holds the conversation and posts it back each turn.
- No frontend build step — vanilla HTML/CSS/JS served by FastAPI from `static/`.
- Rendered Markdown is sanitized (`nh3`) because it mixes model output with scraped HTML.
- Fetches to private/loopback addresses are refused; `save_docs` cannot write outside its output root.
- Crawling is rate-limited by a delay and a page cap; JS rendering is opt-in and slow.

## Brand Commitments

Name: **DocsForge**. It is a component of **FlowIT** and will be embedded in it.

## Evidence on Hand

Real and verified in this build — do not fabricate beyond it:

- 860 passing offline unit tests (`tests/`), 59 skipped behind opt-in gates (live network, Postgres, browser rendering). Verified by a full run, not asserted.
- Live extraction against `petstore3.swagger.io`, `github.com/psf/requests`, `docs.python.org`, and a crawl of `fastapi.tiangolo.com`; plus live smoke tests for MCP stdio, the web API, and a two-turn conversation.
- A full `learn_technology("mojo")` from the name alone, into a throwaway store, reproduced three times: 211 pages, 3,403,639 characters, entirely from `mojolang.org`, labelled `1.0.0` from the manifest's own declaration, in 125–205s.
- Installable: `pyproject.toml` ships `docsforge` and `docsforge-mcp` console scripts, both verified to run from an editable install.
- No users, no pricing, no deployment. None exist yet; nothing may claim otherwise. There is now one benchmark — `measure_answers.py`, which asks whether a stored corpus changes what a model can answer — and its numbers are the only ones that may be cited about that question.

Known open defects, kept here rather than only in `AUDIT.md`:

- Verification confirms a page is about something *with that name*, and that is still not the same as confirming the project. Measured 2026-09-10 over 25 judged names, cold cache: **23 correct**, up from 19 before that round of work. `flask` still reaches an unrelated to-do app at flask.io and `polars` a third-party site; both survive on hostname ownership plus repeated mentions, which is the one remaining path a name-squatter satisfies and the one `repo-identity` cannot outrank. Closing it means raising the identity gate, which is a decision about a stated invariant rather than a fix. `numpy` and `github actions` were not re-measured in that run. See `Resolved.md`, `FINDINGS-B.md` and `FINDINGS-C.md`.

- Two further routes to a *confidently wrong* answer were closed after being caught live, both in the ownership signal rather than the mention count. A domain probe that redirected onto a code host kept its ownership claim, so `mojo.dev` → `github.com/gdejohn/procrastination` — a Java library, since Maven plugins are also called "mojos" — was stamped `verified` on a page that never says the word. And a language whose bare name is a common word was refused a claim on the `lang` domain it publishes from, so nothing ever probed `ziglang.org` or `nim-lang.org` and a registry package answered instead. Measured after the fix: `zig` → `ziglang.org`, `nim` → `nim-lang.org/documentation.html`, `mojo` → `mojolang.org`, each on `own-domain`. Three further routes were closed on 2026-09-10, each after being caught live: a first-come `<name>.github.io` label and a country-code community mirror no longer outrank the project's own domain (`tensorflow` reached the Rust bindings' rustdoc, `pytorch` the Korean user group); a sub-project documented on its parent's host can now be identified through a registry-nominated URL whose path names it (`langgraph`); and `djangoproject.com` counts as Django's own domain. See `Resolved.md`.
- A technology whose documentation spans several sites is discovered, admitted through the identity gate, classified, and — where the caller's intent selects it — harvested and filed as its own corpus with its own count. Corpora left out are listed with their size and marked `not requested`.
- `BREADTH_LIMIT` and the density and kind-confidence thresholds are provisional numbers, not measured ones. See `ISSUES.md` F2 and E4.

## Product Principles

1. **Detect before extracting.** The user should never have to tell it what kind of source they pasted.
2. **A name is enough.** Requiring a URL asks the user for the thing they came here missing.
3. **Never report unearned confidence.** Measured coverage or `unknown` — never a number that looks like a guarantee and is not.
4. **Show the fetch.** What was fetched, how much came back, and what was truncated are part of the answer, not debug noise.
5. **Partial failure is normal.** One dead page must never end a run or hide the pages that worked.
6. **One engine, three surfaces.** CLI, MCP, and panel never drift apart.

## Accessibility & Inclusion

No product-specific requirement established beyond ordinary web accessibility: keyboard-operable composer, visible focus, and text that survives narrow embedded widths.
