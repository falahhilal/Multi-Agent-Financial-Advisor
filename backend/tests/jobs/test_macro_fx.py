"""Tests for backend/jobs/macro_fx.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All values below are synthetic,
shaped like cases seen in the real data (working-day dates with a few
Saturdays, holiday gaps, large day-to-day moves, 6-9 decimal values, an
unrelated status comment) but not copied from it.
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

from jobs import common  # noqa: E402  (needs the sys.path line above)
from jobs import easydata as ed  # noqa: E402
from jobs import macro_fx as fx  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_9c1d+/="  # has characters that URL-encoding changes
TODAY = date(2019, 11, 20)
KEYS = [s.key for s in fx.SERIES]
NAMES = {s.key: s.name for s in fx.SERIES}
CODES = {s.currency_code: s.key for s in fx.SERIES}
USD, TRY, HKD = CODES["USD"], CODES["TRY"], CODES["HKD"]


def weekdays(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DATES = weekdays(date(2019, 10, 1), date(2019, 10, 31))  # 23 working days
N = len(DATES)

Obs = list[tuple[str, str | None, str]]  # date, value, status


def value_for(key: str, i: int) -> str:
    return f"{10 + KEYS.index(key) * 5 + i * 0.01:.6f}"


def obs(key: str, dates: list[str] | None = None) -> Obs:
    return [(d, value_for(key, i), "Normal") for i, d in enumerate(dates or DATES)]


def portal(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%b-%Y")


def meta_body(key: str, start: str | None = None, **override: Any) -> dict[str, Any]:
    # Field names as returned by EasyData /meta. Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": fx.EXPECTED_DATASET,
        "Available Since": portal(start or DATES[0]),
        "Last Refresh Date": "05-Nov-2019",
        "Data Frequency": "Daily",
        "Unit": "PKR",
    }
    body.update(override)
    return {k: v for k, v in body.items() if v is not None}


COLUMNS = [
    "Dataset Name",
    "Series Key",
    "Series Name",
    "Observation Date",
    "Observation Value",
    "Unit",
    "Observation Status",
    "Status Comments",
]


def data_body(key: str, rows: Obs | None = None, comment: str = "0", **override: Any) -> dict[str, Any]:
    """columns/rows layout, as in the Developer Guide."""
    out = []
    for d, v, status in obs(key) if rows is None else rows:
        rec = {
            "Dataset Name": fx.EXPECTED_DATASET,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": portal(d),
            "Observation Value": "" if v is None else v,
            "Unit": "PKR",
            "Observation Status": status,
            "Status Comments": comment,
        }
        rec.update(override)
        out.append([rec[c] for c in COLUMNS])
    return {"columns": COLUMNS, "rows": out}


def with_value(key: str, day: str, value: str | None, status: str = "Normal") -> Obs:
    rows = obs(key)
    rows[DATES.index(day)] = (day, value, status)
    return rows


def with_values(key: str, values: list[str]) -> Obs:
    """Series ``key`` with ``values`` in date order, the last one held to the end."""
    padded = values + [values[-1]] * (N - len(values))
    return [(d, v, "Normal") for d, v in zip(DATES, padded)]


class FakeApi:
    """Routes requests by path. Values are a JSON-able body, bytes, a status
    int, or an exception."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[str] = []
        for k in KEYS:
            self.routes[f"/api/v1/series/{k}/meta"] = meta_body(k)
            self.routes[f"/api/v1/series/{k}/data"] = data_body(k)

    def __call__(self, url: str, headers: Any, timeout: float) -> common.HttpResponse:
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/robots.txt":
            return common.HttpResponse(url, 404, {}, b"")
        self.requests.append(url)
        route = self.routes.get(parts.path)
        if route is None:
            return common.HttpResponse(url, 404, {}, b"not found")
        if isinstance(route, BaseException):
            raise route
        if isinstance(route, int):
            return common.HttpResponse(url, route, {}, b"error")
        body = route if isinstance(route, bytes) else json.dumps(route).encode()
        return common.HttpResponse(url, 200, {"content-type": "application/json"}, body)

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(u).path for u in self.requests]

    def params(self, i: int) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.requests[i]).query))

    def set_data(self, key: str, rows: Obs, **kw: Any) -> None:
        self.routes[f"/api/v1/series/{key}/data"] = data_body(key, rows, **kw)


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
    monkeypatch.setattr(fx, "LOCAL_ROOT", tmp_path / "data" / "macro" / "fx")
    monkeypatch.setattr(fx, "_today", lambda: TODAY)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)  # manifest warnings independent of the real tree
    monkeypatch.setattr(fx, "git_info", clean_git)
    monkeypatch.setenv(fx.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(fx, "upload_to_hf", no_upload)
    monkeypatch.setattr(fx, "hf_download", lambda *a, **k: None)
    return api


def use_dates(env: FakeApi, monkeypatch: pytest.MonkeyPatch, dates: list[str]) -> None:
    """Serve every series on ``dates`` (meta start = first date); today is five
    days after the last date."""
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, start=dates[0])
        env.set_data(k, obs(k, dates))
    today = date.fromisoformat(dates[-1]) + timedelta(days=5)
    monkeypatch.setattr(fx, "_today", lambda: today)


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "fx"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_fx.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / fx.PROCESSED_REL).to_pylist()
    return {(r["series_key"], r["observation_date"].isoformat()): r for r in rows}


