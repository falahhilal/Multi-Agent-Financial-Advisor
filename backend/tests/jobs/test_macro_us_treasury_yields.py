"""Tests for backend/jobs/macro_us_treasury_yields.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``. Feeds are
synthetic, shaped like the manual download of the 2026 feed (2026-11-23): an
Atom feed titled ``DailyTreasuryYieldCurveRateData`` whose entries hold
``m:properties`` with ``d:Id`` (Edm.Int32), ``d:NEW_DATE`` (Edm.DateTime,
``YYYY-MM-DDT00:00:00``), the ``d:BC_*`` maturities (Edm.Double) and
``d:BC_30YEARDISPLAY``. To keep the tests small, the years run from 2024 and
the series table is replaced by one with starts in that range.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import sys
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402
from jobs import macro_us_treasury_yields as ty  # noqa: E402
from jobs import us_macro as um  # noqa: E402

TODAY = date(2026, 11, 23)
LAST = date(2026, 11, 20)
FIRST_YEAR = 2024
YEARS = [2024, 2025, 2026]
HOLIDAYS = {
    date(2024, 1, 1), date(2024, 7, 4), date(2024, 12, 25),
    date(2025, 1, 1), date(2025, 7, 4), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 7, 3),
}
FEED_PATH = "/resource-center/data-chart-center/interest-rates/pages/xml"
START = date(2024, 1, 2)
GAP = (date(2024, 6, 3), date(2024, 6, 28))
REAL_SERIES = ty.SERIES  # captured before the autouse fixture replaces it
REAL_BY_CODE = dict(ty.BY_CODE)
REAL_CONSTANTS = (ty.FIRST_YEAR, ty.EXPECTED_FIRST_DATE, ty.EXPECTED_TITLE)
TEST_SERIES = tuple(
    dataclasses.replace(
        s,
        start=date(2025, 2, 18) if s.code == "BC_1_5MONTH" else START,
        gap=GAP if s.code == "BC_30YEAR" else None,
    )
    for s in ty.SERIES
)
CODES = [s.code for s in TEST_SERIES]

Override = dict[str, Any]  # element -> value text, None (omit), or (m:type, text) / {"null": True}


def trading_days(year: int) -> list[date]:
    d, out = date(year, 1, 1), []
    while d.year == year and d <= LAST:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def base_value(code: str, d: date) -> str:
    s = next(x for x in TEST_SERIES if x.code == code)
    return f"{3 + s.months / 100 + (d.toordinal() % 5) * 0.01:.2f}"


def default_values(d: date) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for s in TEST_SERIES:
        inside = d >= s.start and not (s.gap and s.gap[0] <= d <= s.gap[1])
        out[s.code] = base_value(s.code, d) if inside else None
    out["BC_30YEARDISPLAY"] = out["BC_30YEAR"] or "0.00"
    return out


def entry_xml(i: int, d: date, values: dict[str, Any], date_type: str = "Edm.DateTime") -> str:
    props = [
        f'<d:Id m:type="Edm.Int32">{i}</d:Id>',
        f'<d:NEW_DATE m:type="{date_type}">{d.isoformat()}T00:00:00</d:NEW_DATE>',
    ]
    for name, v in values.items():
        if v is None:
            continue
        if isinstance(v, dict):
            props.append(f'<d:{name} m:type="Edm.Double" m:null="true" />')
        elif isinstance(v, tuple):
            props.append(f'<d:{name} m:type="{v[0]}">{v[1]}</d:{name}>')
        else:
            props.append(f'<d:{name} m:type="Edm.Double">{v}</d:{name}>')
    return (
        "<entry><id>x</id><title type=\"text\"></title><updated>2026-11-22T01:01:49Z</updated>"
        '<content type="application/xml"><m:properties>' + "".join(props) + "</m:properties></content></entry>"
    )


def feed_xml(entries: list[str], title: str = "DailyTreasuryYieldCurveRateData", prolog: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes" ?>' + prolog
        + '<feed xml:base="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml" '
        'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
        'xmlns="http://www.w3.org/2005/Atom">'
        f'<title type="text">{title}</title><id>x</id><updated>2026-11-22T01:01:49Z</updated>'
        + "".join(entries) + "</feed>"
    ).encode()


class FakeApi:
    def __init__(self) -> None:
        self.days: dict[int, list[date]] = {y: trading_days(y) for y in YEARS}
        self.overrides: dict[date, Override] = {}
        self.bodies: dict[int, Any] = {}
        self.requests: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.stalls: dict[int, int] = {}  # year -> number of attempts that stall

    def bounded(self, url: str, headers: Any, timeout: float, total: float, max_bytes: int) -> common.HttpResponse:
        """Stands in for us_macro._fetch_bounded (yearly feed requests)."""
        self.headers.append(dict(headers))
        year = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["field_tdr_date_value"])
        if self.stalls.get(year):
            self.stalls[year] -= 1
            self.requests.append(url)
            raise um.Stalled(4096, total + 1)
        return self(url, headers, timeout)

    def body(self, year: int) -> Any:
        if year in self.bodies:
            return self.bodies[year]
        entries = [
            entry_xml(i, d, {**default_values(d), **self.overrides.get(d, {})}) for i, d in enumerate(self.days[year])
        ]
        return feed_xml(entries)

    def __call__(self, url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "home.treasury.gov", url
        if parts.path == "/robots.txt":
            return common.HttpResponse(url, 200, {}, b"User-agent: *\nAllow: /\n")
        self.requests.append(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert parts.path == FEED_PATH and q["data"] == "daily_treasury_yield_curve", url
        out = self.body(int(q["field_tdr_date_value"]))
        if isinstance(out, int):
            return common.HttpResponse(url, out, {}, b"error")
        return common.HttpResponse(url, 200, {"content-type": "text/xml"}, out)

    def years(self) -> list[int]:
        return [int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["field_tdr_date_value"]) for u in self.requests]


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(common, "_fetch_once", api)  # robots.txt; no real network, ever
    monkeypatch.setattr(um, "_fetch_bounded", api.bounded)  # the yearly feed requests
    monkeypatch.setattr(common, "_robots_cache", {})
    monkeypatch.setattr(common, "_wait_for_host", lambda host, interval: None)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    monkeypatch.setattr(common, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(common, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(ty, "LOCAL_ROOT", tmp_path / "data" / "macro" / "us_treasury_yields")
    monkeypatch.setattr(ty, "_today", lambda: TODAY)
    monkeypatch.setattr(ty, "FIRST_YEAR", FIRST_YEAR)
    monkeypatch.setattr(ty, "SERIES", TEST_SERIES)
    monkeypatch.setattr(ty, "BY_CODE", {s.code: s for s in TEST_SERIES})
    monkeypatch.setattr(ty, "EXPECTED_FIRST_DATE", START)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)
    monkeypatch.setattr(ty, "git_info", clean_git)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(ty, "upload_to_hf", no_upload)
    monkeypatch.setattr(ty, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "us_treasury_yields"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_us_treasury_yields.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / ty.PROCESSED_REL).to_pylist()
    return {(r["series_code"], r["observation_date"].isoformat()): r for r in rows}


def warnings_text(tmp_path: Path) -> str:
    return "\n".join(manifest(tmp_path)["warnings"])


def test_series_table() -> None:
    assert [s.code for s in REAL_SERIES] == [
        "BC_1MONTH", "BC_1_5MONTH", "BC_2MONTH", "BC_3MONTH", "BC_4MONTH", "BC_6MONTH", "BC_1YEAR",
        "BC_2YEAR", "BC_3YEAR", "BC_5YEAR", "BC_7YEAR", "BC_10YEAR", "BC_20YEAR", "BC_30YEAR",
    ]
    starts = {s.code: s.start for s in REAL_SERIES}
    assert starts["BC_1MONTH"] == date(2001, 7, 31) and starts["BC_2MONTH"] == date(2018, 10, 16)
    assert starts["BC_4MONTH"] == date(2022, 10, 19) and starts["BC_1_5MONTH"] == date(2025, 2, 18)
    assert starts["BC_20YEAR"] == date(1993, 10, 1) and starts["BC_10YEAR"] == date(1990, 1, 2)
    assert REAL_BY_CODE["BC_30YEAR"].gap == (date(2002, 2, 18), date(2006, 2, 8))
    assert sum(s.gap is not None for s in REAL_SERIES) == 1
    assert ty.KNOWN_ELEMENTS == set(REAL_BY_CODE) | {"Id", "NEW_DATE", "BC_30YEARDISPLAY"}
    assert REAL_CONSTANTS == (1990, date(1990, 1, 2), "DailyTreasuryYieldCurveRateData")


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi) -> None:
    assert ty.run(upload=False) == 0
    assert env.years() == [2025, 2024, 2026]  # probe (last complete year) first

    raws = sorted((root(tmp_path) / "raw").rglob("*.xml"))
    assert [p.name.split("_par_yield_")[1] for p in raws] == ["2024.xml", "2025.xml", "2026.xml"]
    assert raws[2].read_bytes() == env.body(2026)

    rows = processed(tmp_path)
    r = rows[("BC_10YEAR", LAST.isoformat())]
    assert r["series_key"] == "BC_10YEAR" and r["series_name"] == "Treasury par yield, 10 Yr"
    assert r["maturity_label"] == "10 Yr" and r["maturity_months"] == 120.0
    assert r["value"] == float(base_value("BC_10YEAR", LAST)) and r["unit"] == "Percent"
    assert r["published_at"] == datetime(2026, 11, 21, 18, 59, 59, tzinfo=timezone.utc)  # Friday + 1 day
    assert r["published_at_basis"] == "treasury_same_day_plus_1d"
    assert r["feed_updated"] == "2026-11-22T01:01:49Z"
    assert r["source_endpoint"] == (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
        "?data=daily_treasury_yield_curve&field_tdr_date_value=2026"
    )
    assert ("BC_1_5MONTH", "2025-02-14") not in rows and ("BC_1_5MONTH", "2025-02-18") in rows
    assert ("BC_30YEAR", "2024-06-14") not in rows and ("BC_30YEAR", "2024-07-01") in rows
    assert not any(code == "BC_30YEARDISPLAY" for code, _ in rows)

    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert {r["element"] for r in interim} == set(CODES) | {"BC_30YEARDISPLAY"}

    m = manifest(tmp_path)
    assert m["id"] == "macro_us_treasury_yields" and m["fetch_mode"] == "append_only_snapshot"
    assert m["warnings"] == []
    assert m["date_range"] == {"min": "2024-01-02", "max": LAST.isoformat()}
    assert m["extra"]["dates_per_year"] == {str(y): len(env.days[y]) for y in YEARS}
    assert m["extra"]["rows_per_series"]["BC_10YEAR"] == sum(len(v) for v in env.days.values())
    assert m["extra"]["citation"].startswith(
        "U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates, 02-Jan-2024 to 20-Nov-2026, "
        "home.treasury.gov interest rate XML feed (daily_treasury_yield_curve), retrieved "
    )
    assert m["row_count"] == len(rows)


def test_progress_lines_and_request_policy(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert ty.run(upload=False) == 0
    text = caplog.text
    assert "Treasury: 3 years (2024-2026), 5 s apart, 120 s limit per request, 3 attempts" in text
    assert "Treasury 2025: requesting (1/3)" in text and "Treasury 2025 (probe): " in text
    assert "Treasury 2024: requesting (2/3)" in text and "Treasury 2026: requesting (3/3)" in text
    assert f"Treasury 2024: received {len(env.body(2024))} bytes in " in text
    assert f"Treasury 2024: {len(env.days[2024])} entries" in text
    for h in env.headers:
        assert h["User-Agent"] == common.USER_AGENT and h["Accept-Encoding"] == "gzip"
        assert h["Accept"].startswith("application/xml")
    req = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["requests"]["2024"]
    assert req["content_encoding"] == "none" and "seconds" in req


def test_stalled_year_is_retried(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.stalls[2024] = 1
    assert ty.run(upload=False) == 0
    assert env.years() == [2025, 2024, 2024, 2026]
    assert "Treasury feed 2024: stalled: 4096 bytes after 121 s (limit 120 s); retry 1/2 in 30 s" in caplog.text
    assert manifest(tmp_path)["warnings"] == []


def test_year_that_keeps_stalling_fails_naming_the_year(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.stalls[2024] = 3
    assert ty.run(upload=False) == 1
    assert env.years() == [2025, 2024, 2024, 2024]
    assert "Treasury feed 2024: no complete response after 3 attempt(s) (last: stalled: 4096 bytes after 121 s" in caplog.text
    assert not (tmp_path / "data").exists()  # nothing written before every year has arrived


def test_upload_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_upload(local: Any, path_in_repo: str, stream: str, **kw: Any) -> str:
        common.check_hf_path(path_in_repo, stream)
        local = Path(local)
        names = sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()) if local.is_dir() else [local.name]
        calls.append((path_in_repo, names))
        return "rev2"

    monkeypatch.setattr(ty, "upload_to_hf", fake_upload)
    assert ty.run(upload=True) == 0
    assert [c[0] for c in calls] == ["macro/us_treasury_yields/", "macro/us_treasury_yields/manifest.json"]
    assert len(calls[0][1]) == 6 and "processed/us_treasury_yields.parquet" in calls[0][1]


# --------------------------------------------------------------------------
# Probe and shape failures: nothing written
# --------------------------------------------------------------------------


def _probe_entries(**override: Any) -> list[str]:
    return [entry_xml(i, d, {**default_values(d), **override}) for i, d in enumerate(trading_days(2025))]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (lambda: feed_xml(_probe_entries(), title="SomethingElse"), "feed title"),
        (lambda: feed_xml([entry_xml(0, date(2025, 1, 2), default_values(date(2025, 1, 2)), date_type="Edm.String")]), "NEW_DATE has m:type"),
        (lambda: feed_xml(_probe_entries(BC_10YEAR=("Edm.String", "4.1"))), "BC_10YEAR has m:type"),
        (lambda: feed_xml(_probe_entries(BC_10YEAR=None)), "no BC_10YEAR element"),
        (lambda: feed_xml([]), "no entries"),
        (lambda: feed_xml(_probe_entries(), prolog='<!DOCTYPE feed [<!ENTITY x "y">]>'), "DTD"),
        (lambda: b"<html><body>Access denied</body></html>", "not an Atom feed"),
        (lambda: b"not xml at all", "does not parse"),
    ],
)
def test_probe_failure_aborts_before_other_years(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, body: Callable[[], bytes], match: str
) -> None:
    env.bodies[2025] = body()
    assert ty.run(upload=False) == 1
    assert env.years() == [2025]
    assert match in caplog.text
    assert not (tmp_path / "data").exists()


def test_later_year_shape_failure_writes_nothing(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.bodies[2024] = feed_xml([], title="Other")
    assert ty.run(upload=False) == 1
    assert env.years() == [2025, 2024]
    assert "Treasury feed 2024: feed title" in caplog.text
    assert not (tmp_path / "data").exists()


def test_http_error(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.bodies[2026] = 404
    assert ty.run(upload=False) == 1
    assert "Treasury feed 2026 failed: HTTP 404" in caplog.text
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------
# Validation failures (raw kept, no processed)
# --------------------------------------------------------------------------


def _fails(tmp_path: Path, caplog: pytest.LogCaptureFixture, match: str) -> None:
    assert ty.run(upload=False) == 1
    assert match in caplog.text
    assert len(list((root(tmp_path) / "raw").rglob("*.xml"))) == 3
    assert not (root(tmp_path) / ty.PROCESSED_REL).exists()


def test_date_outside_requested_year_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.days[2024].append(date(2025, 1, 2))
    _fails(tmp_path, caplog, "Treasury feed 2024: entry dated 2025-01-02 is outside the requested year")


def test_duplicate_date_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.days[2024].insert(5, env.days[2024][5])
    _fails(tmp_path, caplog, "duplicate dates ['2024-01-09']")


def test_non_numeric_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.overrides[date(2025, 3, 3)] = {"BC_2YEAR": "N/A"}
    _fails(tmp_path, caplog, "BC_2YEAR 2025-03-03: non-numeric value 'N/A'")


def test_future_date_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.days[2026].append(date(2026, 11, 24))
    _fails(tmp_path, caplog, "observation after end_date: 2026-11-24")


def test_empty_completed_year_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.days[2024] = []
    _fails(tmp_path, caplog, "completed year 2024 has no dates")


def test_ten_year_missing_for_a_whole_year_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    for d in env.days[2024]:
        env.overrides[d] = {"BC_10YEAR": None}
    _fails(tmp_path, caplog, "year 2024: BC_10YEAR is missing on every date")


# --------------------------------------------------------------------------
# Warnings
# --------------------------------------------------------------------------


def test_warnings(tmp_path: Path, env: FakeApi) -> None:
    d2024 = env.days[2024]
    del d2024[20:24]  # gap of more than 4 days
    env.days[2024] = d2024 + [date(2024, 12, 28)]  # Saturday
    env.days[2024].sort()
    env.overrides[date(2025, 3, 4)] = {"BC_5YEAR": None}  # missing inside span
    env.overrides[date(2025, 3, 5)] = {"BC_7YEAR": {"null": True}}  # m:null = missing
    env.overrides[date(2024, 5, 1)] = {"BC_1_5MONTH": "5.10"}  # before its start
    env.overrides[date(2024, 6, 10)] = {"BC_30YEAR": "4.50"}  # inside the gap
    env.overrides[date(2025, 4, 7)] = {"BC_10YEAR": "4.90"}  # big move (>0.35 pp)
    env.overrides[date(2025, 4, 8)] = {"BC_1MONTH": "25.00"}  # out of range (and a big move)
    env.overrides[date(2025, 4, 9)] = {"BC_NEW_9WEEK": "4.00"}  # unknown element
    assert ty.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "weekend date(s): 1: 2024-12-28" in w
    assert "gap(s) of more than 4 days: 1: 2024-01-29 to 2024-02-05 (7 days)" in w
    assert "BC_5YEAR: missing on date(s) inside its span, not in KNOWN_MISSING: 1: 2025-03-04" in w
    assert "BC_7YEAR: missing on date(s) inside its span, not in KNOWN_MISSING: 1: 2025-03-05" in w
    assert "BC_1_5MONTH: value(s) before its expected start 2025-02-18 or inside its gap: 1: 2024-05-01" in w
    assert "BC_1_5MONTH: first value 2024-05-01 differs from expected start 2025-02-18" in w
    assert "BC_30YEAR: value(s) before its expected start 2024-01-02 or inside its gap: 1: 2024-06-10" in w
    assert "BC_10YEAR: daily change(s) above 0.35 pp not in KNOWN_MOVES: 2: 2025-04-07" in w
    assert "BC_1MONTH: value(s) outside -0.5 to 20: 1: 2025-04-08 (25)" in w
    assert "BC_1MONTH: daily change(s) above 1 pp not in KNOWN_MOVES: 2: 2025-04-08" in w
    assert "Treasury feed 2025: unknown element names ['BC_NEW_9WEEK'] (kept in raw and interim)" in w
    assert "BC_NEW_9WEEK" in {r["element"] for r in pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()}


def _checks(tmp_path: Path) -> dict[str, Any]:
    return json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))["checks"]


def test_reviewed_constants() -> None:
    assert ty.KNOWN_MISSING_ALL == {date(2010, 10, 11)}
    assert ty.KNOWN_MISSING == {"BC_3MONTH": {date(2008, 12, 10), date(2008, 12, 18), date(2008, 12, 24)}}
    assert {k: len(v) for k, v in ty.KNOWN_MOVES.items()} == {
        "BC_1MONTH": 3, "BC_2YEAR": 7, "BC_3YEAR": 7, "BC_5YEAR": 4, "BC_7YEAR": 2, "BC_10YEAR": 2,
    }
    assert date(1994, 4, 4) in ty.KNOWN_MOVES["BC_10YEAR"] and date(2009, 3, 18) in ty.KNOWN_MOVES["BC_10YEAR"]
    assert set(ty.KNOWN_MOVES) <= set(REAL_BY_CODE) and set(ty.KNOWN_MISSING) <= set(REAL_BY_CODE)


def test_known_missing_not_warned(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_day, bill_day = date(2025, 3, 4), date(2025, 3, 5)
    env.overrides[empty_day] = {k: None for k in [*CODES, "BC_30YEARDISPLAY"]}  # listed with no values at all
    env.overrides[bill_day] = {"BC_3MONTH": None}
    monkeypatch.setattr(ty, "KNOWN_MISSING_ALL", frozenset({empty_day}))
    monkeypatch.setattr(ty, "KNOWN_MISSING", {"BC_3MONTH": frozenset({bill_day})})
    assert ty.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    series = _checks(tmp_path)["series"]
    assert series["BC_3MONTH"]["known_missing_in_span"] == [empty_day.isoformat(), bill_day.isoformat()]
    assert series["BC_10YEAR"]["known_missing_in_span"] == [empty_day.isoformat()]
    assert ("BC_10YEAR", empty_day.isoformat()) not in processed(tmp_path)
    # Not reviewed: the same day warns for every maturity in span.
    monkeypatch.setattr(ty, "KNOWN_MISSING_ALL", frozenset())
    for sub in ("raw", "interim", "runs"):
        shutil.rmtree(root(tmp_path) / sub)
    assert ty.run(upload=False) == 0
    assert "BC_10YEAR: missing on date(s) inside its span, not in KNOWN_MISSING: 1: 2025-03-04" in warnings_text(tmp_path)


def test_known_moves_not_warned(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    env.overrides[date(2025, 4, 7)] = {"BC_10YEAR": "4.90"}
    monkeypatch.setattr(ty, "KNOWN_MOVES", {"BC_10YEAR": frozenset({date(2025, 4, 7), date(2025, 4, 8)})})
    assert ty.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    s = _checks(tmp_path)["series"]["BC_10YEAR"]
    assert s["large_moves"] == [] and [m[:10] for m in s["reviewed_large_moves"]] == ["2025-04-07", "2025-04-08"]


def test_no_move_measured_across_the_publication_gap(tmp_path: Path, env: FakeApi) -> None:
    # The 30-year resumes after its gap (test gap: 2024-06-03 to 2024-06-28)
    # one full point higher; that step is not a daily move.
    for y in YEARS:
        for d in env.days[y]:
            if d > GAP[1]:
                env.overrides[d] = {"BC_30YEAR": f"{float(base_value('BC_30YEAR', d)) + 1:.2f}"}
    assert ty.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    assert _checks(tmp_path)["series"]["BC_30YEAR"]["large_moves"] == []
    # A big move inside a segment still warns.
    env.overrides[date(2024, 7, 2)] = {"BC_30YEAR": "9.00"}
    for sub in ("raw", "interim", "runs"):
        shutil.rmtree(root(tmp_path) / sub)
    assert ty.run(upload=False) == 0
    assert "BC_30YEAR: daily change(s) above 0.35 pp not in KNOWN_MOVES: 2: 2024-07-02" in warnings_text(tmp_path)


def test_segments() -> None:
    s = REAL_BY_CODE["BC_30YEAR"]
    pts = [(date(2002, 2, 15), 5.37), (date(2006, 2, 9), 4.51), (date(2006, 2, 10), 4.52)]
    assert ty._segments(s, pts) == [pts[:1], pts[1:]]
    assert ty._segments(REAL_BY_CODE["BC_10YEAR"], pts) == [pts]


def test_short_year_and_empty_current_year_warn(tmp_path: Path, env: FakeApi) -> None:
    env.days[2025] = env.days[2025][:200]
    env.days[2026] = []
    assert ty.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "year 2025 has 200 dates (fewer than 240)" in w
    assert "no dates for the current year 2026 yet" in w
    assert "latest date 2025-10-" in w  # stale


def test_page_cap_warns(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ty, "PAGE_CAP", len(env.days[2024]))
    assert ty.run(upload=False) == 0
    assert f"year 2024 has exactly {len(env.days[2024])} dates (possible cap)" in warnings_text(tmp_path)


def test_revisions(tmp_path: Path, env: FakeApi) -> None:
    assert ty.run(upload=False) == 0
    for sub in ("raw", "interim", "runs"):
        shutil.rmtree(root(tmp_path) / sub)
    d = date(2025, 6, 2)
    old = base_value("BC_10YEAR", d)
    env.overrides[d] = {"BC_10YEAR": f"{float(old) + 0.01:.2f}"}
    assert ty.run(upload=False) == 0
    assert f"revised values since previous run: ['BC_10YEAR 2025-06-02: {float(old)} -> {float(old) + 0.01:.2f}']" in warnings_text(tmp_path)
