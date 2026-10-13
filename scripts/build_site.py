"""
Build the public site from its sources.

The three pages under docsforge/server/site/ are hand-authored in
scripts/site_src/: one HTML file per page, one stylesheet (site.css), one
behaviour script (site.js), and the client picker for /connect
(picker.html). This assembles them into what the server serves: the shared
nav and footer stamped into each page, the stylesheet and script inlined so
every page is self-contained (fonts still come from Google Fonts, with a
system fallback, and the Google tag is the one outside script), and two
placeholders left for the server to fill at
request time — `{{VERSION}}` from the package and `{{BASE_URL}}` from the
request. The logo and favicon are the site's only files besides the pages:
copied from Logo/ at the repository root to site/logo.svg and
site/favicon.ico, which every DocsForge page asks for by those two paths,
and the social-sharing card, Logo/docsforge-og.png, copied to site/og.png.

Beside the pages it writes the files search engines and AI agents read, as
plain files the server hands out unchanged: robots.txt, sitemap.xml (each
page's <lastmod> moves only when that page's built HTML changed),
site.webmanifest and the home-screen icons, and llms.txt, the site described
for language models. The not-found page, 404.html, is built from the same
sources but marked noindex.

Search metadata is stamped here too, from each page's own <title> and meta
description so the two can never disagree: the canonical URL, Open Graph and
Twitter card tags, and Schema.org JSON-LD. The developer's links live in
DEVELOPER below and nowhere else; the footer and the home page's "Built by"
section are both drawn from them. A third placeholder,
`{{SITE_VERIFICATION}}`, is filled by the server from the environment — see
"System Files/SEO.md".

    python scripts/build_site.py

No toolchain: plain files in, plain files out. Re-run after any edit.
"""

from __future__ import annotations

import datetime
import html as htmllib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "site_src"))
sys.path.insert(0, str(ROOT))
from diagrams import DIAGRAMS  # noqa: E402

from docsforge import SITE_URL  # noqa: E402

REPO = "https://github.com/R204570/DocsForge"
SRC = ROOT / "scripts" / "site_src"
SITE = ROOT / "docsforge" / "server" / "site"

PAGES = ("index", "tools", "connect")
#: Where each page is served, for its canonical URL.
PATHS = {"index": "/", "tools": "/tools", "connect": "/connect"}

#: The person behind DocsForge. `rel="me"` on these links, and `sameAs` in the
#: structured data, tie the three profiles to the site.
DEVELOPER = {
    "name": "Raj Patel",
    "links": (("Portfolio", "https://portfolio-raj-25.netlify.app/"),
              ("LinkedIn", "https://www.linkedin.com/in/raj-patel5/"),
              ("GitHub", "https://github.com/R204570")),
}

#: The home page's questions and answers, shown on the page and repeated as
#: FAQPage structured data, so the markup can never say what the page does
#: not. `code` in backticks is set as <code> on the page and plain in the data.
FAQ = (
    ("What is DocsForge?",
     "DocsForge is an open-source documentation MCP server. Give it the name of a "
     "library, framework or tool and it finds the official documentation through the "
     "package registries, verifies that the page really documents that library, "
     "harvests the whole set as Markdown, and serves it to your AI coding assistant "
     "through MCP tools."),
    ("Who is it for?",
     "Developers whose AI coding assistant writes code against libraries newer than "
     "its training data, or mixes up their versions; AI engineers building agents that "
     "must answer from real documentation rather than from memory; and teams that want "
     "one shared, searchable store of the docs their projects depend on."),
    ("What is an MCP server, and which tools does DocsForge provide?",
     "The Model Context Protocol (MCP) is an open standard that lets an AI assistant "
     "call tools on a server. DocsForge provides thirteen: `learn_technology` learns a "
     "library from its name alone, `search_knowledge_base` and `read_knowledge_base` "
     "answer from what is stored, `scan_project` reports a project's dependencies and "
     "which are already documented, and the rest fetch, harvest and manage "
     "documentation. They are served over Streamable HTTP or stdio."),
    ("Which AI coding assistants does it work with?",
     "Any MCP client. The connect page has a ready command or config block for Claude "
     "Code, Codex, ChatGPT, Cursor, Windsurf, Antigravity, Gemini CLI, VS Code and "
     "Claude Desktop."),
    ("How does it avoid handing the model the wrong documentation?",
     "A page must name the package repeatedly before it is trusted; if nothing "
     "verifies, DocsForge says so and asks for a URL instead of guessing. Versions are "
     "stored apart, so v2 and v3 are never mixed, and a harvest that stopped early is "
     "marked incomplete every time it is read."),
    ("Does it need an API key?",
     "Not to resolve a name: npm, PyPI and crates.io are asked without a key. A hosted "
     "DocsForge asks clients for a bearer token; running it yourself on your own "
     "machine needs none."),
    ("Is DocsForge free and open source?",
     "Yes. DocsForge is MIT-licensed. Install it with `pipx install docsforge`, run it "
     "on your own machine or host it yourself, and keep harvested documentation as "
     "Markdown files or in PostgreSQL."),
)

