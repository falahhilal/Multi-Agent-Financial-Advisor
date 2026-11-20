"""Tests for backend/jobs/news_rss.py. Offline, synthetic feed (no real articles)."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common, news_rss  # noqa: E402  (needs the sys.path line above)

FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Synthetic</title><language>en-US</language>
<item>
  <title>PSX: KSE-100 gains 500 points</title>
  <link>https://www.example.com/news/101/psx-gains?utm_source=rss</link>
  <guid>https://www.example.com/news/101</guid>
  <pubDate>Sat, 07 Nov 26 22:32:15 +0500</pubDate>
  <category>Markets</category>
  <description>&lt;p&gt;Short &lt;b&gt;summary&lt;/b&gt;.&lt;/p&gt;</description>
  <content:encoded><![CDATA[<p>First para.</p><p>Second <a href="#">para</a>.</p>]]>
  </content:encoded>
</item>
<item>
  <title>Nikkei slumps on tech rout</title>
  <link>https://www.example.com/news/102/nikkei</link>
  <pubDate>Sun, 08 Nov 2026 03:00:00 +0000</pubDate>
  <description>Japan shares fall.</description>
</item>
<item>
  <title>PSX: KSE-100 gains 500 points (updated headline)</title>
  <link>https://www.example.com/news/101/psx-gains-updated/</link>
  <pubDate>Sat, 07 Nov 2026 17:40:00 +0000</pubDate>
</item>
</channel></rss>
"""

ID_RE = re.compile(r"^(https://www\.example\.com/news/\d+)")


def _feed(**kw: Any) -> news_rss.Feed:
    base: dict[str, Any] = {
        "name": "synthetic",
        "source": "Synthetic",
        "url": "https://www.example.com/feed",
        "role": "primary",
        "filter_name": None,
        "filter_re": None,
        "canonical_id_re": ID_RE,
        "store_full_text": True,
    }
    return news_rss.Feed(**{**base, **kw})


def test_canonical_url() -> None:
    f = news_rss.canonical_url
    assert f("HTTP://WWW.Example.com/a/b/?utm_source=x&id=3#frag") == (
        "https://www.example.com/a/b?id=3"
    )
    assert f("https://www.example.com/news/101/slug", ID_RE) == (
        "https://www.example.com/news/101"
    )
    assert (
        f("https://www.example.com/other/1", ID_RE) == "https://www.example.com/other/1"
    )


def test_parse_published_keeps_offset_and_two_digit_year() -> None:
    entry = {"published": "Sat, 07 Nov 26 22:32:15 +0500"}
    utc, raw, tz = news_rss.parse_published(entry)
    assert utc == datetime(2026, 11, 7, 17, 32, 15, tzinfo=timezone.utc)
    assert raw == entry["published"] and tz == "+05:00"


def test_detect_language() -> None:
    assert news_rss.detect_language("en-US", "PSX gains") == "en-us"
    assert news_rss.detect_language("en", "اسٹاک ایکسچینج میں تیزی") == "ur"


def test_parse_feed_fields() -> None:
    fetched = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    recs, stats = news_rss.parse_feed(FEED_XML, _feed(), fetched, "RUN")
    assert stats["items"] == 3 and stats["kept"] == 3 and stats["with_full_text"] == 1
    a = recs[0]
    assert a["canonical_url"] == "https://www.example.com/news/101"
    assert a["article_id"] == news_rss.article_id("https://www.example.com/news/101")
    assert a["summary"] == "Short summary."
    assert a["full_text"] == "First para.\nSecond para."
    assert a["full_text_source"] == "content:encoded"
    assert a["categories"] == ["Markets"] and a["tickers_mentioned"] == []
    assert a["language"] == "en-us" and a["published_tz"] == "+05:00"
    # slug change -> same article id (deduped later)
    assert recs[2]["article_id"] == a["article_id"]
    assert stats["newest_published_at"] == "2026-11-08T03:00:00Z"


def test_filter_feed_keeps_only_matches() -> None:
    cfg = news_rss.load_config()
    f = next(x for x in cfg.feeds if x.role == "filter")
    feed = _feed(role="filter", filter_name=f.filter_name, filter_re=f.filter_re)
    recs, stats = news_rss.parse_feed(FEED_XML, feed, datetime.now(timezone.utc), "R")
    assert [r["title"][:3] for r in recs] == ["PSX", "PSX"]
    assert stats["filtered_out"] == 1


def test_headline_only_mode_drops_text() -> None:
    recs, _ = news_rss.parse_feed(
        FEED_XML, _feed(store_full_text=False), datetime.now(timezone.utc), "R"
    )
    assert all(r["summary"] is None and r["full_text"] is None for r in recs)


def test_config_loads() -> None:
    cfg = news_rss.load_config()
    assert {f.role for f in cfg.feeds} == {"primary", "filter", "test"}
    assert len(cfg.feeds) == 8


