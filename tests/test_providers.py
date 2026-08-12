"""Offline tests for the provider layer — no network, no API keys."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forge_tools
import providers
from providers._openai_shape import accumulate, schemas
from providers.base import Provider, ProviderError, tool_end


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

    from providers import claude as claude_mod

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
    assert allowed.split(",") == [
        "mcp__docsforge__detect_source_type",
        "mcp__docsforge__fetch_docs",
        "mcp__docsforge__save_docs",
    ]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "mcp_server.py" in argv[argv.index("--mcp-config") + 1]


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
    from providers._openai_shape import OpenAIShapedProvider

    assert isinstance(providers.get("ollama"), OpenAIShapedProvider)
