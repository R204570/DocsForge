"""
The test lab (`docsforge/lab/`): the signed-in panel in the local web app.

What it must never get wrong, each pinned here without the network:

* it answers this machine only, and only someone signed in, and an admin's
  pages only an admin -- a tester's dashboard is a different one;
* a state change needs the panel's own header, so another site cannot make
  a signed-in browser start tests or change verdicts;
* passwords and session tokens are never stored as themselves;
* a test records every tool it ran and what each returned, and a finished
  test takes a verdict, one per person, that an admin can move along;
* a harvest test runs in a process of its own, against a store of its own,
  and its trace, pages and measures come back;
* every chat turn and every tool call is recorded, tagged with where it came
  from -- and nothing is recorded before the server has started.
"""

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from docsforge import lab
from docsforge.core.resolver import Candidate, Resolution
from docsforge.lab import activity, auth, db, quality, records, routes, runner, stats
from docsforge.providers.base import text, tool_end, tool_start
from docsforge.server import app
from docsforge.store.kb_store import FileStore
from docsforge.tools import forge_tools, tracing

H = {"x-docsforge-lab": "1"}
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def lab_env(tmp_path, monkeypatch):
    """A fresh lab database, recording on, workers never started: a test runs
    a lab test on its own thread with `RUNNER.run`, so what it asserts on is
    finished by the time it looks."""
    monkeypatch.setenv("DOCSFORGE_LAB_DIR", str(tmp_path / "lab"))
    monkeypatch.delenv("DOCSFORGE_LAB", raising=False)
    monkeypatch.delenv("DOCSFORGE_LAB_RECORD", raising=False)
    monkeypatch.delenv("DOCSFORGE_LAB_REMOTE", raising=False)
    db.forget_paths()
    monkeypatch.setattr(lab, "ACTIVE", True)
    activity.install()
    queued = []
    monkeypatch.setattr(runner.RUNNER, "enqueue", lambda tid, kind: queued.append(tid))
    monkeypatch.setattr(quality, "lands_on", lambda address: "")
    yield queued
    tracing.remove_listener(activity.record_trace)
    db.forget_paths()


def client(host=LOCAL):
    return TestClient(app.app, client=host)


def set_up(c):
    r = c.post("/api/lab/setup", headers=H, json={
        "admin": {"username": "raj", "password": "adminpass1"},
        "tester": {"username": "tess", "password": "testerpass1"}})
    assert r.status_code == 200, r.text
    return r