#: Logo/ holds the brand as it was drawn; the server serves these copies.
BRAND = {"logo.svg": ROOT / "Logo" / "docsforge-logo.svg",
         "favicon.ico": ROOT / "Logo" / "docsforge-logo.ico",
         "og.png": ROOT / "Logo" / "docsforge-og.png",
         "apple-touch-icon.png": ROOT / "Logo" / "docsforge-icon-180.png",
         "icon-192.png": ROOT / "Logo" / "docsforge-icon-192.png",
         "icon-512.png": ROOT / "Logo" / "docsforge-icon-512.png"}
#: Pages served but never indexed: the not-found page.
UNLISTED = ("404",)
#: The social card is 1200 x 630, the size every platform crops to.
OG_IMAGE = (f"{SITE_URL}/og.png", 1200, 630)
ICON = '<link rel="icon" href="/favicon.ico" sizes="any">'
#: Home-screen icons and the web app manifest that lists them.
HOME_ICONS = ('<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
              '  <link rel="manifest" href="/site.webmanifest">')
#: What the not-found page says to search engines in place of the SEO block.
NOINDEX = '<meta name="robots" content="noindex">'
#: The fox is 795 x 623; width and height are set so nothing shifts as it loads.
LOGO = '<img src="/logo.svg" alt="" width="{w}" height="{h}">'


def nav(active: str) -> str:
    """One nav for every page. `active` is the page's file name."""
    items = [("How it works", "/#how-it-works", "wide", ""),
             ("Tools", "/tools", "", "tools"),
             ("Connect", "/connect", "", "connect"),
             ("Storage", "/connect#storage", "wide", "")]
    links = []
    for label, href, cls, page in items:
        on = page == active
        attrs = f' class="{cls}"' if cls else ""
        attrs += ' aria-current="page"' if on else ""
        links.append(f'      <a href="{href}"{attrs}>{label}</a>')
    links.append(f'      <a href="{REPO}" class="ext" target="_blank" rel="noopener noreferrer">GitHub '
                 '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 8 L8 2 M3.5 2 H8 V6.5"/></svg></a>')
    return ('<header class="top">\n  <div class="wrap">\n'
            '    <a href="/" class="mark">' + LOGO.format(w=31, h=24) + 'DocsForge</a>\n'
            '    <nav class="nav" aria-label="Site">\n' + "\n".join(links) + '\n    </nav>\n'
            '    <span class="chip">v{{VERSION}}</span>\n'
            '  </div>\n</header>')


def external(label: str, href: str, rel: str = "noopener noreferrer") -> str:
    return f'<a href="{href}" target="_blank" rel="{rel}">{label}</a>'


def shown(href: str) -> str:
    """A profile URL as a person reads it: no scheme, no www, no trailing slash."""
    return re.sub(r"^https://(www\.)?|/$", "", href)


ME = "me noopener noreferrer"
DEV_NAV = "\n        ".join(external(label, href, ME) for label, href in DEVELOPER["links"])

FOOT = f'''<footer class="foot">
  <div class="wrap">
    <div class="l"><b>{LOGO.format(w=23, h=18)}DocsForge</b><span>MIT License</span><a href="/#developer">Built by {DEVELOPER["name"]}</a><span class="note">Bugs, bad extractions, ideas — open an issue.</span></div>
    <div class="r">
      <nav aria-label="Project">
        {external("GitHub", REPO)}
        {external("Issues", REPO + "/issues")}
        {external("Contribute", REPO + "/blob/main/CONTRIBUTING.md")}
      </nav>
      <nav aria-label="Developer">
        {DEV_NAV}
      </nav>
    </div>
  </div>
</footer>'''