def test_run_local_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    resp = common.HttpResponse("https://www.example.com/feed", 200, {}, FEED_XML)
    monkeypatch.setattr(news_rss, "http_get", lambda *a, **k: resp)
    cfg = news_rss.Config(1, "x", 0.0, 7, (_feed(),))

    assert news_rss.run(cfg, upload=False) == 0
    first = pq.read_table(tmp_path / "rss" / news_rss.INDEX_REL)
    assert first.num_rows == 2  # 3 items, one is a slug-changed duplicate

    # Same second would reuse the run id; force a new one.
    import time

    time.sleep(1.1)
    assert news_rss.run(cfg, upload=False) == 0
    second = pq.read_table(tmp_path / "rss" / news_rss.INDEX_REL)
    assert second.num_rows == 2  # nothing new
    parts = list((tmp_path / "rss" / "articles").rglob("*.parquet"))
    raws = list((tmp_path / "rss" / "raw").rglob("*.xml.gz"))
    assert len(parts) == 1 and len(raws) == 2

    m = json.loads((tmp_path / "manifests" / "news_rss_articles.json").read_text())
    assert m["fetch_mode"] == "append_only_snapshot" and m["row_count"] == 2
    assert m["extra"]["last_run"]["duplicates"] == 3


def test_stale_feed_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    resp = common.HttpResponse("https://www.example.com/feed", 200, {}, FEED_XML)
    monkeypatch.setattr(news_rss, "http_get", lambda *a, **k: resp)
    cfg = news_rss.Config(1, "x", 0.0, 0, (_feed(),))  # stale after 0 days
    news_rss.run(cfg, upload=False)
    m = json.loads((tmp_path / "manifests" / "news_rss_articles.json").read_text())
    assert any("older than 0 days" in w for w in m["warnings"])


SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.example.com/</loc></url>
<url><loc>https://www.example.com/news/101/psx-gains</loc><news:news>
  <news:publication_date>2026-11-09</news:publication_date>
  <news:title>PSX: KSE-100 gains 500 points</news:title></news:news></url>
<url><loc>https://www.example.com/news/201/print-edition</loc><news:news>
  <news:publication_date>2026-11-09</news:publication_date>
  <news:title>Lahore&#039;s smog</news:title></news:news></url>
<url><loc>https://www.example.com/news/202/another</loc><news:news>
  <news:title>Another</news:title></news:news></url>