def signed_in(role="admin"):
    c = client()
    if auth.needs_setup():
        set_up(c)
    name, pw = ("raj", "adminpass1") if role == "admin" else ("tess", "testerpass1")
    r = c.post("/api/lab/login", headers=H, json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return c


# ── the gates ───────────────────────────────────────────────
def test_the_lab_answers_this_machine_only():
    far = client(("192.168.1.20", 50000))
    assert far.get("/lab").status_code == 404
    assert far.get("/api/lab/state").status_code == 404
    assert client().get("/api/lab/state").status_code == 200


def test_a_network_you_own_can_be_let_in(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_LAB_REMOTE", "1")
    assert client(("192.168.1.20", 50000)).get("/api/lab/state").status_code == 200


def test_the_public_server_has_no_lab():
    from docsforge.server import mcp_server
    public = mcp_server.build_http_app() if hasattr(mcp_server, "build_http_app") else None
    paths = {getattr(r, "path", "") for r in getattr(public, "routes", [])} if public else set()
    assert not any(p.startswith(("/lab", "/api/lab")) for p in paths)
    assert "docsforge.lab" not in Path(mcp_server.__file__).read_text(encoding="utf-8")


def test_the_page_is_not_for_indexing_or_framing():
    r = client().get("/lab")
    assert r.status_code == 200
    assert "noindex" in r.headers["x-robots-tag"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert client().get("/lab/assets/lab.js").status_code == 200
    assert client().get("/lab/assets/lab.db").status_code == 404


def test_first_run_makes_an_admin_and_a_tester_once():
    c = client()
    assert c.get("/api/lab/state").json()["setup"] is True
    r = set_up(c)
    assert r.json()["user"]["role"] == "admin"
    assert {u["username"]: u["role"] for u in auth.users()} == {"raj": "admin", "tess": "tester"}
    again = c.post("/api/lab/setup", headers=H, json={
        "admin": {"username": "x1", "password": "adminpass1"},
        "tester": {"username": "x2", "password": "adminpass1"}})
    assert again.status_code == 409


def test_setup_refuses_weak_or_clashing_accounts():
    c = client()
    short = c.post("/api/lab/setup", headers=H, json={
        "admin": {"username": "raj", "password": "short"},
        "tester": {"username": "tess", "password": "testerpass1"}})
    assert short.status_code == 400
    same = c.post("/api/lab/setup", headers=H, json={
        "admin": {"username": "raj", "password": "adminpass1"},
        "tester": {"username": "RAJ", "password": "testerpass1"}})
    assert same.status_code == 400
    assert auth.needs_setup()


def test_nothing_changes_without_the_panels_header():
    c = client()
    assert c.post("/api/lab/setup", json={}).status_code == 403
    c = signed_in()
    assert c.post("/api/lab/tests", json={"kind": "resolve", "input": {"name": "zod"}}).status_code == 403
    cross = c.post("/api/lab/tests", headers={**H, "origin": "https://evil.example"},
                   json={"kind": "resolve", "input": {"name": "zod"}})
    assert cross.status_code == 403


def test_signed_out_is_refused_and_a_tester_is_kept_off_admin_pages():
    set_up(client())
    assert client().get("/api/lab/me").status_code == 401
    tess = signed_in("tester")
    assert tess.get("/api/lab/me").json()["user"]["role"] == "tester"
    for path in ("/api/lab/overview", "/api/lab/setup-info", "/api/lab/users", "/api/lab/logs",
                 "/api/lab/feedback/export", "/api/lab/export/cases"):
        assert tess.get(path).status_code == 403, path
    raj = signed_in("admin")
    assert raj.get("/api/lab/overview").status_code == 200


def test_signing_out_ends_the_session():
    c = signed_in()
    assert c.post("/api/lab/logout", headers=H).status_code == 200
    assert c.get("/api/lab/me").status_code == 401


def test_wrong_passwords_lock_the_name_for_a_while():
    set_up(client())
    c = client()
    for _ in range(auth.LOCK_AFTER):
        assert c.post("/api/lab/login", headers=H,
                      json={"username": "tess", "password": "wrong-one"}).status_code == 401
    locked = c.post("/api/lab/login", headers=H, json={"username": "tess", "password": "testerpass1"})
    assert locked.status_code == 429


def test_neither_passwords_nor_session_tokens_are_stored_as_themselves():
    c = signed_in()
    token = c.cookies.get(auth.COOKIE)
    with db.read() as cx:
        dump = "\n".join(cx.iterdump())
    assert "adminpass1" not in dump and "testerpass1" not in dump
    assert token and token not in dump
    assert "scrypt$" in dump


def test_the_last_admin_cannot_be_demoted_or_disabled():
    c = signed_in()
    me = c.get("/api/lab/me").json()["user"]
    assert c.patch(f"/api/lab/users/{me['id']}", headers=H, json={"role": "tester"}).status_code == 400
    assert c.patch(f"/api/lab/users/{me['id']}", headers=H, json={"disabled": True}).status_code == 400
    made = c.post("/api/lab/users", headers=H,
                  json={"username": "second", "password": "secondpass", "role": "admin"})
    assert made.status_code == 200
    assert c.patch(f"/api/lab/users/{me['id']}", headers=H, json={"role": "tester"}).status_code == 200


def test_a_reset_password_ends_that_accounts_sessions():
    tess = signed_in("tester")
    raj = signed_in("admin")
    uid = next(u["id"] for u in auth.users() if u["username"] == "tess")
    assert raj.patch(f"/api/lab/users/{uid}", headers=H, json={"password": "newpass123"}).status_code == 200
    assert tess.get("/api/lab/me").status_code == 401


# ── a resolution test ───────────────────────────────────────
def _resolution(url="https://zod.dev/"):
    best = Candidate(url, "domain:dev", 0.97, "names zod 392 times", verified=True,
                     reason="identified by own-domain", signals=["own-domain"])
    return Resolution(name="zod", candidates=[best], best=best, resolved_via="domain",
                      note="zod's own domain")


@pytest.fixture
def offline_tools(monkeypatch):
    """fetch_docs and detect_source_type, answered from here."""
    page = "<!-- source: https://zod.dev/ | type: html | scraped: now -->\n# Zod\n\n" + \
           "Zod is a TypeScript-first schema library. " * 30 + "\n\n```ts\nconst a = z.string();\n```\n"
    monkeypatch.setattr(forge_tools.BY_NAME["fetch_docs"], "fn", lambda url, **k: page)
    monkeypatch.setattr(forge_tools.BY_NAME["detect_source_type"], "fn", lambda url: "html")
    return page


def test_a_resolution_test_records_what_ran_and_checks_itself(monkeypatch, offline_tools, lab_env):
    from docsforge.core import resolver
    calls = []

    def fake_resolve(name, ecosystem="", use_memory=True, language="", **kw):
        calls.append((name, use_memory, language))
        return _resolution()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    c = signed_in("tester")
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {
        "name": "zod", "expected": "https://zod.dev, docs.example.com/x/"}}).json()["id"]
    assert lab_env == [tid], "a new test is queued"
    runner.RUNNER.run(tid)

    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["state"] == "done"
    assert calls == [("zod", False, "")], "the cache is off unless asked for"
    assert t["result"]["best_url"] == "https://zod.dev/"
    assert t["auto"]["pass"] is True
    assert t["input"]["expected"] == ["zod.dev", "docs.example.com/x"]
    tools = [s["tool"] for s in t["steps"]]
    assert tools == ["find_docs", "fetch_docs"]
    find, fetch = t["steps"]
    assert "Best: https://zod.dev/" in find["output"], "the words find_docs gives a model"
    assert fetch["output"] == offline_tools, "exactly what the tool returned"
    assert fetch["trace_id"] and fetch["events"], "and the trace underneath it"
    page = t["result"]["page"]
    assert page["mentions"] > 0 and page["metrics"]["fences"] == 1

    # Both calls are in the record of what ran, as lab calls of this test.
    calls_seen, _ = activity.calls(source="lab")
    assert {c_["tool"] for c_ in calls_seen} == {"find_docs", "fetch_docs"}
    assert all(c_["test_id"] == tid for c_ in calls_seen)


def test_a_wrong_answer_fails_the_automatic_check(monkeypatch, offline_tools):
    from docsforge.core import resolver
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: _resolution("https://zod.example.org/"))
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {
        "name": "zod", "expected": "zod.dev", "fetch_page": False}}).json()["id"]
    runner.RUNNER.run(tid)
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["auto"]["pass"] is False
    assert "expected zod.dev" in t["auto"]["reason"]
    assert [s["tool"] for s in t["steps"]] == ["find_docs"], "no page fetched when not asked"


