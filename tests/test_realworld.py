"""
Real-world acquisition: every case here was measured on a live site.

`scripts/fieldtest.py` harvested 53 documentation sites built with every
common generator on 2026-09-24, and these are the failures it found, each as
the smallest fake that reproduces what the site actually did. The docstring
names the site and what it cost; the assertion is what now happens instead.
No network: the fakes answer from dicts, and say where a request landed the
way the real `Fetcher` does.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.core import engine as df
from docsforge.core import languages, topics


class Resp:
    def __init__(self, text="", status=200, ctype="text/html", url="", location=""):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}
        if location:
            self.headers["location"] = location
        self.url = url

    @property
    def content(self):
        return self.text.encode("utf-8")

    def close(self):
        pass


class Site:
    """A site as a dict of exact URL -> page. `redirects` maps a URL to where
    the server sends it. Slash-sensitive, because servers are."""

    def __init__(self, pages=None, redirects=None, types=None):
        self.pages = dict(pages or {})
        self.redirects = dict(redirects or {})
        self.types = dict(types or {})
        self.asked: list[str] = []

    def _land(self, url):
        seen = 0
        while url in self.redirects and seen < 5:
            url = self.redirects[url]
            seen += 1
        return url

    def get(self, url, **kw):
        self.asked.append(url)
        landed = self._land(url)
        if landed not in self.pages:
            return Resp("not found", status=404, url=landed)
        ctype = self.types.get(landed, "text/html" if not landed.endswith(
            (".txt", ".md", ".xml")) else "text/plain")
        return Resp(self.pages[landed], ctype=ctype, url=landed)

    def text(self, url, **kw):
        r = self.get(url)
        if r.status_code != 200:
            raise df.HTTPStatusError(r.status_code, f"HTTP {r.status_code} for {url}")
        return r.text

    def html_at(self, url):
        r = self.get(url)
        if r.status_code != 200:
            raise df.HTTPStatusError(r.status_code, f"HTTP {r.status_code} for {url}")
        return r.text, r.url

    def html(self, url):
        return self.html_at(url)[0]

    def render(self, url):
        return self.html(url)

    def close(self):
        pass


PROSE = "Documentation prose that a reader would want to keep. " * 12


def page(title, body="", links="", head="", nav=""):
    return (f"<html><head><title>{title}</title>{head}</head><body>{nav}<main>"
            f"<h1>{title}</h1><p>{PROSE}</p>{body}{links}</main></body></html>")


def opts(**kw):
    kw.setdefault("delay", 0)
    kw.setdefault("verbose", False)
    kw.setdefault("workers", 1)
    kw.setdefault("max_pages", 0)
    return df.Options(**kw)


# ── fetching and scope ───────────────────────────────────────────────────────

def test_a_directory_url_is_fetched_with_its_slash():
    """doc.rust-lang.org answers `/book/` with the Rust book and `/book` with a
    302 to `/stable/book/`, outside the scope. The crawl fetched `/book` and
    stored the title page alone."""
    site = Site({
        "https://doc.rust-lang.org/book/": page("Book", links='<a href="ch01.html">1</a>'
                                                             '<a href="ch02.html">2</a>'),
        "https://doc.rust-lang.org/book/ch01.html": page("Ch 1"),
        "https://doc.rust-lang.org/book/ch02.html": page("Ch 2"),
        "https://doc.rust-lang.org/stable/book/": page("Wrong place"),
    }, redirects={"https://doc.rust-lang.org/book": "https://doc.rust-lang.org/stable/book/"})
    docs = df._crawl_html("https://doc.rust-lang.org/book/", site, opts(crawl=True))
    assert {d.title for d in docs} == {"Book", "Ch 1", "Ch 2"}
    assert "https://doc.rust-lang.org/book" not in site.asked


def test_the_frontier_keys_a_page_once_however_it_is_spelled():
    frontier = df._Frontier("https://x.dev/docs/a/")
    frontier.append("https://x.dev/docs/a")
    frontier.append("https://x.dev/docs/a.md")
    frontier.append("https://x.dev/docs/a/index.html")
    assert len(frontier) == 1


def test_a_start_url_that_redirects_is_harvested_where_it_lands():
    """laravel.com/docs is a 301 to /framework/docs; the section stayed /docs/
    and the harvest stored one page."""
    nav = "".join(f'<a href="/framework/docs/p{i}">p{i}</a>' for i in range(3))
    site = Site({
        "https://l.dev/framework/docs": page("Docs", links=nav),
        **{f"https://l.dev/framework/docs/p{i}": page(f"P{i}") for i in range(3)},
    }, redirects={"https://l.dev/docs": "https://l.dev/framework/docs"})
    stats = {}
    docs, strategy = df.harvest("https://l.dev/docs", opts(), fetcher=site, stats=stats)
    assert {d.title for d in docs} == {"Docs", "P0", "P1", "P2"}


def test_the_llms_file_nearest_the_section_wins():
    """resend.com/llms-full.txt is 8 KB about the company; resend.com/docs/
    llms-full.txt is 2.2 MB of documentation. The origin's was taken."""
    site = Site({
        "https://r.dev/llms-full.txt": "# Resend the company\n\n" + "We are great. " * 100,
        "https://r.dev/docs/llms-full.txt": "# Resend docs\n\n" + "Send an email. " * 400,
    })
    det = df.detect_source("https://r.dev/docs/introduction", site)
    assert det.url == "https://r.dev/docs/llms-full.txt"


