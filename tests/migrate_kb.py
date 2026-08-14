"""
Move an existing file-based knowledge base into Postgres.

The combined Markdown already has everything Postgres needs — a title, a source
URL and a body per page — so a migration beats re-crawling a site that took ten
minutes the first time.

    python tests/migrate_kb.py                       # migrate every entry
    python tests/migrate_kb.py --from ../knowledge_base
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb_store import FileStore, PostgresStore, parse_page, split_pages

SOURCE = Path(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else None
DSN = os.environ.get("DOCSFORGE_DB") or os.environ.get("DATABASE_URL")


def main() -> int:
    if not DSN:
        print("set DOCSFORGE_DB to the target database first")
        return 1

    here = Path(__file__).resolve().parent.parent
    src = FileStore(SOURCE or (here / "knowledge_base"))
    dest = PostgresStore(DSN)
    dest.migrate()

    technologies, total = src.technologies()
    if not total:
        print(f"nothing stored in {src.location}")
        return 1

    print(f"from {src.location}")
    print(f"  to {dest.kind}://{dest.location}\n")

    # Every version of every technology: a migration that kept only the newest
    # would silently throw away the older docs someone deliberately harvested.
    for tech in technologies:
        for meta in src.versions(tech["name"]):
            label = f"{meta['technology']} {meta['version']}"
            path = Path(meta["file"])
            if not path.exists():
                print(f"  {label}: file missing ({path}) — skipped")
                continue

            _, blocks = split_pages(path.read_text(encoding="utf-8"))
            pages = [parse_page(b) for b in blocks]
            if not pages:
                print(f"  {label}: no pages parsed — skipped")
                continue

            saved = dest.save(
                meta["technology"], meta["version"], meta["source"],
                meta.get("strategy", "migrated"), pages,
                complete=meta.get("complete", True),
            )
            print(f"  {label}: {saved['pages']} pages, {saved['characters']:,} chars")

    print("\nnow in the database:")
    stored, _ = dest.technologies()
    for entry in stored:
        flag = "" if entry["complete"] else "  [INCOMPLETE]"
        print(f"  {entry['name']}: {entry['versions']} version(s), "
              f"{entry['pages']} pages, {entry['characters']:,} chars{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
