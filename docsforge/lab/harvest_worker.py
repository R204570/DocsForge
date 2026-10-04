"""
One harvest test, in a process of its own.

    python -m docsforge.lab.harvest_worker <run dir>

The run directory holds `spec.json` -- `{"tool": ..., "args": {...}}` -- and
the parent has pointed every store this process could write to inside it (see
`runner._child_env`): the knowledge base, the resolution cache, the harvest
records, the log. So the tool runs exactly as a model's call would, and
nothing it stores reaches the real knowledge base.

It speaks to the parent on stdout, one JSON message per line after the
`runner.MARK` prefix: every trace event as it happens (so the lab shows the
harvest live), then the text the tool returned. Whatever else is printed --
the engine's own progress -- is the run's log. What was stored is measured
page by page into `result.json`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from docsforge.lab.runner import MARK

EVENT_OUTPUT = 2000
TOOL_OUTPUT = 250_000

_out = threading.Lock()


def emit(message: dict) -> None:
    line = MARK + json.dumps(message, ensure_ascii=False, default=str)
    with _out:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _on_event(trace, event) -> None:
    data = event.as_dict()
    text = data.get("output") or ""
    if data.get("parent_id") is None:
        data.pop("output", None)            # the call's own output comes whole, below
    elif len(text) > EVENT_OUTPUT:
        data["output"] = text[:EVENT_OUTPUT]
        data["omitted"] = (data.get("omitted") or 0) + len(text) - EVENT_OUTPUT
    emit({"type": "event", "trace_id": trace.id, "event": data})


def summarise(kb_root: Path) -> dict:
    """Every version stored under `kb_root`, and every page measured."""
    from docsforge.lab import quality
    from docsforge.store.kb_store import FileStore, parse_page, split_pages

    store = FileStore(kb_root)
    entries, pages = [], []
    techs, _ = store.technologies()
    for tech in techs:
        for entry in store.versions(tech["name"]):
            entries.append({k: entry.get(k) for k in (
                "technology", "version", "source", "strategy", "pages", "characters",
                "complete", "expected", "harvested")})
            path = Path(entry["file"])
            if not path.exists():
                continue
            _, blocks = split_pages(path.read_text(encoding="utf-8"))
            for ordinal, block in enumerate(blocks, 1):
                title, url, body = parse_page(block)
                metrics = quality.assess(body)
                pages.append({"technology": entry["technology"], "version": entry["version"],
                              "ordinal": ordinal, "title": title, "url": url, **metrics,
                              "flags": quality.page_flags(metrics)})
    totals = {k: sum(p[k] for p in pages) for k in
              ("chars", "fences", "collapsed_fences", "relative_links", "chrome_lines",
               "permalink_marks")}
    totals["pages"] = len(pages)
    totals["thin_pages"] = sum(1 for p in pages if p["chars"] < quality.THIN)
    totals["clean_pages"] = sum(1 for p in pages if quality.clean(p))
    return {"entries": entries, "pages": pages, "totals": totals}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    where = Path(argv[0])
    spec = json.loads((where / "spec.json").read_text(encoding="utf-8"))

    kb_root = os.environ.get("DOCSFORGE_KB_ROOT", "")
    if os.environ.get("DOCSFORGE_DB") or os.environ.get("DATABASE_URL") or not kb_root:
        # Run by hand without the runner's environment: this would harvest
        # into whatever store .env names. Refuse rather than find out.
        emit({"type": "output", "ok": False, "trace_id": "", "seconds": 0,
              "text": "Error: the harvest worker runs only with a store of its own "
                      "(DOCSFORGE_KB_ROOT set, DOCSFORGE_DB and DATABASE_URL empty)."})
        return 2

    from docsforge.tools import forge_tools, tracing

    tracing.on_event(_on_event)
    started = time.time()
    try:
        text, ok = forge_tools.run_tool_checked(spec["tool"], spec.get("args") or {})
    except BaseException as e:                          # noqa: BLE001 -- reported
        text, ok = f"Error: {type(e).__name__}: {e}", False
    emit({"type": "output", "ok": ok, "text": text[:TOOL_OUTPUT],
          "omitted": max(0, len(text) - TOOL_OUTPUT),
          "trace_id": forge_tools.last_trace_id() or "",
          "seconds": round(time.time() - started, 1)})
    try:
        summary = summarise(Path(kb_root))
    except Exception as e:                              # noqa: BLE001 -- reported
        summary = {"entries": [], "pages": [], "totals": {},
                   "error": f"could not read what was stored: {type(e).__name__}: {e}"}
    (where / "result.json").write_text(json.dumps(summary, default=str), encoding="utf-8")
    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