def runlog(tmp_path: Path) -> dict[str, Any]:
    return json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))


def warnings_text(tmp_path: Path) -> str:
    return "\n".join(manifest(tmp_path)["warnings"])


def clear_run_files(tmp_path: Path) -> None:
    for sub in ("raw", "interim", "runs"):  # same-second run ids would collide
        shutil.rmtree(root(tmp_path) / sub)


def assert_no_canary(tmp_path: Path, *texts: str) -> None:
    forms = ed.secret_forms(CANARY)
    for text in texts:
        assert not any(f in text for f in forms), text
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.suffix == ".parquet":
            data = json.dumps(pq.read_table(path).to_pylist(), default=str).encode()
        assert not any(f.encode() in data for f in forms), path


# --------------------------------------------------------------------------
# Series table
# --------------------------------------------------------------------------


def test_series_keys_names_and_codes() -> None:
    assert len(KEYS) == 23 == len(set(KEYS))
    assert KEYS == [f"TS_GP_ES_FADERPKR_M.XRDAVG{n * 10:04d}" for n in range(1, 24)]
    assert KEYS[0] == "TS_GP_ES_FADERPKR_M.XRDAVG0010" and KEYS[-1] == "TS_GP_ES_FADERPKR_M.XRDAVG0230"
    assert [s.currency_code for s in fx.SERIES] == [
        "AUD", "BHD", "CAD", "CNY", "DKK", "HKD", "JPY", "KWD", "MYR", "NZD", "NOK", "OMR",
        "QAR", "SGD", "SEK", "CHF", "SAR", "THB", "TRY", "AED", "GBP", "USD", "EUR",
    ]  # fmt: skip
    assert USD == "TS_GP_ES_FADERPKR_M.XRDAVG0220"
    assert NAMES[KEYS[0]] == "Daily Average Exchange rate of PAK Rupees per Australian Dollar"
    assert NAMES[HKD] == "Daily Average Exchange rate of PAK Rupees per Hong Kong Dollar"
    assert NAMES[CODES["AED"]] == "Daily Average Exchange rate of PAK Rupees per U.A.E. Dirham"
    assert NAMES[CODES["GBP"]] == "Daily Average Exchange rate of PAK Rupees per U.K. Pound Sterling"
    assert NAMES[USD] == "Daily Average Exchange rate of PAK Rupees per U.S. Dollar"
    assert NAMES[KEYS[-1]] == "Daily Average Exchange rate of PAK Rupees per Euro"


