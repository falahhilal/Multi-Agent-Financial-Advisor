"""Tests for backend/jobs/news_aaj_psx.py on synthetic HTML (no real articles)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import news_aaj_psx as aaj  # noqa: E402  (needs the sys.path line above)

LISTING = b"""<html><body>
<div class="flex"><div class="w-full max-w-screen-md sm:pr-4">
  <article class="story" id="111"><a href="/news/111/first-story">First</a></article>
  <article class="story" id="222"><a href="https://english.aaj.tv/news/222/">Second</a>
  </article>
  <article class="story" id="111"><a href="/news/111/first-story">Again</a></article>
</div>
<aside><article class="story"><a href="/news/999/sidebar">Sidebar</a></article></aside>
</div></body></html>"""

ARTICLE = b"""<html lang="en"><head>
<link rel="canonical" href="https://english.aaj.tv/news/111/first-story">
<meta name="description" content="Short summary.">
<meta property="article:published_time" content="2021-01-14 17:14:52+05:00">
<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>
<script type="application/ld+json">{"@type": "NewsArticle", "headline": "KSE-100 up",
 "datePublished": "2021-01-14T17:14:52+05:00",
 "dateModified": "2021-01-14T17:16:19+05:00",
 "author": [{"@type": "Person", "name": "Business Recorder"}]}</script>
</head><body>
<div class="story__content"><p>Para one.</p><p>Para two.</p></div>
<div class="story__tags"><div class="tags"><a href="/trends/psx">PSX</a>
<a href="/trends/kse100">KSE100 index</a></div></div>
</body></html>"""


def test_parse_listing_main_column_only_and_unique() -> None:
    items = aaj.parse_listing(
        LISTING, "https://english.aaj.tv/trends/pakistan-stock-exchange"
    )
    assert items == [
        ("111", "https://english.aaj.tv/news/111/first-story"),
        ("222", "https://english.aaj.tv/news/222/"),
    ]


def test_parse_article_fields() -> None:
    f = aaj.parse_article(ARTICLE, "https://english.aaj.tv/news/111/first-story")
    assert f["title"] == "KSE-100 up"
    assert f["published_at"] == datetime(2021, 1, 14, 12, 14, 52, tzinfo=timezone.utc)
    assert f["published_tz"] == "+05:00"
    assert f["modified_at"] == datetime(2021, 1, 14, 12, 16, 19, tzinfo=timezone.utc)
    assert f["authors"] == ["Business Recorder"]
    assert f["full_text"] == "Para one.\nPara two."
    assert f["summary"] == "Short summary."
    assert f["tags"] == ["PSX", "KSE100 index"]
    assert f["language"] == "en"
    assert f["canonical_url"] == "https://english.aaj.tv/news/111/first-story"


def test_parse_article_missing_date_is_none() -> None:
    f = aaj.parse_article(
        b"<html><body><p>x</p></body></html>", "https://english.aaj.tv/news/1/"
    )
    assert f["published_at"] is None and f["title"] is None and f["full_text"] is None
