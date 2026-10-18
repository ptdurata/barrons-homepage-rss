#!/usr/bin/env python3
"""Build an RSS 2.0 feed from the public Barron's homepage.

Only headline metadata that the homepage itself exposes (title, link, and, when
present, a short summary and publication date) is used. Article pages are never
fetched and article body text is never stored.

Exit codes:
    0  feed written, or feed unchanged
    1  fetch, block, parse or credibility failure; existing feed left untouched
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

HOMEPAGE_URL = "https://www.barrons.com/"
ROBOTS_URL = "https://www.barrons.com/robots.txt"
FEED_SELF_URL = "https://ptdurata.github.io/barrons-homepage-rss/barrons.xml"
REPO_URL = "https://github.com/ptdurata/barrons-homepage-rss"
USER_AGENT = f"barrons-homepage-rss/1.0 (+{REPO_URL}; public headline RSS feed)"

TIMEOUT_SECONDS = 20
MIN_ITEMS = 5
MAX_ITEMS = 60
MAX_SUMMARY_CHARS = 300
MIN_TITLE_CHARS = 15

# Article-like paths on barrons.com. Everything else (quotes, topics, video hubs,
# account pages) is ignored.
ARTICLE_PATH_RE = re.compile(r"^/(articles|livecoverage)/[A-Za-z0-9][A-Za-z0-9\-_/]*$")
BARRONS_HOSTS = {"www.barrons.com", "barrons.com"}

# Markers of an anti-bot interstitial instead of the real homepage.
BLOCK_MARKERS = ("captcha-delivery.com", "geo.captcha-delivery", "datadome", "px-captcha", "cf-chl")

URL_KEYS = ("canonicalUrl", "canonical_url", "url", "articleUrl", "link", "href", "seoUrl", "webUrl")
TITLE_KEYS = ("headline", "headlineText", "title", "flashline", "name")
SUMMARY_KEYS = ("summary", "summaryText", "description", "standfirst", "dek", "subHeadline", "subheadline")
DATE_KEYS = (
    "datePublished",
    "publishedDateTime",
    "publishedDate",
    "publishDate",
    "pubDate",
    "liveDate",
    "displayDate",
    "timestamp",
)

log = logging.getLogger("barrons-rss")


class ScrapeError(Exception):
    """Raised when the run must not replace the existing feed."""


@dataclass
class Article:
    title: str
    link: str
    summary: str | None = None
    published: datetime | None = None
    source: str = ""

    @property
    def guid(self) -> str:
        return self.link


# --------------------------------------------------------------------------- fetch


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )
    return session


def robots_allows(session: requests.Session, url: str) -> bool:
    """Check robots.txt for our User-Agent. Unreachable robots.txt is treated as a failure."""
    try:
        resp = session.get(ROBOTS_URL, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ScrapeError(f"could not fetch robots.txt: {exc}") from exc
    if resp.status_code >= 500 or resp.status_code in (401, 403, 429):
        raise ScrapeError(f"robots.txt returned HTTP {resp.status_code}; refusing to proceed")
    if resp.status_code == 404:
        return True
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    allowed = parser.can_fetch(USER_AGENT, url)
    log.info("robots.txt HTTP %s, %d bytes; homepage allowed: %s", resp.status_code, len(resp.text), allowed)
    if not allowed:
        # Show the public rules that matched, so a refusal can be checked by hand.
        for line in resp.text.splitlines()[:400]:
            if re.match(r"\s*(user-agent|allow|disallow)\s*:", line, re.I):
                log.info("robots.txt: %s", line.strip())
    return allowed


def fetch_homepage(session: requests.Session) -> str:
    try:
        resp = session.get(HOMEPAGE_URL, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ScrapeError(f"network error fetching homepage: {exc}") from exc
    body = resp.text or ""
    lowered = body[:20000].lower()
    headers = getattr(resp, "headers", {}) or {}
    log.info(
        "homepage HTTP %s, %d bytes, server=%r", resp.status_code, len(body), headers.get("server")
    )
    if resp.status_code in (401, 403, 429) or any(m in lowered for m in BLOCK_MARKERS):
        raise ScrapeError(
            f"homepage appears blocked by anti-bot controls (HTTP {resp.status_code}, "
            f"x-datadome header present: {'x-datadome' in {k.lower() for k in headers}}); "
            "not attempting any workaround"
        )
    if resp.status_code != 200:
        raise ScrapeError(f"homepage returned HTTP {resp.status_code}")
    if len(body) < 5000:
        raise ScrapeError(f"homepage body suspiciously small ({len(body)} bytes)")
    return body


# ------------------------------------------------------------------------- helpers


def canonical_url(raw: Any, base: str = HOMEPAGE_URL) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = urlsplit(urljoin(base, raw.strip()))
    host = parts.netloc.lower()
    if host not in BARRONS_HOSTS:
        return None
    path = parts.path.rstrip("/")
    if not ARTICLE_PATH_RE.match(path):
        return None
    # Drop tracking query strings (?mod=hp_LEAD_1) and fragments.
    return urlunsplit(("https", "www.barrons.com", path, "", ""))


def clean_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("text", "value", "plain", "content"):
            if key in value:
                return clean_text(value[key])
        return None
    if isinstance(value, list):
        parts = [clean_text(v) for v in value]
        value = " ".join(p for p in parts if p)
    if not isinstance(value, str):
        return None
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def truncate(text: str | None, limit: int = MAX_SUMMARY_CHARS) -> str | None:
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.- ")
    return cut + "…"


def parse_date(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            num = float(value)
            if num > 1e12:  # milliseconds
                num /= 1000
            if not 946684800 <= num <= 4102444800:  # 2000..2100
                return None
            return datetime.fromtimestamp(num, tz=timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = email.utils.parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not 2000 <= dt.year <= 2100:
                return None
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def first_value(obj: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
    return None


def credible_title(title: str | None) -> bool:
    if not title or len(title) < MIN_TITLE_CHARS or len(title) > 300:
        return False
    return len(title.split()) >= 3


# ---------------------------------------------------------- embedded JSON sources


def iter_dicts(node: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 60:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_dicts(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from iter_dicts(value, depth + 1)


def articles_from_json(data: Any, source: str) -> list[Article]:
    found: list[Article] = []
    for obj in iter_dicts(data):
        link = None
        for key in URL_KEYS:
            link = canonical_url(obj.get(key))
            if link:
                break
        if not link:
            continue
        title = clean_text(first_value(obj, TITLE_KEYS))
        if not credible_title(title):
            continue
        summary = truncate(clean_text(first_value(obj, SUMMARY_KEYS)))
        if summary == title:
            summary = None
        published = parse_date(first_value(obj, DATE_KEYS))
        found.append(Article(title=title, link=link, summary=summary, published=published, source=source))
    return found


def embedded_json_blobs(soup: BeautifulSoup) -> Iterator[tuple[str, Any]]:
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = script.get("id") or ""
        if script_type not in ("application/json", "application/ld+json"):
            continue
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.debug("skipping malformed JSON in <script id=%r>", script_id)
            continue
        if script_id == "__NEXT_DATA__":
            label = "next-data"
        elif script_type == "application/ld+json":
            label = "json-ld"
        else:
            label = f"embedded-json:{script_id or 'anon'}"
        yield label, data


# --------------------------------------------------------------- raw HTML fallback


def articles_from_html(soup: BeautifulSoup) -> list[Article]:
    found: list[Article] = []
    for anchor in soup.find_all("a", href=True):
        link = canonical_url(anchor["href"])
        if not link:
            continue
        heading = anchor.find(["h1", "h2", "h3", "h4", "span"])
        title = clean_text(anchor.get_text(" ")) if heading is None else clean_text(heading.get_text(" "))
        if not credible_title(title):
            title = clean_text(anchor.get("aria-label") or anchor.get("title"))
        if not credible_title(title):
            continue
        summary = None
        container = anchor.find_parent(["article", "li", "div"])
        if container is not None:
            para = container.find("p")
            if para is not None:
                text = clean_text(para.get_text(" "))
                if text and text != title and len(text) >= 30:
                    summary = truncate(text)
            time_tag = container.find("time")
            published = parse_date(time_tag.get("datetime")) if time_tag is not None else None
        else:
            published = None
        found.append(Article(title=title, link=link, summary=summary, published=published, source="html"))
    return found


# ------------------------------------------------------------------------- extract


def merge(articles: Iterable[Article]) -> list[Article]:
    """Deduplicate by canonical URL, keeping first-seen order and filling gaps."""
    merged: dict[str, Article] = {}
    for art in articles:
        existing = merged.get(art.link)
        if existing is None:
            merged[art.link] = art
            continue
        if not existing.summary and art.summary:
            existing.summary = art.summary
        if not existing.published and art.published:
            existing.published = art.published
    return list(merged.values())


def extract_articles(html: str) -> tuple[list[Article], dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    by_source: dict[str, list[Article]] = {}
    for label, data in embedded_json_blobs(soup):
        by_source.setdefault(label.split(":")[0], []).extend(articles_from_json(data, label))
    by_source["html"] = articles_from_html(soup)

    counts = {src: len(merge(items)) for src, items in by_source.items()}

    # Homepage HTML order reflects editorial placement, so it leads when it is
    # credible; structured data then fills in summaries and dates for the same URLs.
    order = ["html", "next-data", "embedded-json", "json-ld"]
    if counts.get("html", 0) < MIN_ITEMS:
        order = ["next-data", "embedded-json", "json-ld", "html"]
    combined: list[Article] = []
    for src in order:
        combined.extend(by_source.get(src, []))
    return merge(combined)[:MAX_ITEMS], counts


# ----------------------------------------------------------------------------- RSS


def rfc822(dt: datetime) -> str:
    return email.utils.format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def build_rss(articles: list[Article], build_time: datetime) -> bytes:
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Barron's Homepage Headlines (unofficial)"
    ET.SubElement(channel, "link").text = HOMEPAGE_URL
    ET.SubElement(channel, "description").text = (
        "Unofficial feed of headlines and links publicly listed on the Barron's homepage. "
        "Headline metadata only; no article text. Not affiliated with Barron's or Dow Jones."
    )
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = rfc822(build_time)
    ET.SubElement(channel, "generator").text = REPO_URL
    ET.SubElement(channel, "docs").text = "https://www.rssboard.org/rss-specification"
    ET.SubElement(channel, "ttl").text = "60"
    ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link",
        {"href": FEED_SELF_URL, "rel": "self", "type": "application/rss+xml"},
    )
    for art in articles:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = art.title
        ET.SubElement(item, "link").text = art.link
        if art.summary:
            ET.SubElement(item, "description").text = art.summary
        if art.published:
            ET.SubElement(item, "pubDate").text = rfc822(art.published)
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = art.guid
    ET.indent(rss, space="  ")
    # ElementTree escapes &, <, > and quotes in text and attributes.
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def item_signature(xml_bytes: bytes) -> list[tuple]:
    """Items without lastBuildDate, used to skip rewrites when nothing changed."""
    root = ET.fromstring(xml_bytes)
    sig = []
    for item in root.iter("item"):
        sig.append(tuple((child.tag, child.text, tuple(sorted(child.attrib.items()))) for child in item))
    return sig


def validate_rss(xml_bytes: bytes) -> int:
    """Structural RSS 2.0 checks. Returns the item count or raises ScrapeError."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ScrapeError(f"generated XML is not well-formed: {exc}") from exc
    if root.tag != "rss" or root.get("version") != "2.0":
        raise ScrapeError("root element is not <rss version=\"2.0\">")
    channel = root.find("channel")
    if channel is None:
        raise ScrapeError("missing <channel>")
    for tag in ("title", "link", "description"):
        if not (channel.findtext(tag) or "").strip():
            raise ScrapeError(f"channel missing required <{tag}>")
    items = channel.findall("item")
    guids = set()
    for item in items:
        if not (item.findtext("title") or "").strip() or not (item.findtext("link") or "").strip():
            raise ScrapeError("item missing title or link")
        guid = item.findtext("guid")
        if not guid or guid in guids:
            raise ScrapeError(f"missing or duplicate guid: {guid!r}")
        guids.add(guid)
        pub = item.findtext("pubDate")
        if pub is not None and email.utils.parsedate_to_datetime(pub) is None:
            raise ScrapeError(f"bad pubDate: {pub!r}")
    return len(items)


