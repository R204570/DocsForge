#!/usr/bin/env python3
"""
DocsForge MCP server.

Exposes the DocsForge extraction tools over the Model Context Protocol so any
MCP client (Claude Code, Claude Desktop, an agent framework) can pull clean
Markdown docs on demand.

Run:
  python mcp_server.py                  # stdio (what MCP clients launch)
  python mcp_server.py --http           # streamable HTTP on :8765
  python mcp_server.py --http --port 9000

Register with Claude Code:
  claude mcp add docsforge -- python E:/DocsForge/mcp_server.py

The tool bodies live in forge_tools.py, which app.py also uses — the web chat
and MCP clients therefore run exactly the same code.
"""

from __future__ import annotations

import argparse
import sys
from typing import Annotated, Literal

import anyio
from pydantic import Field

from mcp.server import MCPServer

import forge_tools
from docsforge import __version__, enable_utf8_console

server = MCPServer(
    name="docsforge",
    title="DocsForge",
    version=__version__,
    instructions=(
        "Extracts software documentation from any URL into clean Markdown. "
        "Handles llms.txt, OpenAPI/Swagger specs, sitemap.xml, GitHub repos, and "
        "generic HTML docs sites. Call detect_source_type first if you are unsure "
        "what a URL points at; otherwise go straight to fetch_docs."
    ),
)

Url = Annotated[str, Field(description="Absolute http(s) URL of the documentation source.")]
Crawl = Annotated[bool, Field(description="Follow same-host links from the start URL. HTML sources only.")]
MaxPages = Annotated[int, Field(ge=1, le=200, description="Maximum number of pages to fetch.")]
Js = Annotated[bool, Field(description="Render JavaScript with Playwright. Slow; only for client-rendered sites.")]
Force = Annotated[
    Literal["llms_txt", "openapi", "sitemap", "github", "raw_text", "html"] | None,
    Field(description="Skip auto-detection and force a specific extraction strategy."),
]


@server.tool(
    description=(
        "Identify what kind of documentation source a URL is (llms_txt, openapi, "
        "sitemap, github, raw_text, or html) without extracting it. Cheap probe — "
        "use it first when you are unsure what a URL points at."
    )
)
async def detect_source_type(url: Url) -> str:
    return await anyio.to_thread.run_sync(forge_tools.tool_detect_source_type, url)


@server.tool(
    description=(
        "Extract documentation from any URL and return it as clean Markdown. "
        "Auto-detects the source type: llms.txt, OpenAPI/Swagger specs (rendered as "
        "endpoint tables), sitemap.xml, GitHub repos (README + docs/), raw Markdown, "
        "or a generic HTML docs site with nav and chrome stripped out."
    )
)
async def fetch_docs(
    url: Url,
    crawl: Crawl = False,
    max_pages: MaxPages = 25,
    js: Js = False,
    force: Force = None,
) -> str:
    return await anyio.to_thread.run_sync(
        lambda: forge_tools.tool_fetch_docs(url, crawl=crawl, max_pages=max_pages, js=js, force=force)
    )


@server.tool(
    description=(
        "Extract documentation from a URL and write it to Markdown files on disk. "
        "Use when the caller wants the docs saved rather than returned inline. "
        "Returns the list of paths written."
    )
)
async def save_docs(
    url: Url,
    out_dir: Annotated[str, Field(description="Directory under the output root to write into.")] = "docs_md",
    crawl: Crawl = False,
    max_pages: MaxPages = 25,
    js: Js = False,
    force: Force = None,
    single_file: Annotated[bool, Field(description="Concatenate everything into one .md file.")] = False,
) -> str:
    return await anyio.to_thread.run_sync(
        lambda: forge_tools.tool_save_docs(
            url, out_dir=out_dir, crawl=crawl, max_pages=max_pages,
            js=js, force=force, single_file=single_file,
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="docsforge-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    # Only stderr: on stdio transport, stdout is the JSON-RPC channel and the
    # SDK owns its encoding.
    enable_utf8_console(("stderr",))

    if args.http:
        print(f"DocsForge MCP → http://{args.host}:{args.port}/mcp", file=sys.stderr)
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        # stdout is the protocol channel on stdio; never print to it.
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