def test_a_dump_that_opens_with_a_system_tag_is_not_taken_for_html():
    """svelte.dev's dumps begin `<SYSTEM>This is the developer documentation`;
    rejecting any body that starts with `<` lost the whole of Svelte's docs."""
    body = "<SYSTEM>This is the developer documentation for Svelte.</SYSTEM>\n\n# Overview\n" + "x " * 600
    site = Site({"https://s.dev/docs/svelte/llms.txt": body})
    det = df.detect_source("https://s.dev/docs/svelte/overview", site, scope="/docs/svelte/")
    assert det.kind == "llms_txt" and det.url.endswith("/docs/svelte/llms.txt")


def test_an_html_fallback_at_an_llms_path_is_refused():
    site = Site({"https://a.dev/llms-full.txt": "<!DOCTYPE html><html><body>app</body></html>"},
                types={"https://a.dev/llms-full.txt": "text/plain"})
    assert df.detect_source("https://a.dev/overview", site).kind == "html"


def test_a_text_sitemap_is_read():
    """doc.rust-lang.org/robots.txt names `sitemap.txt`, one URL a line."""
    text = "https://d.dev/book/\nhttps://d.dev/book/ch01.html\n\nhttps://d.dev/std/\n"
    links = df._sitemap_links(text, Site(), opts())
    assert links == ["https://d.dev/book/", "https://d.dev/book/ch01.html", "https://d.dev/std/"]


def test_a_sitemap_beside_the_section_is_read_before_the_site_wide_index():
    """docs.aws.amazon.com's sitemap index is every AWS service; the Lambda guide
    has its own. Reading the index timed the harvest out at seven minutes."""
    site = Site({
        "https://a.dev/robots.txt": "Sitemap: https://a.dev/sitemap_index.xml\n",
        "https://a.dev/lambda/dg/sitemap.xml":
            "<urlset>" + "".join(f"<url><loc>https://a.dev/lambda/dg/p{i}</loc></url>"
                                 for i in range(4)) + "</urlset>",
        "https://a.dev/sitemap_index.xml": "<sitemapindex><sitemap><loc>https://a.dev/s1.xml"
                                           "</loc></sitemap></sitemapindex>",
    }, types={"https://a.dev/lambda/dg/sitemap.xml": "application/xml",
              "https://a.dev/sitemap_index.xml": "application/xml"})
    o = opts(section="/lambda/dg/")
    urls = df._sitemap_urls("https://a.dev/lambda/dg/welcome", site, o,
                            keep=lambda u: "/lambda/dg/" in u)
    assert len(urls) == 4
    assert "https://a.dev/sitemap_index.xml" not in site.asked


