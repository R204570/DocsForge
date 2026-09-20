"""A site's "slow down" is waited for, and its "no" is reported as a no.

Found 2026-09-20 by the offline benchmark: the third whole-site harvest of
click.palletsprojects.com in an hour met HTTP 429 on every page. The crawl
filed all thirty-three as "reached but not extractable -- nothing on them
read like documentation", stored the five pages it had got as COVERAGE
UNKNOWN, and never paused. Nothing on those pages had been read at all.

Now a 429 carrying Retry-After is honoured (bounded), an HTTP failure
carries its status so a 429 can be told from a 404, three refusals in a row
stop a harvest rather than keep asking, and the pages refused are reported
as refused -- missing for the site's reasons, not for anything about the
documentation.
"""

import pytest

from docsforge.core import engine as df

PAGE = ('<html><head><title>{title}</title></head><body><main>'
        + "body text " * 40 + '{links}</main></body></html>')


class Response:
    def __init__(self, status, headers=None, body=b"<html><body>hi</body></html>"):
        self.status_code = status
        self.headers = headers or {}
        self.content = body
        self.text = body.decode()
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.url = "https://x.dev/docs/"
        self.is_redirect = False
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    """Answers the scripted statuses in order, then the last one forever."""

    def __init__(self, *statuses, retry_after=None):
        self.statuses = list(statuses)
        self.retry_after = retry_after
        self.calls = 0
        self.headers = {}

    def get(self, url, **kw):
        self.calls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        headers = {"content-type": "text/html"}
        if status == 429 and self.retry_after is not None:
            headers["retry-after"] = str(self.retry_after)
        return Response(status, headers)

    def close(self):
        pass


def _fetcher(session, monkeypatch, slept):
    f = df.Fetcher(df.Options(delay=0, verbose=False, allow_private=True))
    f.session = session
    monkeypatch.setattr(df.time, "sleep", lambda s: slept.append(s))
    return f


def test_a_429_with_retry_after_is_waited_for_and_the_page_then_fetched(monkeypatch):
    slept: list = []
    f = _fetcher(Session(429, 429, 200, retry_after=2), monkeypatch, slept)
    html, landed = f.html_at("https://x.dev/docs/")
    assert "hi" in html
    assert slept == [2.0, 2.0]
    assert f.throttled == 2
    assert f.session.calls == 3


def test_the_wait_is_capped_and_a_429_without_retry_after_gets_a_short_one(monkeypatch):
    slept: list = []
    f = _fetcher(Session(429, 200, retry_after=3600), monkeypatch, slept)
    f.html("https://x.dev/docs/")
    assert slept == [df.RETRY_AFTER_CAP]

    slept.clear()
    f = _fetcher(Session(429, 200), monkeypatch, slept)
    f.html("https://x.dev/docs/")
    assert slept == [5.0]


def test_a_site_that_keeps_saying_429_is_given_up_on_with_the_status_attached(monkeypatch):
    slept: list = []
    f = _fetcher(Session(429, retry_after=1), monkeypatch, slept)
    with pytest.raises(df.HTTPStatusError) as caught:
        f.text("https://x.dev/docs/")
    assert caught.value.status == 429
    assert "HTTP 429 for https://x.dev/docs/" in str(caught.value)
    assert f.session.calls == df.RETRIES_ON_429 + 1
    # A 404 is the same class, different status: callers can tell them apart.
    f = _fetcher(Session(404), monkeypatch, slept)
    with pytest.raises(df.HTTPStatusError) as caught:
        f.text("https://x.dev/missing")
    assert caught.value.status == 404
    assert isinstance(caught.value, df.ForgeError)


class RateLimitedSite:
    """An entry page that links to many, and a site that refuses the rest."""

    def __init__(self, refuse_from=1):
        self.asked: list[str] = []
        self.refuse_from = refuse_from
        links = "".join(f'<a href="/docs/p{i}">p{i}</a>' for i in range(12))
        self.entry = PAGE.format(title="Entry", links=links)

    def html_at(self, url):
        self.asked.append(url)
        if len(self.asked) > self.refuse_from:
            raise df.HTTPStatusError(429, f"HTTP 429 for {url}")
        return (self.entry if url.endswith("/docs/start")
                else PAGE.format(title=url.rsplit("/", 1)[-1], links="")), url


def test_three_refusals_in_a_row_stop_the_crawl_and_are_reported_as_refused():
    site = RateLimitedSite(refuse_from=2)          # entry + one page, then 429s
    stats: dict = {}
    opts = df.Options(crawl=True, max_pages=0, delay=0, verbose=False, workers=1)
    docs = df._crawl_html("https://x.dev/docs/start", site, opts, stats)

    assert len(docs) == 2
    assert len(site.asked) == 2 + df.RATE_LIMIT_STOP, "stopped asking after three refusals"
    assert stats["rate_limited"] is True
    assert stats["whole"] is False
    assert stats["truncated"] is False, "the page cap did not cut this; the site did"
    assert len(stats["refused"]) == df.RATE_LIMIT_STOP
    assert "unextractable" not in stats, "nothing was read from a refused page"
    assert "HTTP 429" in stats["reason"] and "harvest again later" in stats["reason"]
    assert stats["remaining"] >= 1


def test_an_entry_page_refused_outright_says_so_rather_than_no_pages():
    site = RateLimitedSite(refuse_from=0)
    opts = df.Options(crawl=True, max_pages=0, delay=0, verbose=False, workers=1)
    with pytest.raises(df.ForgeError, match="HTTP 429.*harvest again later"):
        df._crawl_html("https://x.dev/docs/start", site, opts, {})


def test_a_single_refusal_does_not_stop_a_crawl_that_recovers():
    class Flaky(RateLimitedSite):
        def html_at(self, url):
            self.asked.append(url)
            if len(self.asked) == 2:
                raise df.HTTPStatusError(429, f"HTTP 429 for {url}")
            return (self.entry if url.endswith("/docs/start")
                    else PAGE.format(title="p", links="")), url

    site = Flaky()
    stats: dict = {}
    opts = df.Options(crawl=True, max_pages=0, delay=0, verbose=False, workers=1)
    docs = df._crawl_html("https://x.dev/docs/start", site, opts, stats)
    assert len(docs) == 12                        # entry + 11 of 12 linked pages
    assert stats["refused"] and len(stats["refused"]) == 1
    assert not stats.get("rate_limited")
    assert stats["whole"] is None, "the frontier drained; coverage is unknown, as before"
