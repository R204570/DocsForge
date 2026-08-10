"""
Model providers for DocsForge.

Every provider exposes the same three DocsForge tools and yields the same
event stream, so the web app can switch between them per request — which is
the point: when one provider's daily quota is gone, the others still work.

    from providers import get, catalog
    for event in get("claude").stream(system=..., history=..., tools=..., run_tool=...):
        ...
"""

from __future__ import annotations

from .base import (
    MAX_CONTENT,
    MAX_HISTORY,
    MAX_ROUNDS,
    Provider,
    ProviderError,
    notice,
    text,
    tool_end,
    tool_start,
)
from . import chatgpt, claude, claudecode, gemini, groq

#: Registration order is the order shown in the UI.
PROVIDERS: list[Provider] = [
    claude.provider,
    claudecode.provider,
    groq.provider,
    chatgpt.provider,
    gemini.provider,
]

BY_NAME: dict[str, Provider] = {p.name: p for p in PROVIDERS}

DEFAULT = "groq"


def get(name: str | None) -> Provider:
    """Look up a provider, falling back to the first one that could run."""
    if name and name in BY_NAME:
        return BY_NAME[name]
    if name:
        raise ProviderError(f"Unknown provider {name!r}. Choose from: {', '.join(BY_NAME)}")
    return BY_NAME[default_name()]


def default_name() -> str:
    """DOCSFORGE_PROVIDER if set and usable, else the first ready provider."""
    import os

    wanted = os.environ.get("DOCSFORGE_PROVIDER", "").strip().lower()
    if wanted in BY_NAME:
        return wanted
    for provider in PROVIDERS:
        if provider.available():
            return provider.name
    return DEFAULT


def catalog() -> list[dict]:
    """Everything the UI needs to render the provider picker."""
    return [
        {
            "name": p.name,
            "label": p.label,
            "model": p.model(),
            "available": p.available(),
            "env_key": p.env_key,
            "docs": p.docs,
            "notes": p.notes,
        }
        for p in PROVIDERS
    ]


__all__ = [
    "PROVIDERS", "BY_NAME", "DEFAULT", "get", "default_name", "catalog",
    "Provider", "ProviderError", "MAX_ROUNDS", "MAX_HISTORY", "MAX_CONTENT",
    "text", "tool_start", "tool_end", "notice",
]
