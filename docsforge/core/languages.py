"""
Which programming language a request is for, and which a page is written for.

A technology is often documented once per language it supports: LangGraph
publishes `docs.langchain.com/oss/python/langgraph/` and
`docs.langchain.com/oss/javascript/langgraph/`, Playwright publishes
`playwright.dev/docs/` for Node and `playwright.dev/python/docs/` beside it.
"langgraph for node" names the second, and a harvest of the first is the
wrong documentation under exactly the right name -- the same failure a wrong
release is, one axis over.

This module is data and arithmetic: the aliases a caller may use, the path
segments and host labels a site files a language under, the registry that
publishes its packages, and how to tell from a page's own code which language
it is written for. It fetches nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    name: str                       # canonical: "javascript"
    aliases: tuple[str, ...]        # what a caller may say
    segments: tuple[str, ...]       # how a site spells it in a path, best first
    ecosystem: str = ""             # the registry its packages live in, if one is wired up
    fences: tuple[str, ...] = ()    # code-block languages that are this language
    marks: tuple[str, ...] = ()     # text only this language's docs contain


LANGUAGES: tuple[Language, ...] = (
    Language("javascript",
             ("javascript", "js", "typescript", "ts", "node", "nodejs", "node.js",
              "deno", "bun", "ecmascript", "jsx", "tsx"),
             ("javascript", "js", "typescript", "ts", "node", "nodejs"),
             "npm",
             ("js", "javascript", "ts", "typescript", "jsx", "tsx", "mjs", "cjs"),
             ("npm install", "npm i ", "yarn add", "pnpm add", "bun add", "npx ",
              "require(", "import {", "} from '", '} from "', "console.log(")),
    Language("python", ("python", "py", "python3", "cpython"),
             ("python", "py"), "pypi",
             ("python", "py", "python3", "pycon"),
             ("pip install", "poetry add", "uv add", "conda install", "__init__",
              "elif ", "from typing import", "async def ", "import asyncio")),
    Language("go", ("go", "golang"), ("go", "golang"), "",
             ("go", "golang"),
             ("go get ", "go install ", "go mod ", "package main", ":= ", "fmt.",
              "func (")),
    Language("rust", ("rust", "rs"), ("rust", "rs"), "crates",
             ("rust", "rs"),
             ("cargo add", "cargo install", "fn main", "let mut ", "use std::",
              "impl<", "#[derive")),
    Language("java", ("java", "jvm"), ("java",), "",
             ("java",),
             ("<dependency>", "public static void", "import java.", "System.out.",
              "@Override")),
    Language("kotlin", ("kotlin", "kt"), ("kotlin", "kt"), "",
             ("kotlin", "kt", "kts"),
             ("fun main", "implementation(\"", "suspend fun", "data class ")),
    Language("csharp", ("csharp", "c#", "cs", "dotnet", ".net", "net"),
             ("dotnet", "csharp", "net", "cs"), "",
             ("csharp", "cs", "c#", "fsharp"),
             ("dotnet add package", "install-package", "using System",
              "Console.WriteLine", "public async Task")),
    Language("ruby", ("ruby", "rb", "rails"), ("ruby", "rb"), "",
             ("ruby", "rb"),
             ("gem install", "bundle add", "require '", "puts ", "do |")),
    Language("php", ("php", "laravel"), ("php",), "",
             ("php",),
             ("composer require", "<?php", "$this->")),
    Language("swift", ("swift", "ios", "swiftui"), ("swift", "ios"), "",
             ("swift",),
             (".package(url:", "import SwiftUI", "import Foundation", "guard let ",
              "@State")),
    Language("dart", ("dart", "flutter"), ("dart", "flutter"), "",
             ("dart",),
             ("flutter pub add", "dart pub add", "import 'package:", "Widget build(")),
    Language("elixir", ("elixir", "ex", "erlang"), ("elixir",), "",
             ("elixir", "ex", "exs"),
             ("mix deps", "defmodule ", "|> ")),
    Language("cpp", ("cpp", "c++", "cplusplus"), ("cpp", "cplusplus"), "",
             ("cpp", "c++", "cc", "cxx", "hpp"),
             ("#include <", "std::", "int main(")),
)

#: Where a language, or a runtime, keeps its own manual. A language is not a
#: package: asked for "go", the resolver held `go.dev` back as ownership-only
#: -- the squatter rule, `Issues.md` R10 -- and let the one registry that knows
#: the word answer instead, and `docs.rs/go` is somebody's Rust crate
#: (2026-09-24). No registry is the authority on a language, so the
#: language's own documentation is looked up here first. Each entry still has
#: to pass the identity gate before it is used.
OFFICIAL_DOCS: dict[str, str] = {
    "go": "https://go.dev/doc/",
    "golang": "https://go.dev/doc/",
    "python": "https://docs.python.org/3/",
    "python3": "https://docs.python.org/3/",
    "rust": "https://doc.rust-lang.org/book/",
    "rust-lang": "https://doc.rust-lang.org/book/",
    "javascript": "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    "js": "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    "typescript": "https://www.typescriptlang.org/docs/",
    "ts": "https://www.typescriptlang.org/docs/",
    "node": "https://nodejs.org/docs/latest/api/",
    "nodejs": "https://nodejs.org/docs/latest/api/",
    "node-js": "https://nodejs.org/docs/latest/api/",
    "java": "https://docs.oracle.com/en/java/javase/21/",
    "kotlin": "https://kotlinlang.org/docs/home.html",
    "csharp": "https://learn.microsoft.com/en-us/dotnet/csharp/",
    "c-sharp": "https://learn.microsoft.com/en-us/dotnet/csharp/",
    "dotnet": "https://learn.microsoft.com/en-us/dotnet/",
    "ruby": "https://docs.ruby-lang.org/en/master/",
    "php": "https://www.php.net/manual/en/",
    "swift": "https://docs.swift.org/swift-book/documentation/the-swift-programming-language/",
    "dart": "https://dart.dev/docs",
    "elixir": "https://hexdocs.pm/elixir/",
    "cpp": "https://en.cppreference.com/w/cpp",
    "c": "https://en.cppreference.com/w/c",
    "html": "https://developer.mozilla.org/en-US/docs/Web/HTML",
    "css": "https://developer.mozilla.org/en-US/docs/Web/CSS",
}


def official_docs(name: str) -> str:
    """The manual a language or runtime publishes for itself, or ""."""
    key = re.sub(r"[^a-z0-9#+]+", "-", (name or "").strip().lower()).strip("-")
    key = {"c#": "csharp", "c++": "cpp", ".net": "dotnet", "net": "dotnet"}.get(key, key)
    return OFFICIAL_DOCS.get(key, "")


_BY_ALIAS = {alias: lang for lang in LANGUAGES for alias in lang.aliases}
#: Every path segment any language is filed under, for spotting a variant.
_SEGMENTS = {seg: lang for lang in LANGUAGES for seg in lang.segments}


def canonical(said: str | None) -> Language | None:
    """The language a caller meant, from whatever they called it."""
    text = (said or "").strip().lower()
    if not text:
        return None
    return _BY_ALIAS.get(text) or _BY_ALIAS.get(text.replace(" ", "")) or \
        _BY_ALIAS.get(text.split()[0] if text.split() else "")


def segment_language(segment: str) -> Language | None:
    """The language a path segment or host label names, if it names one.

    Only the unambiguous ones: `/go/` is as often a section called "go" as it
    is the Go variant, and `/net/` is a package, so two-letter and ordinary
    words are answered only where a caller has already asked for a language
    (`variant_segments`)."""
    low = (segment or "").lower()
    if low in ("go", "net", "cs", "ts", "rs", "kt", "rb", "ex", "ios"):
        return None
    return _SEGMENTS.get(low)


def variant_segments(lang: Language) -> tuple[str, ...]:
    return lang.segments


_FENCE_CLASS = re.compile(r"""(?:language|lang|highlight|brush)-([\w+#.-]+)""", re.I)
_MD_FENCE = re.compile(r"^```\s*([\w+#.-]+)", re.M)


def evidence(text: str) -> dict[str, int]:
    """How many signs of each language a page carries: its code blocks'
    declared languages, weighted, and the idioms only that language's docs
    contain.

    Meant for a page's *content* -- its extracted Markdown -- and not its raw
    HTML: a page's script bundles and stylesheets hold idioms of every
    language there is, and measured on the raw HTML of
    `docs.langchain.com/oss/python/langgraph/overview` and its JavaScript
    twin, both came out "mostly PHP" (2026-09-24)."""
    counts: dict[str, int] = {}
    low = text or ""
    for m in list(_FENCE_CLASS.finditer(low)) + list(_MD_FENCE.finditer(low)):
        tag = m.group(1).lower()
        for lang in LANGUAGES:
            if tag in lang.fences:
                counts[lang.name] = counts.get(lang.name, 0) + 3
                break
    for lang in LANGUAGES:
        hits = sum(min(low.count(mark), 5) for mark in lang.marks)
        if hits:
            counts[lang.name] = counts.get(lang.name, 0) + hits
    return counts


def written_for(text: str, lang: Language, share: float = 0.5) -> bool | None:
    """Is this page written for `lang`? None when it shows too little code to
    say. A page that shows several languages side by side -- a Stripe page
    with a tab per SDK -- counts as written for each of them that it shows
    substantially."""
    counts = evidence(text)
    total = sum(counts.values())
    if total < 6:
        return None
    mine = counts.get(lang.name, 0)
    if mine >= share * total:
        return True
    # Several languages, each substantially: a multi-language page serves this
    # one too if it is among the leaders.
    leaders = sorted(counts.values(), reverse=True)
    return mine >= 6 and mine >= leaders[min(2, len(leaders) - 1)]


def dominant(text: str) -> str:
    """The language a page is mostly written for, or "" if it does not say."""
    counts = evidence(text)
    if not counts or sum(counts.values()) < 6:
        return ""
    name, n = max(counts.items(), key=lambda kv: kv[1])
    return name if n >= 0.5 * sum(counts.values()) else ""
