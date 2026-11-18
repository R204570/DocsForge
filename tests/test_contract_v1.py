"""
Contract v1, Phase 2 (issue #42): a stored revision describes itself.

What was promised in Discussion #35, checked here against both stores:

* frontmatter with `harvest_id`, `harvested_at`, `complete` (true / false /
  null), `complete_reason`, `expected_pages` and `stored_pages`;
* `captured_at` and `sha256` on every page;
* `complete: true` only when it is earned (§5.6), enforced where the store
  knows what was stored;
* one renderer for both stores, so an export is the same document whichever
  store produced it;
* contract-0 files still read, export with `legacy` provenance, and upgrade
  in place.

No network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from docsforge.store import contract, kb_store
from docsforge.store.contract import ContractError, parse_revision, validate
from docsforge.store.kb_store import FileStore, PostgresStore, RunContext, StoreError
from docsforge.tools import forge_tools as ft

DSN = os.environ.get("DOCSFORGE_TEST_DB", "")

PAGES = [
    ("Getting Started", "https://docs.acme.example/2.4/start",
     "Install the SDK with `pip install acme-sdk`, then create a client.\n\n"
     "```python\nfrom acme import Client\nclient = Client(token=\"YOUR_TOKEN\")\n```"),
    ("Client API", "https://docs.acme.example/2.4/api/client",
     "`Client(token)` opens a session. Every method raises `AcmeError` on a "
     "non-2xx response."),
]


@pytest.fixture
def files(tmp_path):
    return FileStore(tmp_path / "kb")


def _store_pages(store, tech="acme-sdk", version="2.4", pages=PAGES, expected=2,
                 complete=True, facts=None, run=None):
    token = kb_store.CURRENT_RUN.set(run) if run is not None else None
    try:
        with store.writer(tech, version, "https://docs.acme.example/2.4/", "sitemap",
                          expected=expected) as w:
            for i, (title, url, body) in enumerate(pages):
                w.add(title, url, body, captured_at=f"2026-11-18T10:15:1{i}Z")
            return w.settle(complete=complete, expected=expected,
                            facts=facts if facts is not None
                            else {"count_basis": "sitemap", "version_basis": "site-path"})
    finally:
        if token is not None:
            kb_store.CURRENT_RUN.reset(token)


# ── the promised fields ──────────────────────────────────────────
def test_every_promised_field_is_in_the_frontmatter(files):
    entry = _store_pages(files, run=RunContext("run-x", job="acme-sdk-1"))
    revision = parse_revision(files.export("acme-sdk", "2.4"))
    meta = revision.meta

    assert list(meta) == list(contract.FIELDS), "every field, in order"
    assert meta["docsforge_contract"] == 1
    assert meta["harvest_id"] == entry["harvest_id"] and meta["harvest_id"].startswith("hv-")
    assert meta["run_id"] == "run-x" and meta["job"] == "acme-sdk-1"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["harvested_at"])
    assert meta["complete"] is True and meta["complete_reason"] is None
    assert meta["expected_pages"] == 2 and meta["stored_pages"] == 2
    assert meta["count_basis"] == "sitemap" and meta["version_basis"] == "site-path"
    assert meta["store"] == "files" and meta["corpus_role"] == "entry"
    assert set(meta["not_stored"]) == set(contract.NOT_STORED_KINDS)


def test_every_page_carries_captured_at_and_its_hash(files):
    _store_pages(files)
    revision = parse_revision(files.export("acme-sdk", "2.4"))
    assert [p.title for p in revision.pages] == ["Getting Started", "Client API"]
    for (_, _, body), page in zip(PAGES, revision.pages):
        assert page.body == body
        assert page.sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert page.bytes == len(body.encode("utf-8"))
    assert revision.pages[0].captured_at == "2026-11-18T10:15:10Z"
    assert validate(revision) == []


def test_the_spec_examples_hashes_are_what_the_renderer_writes():
    """§5.7 quotes real hashes; the renderer must agree with them."""
    block = contract.render_page(1, *PAGES[0], "2026-11-18T10:15:12Z")
    assert ('sha256="cb2c406543db031ca8b9bb8ee355235f536b347793721dd07f0f4262a7b903e0" '
            'bytes=141') in block
    block = contract.render_page(2, *PAGES[1], "2026-11-18T10:15:13Z")
    assert 'bytes=87' in block


# ── §5.6: `complete: true` is earned ────────────────────────────
@pytest.mark.parametrize("facts, expected, want, code", [
    ({"count_basis": "sitemap"}, 2, True, None),
    ({"count_basis": "crawl-frontier"}, 2, None, "no-enumeration"),
    ({"count_basis": "none"}, 2, None, "no-enumeration"),
    ({"count_basis": "sitemap"}, None, None, "no-enumeration"),
    ({"count_basis": "sitemap"}, 5, False, "listed-not-acquired"),
    ({"count_basis": "sitemap", "unextractable": ["https://x/a"]}, 2, False, "unextractable"),
    ({"count_basis": "sitemap", "refused": ["https://x/b"]}, 2, False, "rate-limited"),
    ({"count_basis": "sitemap", "dead": ["https://x/c"], "off_topic": 3}, 2, True, None),
])
def test_settle_enforces_when_complete_may_be_true(files, facts, expected, want, code):
    entry = _store_pages(files, expected=expected, facts=facts)
    assert entry["complete"] is want
    assert (entry["complete_reason"] or {}).get("code") == code
    meta = parse_revision(files.export("acme-sdk", "2.4")).meta
    assert meta["complete"] is want, "the file says what the store recorded"
    assert validate(parse_revision(files.export("acme-sdk", "2.4"))) == []


def test_a_false_or_null_always_says_why(files):
    entry = _store_pages(files, complete=None, facts={"count_basis": "crawl-frontier"})
    assert entry["complete"] is None
    assert entry["complete_reason"]["code"] == "no-enumeration"


def test_not_stored_counts_everything_and_lists_at_most_a_hundred(files):
    urls = [f"https://x.dev/{i}" for i in range(150)]
    _store_pages(files, facts={"count_basis": "sitemap", "dead": urls})
    dead = parse_revision(files.export("acme-sdk", "2.4")).meta["not_stored"]["dead_404"]
    assert dead["count"] == 150 and len(dead["urls"]) == 100


# ── one renderer, one parser ─────────────────────────────────────
def test_render_then_parse_round_trips_awkward_bodies():
    awkward = [
        ("Looks like a page", "https://x/1",
         "Intro.\n\n---\n\n## Not a page\n\nSource: <https://x/fake>\n"
         "<!-- docsforge:page n=9 captured_at=null sha256=\"" + "0" * 64 + "\" bytes=3 -->\n\nhah"),
        ("Unicode", "https://x/2", "naïve café — 日本語 🚀"),
        ("Empty", "https://x/3", ""),
        ("Ends in a rule", "https://x/4", "text\n\n---"),
    ]
    meta = contract.legacy_meta(harvest_id="hv-t", technology="t", version="1",
                                source="s", strategy="x", store="files",
                                complete=None, expected=None, stored=4, harvested_at=None)
    text = contract.render_revision(meta, [(t, u, b, None) for t, u, b in awkward])
    revision = parse_revision(text)
    assert [(p.title, p.url, p.body) for p in revision.pages] == awkward
    assert validate(revision) == []


def test_validate_catches_a_one_byte_edit(files):
    _store_pages(files)
    text = files.export("acme-sdk", "2.4")
    edited = text.replace("opens a session", "opens a sessiom")
    problems = validate(parse_revision(edited))
    assert any("sha256" in p for p in problems)


def test_a_body_changed_in_length_is_refused_by_the_parser(files):
    _store_pages(files)
    text = files.export("acme-sdk", "2.4")
    with pytest.raises(ContractError):
        parse_revision(text.replace("opens a session", "opens one session"))


def test_a_v1_file_reads_back_through_the_ordinary_readers(files):
    _store_pages(files)
    assert [p["title"] for p in files.pages("acme-sdk", "2.4")] == ["Getting Started", "Client API"]
    assert files.page("acme-sdk", "2.4", 2)["content"] == PAGES[1][2]
    body, how, n = files.read("acme-sdk", None, "2.4")
    assert how == "all" and n == 2 and "docsforge:page" not in body


# ── contract 0 ───────────────────────────────────────────────────
LEGACY = """# old 1.0 documentation

