"""
Shared test setup.

`app.py` calls load_dotenv() at import time, so importing it in a test pulls the
developer's real .env into os.environ for the rest of the session — including
DOCSFORGE_DB. That silently pointed the knowledge-base tests at a live database
instead of a temporary one.

Tests therefore run with the production storage variables stripped, and opt into
a real database explicitly through DOCSFORGE_TEST_DB.
"""

import os

import pytest

#: Set this to a throwaway database to exercise the Postgres backend, e.g.
#:   DOCSFORGE_TEST_DB=postgresql://postgres:pw@127.0.0.1:5432/DocsForge
TEST_DB_VAR = "DOCSFORGE_TEST_DB"

#: `DOCSFORGE_MAX_CHARS` is here for the same reason as the rest: a developer's
#: own `.env` should not decide what the suite asserts. One was found setting it
#: twice — 60000 then 80000 — so the machine's real read cap was neither the
#: code's default nor the first line of its own config.
_PRODUCTION_VARS = ("DOCSFORGE_DB", "DATABASE_URL", "DOCSFORGE_KB_ROOT",
                    "DOCSFORGE_OUT_ROOT", "DOCSFORGE_SEARCH",
                    "DOCSFORGE_MAX_CHARS")


@pytest.fixture(autouse=True)
def _isolate_resolution_memory(tmp_path, monkeypatch):
    """Point L0 at a throwaway file.

    The resolution cache defaults to the developer's home directory. Without
    this a test run would read, and then write, whatever they had resolved for
    real — which both leaks state between runs and makes a cached wrong answer
    survive the suite that exists to catch it.
    """
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE",
                       str(tmp_path / "resolutions.json"))
    monkeypatch.setenv("DOCSFORGE_SELECTION_POLICY",
                       str(tmp_path / "selection.json"))
    # Harvest status records default to the same home directory, and a suite
    # that started background harvests there would show up in the developer's
    # own `list_knowledge_base` as jobs that stalled.
    monkeypatch.setenv("DOCSFORGE_HARVEST_STATE", str(tmp_path / "harvests"))


@pytest.fixture(autouse=True, scope="session")
def _isolate_storage_env():
    """Keep the suite off whatever the developer has configured for real use."""
    saved = {k: os.environ.pop(k, None) for k in _PRODUCTION_VARS}
    yield
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _reset_store_between_tests():
    """No test should inherit the backend another test installed."""
    from docsforge.tools import forge_tools

    forge_tools.reset_store(None)
    yield
    forge_tools.reset_store(None)
