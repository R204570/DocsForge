"""`RULES` has to move when the rules do, and relying on memory did not work.

The stamp exists so a fix reaches the cache: an entry decided under superseded
rules is evidence about the code that wrote it, not about the name, so `recall`
discards it. It works. What failed is the handle.

PR #9 changed how a winner is chosen — read every candidate and rank them by
what their signals mean — and left `RULES` at 1. A cache holding
`langchain -> reference.langchain.com` at `rules: 1` therefore still matched,
`recall` served it in one second, and the new ranking never ran. The fix
shipped and reached nobody.

So the reminder is a test rather than a comment. It fingerprints the functions
that actually decide an answer, ignoring comments and docstrings so that
rewording is free. If the fingerprint moves, either bump `RULES` and update
`FINGERPRINT` below, or explain in review why an answer decided by the new code
is still interchangeable with one decided by the old.
"""
import ast
import hashlib
import inspect
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import resolver

#: Everything that can change *which URL comes back* for a name.
DECIDERS = (
    "identity_signals",     # what a page is taken to be claiming
    "is_identified",        # whether that clears the bar
    "evidence",             # how two candidates that both clear it compare
    "best_verified",        # which one is returned
    "_owns_the_name",       # whether a host counts as the project's own
    "is_forge",             # whether it is a code host rather than a site
    # The list above named only the functions that existed when it was
    # written, and `_behaviour` reads one function's own source -- not its
    # callees. So decision logic moved *into* a helper leaves the tripwire
    # silent: changing `name_authority` without touching `evidence` would
    # alter which URL every name resolves to and match the old fingerprint.
    "name_authority",       # how strong a claim the host makes on the name
    "_path_identity",       # whether a registry-nominated path names it
    "probe_docs_root",      # which URLs are offered as a docs root at all
    "_indexes_only_articles",   # whether a published llms.txt is a newsroom
)


def _behaviour(func) -> str:
    """A function's source with comments and docstrings removed.

    Textual rather than an AST dump, because `ast.dump` output has changed
    between Python versions and this must not fail merely for running on a
    different interpreter than it was pinned on.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    drop: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            first = body[0].value
            drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if number in drop:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append(" ".join(stripped.split()))
    return "\n".join(kept)


def fingerprint() -> str:
    joined = "\n---\n".join(_behaviour(getattr(resolver, n)) for n in DECIDERS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


#: Bump `RULES` and update this together, never one without the other.
#:
#: One exception on record: 2026-09-13 the modules moved into the `docsforge`
#: package and the two local imports inside `_indexes_only_articles` were
#: re-spelled (`import docsforge` -> `from docsforge.core import engine`).
#: The pre-move source hashed to ccb75e075c7c0454; nothing about which URL
#: a name resolves to changed, so the fingerprint was re-pinned and RULES
#: was not bumped — a bump would have thrown away every cached resolution
#: for a change that made no decision differently.
FINGERPRINT = "3f2b9744ec1462ea"
EXPECTED_RULES = 4


def test_rules_is_bumped_when_the_decision_logic_changes():
    assert resolver.RULES == EXPECTED_RULES, (
        "RULES moved without EXPECTED_RULES. If that was deliberate, update "
        "both this and FINGERPRINT together."
    )
    assert fingerprint() == FINGERPRINT, (
        "The functions that decide which URL a name resolves to have changed, "
        f"but RULES is still {resolver.RULES}.\n\n"
        "Every cached resolution decided by the old code will keep being "
        "served — which is exactly how the langchain fix reached nobody.\n\n"
        f"Bump resolver.RULES to {resolver.RULES + 1}, set EXPECTED_RULES to "
        f"match, and set FINGERPRINT to {fingerprint()!r}.\n\n"
        "If the change genuinely cannot alter any answer, update FINGERPRINT "
        "alone and say why in review."
    )


def test_rewording_a_comment_or_docstring_does_not_demand_a_bump():
    """The tripwire must be worth keeping. One that fires on a typo fix gets
    disabled, and then it protects nothing."""
    body = _behaviour(resolver.evidence)
    assert "#" not in body
    assert "How good an answer" not in body, "docstring should be stripped"
    assert "is_forge" in body, "the logic itself must still be fingerprinted"


def test_a_cache_entry_from_the_previous_rules_is_refused(tmp_path, monkeypatch):
    """The concrete case: what your machine was actually holding."""
    import json
    import time

    cache = tmp_path / "resolutions.json"
    cache.write_text(json.dumps({"langchain": {
        "at": time.time(), "rules": 1,
        "url": "https://reference.langchain.com/python/langchain/langchain/",
        "evidence": "", "reason": "", "signals": [], "ecosystem": "",
        "resolved_via": "registry", "note": "",
    }}), encoding="utf-8")
    monkeypatch.setenv("DOCSFORGE_RESOLVE_CACHE", str(cache))

    assert resolver.recall("langchain") is None
