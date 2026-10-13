"""
Tell IndexNow the site's pages changed, so Bing (and Yandex, Seznam, Naver)
recrawl them now rather than whenever they next get round to it.

Run after a deploy that changed a page. The key must already be served by
the deployment — set DOCSFORGE_INDEXNOW_KEY in the platform's environment
and redeploy first; the site then answers `/<key>.txt` with the key, which
is how IndexNow checks the submission came from the site's owner.

    DOCSFORGE_INDEXNOW_KEY=<key> python scripts/indexnow.py
    DOCSFORGE_INDEXNOW_KEY=<key> python scripts/indexnow.py --dry-run

The URLs submitted are the ones the live sitemap lists, so what is announced
is exactly what is deployed. Not collected by pytest: it needs the network.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from docsforge import SITE_URL  # noqa: E402

ENDPOINT = "https://api.indexnow.org/indexnow"
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def sitemap_urls(site: str) -> list[str]:
    r = requests.get(f"{site}/sitemap.xml", timeout=20)
    r.raise_for_status()
    return [loc.text.strip() for loc in ET.fromstring(r.content).findall("s:url/s:loc", NS)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=SITE_URL, help=f"the deployed site (default {SITE_URL})")
    ap.add_argument("--dry-run", action="store_true", help="check the key and list the URLs only")
    args = ap.parse_args(argv)

    key = (os.environ.get("DOCSFORGE_INDEXNOW_KEY") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
        print("DOCSFORGE_INDEXNOW_KEY must be set to 8-128 letters, digits or dashes.",
              file=sys.stderr)
        return 2
    site = args.site.rstrip("/")
    key_location = f"{site}/{key}.txt"

    served = requests.get(key_location, timeout=20)
    if served.status_code != 200 or served.text.strip() != key:
        print(f"{key_location} does not serve the key (HTTP {served.status_code}). Set "
              "DOCSFORGE_INDEXNOW_KEY on the deployment and redeploy first.", file=sys.stderr)
        return 1

    urls = sitemap_urls(site)
    print(f"{len(urls)} URLs from {site}/sitemap.xml:")
    for url in urls:
        print(f"  {url}")
    if args.dry_run:
        return 0

    r = requests.post(ENDPOINT, timeout=20, json={
        "host": urlparse(site).netloc, "key": key, "keyLocation": key_location, "urlList": urls})
    # 200: accepted. 202: accepted, key still being validated.
    print(f"IndexNow answered HTTP {r.status_code}{': ' + r.text if r.text else ''}")
    return 0 if r.status_code in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
