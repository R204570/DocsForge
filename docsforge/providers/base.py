"""
Shared contract for every DocsForge model provider.

Each provider owns its own conversation loop, because the vendors spell tool
calling differently: OpenAI-shaped APIs use `tool_calls` plus `role: "tool"`
messages, Anthropic uses `tool_use` blocks answered by `tool_result` blocks,
and Gemini uses `functionCall` / `functionResponse` parts. Forcing one generic
loop over all three produces a lowest-common-denominator mess and breaks the
moment a vendor adds something.

What providers do share is the *event stream* they yield, so app.py can map any
of them onto the same SSE shape without knowing which one is running.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterator

# Tool round-trips before we stop offering tools and make the model answer.
# The happy path is three — learn_technology, read_knowledge_base, answer — and
# a smaller model routinely spends one round on something unhelpful before
# finding its footing. At four it then runs out mid-task and is forced to answer
# without ever having read the documentation, which is the one outcome this
# product exists to prevent. The headroom costs nothing when it is not needed.
MAX_ROUNDS = 6

# Conversation turns and per-message characters accepted from a client.
MAX_HISTORY = 40
MAX_CONTENT = 100_000


class ProviderError(RuntimeError):
    """Anything the user can act on: missing key, bad model, refused request."""


# ── the event stream every provider yields ───────────────────
def text(chunk: str) -> dict:
    return {"type": "text", "text": chunk}


def tool_start(name: str, args: dict) -> dict:
    return {"type": "tool_start", "name": name, "args": args}


def tool_end(name: str, result: str, kind: str = "") -> dict:
    ok = not result.startswith("Error:")
    return {
        "type": "tool_end",
        "name": name,
        "ok": ok,
        "chars": len(result),
        "kind": kind,
        "preview": result[:200],
        # The whole result, not just the preview. A provider that runs tools
        # outside this process -- the Claude Code CLI calls them over MCP --
        # leaves `run_tool` no chance to record what came back, and this is
        # then the only place the answer exists. It is bounded before it
        # reaches a browser, by tracing.clip(), not here.
        "result": result,
    }


def notice(message: str) -> dict:
    """Something the user should know that is not a failure — a degraded beta,
    a model substitution, a run that stopped at the round limit."""
    return {"type": "notice", "message": message}


RunTool = Callable[[str, dict], str]


class Provider(ABC):
    """One model backend."""

    name: str = ""            # stable id used in config, the API, and .env
    label: str = ""           # shown in the UI
    env_key: str | None = None
    default_model: str = ""
    docs: str = ""            # where to get a key
    notes: str = ""           # one line shown in the picker

    def api_key(self) -> str | None:
        return os.environ.get(self.env_key) if self.env_key else None

    def available(self) -> bool:
        """True when this provider could actually run a turn right now."""
        return bool(self.api_key()) if self.env_key else True

    def model(self, override: str | None = None) -> str:
        """Override > per-provider env var > built-in default."""
        env = os.environ.get(f"{self.name.upper()}_MODEL")
        return override or env or self.default_model

    def require_key(self) -> str:
        key = self.api_key()
        if not key:
            hint = f"\nGet one at {self.docs}" if self.docs else ""
            raise ProviderError(f"{self.env_key} is not set. Add it to .env and restart.{hint}")
        return key

    @abstractmethod
    def stream(
        self,
        *,
        system: str,
        history: list[dict[str, str]],
        tools: list,
        run_tool: RunTool,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run one turn, yielding events until the model stops calling tools."""
        raise NotImplementedError
