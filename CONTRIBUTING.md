# Contributing to DocsForge

DocsForge is an open-source documentation MCP server, built and maintained by
**Raj Patel** ([portfolio](https://portfolio-raj-25.netlify.app/) ·
[LinkedIn](https://www.linkedin.com/in/raj-patel5/) ·
[GitHub](https://github.com/R204570)). Bug reports, fixes and ideas are all
welcome.

## Report a problem

Found a bug, a bad extraction, or a library DocsForge resolves to the wrong
documentation? [Open an issue](https://github.com/R204570/DocsForge/issues)
with as much detail as you can: the tool you called, the name or URL you gave
it, what came back, and what you expected. Every report makes it more
reliable.

## Propose a change

Email **rajpatel9408019@gmail.com** with the subject
**DocsForge Contribution Proposal**, and include:

- Your **GitHub** — this is what I read to get a picture of your work
- Your experience with crawlers, MCP tools or AI agents, or any related project
- What you'd like to work on, and any ideas you already have

Useful ground: crawling and scraping, documentation extraction, MCP tools, AI
agents, backend and developer tooling. Bug fixes and documentation count as
much as new crawling strategies.

## Working on the code

```bash
git clone https://github.com/R204570/DocsForge.git
cd DocsForge
pip install -r requirements.txt pytest
python -m pytest -q
```

- The public site (`docsforge.vercel.app`) is built from `scripts/site_src/`
  by `python scripts/build_site.py`; commit the rebuilt files under
  `docsforge/server/site/` with your change.
- Search-engine and social metadata are described in
  [`System Files/SEO.md`](System%20Files/SEO.md).
- The architecture and the reasons behind it are in
  [`System Files/Architecture.md`](System%20Files/Architecture.md) and
  [`System Files/Design.md`](System%20Files/Design.md).
