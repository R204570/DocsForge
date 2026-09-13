"""Does a harvested corpus change what a model can answer?

This is the product's thesis, and until now nothing measured it. `AUDIT.md`
recorded it as M3 — "correctness is judged by hand · deferred" — and
`PRODUCT.md` still calls the whole build experimental for exactly this reason:
*"to find out whether feeding harvested docs to a model actually clears the
unknown-technology wall."*

So: the same questions, asked three ways.

  closed    the model alone, no tools, no documentation. The wall.
  passages  the model with the corpus retrieved for it — `search_knowledge_base`
            run on its behalf and the result put in front of it. Does the
            *documentation* clear the wall?
  tools     the model given DocsForge's tools and left to use them, which is
            the product as a caller actually meets it. Does the *product*
            clear it?

Splitting the last two is the point. If `passages` scores well and `tools` does
not, the corpus is good and retrieval is not being reached — a different defect
with a different fix, and one that a single "with DocsForge / without" number
would hide completely.

Grading is a string match against an exact identifier, not a model's opinion of
another model. Every expected answer is checked against the stored corpus
before a single question is asked: a fixture whose answer is not in the
documentation is testing nothing, and this refuses to run rather than report a
number nobody should believe.

    python scripts/measure_answers.py                    # Claude Code, the CLI's model
    python scripts/measure_answers.py --model claude-opus-5
    python scripts/measure_answers.py --out answers.json

The subject is Claude Code. It is the client this MCP server is built for, and
its provider was taught to honour `tools` for exactly this: the closed-book
phase runs the CLI with no MCP server and its own built-ins off, so "no
documentation" means the weights and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from docsforge.tools import forge_tools as ft          # noqa: E402  (loads .env)
from docsforge import providers                  # noqa: E402


#: One fact per question, answerable in a single identifier, and present in the
#: stored corpus — `verify()` refuses to run otherwise.
#:
#: `known` is a prediction, not a measurement: whether a model with no
#: documentation could reasonably be expected to answer. Recorded so the result
#: separates "the wall came down" from "there was no wall", which a single
#: accuracy figure cannot.
QUESTIONS = [
    # Google's Agent Development Kit — recent and niche.
    dict(tech="google-adk", known=False, expect=["LlmAgent"],
         q="In Google's Agent Development Kit (ADK), what is the exact name of "
           "the class used to build a simple LLM-backed agent? Answer with the "
           "class name only."),
    dict(tech="google-adk", known=False, expect=["google-adk"],
         q="What is the exact name of the pip package you install to use "
           "Google's Agent Development Kit in Python? Answer with the package "
           "name only."),
    dict(tech="google-adk", known=False, expect=["SequentialAgent"],
         q="In Google's Agent Development Kit (ADK), what is the exact name of "
           "the agent class that runs its sub-agents one after another, in "
           "order? Answer with the class name only."),
    dict(tech="google-adk", known=False, expect=["task_completed"],
         q="In Google's Agent Development Kit (ADK), a SequentialAgent "
           "automatically gives each sub-agent a tool to signal it has "
           "finished. What is that tool's exact function name?"),

    # Effect — a large, niche TypeScript library.
    dict(tech="effect", known=False, expect=["Effect.gen"],
         q="In the Effect library for TypeScript, which function do you call "
           "with a generator function to write sequential effectful code? "
           "Answer with the exact function name."),
    dict(tech="effect", known=False, expect=["Effect.succeed"],
         q="In the Effect library for TypeScript, what is the exact name of "
           "the constructor that creates an Effect which always succeeds with "
           "a given value?"),

    # Mojo — a new language.
    dict(tech="mojo", known=False, expect=["comptime"],
         q="In the Mojo programming language, which keyword marks a value or "
           "list that is evaluated at compile time? Answer with the keyword "
           "only."),

    # Gin — a well-known Go framework. Expected to need no documentation.
    dict(tech="gin-gonic", known=True, expect=["gin.Default"],
         q="In the Gin web framework for Go, which function returns an Engine "
           "that already has the Logger and Recovery middleware attached? "
           "Answer with the exact function call."),
    dict(tech="gin-gonic", known=True, expect=["ShouldBindJSON"],
         q="In the Gin web framework for Go, which Context method binds a JSON "
           "request body and returns the error to you to handle, rather than "
           "writing a 400 response itself? Answer with the method name only."),

    # Pydantic v2 — widely known, but the v1→v2 renames are a real trap.
    dict(tech="pydantic", known=True, expect=["model_validate"],
         q="In Pydantic v2, what replaced the v1 method `parse_obj()`? Answer "
           "with the exact method name."),
    dict(tech="pydantic", known=True, expect=["model_dump_json"],
         q="In Pydantic v2, what replaced the v1 method `json()`? Answer with "
           "the exact method name."),

    # Astro — widely known.
    dict(tech="astro", known=True, expect=["Astro.props"],
         q="In an Astro component, what expression gives you the props that "
           "were passed to it? Answer with the exact expression."),
]


CLOSED_SYSTEM = (
    "Answer the question from your own knowledge. You have no documentation "
    "and no tools. Reply with the exact identifier asked for and nothing else. "
    "If you do not know, reply exactly: I DO NOT KNOW."
)

PASSAGE_SYSTEM = (
    "Answer the question using ONLY the documentation excerpts provided. "
    "Reply with the exact identifier asked for and nothing else. "
    "If the excerpts do not contain the answer, reply exactly: I DO NOT KNOW."
)

TOOL_SYSTEM = (
    "You answer questions about software libraries. Documentation for the "
    "library has already been harvested into the knowledge base. Use "
    "search_knowledge_base and read_knowledge_base to find the answer before "
    "replying. Do NOT call learn_technology. "
    "Reply with the exact identifier asked for and nothing else. "
    "If you cannot find it, reply exactly: I DO NOT KNOW."
)

#: How much retrieved documentation the `passages` phase puts in front of the
#: model. Small models have small windows, and an excerpt nobody can read is
#: not a fair test of the corpus.
PASSAGE_CHARS = 6_000


def verify() -> list[str]:
    """Every expected answer must actually be in the stored corpus."""
    store = ft.store()
    problems = []
    for row in QUESTIONS:
        tech = row["tech"]
        entry = store.entry(tech)
        if entry is None:
            problems.append(f"{tech}: nothing stored")
            continue
        for token in row["expect"]:
            if not store.search(token, tech=tech, limit=1):
                problems.append(f"{tech}: {token!r} is not in the stored corpus")
    return problems


def _say(provider, system: str, question: str, tools, run_tool) -> str:
    out: list[str] = []
    for event in provider.stream(system=system,
                                 history=[{"role": "user", "content": question}],
                                 tools=tools, run_tool=run_tool):
        if event.get("type") == "text":
            out.append(event["text"])
    return "".join(out)


def _no_tool(name: str, args: dict) -> str:      # never called
    return ""


def closed(provider, row) -> str:
    return _say(provider, CLOSED_SYSTEM, row["q"], [], _no_tool)


def passages(provider, row) -> str:
    """Retrieve with the product's own read-time relevance, then ask."""
    found = ft.tool_search_knowledge_base(query=row["q"], technology=row["tech"],
                                          limit=5)
    excerpt = found[:PASSAGE_CHARS]
    prompt = (f"Documentation excerpts:\n\n{excerpt}\n\n"
              f"---\n\nQuestion: {row['q']}")
    return _say(provider, PASSAGE_SYSTEM, prompt, [], _no_tool)


