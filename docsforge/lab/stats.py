"""
The admin's numbers: what is stored, what the testers found, what the tools
did, and how this DocsForge is set up.

Every figure is counted from something real -- the store, the lab's own
tables, the log -- and a rate is given only with its denominator beside it,
so "92%" never stands in for "12 of 13". Store figures are cached for a
minute, because the store may be a hosted database a round trip away.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from docsforge import lab
from docsforge.lab import auth, db

DAY = 86400
STORE_TTL = 60.0

_cache: dict = {}
_cache_lock = threading.Lock()


# ── the store ───────────────────────────────────────────────
def store_stats(refresh: bool = False) -> dict:
    with _cache_lock:
        hit = _cache.get("store")
        if hit and not refresh and time.time() - hit[0] < STORE_TTL:
            return hit[1]
    from docsforge.tools import forge_tools
    backend = forge_tools.store()
    out = {"kind": backend.kind, "location": backend.location,
           "degraded": getattr(backend, "degraded", "") or "", "ok": True}
    try:
        techs, total = backend.technologies()
        out.update({
            "technologies": total,
            "versions": sum(t["versions"] for t in techs),
            "pages": sum(t["pages"] for t in techs),
            "characters": sum(t["characters"] for t in techs),
            "incomplete": sum(1 for t in techs if t.get("complete") is False),
            "largest": sorted(({"name": t["name"], "pages": t["pages"],
                                "characters": t["characters"]} for t in techs),
                              key=lambda t: -t["pages"])[:8],
            "recent": sorted(({"name": t["name"], "harvested": t["harvested"],
                               "latest": t["latest"], "pages": t["pages"]} for t in techs),
                             key=lambda t: t["harvested"] or "", reverse=True)[:8],
        })
        footprint = backend.footprint() if hasattr(backend, "footprint") else {}
        out["bytes"] = footprint.get("bytes")
    except Exception as e:                              # noqa: BLE001 -- shown, not raised
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    out["measured"] = time.time()
    with _cache_lock:
        _cache["store"] = (time.time(), out)
    return out


def harvest_stats() -> dict:
    from docsforge.tools import harvest_jobs
    try:
        running = [j.as_dict() for j in harvest_jobs.running()]
        recent = [j.as_dict() for j in harvest_jobs.recent()[:10]]
    except Exception:                                   # noqa: BLE001
        running, recent = [], []
    return {"running": running, "recent": recent,
            "states": dict(Counter(j.get("state") for j in recent))}


# ── the lab's own tables ────────────────────────────────────
def _rate(good: int, total: int) -> dict:
    return {"good": good, "total": total,
            "percent": round(100 * good / total, 1) if total else None}


def test_stats(user_id: int | None = None) -> dict:
    """Tests by kind: how many, how they ended, what the automatic check said,
    and what testers said -- the last being the accuracy that counts."""
    mine = " AND t.created_by = ?" if user_id else ""
    params = (user_id,) if user_id else ()
    by_kind = {}
    for kind in ("resolve", "fetch", "harvest"):
        rows = db.rows(
            f"SELECT t.state, t.auto, (SELECT f.verdict FROM feedback f WHERE f.target_kind = "
            f"'test' AND f.target_id = CAST(t.id AS TEXT) ORDER BY f.updated DESC LIMIT 1) "
            f"AS verdict FROM tests t WHERE t.kind = ?{mine}", (kind,) + params)
        states = Counter(r["state"] for r in rows)
        autos = [db.loads(r["auto"]) for r in rows if r["auto"]]
        verdicts = Counter(r["verdict"] for r in rows if r["verdict"])
        judged = sum(verdicts[v] for v in ("correct", "partial", "wrong"))
        by_kind[kind] = {
            "total": len(rows), "states": dict(states),
            "auto": _rate(sum(1 for a in autos if a and a.get("pass")), len(autos)),
            "verdicts": dict(verdicts),
            "accuracy": _rate(verdicts["correct"], judged),
            "reviewed": sum(verdicts.values()),
        }
    pending = db.scalar(
        f"SELECT COUNT(*) FROM tests t WHERE t.state IN ('done', 'failed'){mine} AND NOT EXISTS "
        f"(SELECT 1 FROM feedback f WHERE f.target_kind = 'test' "
        f"AND f.target_id = CAST(t.id AS TEXT))", params) or 0
    active = db.scalar(f"SELECT COUNT(*) FROM tests t WHERE t.state IN ('queued', 'running'){mine}",
                       params) or 0
    return {"kinds": by_kind, "pending_review": pending, "active": active,
            "total": sum(k["total"] for k in by_kind.values())}


def tester_stats() -> list[dict]:
    return db.rows(
        "SELECT u.id, u.username, u.role, u.last_login, "
        "(SELECT COUNT(*) FROM tests t WHERE t.created_by = u.id) AS tests, "
        "(SELECT COUNT(*) FROM feedback f WHERE f.user_id = u.id) AS verdicts, "
        "(SELECT MAX(f.updated) FROM feedback f WHERE f.user_id = u.id) AS last_verdict "
        "FROM users u ORDER BY tests DESC, u.username")


def feedback_stats() -> dict:
    rows = db.rows("SELECT target_kind, verdict, status, issues FROM feedback")
    issues = Counter()
    for r in rows:
        for issue in db.loads(r["issues"], []) or []:
            issues[issue] += 1
    return {"total": len(rows),
            "status": dict(Counter(r["status"] for r in rows)),
            "verdicts": dict(Counter(r["verdict"] for r in rows)),
            "targets": dict(Counter(r["target_kind"] for r in rows)),
            "issues": issues.most_common(10)}


def activity_stats(days: int = 14) -> dict:
    # Calendar days on this machine's clock, today last: a call made this
    # morning belongs to today's column, not to "24 hours ago".
    now = time.localtime()
    midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    since = midnight - (days - 1) * DAY
    by_tool = db.rows(
        "SELECT tool, COUNT(*) AS calls, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors, "
        "AVG(duration_ms) AS avg_ms, MAX(duration_ms) AS max_ms FROM activity "
        "WHERE tool IS NOT NULL GROUP BY tool ORDER BY calls DESC")
    by_source = {r["source"]: r["n"] for r in db.rows(
        "SELECT COALESCE(source, 'other') AS source, COUNT(*) AS n FROM activity "
        "WHERE tool IS NOT NULL GROUP BY 1")}
    outcomes, providers = Counter(), Counter()
    for r in db.rows("SELECT provider, outcome, COUNT(*) AS n FROM turns GROUP BY 1, 2"):
        outcomes[r["outcome"] or "?"] += r["n"]
        providers[r["provider"] or "?"] += r["n"]
    daily = {}
    for table, column in (("activity", "calls"), ("turns", "turns"), ("tests", "tests")):
        stamp = "created" if table == "tests" else "started"
        for r in db.rows(f"SELECT CAST(({stamp} - ?) / {DAY} AS INTEGER) AS d, COUNT(*) AS n "
                         f"FROM {table} WHERE {stamp} >= ? GROUP BY d", (since, since)):
            day = time.strftime("%Y-%m-%d", time.localtime(since + r["d"] * DAY + DAY / 2))
            daily.setdefault(day, {"calls": 0, "turns": 0, "tests": 0})[column] = r["n"]
    series = []
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.localtime(since + i * DAY + DAY / 2))
        series.append({"day": day, **daily.get(day, {"calls": 0, "turns": 0, "tests": 0})})
    return {
        "calls": sum(r["calls"] for r in by_tool),
        "errors": sum(r["errors"] or 0 for r in by_tool),
        "by_tool": [{**r, "avg_ms": round(r["avg_ms"] or 0), "max_ms": round(r["max_ms"] or 0)}
                    for r in by_tool],
        "by_source": by_source,
        "turns": sum(outcomes.values()),
        "turn_outcomes": dict(outcomes),
        "turn_providers": dict(providers),
        "daily": series,
    }


def overview(refresh: bool = False) -> dict:
    return {"store": store_stats(refresh), "harvests": harvest_stats(),
            "tests": test_stats(), "feedback": feedback_stats(),
            "activity": activity_stats(), "testers": tester_stats(),
            "log": log_summary(), "generated": time.time()}


# ── setup ───────────────────────────────────────────────
#: Every setting worth showing: whether its value may be shown (a secret is
#: reported as set or not, never echoed), and what it does, so the page
#: explains itself instead of printing a bare value.
SETTINGS = (
    ("DOCSFORGE_PROVIDER", True, "Which model the chat starts on. Unset: the first one configured."),
    ("DOCSFORGE_DB", False, "The Postgres database harvests are stored in. Unset: Markdown files in knowledge_base/."),
    ("DATABASE_URL", False, "Read when DOCSFORGE_DB is unset (the name hosting platforms use)."),
    ("DOCSFORGE_KB_ROOT", True, "Where the file store keeps harvests. Unset: knowledge_base/ beside the code."),
    ("DOCSFORGE_MAX_CHARS", True, "The longest tool result handed to a model, in characters. Longer ones are cut with a marker; storage keeps everything. Default 200,000."),
    ("DOCSFORGE_HARVEST_DEADLINE", True, "Seconds a harvest runs inside one tool call before it carries on in the background. Default 25."),
    ("DOCSFORGE_REASONING", True, "on lets a model settle four hard decisions during a harvest, at most 12 calls. Default off."),
    ("DOCSFORGE_ALLOW_DELETE", True, "1 gives models a tool that deletes stored documentation. Default off: you delete, in DocsStore."),
    ("DOCSFORGE_ALLOW_PRIVATE", True, "1 allows fetching private and loopback addresses (docs on your own network). Default off: the SSRF guard."),
    ("DOCSFORGE_OUT_ROOT", True, "The only folder save_docs may write files into. Default ./docs_md."),
    ("DOCSFORGE_SEARCH", True, "An optional search service the resolver may ask as a last resort."),
    ("DOCSFORGE_RESOLVE_CACHE", True, "Where remembered name → URL answers are kept. Default ~/.docsforge/resolutions.json."),
    ("DOCSFORGE_LOG_DIR", True, "Where docsforge.log is written. Default logs/ in the folder the server started from."),
    ("DOCSFORGE_MCP_TOKEN", False, "The bearer token /mcp requires when main.py serves over HTTP. Not used by this chat."),
    ("GITHUB_TOKEN", False, "Raises GitHub's API limits. Resolution leans on GitHub's search: 10 searches a minute without it."),
    ("DOCSFORGE_LAB", True, "0 removes the test lab. Default on."),
    ("DOCSFORGE_LAB_DIR", True, "The lab's database and harvest-test runs. Default lab_data/ beside the code."),
    ("DOCSFORGE_LAB_RECORD", True, "0 stops recording chat turns and tool calls. Default on."),
    ("DOCSFORGE_LAB_WORKERS", True, "Resolution and content tests run at once. Default 2."),
    ("DOCSFORGE_LAB_HARVESTS", True, "Harvest tests run at once. Default 1."),
    ("DOCSFORGE_LAB_REMOTE", True, "1 lets other machines reach /lab. Default off: this machine only."),
)


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def playwright_state() -> dict:
    """Whether pages that need JavaScript can be rendered by *this* process:
    the package importable from the Python running the server, and a
    Chromium downloaded where Playwright looks for one. Checked on disk,
    without starting a browser."""
    if importlib.util.find_spec("playwright") is None:
        return {"state": "missing"}
    where = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if where == "0":
        spec = importlib.util.find_spec("playwright")
        roots = [Path(spec.origin).parent / "driver" / "package" / ".local-browsers"]
    elif where:
        roots = [Path(where)]
    elif sys.platform == "win32":
        roots = [Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"]
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]
    for root in roots:
        try:
            if any(d.name.startswith("chromium") for d in root.iterdir() if d.is_dir()):
                return {"state": "ready", "browsers": str(root)}
        except OSError:
            continue
    return {"state": "no-browser", "browsers": str(roots[0])}


def _features(catalog: list[dict]) -> list[dict]:
    """Each switch as a sentence: its state, what that state means here, and
    how to change it. `ok` is True (on), False (off by choice) or None (a
    problem worth fixing)."""
    from docsforge.tools import forge_tools, harvest_jobs

    out = []
    pw = playwright_state()
    python = sys.executable
    if pw["state"] == "ready":
        out.append({"label": "JavaScript rendering", "state": "ready", "ok": True,
                    "meaning": "Pages that draw their content with JavaScript are opened in a headless "
                               "Chromium, and a site that is mostly rendered switches the whole crawl to "
                               "rendering.",
                    "change": f"Chromium found in {pw['browsers']}."})
    elif pw["state"] == "no-browser":
        out.append({"label": "JavaScript rendering", "state": "no Chromium", "ok": None,
                    "meaning": "Playwright is installed but its browser is not downloaded, so a page that "
                               "is only a JavaScript shell cannot be read and is reported as failed.",
                    "change": f'"{python}" -m playwright install chromium'})
    else:
        out.append({"label": "JavaScript rendering", "state": "not installed", "ok": None,
                    "meaning": "The Python running this server has no Playwright. Static sites, llms.txt "
                               "and sites that publish a Markdown copy are unaffected, but a page that is "
                               "only a JavaScript shell (Ember's guides, egui) cannot be read and is "
                               "reported as failed.",
                    "change": f'"{python}" -m pip install playwright, then '
                              f'"{python}" -m playwright install chromium — or start the server with '
                              f'a Python that already has it.'})

    reasoning = os.environ.get("DOCSFORGE_REASONING", "off").strip().lower() in ("1", "on", "true", "yes")
    any_model = any(p.get("available") for p in catalog)
    out.append({
        "label": "Bounded reasoning",
        "state": ("on" if any_model else "on, but no model") if reasoning else "off",
        "ok": (True if any_model else None) if reasoning else False,
        "meaning": ("Four decisions are settled by rules alone — which part of an unfamiliar page is "
                    "the content, what kind of documentation a set is, whether a new host is the same "
                    "project, and whether a page that loaded is really an error page. Nothing is spent "
                    "on a model. This is the default, and what the tests run."
                    if not reasoning else
                    "At those four decisions DocsForge may ask the default model: at most 12 calls a "
                    "harvest, one per page template or host, every answer checked before it is used "
                    "and recorded beside the coverage note. It can refuse a host, never admit one."
                    + ("" if any_model else " No model is configured, so it cannot actually ask.")),
        "change": "DOCSFORGE_REASONING=on in .env (off to turn it back off), then restart."})
    out.append({
        "label": "Delete tool for models", "state": "on" if forge_tools.ALLOW_DELETE else "off",
        "ok": bool(forge_tools.ALLOW_DELETE),
        "meaning": ("A model can call forget_documentation and delete stored documentation."
                    if forge_tools.ALLOW_DELETE else
                    "A model cannot delete anything it harvested; you delete, in DocsStore or with "
                    "python -m docsforge --forget. Re-harvesting replaces a version without it."),
        "change": "DOCSFORGE_ALLOW_DELETE=1 to give models the tool."})
    out.append({
        "label": "Tool result cap", "state": f"{forge_tools.MAX_CHARS:,} characters", "ok": True,
        "meaning": "The longest result one tool call hands a model. Longer results end with a marker "
                   "saying how much was left out; what is stored is never cut.",
        "change": "DOCSFORGE_MAX_CHARS in .env. Raise it only as far as your model's context allows."})
    out.append({
        "label": "Harvest deadline", "state": f"{harvest_jobs.DEADLINE:g} seconds", "ok": True,
        "meaning": "How long a harvest runs inside one tool call. Past it, the call answers with a "
                   "harvest id and the harvest carries on in the background, so a client never times out.",
        "change": "DOCSFORGE_HARVEST_DEADLINE in .env."})
    token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    out.append({
        "label": "GitHub token", "state": "set" if token else "not set", "ok": True if token else False,
        "meaning": ("GitHub's API answers at the authenticated rate." if token else
                    "Resolution asks GitHub's search what a name usually means, which allows 10 "
                    "searches a minute without a token; a burst waits up to a minute. Everything "
                    "still works, sometimes slower."),
        "change": "GITHUB_TOKEN=… in .env (a token with no scopes is enough)."})
    out.append({
        "label": "Lab records the chat", "state": "on" if lab.recording() else "off",
        "ok": lab.recording(),
        "meaning": ("Every chat turn — question, tool calls with their output, answer — is kept in "
                    "lab_data/ for AI activity." if lab.recording() else
                    "Chat turns and tool calls are not recorded; AI activity shows only what was "
                    "recorded before."),
        "change": "DOCSFORGE_LAB_RECORD=0 to stop recording."})
    return out


def setup_info() -> dict:
    import docsforge
    from docsforge import providers
    from docsforge.core import resolver
    from docsforge.tools import applog, forge_tools, harvest_jobs

    env = []
    for key, showable, what in SETTINGS:
        value = os.environ.get(key)
        env.append({"key": key, "set": bool(value), "about": what,
                    "value": (value if showable else ("set" if value else "")) or ""})
    try:
        catalog = providers.catalog()
        default = providers.default_name()
    except Exception as e:                              # noqa: BLE001
        catalog, default = [], f"unavailable: {e}"
    cache = resolver._cache_file() if hasattr(resolver, "_cache_file") else None
    remembered = None
    if cache and Path(cache).exists():
        try:
            remembered = len(json.loads(Path(cache).read_text(encoding="utf-8")))
        except (ValueError, OSError):
            remembered = None
    log_file = Path(applog.LOG_FILE)
    lab_db = db.path()
    return {
        "build": [
            {"label": "Version", "value": docsforge.__version__, "about": "The DocsForge package running."},
            {"label": "Python", "value": sys.version.split()[0], "about": "The interpreter's version."},
            {"label": "Interpreter", "value": sys.executable,
             "about": "The Python this server runs on. Optional packages — Playwright, model SDKs — "
                      "count only if they are installed in this one."},
            {"label": "Platform", "value": platform.platform(), "about": "The operating system."},
            {"label": "Default model", "value": default,
             "about": "What the chat starts on: DOCSFORGE_PROVIDER, or the first one ready."},
        ],
        "features": _features(catalog),
        "providers": catalog, "default_provider": default,
        "tools": [t.name for t in forge_tools.TOOLS],
        "store": store_stats(),
        "paths": [
            {"label": "Checkout", "value": str(docsforge.ROOT),
             "about": "Where main.py, .env and knowledge_base/ live."},
            {"label": "Lab data", "value": str(lab.data_dir()),
             "about": "The lab's database and every harvest test's own store."},
            {"label": "Lab database", "value": str(lab_db), "size": _size(lab_db),
             "about": "Accounts, tests, verdicts and the record of every chat turn and tool call."},
            {"label": "Log", "value": str(log_file.resolve()), "size": _size(log_file),
             "about": "One JSON line per request, tool call, harvest step and lab event."},
            {"label": "Remembered resolutions", "value": str(cache or ""),
             "count": remembered,
             "about": "Names already resolved, kept 30 days so a repeat lookup costs nothing. "
                      "forget_resolution clears one."},
            {"label": "Harvest records", "value": str(harvest_jobs.state_dir()),
             "about": "Status of background harvests, shared by every DocsForge process on this machine."},
        ],
        "env": env,
        "accounts": auth.users(),
    }


# ── the log ─────────────────────────────────────────────────
TAIL_BYTES = 2_000_000


def _tail_lines(limit_bytes: int = TAIL_BYTES) -> list[str]:
    from docsforge.tools import applog
    path = Path(applog.LOG_FILE)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > limit_bytes else lines        # the first may be cut


def log_lines(kind: str = "", q: str = "", limit: int = 200) -> list[dict]:
    """The newest `limit` lines of `logs/docsforge.log`, parsed, newest first."""
    out = []
    needle = (q or "").lower()
    for line in reversed(_tail_lines()):
        if needle and needle not in line.lower():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = {"kind": "text", "message": line}
        if kind.startswith("-") and record.get("kind") == kind[1:]:
            continue
        if kind and not kind.startswith("-") and record.get("kind") != kind:
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def log_summary() -> dict:
    kinds = Counter()
    errors = 0
    for line in _tail_lines(500_000):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        kinds[record.get("kind", "?")] += 1
        if record.get("kind") == "error" or (record.get("kind") == "tool_call"
                                              and not record.get("ok", True)):
            errors += 1
    return {"kinds": dict(kinds), "errors": errors}


def lab_events(limit: int = 200) -> list[dict]:
    rows = db.rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["detail"] = db.loads(r["detail"], {})
    return rows