# ── the section, from the site's own navigation ──────────────────────────────

def sidebar(paths, here=""):
    items = "".join(f'<li><a href="{p}">{p}</a></li>' for p in paths)
    return f'<aside class="sidebar"><ul>{items}</ul></aside>'


def test_a_page_at_the_root_is_not_a_folder_of_its_own():
    """angular.dev/overview: its URL says `/overview/`, its sidebar covers the
    whole site, and a harvest kept to `/overview/` stored one page."""
    nav = sidebar(["/overview", "/installation", "/essentials", "/guide/components",
                   "/guide/templates", "/guide/di", "/tutorials/learn", "/reference/cli",
                   "/best-practices"])
    html = page("Overview", nav=nav)
    assert df._resolve_section("https://angular.dev/overview", html) == "/"


def test_a_section_root_is_inside_its_own_section():
    """nextjs.org/docs is `/docs/`'s own root page; measuring it by its parent
    directory widened the harvest to all of nextjs.org."""
    nav = sidebar([f"/docs/app/p{i}" for i in range(10)])
    assert df._resolve_section("https://n.dev/docs", page("Docs", nav=nav)) == "/docs/"


def test_a_real_section_is_not_widened_by_a_broad_sidebar():
    """docs.stripe.com/payments names a section with its own subtree; widening
    it to all of Stripe answered a question nobody asked."""
    nav = sidebar([f"/payments/p{i}" for i in range(9)] + [f"/billing/b{i}" for i in range(40)]
                  + [f"/connect/c{i}" for i in range(40)])
    html = page("Payments", nav=nav)
    assert df._resolve_section("https://docs.s.com/payments", html) == "/payments/"


def test_the_mobile_drawer_copy_of_the_top_bar_is_not_the_sidebar():
    """go.dev puts its header menu again in an `<aside class="NavigationDrawer
    js-header">`, outside `<header>`; it links to the whole site."""
    drawer = ('<aside class="NavigationDrawer js-header"><nav>'
              + "".join(f'<a href="/{p}">{p}</a>' for p in
                        ("solutions", "learn", "blog", "play", "doc", "help", "talks",
                         "security", "wiki"))
              + "</nav></aside>")
    assert df._resolve_section("https://go.dev/doc/", page("Docs", nav=drawer)) == "/doc/"


def test_a_declared_base_that_the_links_ignore_does_not_move_the_crawl():
    """Apple's app shell declares `<base href="/tutorials/">` on every
    documentation page and links nowhere under it."""
    html = page("SwiftUI", head='<base href="https://d.apple/tutorials/">',
                links='<a href="https://d.apple/documentation/swiftui/app">a</a>'
                      '<a href="https://d.apple/documentation/swiftui/view">v</a>'
                      '<a href="https://d.apple/documentation/swiftui/scene">s</a>')
    soup = df._soup(html)
    assert not df._lives_at(soup, "https://d.apple/documentation/swiftui",
                            "https://d.apple/documentation/swiftui",
                            "https://d.apple/tutorials/")


# ── what the site lists, together ─────────────────────────────────────────────

def test_a_manifest_and_the_sitemap_are_read_together():
    """fastapi.tiangolo.com's `objects.inv` comes from mkdocstrings and lists
    its 22 API reference pages; the ~150 guide pages are only in the sitemap."""
    sitemap = "<urlset>" + "".join(f"<url><loc>https://f.dev/tutorial/t{i}/</loc></url>"
                                   for i in range(5)) + "</urlset>"
    site = Site({"https://f.dev/sitemap.xml": sitemap},
                types={"https://f.dev/sitemap.xml": "application/xml"})
    import unittest.mock as mock
    api = [f"https://f.dev/reference/r{i}/" for i in range(3)]
    with mock.patch.object(df, "site_manifest", return_value=(api, "sphinx")):
        listed, kind = df._listed_pages("https://f.dev/", site, opts())
    assert kind == "sphinx"
    assert len(listed) == 8


