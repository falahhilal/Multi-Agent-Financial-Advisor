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
    assert len(cfg.feeds) == 7


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
