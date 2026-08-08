#!/usr/bin/env python3
"""
docsforge — Universal software documentation → Markdown for LLMs.

Detects what KIND of source it is and extracts accordingly:
  - llms.txt / llms-full.txt      (the LLM-native docs standard)
  - OpenAPI / Swagger (JSON/YAML)  → API reference tables
  - sitemap.xml                    → structured crawl
  - GitHub repo                    → README + /docs via API
  - Generic HTML docs site         → readability extraction
  - Raw Markdown / plaintext       → passthrough + cleanup

Usage:
  python docsforge.py https://docs.stripe.com
  python docsforge.py https://api.example.com/openapi.json
  python docsforge.py https://github.com/tiangolo/fastapi
  python docsforge.py https://docs.example.com --crawl --max-pages 50
  python docsforge.py https://site.com --js            # JS-rendered
  python docsforge.py https://site.com --single-file   # one combined .md
"""

import argparse, os, re, sys, time, json, hashlib
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque

import requests

HEADERS = {"User-Agent": "docsforge/1.0"}
TIMEOUT = 25


# ─────────────────────────────────────────────────────────────
# Source detection
# ─────────────────────────────────────────────────────────────
def detect_source(url, session):
    """Return a strategy name based on URL + a cheap probe."""
    u = url.lower()
    host = urlparse(url).netloc

    if "github.com" in host and not u.endswith((".md", ".txt")):
        return "github"
    if u.endswith("llms-full.txt") or u.endswith("llms.txt"):
        return "llms_txt"
    if u.endswith((".yaml", ".yml", ".json")):
        # Could be OpenAPI — check content
        try:
            r = session.get(url, timeout=TIMEOUT)
            if _looks_like_openapi(r.text):
                return "openapi"
        except Exception:
            pass
    if u.endswith("sitemap.xml"):
        return "sitemap"
    if u.endswith((".md", ".markdown", ".txt")):
        return "raw_text"

    # For bare domains, probe for llms.txt (the modern convention)
    if urlparse(url).path.strip("/") == "":
        for candidate in ("llms.txt", "llms-full.txt"):
            probe = urljoin(url, "/" + candidate)
            try:
                r = session.head(probe, timeout=10, allow_redirects=True)
                if r.status_code == 200:
                    return "llms_txt_redirect:" + probe
            except Exception:
                pass
    return "html"


def _looks_like_openapi(text):
    t = text.lstrip()[:2000]
    return ('"openapi"' in t or "openapi:" in t or
            '"swagger"' in t or "swagger:" in t)


# ─────────────────────────────────────────────────────────────
# Strategy: llms.txt (already LLM-ready)
# ─────────────────────────────────────────────────────────────
def handle_llms_txt(url, session, args):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    header = _meta_header(url, "llms.txt")
    return [(url, "llms.txt", header + r.text.strip())]


# ─────────────────────────────────────────────────────────────
# Strategy: OpenAPI / Swagger → readable API reference
# ─────────────────────────────────────────────────────────────
def handle_openapi(url, session, args):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        spec = json.loads(r.text)
    except json.JSONDecodeError:
        import yaml  # pip install pyyaml
        spec = yaml.safe_load(r.text)

    title = spec.get("info", {}).get("title", "API Reference")
    version = spec.get("info", {}).get("version", "")
    desc = spec.get("info", {}).get("description", "")

    lines = [_meta_header(url, "openapi"),
             f"**Version:** {version}\n" if version else "",
             (desc.strip() + "\n\n") if desc else "",
             "## Endpoints\n"]

    for path, methods in sorted(spec.get("paths", {}).items()):
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            summary = op.get("summary", "")
            lines.append(f"### `{method.upper()} {path}`")
            if summary:
                lines.append(f"{summary}\n")
            if op.get("description"):
                lines.append(op["description"].strip() + "\n")

            params = op.get("parameters", [])
            if params:
                lines.append("| Param | In | Type | Required | Description |")
                lines.append("|---|---|---|---|---|")
                for p in params:
                    schema = p.get("schema", {})
                    lines.append(
                        f"| `{p.get('name','')}` | {p.get('in','')} "
                        f"| {schema.get('type','')} | {p.get('required', False)} "
                        f"| {p.get('description','').replace(chr(10),' ')} |"
                    )
                lines.append("")

            # Request body (brief)
            rb = op.get("requestBody", {})
            if rb:
                lines.append("**Request body:** " +
                             ", ".join(rb.get("content", {}).keys()) + "\n")

            # Responses
            resp = op.get("responses", {})
            if resp:
                codes = ", ".join(f"`{c}`" for c in resp.keys())
                lines.append(f"**Responses:** {codes}\n")
            lines.append("")

    return [(url, title, "\n".join(l for l in lines if l is not None))]


# ─────────────────────────────────────────────────────────────
# Strategy: GitHub repo → README + docs via API
# ─────────────────────────────────────────────────────────────
def handle_github(url, session, args):
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("Not a repo URL")
    owner, repo = parts[0], parts[1]
    api = f"https://api.github.com/repos/{owner}/{repo}"

    docs = []
    # README
    rr = session.get(api + "/readme",
                     headers={**HEADERS, "Accept": "application/vnd.github.raw"},
                     timeout=TIMEOUT)
    if rr.status_code == 200:
        docs.append((url, f"{repo} — README",
                     _meta_header(url, "github-readme") + rr.text.strip()))

    # /docs directory markdown files
    tree = session.get(api + "/git/trees/HEAD?recursive=1", timeout=TIMEOUT)
    if tree.status_code == 200:
        for node in tree.json().get("tree", []):
            p = node["path"]
            if p.lower().endswith((".md", ".mdx")) and (
                p.lower().startswith("docs/") or "/docs/" in p.lower()
            ):
                raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{p}"
                fr = session.get(raw, timeout=TIMEOUT)
                if fr.status_code == 200:
                    docs.append((raw, p,
                                 _meta_header(raw, "github-doc") + fr.text.strip()))
                if len(docs) >= args.max_pages:
                    break
    return docs