def developer() -> str:
    """The home page's "Built by" section."""
    links = "\n".join(
        f'        <a class="btn btn-ghost" href="{href}" target="_blank" rel="{ME}">'
        f'{label}<span>{htmllib.escape(shown(href))}</span></a>'
        for label, href in DEVELOPER["links"])
    return f'''<section class="band" id="developer" style="padding-top:0">
    <div class="dev rv">
      <div>
        <h2>Built by {DEVELOPER["name"]}</h2>
        <p class="prose" style="margin-top:12px">DocsForge is designed, built and maintained by {DEVELOPER["name"]}. Found a bug or a bad extraction, have an idea, or want to contribute? Open an issue on GitHub, or reach out directly.</p>
      </div>
      <div class="dev-links">
{links}
      </div>
    </div>
  </section>'''


def code_html(text: str) -> str:
    """Escaped for the page, with `backticks` set as <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", htmllib.escape(text))


def code_plain(text: str) -> str:
    return text.replace("`", "")


def faq() -> str:
    """The home page's FAQ section, from FAQ."""
    items = "\n".join(
        f'      <div class="qa">\n        <h3>{htmllib.escape(q)}</h3>\n'
        f'        <p>{code_html(a)}</p>\n      </div>'
        for q, a in FAQ)
    return f'''<section class="band" id="faq" style="padding-top:0">
    <h2 class="rv">Frequently asked questions</h2>
    <div class="faq rv-list">
{items}
    </div>
  </section>'''


#: The Google tag, stamped first in every <head>: the one script a page fetches
#: from elsewhere.
GTAG_ID = "G-KTBKMGZM75"
GTAG_SRC = f"https://www.googletagmanager.com/gtag/js?id={GTAG_ID}"
GTAG = f'''<!-- Google tag (gtag.js) -->
  <script async src="{GTAG_SRC}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());

    gtag('config', '{GTAG_ID}');
  </script>'''


