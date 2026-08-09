# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a developer working inside **FlowIT** who has hit the wall where the AI model does not know the technology they are working with — a library, framework, or API released or updated after the model's training, or simply too niche to have been learned well.

They are mid-task, not researching for its own sake. They already know which technology is missing and usually have a URL for it: a docs site, an OpenAPI spec, a GitHub repo. What they lack is that knowledge in a form the model can actually consume.

Secondary: the same person using DocsForge standalone (CLI or MCP server) outside FlowIT.

## Product Purpose

DocsForge turns any documentation source into clean Markdown that a language model can read.

Point it at a URL; it identifies what kind of source that URL is and extracts it accordingly. The output is a Markdown document the user keeps — pasted into a prompt, saved to disk, or handed to a model as a tool result.

Success is the wall coming down: the model, which a minute ago did not know the technology, now answers about it correctly because the docs are in front of it.

## Positioning

Most scrapers assume one shape of input. DocsForge detects the shape first and then extracts accordingly, which is why one URL field can accept six materially different kinds of source:

`llms.txt` · OpenAPI/Swagger (JSON or YAML) · sitemap.xml · GitHub repos · generic HTML docs sites · raw Markdown

One extraction engine backs three surfaces — CLI, MCP server, and this chat panel — so an MCP client and the panel return byte-identical results.

## Operating Context

This surface is an **embedded panel inside FlowIT**, not a standalone site. It has to survive narrow widths and sit beside a host UI that owns the surrounding chrome.

The working loop: the user arrives already blocked, supplies a URL or a question, watches the fetch happen, and leaves with a Markdown document. Fetches take seconds, not milliseconds, and can partially fail — a page 404s, a crawl truncates, a rate limit hits. Those states are normal operating conditions, not exceptions.

The user's stated goal for this build is experimental: to find out whether feeding fetched docs to a model actually clears the unknown-technology wall.

## Capabilities and Constraints

- Chat backed by Groq, `llama-3.3-70b-versatile`, streaming, with tool calling.
- Three tools, shared with the MCP server: `detect_source_type`, `fetch_docs`, `save_docs`.
- Replies are always Markdown; the panel renders it and offers raw `.md`, copy, and download.
- Tool results are capped at 60,000 characters with an explicit truncation marker.
- Stateless server; the browser holds the conversation and posts it back each turn.
- No frontend build step — vanilla HTML/CSS/JS served by FastAPI from `static/`.
- Rendered Markdown is sanitized (`nh3`) because it mixes model output with scraped HTML.
- Fetches to private/loopback addresses are refused; `save_docs` cannot write outside its output root.
- Crawling is rate-limited by a delay and a page cap; JS rendering is opt-in and slow.

## Brand Commitments

Name: **DocsForge**. It is a component of **FlowIT** and will be embedded in it.

## Evidence on Hand

Real and verified in this build — do not fabricate beyond it:

- Live extraction against `petstore3.swagger.io` (8,761 chars), `github.com/psf/requests`, `docs.python.org`, and a 4-page crawl of `fastapi.tiangolo.com`.
- 42 passing offline unit tests (`tests/`), plus live smoke tests for MCP stdio, the web API, and a two-turn conversation.
- No users, no benchmarks, no pricing, no deployment. None exist yet; nothing may claim otherwise.

## Product Principles

1. **Detect before extracting.** The user should never have to tell it what kind of source they pasted.
2. **The Markdown is the deliverable.** Everything else is how you request it.
3. **Show the fetch.** What was fetched, how much came back, and what was truncated are part of the answer, not debug noise.
4. **Partial failure is normal.** One dead page must never end a run or hide the pages that worked.
5. **One engine, three surfaces.** CLI, MCP, and panel never drift apart.

## Accessibility & Inclusion

No product-specific requirement established beyond ordinary web accessibility: keyboard-operable composer, visible focus, and text that survives narrow embedded widths.