def test_links_on_the_pages_a_curated_index_lists_are_followed():
    """docs.github.com/llms.txt lists seven pages of Actions and there is no
    sitemap; those seven link to the rest."""
    index = "# GH\n\n" + "\n".join(f"- [A{i}](https://g.dev/en/actions/a{i})" for i in range(3))
    more = '<a href="/en/actions/deep">deep</a>'
    site = Site({
        "https://g.dev/llms.txt": index,
        "https://g.dev/en/actions/a0": page("A0", links=more),
        "https://g.dev/en/actions/a1": page("A1"),
        "https://g.dev/en/actions/a2": page("A2"),
        "https://g.dev/en/actions/deep": page("Deep"),
    })
    stats = {}
    docs, strategy = df.harvest("https://g.dev/en/actions/", opts(section="/en/actions/"),
                                fetcher=site, stats=stats)
    assert "Deep" in {d.title for d in docs}
    assert strategy.endswith("+ pages it links to")


# ── dumps cut where they say their pages are ─────────────────────────────────

DENO_LIKE = (
    "# Deno docs\n\n> preamble " + "words " * 60 + "\n\n---\n\n"
    "# Config files\n\n> How Deno projects are configured.\n\n"
    "URL: https://d.dev/runtime/config\n\n" + "Config prose. " * 40 + "\n\n"
    "```sh\n# For entire app\ndeno run main.ts\n```\n\n---\n\n"
    "# Modules\n\n> How modules resolve.\n\nURL: https://d.dev/runtime/modules\n\n"
    + "Module prose. " * 40 + "\n\n---\n\n"
    "# Testing\n\nURL: https://d.dev/runtime/testing\n\n" + "Testing prose. " * 40 + "\n"
)


def test_a_dump_is_cut_where_it_says_its_pages_are():
    """docs.deno.com/llms-full.txt marks each page `URL:` under a heading and a
    one-line summary; the marker was missed, and the dump was cut on `#`
    comments inside shell snippets into 254 fragments."""
    pages = df._dump_pages(DENO_LIKE)
    urls = [u for u, _t, _c in pages if u]
    assert urls == ["https://d.dev/runtime/config", "https://d.dev/runtime/modules",
                    "https://d.dev/runtime/testing"]
    titles = [t for _u, t, _c in pages]
    assert "For entire app" not in titles


def test_a_dump_whose_page_headings_are_links_is_cut_on_them():
    """pydantic.dev's dump opens each page `# [Aliases](https://…/aliases/)` and
    states no `Source:`; it was cut on headings into 214 fragments."""
    text = "".join(f"# [Page {i}](https://p.dev/docs/p{i}/)\n\n# Page {i}\n\n## Part\n\n"
                   f"{'words ' * 80}\n\n---\n\n" for i in range(4))
    pages = df._dump_pages(text)
    assert [u for u, _t, _c in pages] == [f"https://p.dev/docs/p{i}/" for i in range(4)]
    assert [t for _u, t, _c in pages] == [f"Page {i}" for i in range(4)]


def test_published_markdown_keeps_its_words_and_loses_its_dead_links():
    """react-native's dump carries `[​](#x "Direct link to X")` 2,665 times and
    Next.js's links `/docs/...` 4,224 times -- nowhere, once stored."""
    text = ('### Highlights[​](#highlights "Direct link to Highlights")\n\n'
            'See [routing](/docs/app/routing) and [up](../x "t").\n\n'
            '```md\n[kept as written](/raw)\n```\n')
    tidy = df._tidy_markdown(text, "https://n.dev/docs/app/page")
    assert "### Highlights\n" in tidy
    assert "(https://n.dev/docs/app/routing)" in tidy
    assert '(https://n.dev/docs/x "t")' in tidy
    assert "[kept as written](/raw)" in tidy


