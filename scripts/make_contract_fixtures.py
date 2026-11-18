"""
Write the contract-v1 fixture set to `tests/fixtures/contract-v1/`.

The synthetic pair asked for in Discussion #35, plus the two records that sit
beside a revision, all from one imaginary run against `acme.example`:

    complete.md          an entry corpus, harvested whole (sitemap-counted)
    partial.md           a federated corpus of the same run, cut by its page limit
    decision.json        the planning decision for that run (`strict=True`):
                         not usable, because the SDK corpus is incomplete
    attempt-stopped.json a different run that stopped reporting and published
                         nothing — a status record, since there is no revision

Generated through the real renderer (`docsforge.store.contract`) and the real
status record (`harvest_jobs.Job.as_dict`), with `complete` decided by the
real rule (`contract.judge`), so the fixtures cannot drift from the code:
`tests/test_contract_fixtures.py` regenerates them and compares byte for
byte. Run this after changing the format, and commit what it writes.

    python scripts/make_contract_fixtures.py
"""

from __future__ import annotations

import calendar
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.store import contract  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "contract-v1"

RUN = "run-20261118T101458Z-77e0b4a2"
JOB = "acme-sdk-1"
COMPLETE_ID = "hv-20261118T101500Z-3f9a2c1d"
PARTIAL_ID = "hv-20261118T101530Z-5e2d90aa"

COMPLETE_PAGES = [
    ("Getting Started", "https://docs.acme.example/2.4/start",
     "Install the SDK with `pip install acme-sdk`, then create a client.\n\n"
     "```python\nfrom acme import Client\nclient = Client(token=\"YOUR_TOKEN\")\n```",
     "2026-11-18T10:15:12Z"),
    ("Client API", "https://docs.acme.example/2.4/api/client",
     "`Client(token)` opens a session. Every method raises `AcmeError` on a "
     "non-2xx response.", "2026-11-18T10:15:13Z"),
]

PARTIAL_PAGES = [
    ("Streaming", "https://sdk.acme.example/streaming.md",
     "Call `client.stream(resource)` to iterate over results as they arrive.",
     "2026-11-18T10:15:34Z"),
    ("Retries", "https://sdk.acme.example/retries.md",
     "Requests are retried three times with exponential backoff; pass "
     "`retries=0` to turn that off.", "2026-11-18T10:15:35Z"),
]


def _meta(**fields) -> dict:
    """A revision's frontmatter, with `complete` decided by the real rule."""
    missing = fields.pop("not_stored")
    complete, reason = contract.judge(
        fields.pop("claimed"), fields.pop("reason", None), fields["count_basis"],
        fields["expected_pages"], fields["stored_pages"], missing)
    meta = {"docsforge_contract": contract.CONTRACT, "run_id": RUN, "job": JOB,
            "technology": "acme-sdk", "complete": complete, "complete_reason": reason,
            "not_stored": missing, "generator": "docsforge 1.1.0"}
    meta.update(fields)
    return {k: meta[k] for k in contract.FIELDS}


def build() -> dict[str, bytes]:
    """Every fixture, by file name, as the exact bytes to write."""
    complete = _meta(
        harvest_id=COMPLETE_ID, corpus="acme-sdk", corpus_role="entry",
        version="2.4", version_basis="site-path",
        source="https://docs.acme.example/2.4/", strategy="sitemap", store="files",
        scope={"mode": "section", "start": "https://docs.acme.example/2.4/",
               "widened_to": None, "topic": None, "max_pages": None},
        started_at="2026-11-18T10:15:00Z", harvested_at="2026-11-18T10:15:41Z",
        claimed=True, count_basis="sitemap", expected_pages=2, stored_pages=2,
        # A page the sitemap lists that answers 404 is not a gap.
        not_stored=contract.not_stored(
            dead_404=["https://docs.acme.example/2.4/old-quickstart"]),
    )
    partial = _meta(
        harvest_id=PARTIAL_ID, corpus="acme-sdk--sdk-acme-example",
        corpus_role="federated", version="undated", version_basis="undated",
        source="https://sdk.acme.example/", strategy="llms.txt (md manifest)",
        # Deliberately the other store: the export is the same document
        # whichever store produced it.
        store="postgres",
        scope={"mode": "section", "start": "https://sdk.acme.example/",
               "widened_to": None, "topic": None, "max_pages": 2},
        started_at="2026-11-18T10:15:30Z", harvested_at="2026-11-18T10:15:36Z",
        claimed=False, count_basis="manifest", expected_pages=120, stored_pages=2,
        reason={"code": "page-limit",
                "text": "stopped at the 2-page limit; 118 of the 120 pages the "
                        "manifest lists were not fetched"},
        not_stored=contract.not_stored(),
    )
    decision = {
        "contract": contract.CONTRACT,
        "decided_at": "2026-11-18T10:15:37Z",
        "run_id": RUN,
        "intent": "resolve-import",
        "usable_for_planning": False,
        "reason": "the 'sdk' corpus is INCOMPLETE (2 of 120 pages)",
        "judged": [
            {"corpus": "acme-sdk", "version": "2.4", "harvest_id": COMPLETE_ID,
             "kind": "api", "complete": complete["complete"]},
            {"corpus": "acme-sdk--sdk-acme-example", "version": "undated",
             "harvest_id": PARTIAL_ID, "kind": "sdk", "complete": partial["complete"]},
        ],
    }
    return {
        "complete.md": contract.render_revision(complete, COMPLETE_PAGES).encode("utf-8"),
        "partial.md": contract.render_revision(partial, PARTIAL_PAGES).encode("utf-8"),
        "decision.json": _json(decision),
        "attempt-stopped.json": _json(_stopped_attempt()),
    }


def _stopped_attempt() -> dict:
    """The status record of a run that stopped reporting with nothing stored.

    There is no revision to export, so the record is the observation: the
    shared ledger keeps it for `DOCSFORGE_HARVEST_KEEP_DAYS` and
    `harvest_status(harvest="run-…")` reports it.
    """
    from docsforge.tools import harvest_jobs as hj

    def at(stamp: str) -> float:
        return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))

    job = hj.Job(id="acme-sdk-2", label="acme-sdk", started=at("2026-11-18T10:30:00Z"),
                 progress=hj.Progress(phase="harvesting", url="https://docs.acme.example/2.5/",
                                      pages=37, expected=120),
                 state=hj.STALLED, finished=at("2026-11-18T10:30:25Z"), pid=4242,
                 updated=at("2026-11-18T10:30:25Z"), mine=False,
                 run_id="run-20261118T103000Z-c41d9e07", published=[])
    record = job.as_dict()
    record.pop("result", None)              # empty, and never part of the observation
    return record


def _json(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_bytes(data)
        print(f"wrote {OUT / name} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
