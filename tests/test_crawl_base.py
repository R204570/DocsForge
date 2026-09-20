"""A crawl resolves relative links the way a browser would.

Found 2026-09-19 by the live benchmark `fetch/crawl_three_pages`:
`fetch_docs(url="https://click.palletsprojects.com/en/stable/", crawl=True,
max_pages=3)` came back with ONE page after 25 seconds. The crawl keys pages
by a normalised spelling with the trailing slash dropped, fetched
`/en/stable`, followed the server's 302 to `/en/stable/`, and then resolved
the page's relative links (`quickstart/`, `api/`, ...) against the spelling
it had asked for. `urljoin("/en/stable", "quickstart/")` is `/en/quickstart`:
thirty-eight pages requested under `/en/`, thirty-eight 404s, and a
"complete" one-page crawl. The same crawl now returns three pages in under
five seconds with 35 reported as remaining.

The fakes below say where a page was served from, the way the real
`Fetcher.html_at` does. A fake that only knows `html()` still works and is
taken at its word -- that keeps every other test's stub valid.
"""

from docsforge.core import engine as df

PAGE = ('<html><head><title>{title}</title>{head}</head><body><main>'
        + "body text " * 40 + '{links}</main></body></html>')


class Site:
    """click.palletsprojects.com in miniature: `/en/stable` redirects to
    `/en/stable/`, whose links are relative and mean `/en/stable/<page>`."""

    def __init__(self, base_tag: str = ""):
        self.asked: list[str] = []
        self.base_tag = base_tag
        links = '<a href="quickstart/">q</a><a href="api/">a</a><a href="why/">w</a>'
        self.pages = {
            "https://click.palletsprojects.com/en/stable/":
                PAGE.format(title="Welcome", head=base_tag, links=links),
            "https://click.palletsprojects.com/en/stable/quickstart/":
                PAGE.format(title="Quickstart", head="", links=""),
            "https://click.palletsprojects.com/en/stable/api/":
                PAGE.format(title="API", head="", links=""),
            "https://click.palletsprojects.com/en/stable/why/":
                PAGE.format(title="Why", head="", links=""),
        }

    def html_at(self, url: str) -> tuple[str, str]:
        self.asked.append(url)
        landed = url if url.endswith("/") else url + "/"      # the 302
        if landed not in self.pages:
            raise df.ForgeError(f"HTTP 404 for {url}")
        return self.pages[landed], landed

    def html(self, url: str) -> str:
        return self.html_at(url)[0]


def _crawl(site, start="https://click.palletsprojects.com/en/stable/", max_pages=3):
    stats: dict = {}
    opts = df.Options(crawl=True, max_pages=max_pages, delay=0, verbose=False)
    docs = df._crawl_html(start, site, opts, stats)
    return docs, stats


def test_relative_links_resolve_against_where_the_page_was_served_from():
    site = Site()
    docs, stats = _crawl(site)

    assert [d.title for d in docs] == ["Welcome", "API", "Quickstart"], \
        "three real pages, in frontier order"
    assert stats["fetched"] == 3 and stats["truncated"] is True
    # Nothing was ever asked for under /en/ directly: that was the bug.
    assert not [u for u in site.asked if "/en/stable" not in u], site.asked


def test_a_declared_base_href_wins_over_the_served_url():
    # A page served from /en/stable/ that declares its base as /en/8.5.x/
    # means every relative link there, whatever URL it was fetched from.
    site = Site(base_tag='<base href="https://click.palletsprojects.com/en/8.5.x/">')
    site.pages["https://click.palletsprojects.com/en/8.5.x/api/"] = \
        PAGE.format(title="API 8.5", head="", links="")
    docs, _ = _crawl(site, max_pages=2)

    assert [d.title for d in docs] == ["Welcome", "API 8.5"]


def test_a_fetcher_that_only_knows_html_is_taken_at_its_word():
    class Bare:
        def html(self, url):
            return PAGE.format(title="P", head="", links='<a href="/docs/b">b</a>')

    html, landed = df._fetch_at(Bare(), "https://x.dev/docs/a")
    assert landed == "https://x.dev/docs/a"
    assert "<a href" in html


def test_the_real_fetcher_reports_the_landed_url(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        url = "https://x.dev/docs/"           # after the 301 from /docs
        content = b"<html><body>hi</body></html>"
        text = content.decode()
        encoding = "utf-8"
        apparent_encoding = "utf-8"

    fetcher = df.Fetcher(df.Options(delay=0))
    monkeypatch.setattr(fetcher, "get", lambda url, **kw: Response())
    html, landed = fetcher.html_at("https://x.dev/docs")
    assert landed == "https://x.dev/docs/"
    assert fetcher.html("https://x.dev/docs") == html
