"""
The MCP surface is the product, so it is held to the library it is generated
from — no hand-written second copy allowed to drift.

This file exists because the two *did* drift: four tools lived in forge_tools
and were simply absent over MCP, and `max_pages` was declared `ge=1, le=200`
here while the harvester treats 0 as unlimited, so no MCP client could request
a full harvest.
"""

import os
import sys

import anyio
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forge_tools as ft
import mcp_server


def tools():
    return anyio.run(mcp_server.server.list_tools)


@pytest.fixture(scope="module")
def exposed():
    return {t.name: t for t in tools()}


# ── the surface matches its source ───────────────────────
def test_every_library_tool_is_exposed_over_mcp(exposed):
    assert set(exposed) == {t.name for t in ft.TOOLS}


def test_the_name_addressable_tools_are_present(exposed):
    # The whole point of the product: a model with a name and no URL.
    assert {"learn_technology", "find_docs",
            "scan_project", "search_knowledge_base"} <= set(exposed)


def test_descriptions_come_from_the_library(exposed):
    for tool in ft.TOOLS:
        assert exposed[tool.name].description == tool.description


def test_required_arguments_match_the_library(exposed):
    for tool in ft.TOOLS:
        want = set(tool.schema.get("required") or [])
        got = set(exposed[tool.name].input_schema.get("required") or [])
        assert got == want, tool.name


def test_every_argument_is_carried_across(exposed):
    for tool in ft.TOOLS:
        want = set((tool.schema.get("properties") or {}))
        got = set(exposed[tool.name].input_schema["properties"])
        assert got == want, tool.name


def test_every_argument_keeps_its_description(exposed):
    # Over MCP the description *is* the interface: no human reads it, and the
    # model's whole choice of tool and arguments is made from this text.
    for tool in ft.TOOLS:
        for key, spec in (tool.schema.get("properties") or {}).items():
            if not spec.get("description"):
                continue
            got = exposed[tool.name].input_schema["properties"][key]
            assert got.get("description") == spec["description"], f"{tool.name}.{key}"


# ── the specific drift that happened ─────────────────────
def test_an_unlimited_harvest_can_be_requested_over_mcp(exposed):
    spec = exposed["harvest_docs"].input_schema["properties"]["max_pages"]
    assert spec.get("minimum") == 0, "0 means unlimited; ge=1 made it unreachable"
    assert spec.get("default") == 0
    assert "maximum" not in spec, "a page ceiling here contradicts the harvester"


def test_learn_technology_asks_only_for_a_name(exposed):
    schema = exposed["learn_technology"].input_schema
    assert schema["required"] == ["name"]
    assert "url" not in schema["properties"], "needing a URL is the bug it fixes"


def test_enums_survive_the_translation(exposed):
    eco = exposed["learn_technology"].input_schema["properties"]["ecosystem"]
    choices = [c for branch in eco.get("anyOf", [eco]) for c in branch.get("enum", [])]
    assert set(choices) == {"npm", "pypi", "crates"}


# ── it actually dispatches ───────────────────────────────
def test_a_tool_call_reaches_the_library(monkeypatch):
    seen = {}

    def fake(query, technology=None, version=None, limit=20):
        seen.update(query=query, technology=technology, limit=limit)
        return "stub result"

    monkeypatch.setattr(ft.BY_NAME["search_knowledge_base"], "fn", fake)

    # Rebuild against the patched tool, as a fresh process would.
    from mcp.server import MCPServer
    server = MCPServer(name="t", version="0")
    mcp_server.register(server)

    result = anyio.run(
        lambda: server.call_tool("search_knowledge_base", {"query": "retry", "limit": 5}))
    assert "stub result" in str(result)
    assert seen == {"query": "retry", "technology": None, "limit": 5}


def test_unset_optionals_do_not_override_a_tools_own_defaults(monkeypatch):
    # The signature has to give every optional a default, but passing those
    # straight through would silently overwrite what the tool itself chose.
    seen = {}

    def fake(url, name=None, max_pages=0, js=False, scope="section", version=None):
        seen.update(scope=scope, max_pages=max_pages)
        return "ok"

    monkeypatch.setattr(ft.BY_NAME["harvest_docs"], "fn", fake)

    from mcp.server import MCPServer
    server = MCPServer(name="t", version="0")
    mcp_server.register(server)

    anyio.run(lambda: server.call_tool("harvest_docs", {"url": "https://x.dev/docs/"}))
    assert seen["scope"] == "section"
    assert seen["max_pages"] == 0


def test_the_server_tells_clients_they_do_not_need_a_url():
    # The instructions are read by the model before it picks anything, and they
    # used to say "go straight to fetch_docs".
    instructions = mcp_server.server.instructions or ""
    assert "learn_technology" in instructions
    assert "not need a documentation URL" in instructions
