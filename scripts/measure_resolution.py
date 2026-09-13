"""Measure name -> URL resolution, cold, for a fixed set of names.

Writes JSON so two runs can be diffed. Points the resolution cache at a
throwaway file: every name is resolved from scratch, and the developer's own
cache is neither read nor written.

    python scripts/measure_resolution.py before.json
    python scripts/measure_resolution.py after.json
    python scripts/measure_resolution.py --diff before.json after.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
sys.path.insert(0, ROOT)

# Cold cache, and never the developer's own.
os.environ["DOCSFORGE_RESOLVE_CACHE"] = os.path.join(
    tempfile.mkdtemp(prefix="resolve-measure-"), "resolutions.json")

from docsforge.core import resolver  # noqa: E402

#: name -> what a correct answer looks like (substring of the URL), or None
#: where nobody has established one. `expected` is judged by hand and is the
#: only opinion in this file.
NAMES = {
    # The five this work exists for.
    "langchain":   "docs.langchain.com",
    "langgraph":   "langchain.com",        # docs.langchain.com/.../langgraph
    "django":      "docs.djangoproject.com",
    "tensorflow":  "tensorflow.org",
    "pytorch":     "pytorch.org",

    # Regression set: names that resolved correctly before this work and must
    # keep doing so. Drawn from measure.py's list and the store's contents.
    "fastapi":     "fastapi.tiangolo.com",
    "pydantic":    "pydantic.dev",
    "astro":       "astro.build",
    "effect":      "effect.website",
    "htmx":        "htmx.org",
    "kubernetes":  "kubernetes.io",
    "terraform":   None,                   # hashicorp consolidation
    "mojo":        "mojolang.org",
    "zig":         "ziglang.org",
    "nim":         "nim-lang.org",
    "svelte":      "svelte.dev",
    "vite":        "vite.dev",
    "vue":         "vuejs.org",
    "tokio":       "tokio.rs",
    "serde":       "serde.rs",
    "prisma":      "prisma.io",
    "tailwindcss": "tailwindcss.com",
    "sqlalchemy":  "sqlalchemy.org",
    "requests":    None,
    "numpy":       "numpy.org",

    # Known-wrong before this work (FINDINGS-C). Not required to be fixed;
    # recorded so a change that fixes or worsens them is visible.
    "flask":       "palletsprojects.com",
    "polars":      "pola.rs",
}


def run() -> dict:
    out = {"at": time.time(), "results": {}}
    for name, expected in NAMES.items():
        started = time.time()
        try:
            res = resolver.resolve(name, use_memory=False)
            best = res.best.url if res.best else None
            row = {
                "best": best,
                "via": res.resolved_via,
                "reason": res.best.reason if res.best else (res.note or ""),
                "signals": list(res.best.signals) if res.best else [],
                "expected": expected,
                "ok": bool(best and expected and expected in best),
                "seconds": round(time.time() - started, 1),
                "candidates": [
                    {"url": c.url, "source": c.source, "verified": c.verified,
                     "signals": list(c.signals)}
                    for c in res.candidates
                ],
            }
        except Exception as e:                                  # noqa: BLE001
            row = {"best": None, "error": f"{type(e).__name__}: {e}",
                   "expected": expected, "ok": False,
                   "seconds": round(time.time() - started, 1),
                   "candidates": []}
        out["results"][name] = row
        mark = "ok  " if row.get("ok") else ("--  " if expected is None else "WRONG")
        print(f"{mark} {name:12s} {str(row.get('best'))[:64]:64s} "
              f"{row.get('seconds')}s", flush=True)
    return out


def table(data: dict) -> None:
    ok = sum(1 for r in data["results"].values() if r.get("ok"))
    judged = sum(1 for r in data["results"].values() if r.get("expected"))
    print(f"\n{ok}/{judged} names resolved to the expected documentation")


def diff(a: dict, b: dict) -> None:
    print(f"{'name':14s} {'before':44s} {'after':44s}")
    print("-" * 104)
    changed = fixed = broken = 0
    for name in a["results"]:
        ra, rb = a["results"][name], b["results"].get(name, {})
        ua, ub = str(ra.get("best")), str(rb.get("best"))
        if ua == ub:
            continue
        changed += 1
        if rb.get("ok") and not ra.get("ok"):
            fixed += 1
            tag = "FIXED  "
        elif ra.get("ok") and not rb.get("ok"):
            broken += 1
            tag = "BROKEN "
        else:
            tag = "moved  "
        print(f"{tag}{name:14s} {ua[:44]:44s} {ub[:44]:44s}")
    oka = sum(1 for r in a["results"].values() if r.get("ok"))
    okb = sum(1 for r in b["results"].values() if r.get("ok"))
    print("-" * 104)
    print(f"{changed} changed, {fixed} fixed, {broken} broken")
    print(f"expected-match: {oka} -> {okb}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--diff"]:
        with open(sys.argv[2], encoding="utf-8") as fh:
            a = json.load(fh)
        with open(sys.argv[3], encoding="utf-8") as fh:
            b = json.load(fh)
        diff(a, b)
    else:
        data = run()
        table(data)
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        print(f"\nwrote {sys.argv[1]}")