# ─────────────────────────────────────────────────────────────
# Strategy: raw markdown / text passthrough
# ─────────────────────────────────────────────────────────────
def handle_raw_text(url, session, args):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return [(url, os.path.basename(urlparse(url).path),
             _meta_header(url, "raw") + r.text.strip())]


# ─────────────────────────────────────────────────────────────
# Strategy: generic HTML (with optional crawl / JS)
# ─────────────────────────────────────────────────────────────
STRIP = ["nav", "header", "footer", "aside", "script", "style", "noscript",
         "form", "iframe", "[role=navigation]", "[role=banner]",
         "[role=contentinfo]", ".sidebar", ".navbar", ".toc",
         ".breadcrumb", ".ad", ".cookie", "[aria-hidden=true]"]
CONTENT = ["main", "article", "[role=main]", ".markdown-body",
           ".doc-content", ".content", ".prose", "#content", "#main"]


def _fetch_html(url, session, js):
    if js:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto(url, wait_until="networkidle", timeout=30000)
            html = pg.content()
            b.close()
        return html
    return session.get(url, timeout=TIMEOUT).text


def _html_to_md(html, url):
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string.strip()
             if soup.title and soup.title.string else "Untitled")
    for sel in STRIP:
        for el in soup.select(sel):
            el.decompose()
    main = None
    for sel in CONTENT:
        f = soup.select_one(sel)
        if f and len(f.get_text(strip=True)) > 200:
            main = f
            break
    main = main or soup.body or soup
    body = md(str(main), heading_style="ATX", bullets="-")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, _meta_header(url, "html") + body


def handle_html(url, session, args):
    if args.crawl:
        return _crawl_html(url, session, args)
    html = _fetch_html(url, session, args.js)
    title, doc = _html_to_md(html, url)
    return [(url, title, doc)]


def _crawl_html(start, session, args):
    seen, out = set(), []
    q = deque([start])
    host = urlparse(start).netloc
    while q and len(out) < args.max_pages:
        url = urldefrag(q.popleft())[0]
        if url in seen:
            continue
        seen.add(url)
        try:
            html = _fetch_html(url, session, args.js)
        except Exception as e:
            print(f"  skip {url}: {e}", file=sys.stderr)
            continue
        title, doc = _html_to_md(html, url)
        out.append((url, title, doc))
        print(f"  [{len(out)}] {url}")
        from bs4 import BeautifulSoup
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            link = urldefrag(urljoin(url, a["href"]))[0]
            if urlparse(link).netloc == host and link.startswith("http") \
                    and link not in seen:
                q.append(link)
        time.sleep(args.delay)
    return out


# ─────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────
def _meta_header(url, kind):
    return (f"<!-- source: {url} | type: {kind} | "
            f"scraped: {time.strftime('%Y-%m-%d %H:%M')} -->\n\n")


def _slug(url):
    p = urlparse(url).path.strip("/").replace("/", "-") or "index"
    p = re.sub(r"[^a-zA-Z0-9\-_.]", "", p)
    return (p[:80] or hashlib.md5(url.encode()).hexdigest()[:10])


HANDLERS = {
    "llms_txt": handle_llms_txt,
    "openapi": handle_openapi,
    "github": handle_github,
    "raw_text": handle_raw_text,
    "html": handle_html,
    "sitemap": None,  # handled inline below
}


def handle_sitemap(url, session, args):
    from bs4 import BeautifulSoup
    r = session.get(url, timeout=TIMEOUT)
    soup = BeautifulSoup(r.text, "xml")
    locs = [l.text for l in soup.find_all("loc")][:args.max_pages]
    out = []
    for link in locs:
        try:
            html = _fetch_html(link, session, args.js)
            title, doc = _html_to_md(html, link)
            out.append((link, title, doc))
            print(f"  [{len(out)}] {link}")
            time.sleep(args.delay)
        except Exception as e:
            print(f"  skip {link}: {e}", file=sys.stderr)
    return out


# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default="./docs_md")
    ap.add_argument("--crawl", action="store_true")
    ap.add_argument("--max-pages", type=int, default=25)
    ap.add_argument("--js", action="store_true", help="render JS (needs playwright)")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--single-file", action="store_true")
    ap.add_argument("--force", choices=list(HANDLERS) + ["sitemap"],
                    help="skip detection, force a strategy")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    os.makedirs(args.out, exist_ok=True)

    kind = args.force or detect_source(args.url, session)
    if kind.startswith("llms_txt_redirect:"):
        args.url = kind.split(":", 1)[1]
        kind = "llms_txt"
    print(f"Detected source type: {kind}")

    if kind == "sitemap":
        docs = handle_sitemap(args.url, session, args)
    else:
        docs = HANDLERS[kind](args.url, session, args)

    if args.single_file:
        combined = "\n\n---\n\n".join(d for _, _, d in docs)
        fn = os.path.join(args.out, _slug(args.url) + "-combined.md")
        open(fn, "w", encoding="utf-8").write(combined)
        print(f"  wrote {fn}")
    else:
        for url, title, doc in docs:
            fn = os.path.join(args.out, _slug(url) + ".md")
            open(fn, "w", encoding="utf-8").write(doc)
            print(f"  wrote {fn}")

    print(f"\nDone. {len(docs)} document(s) → {args.out}")


if __name__ == "__main__":
    main()
