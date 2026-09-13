"""A tool result should read as what the tool wrote.

FastMCP has no object schema for a tool returning a bare string, so it wraps
the value as `{"result": "..."}`. That JSON reached the execution trace intact,
and the panel showed the user

    {"result":"1 harvest still running:\\n- **langchain** - harvesting 144/561 ...

one unbroken line of escaped newlines instead of the report `list_knowledge_base`
had actually produced.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.providers.claudecode import _unwrap_mcp_result


def test_the_envelope_is_removed_and_the_newlines_come_back():
    report = ("1 harvest still running:\n"
              "- **langchain** - harvesting 144/561 pages, 49s elapsed\n\n"
              "23 technologies stored in postgres")
    wrapped = json.dumps({"result": report})

    assert "\\n" in wrapped, "the wrapper is what escapes the newlines"
    assert _unwrap_mcp_result(wrapped) == report
    assert "\n" in _unwrap_mcp_result(wrapped)


def test_a_tool_that_genuinely_returns_json_keeps_it():
    """The panel's job is to show what the tool returned, not to improve it."""
    real = json.dumps({"pages": 561, "complete": False})
    assert _unwrap_mcp_result(real) == real


def test_an_object_with_more_than_result_is_left_alone():
    payload = json.dumps({"result": "ok", "elapsed": 3})
    assert _unwrap_mcp_result(payload) == payload


def test_a_result_that_is_not_a_string_is_left_alone():
    payload = json.dumps({"result": {"pages": 3}})
    assert _unwrap_mcp_result(payload) == payload


def test_ordinary_markdown_passes_through_untouched():
    body = "# Harvested\n\n- one\n- two\n"
    assert _unwrap_mcp_result(body) == body


def test_something_that_only_looks_like_json_is_not_mangled():
    """A clipped result is the common case: 20,000 characters of JSON cut
    mid-string parses as nothing, and must survive rather than vanish."""
    clipped = '{"result":"1 harvest still running:\\n- **langcha'
    assert _unwrap_mcp_result(clipped) == clipped


def test_an_empty_body_is_safe():
    assert _unwrap_mcp_result("") == ""
    assert _unwrap_mcp_result("   ") == "   "