#: The tools this phase offers. Read-only, deliberately, for two reasons.
#:
#: The measurement is "the documentation is already stored — can the model use
#: it", so acquisition is not under test and offering it only adds a way to
#: fail. And a benchmark must not write: told plainly not to call
#: `learn_technology`, qwen3.5:9b called it fifteen times in the first run and
#: began harvesting live sites. Nothing was damaged, because
#: `learn_technology` found each technology already stored and fetched
#: nothing — the short-circuit that only works now that every surface reads
#: `.env` — but a harness that relies on a product guarantee to stay harmless
#: is not a harness. It is offered no such tool now.
READ_ONLY = ("search_knowledge_base", "read_knowledge_base",
             "list_knowledge_base")


def _read_only_tools():
    """`Tool` objects, not schemas: every provider takes `forge_tools.TOOLS`
    and shapes them itself, which is what `app.py` passes."""
    return [t for t in ft.TOOLS if t.name in READ_ONLY]


def _read_only_run(name: str, args: dict) -> str:
    if name not in READ_ONLY:
        return (f"Error: {name} is not available in this evaluation. "
                f"The documentation is already stored — read it.")
    return ft.run_tool(name, args)


def tools(provider, row) -> str:
    """The product as a caller meets it: the model drives the tools itself."""
    return _say(provider, TOOL_SYSTEM,
                f"{row['q']}\n\n(The library is stored as {row['tech']!r}.)",
                _read_only_tools(), _read_only_run)


