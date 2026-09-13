#!/usr/bin/env python3
"""
Start the DocsForge MCP server.

    python main.py            # the site and /mcp on http://127.0.0.1:8765
    python main.py --list     # print the tool surface and exit

One command, two callers. At a terminal it serves HTTP: the site at `/` and
the MCP endpoint at `/mcp`. Launched by an MCP client — over pipes, never a
terminal — it speaks MCP on stdin/stdout instead. `--http` and `--stdio`
force either. Hosted, set DOCSFORGE_MCP_TOKEN and bind 0.0.0.0 — see the
Containerfile.

This is the only script in the repository root; everything else lives in the
`docsforge` package. Being here is what makes it launchable by path from any
MCP client config — Python puts this file's directory on `sys.path`, which is
exactly where the package is.
"""

import sys

from docsforge.server.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
