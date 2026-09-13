"""Offline tests for the provider layer — no network, no API keys."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import forge_tools
from docsforge import providers
from docsforge.providers._openai_shape import accumulate, schemas
from docsforge.providers.base import Provider, ProviderError, tool_end


# ── registry ─────────────────────────────────────────────
def test_all_providers_registered():
    assert set(providers.BY_NAME) == {
        "claude", "claudecode", "ollama", "groq", "chatgpt", "gemini",
    }


def test_every_provider_is_complete():
    for p in providers.PROVIDERS:
        assert p.name and p.label, p
        assert isinstance(p, Provider)
        # claudecode and ollama run locally, so they need no API key.
        if p.name not in ("claudecode", "ollama"):
            assert p.env_key, p.name
            assert p.default_model, p.name


def test_catalog_shape_matches_what_the_ui_reads():
    for entry in providers.catalog():
        assert set(entry) == {"name", "label", "model", "available", "env_key", "docs", "notes"}
        assert isinstance(entry["available"], bool)


def test_get_rejects_an_unknown_provider():
    with pytest.raises(ProviderError, match="Unknown provider"):
        providers.get("bard")


def test_get_returns_the_named_provider():
    assert providers.get("claude").name == "claude"


def test_default_honours_the_env_var(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_PROVIDER", "gemini")
    assert providers.default_name() == "gemini"


def test_default_ignores_a_bogus_env_var(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_PROVIDER", "nonsense")
    assert providers.default_name() in providers.BY_NAME


# ── keys and models ──────────────────────────────────────
def test_available_follows_the_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert providers.get("claude").available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert providers.get("claude").available() is True


def test_missing_key_names_the_variable_and_where_to_get_one(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError) as excinfo:
        providers.get("chatgpt").require_key()
    assert "OPENAI_API_KEY" in str(excinfo.value)
    assert "platform.openai.com" in str(excinfo.value)


def test_model_precedence_is_override_then_env_then_default(monkeypatch):
    claude = providers.get("claude")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert claude.model() == "claude-opus-5"
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    assert claude.model() == "claude-sonnet-5"
    assert claude.model("claude-haiku-4-5") == "claude-haiku-4-5"


def test_claude_does_not_send_sampling_parameters():
    """temperature/top_p/top_k were removed on Opus 5 and return a 400."""
    import inspect

    from docsforge.providers import claude as claude_mod

    source = inspect.getsource(claude_mod)
    for banned in ("temperature", "top_p", "top_k"):
        assert f"{banned}=" not in source, f"{banned} must not be sent to Claude"


def test_claudecode_needs_no_api_key():
    assert providers.get("claudecode").env_key is None


# ── OpenAI-shaped plumbing ───────────────────────────────
def test_schemas_match_the_shared_tool_definitions():
    out = schemas(forge_tools.TOOLS)
    assert {t["function"]["name"] for t in out} == set(forge_tools.BY_NAME)
    assert all(t["type"] == "function" for t in out)


class Fn:
    def __init__(self, name=None, arguments=None):
        self.name, self.arguments = name, arguments


class TC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index, self.id = index, id
        self.function = Fn(name, arguments)


class Delta:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls


def test_tool_calls_are_stitched_across_chunks():
    sink = {}
    accumulate(Delta([TC(0, id="call_1", name="fetch_docs", arguments='{"ur')]), sink)
    accumulate(Delta([TC(0, arguments='l": "https://x.com"}')]), sink)
    assert sink[0] == {"id": "call_1", "name": "fetch_docs", "args": '{"url": "https://x.com"}'}


def test_parallel_tool_calls_stay_separate():
    sink = {}
    accumulate(Delta([TC(0, id="a", name="fetch_docs", arguments="{}"),
                      TC(1, id="b", name="save_docs", arguments="{}")]), sink)
    assert sink[0]["name"] == "fetch_docs"
    assert sink[1]["name"] == "save_docs"


def test_content_only_delta_adds_nothing():
    sink = {}
    accumulate(Delta(None), sink)
    assert sink == {}


# ── event helpers ────────────────────────────────────────
def test_tool_end_reads_success_from_the_result():
    ok = tool_end("fetch_docs", "# Doc\n\nbody", "html")
    assert ok["ok"] is True and ok["kind"] == "html"
    bad = tool_end("fetch_docs", "Error: HTTP 404")
    assert bad["ok"] is False


def test_tool_end_preview_is_bounded():
    assert len(tool_end("fetch_docs", "x" * 5000)["preview"]) == 200


# ── claude code command construction ─────────────────────
def test_claudecode_command_locks_the_session_to_docsforge_tools(monkeypatch):
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: "/usr/bin/claude")
    argv = cc.command("hello", "SYSTEM", None)

    assert "--strict-mcp-config" in argv, "must not inherit the user's own MCP servers"
    allowed = argv[argv.index("--allowedTools") + 1]

    # Derived from the one tool list, not copied. The copy this replaces had
    # drifted to three of fourteen and this test asserted the drift was
    # correct — which is how a hardcoded list stays wrong.
    from docsforge.tools import forge_tools
    assert allowed.split(",") == [
        f"mcp__docsforge__{t.name}" for t in forge_tools.TOOLS
    ]
    assert "mcp__docsforge__harvest_docs" in allowed, \
        "the tool that does the actual work must be allowed"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "main.py" in argv[argv.index("--mcp-config") + 1]


def test_claudecode_errors_clearly_when_the_cli_is_absent(monkeypatch):
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: None)
    with pytest.raises(ProviderError, match="not on PATH"):
        cc.command("hi", "sys", None)


def test_claudecode_folds_prior_turns_into_one_prompt():
    cc = providers.get("claudecode")
    assert cc.transcript([{"role": "user", "content": "only"}]) == "only"

    folded = cc.transcript([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ])
    assert "first" in folded and "answer" in folded
    assert folded.rstrip().endswith("second")


# ── ollama: local daemon, no key ─────────────────────────
def _fake_ollama(monkeypatch, models, up=True):
    """Pin the daemon probe so these tests never touch the network."""
    o = providers.get("ollama")
    monkeypatch.setattr(o, "_refresh", lambda force=False: None)
    monkeypatch.setattr(o, "_models", list(models))
    monkeypatch.setattr(o, "_up", up)
    return o


def test_ollama_needs_no_api_key():
    assert providers.get("ollama").env_key is None


def test_ollama_host_follows_the_env_var(monkeypatch):
    o = providers.get("ollama")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert o.host() == "http://127.0.0.1:11434"
    assert o.base_url().endswith("/v1")
    monkeypatch.setenv("OLLAMA_HOST", "http://box.local:11434/")
    assert o.host() == "http://box.local:11434"  # trailing slash trimmed


def test_ollama_hides_models_that_cannot_chat(monkeypatch):
    o = _fake_ollama(monkeypatch, [
        "qwen3.5:9b", "nomic-embed-text:latest", "phi3:mini", "llava:7b", "llama3.1:8b",
    ])
    assert o.chat_models() == ["qwen3.5:9b", "llama3.1:8b"]


def test_ollama_picks_the_best_tool_capable_model(monkeypatch):
    o = _fake_ollama(monkeypatch, ["llama3.1:8b", "qwen3.5:9b", "phi3:mini"])
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert o.model() == "qwen3.5:9b"          # qwen3.5 outranks llama3.1
    assert o.model("llama3.2:latest") == "llama3.2:latest"   # explicit wins
    monkeypatch.setenv("OLLAMA_MODEL", "mistral:7b")
    assert o.model() == "mistral:7b"          # env beats auto-detection


def test_ollama_falls_back_rather_than_naming_something_uninstalled(monkeypatch):
    o = _fake_ollama(monkeypatch, ["some-exotic-model:latest"])
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert o.model() == "some-exotic-model:latest"


def test_ollama_is_unavailable_when_the_daemon_is_down(monkeypatch):
    assert _fake_ollama(monkeypatch, [], up=False).available() is False


def test_ollama_is_unavailable_with_only_embedding_models(monkeypatch):
    # Daemon is up, but nothing on it can hold a conversation.
    assert _fake_ollama(monkeypatch, ["nomic-embed-text:latest"], up=True).available() is False


def test_ollama_says_how_to_start_the_daemon(monkeypatch):
    o = _fake_ollama(monkeypatch, [], up=False)
    monkeypatch.setattr(o, "_refresh", lambda force=False: None)
    with pytest.raises(ProviderError, match="ollama serve"):
        o.client()


def test_ollama_says_what_to_pull_when_empty(monkeypatch):
    o = _fake_ollama(monkeypatch, [], up=True)
    with pytest.raises(ProviderError, match="ollama pull"):
        o.client()


def test_ollama_reuses_the_shared_openai_loop():
    from docsforge.providers._openai_shape import OpenAIShapedProvider

    assert isinstance(providers.get("ollama"), OpenAIShapedProvider)


# ── running out of turns is not failing ──────────────────
class _FakeProc:
    """Just enough of Popen for ClaudeCodeProvider.stream to run offline."""

    def __init__(self, lines, code=1, stderr=""):
        self.stdout = iter(lines)
        self.stderr = type("E", (), {"read": lambda _self: stderr})()
        self._code = code
        self.killed = False

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return self._code

    def kill(self):
        self.killed = True


def _run(monkeypatch, lines, code=1):
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _FakeProc(lines, code))
    return list(cc.stream(system="s", history=[{"role": "user", "content": "go"}],
                          tools=[], run_tool=lambda *a, **kw: ""))


#: A harvest that worked, followed by the CLI stopping at its turn cap. This is
#: the `google-adk` run reduced to three lines: 1,799 pages were stored and the
#: user was told "that did not go through".
_HARVEST_THEN_CAP = [
    '{"type":"assistant","message":{"content":[{"type":"tool_use",'
    '"id":"t1","name":"mcp__docsforge__harvest_docs","input":{"url":"https://x.dev"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result",'
    '"tool_use_id":"t1","content":"Harvested google-adk - 1799 pages."}]}}',
    '{"type":"result","is_error":true,"subtype":"error_max_turns"}',
]


def test_running_out_of_turns_does_not_discard_the_work(monkeypatch):
    events = _run(monkeypatch, _HARVEST_THEN_CAP)

    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds and "tool_end" in kinds,         "the harvest that actually happened must still be reported"
    done = next(e for e in events if e["type"] == "tool_end")
    assert done["name"] == "harvest_docs"
    assert done["ok"] is True


def test_running_out_of_turns_says_so_rather_than_failing(monkeypatch):
    events = _run(monkeypatch, _HARVEST_THEN_CAP)

    notices = [e["message"] for e in events if e["type"] == "notice"]
    assert notices, "the user is owed an account of why the reply stopped"
    assert "stored" in notices[0].lower()


def test_a_capped_turn_survives_a_non_zero_exit(monkeypatch):
    # The CLI exits non-zero when it stops at the cap. Raising on that would
    # undo the whole point of tolerating the result event.
    events = _run(monkeypatch, _HARVEST_THEN_CAP, code=1)
    assert any(e["type"] == "notice" for e in events)


def test_a_real_failure_still_raises(monkeypatch):
    # Tolerating the turn cap must not turn every failure into a shrug.
    lines = ['{"type":"result","is_error":true,"subtype":"error_during_execution",'
             '"result":"something broke"}']
    with pytest.raises(ProviderError, match="something broke"):
        _run(monkeypatch, lines)


def test_the_turn_budget_leaves_room_to_harvest():
    # Nine calls found the entry point on a real run and left nothing to reply
    # with. A cap set for chatting is not a cap for harvesting.
    assert providers.get("claudecode").max_turns >= 20


def test_claudecode_with_no_tools_attaches_no_mcp_server(monkeypatch):
    """The benchmark's closed-book phase measures the wall the product exists
    to remove. A "no documentation" run that could still reach for
    `search_knowledge_base` -- or WebSearch -- is not measuring anything."""
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: "/usr/bin/claude")
    argv = cc.command("hello", "SYSTEM", None, tools=[])

    assert "--mcp-config" not in argv
    assert "--allowedTools" not in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "", "built-ins off too"


def test_claudecode_with_a_subset_allows_only_that_subset(monkeypatch):
    from docsforge.tools import forge_tools
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: "/usr/bin/claude")
    subset = [t for t in forge_tools.TOOLS if t.name in ("search_knowledge_base",
                                                          "read_knowledge_base")]
    argv = cc.command("hello", "SYSTEM", None, tools=subset)

    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert sorted(allowed) == ["mcp__docsforge__read_knowledge_base",
                               "mcp__docsforge__search_knowledge_base"]
    assert "mcp__docsforge__learn_technology" not in allowed
    assert "main.py" in argv[argv.index("--mcp-config") + 1]


def test_claudecode_built_in_tools_are_always_off(monkeypatch):
    """A DocsForge turn sees DocsForge tools and nothing else -- the docstring
    has claimed that since `--strict-mcp-config`; this is the half of the
    claim the CLI's own Bash, Read, WebFetch and WebSearch were not covered by."""
    cc = providers.get("claudecode")
    monkeypatch.setattr(cc, "binary", lambda: "/usr/bin/claude")
    for tools in (None, []):
        argv = cc.command("hello", "SYSTEM", None, tools=tools)
        assert argv[argv.index("--tools") + 1] == ""