def test_code_in_a_table_cell_does_not_turn_the_rest_of_a_dump_into_code():
    """react-native's dump puts code in table cells -- `| ```jsx` opens
    mid-line, `` ``` | ![img](…) `` closes one -- and a list-indented fence
    left the count odd: everything after counted as code and 1,367 permalink
    marks and 547 relative links survived the tidy."""
    text = ("| a | b |\n|---|---|\n| ```jsx\n<View/>\n``` | ![New](/blog/new.png) |\n\n"
            "- item\n\n      ```sh\n      npm i\n      ```\n\n"
            "See [the timers](/docs/timers.md#content).\n\n"
            "### Next[​](#next \"Direct link to Removal of \\\"run\\\" handlers\")\n")
    tidy = df._tidy_markdown(text, "https://rn.dev/llms-full.txt")
    assert "(https://rn.dev/docs/timers.md#content)" in tidy
    assert "(https://rn.dev/blog/new.png)" in tidy
    assert "Direct link" not in tidy


def test_a_sites_machine_files_are_never_harvested_as_pages():
    """Angular's sitemap lists its own llms.txt; an uncapped harvest counted it
    as a page it could not read."""
    assert not df._crawlable("https://a.dev/llms.txt", "a.dev", "/")
    assert not df._crawlable("https://a.dev/docs/sitemap.xml", "a.dev", "/")
    assert df._crawlable("https://a.dev/docs/guide", "a.dev", "/")


def test_a_heading_inside_a_code_block_is_not_a_page_boundary():
    body = "".join(f"## Section {i}\n\n{'text ' * 400}\n\n```sh\n# comment {i}\nls\n```\n\n"
                   for i in range(5))
    titles = [t for t, _c in df._split_dump(body, above=0)]
    assert not [t for t in titles if t.startswith("comment")]
    assert len(titles) == 5


# ── extraction ────────────────────────────────────────────────────────────────

def test_heading_permalinks_and_chrome_are_not_stored():
    html = page("Guide", body=(
        '<h2 id="a">Setup<a class="hash-link" href="#a" aria-label="Direct link to Setup">'
        '&#8203;</a></h2><h3 id="b">Next<a class="headerlink" href="#b">¶</a></h3>'
        '<p>Copy</p><div class="theme-edit-this-page">Edit this page</div>'
        '<p>Last updated on Sep 12, 2026</p>'))
    _t, md = df._html_to_md(html, "https://x.dev/docs/guide")
    assert "## Setup" in md and "### Next" in md
    assert "Direct link" not in md and "¶" not in md
    assert "Edit this page" not in md and "Last updated" not in md
    assert "\nCopy\n" not in md


def test_code_highlighted_one_span_per_line_keeps_its_lines():
    """tailwindcss.com: `npm create vite@latest my-projectcd my-project`."""
    html = page("Install", body=(
        '<pre class="shiki"><code><span class="line"><span>npm create vite@latest my-project'
        '</span></span><span class="line"><span>cd my-project</span></span></code></pre>'))
    _t, md = df._html_to_md(html, "https://x.dev/docs/install")
    assert "npm create vite@latest my-project\ncd my-project" in md


def test_a_code_block_keeps_its_language():
    html = page("P", body='<div class="highlight-python"><pre>print(1)\n</pre></div>')
    _t, md = df._html_to_md(html, "https://x.dev/docs/p")
    assert "```python\nprint(1)\n```" in md


