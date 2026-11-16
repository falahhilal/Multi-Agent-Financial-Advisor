"""Tests for backend/jobs/news_gdelt_text.py on synthetic pages (no network)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402  (needs the sys.path line above)
from jobs import news_gdelt_text as gt  # noqa: E402

UTC = timezone.utc
SEEN = datetime(2023, 7, 3, 12, 45, tzinfo=UTC)


def _page(body_tag: str, published: str) -> bytes:
    return (
        '<html lang="en"><head><script type="application/ld+json">'
        '{"@type": "NewsArticle", "headline": "Index gains",'
        f' "datePublished": "{published}", "author": {{"name": "Our Correspondent"}}}}'
        f"</script></head><body>{body_tag}</body></html>"
    ).encode()


DAWN = _page(
    '<div class="story__content"><p>Dawn body.</p></div>', "2023-07-03T09:00:00+05:00"
)
TRIBUNE = _page(
    '<span class="story-text"><p>Tribune body.</p></span>', "2023-07-03T23:23:00+05:00"
)
EMPTY = _page("<p>nothing here</p>", "2023-07-03T09:00:00+05:00")


def _link(aid: str, domain: str, url: str) -> dict[str, Any]:
    return {
        "article_id": aid,
        "domain": domain,
        "canonical_url": url,
        "title": "t",
        "seendate": SEEN,
    }


LINKS = [
    _link("d1", "dawn.com", "https://www.dawn.com/news/101"),
    _link("d2", "dawn.com", "https://www.dawn.com/news/102"),
    _link("d3", "dawn.com", "https://www.dawn.com/authors/5/someone"),
    _link("t1", "tribune.com.pk", "https://tribune.com.pk/story/201"),
    _link("b1", "brecorder.com", "https://www.brecorder.com/2017/01/30/335922"),
    _link("b2", "brecorder.com", "https://www.brecorder.com/news/40000290"),
]


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(gt, "LOCAL_ROOT", tmp_path / "text")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    return tmp_path


def test_is_article() -> None:
    assert gt.is_article("dawn.com", "https://www.dawn.com/news/101")
    assert not gt.is_article("dawn.com", "https://www.dawn.com/stocks/2019-09-19/x")
    assert gt.is_article(
        "brecorder.com", "https://www.brecorder.com/2017/04/18/344248/slug-18"
    )
    assert not gt.is_article(
        "brecorder.com", "https://www.brecorder.com/market-data/x/0/582"
    )
    assert not gt.is_article("tribune.com.pk", "https://tribune.com.pk/author/1918/x")


def test_first_public_uses_earlier_time() -> None:
    late = datetime(2023, 7, 3, 18, 23, tzinfo=UTC)
    early = datetime(2023, 7, 3, 4, 0, tzinfo=UTC)
    assert gt.first_public(late, SEEN) == (SEEN, True)
    assert gt.first_public(early, SEEN) == (early, False)
    assert gt.first_public(None, SEEN) == (SEEN, False)


def test_run_statuses_resume_and_dates(
    setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    fail_b2 = {"on": True}

    def fake_get(url: str, **kw: Any) -> common.HttpResponse:
        calls.append(url)
        if url.endswith("/news/102"):
            raise common.HttpError(url, 404, "HTTP 404")
        if url.endswith("/40000290") and fail_b2["on"]:
            raise common.HttpError(url, 503, "HTTP 503")
        if "dawn.com" in url:
            body = DAWN
        elif "tribune" in url:
            body = TRIBUNE
        else:
            body = EMPTY
        final = url.replace("/2017/01/30/335922", "/news/335922/")
        return common.HttpResponse(final, 200, {}, body)

    monkeypatch.setattr(gt, "http_get", fake_get)
    assert gt.run(upload=False, limit=None, links=LINKS) == 0
    rows = {
        r["article_id"]: r
        for r in pq.read_table(setup / "text" / "articles.parquet").to_pylist()
    }
    assert {k: r["status"] for k, r in rows.items()} == {
        "d1": "ok",
        "d2": "gone",
        "d3": "skipped_not_article",
        "t1": "ok",
        "b1": "no_text",
        "b2": "failed",
    }
    assert (
        rows["d1"]["full_text"] == "Dawn body."
        and rows["d1"]["published_after_seen"] is False
    )
    t1 = rows["t1"]
    assert t1["full_text"] == "Tribune body." and t1["authors"] == ["Our Correspondent"]
    assert t1["published_after_seen"] is True and t1["first_public_at"] == SEEN
    assert rows["b1"]["final_url"].endswith("/news/335922/")
    assert (setup / "text" / "raw" / "dawn.com" / "d1.html.gz").is_file()
    assert "https://www.dawn.com/authors/5/someone" not in calls

    calls.clear()
    fail_b2["on"] = False
    gt.run(upload=False, limit=None, links=LINKS)
    assert calls == ["https://www.brecorder.com/news/40000290"]  # only the failed one
    rows = {
        r["article_id"]: r
        for r in pq.read_table(setup / "text" / "articles.parquet").to_pylist()
    }
    assert rows["b2"]["status"] == "no_text" and rows["b2"]["attempts"] == 2
    m = json.loads((setup / "manifests" / "news_gdelt_fulltext.json").read_text())
    assert m["row_count"] == 6 and any("gone" in w for w in m["warnings"])
    cov = (setup / "text" / "coverage_by_month.csv").read_text().splitlines()
    assert cov[0] == "month,domain,gdelt_links,recovered,missing"
    assert "2023-07,dawn.com,2,1,1" in cov


def test_breaker_stops_a_failing_site(
    setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kw: Any) -> common.HttpResponse:
        calls.append(url)
        raise common.HttpError(url, 403, "HTTP 403")

    monkeypatch.setattr(gt, "http_get", fake_get)
    monkeypatch.setattr(gt, "BREAKER", 3)
    links = [
        _link(f"d{i}", "dawn.com", f"https://www.dawn.com/news/{i}") for i in range(10)
    ]
    gt.run(upload=False, limit=None, links=links)
    assert len(calls) == 3  # stopped after 3 failures in a row
