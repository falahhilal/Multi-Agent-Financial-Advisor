"""Tests for backend/jobs/news_gdelt.py with a fake fetcher (no network)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common, news_gdelt  # noqa: E402  (needs the sys.path line above)

UTC = timezone.utc


def _body(n: int, start: datetime, domain: str = "dawn.com") -> bytes:
    arts = [
        {
            "url": f"https://www.{domain}/news/{1000 + i}/slug-{i}",
            "title": f"T{i}",
            "seendate": (start + timedelta(minutes=i)).strftime("%Y%m%dT%H%M%SZ"),
            "domain": domain,
            "language": "English",
            "sourcecountry": "Pakistan",
        }
        for i in range(n)
    ]
    return json.dumps({"articles": arts}).encode()


def test_query_string_has_no_short_phrases() -> None:
    assert '"PSX"' not in news_gdelt.QUERY  # GDELT rejects phrases under 4 chars
    assert "domainis:dawn.com" in news_gdelt.QUERY


def test_week_windows_cover_range() -> None:
    s = datetime(2017, 1, 1, tzinfo=UTC)
    w = news_gdelt.week_windows(s, s + timedelta(days=17))
    assert w[0] == (s, s + timedelta(days=7))
    assert w[-1] == (s + timedelta(days=14), s + timedelta(days=17))
    assert len(w) == 3


def test_split_when_capped() -> None:
    calls: list[tuple[datetime, datetime]] = []
    s = datetime(2024, 1, 1, tzinfo=UTC)

    def fetch(a: datetime, b: datetime) -> tuple[bytes | None, str | None]:
        calls.append((a, b))
        return (_body(250, a) if b - a > timedelta(days=4) else _body(10, a)), None

    res = news_gdelt.query_window(s, s + timedelta(days=7), fetch)
    assert res.status == "ok" and len(calls) == 3 and res.n_requests == 3
    assert len(res.articles) == 20  # only the two halves' results are kept


def test_failed_and_non_json() -> None:
    s = datetime(2024, 1, 1, tzinfo=UTC)
    res = news_gdelt.query_window(
        s, s + timedelta(days=7), lambda a, b: (None, "HTTP 429")
    )
    assert res.status == "failed" and res.error == "HTTP 429"
    res = news_gdelt.query_window(
        s,
        s + timedelta(days=7),
        lambda a, b: (b"The specified phrase is too short.", None),
    )
    assert res.status == "failed" and "too short" in (res.error or "")


def test_needs_query() -> None:
    now = datetime(2026, 11, 9, tzinfo=UTC)
    s = datetime(2024, 1, 1, tzinfo=UTC)
    row = {"status": "ok", "window_start": s, "window_end": s + timedelta(days=7)}
    assert not news_gdelt.needs_query(row, now)
    assert news_gdelt.needs_query({**row, "status": "failed"}, now)
    assert news_gdelt.needs_query({**row, "window_end": s + timedelta(days=3)}, now)
    assert news_gdelt.needs_query(None, now)
    recent = now - timedelta(days=5)
    assert news_gdelt.needs_query(
        {
            "status": "ok",
            "window_start": recent,
            "window_end": recent + timedelta(days=7),
        },
        now,
    )


def test_canonical_matches_rss_ids() -> None:
    pats = news_gdelt._canonical_patterns()
    assert news_gdelt.gdelt_canonical(
        "http://www.dawn.com/news/123/some-slug", pats
    ) == ("https://www.dawn.com/news/123")


def test_run_resumes_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_gdelt, "LOCAL_ROOT", tmp_path / "gdelt")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    start = datetime(2017, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=21)
    fail_second = {"on": True}

    def fetch(a: datetime, b: datetime) -> tuple[bytes | None, str | None]:
        if fail_second["on"] and a == start + timedelta(days=7):
            return None, "HTTP 429"
        return _body(3, start), None  # same 3 URLs every window -> dedupe

    assert (
        news_gdelt.run(
            upload=False, start=start, end=end, max_windows=None, fetch=fetch
        )
        == 0
    )
    w = pq.read_table(tmp_path / "gdelt" / "windows.parquet").to_pylist()
    assert [x["status"] for x in w] == ["ok", "failed", "ok"]
    assert pq.read_table(tmp_path / "gdelt" / "articles.parquet").num_rows == 3

    fail_second["on"] = False
    calls: list[datetime] = []

    def fetch2(a: datetime, b: datetime) -> tuple[bytes | None, str | None]:
        calls.append(a)
        return fetch(a, b)

    news_gdelt.run(upload=False, start=start, end=end, max_windows=None, fetch=fetch2)
    assert calls == [start + timedelta(days=7)]  # only the failed window is retried
    w = pq.read_table(tmp_path / "gdelt" / "windows.parquet").to_pylist()
    assert [x["status"] for x in w] == ["ok", "ok", "ok"] and w[1]["attempts"] == 2
    m = json.loads((tmp_path / "manifests" / "news_gdelt_index.json").read_text())
    assert m["row_count"] == 3 and m["fetch_mode"] == "reproducible"


def test_all_failed_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(news_gdelt, "LOCAL_ROOT", tmp_path / "gdelt")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    start = datetime(2017, 1, 1, tzinfo=UTC)
    code = news_gdelt.run(
        upload=False,
        start=start,
        end=start + timedelta(days=14),
        max_windows=None,
        fetch=lambda a, b: (None, "HTTP 429"),
    )
    assert code == 1