def write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".barrons-", suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------- main


def run(output: Path, html: str | None = None, check_robots: bool = True) -> int:
    if html is None:
        session = make_session()
        if check_robots and not robots_allows(session, HOMEPAGE_URL):
            raise ScrapeError("robots.txt disallows the homepage for this User-Agent")
        html = fetch_homepage(session)

    articles, counts = extract_articles(html)
    log.info("candidates per source: %s", counts)
    if len(articles) < MIN_ITEMS:
        raise ScrapeError(f"only {len(articles)} credible articles found (need {MIN_ITEMS}); markup may have changed")

    feed = build_rss(articles, datetime.now(timezone.utc))
    count = validate_rss(feed)

    if output.exists():
        try:
            if item_signature(output.read_bytes()) == item_signature(feed):
                log.info("no item changes; leaving %s untouched (%d items)", output, count)
                return 0
        except ET.ParseError:
            log.warning("existing %s is not valid XML; replacing it", output)

    write_atomic(output, feed)
    log.info("wrote %s with %d items", output, count)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="barrons.xml", type=Path, help="feed path (default: barrons.xml)")
    ap.add_argument("--from-file", type=Path, help="parse a saved homepage HTML file instead of fetching")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    html = args.from_file.read_text(encoding="utf-8") if args.from_file else None
    try:
        return run(args.output, html=html)
    except ScrapeError as exc:
        log.error("%s", exc)
        log.error("existing %s was NOT modified", args.output)
        return 1


if __name__ == "__main__":
    sys.exit(main())
