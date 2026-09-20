"""The whole of DocsForge on this machine: a local database, a local MCP
server, and the benchmarks pointed at it -- with nothing shared.

    python -m benchmarks.offline             # start the offline server, leave it up (Ctrl-C stops it)
    python -m benchmarks.offline --bench     # start it, run every suite (writes and whole harvests too), stop it
    python -m benchmarks.run --offline       # the same as --bench
    python -m benchmarks.offline --env       # show the environment the server gets, secrets masked
    python -m benchmarks.offline --reset     # drop and recreate the offline database first

The hosted DocsForge is stateless, ephemeral, and shares one Aiven database
with everything that connects to it -- so a harvest there is cut at the
request deadline and a benchmark there must not write. Here the opposite
holds on purpose: the server is one long-lived process, a harvest runs to
the last page the site lists, and the database is local and disposable.

Every setting the server reads comes from its own offline name, and the
offline value is placed in the server's environment before it starts. That
is what keeps `.env` out of it: DocsForge loads `.env` on import but never
overrides a variable that is already set, so the shared database, the
hosted token and the hosted deadline are never seen.

    DOCSFORGE_OFFLINE_DB            full DSN; else built from the five below
    DOCSFORGE_OFFLINE_PG_HOST       127.0.0.1
    DOCSFORGE_OFFLINE_PG_PORT       5432
    DOCSFORGE_OFFLINE_PG_USER       postgres
    DOCSFORGE_OFFLINE_PG_PASSWORD   the local password on `.env`'s commented-out
                                    local DSN, when there is one
    DOCSFORGE_OFFLINE_PG_DATABASE   docsforge          (created if missing)
    DOCSFORGE_OFFLINE_ROOT          ./offline          (logs, caches, harvest records, save_docs output)
    DOCSFORGE_OFFLINE_PORT          8765
    DOCSFORGE_OFFLINE_TOKEN         none: loopback needs no bearer
    DOCSFORGE_OFFLINE_DEADLINE      25 seconds a tool call waits before handing back a harvest id;
                                    the harvest itself is never cut

The database must be on this machine. A DSN whose host is not loopback is
refused outright, because "offline" that reaches the shared store is the one
mistake this file exists to make impossible.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx2 as httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


# -- settings ----------------------------------------------------------------

@dataclass
class Offline:
    dsn: str
    database: str
    root: Path
    port: int
    token: str
    deadline: float

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def admin_dsn(self) -> str:
        """The same server, its maintenance database: where CREATE DATABASE runs."""
        u = urlsplit(self.dsn)
        return urlunsplit((u.scheme, u.netloc, "/postgres", u.query, ""))

    def masked(self) -> str:
        return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", self.dsn)


def _local_credentials() -> dict:
    """User, password, host and port from `.env`'s commented-out local DSN.

    The line `# DOCSFORGE_DB=postgresql://postgres:…@127.0.0.1:5432/DocsForge`
    is kept in `.env` for switching back to the local store, and it is the
    one place the local password is written down. Only a loopback host is
    taken from it: the live line above it names the shared database.
    """
    path = ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        m = re.match(r"\s*#\s*DOCSFORGE_DB\s*=\s*(postgres(?:ql)?://\S+)", line)
        if not m:
            continue
        u = urlsplit(m.group(1))
        if (u.hostname or "") in LOOPBACK:
            # As written in a URL the password is percent-encoded (`.env`
            # itself says an `@` must be `%40`); `urlsplit` hands it back
            # still encoded, and it is re-encoded when the DSN is rebuilt.
            return {"user": unquote(u.username or ""), "password": unquote(u.password or ""),
                    "host": u.hostname or "", "port": u.port or 5432}
    return {}


def settings(env=os.environ) -> Offline:
    dsn = (env.get("DOCSFORGE_OFFLINE_DB") or "").strip()
    database = (env.get("DOCSFORGE_OFFLINE_PG_DATABASE") or "").strip()
    if not dsn:
        local = _local_credentials()
        host = env.get("DOCSFORGE_OFFLINE_PG_HOST") or local.get("host") or "127.0.0.1"
        port = env.get("DOCSFORGE_OFFLINE_PG_PORT") or local.get("port") or 5432
        user = env.get("DOCSFORGE_OFFLINE_PG_USER") or local.get("user") or "postgres"
        password = env.get("DOCSFORGE_OFFLINE_PG_PASSWORD")
        if password is None:
            password = local.get("password", "")
        database = database or "docsforge"
        auth = quote(user, safe="") + (f":{quote(password, safe='')}" if password else "")
        dsn = f"postgresql://{auth}@{host}:{port}/{database}"
    else:
        database = database or urlsplit(dsn).path.lstrip("/") or "docsforge"
    host = urlsplit(dsn).hostname or ""
    if host not in LOOPBACK:
        raise SystemExit(f"offline refuses a database that is not on this machine: {host!r}")
    root = Path(env.get("DOCSFORGE_OFFLINE_ROOT") or (ROOT / "offline")).resolve()
    return Offline(
        dsn=dsn, database=database, root=root,
        port=int(env.get("DOCSFORGE_OFFLINE_PORT") or 8765),
        token=(env.get("DOCSFORGE_OFFLINE_TOKEN") or "").strip(),
        deadline=float(env.get("DOCSFORGE_OFFLINE_DEADLINE") or 25),
    )


def environment(off: Offline) -> dict[str, str]:
    """What the server process is started with.

    Every variable DocsForge reads for storage, caches, the token and the
    harvest clock is set here -- to the offline value, or to empty, which
    `load_dotenv` respects as "already set" and leaves alone. Nothing about
    providers or model keys is touched: those are not what offline is about.
    """
    root = off.root
    for sub in ("logs", "harvests", "out", "knowledge_base"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DOCSFORGE_DB": off.dsn,
        "DATABASE_URL": "",
        "DOCSFORGE_KB_ROOT": str(root / "knowledge_base"),      # only if Postgres is down
        "DOCSFORGE_LOG_DIR": str(root / "logs"),
        "DOCSFORGE_HARVEST_STATE": str(root / "harvests"),
        "DOCSFORGE_RESOLVE_CACHE": str(root / "resolutions.json"),
        "DOCSFORGE_SELECTION_POLICY": str(root / "selection.json"),
        "DOCSFORGE_OUT_ROOT": str(root / "out"),
        "DOCSFORGE_MCP_TOKEN": off.token,
        "DOCSFORGE_PUBLIC_URL": "",
        # A tool call hands back a harvest id after `deadline` seconds; the
        # harvest keeps going, and with no linger ceiling the process waits
        # for every page rather than abandoning a crawl at a timer.
        "DOCSFORGE_HARVEST_DEADLINE": str(off.deadline),
        "DOCSFORGE_HARVEST_LINGER": "0",
        # The offline store is disposable, so the one irreversible tool is
        # available here and the benchmarks can clean up after themselves.
        "DOCSFORGE_ALLOW_DELETE": "1",
        "DOCSFORGE_BUILD": _git("rev-parse", "--short=7", "HEAD") or "offline",
        "PORT": str(off.port),
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


# -- the database ------------------------------------------------------------

def reset_caches(off: Offline) -> list[str]:
    """Forget what the offline server remembered between runs.

    A reset that dropped the database but kept `resolutions.json` would run
    the next `find_docs` from memory in a hundredth of a second and call it
    resolution. Logs are kept; they are the record of what happened.
    """
    gone = []
    for name in ("resolutions.json", "selection.json"):
        path = off.root / name
        if path.exists():
            path.unlink()
            gone.append(name)
    for sub in ("harvests", "out", "knowledge_base"):
        folder = off.root / sub
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            gone.append(sub + "/")
    return gone


def ensure_database(off: Offline, reset: bool = False) -> str:
    """Create the offline database if it is missing; with `reset`, first drop it.

    The tables come from DocsForge's own `migrate()` on the server's first
    connection, so only the database itself is made here.
    """
    import psycopg
    if reset:
        cleared = reset_caches(off)
        if cleared:
            print(f"caches    cleared {', '.join(cleared)}")
    host = urlsplit(off.dsn).hostname or ""
    if host not in LOOPBACK:
        raise SystemExit(f"refusing to create or drop a database on {host!r}")
    try:
        cx = psycopg.connect(off.admin_dsn, connect_timeout=8, autocommit=True)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"cannot reach the local Postgres at {off.masked()}: {e}\n"
            f"Is it running? Set DOCSFORGE_OFFLINE_PG_* or DOCSFORGE_OFFLINE_DB "
            f"if the host, port, user or password differ.") from e
    with cx:
        name = off.database
        exists = cx.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
        if exists and reset:
            cx.execute(f'drop database "{name}" with (force)')
            exists = None
            action = "recreated"
        else:
            action = "exists" if exists else "created"
        if not exists:
            cx.execute(f'create database "{name}"')
    return action


# -- the server --------------------------------------------------------------

def health(base: str, timeout: float = 5.0) -> dict | None:
    try:
        r = httpx.get(f"{base}/health", timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    try:
        return r.json() if r.status_code == 200 else None
    except ValueError:
        return None


def start_server(off: Offline, wait: float = 90.0) -> subprocess.Popen:
    """`python main.py --http` on loopback with the offline environment.

    Waits until `/health` answers, then insists it answers *postgres* and not
    degraded: a server that quietly fell back to files would run every
    benchmark against an empty folder and report it as the store.
    """
    if health(off.base, timeout=2.0):
        raise SystemExit(f"something already answers on {off.base}; stop it or set DOCSFORGE_OFFLINE_PORT")
    log = (off.root / "logs" / "server.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--http", "--host", "127.0.0.1",
         "--port", str(off.port)],
        cwd=ROOT, env=environment(off), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + wait
    body = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the server exited with {proc.returncode}; see {off.root / 'logs' / 'server.log'}")
        body = health(off.base, timeout=3.0)
        if body:
            break
        time.sleep(0.5)
    if not body:
        stop_server(proc)
        raise SystemExit(f"the server did not answer on {off.base} within {wait:.0f}s")
    if body.get("store") != "postgres" or body.get("degraded"):
        stop_server(proc)
        raise SystemExit(
            f"the server is on {body.get('store')!r}, degraded={body.get('degraded')!r}: "
            f"the offline database was not usable. Check {off.root / 'logs' / 'server.log'}.")
    return proc


def stop_server(proc: subprocess.Popen, grace: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace)


# -- entry points ------------------------------------------------------------

def describe(off: Offline) -> str:
    lines = [f"database  {off.masked()}",
             f"server    {off.url}  ({'bearer required' if off.token else 'open: loopback only'})",
             f"root      {off.root}",
             f"deadline  {off.deadline:.0f}s per tool call; harvests unbounded, linger 0",
             "", "environment handed to the server:"]
    for key, value in sorted(environment(off).items()):
        if not key.startswith(("DOCSFORGE_", "DATABASE_URL", "PORT")):
            continue
        if key in ("DOCSFORGE_DB", "DATABASE_URL") and value:
            value = re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", value)
        elif key.endswith(("TOKEN", "KEY", "PASSWORD")) and value:
            value = "***"
        lines.append(f"  {key}={value}")
    return "\n".join(lines)


def serve(off: Offline, reset: bool) -> int:
    print(describe(off).split("\n\n")[0])
    print(f"database  {ensure_database(off, reset=reset)}")
    proc = start_server(off)
    print(f"server    up: {health(off.base)}")
    print(f"\nConnect Claude Code to it:\n"
          f"  claude mcp add --transport http docsforge-offline {off.url}"
          + (f' --header "Authorization: Bearer {off.token}"' if off.token else "")
          + "\n\nCtrl-C stops it. Harvests in flight are waited for.")
    try:
        while proc.poll() is None:
            time.sleep(1.0)
        print(f"server exited with {proc.returncode}")
        return proc.returncode or 0
    except KeyboardInterrupt:
        print("\nstopping ...")
        stop_server(proc)
        return 0


def bench(off: Offline, reset: bool, run_args) -> int:
    """Start the offline server, run the benchmarks against it, stop it."""
    from . import run as runner
    print(describe(off).split("\n\n")[0])
    print(f"database  {ensure_database(off, reset=reset)}")
    proc = start_server(off)
    print(f"server    up: {health(off.base)}\n")
    try:
        run_args.url = off.url
        run_args.token = off.token
        run_args.allow_writes = True
        run_args.offline = True
        return asyncio.run(runner.run(run_args))
    finally:
        stop_server(proc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m benchmarks.offline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", action="store_true",
                    help="run every benchmark suite against the offline server, then stop it")
    ap.add_argument("--env", action="store_true", help="print the offline settings and exit")
    ap.add_argument("--reset", action="store_true",
                    help="drop and recreate the offline database before starting")
    ap.add_argument("--suite", action="append", default=[], help="with --bench: only this suite")
    ap.add_argument("--case", action="append", default=[], help="with --bench: only these cases")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    off = settings()
    if args.env:
        print(describe(off))
        return 0
    if args.bench:
        from . import run as runner
        run_args = runner.parse([])
        run_args.suite, run_args.case, run_args.repeat = args.suite, args.case, args.repeat
        return bench(off, args.reset, run_args)
    return serve(off, args.reset)


if __name__ == "__main__":
    sys.exit(main())
