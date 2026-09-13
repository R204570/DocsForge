"""
The OpenAI-shaped tool-calling loop, shared by every vendor that speaks it.

Groq and OpenAI use the identical wire format, so the loop lives here once and
each provider module supplies only its client and defaults. Anything that is
genuinely different between them belongs in that module, not in here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from docsforge.tools import forge_tools

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


#: A model that cannot drive tool calling properly writes the call out as
#: prose instead. Measured 2026-09-10 with `llama3.2` over Ollama, asked
#: questions whose answers were sitting in the knowledge base:
#:
#:     {"name": "create_agent", "parameters": {"namespace": "google-adk"}}
#:     {"name": "sendFinishSignal", "parameters": {"technology": "google-adk"}}
#:     {"name": "gin.NewEngine", "parameters": {"middleware": "[...]"}}
#:
#: Every one is invented — none is a DocsForge tool — and none was emitted as a
#: tool call, so the loop saw a plain text answer, returned it, and the model
#: scored 1 of 12 on documentation it was sitting on top of. The same questions
#: with the documentation handed over directly scored 8 of 12.
#:
#: The model is asking for a tool. Answering "that is not a tool" is a far
#: better turn than treating the request as the reply.
_FAKE_CALL = ("{", '"name"')


#: The other spelling, from the same measured run: the model names a real tool
#: and writes it as source rather than as a call.
#:
#:     search_knowledge_base("Effect library sequential effectful code")
#:     search_knowledge_base(name='const')
#:
#: The arguments are not reliably a tool's actual parameters -- `name=` there is
#: not one of `search_knowledge_base`'s -- so this never guesses them. Naming
#: the tool is enough to know the model wanted one, and asking it to call
#: properly is a better turn than printing its source as the answer.
_CALL_SYNTAX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)\s*$", re.S)


def _names_a_tool(buffered: str, known: set[str]) -> str:
    """The tool a model wrote out as source, or ""."""
    match = _CALL_SYNTAX.match((buffered or "").strip())
    return match.group(1) if match and match.group(1) in known else ""


def _might_be_a_call(seen: str, known: set[str]) -> bool:
    """Could this partial output still turn out to be a tool call as text?

    Asked once per round, on the first chunk with content in it, so that a
    plain answer is released immediately and only a brace or a tool's own name
    is ever held back.
    """
    if seen.startswith("{"):
        return True
    head = seen.split("(", 1)[0].strip()
    return any(name == head or name.startswith(head) for name in known)


def _looks_like_a_call(buffered: str) -> dict | None:
    """The tool call a model wrote out as text, or None."""
    body = (buffered or "").strip()
    # Only output that was a JSON object from its first character. Output that
    # merely contains one somewhere is prose, and prose is an answer — the
    # caller streams it the moment it does not begin with `{`, so it never
    # reaches here anyway. Keeping the two consistent matters: a detector
    # looser than the buffer would describe a case that cannot occur.
    if not body.startswith("{"):
        return None
    end = body.rfind("}")
    if end <= 0:
        return None
    start = 0
    try:
        parsed = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
    if not isinstance(name, str) or not name:
        return None
    args = (parsed.get("parameters") or parsed.get("arguments")
            or parsed.get("args") or {})
    return {"name": name, "args": args if isinstance(args, dict) else {}}


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
            known = {t.name for t in tools}
            #: Output that might turn out to be a tool call written as text is
            #: held rather than streamed. The decision is made once, on the
            #: first chunk that carries anything: output that could still
            #: become `{...}` or `some_tool(...)` keeps being held, and
            #: everything else is released and streams token by token from
            #: there on. One chunk of latency, and only when the model has
            #: started with a brace or a tool's name.
            held, streaming = "", False
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    if streaming:
                        yield text(piece)
                    else:
                        held += piece
                        seen = held.lstrip()
                        if seen and not _might_be_a_call(seen, known):
                            yield text(held)
                            held, streaming = "", True
                accumulate(delta, pending)

            calls = [pending[i] for i in sorted(pending) if pending[i]["name"]]

            if not calls and held:
                faked = None if last else _looks_like_a_call(held)
                if faked is None and not last:
                    # The other spelling: a real tool written as source. Its
                    # arguments are not trustworthy, so only the name is taken.
                    named = _names_a_tool(held, known)
                    if named:
                        faked = {"name": named, "args": None}
                if faked is not None:
                    messages.append({"role": "assistant", "content": held})
                    if faked["name"] in known and faked["args"] is not None:
                        # It asked for a real tool in the wrong envelope. Run it.
                        yield tool_start(faked["name"], faked["args"])
                        result = run_tool(faked["name"], faked["args"])
                        yield tool_end(faked["name"], result,
                                       forge_tools.kind_of(result))
                        messages.append({"role": "user", "content": (
                            f"Result of {faked['name']}:\n\n{result}\n\n"
                            f"Now answer the original question from this.")})
                    else:
                        messages.append({"role": "user", "content": (
                            f"{faked['name']!r} is not a tool. The tools you have "
                            f"are: {', '.join(sorted(known))}. Call one of them "
                            f"properly, or answer the question directly.")})
                    continue
                yield text(held)

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
