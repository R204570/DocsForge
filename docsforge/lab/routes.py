"""
The lab's HTTP surface, mounted by `docsforge.server.app` and nothing else.

  /lab                  the panel (one page; it asks for a sign-in)
  /lab/assets/...       its script and stylesheet
  /api/lab/...          everything it reads and does

Three gates, in order:

  1. **This machine only.** Every route answers 404 to a client that is not
     loopback, as though it did not exist -- the chat binds 127.0.0.1 by
     default, and this holds even when someone binds it wider.
     `DOCSFORGE_LAB_REMOTE=1` lifts it, for a network you own.
  2. **Signed in.** A session cookie (see `auth`); admin-only routes check
     the role as well.
  3. **Asked for by the panel.** Anything that changes state needs the
     `X-DocsForge-Lab` header, which a page on another site cannot send
     without a CORS preflight this server never grants, and an Origin, when
     the browser sends one, of this host.
"""

from __future__ import annotations

import ipaddress
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from docsforge import lab
from docsforge.lab import activity, auth, db, records, runner, stats

router = APIRouter(include_in_schema=False)

ASSETS = Path(__file__).resolve().parent / "static"
_ASSET_TYPES = {"lab.css": "text/css", "lab.js": "text/javascript"}
CSRF_HEADER = "x-docsforge-lab"
_UNSAFE = ("POST", "PUT", "PATCH", "DELETE")

_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Rendered pages are scraped HTML, sanitised by nh3 before they arrive;
    # this is the second wall. Images may come from the documented site.
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: https:; "
                                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
}


# ── gates ───────────────────────────────────────────────────
def _client(request: Request) -> str:
    return request.client.host if request.client else ""


def is_local(host: str) -> bool:
    if os.environ.get("DOCSFORGE_LAB_REMOTE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def local_only(request: Request) -> None:
    if not lab.enabled() or not is_local(_client(request)):
        raise HTTPException(status_code=404)
    if request.method in _UNSAFE:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="Missing the lab's request header.")
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="Cross-site request refused.")


