"""
The lab's accounts from the command line -- for the one case the panel cannot
handle: nobody left who can sign in as an admin.

    python -m docsforge.lab users                      # list accounts
    python -m docsforge.lab add NAME --role tester     # asks for a password
    python -m docsforge.lab reset NAME                 # new password, sessions ended
    python -m docsforge.lab disable NAME | enable NAME
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from docsforge.lab import auth, db


def _password(given: str | None) -> str:
    if given:
        return given
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The two passwords differ.")
    return first


def _find(name: str) -> dict:
    found = db.row("SELECT * FROM users WHERE username = ?", (name,))
    if not found:
        raise SystemExit(f"No account {name!r}.")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m docsforge.lab", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--role", choices=auth.ROLES, default=auth.TESTER)
    add.add_argument("--password", help="omit to be asked (keeps it out of shell history)")
    reset = sub.add_parser("reset")
    reset.add_argument("name")
    reset.add_argument("--password")
    for cmd in ("disable", "enable"):
        sub.add_parser(cmd).add_argument("name")
    args = ap.parse_args(argv)

    print(f"lab database: {db.path()}", file=sys.stderr)
    try:
        if args.cmd == "users":
            for u in auth.users():
                seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["last_login"])) \
                    if u["last_login"] else "never"
                print(f"{u['username']:24} {u['role']:7} last sign-in {seen}"
                      + ("  (disabled)" if u["disabled"] else ""))
        elif args.cmd == "add":
            made = auth.create_user(args.name, _password(args.password), args.role)
            db.log_event("user_created", "cli", account=made["username"], role=made["role"])
            print(f"added {made['username']} ({made['role']})")
        elif args.cmd == "reset":
            auth.update_user(_find(args.name)["id"], password=_password(args.password))
            db.log_event("user_changed", "cli", account=args.name, fields=["password"])
            print(f"reset {args.name}; their sessions are ended")
        else:
            auth.update_user(_find(args.name)["id"], disabled=args.cmd == "disable")
            print(f"{args.cmd}d {args.name}")
    except auth.AuthError as e:
        raise SystemExit(str(e)) from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
