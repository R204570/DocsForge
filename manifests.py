"""
Read a project's dependency manifests.

The registry can tell you where a library documents itself. Only the project
can tell you *which version of it this codebase actually uses* — and that is
the difference between answering from the right manual and the wrong one. Ask
for "pydantic" and you get whatever was harvested last; a project pinned to
1.10 needs 1.10, and 1.10 and 2.11 contradict each other on the basics.

Nothing here touches the network, and nothing is executed: manifests are
parsed as data, never imported or evaluated.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: Manifests worth reading, and which registry their names belong to.
MANIFESTS = {
    "package.json": "npm",
    "pyproject.toml": "pypi",
    "requirements.txt": "pypi",
    "Cargo.toml": "crates",
    "go.mod": "go",
}

#: Directories never worth walking into when looking for manifests.
SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
        "target", ".next", ".nuxt", "vendor", ".claude"}

#: A dependency list is for orienting a model, not for auditing a supply chain.
MAX_DEPS = 300


@dataclass
class Dep:
    name: str
    version: str          # as written: "^3.1.0", "==1.10.13", "" if unpinned
    ecosystem: str
    manifest: str         # the file it came from, relative to the root

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "ecosystem": self.ecosystem, "manifest": self.manifest}


def pinned_version(spec: str) -> str:
    """The concrete version a spec points at, as far as one can be read off.

    Only the number matters for choosing documentation, and only its leading
    parts: docs are published per major.minor far more often than per patch.
    """
    match = re.search(r"(\d+(?:\.\d+)*)", spec or "")
    return match.group(1) if match else ""


def doc_versions(spec: str) -> list[str]:
    """Version labels to try against a docs site, most specific first.

    A project pinned to 1.10.13 will not find docs under that exact string;
    sites publish /1.10/ or /v1/. Trying widest-last keeps the answer as
    precise as the site allows.
    """
    number = pinned_version(spec)
    if not number:
        return []
    parts = number.split(".")
    out = []
    for take in (3, 2, 1):
        if len(parts) >= take:
            label = ".".join(parts[:take])
            for form in (label, f"v{label}"):
                if form not in out:
                    out.append(form)
    return out


# ── individual formats ───────────────────────────────────────
def _package_json(text: str, where: str) -> list[Dep]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, str):
                out.append(Dep(name, spec, "npm", where))
    return out


def _pyproject(text: str, where: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    out = []

    # PEP 621
    for entry in (data.get("project") or {}).get("dependencies") or []:
        if isinstance(entry, str):
            name = re.split(r"[<>=!~\[; ]", entry.strip(), maxsplit=1)[0]
            if name:
                out.append(Dep(name, entry[len(name):].strip(), "pypi", where))

    # Poetry
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        if isinstance(spec, dict):
            spec = spec.get("version", "")
        out.append(Dep(name, spec if isinstance(spec, str) else "", "pypi", where))
    return out


def _requirements(text: str, where: str) -> list[Dep]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        # -r other.txt, -e ., --index-url … are instructions, not dependencies.
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            out.append(Dep(name, line[len(name):].strip(), "pypi", where))
    return out


def _cargo(text: str, where: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    out = []
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, dict):
                spec = spec.get("version", "")
            out.append(Dep(name, spec if isinstance(spec, str) else "", "crates", where))
    return out


def _gomod(text: str, where: str) -> list[Dep]:
    out, inside = [], False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("require ("):
            inside = True
            continue
        if inside and line == ")":
            inside = False
            continue
        if line.startswith("require "):
            line = line[len("require "):].strip()
        elif not inside:
            continue
        parts = line.split()
        if len(parts) >= 2 and "." in parts[0]:
            out.append(Dep(parts[0], parts[1], "go", where))
    return out


PARSERS = {
    "package.json": _package_json,
    "pyproject.toml": _pyproject,
    "requirements.txt": _requirements,
    "Cargo.toml": _cargo,
    "go.mod": _gomod,
}


# ── walking a project ────────────────────────────────────────
def find_manifests(root: Path, depth: int = 3) -> list[Path]:
    """Manifests at or near the project root.

    Bounded on purpose: a monorepo can hold hundreds, and node_modules holds
    one per package. The point is to learn what this project uses, not to
    enumerate the world.
    """
    root = Path(root)
    found: list[Path] = []
    if not root.is_dir():
        return found

    def walk(folder: Path, level: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and entry.name in MANIFESTS:
                found.append(entry)
            elif (entry.is_dir() and level < depth
                  and entry.name not in SKIP and not entry.name.startswith(".")):
                walk(entry, level + 1)

    walk(root, 0)
    return found


def read_project(root: Path | str, depth: int = 3) -> list[Dep]:
    """Every declared dependency of a project, de-duplicated by name."""
    root = Path(root)
    seen: dict[str, Dep] = {}
    for path in find_manifests(root, depth):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            where = str(path.relative_to(root))
        except ValueError:
            where = path.name
        for dep in PARSERS[path.name](text, where):
            # A pinned version beats an unpinned mention of the same package.
            old = seen.get(dep.name)
            if old is None or (not pinned_version(old.version) and pinned_version(dep.version)):
                seen[dep.name] = dep
            if len(seen) >= MAX_DEPS:
                return list(seen.values())
    return list(seen.values())