def signed_in(request: Request, _=Depends(local_only)) -> dict:
    user = auth.session_user(request.cookies.get(auth.COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return user


def admin_only(user: dict = Depends(signed_in)) -> dict:
    if user["role"] != auth.ADMIN:
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


def _bad(e: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=status)


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


# ── the page ────────────────────────────────────────────────
@router.get("/lab", dependencies=[Depends(local_only)])
@router.get("/lab/", dependencies=[Depends(local_only)])
def page():
    return FileResponse(ASSETS / "lab.html", headers=_HEADERS)


@router.get("/lab/assets/{name}", dependencies=[Depends(local_only)])
def asset(name: str):
    if name not in _ASSET_TYPES:
        raise HTTPException(status_code=404)
    return FileResponse(ASSETS / name, media_type=_ASSET_TYPES[name], headers=_HEADERS)


# ── signing in ──────────────────────────────────────────────
def _with_session(payload: dict, user: dict, request: Request) -> JSONResponse:
    token = auth.new_session(user["id"], _client(request))
    response = JSONResponse(payload)
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                        samesite="strict", path="/",
                        secure=request.url.scheme == "https")
    return response


@router.get("/api/lab/state", dependencies=[Depends(local_only)])
def state(request: Request):
    return {"setup": auth.needs_setup(),
            "user": auth.session_user(request.cookies.get(auth.COOKIE)),
            "recording": lab.recording()}


@router.post("/api/lab/setup", dependencies=[Depends(local_only)])
def setup(payload: dict, request: Request):
    """The first run: an admin account and a tester account, together."""
    if not auth.needs_setup():
        return _bad(ValueError("The lab is already set up. Sign in."), 409)
    admin = payload.get("admin") or {}
    tester = payload.get("tester") or {}
    try:
        auth.validate(str(admin.get("username") or ""), str(admin.get("password") or ""))
        auth.validate(str(tester.get("username") or ""), str(tester.get("password") or ""))
        if str(admin.get("username")).lower() == str(tester.get("username")).lower():
            raise auth.AuthError("The admin and the tester need different names.")
        made = auth.create_user(admin["username"], admin["password"], auth.ADMIN)
        auth.create_user(tester["username"], tester["password"], auth.TESTER)
    except auth.AuthError as e:
        return _bad(e)
    db.log_event("setup", made["username"], tester=tester.get("username"))
    return _with_session({"user": made}, made, request)


@router.post("/api/lab/login", dependencies=[Depends(local_only)])
def login(payload: dict, request: Request):
    username = str(payload.get("username") or "")
    try:
        user = auth.authenticate(username, str(payload.get("password") or ""), _client(request))
    except auth.AuthError as e:
        return _bad(e, 429)
    if not user:
        db.log_event("login_failed", username)
        return _bad(ValueError("Wrong name or password."), 401)
    db.log_event("login", user["username"], role=user["role"])
    return _with_session({"user": user}, user, request)


@router.post("/api/lab/logout", dependencies=[Depends(local_only)])
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@router.get("/api/lab/me")
def me(user: dict = Depends(signed_in)):
    everyone = stats.test_stats()
    return {"user": user, "tests": stats.test_stats(user["id"]),
            "review": everyone["pending_review"], "active": everyone["active"],
            "open": db.scalar("SELECT COUNT(*) FROM feedback WHERE status = 'open'") or 0,
            "recording": lab.recording(), "tools": _tool_names()}


def _tool_names() -> list[str]:
    from docsforge.tools import forge_tools
    return [t.name for t in forge_tools.TOOLS]


@router.post("/api/lab/password")
def change_password(payload: dict, user: dict = Depends(signed_in)):
    found = db.row("SELECT pw_hash FROM users WHERE id = ?", (user["id"],))
    if not auth.check_password(str(payload.get("current") or ""), found["pw_hash"]):
        return _bad(ValueError("The current password is wrong."), 403)
    try:
        auth.update_user(user["id"], password=str(payload.get("new") or ""))
    except auth.AuthError as e:
        return _bad(e)
    db.log_event("password_changed", user["username"])
    return {"ok": True, "signed_out": True}


# ── tests ───────────────────────────────────────────────────
@router.post("/api/lab/tests")
def start_test(payload: dict, user: dict = Depends(signed_in)):
    try:
        test_id = runner.submit(str(payload.get("kind") or ""), payload.get("input") or {}, user)
    except runner.LabInputError as e:
        return _bad(e)
    return {"id": test_id}


@router.get("/api/lab/tests")
def tests(kind: str = "", state: str = "", mine: int = 0, batch: int = 0, review: int = 0,
          q: str = "", limit: int = 50, offset: int = 0, user: dict = Depends(signed_in)):
    rows, total = records.list_tests(
        kind=kind, state=state, user_id=user["id"] if mine else None, batch_id=batch or None,
        review=bool(review), q=q.strip(), limit=_int(limit, 50, 1, 500),
        offset=_int(offset, 0, 0, 10 ** 9))
    return {"tests": rows, "total": total}


@router.get("/api/lab/tests/{test_id}")
def test_detail(test_id: int, user: dict = Depends(signed_in)):
    found = records.get_test(test_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such test.")
    return found


@router.post("/api/lab/tests/{test_id}/cancel")
def cancel(test_id: int, user: dict = Depends(signed_in)):
    outcome = runner.RUNNER.cancel(test_id)
    db.log_event("test_cancel", user["username"], test=test_id, outcome=outcome)
    if outcome not in (runner.CANCELLED, "stopping"):
        return _bad(ValueError(outcome), 409)
    return {"outcome": outcome}


@router.post("/api/lab/tests/{test_id}/rerun")
def rerun(test_id: int, user: dict = Depends(signed_in)):
    try:
        return {"id": runner.rerun(test_id, user)}
    except runner.LabInputError as e:
        return _bad(e, 404)


@router.get("/api/lab/tests/{test_id}/pages/{ordinal}")
def harvested_page(test_id: int, ordinal: int, tech: str, version: str,
                   user: dict = Depends(signed_in)):
    """One page a harvest test stored, from that test's own store."""
    from docsforge.lab import quality
    from docsforge.server.app import render_markdown
    from docsforge.store.kb_store import FileStore, StoreError
    kb = runner.run_dir(test_id) / "kb"
    if not kb.exists():
        raise HTTPException(status_code=404, detail="This test's store is gone.")
    try:
        found = FileStore(kb).page(tech, version, ordinal)
    except StoreError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    metrics = quality.assess(found["content"])
    return {**found, "html": render_markdown(found["content"]), "metrics": metrics,
            "flags": quality.page_flags(metrics)}


@router.post("/api/lab/render")
def render(payload: dict, user: dict = Depends(signed_in)):
    from docsforge.server.app import render_markdown
    return {"html": render_markdown(str(payload.get("markdown") or "")[:runner.STEP_OUTPUT])}


# ── batches ─────────────────────────────────────────────────
@router.get("/api/lab/presets")
def presets(user: dict = Depends(signed_in)):
    return {"presets": runner.presets()}


@router.post("/api/lab/batches")
def start_batch(payload: dict, user: dict = Depends(signed_in)):
    kind = str(payload.get("kind") or "")
    try:
        items = runner.parse_batch(kind, str(payload.get("text") or ""),
                                   payload.get("defaults") or {})
        batch_id, ids = runner.create_batch(kind, items, user, str(payload.get("title") or ""),
                                            payload.get("defaults") or {})
    except runner.LabInputError as e:
        return _bad(e)
    return {"id": batch_id, "tests": ids}


@router.get("/api/lab/batches")
def batches(user: dict = Depends(signed_in)):
    return {"batches": records.list_batches()}


@router.get("/api/lab/batches/{batch_id}")
def batch(batch_id: int, user: dict = Depends(signed_in)):
    found = records.get_batch(batch_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such batch.")
    return found


# ── verdicts ────────────────────────────────────────────────
@router.post("/api/lab/feedback")
def give_feedback(payload: dict, user: dict = Depends(signed_in)):
    try:
        return records.save_feedback(user, payload)
    except records.RecordError as e:
        return _bad(e)


@router.get("/api/lab/feedback")
def feedback(status: str = "", target_kind: str = "", verdict: str = "", mine: int = 0,
             user: dict = Depends(signed_in)):
    # A tester reads their own verdicts; an admin reads everyone's.
    own = user["role"] != auth.ADMIN or bool(mine)
    return {"feedback": records.list_feedback(status=status, target_kind=target_kind,
                                              verdict=verdict,
                                              user_id=user["id"] if own else None)}


@router.patch("/api/lab/feedback/{fid}")
def triage(fid: int, payload: dict, user: dict = Depends(admin_only)):
    try:
        return records.update_feedback(fid, user, payload.get("status"), payload.get("resolution"))
    except records.RecordError as e:
        return _bad(e)


@router.get("/api/lab/feedback/export")
def export_feedback(request: Request, format: str = "md", user: dict = Depends(admin_only)):
    stamp = time.strftime("%Y%m%d-%H%M")
    if format == "json":
        body = db.dumps({"exported": stamp, "feedback": records.list_feedback(limit=10_000)})
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="docsforge-verdicts-{stamp}.json"'})
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(records.export_feedback_markdown(base), headers={
        "Content-Disposition": f'attachment; filename="docsforge-verdicts-{stamp}.md"'})


@router.get("/api/lab/export/cases")
def export_cases(user: dict = Depends(admin_only)):
    return PlainTextResponse(records.export_cases())


# ── what the AI ran ─────────────────────────────────────────
@router.get("/api/lab/activity/turns")
def turns(q: str = "", limit: int = 50, offset: int = 0, user: dict = Depends(signed_in)):
    rows, total = activity.turns(_int(limit, 50, 1, 200), _int(offset, 0, 0, 10 ** 9), q.strip())
    return {"turns": rows, "total": total, "recording": lab.recording()}


@router.get("/api/lab/activity/turns/{turn_id}")
def turn(turn_id: str, user: dict = Depends(signed_in)):
    found = activity.turn(turn_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such turn.")
    found["feedback"] = records.feedback_for("turn", turn_id)
    return found


@router.get("/api/lab/activity/tools")
def tool_calls(tool: str = "", ok: str = "", source: str = "", q: str = "", limit: int = 50,
               offset: int = 0, user: dict = Depends(signed_in)):
    rows, total = activity.calls(_int(limit, 50, 1, 200), _int(offset, 0, 0, 10 ** 9),
                                 tool, ok, source, q.strip())
    return {"calls": rows, "total": total, "recording": lab.recording()}


@router.get("/api/lab/activity/tools/{trace_id}")
def tool_call(trace_id: str, user: dict = Depends(signed_in)):
    found = activity.call(trace_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such tool call.")
    found["feedback"] = records.feedback_for("tool", trace_id)
    return found


# ── admin ───────────────────────────────────────────────────
@router.get("/api/lab/overview")
def overview(refresh: int = 0, user: dict = Depends(admin_only)):
    return stats.overview(bool(refresh))


@router.get("/api/lab/setup-info")
def setup_info(user: dict = Depends(admin_only)):
    return stats.setup_info()


@router.get("/api/lab/logs")
def logs(kind: str = "", q: str = "", limit: int = 200, user: dict = Depends(admin_only)):
    return {"lines": stats.log_lines(kind, q, _int(limit, 200, 1, 2000)),
            "summary": stats.log_summary()}


@router.get("/api/lab/events")
def events(limit: int = 200, user: dict = Depends(admin_only)):
    return {"events": stats.lab_events(_int(limit, 200, 1, 2000))}


@router.get("/api/lab/users")
def users(user: dict = Depends(admin_only)):
    return {"users": auth.users(), "activity": stats.tester_stats()}


@router.post("/api/lab/users")
def add_user(payload: dict, user: dict = Depends(admin_only)):
    try:
        made = auth.create_user(str(payload.get("username") or ""),
                                str(payload.get("password") or ""),
                                str(payload.get("role") or auth.TESTER))
    except auth.AuthError as e:
        return _bad(e)
    db.log_event("user_created", user["username"], account=made["username"], role=made["role"])
    return made


@router.patch("/api/lab/users/{uid}")
def change_user(uid: int, payload: dict, user: dict = Depends(admin_only)):
    try:
        changed = auth.update_user(
            uid, role=payload.get("role"),
            disabled=payload.get("disabled") if "disabled" in payload else None,
            password=payload.get("password") or None)
    except auth.AuthError as e:
        return _bad(e)
    db.log_event("user_changed", user["username"], account=changed["username"],
                 fields=sorted(k for k in payload if k != "password")
                 + (["password"] if payload.get("password") else []))
    return changed


# ── actions a verdict leads to ──────────────────────────────
@router.post("/api/lab/actions/forget-resolution")
def forget_resolution(payload: dict, user: dict = Depends(signed_in)):
    """A name the tester found resolved wrongly: drop what the chat remembers
    of it, so its next lookup starts over. Deletes no documentation."""
    from docsforge.tools import forge_tools
    name = str(payload.get("name") or "").strip()
    if not name:
        return _bad(ValueError("Which name?"))
    said = forge_tools.tool_forget_resolution(name)
    db.log_event("forget_resolution", user["username"], name=name)
    return {"message": said}


# ── wiring ──────────────────────────────────────────────────
_installed = False


def install() -> None:
    """Called once by app.py: record tool calls, and pick up tests the last
    process left behind."""
    global _installed
    if _installed or not lab.enabled():
        return
    _installed = True
    lab.ACTIVE = True
    activity.install()
    try:
        runner.recover()
    except Exception:                                   # noqa: BLE001 -- a broken lab db must not stop the chat
        pass