# --------------------------------------------------------------------------
# Happy path, requests, layout, schema
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert fx.run(upload=False) == 0

    paths = env.paths()
    assert len(paths) == 46
    assert paths[:23] == [f"/api/v1/series/{k}/meta" for k in KEYS]
    assert paths[23:] == [f"/api/v1/series/{k}/data" for k in KEYS]
    for i in range(23):
        assert env.params(i) == {"api_key": CANARY, "format": "json"}
        p = env.params(23 + i)
        assert p["start_date"] == DATES[0]  # "Available Since", read from /meta
        assert p["end_date"] == TODAY.isoformat() and p["format"] == "json"

    assert len(list((root(tmp_path) / "raw").rglob("*.json"))) == 46
    assert len(list((root(tmp_path) / "interim").rglob("*.parquet"))) == 1

    rows = processed(tmp_path)
    assert len(rows) == 23 * N
    r = rows[(USD, "2019-10-31")]
    assert r["series_name"] == "Daily Average Exchange rate of PAK Rupees per U.S. Dollar"
    assert (r["currency_code"], r["currency_name"]) == ("USD", "U.S. Dollar")
    assert r["value"] == float(value_for(USD, N - 1))
    assert r["unit"] == "PKR" and r["observation_status"] == "Normal" and r["status_comments"] == "0"
    assert r["dataset_name"] == fx.EXPECTED_DATASET
    assert r["published_at"] == datetime(2019, 11, 10, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "monthly_release_10th_next_month"
    assert r["portal_last_refresh_date"] == date(2019, 11, 5)
    assert r["source_endpoint"] == f"https://easydata.sbp.org.pk/api/v1/series/{USD}/data"
    assert r["raw_file"].endswith(f"_{USD}_data.json")

    m = manifest(tmp_path)
    assert m["id"] == "macro_fx"
    assert m["script"] == "backend/jobs/macro_fx.py"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["hf_revision"] is None
    assert m["row_count"] == 23 * N
    assert m["date_range"] == {"min": "2019-10-01", "max": "2019-10-31"}
    assert m["warnings"] == []
    assert m["extra"]["latest_date"] == "2019-10-31"
    assert m["extra"]["citation"] == (
        "State Bank of Pakistan, (2019), Daily data from Bank Floating Daily Average Exchange Rates "
        "(PKR per National Currency), for 01-Oct-2019 to 31-Oct-2019, Source EasyData, Bank Floating "
        "Daily Average Exchange Rates (PKR per National Currency), 05-Nov-2019"
    )


def test_rows_per_series_in_run_log_manifest_and_final_log_line(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert fx.run(upload=False) == 0
    log = runlog(tmp_path)
    assert log["rows_per_series"] == {k: N for k in KEYS}
    assert log["series"][USD]["rows"] == N
    assert manifest(tmp_path)["extra"]["rows_per_series"] == {k: N for k in KEYS}
    last = [r for r in caplog.records if r.name == "jobs.macro_fx"][-1]
    assert last.levelno == logging.INFO
    assert last.getMessage() == f"rows per series: {N} for all 23 series ({23 * N} total); 0 warning(s); exit code 0"


def test_rows_summary_lists_each_series_when_counts_differ() -> None:
    counts = {k: 5 for k in KEYS}
    counts[USD] = 4
    line = fx.rows_summary(counts)
    assert line.startswith(f"rows per series differ ({23 * 5 - 1} total): XRDAVG0010=5, ")
    assert "XRDAVG0220=4" in line


def test_citation_year_from_last_refresh_not_run_date(
    tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fx, "_today", lambda: date(2020, 1, 3))  # run in a later year
    env.routes[f"/api/v1/series/{USD}/meta"] = meta_body(USD, **{"Last Refresh Date": "08-Nov-2019"})
    assert fx.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (2019), ")
    assert citation.endswith(", 08-Nov-2019")  # newest refresh across series
    assert runlog(tmp_path)["citation"] == citation


def test_citation_unknown_without_refresh_date(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Last Refresh Date": None})
    assert fx.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (unknown), ")
    assert citation.endswith(", unknown")
    assert f"{USD}: metadata has no last-refresh date" in warnings_text(tmp_path)


def test_upload_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def fake_upload(local: Any, path_in_repo: str, stream: str, **kw: Any) -> str:
        common.check_hf_path(path_in_repo, stream)
        local = Path(local)
        names = (
            sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file())
            if local.is_dir()
            else [local.name]
        )
        calls.append((path_in_repo, stream, names))
        return "rev789"

    monkeypatch.setattr(fx, "upload_to_hf", fake_upload)
    assert fx.run(upload=True) == 0
    assert [c[:2] for c in calls] == [("macro/fx/", "macro"), ("macro/fx/manifest.json", "macro")]
    uploaded = calls[0][2]
    assert "processed/fx.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 46
    assert len(uploaded) == 49  # 46 raw, interim, processed, run log
    m = manifest(tmp_path)
    assert m["hf_path"] == "macro/fx/" and m["hf_revision"] == "rev789"


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "returned",
    [
        "Daily Average Exchange rate of PAK Rupees per Hon Kong Dollar",  # web-page spelling
        "Daily Average Exchange Rate of PAK Rupees per Hong Kong Dollar",  # capitalisation
        "Daily Average Exchange rate of PAK Rupees per Hong Kong Dollar.",  # punctuation
    ],
)
def test_name_mismatch_aborts_before_any_data(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, returned: str
) -> None:
    env.routes[f"/api/v1/series/{HKD}/meta"] = meta_body(HKD, **{"Series Name": returned})
    assert fx.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert "series name mismatch" in caplog.text


def test_leading_dots_stripped_in_processed_kept_in_interim(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Series Name": ". " + NAMES[k]})
        env.routes[f"/api/v1/series/{k}/data"] = data_body(k, **{"Series Name": ". " + NAMES[k]})
    assert fx.run(upload=False) == 0
    assert all(r["series_name"] == NAMES[r["series_key"]] for r in processed(tmp_path).values())
    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert all(r["series_name"].startswith(". ") for r in interim)
    log = runlog(tmp_path)
    assert log["series"][USD]["meta_name_as_returned"] == ". " + NAMES[USD]
    assert log["series"][USD]["rows_with_name_prefix"] == N
    assert manifest(tmp_path)["warnings"] == []


# --------------------------------------------------------------------------
# Dates: Saturdays, gaps, staleness
# --------------------------------------------------------------------------


def test_known_saturdays_allowed_other_weekend_dates_warn(
    tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = sorted(weekdays(date(2022, 4, 11), date(2022, 5, 6)) + ["2022-04-16", "2022-04-23", "2022-04-30"])
    use_dates(env, monkeypatch, dates)
    assert fx.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    assert runlog(tmp_path)["series"][USD]["allowed_saturdays"] == ["2022-04-16", "2022-04-23", "2022-04-30"]

    clear_run_files(tmp_path)
    for k in KEYS:  # 2022-04-09 Saturday and 2022-05-01 Sunday are not known
        env.set_data(k, obs(k, sorted([*dates, "2022-05-01"])))
    env.set_data(USD, obs(USD, sorted([*dates, "2022-04-09", "2022-05-01"])))
    env.routes[f"/api/v1/series/{USD}/meta"] = meta_body(USD, start="2022-04-09")
    assert fx.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"{USD}: 2 weekend date(s) other than the known Saturdays: 2022-04-09, 2022-05-01" in text
    assert f"{TRY}: 1 weekend date(s) other than the known Saturdays: 2022-05-01" in text


def test_single_known_saturday_2019(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    use_dates(env, monkeypatch, sorted(weekdays(date(2019, 8, 12), date(2019, 8, 23)) + ["2019-08-17"]))
    assert fx.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []


@pytest.mark.parametrize(("gap_days", "warns"), [(10, False), (14, False), (15, True)])
def test_gap_warns_only_above_14_days(
    tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch, gap_days: int, warns: bool
) -> None:
    before = weekdays(date(2021, 4, 26), date(2021, 5, 7))  # ends Friday 2021-05-07
    resume = date(2021, 5, 7) + timedelta(days=gap_days)
    after = [resume.isoformat(), *weekdays(resume + timedelta(days=1), resume + timedelta(days=7))]
    use_dates(env, monkeypatch, before + after)
    assert fx.run(upload=False) == 0
    text = warnings_text(tmp_path)
    expected = f"{USD}: 1 gap(s) of more than 14 days: 2021-05-07 to {resume} ({gap_days} days)"
    assert (expected in text) is warns
    if not warns:
        assert text == ""
    assert runlog(tmp_path)["series"][USD]["max_gap_days"] == gap_days


def test_stale_data_warns_after_45_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "_today", lambda: date(2019, 12, 15))  # 45 days after 2019-10-31
    assert fx.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    clear_run_files(tmp_path)
    monkeypatch.setattr(fx, "_today", lambda: date(2019, 12, 16))  # 46 days
    assert fx.run(upload=False) == 0
    assert "latest date 2019-10-31 is 46 days before 2019-12-16" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


def test_large_moves_against_previous_observation(tmp_path: Path, env: FakeApi) -> None:
    # USD +5.65% then +6.35% (no warning); TRY exactly +10% (no warning),
    # then -14.6% and +28.1% (warn).
    env.set_data(USD, with_values(USD, ["100", "105.65", "112.358775", "112.358775"]))
    env.set_data(TRY, with_values(TRY, ["100", "110", "93.94", "120.33714", "120.33714"]))
    assert fx.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert (
        f"{TRY}: 2 change(s) of more than 10% against the previous observation: "
        f"{DATES[2]} (-14.60%), {DATES[3]} (+28.10%)"
    ) in text
    assert USD not in text
    # Every other series moves by a fraction of a percent: only TRY warns.
    assert manifest(tmp_path)["warnings"] == [w for w in manifest(tmp_path)["warnings"] if TRY in w]
    assert runlog(tmp_path)["series"][TRY]["large_moves"] == [f"{DATES[2]} (-14.60%)", f"{DATES[3]} (+28.10%)"]


def test_move_across_holiday_gap_measured_against_previous_observation(
    tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = ["2021-05-05", "2021-05-06", "2021-05-07", "2021-05-17", "2021-05-18"]
    use_dates(env, monkeypatch, dates)
    env.set_data(TRY, list(zip(dates, ["10", "10", "10", "11.5", "11.5"], ["Normal"] * 5)))
    assert fx.run(upload=False) == 0
    assert f"{TRY}: 1 change(s) of more than 10% against the previous observation: 2021-05-17 (+15.00%)" in (
        warnings_text(tmp_path)
    )


def test_value_precision_kept(tmp_path: Path, env: FakeApi) -> None:
    rows = obs(USD)
    rows[1] = (DATES[1], "121.123456", "Normal")  # 6 decimals
    rows[2] = (DATES[2], "121.123456789", "Normal")  # 9 decimals
    rows[3] = (DATES[3], "121.1234567", "Normal")  # 7 decimals
    env.set_data(USD, rows)
    assert fx.run(upload=False) == 0
    out = processed(tmp_path)
    assert out[(USD, DATES[1])]["value"] == 121.123456
    assert out[(USD, DATES[2])]["value"] == 121.123456789
    assert out[(USD, DATES[3])]["value"] == 121.1234567
    assert runlog(tmp_path)["series"][USD]["max_decimals"] == 9


def test_unrelated_status_comment_kept_without_warning(tmp_path: Path, env: FakeApi) -> None:
    comment = "Synthetic: historical note about an unrelated event in another decade"
    env.set_data(CODES["CAD"], obs(CODES["CAD"]), comment=comment)
    assert fx.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    assert processed(tmp_path)[(CODES["CAD"], DATES[4])]["status_comments"] == comment


def test_status_other_than_normal_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(USD, with_value(USD, DATES[5], "120.5", "Provisional"))
    assert fx.run(upload=False) == 0
    assert f"{USD}: 1 row(s) with status other than 'Normal' ['Provisional']: {DATES[5]}" in warnings_text(tmp_path)
    assert processed(tmp_path)[(USD, DATES[5])]["observation_status"] == "Provisional"


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (with_value(USD, DATES[3], "0"), "is <= 0"),
        (with_value(USD, DATES[3], "-1.5"), "is <= 0"),
        (with_value(USD, DATES[3], "n/a"), "non-numeric"),
        (with_value(USD, DATES[3], None), "empty value (status 'Normal')"),
        (with_value(USD, DATES[3], None, "Missing value"), "empty value (status 'Missing value')"),
        ([*obs(USD), (DATES[-1], "120.0", "Normal")], "duplicate observation dates"),
        ([(DATES[-1], "120.0", "Normal")], "1 row(s) returned"),
        ([*obs(USD), ("2019-11-21", "120.0", "Normal")], "observation after end_date"),
    ],
)
def test_bad_rows_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, rows: Obs, message: str
) -> None:
    env.set_data(USD, rows)
    assert fx.run(upload=False) == 1
    assert message in caplog.text
    assert not (tmp_path / "manifests").exists()
    assert not (root(tmp_path) / "processed").exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"Series Key": KEYS[1]}, "contains a row for series"),
        ({"Series Name": "Something else"}, "row series name"),
        ({"Observation Date": "01/02/2019"}, "unrecognised date"),
    ],
)
def test_bad_fields_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, override: dict[str, str], message: str
) -> None:
    env.routes[f"/api/v1/series/{USD}/data"] = data_body(USD, **override)
    assert fx.run(upload=False) == 1
    assert message in caplog.text
    assert not (root(tmp_path) / "processed").exists()


