"""
Ollama — models running locally on this machine.

Ollama serves an OpenAI-compatible endpoint, so the shared loop in
`_openai_shape.py` covers it unchanged: streaming tool-call deltas arrive with
an index, and `tool_choice` is honoured.

What is different is everything around the request:

* **No key and no quota.** The bill is your own electricity, so this provider
  keeps working when every hosted one is rate limited.
* **It can be switched off.** Ollama is a local daemon, so `available()` has to
  probe the port rather than look for an API key — a stale "ready" here would
  mean a failed turn instead of a greyed-out entry.
* **The model list is whatever you pulled.** Rather than hardcode a default
  that may not be installed, pick the best tool-capable model actually present.

Not every local model can call tools. Ones that cannot will happily answer from
memory and never fetch anything, which looks like DocsForge being broken, so the
preference list below only names families known to support tool calling.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ._openai_shape import OpenAIShapedProvider
from .base import ProviderError

DEFAULT_HOST = "http://127.0.0.1:11434"

#: Best first. Matched as a prefix against installed model names, so
#: "qwen3.5" matches "qwen3.5:9b".
TOOL_CAPABLE = (
    "qwen3.5", "qwen3", "qwen2.5",
    "llama3.3", "llama3.2", "llama3.1",
    "mistral-nemo", "mistral", "firefunction", "command-r", "hermes3",
)

#: Cannot call tools, or are not chat models at all.
NEVER = ("embed", "phi3", "phi-3", "llava", "moondream", "codegemma")

_PROBE_TTL = 5.0  # seconds; /api/config must not pay a network probe per hit


class OllamaProvider(OpenAIShapedProvider):
    name = "ollama"
    label = "Ollama"
    env_key = None  # local daemon, no key
    default_model = "llama3.1:8b"
    docs = "https://ollama.com/download"
    notes = "Runs on your machine. No key, no quota, works offline."

    # Local models are slower per token, so keep the cap sane.
    max_tokens = 2048
    sampling = {"temperature": 0.6, "top_p": 0.9}

    def __init__(self) -> None:
        self._checked_at = 0.0
        self._up = False
        self._models: list[str] = []

    # -- host ---------------------------------------------------
    def host(self) -> str:
        return (os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")

    def base_url(self) -> str:
        return f"{self.host()}/v1"

    # -- discovery ----------------------------------------------
    def _refresh(self, force: bool = False) -> None:
        """Ask the daemon what it has, at most every _PROBE_TTL seconds."""
        now = time.monotonic()
        if not force and now - self._checked_at < _PROBE_TTL:
            return
        self._checked_at = now
        try:
            with urllib.request.urlopen(f"{self.host()}/api/tags", timeout=2) as response:
                payload = json.load(response)
            self._models = [m.get("name", "") for m in payload.get("models", []) if m.get("name")]
            self._up = True
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            self._models = []
            self._up = False

    def installed(self) -> list[str]:
        self._refresh()
        return list(self._models)

    def chat_models(self) -> list[str]:
        """Installed models that can actually hold a tool-calling conversation."""
        return [m for m in self.installed() if not any(bad in m.lower() for bad in NEVER)]

    def available(self) -> bool:
        self._refresh()
        return self._up and bool(self.chat_models())

    def model(self, override: str | None = None) -> str:
        """Override > OLLAMA_MODEL > best installed tool-capable model."""
        if override:
            return override
        chosen = os.environ.get("OLLAMA_MODEL")
        if chosen:
            return chosen

        installed = self.chat_models()
        for family in TOOL_CAPABLE:
            for name in installed:
                if name.lower().startswith(family):
                    return name
        # Nothing recognised: fall back to whatever is there rather than to a
        # name that is definitely not installed.
        return installed[0] if installed else self.default_model

    # -- client -------------------------------------------------
    def client(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ProviderError("Ollama needs: pip install openai") from e

        self._refresh(force=True)
        if not self._up:
            raise ProviderError(
                f"No Ollama server at {self.host()}. Start it with `ollama serve`, "
                "or set OLLAMA_HOST if it runs elsewhere."
            )
        if not self.chat_models():
            raise ProviderError(
                "Ollama is running but has no chat model pulled. "
                "Try: ollama pull qwen3.5:9b"
            )

        chosen = self.model()
        if chosen not in self.installed():
            raise ProviderError(
                f"Ollama has no model named {chosen!r}. Installed: "
                f"{', '.join(self.installed()) or '(none)'}. Pull it with `ollama pull {chosen}`."
            )

        # Ollama ignores the key but the OpenAI client insists on one.
        return OpenAI(base_url=self.base_url(), api_key="ollama", timeout=600.0)


provider = OllamaProvider()