def test_every_tab_is_stored_under_its_label():
    """Docusaurus hides inactive tabs; `strip_chrome` deleted the hidden ones."""
    html = page("P", body=(
        '<div><ul role="tablist"><li role="tab">npm</li><li role="tab">yarn</li></ul>'
        '<div><div role="tabpanel"><pre><code>npm i x</code></pre></div>'
        '<div role="tabpanel" hidden aria-hidden="true"><pre><code>yarn add x</code></pre>'
        '</div></div></div>'))
    _t, md = df._html_to_md(html, "https://x.dev/docs/p")
    assert "**npm**" in md and "**yarn**" in md and "yarn add x" in md


def test_links_and_images_point_somewhere_once_stored():
    html = page("P", body='<a href="../other/">o</a><img src="/img/a.png" alt="a">')
    _t, md = df._html_to_md(html, "https://x.dev/docs/guide/p/")
    assert "(https://x.dev/docs/guide/other/)" in md
    assert "(https://x.dev/img/a.png)" in md


def test_a_page_without_readable_html_is_read_from_its_markdown_copy():
    """developer.apple.com/documentation/swiftui renders client-side and declares
    `<link rel="alternate" type="text/markdown">`; it was refused as unreadable."""
    shell = ('<html><head><title>SwiftUI</title><link rel="alternate" type="text/markdown" '
             'href="/documentation/swiftui.md"></head><body><noscript>Enable JS</noscript>'
             '<div id="app"></div><script src="/app.js"></script></body></html>')
    md = "# SwiftUI\n\nDeclare the user interface. " + "More. " * 50 + "\n\n[App](/documentation/swiftui/app)"
    site = Site({
        "https://a.dev/documentation/swiftui": shell,
        "https://a.dev/documentation/swiftui.md": md,
        "https://a.dev/documentation/swiftui/app": page("App"),
    }, types={"https://a.dev/documentation/swiftui.md": "text/markdown"})
    docs = df._crawl_html("https://a.dev/documentation/swiftui", site, opts(crawl=True))
    assert [d.title for d in docs] == ["SwiftUI", "App"]


def test_a_page_that_draws_itself_with_scripts_is_worth_rendering():
    shell = ('<html><body><noscript>Please enable JavaScript to read this documentation '
             'site, which needs it for everything including the words.</noscript>'
             '<div id="root"></div><script src="/main.js"></script></body></html>')
    assert df._wants_render(shell)
    assert not df._wants_render(page("Server rendered"))


# ── releases, languages, articles ─────────────────────────────────────────────

def test_a_snapshot_is_a_release_line_never_chosen_by_number():
    """docs.spring.io/spring-boot files `/4.2-SNAPSHOT/` beside its current pages;
    the snapshot was taken for unversioned current documentation."""
    urls = ([f"https://s.dev/boot/reference/p{i}" for i in range(4)]
            + [f"https://s.dev/boot/4.2-SNAPSHOT/api/p{i}" for i in range(4)]
            + [f"https://s.dev/boot/3.5/reference/p{i}" for i in range(2)])
    kept, chosen = df._prefer_current_release(urls, opts(), entry=lambda: ("", []))
    assert chosen == "" and not [u for u in kept if "SNAPSHOT" in u or "/3.5/" in u]


def test_links_into_other_languages_and_releases_are_not_followed():
    admit = df._admission("https://d.dev/en/5.2/intro/", "/",
                          seeds=["https://d.dev/en/5.2/a/", "https://d.dev/en/5.2/b/"])
    assert admit("https://d.dev/en/5.2/c/")
    assert not admit("https://d.dev/en/4.2/c/")
    assert not admit("https://d.dev/ja/5.2/c/")


def test_a_few_release_like_words_do_not_open_the_version_picker():
    """docusaurus.io lists current pages unversioned, and a few whose paths
    contain a release-like word; the rule for unversioned documentation was
    switched off by them, and an uncapped harvest crawled 311 pages of
    `/docs/3.3.2/` and `/docs/next/` from the version picker."""
    seeds = ([f"https://d.dev/docs/p{i}" for i in range(10)]
             + ["https://d.dev/docs/migration/v3", "https://d.dev/docs/next-steps/v2"])
    admit = df._admission("https://d.dev/docs/", "/docs/", seeds=seeds)
    assert admit("https://d.dev/docs/guides/x")
    assert admit("https://d.dev/docs/migration/v3-details")
    assert not admit("https://d.dev/docs/3.3.2/guides/x")
    assert not admit("https://d.dev/docs/next/guides/x")