def test_a_failing_resolver_fails_the_test_and_says_why(monkeypatch):
    from docsforge.core import resolver

    def boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(resolver, "resolve", boom)
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {"name": "zod"}}).json()["id"]
    runner.RUNNER.run(tid)
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["state"] == "failed" and "registry down" in t["error"]
    assert t["steps"][0]["state"] == "failed"


def test_a_content_test_measures_the_page(offline_tools):
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "fetch", "input": {
        "url": "https://zod.dev/", "name": "zod"}}).json()["id"]
    runner.RUNNER.run(tid)
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert [s["tool"] for s in t["steps"]] == ["detect_source_type", "fetch_docs"]
    assert t["result"]["source_type"] == "html"
    assert t["auto"]["pass"] is True
    assert t["result"]["flags"] == []


@pytest.mark.parametrize("kind,raw,says", [
    ("resolve", {"name": ""}, "needs a name"),
    ("resolve", {"name": "x", "language": "klingon"}, "Unknown language"),
    ("resolve", {"name": "x", "ecosystem": "maven"}, "Ecosystem"),
    ("fetch", {"url": "ftp://x.org/"}, "not an http"),
    ("harvest", {}, "a name or a URL"),
    ("harvest", {"name": "x", "max_pages": 99999}, "between"),
    ("nope", {}, "Unknown test kind"),
])
def test_a_test_that_cannot_run_is_refused_with_the_reason(kind, raw, says):
    c = signed_in()
    r = c.post("/api/lab/tests", headers=H, json={"kind": kind, "input": raw})
    assert r.status_code == 400 and says in r.json()["detail"]


