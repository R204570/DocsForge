"""
The OpenAI-shaped tool-calling loop, shared by every vendor that speaks it.

Groq and OpenAI use the identical wire format, so the loop lives here once and
each provider module supplies only its client and defaults. Anything that is
genuinely different between them belongs in that module, not in here.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import forge_tools

from .base import MAX_ROUNDS, Provider, ProviderError, RunTool, notice, text, tool_end, tool_start


def schemas(tools: list) -> list[dict]:
    """DocsForge tools in the OpenAI `tools=[...]` format."""
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema},
        }
        for t in tools
    ]


def accumulate(delta, sink: dict[int, dict]) -> None:
    """Tool calls arrive split across streaming chunks; stitch them by index."""
    for call in getattr(delta, "tool_calls", None) or []:
        slot = sink.setdefault(call.index, {"id": "", "name": "", "args": ""})
        if getattr(call, "id", None):
            slot["id"] = call.id
        fn = getattr(call, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] = fn.name
            if getattr(fn, "arguments", None):
                slot["args"] += fn.arguments


class OpenAIShapedProvider(Provider):
    """Base for any vendor exposing the OpenAI chat-completions surface."""

    #: extra kwargs sent on every request (temperature, top_p, …)
    sampling: dict[str, Any] = {"temperature": 1, "top_p": 1}
    max_tokens: int = 2048

    def client(self):  # pragma: no cover - overridden
        raise NotImplementedError

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
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history]
        tool_defs = schemas(tools)

        for round_index in range(MAX_ROUNDS + 1):
            last = round_index == MAX_ROUNDS
            try:
                completion = client.chat.completions.create(
                    model=chosen,
                    messages=messages,
                    tools=tool_defs,
                    # On the final round drop tools so the model has to answer.
                    tool_choice="none" if last else "auto",
                    max_completion_tokens=self.max_tokens,
                    stream=True,
                    stop=None,
                    **self.sampling,
                )
            except Exception as e:
                raise ProviderError(f"{type(e).__name__}: {e}") from e

            pending: dict[int, dict] = {}
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield text(piece)
                accumulate(delta, pending)

            calls = [pending[i] for i in sorted(pending) if pending[i]["name"]]
            if not calls:
                return

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["args"] or "{}"},
                    }
                    for i, c in enumerate(calls)
                ],
            })

            for i, call in enumerate(calls):
                try:
                    args = json.loads(call["args"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments were not a JSON object")
                except (json.JSONDecodeError, ValueError) as e:
                    result, args = f"Error: bad arguments for {call['name']}: {e}", {}
                    yield tool_start(call["name"], {})
                else:
                    yield tool_start(call["name"], args)
                    result = run_tool(call["name"], args)

                yield tool_end(call["name"], result, forge_tools.kind_of(result))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{i}",
                    "name": call["name"],
                    "content": result,
                })

        yield notice(f"Stopped after {MAX_ROUNDS} rounds of tool calls.")
