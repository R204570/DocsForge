"""
Order version labels so that "latest" means newest, not most recently fetched.

The store keeps every harvested version of a technology side by side, which is
the point — v2 and v3 of a library contradict each other. But a model that asks
for documentation without naming a version has to be handed *one* of them, and
"whichever was crawled last" is not an answer to that question. Pydantic 1.10
harvested this afternoon is not newer than Pydantic 2.11 harvested this morning.

Labels come from three places and are not comparable with each other:

    a release number   "2.11", "v3", "1.10.4"   ← a real claim about the version
    a harvest date     "2026-08-20"             ← we could not find a version
    anything else      "latest", "stable"       ← a moving target, no ordering

So release numbers outrank dates, dates outrank the rest, and within each kind
the ordering is the obvious one. `1.10` sorts above `1.9` — these are release
numbers, not decimals.
"""

from __future__ import annotations

import re

#: A label that is entirely a date: the fallback applied when a harvest could
#: not establish what version it was reading.
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

#: A release number, with optional `v` prefix and optional trailing prerelease
#: (`1.2.0-rc1`). Only the leading dotted-numeric run is compared.
_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+.]?(.*))?$", re.I)

#: Ranks. Higher wins. A real release number always beats a harvest date,
#: because the date only ever appears when we failed to find a number.
RELEASE, DATE, UNKNOWN = 2, 1, 0


def kind(label: str) -> int:
    """Which of the three sorts of label this is."""
    text = (label or "").strip()
    if _DATE.match(text):
        return DATE
    if _RELEASE.match(text) and re.search(r"\d", text):
        return RELEASE
    return UNKNOWN


def sort_key(label: str) -> tuple:
    """A key that orders labels newest-last, comparable across all three kinds.

    Kind dominates, so no release number can ever be outranked by a date.
    """
    text = (label or "").strip()

    date = _DATE.match(text)
    if date:
        return (DATE, tuple(int(p) for p in date.groups()), ())

    release = _RELEASE.match(text)
    if release and re.search(r"\d", text):
        parts = tuple(int(p) for p in release.group(1).split("."))
        # A prerelease sorts below the release it leads to: 2.0 > 2.0-rc1.
        # An empty suffix has to compare *greater*, so flag it separately.
        suffix = release.group(2) or ""
        return (RELEASE, parts, (1,) if not suffix else (0, suffix))

    # No ordering claim at all. Deliberately *not* keyed on the text: "old" and
    # "new" are not orderable, and pretending they are with a lexical compare
    # gets it exactly backwards. Callers break these ties on harvest time.
    return (UNKNOWN, (), ())


def newest(labels) -> str:
    """The newest of these labels, or "" if there are none.

    Ties keep the first the caller gave us. Labels that carry no ordering at
    all — "stable", "old" — all tie with each other, so pass them in
    harvest-newest-first order and the most recent one wins by default.
    """
    best, best_key = "", None
    for label in labels:
        key = sort_key(label)
        if best_key is None or key > best_key:
            best, best_key = label, key
    return best


def ordered(labels, newest_first: bool = True) -> list[str]:
    """All the labels, newest first by default."""
    return sorted(labels, key=sort_key, reverse=newest_first)


def why(label: str) -> str:
    """How this label was ranked — for the honesty contract.

    A caller shown "2.11" deserves to know whether that is the version the
    documentation announced or the day we happened to download it.
    """
    return {RELEASE: "release number", DATE: "harvest date",
            UNKNOWN: "unrecognised label"}[kind(label)]
