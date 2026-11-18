"""
Contract v1, Phase 3 (issue #42): a re-harvest no longer overwrites.

Promise 3 in Discussion #35: a re-harvest gets a new `harvest_id` and the
revision it replaces is kept, so an importer that recorded an observation
against a `harvest_id` can still fetch exactly that revision. Readers are
unchanged — they see the current revision only — and the newest
`DOCSFORGE_KEEP_HARVESTS` superseded revisions are kept (3 by default).
"""

from __future__ import annotations

import os

import pytest

from docsforge.store import contract
from docsforge.store.contract import parse_revision, validate
from docsforge.store.kb_store import FileStore, PostgresStore, StoreError
from docsforge.tools import forge_tools as ft

DSN = os.environ.get("DOCSFORGE_TEST_DB", "")


def _harvest(store, tech: str, marker: str, version: str = "v1", pages: int = 2) -> dict:
    with store.writer(tech, version, f"https://{tech}.dev/", "sitemap", expected=pages) as w:
        for i in range(pages):
            w.add(f"Page {i}", f"https://{tech}.dev/{i}", f"{marker} body {i}")
        return w.settle(complete=True, expected=pages, facts={"count_basis": "sitemap"})


def _pg_or_skip() -> PostgresStore:
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


@pytest.fixture(params=["files", "postgres"])
def store(request, tmp_path):
    if request.param == "files":
        yield FileStore(tmp_path / "kb")
        return
    db = _pg_or_skip()
    yield db
    for tech in ("hist-a", "hist-b", "hist-c", "hist-d", "hist-e", "hist-f"):
        db.delete(tech)


# ── promise 3 ───────────────────────────────────────────────────
def test_reharvesting_keeps_the_previous_revision(store):
    first = _harvest(store, "hist-a", "OLD")
    second = _harvest(store, "hist-a", "NEW")
    assert first["harvest_id"] != second["harvest_id"]

    current = parse_revision(store.export("hist-a", "v1"))
    assert current.meta["harvest_id"] == second["harvest_id"]
    assert current.pages[0].body == "NEW body 0"

    old = parse_revision(store.export("hist-a", "v1", first["harvest_id"]))
    assert old.meta["harvest_id"] == first["harvest_id"]
    assert old.pages[0].body == "OLD body 0"
    assert validate(old) == []

    history = store.history("hist-a", "v1")
    assert [h["harvest_id"] for h in history] == [second["harvest_id"], first["harvest_id"]]
    assert [h["current"] for h in history] == [True, False]


def test_history_is_never_read_listed_or_searched(store):
    _harvest(store, "hist-b", "zebrafish")
    _harvest(store, "hist-b", "NEW")
    assert len(store.versions("hist-b")) == 1
    assert store.entry("hist-b", "v1")["pages"] == 2
    assert all("zebrafish" not in store.page("hist-b", "v1", i)["content"] for i in (1, 2))
    assert store.search("zebrafish", tech="hist-b") == []


