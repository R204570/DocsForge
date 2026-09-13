"""
Selection: harvest what is needed, ask when unsure, never guess.

This is the layer that makes platform-scale technologies tractable without
spending the completeness guarantee, and it is the one most likely to be eroded
later. The distinction that keeps it safe:

  * Choosing **which corpora** enter scope is a *declaration*. It is recorded in
    the result, and an unselected corpus is listed with its magnitude and marked
    `not requested` — never silently absent.
  * Dropping pages **inside** a selected corpus is *filtering*, and is
    forbidden. A selected corpus is harvested whole or reported partial.

That is Invariant 4, and it is the whole safety property. Once `intent` exists,
every future request to "just skip the irrelevant pages" will sound reasonable
and each concession will be small. The line is corpus granularity, declared and
reported, and `test_a_selected_corpus_is_harvested_whole` exists to hold it.

The other half is Invariant 10: when the system cannot determine what is needed
it **asks**. If it cannot ask, it **refuses and returns the options**. It never
guesses, because a silently truncated harvest that looks successful is worse
than a refusal a caller can act on.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# Intent
# ─────────────────────────────────────────────────────────────
#: What each purpose needs. `reference` is the default and deliberately takes
#: everything except `meta`: a caller who has not said what they are doing has
#: not authorised anything to be left out.
INTENTS: dict[str, dict[str, tuple[str, ...]]] = {
    "resolve-import": {                       # FlowIT, at import time
        "mandatory": ("api", "sdk"),
        # `adk` is optional rather than mandatory: resolving an import needs
        # the symbols and the client library. An agent kit is a real corpus and
        # worth taking when it exists, but a technology without one is not
        # therefore unusable for planning.
        "optional": ("spec", "language", "adk"),
        "excluded": ("guide", "cookbook", "changelog", "meta", "operations"),
    },
    "implement": {
        "mandatory": ("api",),
        "optional": ("guide", "cookbook", "sdk", "adk"),
        "excluded": ("changelog", "meta"),
    },
    "learn": {
        "mandatory": ("guide",),
        "optional": ("language", "spec", "cookbook"),
        "excluded": ("changelog", "meta"),
    },
    "operate": {
        "mandatory": ("operations",),
        "optional": ("guide", "api"),
        "excluded": ("meta",),
    },
    "reference": {                            # the default
        "mandatory": (),
        "optional": ("*",),
        "excluded": ("meta",),
    },
}

DEFAULT_INTENT = "reference"

#: Peer corpora of comparable magnitude past which the intent is not doing
#: enough work to choose. PROVISIONAL — PROPOSAL-II calls eight a guess and it
#: still is; see ISSUES F2. Nothing has measured real platform-scale breadth.
BREADTH_LIMIT = 8

#: Below this, a kind classification is not firm enough to rest a mandatory
#: selection on. Also provisional.
MIN_KIND_CONFIDENCE = 0.5


def intent_spec(intent: str) -> dict[str, tuple[str, ...]]:
    return INTENTS.get(intent or DEFAULT_INTENT, INTENTS[DEFAULT_INTENT])


# ─────────────────────────────────────────────────────────────
# The result
# ─────────────────────────────────────────────────────────────
@dataclass
class Selection:
    """What was chosen, or why nothing could be."""

    intent: str = DEFAULT_INTENT
    selected: list = field(default_factory=list)
    options: list = field(default_factory=list)
    trigger: str = ""            # "" | "breadth" | "confidence" | "tie"
    reason: str = ""
    from_policy: bool = False

    @property
    def needs_selection(self) -> bool:
        """True when the caller has to decide and we must not decide for them."""
        return bool(self.trigger)

    def as_dict(self) -> dict:
        return {
            "intent": self.intent,
            "needs_selection": self.needs_selection,
            "trigger": self.trigger,
            "reason": self.reason,
            "from_policy": self.from_policy,
            "selected": [c.url for c in self.selected],
            "options": [{"url": c.url, "kind": c.kind,
                         "confidence": round(c.kind_confidence, 2),
                         "magnitude": c.magnitude} for c in self.options],
        }

    def question(self) -> str:
        """The one option-based question to put to a caller who can answer.

        Asked once, with options ordered by magnitude and each labelled with
        its kind and estimated size — a caller cannot choose between corpora
        described only by URL.
        """
        lines = [f"**{self.reason}**", "",
                 "Which documentation should be harvested? Options, largest first:"]
        for i, corpus in enumerate(self.options, 1):
            size = f"~{corpus.magnitude} pages" if corpus.magnitude else "size unknown"
            kind = corpus.kind or "unclassified"
            lines.append(f"{i}. {corpus.url} — {kind}, {size}")
        lines += ["", "Answer with `corpora=[...]` naming the URLs you want. "
                      "The answer is remembered, so this is asked once."]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Choosing
# ─────────────────────────────────────────────────────────────
def _wanted(corpus, spec: dict) -> bool:
    kind = corpus.kind or ""
    if kind in spec["excluded"]:
        return False
    if "*" in spec["optional"]:
        return True
    return kind in spec["mandatory"] or kind in spec["optional"]


def select(corpora: list, intent: str = DEFAULT_INTENT,
           explicit: list[str] | None = None,
           policy: list[str] | None = None) -> Selection:
    """Decide which corpora enter scope, or escalate rather than guess.

    Escalation is rare by construction. A single-corpus site never escalates. A
    technology with a manual, a specification and one API reference never
    escalates under any intent, because each mandatory kind is matched
    unambiguously. It happens where it should: platform-scale names, genuinely
    ambiguous classification, and ties.
    """
    intent = intent or DEFAULT_INTENT
    spec = intent_spec(intent)
    result = Selection(intent=intent)

    # 1. The caller has decided. Nothing here second-guesses that.
    if explicit:
        chosen = [c for c in corpora if c.url in set(explicit)]
        _mark(corpora, chosen)
        result.selected = chosen
        result.reason = "the caller named the corpora explicitly"
        return result

    # 2. A remembered answer — unless something has appeared that the caller
    #    never got to rule on.
    #
    #    This is why a policy records the corpora that EXISTED when it was made
    #    and not only the ones chosen. Without the `known` list there is no way
    #    to tell a corpus that has just appeared from one the caller looked at
    #    and deliberately rejected, and treating the second as the first
    #    re-asks a question that was already answered — which is the exact
    #    escalation fatigue the policy cache exists to prevent.
    if policy:
        chosen_urls = set(policy.get("chosen") or [])
        known_urls = set(policy.get("known") or []) | chosen_urls
        surprises = [c for c in corpora
                     if c.url not in known_urls and c.kind in spec["mandatory"]]
        if not surprises:
            chosen = [c for c in corpora if c.url in chosen_urls]
            _mark(corpora, chosen)
            result.selected = chosen
            result.from_policy = True
            result.reason = "a stored selection policy for this technology"
            return result
        result.trigger = "tie"
        result.options = corpora
        result.reason = (
            f"{len(surprises)} corpus/corpora of a required kind appeared "
            f"since the stored choice was made")
        return result

    # 3. One corpus is not a choice.
    if len(corpora) <= 1:
        _mark(corpora, corpora)
        result.selected = list(corpora)
        result.reason = "a single corpus; nothing to choose between"
        return result

    eligible = [c for c in corpora if _wanted(c, spec)]

    # T3 — several corpora tie for one mandatory kind.
    for kind in spec["mandatory"]:
        matches = [c for c in eligible if c.kind == kind]
        if len(matches) > 1:
            result.trigger = "tie"
            result.options = matches
            result.reason = (f"{len(matches)} corpora each look like the "
                             f"{kind!r} documentation for intent {intent!r}")
            return result

        # T2 — low confidence on a mandatory kind.
        if matches and matches[0].kind_confidence < MIN_KIND_CONFIDENCE:
            result.trigger = "confidence"
            result.options = corpora
            result.reason = (f"the {kind!r} corpus was classified with low "
                             f"confidence ({matches[0].kind_confidence:.2f})")
            return result

    # T1 — too many peers of comparable magnitude, and the intent has not
    #      narrowed them.
    if len(eligible) > BREADTH_LIMIT:
        result.trigger = "breadth"
        result.options = sorted(eligible, key=lambda c: -c.magnitude)
        result.reason = (f"{len(eligible)} corpora match intent {intent!r}, "
                         f"which is past the point where it is doing the choosing")
        return result

    _mark(corpora, eligible)
    # `_mark` keeps the entry corpus regardless; the result has to say the same
    # thing, or the note and the harvest loop disagree about what is in scope.
    result.selected = [c for c in corpora if c.selected]
    result.reason = f"intent {intent!r} selected {len(result.selected)} of {len(corpora)} corpora"
    return result


def _mark(corpora: list, chosen: list) -> None:
    """Record scope on every corpus, chosen or not. Invariant 5.

    The entry corpus is never deselected. It is the URL the caller handed in,
    and by the time selection runs it has already been crawled and stored — so
    marking it "not requested" is false twice over. It happened because
    `classify_kind` returns `""` for a docs root with no kind token in its path,
    which is most of them, and an unclassified corpus matches no kind-specific
    intent (`ISSUES.md` W4).
    """
    picked = {id(c) for c in chosen}
    for corpus in corpora:
        corpus.selected = id(corpus) in picked or getattr(corpus, "entry", False)
        if not corpus.selected and not corpus.status:
            corpus.status = "**not requested**"


# ─────────────────────────────────────────────────────────────
# The FlowIT contract
# ─────────────────────────────────────────────────────────────
def usable_for_planning(selection: Selection, corpora: list) -> tuple[bool, str]:
    """Whether a downstream caller may generate a plan from this harvest.

    The honesty contract doing real work rather than decorating a response: a
    downstream system can *refuse to act* on the basis of a coverage value. It
    is also the strongest argument for the whole design — no competitor returns
    a figure a caller can safely gate on, because no competitor knows what its
    own coverage is.
    """
    if selection.needs_selection:
        return False, f"selection unresolved: {selection.reason}"

    spec = intent_spec(selection.intent)
    if not spec["mandatory"]:
        return True, "no kind is mandatory for this intent"

    for kind in spec["mandatory"]:
        matches = [c for c in corpora if c.selected and c.kind == kind]
        if not matches:
            return False, f"no {kind!r} corpus was found or selected"
        for corpus in matches:
            if corpus.complete is False:
                return False, (f"the {kind!r} corpus is INCOMPLETE "
                               f"({corpus.stored} of {corpus.expected} pages)")
            if corpus.complete is None:
                return False, (f"coverage of the {kind!r} corpus is unknown — "
                               f"nothing established how much exists")
    return True, "every mandatory corpus is complete"


# ─────────────────────────────────────────────────────────────
# Asking
# ─────────────────────────────────────────────────────────────
#: The channel this process can put a question on, installed by whichever
#: surface is running. `None` means there is nobody to ask, and Invariant 10 is
#: then explicit about what to do instead: refuse, and return the options.
_ASKER = None


def set_asker(fn) -> None:
    """Install the channel a human can be reached on.

    A callable taking a `Selection` and returning the chosen URLs, or `None`
    if the question could not be put after all. The MCP server installs an
    elicitation-backed one where the client supports elicitation; the CLI
    falls back to the terminal below. Injected rather than imported so this
    module stays testable and so no surface is assumed.
    """
    global _ASKER
    _ASKER = fn


def can_ask() -> bool:
    """Is there anyone to ask?"""
    if _ASKER is not None:
        return True
    try:
        import sys
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def ask(selection: "Selection") -> list[str] | None:
    """Put the question once. Returns the chosen URLs, or None if it could not.

    Returning `None` is not a failure mode to paper over — it is the condition
    Invariant 10 describes, and the caller's job is then to refuse with the
    options rather than pick something.
    """
    if not selection.needs_selection or not selection.options:
        return None

    if _ASKER is not None:
        try:
            answer = _ASKER(selection)
        except Exception:
            return None
        return [u for u in (answer or []) if u] or None

    if not can_ask():
        return None

    import sys
    print(selection.question(), file=sys.stderr)
    try:
        reply = input("Corpora to harvest (numbers, comma-separated; "
                      "blank = all): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not reply:
        return [c.url for c in selection.options]
    chosen: list[str] = []
    for piece in reply.split(","):
        piece = piece.strip()
        if piece.isdigit() and 1 <= int(piece) <= len(selection.options):
            chosen.append(selection.options[int(piece) - 1].url)
    return chosen or None


# ─────────────────────────────────────────────────────────────
# Remembering the answer
# ─────────────────────────────────────────────────────────────
POLICY_TTL = 90 * 86400


def _policy_file() -> Path:
    return Path(os.environ.get("DOCSFORGE_SELECTION_POLICY")
                or Path.home() / ".docsforge" / "selection.json")


def _load() -> dict:
    try:
        return json.loads(_policy_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        path = _policy_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception:
        pass


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def recall_policy(name: str) -> dict | None:
    """The stored choice for this technology: what was chosen, and what existed."""
    entry = _load().get(_slug(name))
    if not entry or time.time() - entry.get("at", 0) > POLICY_TTL:
        return None
    chosen = list(entry.get("chosen") or [])
    if not chosen:
        return None
    return {"chosen": chosen, "known": list(entry.get("known") or [])}


def remember_policy(name: str, chosen: list[str], known: list[str] | None = None) -> None:
    """Store the answer so the question is never asked twice.

    `known` is the full set of corpora that existed when the caller chose. It
    is what lets a later run tell a brand-new corpus from one that was offered
    and turned down.
    """
    data = _load()
    data[_slug(name)] = {"at": time.time(), "chosen": list(chosen),
                         "known": list(known or chosen)}
    _save(data)


def forget_selection(name: str = "") -> str:
    """Drop a remembered selection, or all of them.

    Ships with the policy cache for the same reason `forget_resolution` ships
    with the resolution cache: a remembered choice that has become wrong is
    exactly the state a caller needs to be able to clear.
    """
    data = _load()
    if not name:
        _save({})
        return f"Forgot {len(data)} stored selection policy/policies."
    key = _slug(name)
    if data.pop(key, None) is None:
        return f"No selection policy stored for {name!r}."
    _save(data)
    return f"Forgot the selection policy for {name!r}."
