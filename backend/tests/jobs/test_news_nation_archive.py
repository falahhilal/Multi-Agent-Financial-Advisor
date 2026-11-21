"""Tests for backend/jobs/news_nation_archive.py with a fake fetcher (no network)."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402  (needs the sys.path line above)
from jobs import news_nation_archive as na  # noqa: E402

BASE = "https://www.nation.com.pk"
INDEX = (
    b"<sitemapindex>"
    b"<sitemap><loc>https://www.nation.com.pk/sitemap_news.xml</loc></sitemap>"
    b"<sitemap><loc>https://www.nation.com.pk/sitemap_2018-07-11.xml</loc></sitemap>"
    b"<sitemap><loc>https://www.nation.com.pk/sitemap_2018-07-10.xml</loc></sitemap>"
    b"<sitemap><loc>https://www.nation.com.pk/sitemap_2016-12-31.xml</loc></sitemap>"
    b"</sitemapindex>"
)


def _day(day: str, slugs: list[str]) -> bytes:
    d = datetime.strptime(day, "%Y-%m-%d").strftime("%d-%b-%Y").encode()
    urls = b"".join(
        b"<url><loc>https://www.nation.com.pk/%s/%s</loc>"
        b"<lastmod>%sT23:46:56+05:00</lastmod></url>" % (d, s.encode(), day.encode())
        for s in slugs
    )
    return b'<?xml version="1.0"?><urlset>' + urls + b"</urlset>"


DAYS = {
    "2018-07-10": _day(
        "2018-07-10",
        ["market-slips-below-40-000-point-level", "afridi-vows-rs1-5m-for-dams-fund"],
    ),
    "2018-07-11": _day("2018-07-11", ["psx-gains-300-points", "flour-market-prices"]),
}


def _page(title: str, text: str, published: str) -> bytes:
    return (
        b'<html lang="en-us"><head><script type="application/ld+json">'
        b'{"@type": "NewsArticle", "headline": "%s", "datePublished": "%s",'
        b' "author": {"name": "NEWS WIRE"}}</script></head><body>'
        b'<div class="news-detail-content-class"><p>%s</p></div></body></html>'
        % (title.encode(), published.encode(), text.encode())
    )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(na, "LOCAL_ROOT", tmp_path / "nation")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    return tmp_path


def test_parse_index_and_day() -> None:
    assert na.parse_sitemap_index(INDEX) == ["2016-12-31", "2018-07-10", "2018-07-11"]
    rows = na.parse_day_sitemap(DAYS["2018-07-10"], "2018-07-10")
    assert [r["slug"] for r in rows] == [
        "market-slips-below-40-000-point-level",
        "afridi-vows-rs1-5m-for-dams-fund",
    ]
    assert rows[0]["url_day"] == "2018-07-10"
    assert rows[0]["lastmod"] == datetime(2018, 7, 10, 18, 46, 56, tzinfo=timezone.utc)


def test_slug_filter() -> None:
    assert na.slug_matches("psx-gains-300-points")
    assert na.slug_matches("kse-100-index-sheds-500")
    assert na.slug_matches("market-slips-below-40-000-point-level")
    assert not na.slug_matches("afridi-vows-rs1-5m-for-dams-fund")
    assert not na.slug_matches("psxyz-unrelated")  # whole words only


def test_first_public_caps_at_end_of_url_day() -> None:
    late = datetime(2018, 7, 20, tzinfo=timezone.utc)
    early = datetime(2018, 7, 9, 20, 0, tzinfo=timezone.utc)
    end = datetime(2018, 7, 10, 18, 59, 59, tzinfo=timezone.utc)  # 23:59:59 PKT
    assert na.first_public(late, "2018-07-10") == (end, True)
    assert na.first_public(early, "2018-07-10") == (early, False)
    assert na.first_public(None, "2018-07-10") == (end, False)


def test_index_then_text_resume(setup: Path) -> None:
    calls: list[str] = []
    fail = {"page": True}

    def fetch(url: str) -> common.HttpResponse:
        calls.append(url)
        if url.endswith("/sitemap.xml"):
            return common.HttpResponse(url, 200, {}, INDEX)
        m = na._SITEMAP_DAY.search(url)
        if m:
            return common.HttpResponse(url, 200, {}, DAYS[m.group(1)])
        if url.endswith("flour-market-prices"):
            return common.HttpResponse(
                url,
                200,
                {},
                _page("Flour prices", "Flour up.", "2018-07-11T01:00:00+05:00"),
            )
        if url.endswith("psx-gains-300-points"):
            if fail["page"]:
                raise common.HttpError(url, 503, "HTTP 503")
            return common.HttpResponse(
                url,
                200,
                {},
                _page("PSX gains", "The KSE-100 rose.", "2018-07-25T01:00:00+05:00"),
            )
        return common.HttpResponse(
            url,
            200,
            {},
            _page("Market slips", "The PSX fell.", "2018-07-10T01:00:00+05:00"),
        )

    kw: dict[str, Any] = {
        "upload": False,
        "start": date(2018, 7, 10),
        "end": date(2018, 7, 11),
        "fetch": fetch,
    }
    assert na.run("index", **kw) == 0
    days = pq.read_table(setup / "nation" / "days.parquet").to_pylist()
    assert [(d["day"], d["status"], d["n_links"]) for d in days] == [
        ("2018-07-10", "ok", 2),
        ("2018-07-11", "ok", 2),
    ]  # 2016-12-31 is before the start date
    assert (
        setup / "nation" / "raw" / "sitemaps" / "sitemap_2018-07-10.xml.gz"
    ).is_file()

    calls.clear()
    na.run("index", **kw)
    assert calls == [f"{BASE}/sitemap.xml"]  # days already read are not fetched again

    assert na.run("text", **kw) == 0
    arts = {
        r["slug_rule"] + ":" + r["url"].rsplit("/", 1)[1]: r
        for r in pq.read_table(setup / "nation" / "articles.parquet").to_pylist()
    }
    assert set(arts) == {
        "slug-v1:market-slips-below-40-000-point-level",
        "slug-v1:psx-gains-300-points",
        "slug-v1:flour-market-prices",
    }  # the cricket/dams story is not picked
    slips = arts["slug-v1:market-slips-below-40-000-point-level"]
    assert (
        slips["status"] == "ok"
        and slips["is_market"]
        and slips["full_text"] == "The PSX fell."
    )
    flour = arts["slug-v1:flour-market-prices"]
    assert flour["status"] == "ok" and not flour["is_market"]  # slug match, off topic
    assert arts["slug-v1:psx-gains-300-points"]["status"] == "failed"

    calls.clear()
    fail["page"] = False
    na.run("text", **kw)
    assert calls == [f"{BASE}/11-Jul-2018/psx-gains-300-points"]  # only the failed one
    rows = {
        r["url"].rsplit("/", 1)[1]: r
        for r in pq.read_table(setup / "nation" / "articles.parquet").to_pylist()
    }
    gains = rows["psx-gains-300-points"]
    assert gains["status"] == "ok" and gains["attempts"] == 2
    assert gains["published_after_url_day"] is True  # page dated two weeks later
    assert gains["first_public_at"] == datetime(
        2018, 7, 11, 18, 59, 59, tzinfo=timezone.utc
    )
    cov = (setup / "nation" / "coverage_by_month.csv").read_text().splitlines()
    assert cov == ["month,links,picked,ok,market", "2018-07,4,3,3,2"]
    m = json.loads((setup / "manifests" / "news_nation_archive.json").read_text())
    assert m["row_count"] == 3 and m["file_count"] == 4
    assert m["extra"]["last_run"]["remaining"] == 0


def test_breaker_stops_and_fails(setup: Path) -> None:
    def fetch(url: str) -> common.HttpResponse:
        if url.endswith("/sitemap.xml"):
            body = b"".join(
                b"<loc>https://www.nation.com.pk/sitemap_2018-07-%02d.xml</loc>" % d
                for d in range(1, 15)
            )
            return common.HttpResponse(url, 200, {}, body)
        raise common.HttpError(url, 503, "HTTP 503")

    code = na.run(
        "index",
        upload=False,
        start=date(2018, 7, 1),
        end=date(2018, 7, 31),
        fetch=fetch,
    )
    assert code == 1
    days = pq.read_table(setup / "nation" / "days.parquet").to_pylist()
    assert len(days) == na.BREAKER and {d["status"] for d in days} == {"failed"}


def test_time_budget(setup: Path) -> None:
    def fetch(url: str) -> common.HttpResponse:
        if url.endswith("/sitemap.xml"):
            return common.HttpResponse(url, 200, {}, INDEX)
        return common.HttpResponse(url, 200, {}, DAYS["2018-07-10"])

    assert (
        na.run(
            "index",
            upload=False,
            start=date(2018, 7, 10),
            end=date(2018, 7, 11),
            fetch=fetch,
            max_minutes=0,
        )
        == 0
    )
    m = json.loads((setup / "manifests" / "news_nation_archive.json").read_text())
    assert m["extra"]["last_run"]["days_remaining"] == 2
    assert any("stopped early" in w for w in m["warnings"])


def test_text_limit_leaves_rest_for_next_run(setup: Path) -> None:
    def fetch(url: str) -> common.HttpResponse:
        if url.endswith("/sitemap.xml"):
            return common.HttpResponse(url, 200, {}, INDEX)
        m = na._SITEMAP_DAY.search(url)
        if m:
            return common.HttpResponse(url, 200, {}, DAYS[m.group(1)])
        return common.HttpResponse(
            url, 200, {}, _page("PSX", "The PSX fell.", "2018-07-10T01:00:00+05:00")
        )

    kw: dict[str, Any] = {
        "upload": False,
        "start": date(2018, 7, 10),
        "end": date(2018, 7, 11),
        "fetch": fetch,
    }
    na.run("index", **kw)
    assert na.run("text", limit=1, **kw) == 0
    m = json.loads((setup / "manifests" / "news_nation_archive.json").read_text())
    assert m["extra"]["last_run"]["remaining"] == 2 and m["row_count"] == 1


def test_index_reads_days_the_sitemap_index_omits(setup: Path) -> None:
    def fetch(url: str) -> common.HttpResponse:
        if url.endswith("/sitemap.xml"):
            return common.HttpResponse(url, 200, {}, INDEX)  # lists 07-10, 07-11
        m = na._SITEMAP_DAY.search(url)
        assert m
        if m.group(1) == "2018-07-12":  # not in the index, but published
            return common.HttpResponse(
                url, 200, {}, _day("2018-07-12", ["psx-sheds-100-points"])
            )
        if m.group(1) == "2018-07-13":  # not in the index, not published
            raise common.HttpError(url, 404, "HTTP 404")
        return common.HttpResponse(url, 200, {}, DAYS[m.group(1)])

    kw: dict[str, Any] = {
        "upload": False,
        "start": date(2018, 7, 10),
        "end": date(2018, 7, 13),
        "fetch": fetch,
    }
    assert na.run("index", **kw) == 0
    days = {
        d["day"]: (d["status"], d["in_index"])
        for d in pq.read_table(setup / "nation" / "days.parquet").to_pylist()
    }
    assert days == {
        "2018-07-10": ("ok", True),
        "2018-07-11": ("ok", True),
        "2018-07-12": ("ok", False),
        "2018-07-13": ("gone", False),
    }
    links = pq.read_table(setup / "nation" / "links.parquet").to_pylist()
    assert "psx-sheds-100-points" in {r["slug"] for r in links}
    m = json.loads((setup / "manifests" / "news_nation_archive.json").read_text())
    last = m["extra"]["last_run"]
    assert last["days_not_in_sitemap_index"] == 2 and last["unlisted_days_ok"] == 1
    assert last["days_remaining"] == 0
