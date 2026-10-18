"""Offline tests. The fixtures are synthetic pages shaped like the structures the
parser looks for; they are not copies of Barron's content."""

import json
import xml.etree.ElementTree as ET

import pytest

import scraper


def story(i, **extra):
    data = {
        "headline": f"Synthetic market headline number {i}: S&P 500 & \"Fed\" <i>rally</i>",
        "url": f"https://www.barrons.com/articles/synthetic-story-{i}-abc{i}?mod=hp_LEAD_{i}",
        "summary": f"Short public summary {i}.",
        "datePublished": f"2026-10-1{i % 9}T12:00:00Z",
    }
    data.update(extra)
    return data


def next_data_page(n=8):
    blob = {"props": {"pageProps": {"collections": [{"items": [story(i) for i in range(n)]}]}}}
    return (
        "<html><head><title>x</title></head><body>"
        + "<p>padding</p>" * 400
        + f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script>'
        + "</body></html>"
    )


def html_only_page(n=8):
    cards = "".join(
        f'<article><a href="/articles/plain-story-{i}?mod=hp"><h3>Plain HTML headline about stocks {i}</h3></a>'
        f'<p>A short teaser sentence for card number {i} on the page.</p><time datetime="2026-10-17T0{i}:00:00Z"></time></article>'
        for i in range(n)
    )
    nav = '<a href="/market-data">Market Data Center Overview</a><a href="https://www.wsj.com/articles/x">Other site story here</a>'
    return "<html><body>" + "<p>padding</p>" * 400 + nav + cards + "</body></html>"


def test_canonical_url():
    assert scraper.canonical_url("/articles/foo-bar-123?mod=x#c") == "https://www.barrons.com/articles/foo-bar-123"
    assert scraper.canonical_url("https://barrons.com/livecoverage/stock-market-today/") == (
        "https://www.barrons.com/livecoverage/stock-market-today"
    )
    assert scraper.canonical_url("/market-data/stocks") is None
    assert scraper.canonical_url("https://www.wsj.com/articles/foo") is None
    assert scraper.canonical_url(None) is None


def test_next_data_extraction_and_dedupe():
    page = next_data_page()
    page = page.replace("</body>", '<a href="/articles/synthetic-story-1-abc1">dup</a></body>')
    articles, counts = scraper.extract_articles(page)
    assert counts["next-data"] == 8
    assert len(articles) == 8
    assert len({a.link for a in articles}) == 8
    first = articles[0]
    assert first.link == "https://www.barrons.com/articles/synthetic-story-0-abc0"
    assert first.summary == "Short public summary 0."
    assert first.published.year == 2026


def test_html_fallback():
    articles, counts = scraper.extract_articles(html_only_page())
    assert counts["html"] == 8
    assert all(a.link.startswith("https://www.barrons.com/articles/plain-story-") for a in articles)
    assert articles[0].summary.startswith("A short teaser")
    assert articles[0].published is not None


def test_rss_is_valid_and_escaped(tmp_path):
    out = tmp_path / "barrons.xml"
    assert scraper.run(out, html=next_data_page(), check_robots=False) == 0
    raw = out.read_bytes()
    assert b"S&amp;P 500 &amp; \"Fed\" rally" in raw
    assert b"<i>" not in raw
    root = ET.fromstring(raw)
    assert root.tag == "rss" and root.get("version") == "2.0"
    items = root.findall("./channel/item")
    assert len(items) == 8
    for item in items:
        assert item.findtext("guid") == item.findtext("link")
        assert item.findtext("pubDate").endswith("GMT")
    assert scraper.validate_rss(raw) == 8


def test_unchanged_items_do_not_rewrite(tmp_path):
    out = tmp_path / "barrons.xml"
    scraper.run(out, html=next_data_page(), check_robots=False)
    before = out.read_bytes()
    scraper.run(out, html=next_data_page(), check_robots=False)
    assert out.read_bytes() == before


def test_too_few_articles_preserves_existing_feed(tmp_path):
    out = tmp_path / "barrons.xml"
    scraper.run(out, html=next_data_page(), check_robots=False)
    before = out.read_bytes()
    with pytest.raises(scraper.ScrapeError):
        scraper.run(out, html="<html><body>" + "x" * 6000 + "</body></html>", check_robots=False)
    assert out.read_bytes() == before


def test_malformed_json_is_skipped():
    page = html_only_page().replace(
        "</body>", '<script id="__NEXT_DATA__" type="application/json">{not json</script></body>'
    )
    articles, _ = scraper.extract_articles(page)
    assert len(articles) == 8


class FakeResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeSession:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, timeout):
        assert timeout == scraper.TIMEOUT_SECONDS
        return self.resp


@pytest.mark.parametrize(
    "status,text",
    [
        (403, "<html>blocked</html>"),
        (200, "<html><script src='https://ct.captcha-delivery.com/c.js'></script>" + "x" * 6000),
        (503, "x" * 6000),
        (200, "<html>tiny</html>"),
    ],
)
def test_fetch_failures_raise(status, text):
    with pytest.raises(scraper.ScrapeError):
        scraper.fetch_homepage(FakeSession(FakeResp(status, text)))


def test_robots_disallow():
    session = FakeSession(FakeResp(200, "User-agent: *\nDisallow: /\n"))
    assert scraper.robots_allows(session, scraper.HOMEPAGE_URL) is False
    session = FakeSession(FakeResp(200, "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /search\n"))
    assert scraper.robots_allows(session, scraper.HOMEPAGE_URL) is True