def test_a_dead_listing_and_an_index_page_are_not_gaps():
    """Uncapped field test: FastAPI's sitemap names twelve removed pages (404),
    Flask's sidebar links Sphinx's module index; both made complete harvests
    INCOMPLETE. And Flask's one-sentence `deploying/eventlet` page was
    refused as a stub."""
    sitemap = "<urlset>" + "".join(f"<url><loc>https://f.dev/docs/{p}</loc></url>"
                                   for p in ("a", "b", "gone", "modindex", "eventlet")) + "</urlset>"
    links = "".join(f'<a href="/docs/m{i}">module {i} reference</a>' for i in range(30))
    site = Site({
        "https://f.dev/sitemap.xml": sitemap,
        "https://f.dev/docs/a": page("A"), "https://f.dev/docs/b": page("B"),
        "https://f.dev/docs/modindex": f"<html><head><title>Index</title></head><body>"
                                       f"<div class='modindex'>{links}</div></body></html>",
        "https://f.dev/docs/eventlet": '<html><head><title>eventlet</title></head><body>'
                                       '<div role="main"><h1>eventlet</h1><p>Eventlet is no '
                                       'longer maintained. Use gevent instead.</p></div>'
                                       '</body></html>',
    }, types={"https://f.dev/sitemap.xml": "application/xml"})
    stats = {}
    docs, _ = df.harvest("https://f.dev/docs/", opts(), fetcher=site, stats=stats)
    assert {d.title for d in docs} >= {"A", "B", "eventlet"}
    # The index's own links lead nowhere on this fake site: dead too.
    assert "https://f.dev/docs/gone" in stats["dead"]
    assert stats["index_pages"] == ["https://f.dev/docs/modindex"]
    assert stats["whole"] is not False, stats.get("reason")


def test_a_docusaurus_release_line_is_a_release_line():
    assert df._release_segment("https://jestjs.io/docs/28.x/upgrading") == (1, "28.x")


# ── the network ───────────────────────────────────────────────────────────────

def test_a_reset_is_retried_and_a_missing_name_is_not():
    import requests
    reset = requests.ConnectionError("('Connection aborted.', ConnectionResetError(10054, "
                                     "'An existing connection was forcibly closed'))")
    missing = requests.ConnectionError("NameResolutionError: Failed to resolve 'nope.dev'")
    assert df._was_reset(reset)
    assert not df._was_reset(missing)


# ── docsify ────────────────────────────────────────────────────────────────────

def test_a_docsify_site_is_read_from_its_own_sidebar():
    """docsify.js.org is one HTML page and a Markdown file per route; a crawl
    saw fragments only and stored one page."""
    index = ('<html><body><div id="app"></div><script>window.$docsify = {'
             "alias: {'.*?/changelog': 'https://raw.x.dev/CHANGELOG.md'}, loadSidebar: true}"
             '</script></body></html>')
    site = Site({"https://d.dev/_sidebar.md": "- [Start](quickstart.md)\n- [Guide](/guide)\n"
                                              "- [Changelog](changelog.md)\n"})
    files = df._docsify_pages("https://d.dev/#/", index, site)
    assert files == ["https://d.dev/README.md", "https://d.dev/quickstart.md",
                     "https://d.dev/guide.md", "https://raw.x.dev/CHANGELOG.md"]


# ── topics ─────────────────────────────────────────────────────────────────────

