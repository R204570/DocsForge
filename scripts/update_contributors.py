"""
Refresh the contributors page's data from GitHub.

    python scripts/update_contributors.py
    python scripts/build_site.py

Asks GitHub's API who has committed to the repository and how often, and
writes scripts/site_src/contributors.json, which build_site.py turns into
/contributors. The page is built from the committed file rather than fetched
when someone visits, so it costs no request to GitHub per visitor, works
offline, and is plain HTML a search engine can read.

What is kept from the existing file: `exclude`, the GitHub accounts left off
the page. Bots (type "Bot") are always left off. So is `claude`: GitHub
attributes the commits Claude Code made under noreply@anthropic.com to
whichever account claims that address, and linking a stranger's profile as a
DocsForge contributor would be wrong.

Set GITHUB_TOKEN to lift the anonymous rate limit of 60 requests an hour.
Not collected by pytest: it needs the network.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys

import requests

REPO = "R204570/DocsForge"
OUT = pathlib.Path(__file__).resolve().parent / "site_src" / "contributors.json"
DEFAULT_EXCLUDE = ["claude"]


def fetch(token: str | None) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    people, page = [], 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{REPO}/contributors",
                         params={"per_page": 100, "page": page}, headers=headers, timeout=20)
        r.raise_for_status()
        batch = r.json()
        people += batch
        if len(batch) < 100:
            return people
        page += 1


def main() -> int:
    try:
        existing = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    exclude = existing.get("exclude", DEFAULT_EXCLUDE)

    contributors = [
        {"login": c["login"], "contributions": c["contributions"],
         "avatar": c["avatar_url"], "profile": c["html_url"]}
        for c in fetch(os.environ.get("GITHUB_TOKEN"))
        if c.get("type") == "User" and c["login"].lower() not in {e.lower() for e in exclude}
    ]
    contributors.sort(key=lambda c: (-c["contributions"], c["login"].lower()))

    data = {
        "_about": "Written by scripts/update_contributors.py from GitHub's contributors API; "
                  "edit `exclude` by hand. Rebuild the site after updating.",
        "updated": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "exclude": exclude,
        "contributors": contributors,
    }
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{len(contributors)} contributors written to {OUT.name}")
    for c in contributors:
        print(f"  {c['login']:<24} {c['contributions']:>5} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
