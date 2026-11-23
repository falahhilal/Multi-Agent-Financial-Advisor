"""Tests for backend/jobs/macro_us_effr.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``. Records are
synthetic, shaped like the manual download of ``last/1.json`` (2026-11-23):
``{"refRates": [{"effectiveDate", "type": "EFFR", "percentRate", ...}]}``
with numbers as JSON numbers and ``revisionIndicator`` "" when not revised.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402
from jobs import macro_us_effr as fe  # noqa: E402

TODAY = date(2026, 11, 23)
START = date(2026, 8, 3)
LAST = date(2026, 11, 20)
HOLIDAYS = {date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11)}
PROBE_PATH = "/api/rates/unsecured/effr/last/1.json"
SEARCH_PATH = "/api/rates/unsecured/effr/search.json"


def business_days(start: date = START, last: date = LAST) -> list[date]:
    out, d = [], start
    while d <= last:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def record(d: date, rate: str = "3.88", **override: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "effectiveDate": d.isoformat(),
        "type": "EFFR",
        "percentRate": rate,
        "percentPercentile1": "3.85",
        "percentPercentile25": "3.87",
        "percentPercentile75": "3.89",
        "percentPercentile99": "3.95",
        "targetRateFrom": "3.75",
        "targetRateTo": "4.00",
        "volumeInBillions": "100",
        "revisionIndicator": "",
    }
    rec.update(override)
    return {k: v for k, v in rec.items() if v is not None}


def encode(records: list[dict[str, Any]]) -> bytes:
    """JSON with numeric fields as numbers, written with their exact text
    (as the API does, e.g. 3.90)."""
    parts = []
    for rec in records:
        fields = []
        for k, v in rec.items():
            numeric = isinstance(v, str) and k not in ("effectiveDate", "type", "revisionIndicator", "footnoteId", "targetRange")
            try:
                float(v) if numeric else None
            except ValueError:
                numeric = False
            fields.append(f'"{k}": {v}' if numeric else f'"{k}": {json.dumps(v)}')
        parts.append("{ " + ", ".join(fields) + " }")
    return ('{ "refRates": [ ' + ", ".join(parts) + " ] }").encode()


class FakeApi:
    def __init__(self) -> None:
        self.records = [record(d) for d in business_days()]
        self.routes: dict[str, Any] = {}
        self.requests: list[str] = []
        self.refresh()

    def refresh(self) -> None:
        self.routes[SEARCH_PATH] = encode(self.records)
        self.routes[PROBE_PATH] = encode([self.records[-1]])

    def set(self, d: date, **override: Any) -> None:
        self.records = [record(r_d, **{**r, **override}) if (r_d := date.fromisoformat(r["effectiveDate"])) == d else r for r in self.records]
        self.refresh()

    def __call__(self, url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "markets.newyorkfed.org", url
        if parts.path == "/robots.txt":
            return common.HttpResponse(url, 404, {}, b"")
        self.requests.append(url)
        route = self.routes.get(parts.path)
        if route is None:
            return common.HttpResponse(url, 404, {}, b"not found")
        if isinstance(route, int):
            return common.HttpResponse(url, route, {}, b"error")
        return common.HttpResponse(url, 200, {"content-type": "application/json"}, route)

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(u).path for u in self.requests]

    def params(self, i: int) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.requests[i]).query))


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(common, "_fetch_once", api)  # no real network, ever
    monkeypatch.setattr(common, "_robots_cache", {})
    monkeypatch.setattr(common, "_wait_for_host", lambda host, interval: None)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    monkeypatch.setattr(common, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(common, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(fe, "LOCAL_ROOT", tmp_path / "data" / "macro" / "us_effr")
    monkeypatch.setattr(fe, "_today", lambda: TODAY)
    monkeypatch.setattr(fe, "EXPECTED_START", START)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)
    monkeypatch.setattr(fe, "git_info", clean_git)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(fe, "upload_to_hf", no_upload)
    monkeypatch.setattr(fe, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "us_effr"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_us_effr.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[str, dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / fe.PROCESSED_REL).to_pylist()
    return {r["observation_date"].isoformat(): r for r in rows}


def warnings_text(tmp_path: Path) -> str:
    return "\n".join(manifest(tmp_path)["warnings"])


def clear_run_files(tmp_path: Path) -> None:
    for sub in ("raw", "interim", "runs"):
        shutil.rmtree(root(tmp_path) / sub)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert fe.run(upload=False) == 0

    assert env.paths() == [PROBE_PATH, SEARCH_PATH]
    assert env.params(0) == {}
    assert env.params(1) == {"startDate": "1954-07-01", "endDate": TODAY.isoformat()}

    raw = sorted(p.name for p in (root(tmp_path) / "raw").rglob("*.json"))
    assert len(raw) == 2 and raw[0].endswith("_effr_last1.json") and raw[1].endswith("_effr_search.json")
    assert next((root(tmp_path) / "raw").rglob("*_effr_search.json")).read_bytes() == env.routes[SEARCH_PATH]

    rows = processed(tmp_path)
    assert len(rows) == len(business_days())
    r = rows["2026-09-04"]  # Friday before Labor Day
    assert r["series_key"] == "EFFR" and r["series_name"] == "Effective Federal Funds Rate"
    assert r["value"] == 3.88 and r["unit"] == "Percent"
    assert (r["percentile_1"], r["percentile_99"]) == (3.85, 3.95)
    assert (r["target_rate_from"], r["target_rate_to"], r["volume_billions"]) == (3.75, 4.0, 100.0)
    assert r["observation_status"] is None and r["status_comments"] is None
    assert r["target_range"] is None and r["intraday_low"] is None
    # Next EFFR date is Tue 2026-09-08 (Labor Day skipped); + 1 day, 23:59:59 PKT.
    assert r["published_at"] == datetime(2026, 9, 9, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "effr_next_business_day_plus_1d"
    assert r["source_endpoint"] == "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
    last = rows[LAST.isoformat()]
    assert last["published_at"] == datetime(2026, 11, 23, 18, 59, 59, tzinfo=timezone.utc)  # fetch date
    assert last["published_at_basis"] == "effr_latest_row_fetch_date"

    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert interim[0]["percentile_99"] == "3.95" and interim[0]["target_rate_to"] == "4.00"  # JSON text kept

    m = manifest(tmp_path)
    assert m["id"] == "macro_us_effr" and m["script"] == "backend/jobs/macro_us_effr.py"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["row_count"] == len(rows)
    assert m["date_range"] == {"min": START.isoformat(), "max": LAST.isoformat()}
    assert m["warnings"] == []
    assert m["extra"]["citation"] == (
        "Federal Reserve Bank of New York, Effective Federal Funds Rate (EFFR), daily, 03-Aug-2026 to "
        f"20-Nov-2026, New York Fed Markets Data API (markets.newyorkfed.org), retrieved "
        f"{datetime.now(timezone.utc).strftime('%d-%b-%Y')}. © {datetime.now(timezone.utc).year} Federal Reserve "
        "Bank of New York. Content from the New York Fed subject to the Terms of Use at newyorkfed.org."
    )
    assert m["extra"]["reference_rate_notice"].startswith("The Effective Federal Funds Rate (EFFR) is subject to")
    assert caplog.records[-1].getMessage().endswith("; 0 warning(s); exit code 0")


def test_upload_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_upload(local: Any, path_in_repo: str, stream: str, **kw: Any) -> str:
        common.check_hf_path(path_in_repo, stream)
        local = Path(local)
        names = sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()) if local.is_dir() else [local.name]
        calls.append((path_in_repo, names))
        return "rev1"

    monkeypatch.setattr(fe, "upload_to_hf", fake_upload)
    assert fe.run(upload=True) == 0
    assert [c[0] for c in calls] == ["macro/us_effr/", "macro/us_effr/manifest.json"]
    assert "processed/us_effr.parquet" in calls[0][1] and len(calls[0][1]) == 5  # 2 raw, interim, processed, run log
    assert manifest(tmp_path)["hf_revision"] == "rev1"


# --------------------------------------------------------------------------
# Probe: checked before the full request
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "probe",
    [
        encode([record(LAST, type="SOFR")]),
        encode([record(LAST, percentRate=None)]),
        encode([record(LAST, newField="1")]),
        encode([record(LAST, targetRange="0-0.25")]),  # old field: not allowed in the probe
        encode([record(LAST), record(LAST)]),
        encode([]),
        b'{"rates": []}',
        b"<html>maintenance</html>",
    ],
)
def test_probe_mismatch_aborts_before_search(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, probe: bytes) -> None:
    env.routes[PROBE_PATH] = probe
    assert fe.run(upload=False) == 1
    assert env.paths() == [PROBE_PATH]
    assert not (tmp_path / "data").exists()
    assert "probe" in caplog.text


def test_http_error_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.routes[SEARCH_PATH] = 400
    assert fe.run(upload=False) == 1
    assert "EFFR search failed: HTTP 400" in caplog.text
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------
# Failures in the full history
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"type": "OBFR"}, "type 'OBFR'"),
        ({"percentRate": ""}, "empty percentRate"),
        ({"percentRate": "n/a"}, "non-numeric percent_rate"),
        ({"volumeInBillions": "lots"}, "non-numeric volume_billions"),
        ({"effectiveDate": "09/04/2026"}, "unrecognised date"),
    ],
)
def test_bad_record_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, change: dict[str, str], match: str) -> None:
    env.records[5].update(change)
    env.refresh()
    env.routes[PROBE_PATH] = encode([record(LAST)])
    assert fe.run(upload=False) == 1
    assert match in caplog.text
    assert len(list((root(tmp_path) / "raw").rglob("*.json"))) == 2  # raw kept
    assert not (root(tmp_path) / fe.PROCESSED_REL).exists()


def test_duplicate_and_future_dates_fail(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.records.append(dict(env.records[-1]))
    env.refresh()
    assert fe.run(upload=False) == 1
    assert "duplicate effectiveDate" in caplog.text

    shutil.rmtree(tmp_path / "data")
    env.records = [record(d) for d in business_days()] + [record(date(2026, 11, 24))]
    env.refresh()
    assert fe.run(upload=False) == 1
    assert "observation after end_date: 2026-11-24" in caplog.text


def test_single_row_fails(env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.records = env.records[-1:]
    env.refresh()
    assert fe.run(upload=False) == 1
    assert "1 row(s) returned" in caplog.text


# --------------------------------------------------------------------------
# Warnings
# --------------------------------------------------------------------------


def test_warnings(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fe, "EXPECTED_START", date(2000, 7, 3))
    days = business_days()
    gone = set(days[10:14])  # a gap of more than 4 days
    env.records = [r for r in env.records if date.fromisoformat(r["effectiveDate"]) not in gone]
    env.records.append(record(date(2026, 8, 8)))  # Saturday
    env.records.sort(key=lambda r: r["effectiveDate"])
    env.refresh()
    env.set(days[20], percentRate="4.30", percentPercentile99="4.40", percentPercentile75="4.35")  # big move, outside target
    env.set(days[30], percentPercentile1="3.95")  # p1 > rate
    env.set(days[31], volumeInBillions="0")
    env.set(days[32], percentRate="-0.05", percentPercentile1="-0.10", percentPercentile25="-0.06")
    env.set(days[40], footnoteId="1", extraThing="x")
    env.routes[PROBE_PATH] = encode([record(LAST, percentRate="3.99")])
    assert fe.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "first observation 2026-08-03 differs from expected start 2000-07-03" in w
    assert "weekend date(s) not in KNOWN_WEEKEND_DATES: 1: 2026-08-08" in w
    assert "with no target change, not in KNOWN_MOVES: " in w
    assert f"gap(s) of more than 4 days: 1: {days[9]} to {days[14]}" in w
    assert f"rate(s) outside 0-20: 1: {days[32]} (-0.05)" in w
    assert f"{days[20]} (3.88 -> 4.3, +0.42 pp)" in w
    assert f"rate(s) outside the target range +/- 0.1 pp: 2: {days[20]} (4.3 vs 3.75-4)" in w
    assert f"percentiles out of order (p1 <= p25 <= rate <= p75 <= p99): 1: {days[30]}" in w
    assert f"volume <= 0: 1: {days[31]}" in w
    assert "unknown field names in the full history (kept in interim extra_fields): ['extraThing']" in w
    assert f"probe rate 3.99 on {LAST} differs from the full history (3.88)" in w
    assert processed(tmp_path)[days[40].isoformat()]["status_comments"] == "1"


def test_reviewed_constants() -> None:
    assert fe.KNOWN_WEEKEND_DATES == {date(2003, 7, 20)}
    assert fe.REQUEST_START == date(1954, 7, 1)
    assert fe.KNOWN_MOVES == {
        date(2000, 12, 29), date(2001, 1, 2), date(2001, 9, 20), date(2008, 1, 2), date(2008, 10, 7)
    }


def test_known_move_not_warned(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    days = business_days()
    env.set(days[20], percentRate="4.05", percentPercentile75="4.06", percentPercentile99="4.10")  # +0.17: below threshold
    env.set(days[40], percentRate="4.30", percentPercentile75="4.35", percentPercentile99="4.40")  # +0.42 and back
    monkeypatch.setattr(fe, "KNOWN_MOVES", frozenset({days[40], days[41]}))
    assert fe.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "not in KNOWN_MOVES" not in w
    checks = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["checks"]
    assert checks["reviewed_large_moves"] == [f"{days[40]} (3.88 -> 4.3, +0.42 pp)", f"{days[41]} (4.3 -> 3.88, -0.42 pp)"]
    assert checks["large_moves"] == []


def test_known_weekend_date_not_warned(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    sunday = date(2026, 8, 9)
    env.records.append(record(sunday))
    env.records.sort(key=lambda r: r["effectiveDate"])
    env.refresh()
    monkeypatch.setattr(fe, "KNOWN_WEEKEND_DATES", frozenset({sunday}))
    assert fe.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    assert sunday.isoformat() in processed(tmp_path)  # kept as returned
    checks = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["checks"]
    assert checks["weekend_dates"] == [sunday.isoformat()]


def test_move_with_target_change_is_not_warned(tmp_path: Path, env: FakeApi) -> None:
    days = business_days()
    hike = days[30:]  # FOMC raises the range by 0.50 from days[30]
    for d in hike:
        env.set(
            d, percentRate="4.38", percentPercentile1="4.35", percentPercentile25="4.37",
            percentPercentile75="4.39", percentPercentile99="4.45", targetRateFrom="4.25", targetRateTo="4.50",
        )
    env.set(days[50], percentRate="4.80", percentPercentile75="4.85", percentPercentile99="4.90")  # unexplained
    assert fe.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert f"{days[30]} (3.88 -> 4.38" not in w
    assert f"with no target change, not in KNOWN_MOVES: 2: {days[50]} (4.38 -> 4.8, +0.42 pp)" in w
    checks = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["checks"]
    assert checks["large_moves_with_target_change"] == [f"{days[30]} (3.88 -> 4.38, +0.50 pp)"]
    assert checks["target_changes"] == 1


def test_single_target_change_before_2008_is_a_target_change(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    # Before 2008-12-16 the API gives targetRateFrom only (a single target).
    old = [
        {"effectiveDate": d.isoformat(), "type": "EFFR", "percentRate": "3.00" if d < date(2007, 9, 18) else "4.30",
         "targetRateFrom": "3.00" if d < date(2007, 9, 18) else "4.25", "intraDayLow": "2.9", "intraDayHigh": "4.5",
         "stdDeviation": "0.05", "revisionIndicator": ""}
        for d in business_days(date(2007, 9, 10), date(2007, 9, 28))
    ]
    env.records = old + env.records
    env.refresh()
    monkeypatch.setattr(fe, "EXPECTED_START", date(2007, 9, 10))
    assert fe.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "2007-09-18 (3 -> 4.3" not in w  # +1.30 pp, but the target changed
    checks = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["checks"]
    assert checks["large_moves_with_target_change"] == ["2007-09-18 (3 -> 4.3, +1.30 pp)"]


def test_stale_warns(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fe, "_today", lambda: date(2026, 11, 28))
    assert fe.run(upload=False) == 0
    assert "latest date 2026-11-20 is 8 days before 2026-11-28 (more than 7)" in warnings_text(tmp_path)


def test_old_fields_before_2016(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    old = [
        {"effectiveDate": d.isoformat(), "type": "EFFR", "percentRate": "0.12", "targetRange": "0-0.25",
         "intraDayLow": "0.05", "intraDayHigh": "0.25", "stdDeviation": "0.03", "revisionIndicator": ""}
        for d in business_days(date(2015, 12, 1), date(2015, 12, 31))
    ]
    env.records = old + env.records
    env.refresh()
    monkeypatch.setattr(fe, "EXPECTED_START", date(2015, 12, 1))
    assert fe.run(upload=False) == 0
    r = processed(tmp_path)["2015-12-01"]
    assert r["target_range"] == "0-0.25" and r["intraday_high"] == 0.25 and r["std_deviation"] == 0.03
    assert r["target_rate_from"] is None and r["percentile_1"] is None
    w = warnings_text(tmp_path)
    assert "2015-12-31 to 2026-08-03" in w  # the gap between the two synthetic spans
    # Moves are checked within each methodology period, not across 2016-03-01.
    assert "0.12 -> 3.88" not in w


# --------------------------------------------------------------------------
# Revisions between runs
# --------------------------------------------------------------------------


def test_revisions_and_revision_indicator(tmp_path: Path, env: FakeApi) -> None:
    assert fe.run(upload=False) == 0
    clear_run_files(tmp_path)
    days = business_days()
    env.set(days[3], percentRate="3.90")
    env.set(days[4], revisionIndicator="Revised")
    assert fe.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert f"revised values since previous run: ['EFFR {days[3]}: 3.88 -> 3.9']" in w
    assert f"EFFR {days[4]}: status None -> 'Revised'" in w
    assert manifest(tmp_path)["extra"]["last_run"]["revisions"]["changed"] == 1