def test_a_topic_keeps_the_pages_about_it_and_the_ones_that_introduce():
    """"go for web development" first kept 26 Go release notes, because the
    words that travel with "web" -- json, url, request -- are in all of them."""
    sel = topics.Selector("web development")
    assert sel.wants("Writing Web Applications", "https://go.dev/doc/articles/wiki/")
    assert sel.wants("Tutorial: Developing a RESTful API with Go and Gin",
                     "https://go.dev/doc/tutorial/web-service-gin.html")
    assert sel.wants("Download and install", "https://go.dev/doc/install")
    notes = "## net/http\n\nThe json and url packages changed request handling. " * 20
    assert not sel.wants("Go 1.22 Release Notes", "https://go.dev/doc/go1.22", notes)
    assert not sel.wants("A Guide to the Go Garbage Collector", "https://go.dev/doc/gc-guide")
    assert sel.left_by_section()


def test_no_topic_keeps_everything():
    sel = topics.Selector("")
    assert not sel.active and sel.wants("Anything", "https://x.dev/y")


def test_a_topic_harvest_follows_only_the_pages_it_kept():
    nav = ('<a href="/docs/http-server">h</a><a href="/docs/garbage">g</a>')
    site = Site({
        "https://g.dev/docs/": page("Overview", links=nav),
        "https://g.dev/docs/http-server": page("HTTP server", links='<a href="/docs/router">r</a>'),
        "https://g.dev/docs/router": page("Routing requests"),
        "https://g.dev/docs/garbage": page("Garbage collection", links='<a href="/docs/gc-2">x</a>'),
        "https://g.dev/docs/gc-2": page("GC tuning"),
    })
    stats = {}
    docs, _ = df.harvest("https://g.dev/docs/", opts(topic="web"), fetcher=site, stats=stats)
    titles = {d.title for d in docs}
    assert titles == {"Overview", "HTTP server", "Routing requests"}
    assert "https://g.dev/docs/gc-2" not in site.asked, "an off-topic page is not a way in"
    assert stats["topic"]["left"] == 1


# ── progress ───────────────────────────────────────────────────────────────────

def test_a_crawl_reports_its_pages_as_it_goes(monkeypatch):
    """`Issues.md` T1: the crawl fetches through `html_at`, which the counting
    fetcher never saw, so every crawl reported 0 pages until it ended."""
    from docsforge.tools import forge_tools as ft
    from docsforge.tools import harvest_jobs
    monkeypatch.setattr(ft.Fetcher, "html_at",
                        lambda self, url: ("<html><body>x</body></html>", url))
    progress = harvest_jobs.Progress(phase="harvesting")
    fetcher = ft._CountingFetcher(ft._options(crawl=True), progress, {"discovered": 9})
    fetcher.html_at("https://x.dev/a")
    fetcher.html("https://x.dev/b")          # through html_at: counted once, not twice
    assert progress.pages == 2


# ── languages ──────────────────────────────────────────────────────────────────

def test_what_a_caller_calls_a_language_is_understood():
    assert languages.canonical("node").name == "javascript"
    assert languages.canonical("TypeScript").name == "javascript"
    assert languages.canonical("golang").name == "go"
    assert languages.canonical("c#").name == "csharp"
    assert languages.canonical("klingon") is None


def test_a_page_is_judged_by_its_own_code():
    js = "```ts\nconst g = new StateGraph()\n```\n" * 3 + "npm install @langchain/langgraph\n"
    py = "```python\ng = StateGraph()\n```\n" * 3 + "pip install langgraph\n"
    assert languages.written_for(js, languages.canonical("node")) is True
    assert languages.written_for(py, languages.canonical("node")) is False
    assert languages.dominant(py) == "python"


def test_a_language_is_not_a_package():
    """"go" resolved to docs.rs/go -- someone's Rust crate -- and "golang" to a
    stranger's GitHub repository."""
    assert languages.official_docs("go") == "https://go.dev/doc/"
    assert languages.official_docs("Golang") == "https://go.dev/doc/"
    assert languages.official_docs("C#") == languages.official_docs("csharp")
    assert languages.official_docs("langgraph") == ""
