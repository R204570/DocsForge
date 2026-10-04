"""
Reading the lab's tests back, and the verdicts given on them.

A verdict ("feedback") can be given on three things: a test, a chat turn, or
one tool call from the activity record. One per person per thing -- giving it
again changes it -- and each carries a status an admin moves along as the
problem it reports is dealt with: open -> triaged -> fixed (or wontfix).

The two exports are what someone fixing DocsForge works from: every verdict
as Markdown (or JSON), and the resolution tests turned into held-out cases in
`scripts/heldout.py`'s own shape, so a name a tester checked by hand becomes
one the next measurement checks automatically.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from docsforge.lab import db

VERDICTS = ("correct", "partial", "wrong", "unsure")
STATUSES = ("open", "triaged", "fixed", "wontfix")
TARGETS = ("test", "turn", "tool")


class RecordError(ValueError):
    pass


# ── tests ───────────────────────────────────────────────────
_LATEST_VERDICT = ("(SELECT f.verdict FROM feedback f WHERE f.target_kind = 'test' AND "
                   "f.target_id = CAST(t.id AS TEXT) ORDER BY f.updated DESC LIMIT 1)")
_VERDICT_COUNT = ("(SELECT COUNT(*) FROM feedback f WHERE f.target_kind = 'test' AND "
                  "f.target_id = CAST(t.id AS TEXT))")


def _test_row(r: dict) -> dict:
    return {
        "id": r["id"], "kind": r["kind"], "batch_id": r["batch_id"], "parent_id": r["parent_id"],
        "created": r["created"], "started": r["started"], "finished": r["finished"],
        "state": r["state"], "input": db.loads(r["input"], {}), "auto": db.loads(r["auto"]),
        "error": r["error"] or "", "by": r.get("username") or "",
        "headline": r.get("headline") or "", "verdict": r.get("verdict"),
        "verdicts": r.get("verdicts") or 0,
        "seconds": round(r["finished"] - r["started"], 1)
        if r["finished"] and r["started"] else None,
    }


def list_tests(*, kind: str = "", state: str = "", user_id: int | None = None,
               batch_id: int | None = None, review: bool = False, q: str = "",
               limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, params = [], []
    if kind:
        where.append("t.kind = ?")
        params.append(kind)
    if state:
        where.append("t.state = ?")
        params.append(state)
    if user_id:
        where.append("t.created_by = ?")
        params.append(user_id)
    if batch_id:
        where.append("t.batch_id = ?")
        params.append(batch_id)
    if review:
        where.append(f"t.state IN ('done', 'failed') AND {_VERDICT_COUNT} = 0")
    if q:
        where.append("t.input LIKE ?")
        params.append(f"%{q}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.scalar(f"SELECT COUNT(*) FROM tests t {clause}", params) or 0
    found = db.rows(
        f"SELECT t.id, t.kind, t.batch_id, t.parent_id, t.created, t.started, t.finished, "
        f"t.state, t.input, t.auto, t.error, u.username, "
        f"json_extract(t.result, '$.headline') AS headline, "
        f"{_LATEST_VERDICT} AS verdict, {_VERDICT_COUNT} AS verdicts "
        f"FROM tests t LEFT JOIN users u ON u.id = t.created_by {clause} "
        f"ORDER BY t.id {'ASC' if batch_id else 'DESC'} LIMIT ? OFFSET ?",
        params + [limit, offset])
    return [_test_row(r) for r in found], total


def get_test(test_id: int) -> dict | None:
    found = db.row(
        f"SELECT t.*, u.username, json_extract(t.result, '$.headline') AS headline, "
        f"{_LATEST_VERDICT} AS verdict, {_VERDICT_COUNT} AS verdicts "
        f"FROM tests t LEFT JOIN users u ON u.id = t.created_by WHERE t.id = ?", (test_id,))
    if not found:
        return None
    out = _test_row(found)
    out["result"] = db.loads(found["result"], {})
    out["steps"] = [{
        "id": s["id"], "seq": s["seq"], "tool": s["tool"], "args": db.loads(s["args"], {}),
        "state": s["state"], "started": s["started"], "duration_ms": s["duration_ms"],
        "output": s["output"] or "", "omitted": s["omitted"] or 0,
        "trace_id": s["trace_id"] or "", "events": db.loads(s["events"], []),
        "error": s["error"] or "",
    } for s in db.rows("SELECT * FROM steps WHERE test_id = ? ORDER BY seq", (test_id,))]
    out["feedback"] = feedback_for("test", str(test_id))
    out["reruns"] = [r["id"] for r in db.rows(
        "SELECT id FROM tests WHERE parent_id = ? ORDER BY id", (test_id,))]
    return out


# ── batches ─────────────────────────────────────────────────
def _batch_counts(batch_id: int) -> dict:
    rows = db.rows(f"SELECT t.state, t.auto, {_LATEST_VERDICT} AS verdict FROM tests t "
                   f"WHERE t.batch_id = ?", (batch_id,))
    states, verdicts = {}, {}
    auto_pass = auto_total = 0
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
        if r["verdict"]:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
        auto = db.loads(r["auto"])
        if auto is not None:
            auto_total += 1
            auto_pass += bool(auto.get("pass"))
    finished = sum(states.get(s, 0) for s in ("done", "failed", "cancelled"))
    return {"states": states, "verdicts": verdicts, "finished": finished,
            "auto_pass": auto_pass, "auto_total": auto_total,
            "reviewed": sum(verdicts.values())}


def list_batches(limit: int = 50) -> list[dict]:
    rows = db.rows("SELECT b.*, u.username FROM batches b LEFT JOIN users u "
                   "ON u.id = b.created_by ORDER BY b.id DESC LIMIT ?", (limit,))
    return [{**{k: r[k] for k in ("id", "kind", "title", "created", "total")},
             "by": r["username"] or "", "options": db.loads(r["options"], {}),
             **_batch_counts(r["id"])} for r in rows]


def get_batch(batch_id: int) -> dict | None:
    r = db.row("SELECT b.*, u.username FROM batches b LEFT JOIN users u "
               "ON u.id = b.created_by WHERE b.id = ?", (batch_id,))
    if not r:
        return None
    tests, _ = list_tests(batch_id=batch_id, limit=1000)
    return {**{k: r[k] for k in ("id", "kind", "title", "created", "total")},
            "by": r["username"] or "", "options": db.loads(r["options"], {}),
            **_batch_counts(batch_id), "tests": tests}


# ── feedback ────────────────────────────────────────────────
def _target_exists(kind: str, target_id: str) -> bool:
    if kind == "test":
        return bool(target_id.isdigit() and db.row("SELECT id FROM tests WHERE id = ?",
                                                   (int(target_id),)))
    if kind == "turn":
        return bool(db.row("SELECT id FROM turns WHERE id = ?", (target_id,)))
    if kind == "tool":
        return bool(db.row("SELECT trace_id FROM activity WHERE trace_id = ?", (target_id,)))
    return False


def _clean_list(values, limit: int = 12, each: int = 60) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip()[:each] for v in values if str(v).strip()][:limit]


def save_feedback(user: dict, data: dict) -> dict:
    kind = str(data.get("target_kind") or "")
    target_id = str(data.get("target_id") or "").strip()
    if kind not in TARGETS:
        raise RecordError(f"A verdict is given on one of: {', '.join(TARGETS)}.")
    if not _target_exists(kind, target_id):
        raise RecordError(f"No {kind} {target_id!r}.")
    verdict = str(data.get("verdict") or "")
    if verdict not in VERDICTS:
        raise RecordError(f"A verdict is one of: {', '.join(VERDICTS)}.")
    rating = data.get("rating")
    if rating in ("", None):
        rating = None
    else:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise RecordError("A rating is 1 to 5.") from None
        if not 1 <= rating <= 5:
            raise RecordError("A rating is 1 to 5.")
    aspects = data.get("aspects") if isinstance(data.get("aspects"), dict) else {}
    aspects = {str(k)[:40]: str(v)[:60] for k, v in list(aspects.items())[:10]}
    now = time.time()
    fields = (verdict, db.dumps(aspects), db.dumps(_clean_list(data.get("issues"))), rating,
              str(data.get("expected") or "").strip()[:500],
              str(data.get("notes") or "").strip()[:5000])
    with db.write() as cx:
        found = cx.execute("SELECT id FROM feedback WHERE target_kind = ? AND target_id = ? "
                           "AND user_id = ?", (kind, target_id, user["id"])).fetchone()
        if found:
            # A changed verdict is a new report: back to open.
            cx.execute("UPDATE feedback SET verdict = ?, aspects = ?, issues = ?, rating = ?, "
                       "expected = ?, notes = ?, updated = ?, status = 'open' WHERE id = ?",
                       fields + (now, found["id"]))
            fid = found["id"]
        else:
            fid = cx.execute(
                "INSERT INTO feedback (target_kind, target_id, user_id, created, updated, "
                "verdict, aspects, issues, rating, expected, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, target_id, user["id"], now, now) + fields).lastrowid
    db.log_event("verdict", user["username"], target=f"{kind}:{target_id}", verdict=verdict,
                 issues=_clean_list(data.get("issues")))
    return get_feedback(fid)


def _feedback_row(r: dict) -> dict:
    return {
        "id": r["id"], "target_kind": r["target_kind"], "target_id": r["target_id"],
        "by": r.get("username") or "", "user_id": r["user_id"],
        "created": r["created"], "updated": r["updated"], "verdict": r["verdict"],
        "aspects": db.loads(r["aspects"], {}), "issues": db.loads(r["issues"], []),
        "rating": r["rating"], "expected": r["expected"] or "", "notes": r["notes"] or "",
        "status": r["status"], "resolution": r["resolution"] or "",
    }


def get_feedback(fid: int) -> dict | None:
    r = db.row("SELECT f.*, u.username FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
               "WHERE f.id = ?", (fid,))
    return _feedback_row(r) if r else None


def feedback_for(kind: str, target_id: str) -> list[dict]:
    return [_feedback_row(r) for r in db.rows(
        "SELECT f.*, u.username FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
        "WHERE f.target_kind = ? AND f.target_id = ? ORDER BY f.updated DESC",
        (kind, target_id))]


def _context(fb: dict) -> dict:
    """What a verdict was given on, in a line: enough to act on without opening it."""
    if fb["target_kind"] == "test" and fb["target_id"].isdigit():
        t = db.row("SELECT kind, input, auto, state, json_extract(result, '$.headline') AS "
                   "headline FROM tests WHERE id = ?", (int(fb["target_id"]),))
        if t:
            inp = db.loads(t["input"], {})
            return {"kind": t["kind"], "state": t["state"], "headline": t["headline"] or "",
                    "subject": inp.get("name") or inp.get("url") or "",
                    "language": inp.get("language") or "", "input": inp,
                    "auto": db.loads(t["auto"])}
    if fb["target_kind"] == "turn":
        t = db.row("SELECT provider, model, prompt, outcome FROM turns WHERE id = ?",
                   (fb["target_id"],))
        if t:
            return {"kind": "turn", "subject": (t["prompt"] or "")[:160],
                    "headline": f"{t['provider']} {t['model'] or ''}".strip(),
                    "state": t["outcome"]}
    if fb["target_kind"] == "tool":
        t = db.row("SELECT tool, target, ok FROM activity WHERE trace_id = ?", (fb["target_id"],))
        if t:
            return {"kind": t["tool"], "subject": t["target"] or "",
                    "headline": "ok" if t["ok"] else "error", "state": ""}
    return {"kind": fb["target_kind"], "subject": fb["target_id"], "headline": "", "state": ""}


def list_feedback(*, status: str = "", target_kind: str = "", verdict: str = "",
                  user_id: int | None = None, limit: int = 200) -> list[dict]:
    where, params = [], []
    for column, value in (("f.status", status), ("f.target_kind", target_kind),
                          ("f.verdict", verdict)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if user_id:
        where.append("f.user_id = ?")
        params.append(user_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.rows(f"SELECT f.*, u.username FROM feedback f LEFT JOIN users u "
                   f"ON u.id = f.user_id {clause} ORDER BY f.updated DESC LIMIT ?",
                   params + [limit])
    return [{**_feedback_row(r), "context": _context(_feedback_row(r))} for r in rows]


def update_feedback(fid: int, admin: dict, status: str | None = None,
                    resolution: str | None = None) -> dict:
    if not get_feedback(fid):
        raise RecordError("No such verdict.")
    if status is not None and status not in STATUSES:
        raise RecordError(f"A status is one of: {', '.join(STATUSES)}.")
    with db.write() as cx:
        if status is not None:
            cx.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, fid))
        if resolution is not None:
            cx.execute("UPDATE feedback SET resolution = ? WHERE id = ?",
                       (str(resolution).strip()[:2000], fid))
    db.log_event("triage", admin["username"], feedback=fid, status=status)
    return get_feedback(fid)


# ── exports ─────────────────────────────────────────────────
def export_feedback_markdown(base_url: str = "") -> str:
    items = list_feedback(limit=10_000)
    counts = {s: sum(1 for f in items if f["status"] == s) for s in STATUSES}
    lines = [f"# DocsForge lab verdicts — exported {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"{len(items)} verdicts: " + ", ".join(f"{n} {s}" for s, n in counts.items() if n)
             + ".", ""]
    for status in STATUSES:
        group = [f for f in items if f["status"] == status]
        if not group:
            continue
        lines += [f"## {status.capitalize()} ({len(group)})", ""]
        for f in group:
            c = f["context"]
            title = (f"{f['target_kind'].capitalize()} {f['target_id']} · {c.get('kind', '')}"
                     f" · `{c.get('subject', '')}`"
                     + (f" ({c['language']})" if c.get("language") else "")
                     + f" — **{f['verdict']}**")
            lines.append(f"### {title}")
            if c.get("headline"):
                lines.append(f"- Result: {c['headline']}")
            auto = c.get("auto")
            if auto:
                lines.append(f"- Automatic check: {'pass' if auto.get('pass') else 'fail'}"
                             f" — {auto.get('reason', '')}")
            lines.append(f"- By {f['by'] or '?'}, "
                         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(f['updated']))}")
            if f["aspects"]:
                lines.append("- " + "; ".join(f"{k}: {v}" for k, v in f["aspects"].items()))
            if f["issues"]:
                lines.append(f"- Issues: {', '.join(f['issues'])}")
            if f["rating"]:
                lines.append(f"- Rating: {f['rating']}/5")
            if f["expected"]:
                lines.append(f"- Should be: {f['expected']}")
            if f["notes"]:
                lines.append("- Notes: " + f["notes"].replace("\n", "\n  "))
            if f["resolution"]:
                lines.append(f"- Resolution: {f['resolution']}")
            if f["target_kind"] == "test" and base_url:
                lines.append(f"- Open: {base_url}/lab#/tests/{f['target_id']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _location(url: str, with_path: bool) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").strip("/")
    return f"{host}/{path}" if with_path and path else host


def export_cases() -> str:
    """Resolution tests a tester judged, as `scripts/heldout.py` cases.

    Right answers keep the host they landed on (and the path, for a language
    edition, which lives under one); wrong ones take the address the tester
    said it should have been, when they gave one. Unjudged, unsure and
    uncorrected ones are left out -- a case needs a right answer written down.
    """
    rows = db.rows(
        "SELECT t.id, t.input, json_extract(t.result, '$.best_url') AS best, f.verdict, "
        "f.expected FROM tests t JOIN feedback f ON f.target_kind = 'test' AND "
        "f.target_id = CAST(t.id AS TEXT) WHERE t.kind = 'resolve' ORDER BY t.id")
    seen: dict[tuple, str] = {}
    for r in rows:
        inp = db.loads(r["input"], {})
        key = (inp.get("name", ""), inp.get("language", ""))
        edition = bool(key[1])
        if r["verdict"] == "correct" and r["best"]:
            where = _location(r["best"], edition)
        elif r["verdict"] in ("wrong", "partial") and r["expected"]:
            where = _location(r["expected"], edition or "/" in r["expected"].split("://")[-1])
        else:
            continue
        seen[key] = f"    ({key[0]!r}, {key[1]!r}, ({where!r},)),  # test {r['id']}, {r['verdict']}"
    head = [f"# Exported from the DocsForge lab {time.strftime('%Y-%m-%d %H:%M')}: "
            f"{len(seen)} name(s) a tester checked by hand.",
            "# Paste into a new round in scripts/heldout.py; do not edit to match a run.",
            "LAB: list[tuple[str, str, tuple[str, ...]]] = ["]
    return "\n".join(head + list(seen.values()) + ["]"]) + "\n"
