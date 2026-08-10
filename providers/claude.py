"""
Anthropic Claude via the Messages API.

Three things here differ from the OpenAI-shaped providers and are easy to get
wrong by copying them:

* **No sampling parameters.** `temperature`, `top_p`, and `top_k` were removed
  on Claude Opus 5 and return a 400. Steer with the prompt instead.
* **Thinking is on by default** on Opus 5, and `max_tokens` caps thinking plus
  response text together — hence the generous budget below.
* **Tool results are content blocks**, not a `role: "tool"` message: the whole
  assistant turn is echoed back, then every result returns in one user turn.

Refusal fallbacks are on by default: Opus 5's safety classifiers can decline a
request (HTTP 200 with `stop_reason: "refusal"`), and `fallbacks="default"`
lets the API re-run it on a suitable model server-side. Set
ANTHROPIC_FALLBACKS=off to disable.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import forge_tools

from .base import MAX_ROUNDS, Provider, ProviderError, RunTool, notice, text, tool_end, tool_start

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider(Provider):
    name = "claude"
    label = "Claude"
    env_key = "ANTHROPIC_API_KEY"
    default_model = "claude-opus-5"
    docs = "https://console.anthropic.com/settings/keys"
    notes = "Strongest on long documents and tool use. $5/$25 per Mtok."

    # Thinking counts against this, so leave real headroom. Streaming keeps a
    # budget this large from tripping the SDK's HTTP timeout.
    max_tokens = 16000

    def __init__(self) -> None:
        self._fallbacks = os.environ.get("ANTHROPIC_FALLBACKS", "default").lower() not in ("off", "0", "false")

    def client(self):
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError("Claude needs: pip install anthropic") from e
        return anthropic.Anthropic(api_key=self.require_key())

    def _open(self, client, **kwargs):
        """Stream with refusal fallbacks, degrading once if the beta is not
        enabled for this account rather than failing the whole turn."""
        if self._fallbacks:
            try:
                return client.beta.messages.stream(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                ), True
            except Exception:
                # Only stop trying after a real rejection, not a transient error.
                self._fallbacks = False
        return client.messages.stream(**kwargs), False

    def stream(
        self,
        *,
        system: str,
        history: list[dict[str, str]],
        tools: list,
        run_tool: RunTool,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        client = self.client()
        chosen = self.model(model)
        messages: list[dict[str, Any]] = [dict(m) for m in history]
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.schema}
            for t in tools
        ]

        told_about_degrade = False

        for round_index in range(MAX_ROUNDS + 1):
            last = round_index == MAX_ROUNDS
            kwargs: dict[str, Any] = {
                "model": chosen,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": messages,
            }
            if not last:
                kwargs["tools"] = tool_defs

            wanted_fallbacks = self._fallbacks
            try:
                manager, used_fallbacks = self._open(client, **kwargs)
                with manager as stream:
                    for piece in stream.text_stream:
                        yield text(piece)
                    final = stream.get_final_message()
            except Exception as e:
                raise ProviderError(f"{type(e).__name__}: {e}") from e

            if wanted_fallbacks and not used_fallbacks and not told_about_degrade:
                told_about_degrade = True
                yield notice("Refusal fallbacks unavailable on this account; continuing without them.")

            if final.stop_reason == "refusal":
                detail = getattr(final, "stop_details", None)
                category = getattr(detail, "category", None) or "unspecified"
                raise ProviderError(
                    f"Claude declined this request (category: {category}). "
                    "Rephrasing, or switching provider, may help."
                )

            messages.append({"role": "assistant", "content": final.content})

            # A server-side tool hit its iteration cap; re-send to resume.
            if final.stop_reason == "pause_turn":
                continue

            uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                return

            results = []
            for block in uses:
                args = block.input if isinstance(block.input, dict) else {}
                yield tool_start(block.name, args)
                result = run_tool(block.name, args)
                yield tool_end(block.name, result, forge_tools.kind_of(result))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": result.startswith("Error:"),
                })

            # Every result goes back in ONE user turn — splitting them teaches
            # the model to stop making parallel calls.
            messages.append({"role": "user", "content": results})

        yield notice(f"Stopped after {MAX_ROUNDS} rounds of tool calls.")


provider = ClaudeProvider()
