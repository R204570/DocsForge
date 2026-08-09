"""Live MCP smoke test — spawns mcp_server.py over stdio, lists tools, calls one.

Not collected by pytest (needs a subprocess and the network).
Run: python tests/smoke_mcp.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from mcp import ClientSession, StdioServerParameters, stdio_client


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "mcp_server.py")],
        cwd=ROOT,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected: {init.server_info.name} v{init.server_info.version}")

            listed = await session.list_tools()
            print(f"\n{len(listed.tools)} tools:")
            for tool in listed.tools:
                props = list((tool.input_schema.get("properties") or {}).keys())
                print(f"  - {tool.name}({', '.join(props)})")
                print(f"      {tool.description[:88]}...")

            print("\ncalling detect_source_type …")
            res = await session.call_tool(
                "detect_source_type",
                {"url": "https://petstore3.swagger.io/api/v3/openapi.json"},
            )
            print("  ->", res.content[0].text)

            print("\ncalling fetch_docs …")
            res = await session.call_tool(
                "fetch_docs",
                {"url": "https://petstore3.swagger.io/api/v3/openapi.json"},
            )
            text = res.content[0].text
            print(f"  -> {len(text):,} chars of Markdown")
            print("  ->", text.split("\n")[2][:70])

            print("\ncalling fetch_docs with a bad URL (should degrade, not crash) …")
            res = await session.call_tool("fetch_docs", {"url": "not-a-url"})
            print("  ->", res.content[0].text[:100])

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
