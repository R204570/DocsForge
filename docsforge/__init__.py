"""
DocsForge — documentation for technologies a model was not trained on.

The package is laid out by responsibility:

    docsforge.core       fetch, extract, crawl, resolve a name to a docs site
    docsforge.store      where harvested documentation lives (files | Postgres)
    docsforge.tools      the one tool layer every surface shares, and its plumbing
    docsforge.server     the MCP server and the web chat
    docsforge.providers  model backends for the web chat

Entry points: `main.py` at the repository root starts the MCP server;
`python -m docsforge <URL>` is the extraction CLI.

Nothing is imported here on purpose. `__version__` has to be readable without
pulling in `requests` and `bs4` (packaging reads it statically), and a package
`__init__` that imports its own submodules is how import cycles start.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "1.1.0"

#: The directory that holds `main.py`, `knowledge_base/` and `.env` in a
#: checkout — the parent of this package. Everything that used to anchor a
#: path to "the module's own folder" when the modules sat in the repository
#: root anchors to this instead, so moving the modules moved none of the data.
ROOT = Path(__file__).resolve().parent.parent
