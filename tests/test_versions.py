"""Version-label ordering — what "latest" is allowed to mean.

The bug this exists to prevent: `read_knowledge_base("pydantic")` returned 1.10
over 2.11, because 1.10 was crawled second and "latest" meant most recently
downloaded. Both versions were correctly stored side by side; the last step
picked the wrong one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import versions


@pytest.mark.parametrize("label,kind", [
    ("2.11", versions.RELEASE),
    ("v3", versions.RELEASE),
    ("1.10.4", versions.RELEASE),
    ("2026-08-20", versions.DATE),
    ("latest", versions.UNKNOWN),
    ("stable", versions.UNKNOWN),
    ("", versions.UNKNOWN),
])
def test_labels_are_sorted_into_kinds(label, kind):
    assert versions.kind(label) == kind


def test_release_numbers_are_not_decimals():
    """The measured failure. 1.10 came after 1.9, and a lexical or numeric
    compare puts it before."""
    assert versions.newest(["1.9", "1.10"]) == "1.10"
    assert versions.newest(["1.10", "2.11"]) == "2.11"


def test_the_v_prefix_does_not_change_the_order():
    assert versions.newest(["v2", "v3"]) == "v3"
    assert versions.newest(["v10", "v9"]) == "v10"


def test_a_release_number_always_beats_a_harvest_date():
    """A date label only ever appears because we failed to find a version, so
    it must never outrank a version we did find — whatever the calendar says."""
    assert versions.newest(["2026-08-20", "2.11"]) == "2.11"
    assert versions.newest(["2.11", "2026-08-20"]) == "2.11"


def test_dates_order_among_themselves():
    assert versions.newest(["2026-01-09", "2026-08-20"]) == "2026-08-20"


def test_a_prerelease_sorts_below_the_release_it_leads_to():
    assert versions.newest(["2.0-rc1", "2.0"]) == "2.0"


def test_unorderable_labels_keep_the_callers_order():
    """"old" and "new" carry no ordering, and comparing them as text gets it
    exactly backwards. Callers pass them harvest-newest-first instead."""
    assert versions.newest(["new", "old"]) == "new"
    assert versions.newest(["old", "new"]) == "old"


def test_nothing_in_nothing_out():
    assert versions.newest([]) == ""


def test_ordered_puts_the_newest_first():
    assert versions.ordered(["1.9", "2.11", "1.10"]) == ["2.11", "1.10", "1.9"]


def test_the_provenance_of_a_label_is_reportable():
    """Part of the honesty contract: a caller shown "2.11" deserves to know
    whether that is a release the docs announced or the day we downloaded it."""
    assert versions.why("2.11") == "release number"
    assert versions.why("2026-08-20") == "harvest date"
    assert versions.why("stable") == "unrecognised label"


# ── The default version (`Issues.md` V3) ─────────────────────────────────────
def _default(*rows):
    return max(rows, key=lambda r: versions.default_key(*r))[0]


def test_a_found_date_outranks_a_pinned_release():
    """bench-2 `jest_default_is_current`: Jest's current docs under a date,
    29.7 pinned for another project, and every versionless read got 29.7."""
    assert _default(("29.7", True, 50.0), ("2026-09-21", False, 10.0)) == "2026-09-21"


def test_a_found_alias_outranks_a_pinned_release():
    """bench-2 `pydantic_default_is_current`: `latest` beside a pinned 1.10."""
    assert _default(("1.10", True, 50.0), ("latest", False, 10.0)) == "latest"


def test_among_found_versions_the_newest_harvest_is_current():
    """Each was the current release when it was harvested, so the later
    capture is the later claim — `latest` from January does not outrank
    2.11 from June, the case plain alias-ranking would have got wrong."""
    assert _default(("latest", False, 1.0), ("2.11", False, 9.0)) == "2.11"


def test_rows_that_never_recorded_it_keep_label_ordering():
    assert _default(("2.11", None, 1.0), ("1.10", None, 9.0)) == "2.11"


def test_pinned_versions_are_ordered_by_label_among_themselves():
    assert _default(("1.10", True, 9.0), ("2.11", True, 1.0)) == "2.11"


def test_re_fetching_a_found_release_by_name_keeps_it_found():
    assert versions.still_pinned(True, False) is False
    assert versions.still_pinned(False, True) is False
    assert versions.still_pinned(True, None) is True
    assert versions.still_pinned(None, False) is False
