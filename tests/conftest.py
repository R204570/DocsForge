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

_PRODUCTION_VARS = ("DOCSFORGE_DB", "DATABASE_URL", "DOCSFORGE_KB_ROOT",
                    "DOCSFORGE_OUT_ROOT")


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
    import forge_tools

    forge_tools.reset_store(None)
    yield
    forge_tools.reset_store(None)