def test_retention_keeps_the_newest_n(store, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_KEEP_HARVESTS", "2")
    made = [_harvest(store, "hist-c", f"R{i}") for i in range(5)]
    kept = [h["harvest_id"] for h in store.history("hist-c", "v1")]
    assert kept == [m["harvest_id"] for m in reversed(made)][:3], "current + the newest 2"
    with pytest.raises(StoreError, match="no revision"):
        store.export("hist-c", "v1", made[0]["harvest_id"])


def test_keeping_none_is_the_old_behaviour(store, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_KEEP_HARVESTS", "0")
    _harvest(store, "hist-d", "A")
    latest = _harvest(store, "hist-d", "B")
    assert [h["harvest_id"] for h in store.history("hist-d", "v1")] == [latest["harvest_id"]]


def test_forgetting_a_version_forgets_its_history(store):
    first = _harvest(store, "hist-e", "OLD")
    _harvest(store, "hist-e", "NEW")
    assert store.delete("hist-e", "v1") == 1, "one version, however many revisions"
    with pytest.raises(StoreError):
        store.export("hist-e", "v1", first["harvest_id"])
    _harvest(store, "hist-e", "AGAIN")
    assert len(store.history("hist-e", "v1")) == 1, "nothing came back with it"


def test_pruning_never_touches_the_current_revision(store, monkeypatch):
    monkeypatch.setenv("DOCSFORGE_KEEP_HARVESTS", "1")
    for i in range(4):
        latest = _harvest(store, "hist-f", f"R{i}")
    history = store.history("hist-f", "v1")
    assert history[0]["harvest_id"] == latest["harvest_id"] and history[0]["current"]
    assert parse_revision(store.export("hist-f", "v1")).pages[0].body == "R3 body 0"


# ── the file store specifically ─────────────────────────────────
def test_a_superseded_file_is_not_in_a_rebuilt_index(tmp_path):
    files = FileStore(tmp_path / "kb")
    _harvest(files, "hist-g", "OLD")
    _harvest(files, "hist-g", "NEW")
    files.index_path.write_text("{ damaged", encoding="utf-8")
    assert [v["version"] for v in files.versions("hist-g")] == ["v1"]
    assert files.entry("hist-g", "v1")["pages"] == 2


def test_a_contract_0_revision_is_kept_and_exports_as_legacy(tmp_path):
    files = FileStore(tmp_path / "kb")
    folder = files.root / "hist-h"
    folder.mkdir(parents=True)
    legacy = ("# hist-h v1 documentation\n\n## Contents\n\n1. [Old](https://h.dev/o)\n\n\n"
              "---\n\n## Old\n\nSource: <https://h.dev/o>\n\nold text\n")
    (folder / "v1.md").write_text(legacy, encoding="utf-8", newline="\n")
    files.index_path.write_text(
        '{"hist-h@v1": {"technology": "hist-h", "version": "v1", "source": "https://h.dev/", '
        '"strategy": "crawl", "pages": 1, "characters": 1, "file": "%s", "harvested": "", '
        '"saved": 1.0, "complete": null, "expected": null, "titles": ["Old"]}}'
        % str(folder / "v1.md").replace("\\", "\\\\"), encoding="utf-8")
    old_id = files.history("hist-h", "v1")[0]["harvest_id"]
    assert old_id.startswith("legacy-")

    _harvest(files, "hist-h", "NEW")
    old = parse_revision(files.export("hist-h", "v1", old_id))
    assert old.meta["harvest_id"] == old_id and old.meta["count_basis"] == "legacy"
    assert old.pages[0].body == "old text"


def test_the_library_view_lists_the_history(tmp_path):
    from starlette.testclient import TestClient
    from docsforge.server import app

    files = FileStore(tmp_path / "kb")
    first = _harvest(files, "hist-i", "OLD")
    _harvest(files, "hist-i", "NEW")
    ft.reset_store(files)
    try:
        with TestClient(app.app) as client:
            listed = client.get("/api/library/hist-i/v1").json()["history"]
            assert [h["current"] for h in listed] == [True, False]
            r = client.get(f"/api/library/hist-i/v1/export?harvest_id={first['harvest_id']}")
            assert r.status_code == 200
            assert parse_revision(r.text).meta["harvest_id"] == first["harvest_id"]
    finally:
        ft.reset_store(None)


def test_the_export_command_takes_a_superseded_revision(tmp_path, monkeypatch, capsysbinary):
    from docsforge.core import engine

    files = FileStore(tmp_path / "kb")
    first = _harvest(files, "hist-j", "OLD")
    _harvest(files, "hist-j", "NEW")
    monkeypatch.setenv("DOCSFORGE_KB_ROOT", str(files.root))
    monkeypatch.delenv("DOCSFORGE_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert engine.main(["--export", f"hist-j@v1#{first['harvest_id']}"]) == 0
    out = capsysbinary.readouterr().out.decode("utf-8")
    assert parse_revision(out).pages[0].body == "OLD body 0"
