"""
Accounts and sessions for the test lab.

Two roles, because the two people using the lab want different screens:

  admin    the numbers (store, tests, accuracy, tool calls), the setup, the
           feedback inbox, the logs and the accounts -- and the bench too
  tester   the bench: run a test, watch the tools it ran, give a verdict

Passwords are scrypt-hashed (standard library, no new dependency). A session
is a random token in an HttpOnly, SameSite=Strict cookie; only its SHA-256 is
stored, so a copy of `lab.db` does not sign anyone in. Five wrong passwords
for one name within ten minutes locks that name for the rest of the window.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from docsforge.lab import db

ADMIN, TESTER = "admin", "tester"
ROLES = (ADMIN, TESTER)

COOKIE = "df_lab"
SESSION_TTL = 12 * 3600
LOCK_WINDOW = 600
LOCK_AFTER = 5
MIN_PASSWORD = 8

_USERNAME = re.compile(r"^[A-Za-z0-9._-]{2,32}$")

# scrypt at n=2^14, r=8: about 16 MB and ~50 ms a check, the standard
# library's own default ceiling for memory.
_N, _R, _P = 2 ** 14, 8, 1


class AuthError(ValueError):
    """Something the person can fix: a taken name, a short password."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                             n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def _public(user: dict | None) -> dict | None:
    if not user:
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "created": user["created"], "last_login": user.get("last_login"),
            "disabled": bool(user.get("disabled"))}


def validate(username: str, password: str | None, role: str | None = None) -> None:
    if not _USERNAME.match(username or ""):
        raise AuthError("A username is 2–32 letters, digits, dots, dashes or underscores.")
    if password is not None and len(password) < MIN_PASSWORD:
        raise AuthError(f"A password needs at least {MIN_PASSWORD} characters.")
    if role is not None and role not in ROLES:
        raise AuthError(f"A role is one of: {', '.join(ROLES)}.")


def needs_setup() -> bool:
    return not db.scalar("SELECT COUNT(*) FROM users")


def create_user(username: str, password: str, role: str) -> dict:
    username = (username or "").strip()
    validate(username, password, role)
    if db.row("SELECT id FROM users WHERE username = ?", (username,)):
        raise AuthError(f"{username!r} is already taken.")
    uid = db.execute(
        "INSERT INTO users (username, role, pw_hash, created) VALUES (?, ?, ?, ?)",
        (username, role, hash_password(password), time.time()))
    return _public(db.row("SELECT * FROM users WHERE id = ?", (uid,)))


def users() -> list[dict]:
    return [_public(u) for u in db.rows("SELECT * FROM users ORDER BY role, username")]


def user(uid: int) -> dict | None:
    return _public(db.row("SELECT * FROM users WHERE id = ?", (uid,)))


def update_user(uid: int, *, role: str | None = None, disabled: bool | None = None,
                password: str | None = None) -> dict:
    found = db.row("SELECT * FROM users WHERE id = ?", (uid,))
    if not found:
        raise AuthError("No such account.")
    validate(found["username"], password, role)
    if role is not None or disabled is not None:
        # The last admin cannot be demoted or disabled: nobody could undo it
        # from the panel, only from the command line.
        leaving = (role is not None and role != ADMIN) or bool(disabled)
        if found["role"] == ADMIN and leaving and not found["disabled"]:
            admins = db.scalar(
                "SELECT COUNT(*) FROM users WHERE role = ? AND disabled = 0", (ADMIN,))
            if admins <= 1:
                raise AuthError("This is the only admin; make another one first.")
    with db.write() as cx:
        if role is not None:
            cx.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
        if disabled is not None:
            cx.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(disabled)), uid))
        if password is not None:
            cx.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), uid))
        if password is not None or disabled:
            # A reset password or a disabled account ends every open session.
            cx.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    return user(uid)


# ── signing in ──────────────────────────────────────────────
def locked(username: str) -> bool:
    since = time.time() - LOCK_WINDOW
    failures = db.scalar(
        "SELECT COUNT(*) FROM login_attempts WHERE username = ? COLLATE NOCASE "
        "AND ok = 0 AND ts > ?", (username, since))
    return (failures or 0) >= LOCK_AFTER


def authenticate(username: str, password: str, client: str = "") -> dict | None:
    """The account, or None. Raises AuthError while the name is locked."""
    username = (username or "").strip()
    if locked(username):
        raise AuthError("Too many wrong passwords for this name. Try again in ten minutes.")
    found = db.row("SELECT * FROM users WHERE username = ?", (username,))
    ok = bool(found and not found["disabled"]
              and check_password(password or "", found["pw_hash"]))
    if not found:
        # Spend the same time as a real check, so a name's existence does
        # not show in how long the refusal takes.
        check_password(password or "", hash_password("x" * MIN_PASSWORD))
    with db.write() as cx:
        cx.execute("INSERT INTO login_attempts (username, ts, ok, client) VALUES (?, ?, ?, ?)",
                   (username, time.time(), int(ok), client))
        if ok:
            cx.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), found["id"]))
    return _public(found) if ok else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(uid: int, client: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.write() as cx:
        cx.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        cx.execute("INSERT INTO sessions (token_hash, user_id, created, expires, last_seen, client) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (_digest(token), uid, now, now + SESSION_TTL, now, client))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    found = db.row(
        "SELECT u.*, s.expires, s.last_seen FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?", (_digest(token),))
    now = time.time()
    if not found or found["expires"] < now or found["disabled"]:
        return None
    if now - (found["last_seen"] or 0) > 60:
        db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, _digest(token)))
    return _public(found)


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))
