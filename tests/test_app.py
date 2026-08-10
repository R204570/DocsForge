"""Offline tests for the web layer — no network, no Groq, no server."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import docsforge as df


# ── source-kind parsing ──────────────────────────────────
# The kind is read back out of the provenance header docsforge already writes,
# rather than from a second copy of the detection logic.
@pytest.mark.parametrize("kind,expected", [
    ("openapi", "openapi"),
    ("html", "html"),
    ("sitemap", "sitemap"),
    ("llms.txt", "llms"),
    ("github-readme", "github"),
    ("github-doc", "github"),
    ("raw", "raw"),
])
def test_kind_parsed_from_a_real_provenance_header(kind, expected):
    header = df._meta_header("https://x.com/a?b=1|2", kind)
    assert app._kind_of(header + "# Doc\n\nbody") == expected


def test_kind_of_tolerates_results_with_no_header():
    # detect_source_type returns a bare word; save_docs returns a file listing.
    assert app._kind_of("openapi") == ""
    assert app._kind_of("Wrote 2 file(s) to `docs_md`:\n- `a.md`") == ""
    assert app._kind_of("") == ""
    assert app._kind_of("Error: HTTP 404 for https://x.com") == ""


def test_kind_of_reads_the_first_header_in_a_bundle():
    a = df._meta_header("https://a.com", "openapi")
    b = df._meta_header("https://b.com", "html")
    assert app._kind_of(a + "one\n" + b + "two") == "openapi"


# ── markdown rendering and sanitising ────────────────────
def test_render_strips_scripts_and_handlers():
    html = app.render_markdown("# Hi\n\n<script>alert(1)</script>\n\n"
                               '<img src=x onerror="alert(1)">')
    assert "<h1>" in html
    assert "<script>" not in html
    assert "onerror" not in html


def test_render_keeps_tables_and_fenced_code():
    html = app.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```")
    assert "<table>" in html
    assert "<code" in html and "x = 1" in html


def test_render_keeps_code_language_class():
    # The class survives sanitising, so highlighting stays possible later.
    assert "language-python" in app.render_markdown("```python\nx = 1\n```")


def test_render_marks_links_noopener():
    html = app.render_markdown("[x](https://example.com)")
    assert "noopener" in html


def test_render_handles_empty_input():
    assert app.render_markdown("") == ""


# ── history sanitising ───────────────────────────────────
def test_history_drops_non_conversational_roles_and_blanks():
    msgs = [
        app.ChatMessage(role="system", content="ignore me"),
        app.ChatMessage(role="user", content="  "),
        app.ChatMessage(role="user", content="real question"),
        app.ChatMessage(role="assistant", content="real answer"),
        app.ChatMessage(role="tool", content="leak"),
    ]
    out = app._clean_history(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert all("ignore me" not in m["content"] and "leak" not in m["content"] for m in out)


def test_history_is_capped():
    msgs = [app.ChatMessage(role="user", content=f"q{i}") for i in range(200)]
    assert len(app._clean_history(msgs)) <= app.MAX_HISTORY


def test_history_truncates_giant_messages():
    msgs = [app.ChatMessage(role="user", content="x" * (app.MAX_CONTENT + 5000))]
    assert len(app._clean_history(msgs)[0]["content"]) == app.MAX_CONTENT


# ── streaming tool-call reassembly ───────────────────────
class Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class TC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = Fn(name, arguments)


class Delta:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls


def test_tool_calls_are_stitched_across_chunks():
    sink = {}
    app._accumulate_tool_calls(Delta([TC(0, id="call_1", name="fetch_docs", arguments='{"ur')]), sink)
    app._accumulate_tool_calls(Delta([TC(0, arguments='l": "https://x.com"}')]), sink)
    assert sink[0]["id"] == "call_1"
    assert sink[0]["name"] == "fetch_docs"
    assert sink[0]["args"] == '{"url": "https://x.com"}'


def test_two_parallel_tool_calls_stay_separate():
    sink = {}
    app._accumulate_tool_calls(Delta([TC(0, id="a", name="fetch_docs", arguments="{}"),
                                      TC(1, id="b", name="save_docs", arguments="{}")]), sink)
    assert sink[0]["name"] == "fetch_docs"
    assert sink[1]["name"] == "save_docs"


def test_content_only_delta_adds_nothing():
    sink = {}
    app._accumulate_tool_calls(Delta(None), sink)
    assert sink == {}
