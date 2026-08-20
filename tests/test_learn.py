"""
Offline tests for the name-addressable tools — no network.

`resolver.resolve` is stubbed throughout: what matters here is whether the
tools do the right thing with a resolution, not whether the registries are up.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forge_tools as ft
import resolver
from kb_store import FileStore

PAGES = [
    ("Error Handling", "https://x.dev/docs/errors", "fail fast and recover"),
    ("Layers", "https://x.dev/docs/layers", "wiring services with npm install"),
]


@pytest.fixture
def kb(tmp_path):
    ft.reset_store(FileStore(tmp_path))
    yield tmp_path
    ft.reset_store(None)


def stored(name="effect", version="v3", pages=PAGES):
    return ft.store().save(name, version, "https://x.dev/docs/v3/", "crawl",
                           pages, complete=True)


def resolution(url="https://x.dev/docs/", verified=True, name="effect"):
    got = resolver.Resolution(name=name, ecosystem="npm")
    cand = resolver.Candidate(url, "npm:homepage", 0.8, "stubbed", verified,
                              "names it 9 times" if verified else "never mentions it")
    got.candidates = [cand]
    got.best = cand if verified else None
    if not verified:
        got.note = "Found 1 candidate(s) but none could be confirmed to document it."
    return got


# ── matching a caller's spelling ─────────────────────────
@pytest.mark.parametrize("asked", ["effect", "Effect", "Effect.ts", "effect-ts", "EFFECT"])
def test_a_stored_technology_is_found_however_it_is_spelled(kb, asked):
    stored()
    assert ft.stored_name(asked) == "effect"


def test_an_unknown_name_matches_nothing(kb):
    stored()
    assert ft.stored_name("nothing-like-this") is None


def test_an_ambiguous_prefix_is_refused_rather_than_guessed(kb):
    # Two candidates and no way to choose: answering from the wrong manual is
    # worse than saying nothing.
    stored("react", "v18", PAGES)
    stored("react-query", "v5", PAGES)
    assert ft.stored_name("rea") is None


def test_reading_works_through_an_alias(kb):
    stored()
    assert "fail fast" in ft.tool_read_knowledge_base("Effect.ts", section="error")


# ── learn_technology ─────────────────────────────────────
def test_already_stored_fetches_nothing(kb, monkeypatch):
    stored()
    monkeypatch.setattr(ft, "_resolve",
                        lambda *a, **k: pytest.fail("must not resolve when stored"))
    out = ft.tool_learn_technology("Effect.ts")
    assert "already stored" in out and "Nothing was fetched" in out


def test_a_resolved_name_is_harvested_under_that_name(kb, monkeypatch):
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    seen = {}

    def fake_harvest(url, name=None, max_pages=0, js=False, version=None, **kw):
        seen.update(url=url, name=name, version=version)
        return "Harvested **effect** v3 — 2 pages"

    monkeypatch.setattr(ft, "tool_harvest_docs", fake_harvest)
    out = ft.tool_learn_technology("Effect.ts", version="v3")

    assert seen["url"] == "https://x.dev/docs/"
    # Filed canonically, so "Effect.ts" and "effect" cannot become two copies
    # of the same library.
    assert seen["name"] == "effect"
    assert seen["version"] == "v3"
    assert "Resolved" in out and "Harvested" in out


@pytest.mark.parametrize("spelling", ["effect", "Effect.ts", "effect-ts"])
def test_every_spelling_files_under_the_same_name(kb, monkeypatch, spelling):
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    seen = {}
    monkeypatch.setattr(ft, "tool_harvest_docs",
                        lambda url, name=None, **kw: seen.setdefault("name", name) or "ok")
    ft.tool_learn_technology(spelling)
    assert seen["name"] == "effect"


def test_an_unverifiable_name_refuses_to_harvest(kb, monkeypatch):
    # The whole point of verification: never hand back a plausible wrong
    # project, and never quietly harvest one.
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution(verified=False))
    monkeypatch.setattr(ft, "tool_harvest_docs",
                        lambda *a, **k: pytest.fail("must not harvest unverified"))

    with pytest.raises(ft.ForgeError) as excinfo:
        ft.tool_learn_technology("ghost")
    message = str(excinfo.value)
    assert "none could be confirmed" in message
    assert "harvest_docs" in message, "it should say how to proceed manually"
    assert "https://x.dev/docs/" in message, "and show what it considered"


def test_asking_for_a_version_that_is_not_stored_harvests_it(kb, monkeypatch):
    stored("effect", "v3")
    monkeypatch.setattr(ft, "_resolve", lambda *a, **k: resolution())
    monkeypatch.setattr(ft, "tool_harvest_docs", lambda *a, **k: "Harvested v2")
    out = ft.tool_learn_technology("effect", version="v2")
    assert "not version 'v2'" in out and "have: v3" in out


# ── search ───────────────────────────────────────────────
def test_search_finds_a_page_without_knowing_the_technology(kb):
    stored()
    out = ft.tool_search_knowledge_base("npm")
    assert "effect" in out and "Layers" in out


def test_search_can_be_scoped_by_alias(kb):
    stored()
    assert "Layers" in ft.tool_search_knowledge_base("npm", technology="Effect.ts")


def test_searching_an_unstored_technology_says_so(kb):
    stored()
    with pytest.raises(ft.ForgeError, match="Nothing stored under"):
        ft.tool_search_knowledge_base("npm", technology="django")


def test_no_matches_points_at_learn_technology(kb):
    stored()
    assert "learn_technology" in ft.tool_search_knowledge_base("quantum tunnelling")


# ── scan_project ─────────────────────────────────────────
def test_scan_reports_which_dependencies_are_documented(kb, tmp_path):
    stored("effect", "v3")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies": {"effect": "^3.1.0", "zod": "^3.23.0"}}', encoding="utf-8")

    out = ft.tool_scan_project(path=str(project))
    assert "stored as **effect**" in out
    assert "`zod` 3.23.0" in out and "not stored" in out
    assert "learn_technology" in out, "it should say what to do about the gap"


def test_scan_can_list_only_the_gaps(kb, tmp_path):
    stored("effect", "v3")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text(
        '{"dependencies": {"effect": "^3.1.0", "zod": "^3.23.0"}}', encoding="utf-8")

    out = ft.tool_scan_project(path=str(project), unknown_only=True)
    assert "zod" in out
    assert "1 of 2" in out
    assert "`effect`" not in out


def test_scan_without_manifests_explains_what_it_looked_for(kb, tmp_path):
    with pytest.raises(ft.ForgeError, match="package.json"):
        ft.tool_scan_project(path=str(tmp_path))


def test_scan_rejects_a_path_that_is_not_a_directory(kb, tmp_path):
    with pytest.raises(ft.ForgeError, match="Not a directory"):
        ft.tool_scan_project(path=str(tmp_path / "nope"))


# ── the tool surface ─────────────────────────────────────
def test_the_new_tools_are_exposed_to_models():
    names = {t.name for t in ft.TOOLS}
    assert {"learn_technology", "find_docs",
            "search_knowledge_base", "scan_project"} <= names


def test_learn_technology_needs_only_a_name():
    tool = ft.BY_NAME["learn_technology"]
    assert tool.schema["required"] == ["name"]
    assert "url" not in tool.schema["properties"], "needing a URL is the bug it fixes"


def test_every_tool_schema_is_well_formed():
    for tool in ft.TOOLS:
        assert tool.schema["type"] == "object"
        for field in tool.schema.get("required", []):
            assert field in tool.schema["properties"], f"{tool.name}: {field}"


# ── deletion ─────────────────────────────────────────────
def test_models_cannot_delete_documentation_by_default():
    """The one irreversible thing DocsForge does, and a model that has just
    mis-resolved a name is the last caller who should hold that lever."""
    assert ft.ALLOW_DELETE is False
    assert "forget_documentation" not in {t.name for t in ft.TOOLS}


def test_forget_removes_one_version_and_keeps_the_rest(kb):
    stored(version="v2", pages=PAGES[:1])
    stored(version="v3")
    out = ft.tool_forget_documentation("effect", "v2")
    assert "Deleted" in out and "v2" in out
    assert [v["version"] for v in ft.store().versions("effect")] == ["v3"]
    assert "1 other version" in out


def test_forget_without_a_version_removes_the_technology(kb):
    stored(version="v2")
    stored(version="v3")
    ft.tool_forget_documentation("effect")
    with pytest.raises(Exception):
        ft.store().versions("effect")


def test_forget_accepts_the_spelling_the_caller_saw(kb):
    """`Effect.ts` is filed as `effect`; a delete that misses because of
    dressing would leave the caller believing it worked."""
    stored()
    ft.tool_forget_documentation("Effect.ts")
    with pytest.raises(Exception):
        ft.store().versions("effect")


def test_forget_says_what_is_there_when_the_version_is_wrong(kb):
    stored(version="v3")
    with pytest.raises(ft.ForgeError, match="v3"):
        ft.tool_forget_documentation("effect", "v9")


def test_forget_refuses_a_technology_it_does_not_have(kb):
    with pytest.raises(ft.ForgeError, match="Nothing stored"):
        ft.tool_forget_documentation("nosuchthing")


def test_forget_reports_what_it_destroyed(kb):
    stored()
    out = ft.tool_forget_documentation("effect")
    assert "2 pages" in out
    assert "cannot be undone" in out
