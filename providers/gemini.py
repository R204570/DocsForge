"""
Google Gemini via the google-genai SDK.

Two shape differences from the OpenAI-style providers:

* Roles are `user` / `model` (not `assistant`), and the system prompt is a
  config field rather than a message.
* A tool call is a `functionCall` part; the answer is a `functionResponse`
  part in a *user* turn.

Automatic function calling is switched off deliberately — it only works with
Python callables, and DocsForge tools are declared as JSON Schema so all five
providers can share one definition.
"""

from __future__ import annotations

from typing import Any, Iterator

import forge_tools

from .base import MAX_ROUNDS, Provider, ProviderError, RunTool, notice, text, tool_end, tool_start


class GeminiProvider(Provider):
    name = "gemini"
    label = "Gemini"
    env_key = "GEMINI_API_KEY"
    default_model = "gemini-2.5-flash"
    docs = "https://aistudio.google.com/apikey"
    notes = "Large free tier. Good on long documents."

    max_tokens = 8192

    def _sdk(self):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise ProviderError("Gemini needs: pip install google-genai") from e
        return genai, types

    def stream(
        self,
        *,
        system: str,
        history: list[dict[str, str]],
        tools: list,
        run_tool: RunTool,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        genai, types = self._sdk()
        client = genai.Client(api_key=self.require_key())
        chosen = self.model(model)

        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in history
        ]

        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                # Raw JSON Schema, so the tool layer stays vendor-neutral.
                parameters_json_schema=t.schema,
            )
            for t in tools
        ]
        tool_config = [types.Tool(function_declarations=declarations)]

        for round_index in range(MAX_ROUNDS + 1):
            last = round_index == MAX_ROUNDS
            config = types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=self.max_tokens,
                tools=None if last else tool_config,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

            try:
                chunks = client.models.generate_content_stream(
                    model=chosen, contents=contents, config=config
                )
                calls: list[Any] = []
                parts: list[Any] = []
                for chunk in chunks:
                    for candidate in chunk.candidates or []:
                        content = getattr(candidate, "content", None)
                        for part in (getattr(content, "parts", None) or []):
                            parts.append(part)
                            if getattr(part, "text", None):
                                yield text(part.text)
                            if getattr(part, "function_call", None):
                                calls.append(part.function_call)
            except Exception as e:
                raise ProviderError(f"{type(e).__name__}: {e}") from e

            if not calls:
                return

            contents.append(types.Content(role="model", parts=parts))

            answers = []
            for call in calls:
                args = dict(call.args or {})
                yield tool_start(call.name, args)
                result = run_tool(call.name, args)
                yield tool_end(call.name, result, forge_tools.kind_of(result))
                answers.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": result}
                    )
                )
            contents.append(types.Content(role="user", parts=answers))

        yield notice(f"Stopped after {MAX_ROUNDS} rounds of tool calls.")


provider = GeminiProvider()
