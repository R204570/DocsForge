"""A model that writes a tool call out as text is asking for a tool.

Measured 2026-09-10 with `llama3.2` over Ollama, asked twelve questions whose
answers were sitting in the knowledge base. It scored 1/12 with the tools in
front of it and 8/12 when the same documentation was handed over directly --
because it never called a tool. It wrote them out as prose instead, and
invented the names:

    {"name": "create_agent", "parameters": {"namespace": "google-adk"}}
    {"name": "sendFinishSignal", "parameters": {"technology": "google-adk"}}
    {"name": "gin.NewEngine", "parameters": {"middleware": "[...]"}}

The loop saw a plain text answer, returned it, and the run scored zero on a
corpus it was sitting on top of.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools as ft
from docsforge.providers import _openai_shape as shape


# ── recognising the shape ───────────────────────────────────────────────────

def test_a_tool_call_written_as_text_is_recognised():
    call = shape._looks_like_a_call(
        '{"name": "search_knowledge_base", "parameters": {"query": "LlmAgent"}}')
    assert call == {"name": "search_knowledge_base",
                    "args": {"query": "LlmAgent"}}


def test_the_other_spellings_of_arguments_are_read_too():
    for key in ("parameters", "arguments", "args"):
        call = shape._looks_like_a_call(
            '{"name": "read_knowledge_base", "%s": {"name": "effect"}}' % key)
        assert call["args"] == {"name": "effect"}, key


def test_an_ordinary_answer_is_never_mistaken_for_a_call():
    for answer in ("LlmAgent", "Effect.gen", "I DO NOT KNOW", "",
                   "The answer is {see docs}"):
        assert shape._looks_like_a_call(answer) is None, answer


def test_json_that_is_not_a_call_is_left_alone():
    assert shape._looks_like_a_call('{"foo": 1}') is None
    assert shape._looks_like_a_call('{"name": 3}') is None


def test_prose_containing_json_is_prose():
    """The detector and the buffer have to agree. Output that does not begin
    with `{` is streamed as it arrives and never reaches the detector, so a
    detector that accepted it would be describing a case that cannot occur."""
    assert shape._looks_like_a_call(
        'Here you go: {"name":"read_knowledge_base","arguments":{}}') is None


# ── the loop does something useful with it ──────────────────────────────────

class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None


class _Chunk:
    def __init__(self, content=None):
        self.choices = [type("C", (), {"delta": _Delta(content)})()]


class _Completions:
    """Replays a scripted round per call."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.sent = []

    def create(self, **kw):
        self.sent.append(kw)
        return [_Chunk(piece) for piece in self.rounds.pop(0)]


class _Client:
    def __init__(self, rounds):
        self.chat = type("Chat", (), {"completions": _Completions(rounds)})()


class _Fake(shape.OpenAIShapedProvider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, rounds):
        self._client = _Client(rounds)

    def client(self):
        return self._client


def _run(rounds, run_tool):
    provider = _Fake(rounds)
    tools = [t for t in ft.TOOLS if t.name == "search_knowledge_base"]
    return list(provider.stream(system="s", history=[{"role": "user", "content": "q"}],
                                tools=tools, run_tool=run_tool)), provider


def test_a_faked_call_to_a_real_tool_is_run():
    called = {}

    def run_tool(name, args):
        called["name"], called["args"] = name, args
        return "Effect.gen is the answer"

    events, _ = _run([
        ['{"name": "search_knowledge_base", ', '"parameters": {"query": "gen"}}'],
        ["Effect.gen"],
    ], run_tool)

    assert called["name"] == "search_knowledge_base"
    assert called["args"] == {"query": "gen"}
    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds
    answer = "".join(e["text"] for e in events if e["type"] == "text")
    assert answer == "Effect.gen"
    assert "{" not in answer, "the faked call is not shown as the reply"


def test_an_invented_tool_name_is_answered_with_the_real_ones():
    ran = []
    events, provider = _run([
        ['{"name": "create_agent", "parameters": {}}'],
        ["LlmAgent"],
    ], lambda n, a: ran.append(n) or "")

    assert ran == [], "an invented tool is never executed"
    correction = provider._client.chat.completions.sent[-1]["messages"][-1]
    assert "is not a tool" in correction["content"]
    assert "search_knowledge_base" in correction["content"]
    answer = "".join(e["text"] for e in events if e["type"] == "text")
    assert answer == "LlmAgent"


def test_an_ordinary_answer_still_streams_unchanged():
    events, _ = _run([["Llm", "Agent"]], lambda n, a: "")
    pieces = [e["text"] for e in events if e["type"] == "text"]
    assert pieces == ["Llm", "Agent"], "streamed token by token, not buffered"


def test_json_that_is_the_real_answer_is_still_returned():
    """A question whose answer genuinely is a JSON object must survive."""
    events, _ = _run([['{"foo": ', '1}']], lambda n, a: "")
    answer = "".join(e["text"] for e in events if e["type"] == "text")
    assert answer == '{"foo": 1}'


# ── the other spelling: a real tool written as source ───────────────────────

def test_a_tool_named_in_call_syntax_is_recognised():
    known = {"search_knowledge_base", "read_knowledge_base"}
    assert shape._names_a_tool(
        'search_knowledge_base("Effect sequential code")', known) == "search_knowledge_base"
    assert shape._names_a_tool(
        "search_knowledge_base(name='const')", known) == "search_knowledge_base"


def test_a_function_call_that_is_not_a_tool_is_left_alone():
    known = {"search_knowledge_base"}
    assert shape._names_a_tool("gin.Default()", known) == ""
    assert shape._names_a_tool("print('hello')", known) == ""
    assert shape._names_a_tool("LlmAgent", known) == ""


def test_arguments_written_as_source_are_never_guessed():
    """`search_knowledge_base(name='const')` names a parameter the tool does
    not have. Running it on a guess would be worse than asking again."""
    ran = []
    events, provider = _run([
        ["search_knowledge_base(name='const')"],
        ["comptime"],
    ], lambda n, a: ran.append((n, a)) or "")

    assert ran == [], "nothing is executed from guessed arguments"
    correction = provider._client.chat.completions.sent[-1]["messages"][-1]
    assert "search_knowledge_base" in correction["content"]
    answer = "".join(e["text"] for e in events if e["type"] == "text")
    assert answer == "comptime"


def test_output_is_held_only_while_it_could_still_be_a_call():
    known = {"search_knowledge_base", "read_knowledge_base"}
    assert shape._might_be_a_call('{"name"', known)
    assert shape._might_be_a_call("search_", known), "a prefix of a tool name"
    assert not shape._might_be_a_call("LlmAgent", known)
    assert not shape._might_be_a_call("The answer is", known)
    assert not shape._might_be_a_call("gin.Default", known)
