"""
Bounded reasoning: a few model calls at the decisions that determine correctness.

PROPOSAL-II said "No LLM in the per-page loop", and that rule was right. A model
call per page on a 1,200-page site is the huge bill this project exists not to
send. PROPOSAL-3 amends it to say what it always meant: no LLM *in the loop*, a
bounded number of calls *at the decision points*, cached so a decision is made
once per template or per host rather than once per page.

Four moments decide whether a harvest is correct, and all four are settled by
arithmetic today at the exact place the arithmetic is weakest:

  1. a template none of the nine selectors recognise;
  2. kind confidence below threshold on a mandatory kind;
  3. a corpus proposed on a new host — the identity gate confuses *projects*
     with *names*, which is `ISSUES.md` R1 and the top open defect;
  4. a page answering 200 while rendering an error, which no status code shows.

Four properties make spending anything here safe, and they are Invariant 18:

* **Bounded.** `Budget(calls=12)` per harvest, a hard cap. Exhausted means fall
  back, never stall.
* **Cached** by cluster and by host, never by page. A 703-page site with three
  templates spends at most three calls on selectors.
* **Optional.** Off unless `DOCSFORGE_REASONING` is on *and* a provider is
  configured. The zero-key, one-command install is half the product's pitch and
  does not move. Every decision keeps the algorithmic fallback that runs today.
* **Recorded.** Every consultation — question, answer, whether it came from
  cache, whether it failed — lands in `stats["reasoning"]` beside the coverage
  note. A judgement nobody can audit is worse than a heuristic, because a
  heuristic can at least be read.

The arithmetic, stated plainly because "pay a little" has to mean something:
twelve calls at roughly 1,500 tokens in and 100 out is about 20k tokens per
harvest — against a crawl that otherwise fetches and stores a thousand pages.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

#: A hard cap per harvest, not a target. Most harvests spend none of it: the
#: fallbacks are what run unless a decision is genuinely close.
DEFAULT_CALLS = 12

#: Answers are short by construction — a selector, a kind, a yes/no and a
#: reason. Anything longer is a model ignoring the question, and truncating is
#: a better failure than pasting an essay into a coverage note.
MAX_ANSWER = 400

#: How much of a page a question may carry. Enough to judge a template or a
#: landing page, far below what an LLM-per-page design would send.
MAX_SAMPLE = 4_000

_ON = ("1", "on", "true", "yes")


def configured() -> bool:
    """Whether reasoning is switched on at all.

    Two independent switches, deliberately: the environment says whether the
    operator wants to spend anything, and the provider layer says whether there
    is anything to spend it on. Neither implies the other.
    """
    if os.environ.get("DOCSFORGE_REASONING", "off").strip().lower() not in _ON:
        return False
    try:
        from docsforge import providers
        return any(p.available() for p in providers.PROVIDERS)
    except Exception:                                   # noqa: BLE001
        return False


@dataclass
class Consultation:
    """One decision, and how it was reached. This is the audit trail."""

    moment: str
    key: str
    question: str
    answer: str = ""
    cached: bool = False
    failed: str = ""

    def line(self) -> str:
        if self.failed:
            return f"- **{self.moment}** ({self.key}) — fell back: {self.failed}"
        how = "cached" if self.cached else "asked"
        return f"- **{self.moment}** ({self.key}, {how}) — {self.answer}"

    def as_dict(self) -> dict:
        return {"moment": self.moment, "key": self.key, "question": self.question,
                "answer": self.answer, "cached": self.cached, "failed": self.failed}


@dataclass
class Budget:
    """A hard cap on model calls for one harvest."""

    calls: int = DEFAULT_CALLS
    spent: int = 0

    @property
    def left(self) -> int:
        return max(0, self.calls - self.spent)

    def take(self) -> bool:
        """Reserve one call. False when the budget is gone — fall back, do not wait."""
        if self.spent >= self.calls:
            return False
        self.spent += 1
        return True


@dataclass
class Reasoner:
    """Answers a bounded number of questions, or hands back the fallback.

    `ask` never raises and never blocks on a missing provider: every caller
    gets an answer of the right shape, and the only difference reasoning makes
    is which answer. That is what lets the four decision points be written once
    rather than twice.
    """

    budget: Budget = field(default_factory=Budget)
    provider: object | None = None
    on: bool | None = None                  # None = decide from the environment
    cache: dict[str, str] = field(default_factory=dict)
    log: list[Consultation] = field(default_factory=list)

    def enabled(self) -> bool:
        return configured() if self.on is None else bool(self.on)

    def ask(self, moment: str, key: str, question: str, fallback: str = "",
            check=None) -> str:
        """Consult about one decision, or return `fallback`.

        `check` validates the answer before it is trusted — the model proposes,
        the code disposes. A selector that matches nothing is not an answer, and
        an unvalidated one would be worse than the density score it replaced.
        """
        if not self.enabled():
            return fallback

        slot = f"{moment}:{key}"
        if slot in self.cache:
            answer = self.cache[slot]
            self.log.append(Consultation(moment, key, question, answer, cached=True))
            return answer

        if not self.budget.take():
            self.log.append(Consultation(moment, key, question,
                                         failed=f"budget of {self.budget.calls} calls spent"))
            return fallback

        try:
            answer = (self._consult(question) or "").strip()[:MAX_ANSWER]
        except Exception as e:                          # noqa: BLE001
            # A provider that is slow, rate-limited or simply wrong must cost a
            # fallback, never a harvest.
            self.log.append(Consultation(moment, key, question,
                                         failed=f"{type(e).__name__}: {e}"))
            return fallback

        if not answer or (check is not None and not check(answer)):
            self.log.append(Consultation(moment, key, question, answer=answer,
                                         failed="answer did not validate"))
            return fallback

        self.cache[slot] = answer
        self.log.append(Consultation(moment, key, question, answer))
        return answer

    def _consult(self, question: str) -> str:
        provider = self.provider
        if provider is None:
            from docsforge import providers
            provider = providers.get(None)
        if callable(provider):                          # a plain function, in tests
            return provider(question)

        collected: list[str] = []
        for event in provider.stream(
            system=("You are helping a documentation harvester make one narrow "
                    "decision. Answer in as few words as the question allows. "
                    "No preamble, no explanation unless asked."),
            history=[{"role": "user", "content": question}],
            tools=[], run_tool=lambda *a, **kw: "",
        ):
            if event.get("type") == "text":
                collected.append(event.get("text") or "")
        return "".join(collected)

    # ── the audit trail ──
    def record(self) -> list[dict]:
        return [c.as_dict() for c in self.log]

    def note(self) -> str:
        """The consultations, for the coverage note. Empty when nothing was asked."""
        if not self.log:
            return ""
        head = (f"**{len(self.log)} reasoning consultation(s)**, "
                f"{self.budget.spent} of {self.budget.calls} calls spent:")
        return "\n".join([head] + [c.line() for c in self.log])


#: A reasoner that is switched off. Callers can hold one unconditionally rather
#: than testing for None at every decision point, which is how a decision point
#: quietly acquires two code paths and then only one of them gets tested.
OFF = Reasoner(on=False)


# ─────────────────────────────────────────────────────────────
# The ambient reasoner
# ─────────────────────────────────────────────────────────────
#: Reasoning is cross-cutting: it touches extraction, selection and the identity
#: gate, which sit in three modules reached through call chains that have no
#: business growing a `reasoner` parameter each. A context variable keeps the
#: decision points readable and keeps `OFF` the default everywhere — including
#: in every test that never sets one, which is what makes "off behaves exactly
#: as before" checkable rather than merely intended.
#:
#: Fetch workers never consult. Extraction and selection run on the crawl's own
#: thread, which is the thread this is set on.
_CURRENT: ContextVar["Reasoner"] = ContextVar("docsforge_reasoner", default=OFF)


def current() -> "Reasoner":
    return _CURRENT.get()


@contextmanager
def active(reasoner: "Reasoner"):
    """Make `reasoner` the one the decision points consult, for this block."""
    token = _CURRENT.set(reasoner)
    try:
        yield reasoner
    finally:
        _CURRENT.reset(token)