def test_invalid_json_fails_without_repair(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    # The CPI loader's "MIS" repair must not apply here.
    comment = 'Synthetic: the observation status is missing "MIS" due to a test.'
    body = json.dumps(data_body(USD, comment=comment)).encode()
    broken = body.replace(json.dumps(comment).encode()[1:-1], comment.encode())
    with pytest.raises(ValueError):
        json.loads(broken)
    env.routes[f"/api/v1/series/{USD}/data"] = broken
    assert fx.run(upload=False) == 1
    assert f"{USD}: data response is not JSON" in caplog.text
    assert not (root(tmp_path) / "processed").exists()
    assert not (tmp_path / "manifests").exists()
    raw = next((root(tmp_path) / "raw").rglob(f"*_{USD}_data.json"))
    assert raw.read_bytes() == broken  # raw kept exactly as served


def test_missing_key_env_names_variable_and_makes_no_request(env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fx.KEY_ENV)
    with pytest.raises(RuntimeError, match=fx.KEY_ENV):
        fx.run(upload=False)
    assert env.requests == []


# --------------------------------------------------------------------------
# Cross-series and metadata warnings
# --------------------------------------------------------------------------


def test_series_lacking_a_date_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(USD, [r for r in obs(USD) if r[0] != DATES[10]])
    assert fx.run(upload=False) == 0
    assert f"1 series lack dates present in other series: {USD} (1 missing)" in warnings_text(tmp_path)
    assert runlog(tmp_path)["checks"]["dates_missing_vs_other_series"] == {USD: [DATES[10]]}


def test_series_ending_early_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(USD, obs(USD)[:-1])
    assert fx.run(upload=False) == 0
    assert f"1 series end before the latest date 2019-10-31: {USD} ({DATES[-2]})" in warnings_text(tmp_path)


def test_first_observation_differs_from_meta_start_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{USD}/meta"] = meta_body(USD, start="2019-09-30")
    assert fx.run(upload=False) == 0
    assert f"{USD}: first observation 2019-10-01 differs from metadata start date 2019-09-30" in warnings_text(
        tmp_path
    )


def test_unexpected_dataset_name_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{USD}/data"] = data_body(USD, **{"Dataset Name": "Exchange Rates"})
    assert fx.run(upload=False) == 0
    assert f"{USD}: dataset name ['Exchange Rates']" in warnings_text(tmp_path)


def test_unit_other_than_pkr_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{USD}/data"] = data_body(USD, **{"Unit": "Rupees"})
    assert fx.run(upload=False) == 0
    assert f"{USD}: unit(s) ['Rupees'] != 'PKR'" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Changes between runs
# --------------------------------------------------------------------------


def test_revision_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert fx.run(upload=False) == 0
    env.set_data(USD, with_value(USD, DATES[4], "123.456789"))
    clear_run_files(tmp_path)
    assert fx.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1 and rev["status_changed"] == 0
    assert f"{USD} {DATES[4]}: {float(value_for(USD, 4))} -> 123.456789" in warnings_text(tmp_path)


def test_status_only_change_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert fx.run(upload=False) == 0
    env.set_data(TRY, obs(TRY), comment="Revised")  # value unchanged, comment changed
    clear_run_files(tmp_path)
    assert fx.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 0 and rev["status_changed"] == N
    text = warnings_text(tmp_path)
    assert f"status or status comment changed with value unchanged since previous run ({N} row(s))" in text
    assert "(+13 more, see run log)" in text  # listed() caps at 10


# --------------------------------------------------------------------------
# published_at
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs_date", "expected"),
    [
        (date(2026, 10, 30), datetime(2026, 11, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2026, 10, 1), datetime(2026, 11, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2023, 1, 26), datetime(2023, 2, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2025, 12, 31), datetime(2026, 1, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2022, 4, 16), datetime(2022, 5, 10, 18, 59, 59, tzinfo=timezone.utc)),  # a known Saturday
        (date(2013, 7, 2), datetime(2013, 8, 10, 18, 59, 59, tzinfo=timezone.utc)),  # no floor
    ],
)
def test_published_at(obs_date: date, expected: datetime) -> None:
    assert fx.published_at_for(obs_date) == expected