<!-- harvested: 2 pages | from: https://old.dev/ | via: crawl | 2026-09-01 10:00 -->

## Contents

1. [One](https://old.dev/1)
2. [Two](https://old.dev/2)


---

## One

Source: <https://old.dev/1>

first

---

## Two

Source: <https://old.dev/2>

second
"""


@pytest.fixture
def legacy(files):
    folder = files.root / "old"
    folder.mkdir(parents=True)
    (folder / "1.0.md").write_text(LEGACY, encoding="utf-8", newline="\n")
    files.index_path.write_text(json.dumps({"old@1.0": {
        "technology": "old", "version": "1.0", "source": "https://old.dev/",
        "strategy": "crawl", "pages": 2, "characters": len(LEGACY),
        "file": str(folder / "1.0.md"), "harvested": "2026-09-01 10:00",
        "saved": 1788000000.0, "complete": True, "expected": None,
        "titles": ["One", "Two"]}}), encoding="utf-8")
    return files


def test_a_contract_0_file_still_reads(legacy):
    assert [p["title"] for p in legacy.pages("old", "1.0")] == ["One", "Two"]


def test_a_contract_0_export_says_legacy_and_does_not_claim_complete(legacy):
    revision = parse_revision(legacy.export("old", "1.0"))
    meta = revision.meta
    assert meta["harvest_id"].startswith("legacy-")
    assert meta["count_basis"] == "legacy" and meta["version_basis"] == "legacy"
    # Stored as complete with nothing counted (Issues M7): exported as null (D3).
    assert meta["complete"] is None
    assert meta["complete_reason"]["code"] == "legacy-unverified"
    assert [p.captured_at for p in revision.pages] == [None, None]
    assert [p.body for p in revision.pages] == ["first", "second"]
    assert validate(revision) == []
    assert "docsforge_contract" not in Path(legacy.entry("old", "1.0")["file"]).read_text(
        "utf-8"), "export does not touch the stored file"


def test_upgrade_store_rewrites_contract_0_files_once(legacy):
    exported = legacy.export("old", "1.0")
    assert legacy.upgrade() == [("old", "1.0", "upgraded, 2 pages")]
    path = Path(legacy.entry("old", "1.0")["file"])
    assert path.read_bytes().decode("utf-8") == exported, "the file becomes its export"
    assert legacy.upgrade() == [], "idempotent"
    assert (legacy.root / "index.json.bak").exists()
    entry = legacy.entry("old", "1.0")
    assert entry["complete"] is None and entry["harvest_id"].startswith("legacy-")
    assert [p["title"] for p in legacy.pages("old", "1.0")] == ["One", "Two"]


def test_a_rebuilt_index_restores_every_v1_entry(files):
    _store_pages(files)
    _store_pages(files, tech="other", version="1", expected=5)
    files.index_path.write_text("{ not json", encoding="utf-8")
    acme, other = files.entry("acme-sdk", "2.4"), files.entry("other", "1")
    assert acme["complete"] is True and acme["expected"] == 2
    assert other["complete"] is False and other["harvest_id"].startswith("hv-")


# ── export ───────────────────────────────────────────────────────
def test_export_by_harvest_id(files):
    entry = _store_pages(files)
    assert files.export("acme-sdk", "2.4", entry["harvest_id"]).startswith("---\n")
    with pytest.raises(StoreError, match="not kept"):
        files.export("acme-sdk", "2.4", "hv-20260101T000000Z-deadbeef")


def test_the_export_command_writes_the_exact_bytes(files, tmp_path, monkeypatch):
    from docsforge.core import engine
    entry = _store_pages(files)
    monkeypatch.setenv("DOCSFORGE_KB_ROOT", str(files.root))
    monkeypatch.delenv("DOCSFORGE_DB", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "acme.md"
    assert engine.main(["--export", f"acme-sdk@2.4#{entry['harvest_id']}", "-o", str(out)]) == 0
    assert out.read_bytes() == files.export("acme-sdk", "2.4").encode("utf-8")
    assert engine.main(["--export", "nothing-here"]) == 1


def test_the_local_web_app_serves_the_export(files):
    from starlette.testclient import TestClient
    from docsforge.server import app

    _store_pages(files)
    ft.reset_store(files)
    try:
        with TestClient(app.app) as client:
            r = client.get("/api/library/acme-sdk/2.4/export")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/markdown")
            assert "attachment" in r.headers["content-disposition"]
            assert r.content == files.export("acme-sdk", "2.4").encode("utf-8")
            assert client.get("/api/library/acme-sdk/9.9/export").status_code == 404
    finally:
        ft.reset_store(None)


# ── what tool responses say ─────────────────────────────────────
def test_every_read_names_the_revision_and_a_full_read_warns(files):
    _store_pages(files, expected=5)                 # 2 of 5: incomplete
    ft.reset_store(files)
    try:
        full = ft.tool_read_knowledge_base("acme-sdk")
        part = ft.tool_read_knowledge_base("acme-sdk", section="Client")
    finally:
        ft.reset_store(None)
    entry = files.entry("acme-sdk", "2.4")
    for out in (full, part):
        assert out.startswith(f'<!-- docsforge: corpus="acme-sdk" version="2.4" '
                              f'harvest_id="{entry["harvest_id"]}" complete=false '
                              f'expected=5 stored=2 -->')
    assert "INCOMPLETE" in full, "a full read carries the coverage note too (O1)"


def test_a_read_of_an_unrecorded_complete_is_unknown_not_complete():
    assert "null" in ft._identity_line({"technology": "t", "version": "1"})
    assert "COVERAGE UNKNOWN" in ft._coverage_note(None)


def test_the_planning_decision_points_at_revisions():
    from docsforge.core import federation

    a = federation.Corpus(url="https://acme.example/docs/", entry=True, kind="api",
                          version="2.4", complete=True, harvest_id="hv-a")
    b = federation.Corpus(url="https://sdk.acme.example/", kind="sdk",
                          complete=False, harvest_id="hv-b")
    chosen = type("Chosen", (), {"intent": "resolve-import"})()
    block = ft._decision_block("acme-sdk", chosen, [a, b], False, "the 'sdk' corpus is INCOMPLETE")
    decision = json.loads("\n".join(block[2:-1]))
    assert decision["contract"] == 1 and decision["usable_for_planning"] is False
    assert [j["harvest_id"] for j in decision["judged"]] == ["hv-a", "hv-b"]
    assert [j["complete"] for j in decision["judged"]] == [True, False]
    assert decision["judged"][0]["corpus"] == "acme-sdk"


@pytest.mark.parametrize("stats, strategy, basis", [
    ({"listed": 10, "index": "sitemap", "found_by_links": 0, "discovered": 10}, "crawl", "sitemap"),
    ({"listed": 10, "index": "sitemap", "found_by_links": 3, "discovered": 13}, "crawl", "sitemap+links"),
    ({"listed": 10, "index": "llms.txt", "found_by_links": 0}, "crawl", "manifest"),
    ({"frontier_drained": True}, "crawl", "crawl-frontier"),
    ({"expected": 5, "failed_urls": []}, "llms.txt (md manifest)", "manifest"),
    ({}, "github", "repository"),
    ({}, "openapi", "api-spec"),
    ({"expected": 1, "failed_urls": []}, "llms-full.txt", "dump-file"),
    ({}, "html", "none"),
])
def test_count_basis_is_read_from_what_the_harvest_measured(stats, strategy, basis):
    opts = type("O", (), {"scope": "section", "topic": "", "max_pages": 0})()
    facts = ft._contract_facts(stats, strategy, url="https://x.dev/docs/", opts=opts,
                               docs=[], label="2026-11-18")
    assert facts["count_basis"] == basis
    assert facts["version_basis"] == "harvest-date"


def test_exports_match_the_published_schema(files, legacy):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(contract.__file__).parent / "contract_v1.schema.json")
                        .read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["required"] == list(contract.FIELDS)
    _store_pages(files, run=RunContext("run-x", job="acme-sdk-1"))               # complete
    _store_pages(files, tech="partial", expected=120,                           # partial
                 facts={"count_basis": "manifest",
                        "reason": {"code": "page-limit", "text": "stopped at the 2-page limit"}})
    for tech, version in (("acme-sdk", "2.4"), ("partial", "2.4"), ("old", "1.0")):
        meta = parse_revision(files.export(tech, version)).meta
        jsonschema.validate(meta, schema)


# ── Postgres ─────────────────────────────────────────────────────
def _pg_or_skip() -> PostgresStore:
    if not DSN:
        pytest.skip("set DOCSFORGE_TEST_DB to exercise the Postgres backend")
    store = PostgresStore(DSN)
    if not store.available():
        pytest.skip(f"database not reachable: {store.location}")
    return store


_VARIES = re.compile(r'^(harvest_id|run_id|store|started_at|harvested_at): .*$', re.M)


def test_both_stores_export_the_same_document(files):
    db = _pg_or_skip()
    try:
        _store_pages(files, tech="contract-same")
        _store_pages(db, tech="contract-same")
        a, b = files.export("contract-same", "2.4"), db.export("contract-same", "2.4")
        assert _VARIES.sub("", a) == _VARIES.sub("", b), \
            "only the ids, the times and the store's name may differ"
        assert validate(parse_revision(b)) == []
        assert parse_revision(b).meta["store"] == "postgres"
    finally:
        db.delete("contract-same")


def test_a_postgres_row_from_before_contract_v1_exports_as_legacy():
    db = _pg_or_skip()
    try:
        _store_pages(db, tech="contract-legacy")
        with db._borrow() as cx:
            cx.execute("update doc_version set meta = null, expected = null "
                       "where technology_id = (select id from technology where name = %s)",
                       ("contract-legacy",))
            cx.execute("update page set captured_at = null, sha256 = null "
                       "where version_id in (select v.id from doc_version v join technology t "
                       "on t.id = v.technology_id where t.name = %s)", ("contract-legacy",))
            cx.commit()
        revision = parse_revision(db.export("contract-legacy", "2.4"))
        assert revision.meta["count_basis"] == "legacy"
        assert revision.meta["complete"] is None
        assert validate(revision) == []
    finally:
        db.delete("contract-legacy")


def test_a_postgres_body_changed_after_storing_fails_validation():
    db = _pg_or_skip()
    try:
        _store_pages(db, tech="contract-tamper")
        with db._borrow() as cx:
            cx.execute("update page set content = replace(content, 'session', 'sessiom') "
                       "where version_id in (select v.id from doc_version v join technology t "
                       "on t.id = v.technology_id where t.name = %s)", ("contract-tamper",))
            cx.commit()
        problems = validate(parse_revision(db.export("contract-tamper", "2.4")))
        assert any("sha256" in p for p in problems)
    finally:
        db.delete("contract-tamper")
