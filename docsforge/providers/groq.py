"""Groq — fast and free-tier friendly, with a hard daily token cap."""

from __future__ import annotations

from ._openai_shape import OpenAIShapedProvider
from .base import ProviderError


class GroqProvider(OpenAIShapedProvider):
    name = "groq"
    label = "Groq"
    env_key = "GROQ_API_KEY"
    default_model = "gpt-oss-120b"
    docs = "https://console.groq.com/keys"
    notes = "Fast and cheap. Free tier is capped per day (100k tokens)."

    def client(self):
        try:
            from groq import Groq
        except ImportError as e:
            raise ProviderError("Groq needs: pip install groq") from e
        return Groq(api_key=self.require_key())


provider = GroqProvider()