def structured_data(page: str, title: str, description: str) -> dict:
    """Schema.org JSON-LD: the site, its author, the software, and this page.

    A Person, not an Organization: DocsForge is one developer's project, and
    the structured data says only what the page itself says.
    """
    url = SITE_URL + PATHS[page]
    person = {"@type": "Person", "@id": f"{SITE_URL}/#developer", "name": DEVELOPER["name"],
              "url": DEVELOPER["links"][0][1], "sameAs": [h for _, h in DEVELOPER["links"]]}
    website = {"@type": "WebSite", "@id": f"{SITE_URL}/#website", "url": SITE_URL + "/",
               "name": "DocsForge", "inLanguage": "en",
               "publisher": {"@id": person["@id"]}}
    software = {"@type": "SoftwareApplication", "@id": f"{SITE_URL}/#software",
                "name": "DocsForge", "url": SITE_URL + "/",
                "applicationCategory": "DeveloperApplication",
                "operatingSystem": "Windows, macOS, Linux",
                "description": "An open-source MCP server that finds a library's official "
                               "documentation from its name, verifies it, harvests it and "
                               "serves it to AI agents and coding assistants.",
                "license": "https://opensource.org/licenses/MIT",
                "codeRepository": REPO, "isAccessibleForFree": True,
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                "author": {"@id": person["@id"]}, "image": OG_IMAGE[0]}
    webpage = {"@type": "WebPage", "@id": url, "url": url, "name": title,
               "description": description, "isPartOf": {"@id": website["@id"]},
               "about": {"@id": software["@id"]},
               "primaryImageOfPage": {"@type": "ImageObject", "url": OG_IMAGE[0]}}
    graph = [website, person, software, webpage]
    if page == "index":
        graph.append({"@type": "FAQPage", "@id": f"{url}#faq", "isPartOf": {"@id": url},
                      "mainEntity": [{"@type": "Question", "name": q,
                                      "acceptedAnswer": {"@type": "Answer", "text": code_plain(a)}}
                                     for q, a in FAQ]})
    else:
        webpage["breadcrumb"] = {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "DocsForge", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": page.capitalize(), "item": url}]}
    return {"@context": "https://schema.org", "@graph": graph}


def seo(page: str, html: str) -> str:
    """Canonical, social and structured metadata, from the page's own title
    and description."""
    title = htmllib.unescape(re.search(r"<title>(.*?)</title>", html).group(1))
    description = htmllib.unescape(
        re.search(r'<meta name="description" content="([^"]*)">', html).group(1))
    url = SITE_URL + PATHS[page]
    image, w, h = OG_IMAGE
    # double-quoted attributes: `"` must be escaped, an apostrophe need not be
    q = lambda v: htmllib.escape(str(v), quote=False).replace('"', "&quot;")  # noqa: E731
    ld = json.dumps(structured_data(page, title, description), ensure_ascii=False,
                    separators=(",", ":")).replace("</", "<\\/")
    return "\n  ".join([
        f'<link rel="canonical" href="{q(url)}">',
        '<meta name="robots" content="index, follow, max-image-preview:large">',
        f'<meta name="author" content="{q(DEVELOPER["name"])}">',
        '<meta name="theme-color" content="#0b0b0d">',
        HOME_ICONS,
        "{{SITE_VERIFICATION}}",
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="DocsForge">',
        '<meta property="og:locale" content="en_US">',
        f'<meta property="og:title" content="{q(title)}">',
        f'<meta property="og:description" content="{q(description)}">',
        f'<meta property="og:url" content="{q(url)}">',
        f'<meta property="og:image" content="{q(image)}">',
        f'<meta property="og:image:width" content="{w}">',
        f'<meta property="og:image:height" content="{h}">',
        '<meta property="og:image:alt" content="DocsForge — documentation for technologies '
        'your model was never trained on.">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{q(title)}">',
        f'<meta name="twitter:description" content="{q(description)}">',
        f'<meta name="twitter:image" content="{q(image)}">',
        f'<script type="application/ld+json">{ld}</script>',
    ])


def build(page: str, css: str, js: str, picker: str) -> str:
    listed = page in PATHS
    html = (SRC / f"{page}.html").read_text(encoding="utf-8")
    html = html.replace("<!--SEO-->", seo(page, html) if listed else f"{NOINDEX}\n  {HOME_ICONS}")
    html = html.replace("<!--DEVELOPER-->", developer() if page == "index" else "")
    html = html.replace("<!--FAQ-->", faq() if page == "index" else "")
    for name, draw in DIAGRAMS.items():
        html = html.replace(f"<!--SVG:{name}-->", draw())
    assert "<!--SVG:" not in html, (page, "an unknown diagram marker")
    for marker, value in (("<!--GTAG-->", GTAG),
                          ("<!--STYLE-->", f"<style>\n{css}\n  </style>"),
                          ("<!--NAV-->", nav(page)),
                          ("<!--FOOT-->", FOOT),
                          ("<!--PICKER-->", picker if page == "connect" else ""),
                          ("<!--SCRIPT-->", f"<script>\n{js}\n</script>")):
        assert html.count(marker) == (1 if marker != "<!--PICKER-->" or page == "connect" else 0) \
            or marker == "<!--PICKER-->", (page, marker)
        html = html.replace(marker, value)
    # what must hold for every served page
    assert re.findall(r'<script\b[^>]*\bsrc="([^"]*)"', html) == [GTAG_SRC], \
        (page, "an external script other than the Google tag")
    assert "{{VERSION}}" in html, (page, "version chip placeholder")
    assert ("{{BASE_URL}}" in html) == (page == "connect"), (page, "base url placeholder")
    assert html.count(ICON) == 1, (page, "the favicon link")
    assert html.count(HOME_ICONS) == 1, (page, "the home-screen icons")
    assert ('id="developer"' in html) == (page == "index"), (page, "the developer section")
    assert ('id="faq"' in html) == (page == "index"), (page, "the FAQ section")
    assert len(re.findall(r"<h1\b", html)) == 1, (page, "exactly one h1")
    if not listed:
        assert NOINDEX in html and '<link rel="canonical"' not in html, (page, "unlisted")
        return html
    assert html.count('<link rel="canonical"') == 1, (page, "the canonical link")
    assert html.count("{{SITE_VERIFICATION}}") == 1, (page, "site verification placeholder")
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    description = re.search(r'<meta name="description" content="([^"]*)">', html).group(1)
    assert 10 <= len(title) <= 65, (page, "title length", len(title))
    assert 70 <= len(description) <= 170, (page, "description length", len(description))
    assert 'href="#"' not in html, (page, "unresolved href")
    assert "YOUR-HOST" not in html, (page, "host placeholder")
    return html


def manifest() -> str:
    """The web app manifest: the name and icons a home screen shows."""
    return json.dumps({
        "name": "DocsForge", "short_name": "DocsForge",
        "description": "Open-source documentation MCP server for AI agents and coding assistants.",
        "start_url": "/", "display": "browser",
        "background_color": "#0b0b0d", "theme_color": "#0b0b0d",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, indent=2) + "\n"


def llms_txt() -> str:
    """/llms.txt — the site described for language models (llmstxt.org): what
    DocsForge is, and where each part of it is documented. The tool names
    come from the tools page itself, so the two cannot drift apart."""
    tools = re.findall(r'class="name">([a-z_]+)<', (SRC / "tools.html").read_text(encoding="utf-8"))
    return f"""# DocsForge

> An open-source documentation MCP server. Given the name of a library, framework or tool, DocsForge finds its official documentation through the package registries, verifies that the page really documents it, harvests the whole set as Markdown, and serves it to AI agents and coding assistants over the Model Context Protocol, with a coverage figure that says when a harvest is incomplete.

Built by {DEVELOPER["name"]} ({DEVELOPER["links"][0][1]}). MIT licensed. Install with `pipx install docsforge`.

Its {len(tools)} MCP tools: {", ".join(tools)}.

## Site

- [Home]({SITE_URL}/): how a name is resolved, verified and harvested, coverage reporting, and a FAQ
- [Tools]({SITE_URL}/tools): every MCP tool, its arguments and what it returns
- [Connect]({SITE_URL}/connect): one command or config block per MCP client (Claude Code, Codex, ChatGPT, Cursor, Windsurf, Antigravity, Gemini CLI, VS Code, Claude Desktop), running it yourself, and storage

## Source

- [README]({REPO}#readme): installation, the CLI, the MCP server, the web chat, and deployment
- [Contributing]({REPO}/blob/main/CONTRIBUTING.md): how to report a problem or propose a change
"""


#: Everything public may be crawled; the tool surface and its sign-in may not
#: — they answer only to a token and carry nothing to index.
ROBOTS = f"""User-agent: *
Allow: /
Disallow: /mcp
Disallow: /oauth/
Disallow: /authorize
Disallow: /token
Disallow: /register

Sitemap: {SITE_URL}/sitemap.xml
"""

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def recorded_lastmods() -> dict[str, str]:
    """The <lastmod> of each page in the sitemap as last built, by path."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring((SITE / "sitemap.xml").read_bytes())
    except (OSError, ET.ParseError):
        return {}
    ns = {"s": SITEMAP_NS}
    return {u.findtext("s:loc", "", ns).removeprefix(SITE_URL): u.findtext("s:lastmod", "", ns)
            for u in root.findall("s:url", ns)}


def sitemap(dates: dict[str, str]) -> str:
    """The indexable pages at their canonical addresses — the same ones the
    pages name as canonical — each with the date it last changed."""
    urls = "".join(f"  <url><loc>{SITE_URL}{PATHS[page]}</loc>"
                   f"<lastmod>{dates[PATHS[page]]}</lastmod></url>\n" for page in PAGES)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="{SITEMAP_NS}">\n'
            f"{urls}</urlset>\n")


def main() -> int:
    css = (SRC / "site.css").read_text(encoding="utf-8")
    js = (SRC / "site.js").read_text(encoding="utf-8")
    picker = (SRC / "picker.html").read_text(encoding="utf-8")
    SITE.mkdir(exist_ok=True)
    for name, source in BRAND.items():
        (SITE / name).write_bytes(source.read_bytes())
        print(f"{name}  {source.stat().st_size:,} bytes")
    # A page keeps its sitemap date unless its built HTML changed, so
    # rebuilding an untouched site moves nothing and the dates stay honest.
    before = recorded_lastmods()
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    dates = {}
    for page in PAGES + UNLISTED:
        html = build(page, css, js, picker)
        target = SITE / f"{page}.html"
        unchanged = target.exists() and target.read_text(encoding="utf-8") == html
        target.write_text(html, encoding="utf-8")
        print(f"{page}.html  {len(html):,} bytes")
        if page in PATHS:
            path = PATHS[page]
            dates[path] = before[path] if unchanged and before.get(path) else today
    for name, text in (("robots.txt", ROBOTS), ("sitemap.xml", sitemap(dates)),
                       ("site.webmanifest", manifest()), ("llms.txt", llms_txt())):
        (SITE / name).write_text(text, encoding="utf-8")
        print(f"{name}  {len(text):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
