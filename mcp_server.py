#!/usr/bin/env python3
"""
DocsForge MCP server.

The product surface. Any MCP client — Claude Code, Claude Desktop, Cursor, an
agent framework — gets the same tools the web chat uses, because both are
generated from the one list in forge_tools.py.

That generation is the point. This file used to restate every tool by hand: its
description, its parameter types, its bounds. Two copies of a tool surface
always drift, and this one had: four tools existed in forge_tools and simply
did not exist over MCP, and `max_pages` was declared `ge=1, le=200` here while
the harvester treats 0 as unlimited — so an MCP client could not ask for a full
harvest at all. Now there is nothing to keep in sync.

Run:
  python mcp_server.py                  # stdio (what MCP clients launch)
  python mcp_server.py --http           # streamable HTTP on :8765
  python mcp_server.py --http --port 9000

Register with Claude Code:
  claude mcp add docsforge -- python E:/DocsForge/mcp_server.py
"""

from __future__ import annotations

import argparse
import inspect
import sys
from typing import Annotated, Any, Literal

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
        "Gives a model documentation for technologies it was not trained on. "
        "The usual entry point is learn_technology: pass the NAME of a library, "
        "framework or tool and DocsForge finds its official documentation, "
        "confirms the page really documents it, harvests the whole thing and "
        "stores it. You do not need a documentation URL, and you should not "
        "invent one — a remembered URL comes from the same knowledge that did "
        "not include the technology. "
        "Already harvested? read_knowledge_base and search_knowledge_base answer "
        "from the store without touching the network. Working in a repository? "
        "scan_project reports its dependencies, their versions, and which are "
        "already documented. Use fetch_docs and harvest_docs only when you "
        "genuinely already have a URL."
    ),
)


# ─────────────────────────────────────────────────────────────
# JSON Schema -> a typed Python signature the SDK can read
# ─────────────────────────────────────────────────────────────
SCALARS: dict[str, type] = {
    "string": str, "integer": int, "boolean": bool, "number": float,
}


def _annotation(spec: dict) -> tuple[Any, dict]:
    """The Python type for one JSON Schema property, plus its Field kwargs."""
    base: Any = SCALARS.get(spec.get("type", "string"), str)
    if spec.get("enum"):
        base = Literal[tuple(spec["enum"])]        # type: ignore[misc]

    kwargs: dict = {}
    if spec.get("description"):
        kwargs["description"] = spec["description"]
    # Constraints only apply to numbers; Field would reject them elsewhere.
    if base in (int, float):
        if "minimum" in spec:
            kwargs["ge"] = spec["minimum"]
        if "maximum" in spec:
            kwargs["le"] = spec["maximum"]
    return base, kwargs


def _build(tool: forge_tools.Tool):
    """A callable whose signature mirrors `tool.schema`.

    The SDK derives a tool's input schema from type hints, so the schema has to
    become a signature. Everything runs in a worker thread: the tool bodies are
    blocking, and a crawl must not stall the event loop serving the client.
    """
    properties: dict = tool.schema.get("properties") or {}
    required: list[str] = list(tool.schema.get("required") or [])

    params, hints = [], {}
    # Required first — a signature cannot put a defaulted parameter before one
    # without a default.
    for key in sorted(properties, key=lambda k: k not in required):
        spec = properties[key] or {}
        base, kwargs = _annotation(spec)

        if key in required:
            annotation = Annotated[base, Field(**kwargs)]
            params.append(inspect.Parameter(
                key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation))
        else:
            default = spec.get("default", None)
            if default is None:
                base = base | None
            annotation = Annotated[base, Field(**kwargs)]
            params.append(inspect.Parameter(
                key, inspect.Parameter.KEYWORD_ONLY,
                default=default, annotation=annotation))
        hints[key] = annotation

    async def run(**kwargs: Any) -> str:
        # Drop unset optionals so each tool's own defaults stay authoritative.
        given = {k: v for k, v in kwargs.items()
                 if v is not None or k in required}
        return await anyio.to_thread.run_sync(lambda: tool.fn(**given))

    run.__name__ = tool.name
    run.__doc__ = tool.description
    run.__signature__ = inspect.Signature(params, return_annotation=str)
    run.__annotations__ = dict(hints, **{"return": str})
    return run


def register(target: MCPServer = server) -> list[str]:
    """Expose every tool in forge_tools over MCP. Returns the names."""
    for tool in forge_tools.TOOLS:
        target.add_tool(_build(tool), name=tool.name, description=tool.description)
    return [t.name for t in forge_tools.TOOLS]


register()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="docsforge-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--list", action="store_true", help="print the tool surface and exit")
    args = ap.parse_args(argv)

    # Only stderr: on stdio transport, stdout is the JSON-RPC channel and the
    # SDK owns its encoding.
    enable_utf8_console(("stderr",))

    if args.list:
        for tool in forge_tools.TOOLS:
            print(f"{tool.name}\n    {tool.description[:120]}…")
        return 0

    if args.http:
        print(f"DocsForge MCP → http://{args.host}:{args.port}/mcp", file=sys.stderr)
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        # stdout is the protocol channel on stdio; never print to it.
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