</urlset>
"""


def _article_page(n: int) -> bytes:
    return (
        b'<html lang="en"><head><script type="application/ld+json">'
        b'{"@type": "NewsArticle", "headline": "Page title %d",'
        b' "datePublished": "2026-11-09T04:46:04+05:00",'
        b' "author": [{"name": "Recorder Review"}]}</script></head><body>'
        b'<div class="story__content"><p>Body %d.</p></div>'
        b'<div class="story__tags"><a>PSX</a></div></body></html>' % (n, n)
    )


def test_parse_news_sitemap_skips_section_pages() -> None:
    entries = news_rss.parse_news_sitemap(SITEMAP_XML)
    assert [u for u, _ in entries] == [
        "https://www.example.com/news/101/psx-gains",
        "https://www.example.com/news/201/print-edition",
        "https://www.example.com/news/202/another",
    ]
    assert entries[1][1] == "Lahore's smog"


def test_config_has_br_sitemap() -> None:
    cfg = news_rss.load_config()
    assert [s.name for s in cfg.sitemaps] == ["brecorder_sitemap"]


def test_run_fetches_only_articles_missing_from_rss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    requested: list[str] = []

    def fake_get(url: str, **kw: Any) -> common.HttpResponse:
        requested.append(url)
        if url.endswith("/feed"):
            body = FEED_XML
        elif url.endswith("/sitemap"):
            body = SITEMAP_XML
        else:
            body = _article_page(int(url.rsplit("/", 1)[1]))
        return common.HttpResponse(url, 200, {}, body)

    monkeypatch.setattr(news_rss, "http_get", fake_get)
    sm = news_rss.Sitemap(
        "sm", "Synthetic", "https://www.example.com/sitemap", ID_RE, 150, 0.0, True
    )
    cfg = news_rss.Config(1, "x", 0.0, 7, (_feed(),), (sm,))
    assert news_rss.run(cfg, upload=False) == 0
    # article 101 came from RSS, so only 201 and 202 are fetched from their pages
    assert [u for u in requested if "/news/" in u] == [
        "https://www.example.com/news/201",
        "https://www.example.com/news/202",
    ]
    part = next((tmp_path / "rss" / "articles").rglob("*.parquet"))
    rows = {r["canonical_url"]: r for r in pq.read_table(part).to_pylist()}
    r = rows["https://www.example.com/news/201"]
    assert r["feed_role"] == "sitemap" and r["full_text_source"] == "article_page"
    assert r["title"] == "Page title 201" and r["full_text"] == "Body 201."
    assert r["published_tz"] == "+05:00" and r["author"] == "Recorder Review"
    assert len(rows) == 4  # 2 from RSS (101, 102) + 2 from the sitemap

    requested.clear()
    import time

    time.sleep(1.1)
    news_rss.run(cfg, upload=False)
    assert not [u for u in requested if "/news/" in u]  # nothing fetched twice


def test_sitemap_cap_defers_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(
        news_rss,
        "http_get",
        lambda url, **kw: common.HttpResponse(
            url, 200, {}, SITEMAP_XML if url.endswith("/sitemap") else _article_page(1)
        ),
    )
    sm = news_rss.Sitemap(
        "sm", "S", "https://www.example.com/sitemap", ID_RE, 1, 0.0, True
    )
    recs, stats, _, warns = news_rss.process_sitemap(
        sm,
        seen=set(),
        fetched_at=datetime.now(timezone.utc),
        run_id="R",
        fetch_date="d",
    )
    assert len(recs) == 1 and stats["capped"] == 2 and "next run" in warns[0]


def test_config_has_nation_with_page_fetch() -> None:
    cfg = news_rss.load_config()
    nation = {f.name: f for f in cfg.feeds}["nation_business"]
    assert nation.fetch_pages and nation.store_full_text and nation.max_page_fetch > 0
    assert not any(f.fetch_pages for f in cfg.feeds if f.name != "nation_business")


def _nation_page(n: int) -> bytes:
    return (
        b'<html lang="en-us"><head><script type="application/ld+json">'
        b'{"@type": "NewsArticle", "headline": "Page %d",'
        b' "datePublished": "2026-11-20T01:37:00+05:00",'
        b' "author": {"name": "NEWS WIRE"}}</script></head><body>'
        b'<div class="news-detail-content-class"><p>Body %d.</p>'
        b'<div id="div-gpt-ad-1"><script>ad()</script></div>'
        b'<div class="oembed_ltr"><a>Other story headline</a></div>'
        b"<p>More %d.</p>"
        b'<div class="jeg_post_tags"><span>Tags:</span><div><a>PSX</a></div></div>'
        b"</div></body></html>" % (n, n, n)
    )


def test_fetch_pages_feed_retries_failed_pages_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    requested: list[str] = []
    fail_102 = {"on": True}

    def fake_get(url: str, **kw: Any) -> common.HttpResponse:
        requested.append(url)
        if url.endswith("/feed"):
            return common.HttpResponse(url, 200, {}, FEED_XML)
        n = int(url.rsplit("/", 1)[1])
        if n == 102 and fail_102["on"]:
            raise common.HttpError(url, 503, "HTTP 503")
        return common.HttpResponse(url, 200, {}, _nation_page(n))

    monkeypatch.setattr(news_rss, "http_get", fake_get)
    cfg = news_rss.Config(
        1, "x", 0.0, 7, (_feed(fetch_pages=True, page_min_interval=0.0),)
    )
    assert news_rss.run(cfg, upload=False) == 0
    rows = [
        r
        for p in (tmp_path / "rss" / "articles").rglob("*.parquet")
        for r in pq.read_table(p).to_pylist()
    ]
    assert len(rows) == 1  # 102's page failed: not written, retried next run
    r = rows[0]
    assert "Body 101." in r["full_text"] and "More 101." in r["full_text"]
    assert "Other story" not in r["full_text"] and "ad()" not in r["full_text"]
    assert "Tags" not in r["full_text"]
    assert r["full_text_source"] == "article_page" and r["feed_role"] == "primary"
    assert r["author"] == "NEWS WIRE"
    raw = list((tmp_path / "rss" / "raw").rglob("*_synthetic_*.html.gz"))
    assert len(raw) == 1

    import time

    time.sleep(1.1)
    requested.clear()
    fail_102["on"] = False
    news_rss.run(cfg, upload=False)
    pages = [u for u in requested if "/news/" in u]
    assert pages == ["https://www.example.com/news/102"]  # 101 not fetched again
    rows = [
        r
        for p in (tmp_path / "rss" / "articles").rglob("*.parquet")
        for r in pq.read_table(p).to_pylist()
    ]
    assert sorted(r["canonical_url"] for r in rows) == [
        "https://www.example.com/news/101",
        "https://www.example.com/news/102",
    ]


def test_fetch_pages_cap_defers_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_rss, "LOCAL_ROOT", tmp_path / "rss")
    monkeypatch.setattr(
        news_rss,
        "http_get",
        lambda url, **kw: common.HttpResponse(url, 200, {}, _nation_page(1)),
    )
    feed = _feed(fetch_pages=True, max_page_fetch=1, page_min_interval=0.0)
    recs, _ = news_rss.parse_feed(FEED_XML, feed, datetime.now(timezone.utc), "R")
    done, stats, files, warns = news_rss.fetch_feed_pages(
        feed, recs, run_id="R", fetch_date="d"
    )
    assert len(done) == 1 and len(files) == 1
    assert stats["page_deferred"] == len(recs) - 1
    assert "next run" in warns[0]


def test_parse_article_nation_page() -> None:
    from jobs.news_common import parse_article

    f = parse_article(_nation_page(7), "https://www.nation.com.pk/20-Nov-2026/x")
    assert (
        f["full_text"] and "Body 7." in f["full_text"] and "More 7." in f["full_text"]
    )
    assert "Tags" not in f["full_text"] and "Other story" not in f["full_text"]
    assert f["tags"] == ["PSX"] and f["authors"] == ["NEWS WIRE"]
    assert f["published_tz"] == "+05:00"