def test_a_queued_test_can_be_cancelled_and_a_test_rerun(lab_env):
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {"name": "zod"}}).json()["id"]
    assert c.post(f"/api/lab/tests/{tid}/cancel", headers=H).json()["outcome"] == "cancelled"
    runner.RUNNER.run(tid)                       # a cancelled test is not run
    assert c.get(f"/api/lab/tests/{tid}").json()["state"] == "cancelled"
    again = c.post(f"/api/lab/tests/{tid}/rerun", headers=H).json()["id"]
    t = c.get(f"/api/lab/tests/{again}").json()
    assert t["parent_id"] == tid and t["state"] == "queued" and t["input"]["name"] == "zod"


def test_a_restart_fails_what_was_running_and_requeues_what_waited(lab_env):
    c = signed_in()
    running = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {"name": "a"}}).json()["id"]
    waiting = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {"name": "b"}}).json()["id"]
    db.execute("UPDATE tests SET state = 'running' WHERE id = ?", (running,))
    lab_env.clear()
    runner.recover()
    assert c.get(f"/api/lab/tests/{running}").json()["state"] == "failed"
    assert lab_env == [waiting]


# ── a harvest test, through a stand-in worker ───────────────
FAKE_WORKER = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    MARK = "@@lab "
    where = Path(sys.argv[1])
    spec = json.loads((where / "spec.json").read_text(encoding="utf-8"))
    env = {k: os.environ.get(k) for k in ("DOCSFORGE_DB", "DATABASE_URL", "DOCSFORGE_KB_ROOT",
                                          "DOCSFORGE_HARVEST_DEADLINE", "DOCSFORGE_LAB")}
    (where / "env.json").write_text(json.dumps(env))
    if spec["args"].get("name") == "slow":
        time.sleep(60)
    print("engine chatter, kept as the log", flush=True)
    ev = {"id": "t-1", "parent_id": None, "type": "stage", "name": spec["tool"], "state": "completed",
          "message": "harvested", "counters": {"pages": 12}}
    print(MARK + json.dumps({"type": "event", "trace_id": "t", "event": ev}), flush=True)
    print(MARK + json.dumps({"type": "output", "ok": True, "text": "Harvested **x** - 12 pages.",
                             "trace_id": "t", "omitted": 0}), flush=True)
    pages = [{"technology": "x", "version": "1", "ordinal": i, "title": f"P{i}", "url": f"https://x.dev/{i}",
              "chars": 900, "fences": 1, "collapsed_fences": 0, "relative_links": 0, "chrome_lines": 0,
              "permalink_marks": 0, "headings": 2, "flags": []} for i in range(1, 13)]
    (where / "result.json").write_text(json.dumps({
        "entries": [{"technology": "x", "version": "1", "source": "https://x.dev/", "strategy": "sitemap",
                     "pages": 12, "characters": 10800, "complete": True, "expected": 12}],
        "pages": pages, "totals": {"pages": 12, "clean_pages": 12, "thin_pages": 0}}))
    print(MARK + json.dumps({"type": "done"}), flush=True)
