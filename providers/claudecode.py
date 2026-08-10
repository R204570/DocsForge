"""
Claude Code — the local `claude` CLI in headless mode, with DocsForge's own
MCP server attached.

This provider is the odd one out, deliberately:

* **No API key.** It runs against whatever the user is already logged into
  Claude Code with, so it keeps working when every API key is rate limited.
* **Tools run out of process.** The CLI launches `mcp_server.py` itself and
  calls the tools over MCP, so `run_tool` is never used here — this is the
  path that proves the MCP server works for real clients.

`--strict-mcp-config` keeps the user's own MCP servers out of the session, so
a DocsForge turn only ever sees DocsForge tools.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Iterator

from .base import Provider, ProviderError, RunTool, notice, text, tool_end, tool_start

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_SERVER = os.path.join(HERE, "mcp_server.py")

TOOL_NAMES = ["detect_source_type", "fetch_docs", "save_docs"]
MCP_PREFIX = "mcp__docsforge__"


class ClaudeCodeProvider(Provider):
    name = "claudecode"
    label = "Claude Code"
    env_key = None  # uses the local CLI login, not a key
    default_model = ""  # empty = whatever the CLI is configured for
    docs = "https://claude.com/claude-code"
    notes = "Uses your local Claude Code login — no API key, no per-token bill."

    max_turns = 12
    timeout = 600

    def binary(self) -> str | None:
        return shutil.which("claude")

    def available(self) -> bool:
        return self.binary() is not None and os.path.exists(MCP_SERVER)

    def mcp_config(self) -> str:
        return json.dumps({
            "mcpServers": {
                "docsforge": {"command": sys.executable, "args": [MCP_SERVER]}
            }
        })

    def command(self, prompt: str, system: str, model: str | None) -> list[str]:
        binary = self.binary()
        if binary is None:
            raise ProviderError(
                "The `claude` CLI is not on PATH. Install Claude Code, or pick a different provider."
            )
        argv = [
            binary, "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--mcp-config", self.mcp_config(),
            "--strict-mcp-config",
            "--allowedTools", ",".join(f"{MCP_PREFIX}{n}" for n in TOOL_NAMES),
            "--append-system-prompt", system,
            "--max-turns", str(self.max_turns),
        ]
        chosen = self.model(model)
        if chosen:
            argv += ["--model", chosen]
        return argv

    @staticmethod
    def transcript(history: list[dict[str, str]]) -> str:
        """The CLI takes a single prompt, so prior turns are folded into it."""
        if len(history) == 1:
            return history[0]["content"]
        lines = []
        for turn in history[:-1]:
            who = "User" if turn["role"] == "user" else "You previously answered"
            lines.append(f"{who}: {turn['content']}")
        lines.append(f"\nUser: {history[-1]['content']}")
        return "\n\n".join(lines)

    def stream(
        self,
        *,
        system: str,
        history: list[dict[str, str]],
        tools: list,
        run_tool: RunTool,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        # `tools` and `run_tool` are unused on purpose: the CLI calls the same
        # tools over MCP, in its own process.
        argv = self.command(self.transcript(history), system, model)

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=HERE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as e:
            raise ProviderError(f"Could not start the claude CLI: {e}") from e

        pending: dict[str, str] = {}
        produced = False
        tail = ""  # last text emitted, so separate messages don't run together

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = event.get("type")

                if kind == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            # The CLI emits one assistant message per step, so a
                            # preamble and the answer arrive as separate blocks.
                            if tail and not tail.endswith("\n"):
                                yield text("\n\n")
                            produced = True
                            tail = block["text"]
                            yield text(tail)
                        elif block.get("type") == "tool_use":
                            raw = str(block.get("name", ""))
                            # The CLI has its own internal tools (ToolSearch and
                            # friends). Only DocsForge's own calls are sources.
                            if not raw.startswith(MCP_PREFIX):
                                continue
                            name = raw[len(MCP_PREFIX):]
                            pending[block.get("id", "")] = name
                            args = block.get("input") or {}
                            yield tool_start(name, args if isinstance(args, dict) else {})

                elif kind == "user":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") != "tool_result":
                            continue
                        name = pending.pop(block.get("tool_use_id", ""), "")
                        if not name:
                            continue  # a CLI-internal tool we did not report
                        body = block.get("content")
                        if isinstance(body, list):
                            body = "".join(
                                b.get("text", "") for b in body if isinstance(b, dict)
                            )
                        body = body if isinstance(body, str) else str(body)
                        if block.get("is_error"):
                            body = f"Error: {body}"
                        yield tool_end(name, body, _kind_of(body))

                elif kind == "result":
                    if event.get("is_error"):
                        raise ProviderError(
                            f"Claude Code failed: {event.get('result') or event.get('subtype')}"
                        )
                    # The CLI repeats the final answer here; only use it when
                    # nothing was streamed (e.g. a single-shot reply).
                    if not produced and event.get("result"):
                        yield text(str(event["result"]))
                    cost = event.get("total_cost_usd")
                    if cost:
                        yield notice(f"Claude Code turn cost ${cost:.4f} of your plan usage.")

            code = proc.wait(timeout=self.timeout)
        except ProviderError:
            proc.kill()
            raise
        finally:
            if proc.poll() is None:
                proc.kill()

        if code != 0:
            detail = (proc.stderr.read() or "").strip()[:400]
            raise ProviderError(f"claude CLI exited {code}. {detail}")


def _kind_of(result: str) -> str:
    import forge_tools

    return forge_tools.kind_of(result)


provider = ClaudeCodeProvider()