#: Punctuation a model wraps an identifier in, and that a string match should
#: not be defeated by.
_TRIM = str.maketrans("", "", "`*'\"")


def grade(answer: str, expect: list[str]) -> bool:
    hay = re.sub(r"\s+", " ", (answer or "").translate(_TRIM)).lower()
    return all(token.translate(_TRIM).lower() in hay for token in expect)


PHASES = (("closed", closed), ("passages", passages), ("tools", tools))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="claudecode")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--phases", default="closed,passages,tools")
    ap.add_argument("--first", type=int, default=0,
                    help="only the first N questions, for a quick pass")
    args = ap.parse_args(argv)

    problems = verify()
    if problems:
        print("Fixtures do not match the stored corpus, so nothing was asked:")
        for line in problems:
            print(f"  - {line}")
        print("\nHarvest the technologies above, or correct the fixtures. A "
              "question whose answer is not in the documentation measures "
              "nothing.")
        return 1

    provider = providers.get(args.provider)
    if args.model:
        os.environ[f"{provider.name.upper()}_MODEL"] = args.model
    model = provider.model()
    wanted = [p for p in PHASES if p[0] in args.phases.split(",")]

    print(f"provider : {provider.name} / {model}")
    print(f"questions: {len(QUESTIONS) if not args.first else args.first}   phases: "
          f"{', '.join(n for n, _ in wanted)}\n")

    asked = QUESTIONS[:args.first] if args.first else QUESTIONS
    results = []
    for row in asked:
        record = {"tech": row["tech"], "q": row["q"], "expect": row["expect"],
                  "known": row["known"]}
        marks = []
        for name, fn in wanted:
            started = time.time()
            try:
                answer = fn(provider, row)
                ok = grade(answer, row["expect"])
            except Exception as e:                          # noqa: BLE001
                answer, ok = f"ERROR {type(e).__name__}: {e}", False
            record[name] = {"ok": ok, "answer": answer[-400:],
                            "seconds": round(time.time() - started, 1)}
            marks.append(f"{name}={'PASS' if ok else 'fail'}")
        results.append(record)
        print(f"  {row['tech']:12s} {row['expect'][0]:20s} {'  '.join(marks)}",
              flush=True)

    print()
    print(f"{'phase':10s} {'all':>10s} {'unknown tech':>14s} {'known tech':>12s}")
    print("-" * 50)
    summary = {}
    for name, _ in wanted:
        rows = [r for r in results if name in r]
        unknown = [r for r in rows if not r["known"]]
        known = [r for r in rows if r["known"]]

        def pct(sub):
            if not sub:
                return "-"
            hit = sum(1 for r in sub if r[name]["ok"])
            return f"{hit}/{len(sub)}"

        summary[name] = {"all": pct(rows), "unknown": pct(unknown),
                         "known": pct(known)}
        print(f"{name:10s} {pct(rows):>10s} {pct(unknown):>14s} {pct(known):>12s}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"provider": provider.name, "model": model,
                       "summary": summary, "results": results}, fh, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