''')


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(runner, "harvest_command", lambda where: [sys.executable, str(script), str(where)])
    monkeypatch.setenv("DOCSFORGE_DB", "postgresql://must-not-reach-the-worker")
    return script


def test_a_harvest_test_runs_apart_and_comes_back_measured(fake_worker):
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "harvest", "input": {
        "url": "https://x.dev/docs/", "name": "x", "max_pages": 12, "expected": "x.dev"}}).json()["id"]
    runner.RUNNER.run(tid)
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["state"] == "done", t["error"]
    step = t["steps"][0]
    assert step["tool"] == "harvest_docs" and step["args"]["max_pages"] == 12
    assert step["output"] == "Harvested **x** - 12 pages."
    assert step["events"][0]["counters"] == {"pages": 12}, "the worker's trace came back"
    assert t["auto"]["pass"] is True
    assert len(t["result"]["pages"]) == 12
    assert "12 pages" in t["result"]["headline"]
    assert "engine chatter" in "\n".join(t["result"]["log_tail"])

    env = json.loads((runner.run_dir(tid) / "env.json").read_text())
    assert env["DOCSFORGE_DB"] == "" and env["DATABASE_URL"] == "", "never the real store"
    assert Path(env["DOCSFORGE_KB_ROOT"]).parent == runner.run_dir(tid)
    assert float(env["DOCSFORGE_HARVEST_DEADLINE"]) > 3600 and env["DOCSFORGE_LAB"] == "0"

    calls, _ = activity.calls(tool="harvest_docs")
    assert calls and calls[0]["test_id"] == tid and calls[0]["source"] == "lab"


def test_a_harvest_from_the_wrong_place_fails_its_check(fake_worker):
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "harvest", "input": {
        "name": "x", "expected": "docs.other.io"}}).json()["id"]
    runner.RUNNER.run(tid)
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["steps"][0]["tool"] == "learn_technology"
    assert t["auto"]["pass"] is False and "docs.other.io" in t["auto"]["reason"]


def test_a_running_harvest_can_be_stopped(fake_worker):
    import threading
    c = signed_in()
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "harvest", "input": {"name": "slow"}}).json()["id"]
    worker = threading.Thread(target=runner.RUNNER.run, args=(tid,))
    worker.start()
    deadline = time.time() + 20
    while time.time() < deadline and runner.RUNNER.cancel(tid) != "stopping":
        time.sleep(0.2)
    worker.join(30)
    assert not worker.is_alive()
    t = c.get(f"/api/lab/tests/{tid}").json()
    assert t["state"] == "cancelled"


def test_the_real_worker_refuses_to_run_against_a_configured_store(tmp_path, monkeypatch, capsys):
    from docsforge.lab import harvest_worker
    (tmp_path / "spec.json").write_text(json.dumps({"tool": "harvest_docs", "args": {"url": "https://x.dev/"}}))
    monkeypatch.setenv("DOCSFORGE_DB", "postgresql://real")
    monkeypatch.setenv("DOCSFORGE_KB_ROOT", str(tmp_path / "kb"))
    assert harvest_worker.main([str(tmp_path)]) == 2
    assert "store of its own" in capsys.readouterr().out


def test_the_worker_measures_every_stored_page(tmp_path):
    from docsforge.lab import harvest_worker
    FileStore(tmp_path).save("x", "1", "https://x.dev/", "sitemap", [
        ("Intro", "https://x.dev/a", "# Intro\n\n" + "words " * 200),
        ("Stub", "https://x.dev/b", "Copy\n\ntiny"),
    ], complete=True, expected=2)
    summary = harvest_worker.summarise(tmp_path)
    assert [e["pages"] for e in summary["entries"]] == [2]
    flags = {p["title"]: p["flags"] for p in summary["pages"]}
    assert flags["Intro"] == [] and "thin" in flags["Stub"] and "UI chrome" in flags["Stub"]
    assert summary["totals"]["thin_pages"] == 1


# ── batches ─────────────────────────────────────────────────
def test_a_batch_line_keeps_a_urls_fragment_and_drops_comments():
    items = runner.parse_batch("fetch", "https://x.dev/a#part | x   # a comment\n\n# only a comment\n")
    assert items == [{"url": "https://x.dev/a#part", "name": "x", "js": False}]
    resolved = runner.parse_batch("resolve", "zod | javascript | zod.dev, x.io/y", {"fetch_page": False})
    assert resolved[0]["language"] == "javascript"
    assert resolved[0]["expected"] == ["zod.dev", "x.io/y"] and resolved[0]["fetch_page"] is False
    harvest = runner.parse_batch("harvest", "https://x.dev/ | | 15\nhtmx")
    assert harvest[0]["url"] == "https://x.dev/" and harvest[0]["max_pages"] == 15
    assert harvest[1]["name"] == "htmx"
    with pytest.raises(runner.LabInputError):
        runner.parse_batch("resolve", "# nothing\n")


def test_a_batch_queues_every_line_and_reports_its_progress(lab_env):
    c = signed_in()
    r = c.post("/api/lab/batches", headers=H, json={"kind": "resolve", "text": "zod\ntokio\npinia"})
    assert r.status_code == 200
    batch = r.json()
    assert len(batch["tests"]) == 3 and lab_env == batch["tests"]
    b = c.get(f"/api/lab/batches/{batch['id']}").json()
    assert b["total"] == 3 and b["finished"] == 0
    assert [t["input"]["name"] for t in b["tests"]] == ["zod", "tokio", "pinia"]


# ── verdicts ────────────────────────────────────────────────
def _finished_test(c, monkeypatch):
    from docsforge.core import resolver
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: _resolution())
    tid = c.post("/api/lab/tests", headers=H, json={"kind": "resolve", "input": {
        "name": "zod", "fetch_page": False}}).json()["id"]
    runner.RUNNER.run(tid)
    return tid


def test_a_verdict_is_one_per_person_and_a_change_reopens_it(monkeypatch):
    tess = signed_in("tester")
    tid = _finished_test(tess, monkeypatch)
    assert tess.get("/api/lab/tests?review=1").json()["total"] == 1
    body = {"target_kind": "test", "target_id": str(tid), "verdict": "wrong",
            "aspects": {"url": "Wrong project"}, "issues": ["wrong project"],
            "expected": "https://zod.dev/api", "notes": "the API page", "rating": 2}
    first = tess.post("/api/lab/feedback", headers=H, json=body).json()
    assert first["status"] == "open" and first["issues"] == ["wrong project"]
    assert tess.get("/api/lab/tests?review=1").json()["total"] == 0, "judged tests leave the queue"

    raj = signed_in("admin")
    assert raj.patch(f"/api/lab/feedback/{first['id']}", headers=H,
                     json={"status": "fixed", "resolution": "resolver rule R12"}).json()["status"] == "fixed"
    assert tess.patch(f"/api/lab/feedback/{first['id']}", headers=H, json={"status": "fixed"}).status_code == 403

    again = tess.post("/api/lab/feedback", headers=H, json={**body, "verdict": "partial"}).json()
    assert again["id"] == first["id"] and again["status"] == "open"
    mine = tess.get("/api/lab/feedback").json()["feedback"]
    assert len(mine) == 1 and mine[0]["context"]["subject"] == "zod"


@pytest.mark.parametrize("body,says", [
    ({"target_kind": "test", "target_id": "999", "verdict": "correct"}, "No test"),
    ({"target_kind": "page", "target_id": "1", "verdict": "correct"}, "one of"),
    ({"target_kind": "test", "target_id": "{tid}", "verdict": "great"}, "verdict is one of"),
    ({"target_kind": "test", "target_id": "{tid}", "verdict": "correct", "rating": 9}, "1 to 5"),
])
def test_a_verdict_that_makes_no_sense_is_refused(monkeypatch, body, says):
    c = signed_in()
    tid = _finished_test(c, monkeypatch)
    body = {k: (v.format(tid=tid) if isinstance(v, str) else v) for k, v in body.items()}
    r = c.post("/api/lab/feedback", headers=H, json=body)
    assert r.status_code == 400 and says in r.json()["detail"]


def test_the_exports_are_what_someone_fixing_it_works_from(monkeypatch):
    c = signed_in()
    tid = _finished_test(c, monkeypatch)
    c.post("/api/lab/feedback", headers=H, json={
        "target_kind": "test", "target_id": str(tid), "verdict": "correct",
        "issues": ["slow"], "notes": "fine"})
    md = c.get("/api/lab/feedback/export?format=md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert "`zod`" in md.text and "**correct**" in md.text and "Issues: slow" in md.text
    data = json.loads(c.get("/api/lab/feedback/export?format=json").text)
    assert data["feedback"][0]["context"]["kind"] == "resolve"
    cases = c.get("/api/lab/export/cases").text
    assert "('zod', '', ('zod.dev',))," in cases


def test_a_corrected_wrong_answer_exports_as_the_right_one(monkeypatch):
    c = signed_in()
    tid = _finished_test(c, monkeypatch)
    c.post("/api/lab/feedback", headers=H, json={
        "target_kind": "test", "target_id": str(tid), "verdict": "wrong",
        "expected": "https://www.zod-docs.io/"})
    assert "('zod', '', ('zod-docs.io',))" in records.export_cases()


# ── what the AI ran ─────────────────────────────────────────
class _Provider:
    name = "fake"

    def model(self):
        return "fake-1"

    def stream(self, *, system, history, tools, run_tool):
        yield tool_start("list_knowledge_base", {})
        yield tool_end("list_knowledge_base", run_tool("list_knowledge_base", {}))
        yield text("Nothing is stored yet.")


def test_a_chat_turn_is_recorded_with_its_tool_calls(monkeypatch):
    monkeypatch.setattr(app.providers, "get", lambda name: _Provider())
    list(app.chat_stream([{"role": "user", "content": "what is stored?"}], "fake"))
    turns, total = activity.turns()
    assert total == 1
    turn = activity.turn(turns[0]["id"])
    assert turn["prompt"] == "what is stored?"
    assert turn["answer"] == "Nothing is stored yet." and turn["outcome"] == "done"
    assert turn["model"] == "fake-1" and turn["tools"] == ["list_knowledge_base:ok"]
    [call] = turn["calls"]
    assert call["tool"] == "list_knowledge_base" and call["source"] == "chat"
    assert call["output"] and call["ok"] is True

    c = signed_in("tester")
    listed = c.get("/api/lab/activity/tools?source=chat").json()
    assert listed["total"] == 1
    detail = c.get(f"/api/lab/activity/tools/{call['trace_id']}").json()
    assert detail["output"] == call["output"]
    fb = c.post("/api/lab/feedback", headers=H, json={
        "target_kind": "turn", "target_id": turn["id"], "verdict": "correct"})
    assert fb.status_code == 200


def test_nothing_is_recorded_until_the_server_has_started(monkeypatch):
    monkeypatch.setattr(lab, "ACTIVE", False)
    monkeypatch.setattr(app.providers, "get", lambda name: _Provider())
    list(app.chat_stream([{"role": "user", "content": "hi"}], "fake"))
    assert not db.path().exists() or activity.turns()[1] == 0


def test_recording_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_LAB_RECORD", "0")
    monkeypatch.setattr(app.providers, "get", lambda name: _Provider())
    list(app.chat_stream([{"role": "user", "content": "hi"}], "fake"))
    assert activity.turns()[1] == 0 and activity.calls()[1] == 0


def test_a_trace_listener_that_fails_breaks_nothing():
    seen = []

    def bad(trace):
        raise RuntimeError("listener bug")

    tracing.on_close(bad)
    tracing.on_close(lambda t: seen.append(t.id))
    try:
        ctx = tracing.start("probe")
        ctx.close()
        ctx.close()
    finally:
        tracing.remove_listener(bad)
        tracing._CLOSE_LISTENERS[:] = [f for f in tracing._CLOSE_LISTENERS if f is activity.record_trace]
    assert seen == [ctx.trace_id], "once per trace, whatever else is listening"


# ── the numbers ─────────────────────────────────────────────
def test_accuracy_is_counted_from_verdicts_with_its_denominator(monkeypatch):
    c = signed_in()
    good = _finished_test(c, monkeypatch)
    bad = _finished_test(c, monkeypatch)
    _finished_test(c, monkeypatch)                          # not judged
    for tid, verdict in ((good, "correct"), (bad, "wrong")):
        c.post("/api/lab/feedback", headers=H, json={
            "target_kind": "test", "target_id": str(tid), "verdict": verdict})
    o = c.get("/api/lab/overview").json()
    r = o["tests"]["kinds"]["resolve"]
    assert r["accuracy"] == {"good": 1, "total": 2, "percent": 50.0}
    assert r["total"] == 3 and o["tests"]["pending_review"] == 1
    assert o["feedback"]["status"] == {"open": 2}
    assert len(o["activity"]["daily"]) == 14
    assert o["activity"]["daily"][-1]["day"] == time.strftime("%Y-%m-%d"), "today is the last column"
    assert o["activity"]["daily"][-1]["tests"] == 3


def test_setup_shows_secrets_as_set_never_as_themselves(monkeypatch):
    monkeypatch.setenv("DOCSFORGE_MCP_TOKEN", "super-secret-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    c = signed_in()
    body = c.get("/api/lab/setup-info").text
    assert "super-secret-token" not in body and "ghp_secret" not in body
    info = json.loads(body)
    token = next(e for e in info["env"] if e["key"] == "DOCSFORGE_MCP_TOKEN")
    assert token["set"] is True and token["value"] == "set" and token["about"]
    assert all(e["about"] for e in info["env"]), "every setting says what it does"


def test_setup_explains_a_missing_playwright_and_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(stats, "playwright_state", lambda: {"state": "missing"})
    monkeypatch.setenv("DOCSFORGE_REASONING", "off")
    features = {f["label"]: f for f in signed_in().get("/api/lab/setup-info").json()["features"]}
    js = features["JavaScript rendering"]
    assert js["state"] == "not installed" and js["ok"] is None, "a missing capability, not a choice"
    assert sys.executable in js["change"] and "playwright install chromium" in js["change"]
    reasoning = features["Bounded reasoning"]
    assert reasoning["state"] == "off" and reasoning["ok"] is False, "off by choice, not a fault"
    assert "Nothing is spent" in reasoning["meaning"]


def test_playwright_is_ready_only_with_a_browser_downloaded(tmp_path, monkeypatch):
    import importlib.util as iu
    monkeypatch.setattr(iu, "find_spec", lambda name, *a: object() if name == "playwright" else None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert stats.playwright_state()["state"] == "no-browser"
    (tmp_path / "chromium-1234").mkdir()
    assert stats.playwright_state()["state"] == "ready"


def test_the_log_view_can_leave_requests_out(tmp_path, monkeypatch):
    log = tmp_path / "docsforge.log"
    log.write_text("\n".join(json.dumps(x) for x in [
        {"ts": 1, "kind": "request", "path": "/"},
        {"ts": 2, "kind": "tool_call", "name": "find_docs", "ok": False},
        {"ts": 3, "kind": "lab", "event": "login"},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr("docsforge.tools.applog.LOG_FILE", str(log))
    assert [x["kind"] for x in stats.log_lines("-request")] == ["lab", "tool_call"]
    assert [x["kind"] for x in stats.log_lines("request")] == ["request"]
    assert stats.log_summary()["errors"] == 1


def test_the_labs_own_polling_stays_out_of_the_request_log(tmp_path, monkeypatch):
    monkeypatch.setattr("docsforge.tools.applog.LOG_DIR", str(tmp_path))
    monkeypatch.setattr("docsforge.tools.applog.LOG_FILE", str(tmp_path / "docsforge.log"))
    monkeypatch.setattr("docsforge.tools.applog._configured", False)
    monkeypatch.setattr("docsforge.tools.applog._disabled", False)
    c = signed_in()
    c.get("/api/lab/me")
    c.get("/api/lab/tests")
    lines = [json.loads(l) for l in (tmp_path / "docsforge.log").read_text(encoding="utf-8").splitlines()]
    requests = [l["path"] for l in lines if l["kind"] == "request"]
    assert "/api/lab/me" not in requests and "/api/lab/tests" not in requests
    assert "/api/lab/login" in requests, "an action is still logged"
    assert any(l["kind"] == "lab" and l["event"] == "login" for l in lines)


# ── what the lab judges by itself ───────────────────────────
def test_expected_locations_are_read_however_they_are_typed():
    assert quality.parse_expected("https://Zod.dev/, docs.x.io/api/?q=1  zod.dev") == ["Zod.dev", "docs.x.io/api"]
    assert quality.matches("https://www.zod.dev/docs", ["zod.dev"])
    assert quality.matches("https://docs.x.io/api/v2/", ["docs.x.io/api"])
    assert not quality.matches("https://docs.x.io/guide/", ["docs.x.io/api"])
    assert quality.matches("https://turborepo.com/docs", ["turbo.build"],
                           follow=lambda a: "turborepo.com" if a == "turbo.build" else "")


def test_the_harvest_bar_is_the_held_out_one():
    page = {"chars": 900}
    assert quality.harvest_verdict({"pages": [page] * 10})["pass"] is True
    assert quality.harvest_verdict({"pages": [page] * 3, "expected": 3})["pass"] is True
    short = quality.harvest_verdict({"pages": [page] * 3, "expected": 40})
    assert short["pass"] is False and "3 page(s) of 40" in short["reason"]
    thin = quality.harvest_verdict({"pages": [{"chars": 50}] * 12})
    assert thin["pass"] is False and "thin" in thin["reason"]
    assert quality.harvest_verdict({"pages": [page] * 12, "error": "boom"})["pass"] is False
