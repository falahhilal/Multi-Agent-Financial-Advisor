"""Parsing helpers shared by the news jobs (news_rss, news_aaj_psx, news_gdelt).

* :func:`canonical_url` / :func:`article_id`: one id scheme across all news
  datasets, so the same article gets the same ``article_id`` everywhere.
* :func:`html_to_text`, :func:`parse_published`, :func:`detect_language`.
* :func:`parse_article`: article pages on the Business Recorder Group
  platform (brecorder.com, english.aaj.tv): JSON-LD ``NewsArticle`` plus
  ``div.story__content`` and ``div.story__tags``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

_TRACKING_PARAM = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|cmpid)$", re.I)
_ARABIC_SCRIPT = re.compile("[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
_LETTER = re.compile(r"[^\W\d_]")
_BLOCK_TAGS = ["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"]


def canonical_url(link: str, id_pattern: re.Pattern[str] | None = None) -> str:
    """Normalise a link: https, lower-case host, no fragment, no tracking
    params, no trailing slash. Then, if ``id_pattern`` matches, keep group 1
    (e.g. ``https://www.dawn.com/news/2042903``)."""
    parts = urllib.parse.urlsplit(link.strip())
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = urllib.parse.urlencode(
        [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAM.match(k)
        ]
    )
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    url = urllib.parse.urlunsplit((scheme, host + port, path, query, ""))
    if id_pattern is not None:
        m = id_pattern.match(url)
        if m:
            url = m.group(1)
    return url


def article_id(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def html_to_text(html: str | None) -> str | None:
    """Plain text from an HTML fragment, one paragraph per line."""
    if not html or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.append("\n")
    lines = (
        re.sub("[ \t\xa0]+", " ", ln).strip() for ln in soup.get_text("").splitlines()
    )
    text = "\n".join(ln for ln in lines if ln)
    return text or None


def _fmt_offset(dt: datetime) -> str | None:
    off = dt.utcoffset()
    if off is None:
        return None
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def parse_published(entry: Any) -> tuple[datetime | None, str | None, str | None]:
    """Return (published_at UTC, raw string, original offset)."""
    raw = entry.get("published") or entry.get("updated")
    if raw:
        dt: datetime | None
        try:
            dt = parsedate_to_datetime(raw)  # RFC 822, as used by RSS 2.0
        except (TypeError, ValueError, IndexError):
            try:
                dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is not None and dt.tzinfo is not None:
            return dt.astimezone(timezone.utc), raw, _fmt_offset(dt)
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:  # feedparser's struct is already UTC; original offset unknown
        return (
            datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc),
            raw,
            None,
        )
    return None, raw, None


def detect_language(feed_language: str | None, title: str) -> str | None:
    letters = _LETTER.findall(title)
    if letters and len(_ARABIC_SCRIPT.findall(title)) / len(letters) > 0.5:
        return "ur"
    return feed_language.lower() if feed_language else None


def _news_article_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") in ("NewsArticle", "Article"):
                return obj
    return {}


def _meta(soup: BeautifulSoup, key: str) -> str | None:
    tag = soup.find("meta", attrs={"property": key}) or soup.find(
        "meta", attrs={"name": key}
    )
    content = tag.get("content") if tag else None
    return str(content) if content else None


def parse_article(html: bytes, url: str) -> dict[str, Any]:
    """Fields from one article page. Missing values are None, never guessed."""
    soup = BeautifulSoup(html, "lxml")
    ld = _news_article_ld(soup)
    raw_date = ld.get("datePublished") or _meta(soup, "article:published_time")
    published_at, published_raw, published_tz = parse_published(
        {"published": raw_date} if raw_date else {}
    )
    raw_mod = ld.get("dateModified") or _meta(soup, "article:modified_time")
    modified_at, _, _ = parse_published({"published": raw_mod} if raw_mod else {})
    authors_ld = ld.get("author") or []
    if isinstance(authors_ld, dict):
        authors_ld = [authors_ld]
    authors = [
        str(a.get("name")) for a in authors_ld if isinstance(a, dict) and a.get("name")
    ]
    canon_tag = soup.find("link", rel="canonical")
    canonical = canonical_url(str(canon_tag["href"]) if canon_tag else url)
    body = soup.select_one("div.story__content")
    title = ld.get("headline") or _meta(soup, "og:title")
    lang = soup.html.get("lang") if soup.html else None
    return {
        "title": html_to_text(str(title)) if title else None,
        "published_at": published_at,
        "published_at_raw": published_raw,
        "published_tz": published_tz,
        "modified_at": modified_at,
        "authors": authors,
        "summary": _meta(soup, "description"),
        "full_text": html_to_text(str(body)) if body else None,
        "language": str(lang).lower() if lang else None,
        "tags": [a.get_text(strip=True) for a in soup.select("div.story__tags a")],
        "canonical_url": canonical,
    }