def test_published_at_delay_at_most_about_40_days() -> None:
    d, worst = date(2013, 7, 1), 0
    while d < date(2026, 12, 31):
        delay = fx.published_at_for(d).astimezone(ed.PKT).date() - d
        worst = max(worst, delay.days)
        d += timedelta(days=1)
    assert worst == 40  # e.g. 1 July -> 10 August


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{KEYS[0]}/meta", 404),
        (f"/api/v1/series/{KEYS[-1]}/meta", 403),
        (f"/api/v1/series/{USD}/data", 500),  # retried, then fails
        (f"/api/v1/series/{TRY}/data", OSError("connection reset")),
    ],
)
def test_key_never_leaks_on_failure(
    tmp_path: Path,
    env: FakeApi,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    path: str,
    failure: Any,
) -> None:
    caplog.set_level(logging.DEBUG)
    env.routes[path] = failure
    assert fx.run(upload=False) == 1
    out = capsys.readouterr()
    assert ed.REDACTED in caplog.text
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert fx.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{KEYS[-1]}/data"] = data_body(KEYS[-1], **{"Status Comments": CANARY})
    assert fx.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(fx, "run", lambda *, upload: seen.append(upload) or 0)
    assert fx.main(["--no-upload"]) == 0
    assert fx.main([]) == 0
    assert seen == [False, True]
