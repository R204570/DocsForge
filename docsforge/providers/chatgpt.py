"""OpenAI — same wire format as Groq, different client and defaults."""

from __future__ import annotations

import os

from ._openai_shape import OpenAIShapedProvider
from .base import ProviderError


class OpenAIProvider(OpenAIShapedProvider):
    name = "chatgpt"
    label = "ChatGPT"
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-4.1"
    docs = "https://platform.openai.com/api-keys"
    notes = "Solid tool calling. Billed per token, no free tier."
    max_tokens = 4096

    def client(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ProviderError("OpenAI needs: pip install openai") from e
        # OPENAI_BASE_URL also covers Azure gateways and OpenAI-compatible hosts.
        return OpenAI(api_key=self.require_key(), base_url=os.environ.get("OPENAI_BASE_URL") or None)


provider = OpenAIProvider()
