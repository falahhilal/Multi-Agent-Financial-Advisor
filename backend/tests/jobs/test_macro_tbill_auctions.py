"""Tests for backend/jobs/macro_tbill_auctions.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All yields below are synthetic,
shaped like cases seen in the real data (missing values, a value with status
"Missing value", ".1215"-style values, a 28-day gap) but not copied from it.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402  (needs the sys.path line above)
from jobs import easydata as ed  # noqa: E402
from jobs import macro_tbill_auctions as mtb  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_9c1d+/="  # has characters that URL-encoding changes
TODAY = date(2026, 11, 16)
KEYS = [s.key for s in mtb.SERIES]
C3, C6, C12, W3, W6, W12 = KEYS
NAMES = {s.key: s.name for s in mtb.SERIES}
DATES = ["2026-08-05", "2026-08-19", "2026-09-02", "2026-09-30", "2026-10-14"]
MISSING = "Missing value"

# Synthetic yields per series; None = empty value with status "Missing value".
VALUES: dict[str, list[str | None]] = {
    C3: ["11.50", "11.40", "11.30", "11.20", "11.10"],
    W3: ["11.45", "11.35", "11.25", "11.15", "11.05"],
    C6: ["11.60", "11.50", "11.40", "11.30", "11.20"],
    W6: ["11.55", "11.45", "11.35", "11.25", "11.15"],
    C12: ["11.70", None, "11.50", "11.40", "11.30"],
    W12: ["11.65", None, "11.45", "11.35", "11.25"],
}

Obs = list[tuple[str, str | None, str]]  # date, value, status


def obs(key: str) -> Obs:
    return [(d, v, MISSING if v is None else "Normal") for d, v in zip(DATES, VALUES[key], strict=True)]


def portal(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%b-%Y")


def meta_body(key: str, **override: Any) -> dict[str, Any]:
    # Field names as returned by the real /meta. Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Short Name": NAMES[key].replace("Treasury Bills' ", ""),
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": mtb.EXPECTED_DATASET,
        "Available Since": portal(DATES[0]),
        "Available Upto": portal(DATES[-1]),
        "Last Refresh Date": "16-Oct-2026",
        "Data Frequency": "As-Needed",
        "Unit": "Percent",
    }
    body.update(override)
    return {k: v for k, v in body.items() if v is not None}


def data_body(key: str, rows: Obs | None = None, **override: Any) -> dict[str, Any]:
    """columns/rows layout, as in the Developer Guide."""
    columns = [
        "Dataset Name",
        "Series Key",
        "Series Name",
        "Observation Date",
        "Observation Value",
        "Unit",
        "Observation Status",
        "Status Comments",
    ]
    out = []
    for d, v, status in obs(key) if rows is None else rows:
        rec = {
            "Dataset Name": mtb.EXPECTED_DATASET,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": portal(d),
            "Observation Value": "" if v is None else v,
            "Unit": "Percent",
            "Observation Status": status,
            "Status Comments": "",
        }
        rec.update(override)
        out.append([rec[c] for c in columns])
    return {"columns": columns, "rows": out}


def with_value(key: str, day: str, value: str | None, status: str | None = None) -> Obs:
    rows = obs(key)
    i = DATES.index(day)
    rows[i] = (day, value, status or (MISSING if value is None else "Normal"))
    return rows


class FakeApi:
    """Routes requests by path. Values are a JSON-able body, bytes, a status
    int, or an exception."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[str] = []
        for k in KEYS:
            self.routes[f"/api/v1/series/{k}/meta"] = meta_body(k)
            self.routes[f"/api/v1/series/{k}/data"] = data_body(k)

    def __call__(self, url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
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
    monkeypatch.setattr(mtb, "LOCAL_ROOT", tmp_path / "data" / "macro" / "tbill_auctions")
    monkeypatch.setattr(mtb, "_today", lambda: TODAY)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)  # manifest warnings independent of the real tree
    monkeypatch.setattr(mtb, "git_info", clean_git)
    monkeypatch.setenv(mtb.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(mtb, "upload_to_hf", no_upload)
    monkeypatch.setattr(mtb, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "tbill_auctions"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_tbill_auctions.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / mtb.PROCESSED_REL).to_pylist()
    return {(r["series_key"], r["observation_date"].isoformat()): r for r in rows}


def runlog(tmp_path: Path) -> dict[str, Any]:
    return json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))


def warnings_text(tmp_path: Path) -> str:
    return "\n".join(manifest(tmp_path)["warnings"])


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
# Happy path, requests, layout, schema
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi) -> None:
    assert mtb.run(upload=False) == 0

    paths = env.paths()
    assert paths[:6] == [f"/api/v1/series/{k}/meta" for k in KEYS]
    assert paths[6:] == [f"/api/v1/series/{k}/data" for k in KEYS]
    for i in range(6):
        assert env.params(i) == {"api_key": CANARY, "format": "json"}
        p = env.params(6 + i)
        assert p["start_date"] == DATES[0]  # "Available Since", read from /meta
        assert p["end_date"] == TODAY.isoformat() and p["format"] == "json"

    raw = sorted(p.name for p in (root(tmp_path) / "raw").rglob("*.json"))
    assert len(raw) == 12
    assert len(list((root(tmp_path) / "interim").rglob("*.parquet"))) == 1

    rows = processed(tmp_path)
    assert len(rows) == 30
    r = rows[(W12, "2026-10-14")]
    assert r["series_name"] == "Treasury Bills' Weighted Average Yield 12-Months"
    assert (r["tenor_months"], r["yield_type"]) == (12, "weighted_average")
    assert r["value"] == 11.25 and r["is_missing"] is False
    assert r["published_at"] == datetime(2026, 10, 14, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "auction_date_end_of_day_pkt"
    assert r["portal_last_refresh_date"] == date(2026, 10, 16)
    assert r["source_endpoint"].endswith(f"/series/{W12}/data")
    assert {(s.tenor_months, s.yield_type) for s in mtb.SERIES} == {
        (t, y) for t in (3, 6, 12) for y in ("cutoff", "weighted_average")
    }

    m = manifest(tmp_path)
    assert m["id"] == "macro_tbill_auctions"
    assert m["script"] == "backend/jobs/macro_tbill_auctions.py"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["hf_revision"] is None
    assert m["row_count"] == 30
    assert m["date_range"] == {"min": "2026-08-05", "max": "2026-10-14"}
    assert m["warnings"] == []
    assert m["extra"]["series"][C3]["citation"] == (
        "State Bank of Pakistan, (2026), As-Needed data on Treasury Bills' Cut-off "
        "Yield 3-Months from Structure of Interest Rate: T-Bills Auction Result, "
        "Source EasyData"
    )


def test_missing_values_kept_as_null_without_warning(tmp_path: Path) -> None:
    assert mtb.run(upload=False) == 0
    rows = processed(tmp_path)
    for k in (C12, W12):
        r = rows[(k, "2026-08-19")]
        assert r["value"] is None and r["is_missing"] is True
        assert r["observation_status"] == MISSING
    log = runlog(tmp_path)
    assert log["series"][C12]["missing_rows"] == 1
    assert log["checks"]["series"][C12]["missing_dates"] == ["2026-08-19"]
    assert log["series"][C3]["missing_rows"] == 0
    assert manifest(tmp_path)["warnings"] == []


def test_gap_report_lists_long_gap_without_warning(tmp_path: Path) -> None:
    assert mtb.run(upload=False) == 0
    gaps = manifest(tmp_path)["extra"]["gaps"]
    assert gaps == [{"from": "2026-09-02", "to": "2026-09-30", "days": 28}]
    assert manifest(tmp_path)["warnings"] == []


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
        return "rev456"

    monkeypatch.setattr(mtb, "upload_to_hf", fake_upload)
    assert mtb.run(upload=True) == 0
    assert [c[:2] for c in calls] == [
        ("macro/tbill_auctions/", "macro"),
        ("macro/tbill_auctions/manifest.json", "macro"),
    ]
    uploaded = calls[0][2]
    assert "processed/tbill_auctions.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 12
    m = manifest(tmp_path)
    assert m["hf_path"] == "macro/tbill_auctions/" and m["hf_revision"] == "rev456"


# --------------------------------------------------------------------------
# Names with a leading ". "
# --------------------------------------------------------------------------


def test_leading_dot_prefix_stripped_in_processed_kept_in_interim(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Series Name": ". " + NAMES[k]})
        env.routes[f"/api/v1/series/{k}/data"] = data_body(k, **{"Series Name": ". " + NAMES[k]})
    assert mtb.run(upload=False) == 0
    assert all(r["series_name"] == NAMES[r["series_key"]] for r in processed(tmp_path).values())
    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert all(r["series_name"].startswith(". ") for r in interim)
    log = runlog(tmp_path)
    assert log["series"][C3]["meta_name_as_returned"] == ". " + NAMES[C3]
    assert log["series"][C3]["rows_with_name_prefix"] == 5


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (". Treasury Bills' Cut-off Yield 3-Months", "Treasury Bills' Cut-off Yield 3-Months"),
        ("Treasury Bills' Cut-off Yield 3-Months", "Treasury Bills' Cut-off Yield 3-Months"),
        ("  .   X", "X"),
        (".X", ".X"),  # only ". " followed by space is a prefix
        ("X. Y", "X. Y"),
    ],
)
def test_clean_name(raw: str, clean: str) -> None:
    assert mtb.clean_name(raw) == clean


def test_name_mismatch_after_strip_aborts_before_any_data(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{W6}/meta"] = meta_body(W6, **{"Series Name": ". Weighted Average Yield 6M"})
    assert mtb.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert "series name mismatch" in caplog.text


# --------------------------------------------------------------------------
# Missing-value rule, values, aborts
# --------------------------------------------------------------------------


def test_value_with_missing_status_warns_and_is_kept(tmp_path: Path, env: FakeApi) -> None:
    for k in (C12, W12):
        env.routes[f"/api/v1/series/{k}/data"] = data_body(k, with_value(k, "2026-09-30", "11.40", MISSING))
    assert mtb.run(upload=False) == 0
    r = processed(tmp_path)[(C12, "2026-09-30")]
    assert r["value"] == 11.40 and r["is_missing"] is False and r["observation_status"] == MISSING
    text = warnings_text(tmp_path)
    assert f"{C12}: 1 row(s) have a value but status 'Missing value'" in text
    assert f"{W12}: 1 row(s) have a value but status 'Missing value'" in text
    assert runlog(tmp_path)["checks"]["series"][C12]["value_with_missing_status"] == ["2026-09-30"]


def test_value_without_leading_zero_parsed_and_warned(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{W6}/data"] = data_body(W6, with_value(W6, "2026-08-19", ".1145"))
    assert mtb.run(upload=False) == 0
    assert processed(tmp_path)[(W6, "2026-08-19")]["value"] == pytest.approx(0.1145)
    assert f"{W6}: 1 value(s) outside [1.0, 35.0]: 2026-08-19 (.1145)" in warnings_text(tmp_path)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (with_value(C3, "2026-08-19", None, "Normal"), "empty value with status 'Normal'"),
        (with_value(C3, "2026-08-19", "0"), "outside (0.0, 100.0)"),
        (with_value(C3, "2026-08-19", "-1.5"), "outside (0.0, 100.0)"),
        (with_value(C3, "2026-08-19", "100"), "outside (0.0, 100.0)"),
        (with_value(C3, "2026-08-19", "n/a"), "non-numeric"),
        ([*obs(C3), (DATES[-1], "11.0", "Normal")], "duplicate observation dates"),
        ([(DATES[-1], "11.10", "Normal")], "1 row(s) returned"),
        ([*obs(C3), ("2026-12-01", "11.0", "Normal")], "observation after end_date"),
    ],
)
def test_bad_rows_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, rows: Obs, message: str
) -> None:
    env.routes[f"/api/v1/series/{C3}/data"] = data_body(C3, rows)
    assert mtb.run(upload=False) == 1
    assert message in caplog.text
    assert not (tmp_path / "manifests").exists()
    assert not (root(tmp_path) / "processed").exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"Series Key": C6}, "contains a row for series"),
        ({"Series Name": "Something else"}, "row series name"),
        ({"Observation Date": "01/02/2026"}, "unrecognised date"),
    ],
)
def test_bad_fields_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, override: dict[str, str], message: str
) -> None:
    env.routes[f"/api/v1/series/{C3}/data"] = data_body(C3, **override)
    assert mtb.run(upload=False) == 1
    assert message in caplog.text


def test_missing_key_env_names_variable_and_makes_no_request(env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mtb.KEY_ENV)
    with pytest.raises(RuntimeError, match=mtb.KEY_ENV):
        mtb.run(upload=False)
    assert env.requests == []


# --------------------------------------------------------------------------
# Cross-series checks
# --------------------------------------------------------------------------


def test_weighted_average_above_cutoff_warns(tmp_path: Path, env: FakeApi) -> None:
    # 6-month 2026-09-02: cut-off 11.40; WA 11.48 (above) ; 3-month WA = cut-off + 0.01 (allowed)
    env.routes[f"/api/v1/series/{W6}/data"] = data_body(W6, with_value(W6, "2026-09-02", "11.48"))
    env.routes[f"/api/v1/series/{W3}/data"] = data_body(W3, with_value(W3, "2026-09-02", "11.31"))
    assert mtb.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert "6-month: weighted average above cut-off by more than 0.01 on 1 date(s): 2026-09-02" in text
    assert "3-month: weighted average above" not in text


def test_one_side_missing_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{W3}/data"] = data_body(W3, with_value(W3, "2026-09-30", None))
    assert mtb.run(upload=False) == 0
    assert "3-month: cut-off and weighted average not both present on 1 date(s): 2026-09-30" in warnings_text(
        tmp_path
    )


def test_stale_data_warns_after_45_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mtb, "_today", lambda: date(2026, 11, 28))  # 45 days after 2026-10-14
    assert mtb.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    shutil.rmtree(root(tmp_path) / "raw")
    shutil.rmtree(root(tmp_path) / "runs")
    monkeypatch.setattr(mtb, "_today", lambda: date(2026, 11, 29))  # 46 days
    assert mtb.run(upload=False) == 0
    assert "latest auction 2026-10-14 is 46 days before 2026-11-29" in warnings_text(tmp_path)


def test_revision_null_to_value_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert mtb.run(upload=False) == 0
    env.routes[f"/api/v1/series/{C12}/data"] = data_body(C12, with_value(C12, "2026-08-19", "11.60"))
    for sub in ("raw", "interim", "runs"):  # same-second run ids would collide
        shutil.rmtree(root(tmp_path) / sub)
    assert mtb.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1
    assert f"{C12} 2026-08-19: None -> 11.6" in warnings_text(tmp_path)


def test_published_at_is_end_of_day_pakistan_in_utc() -> None:
    assert mtb.published_at_for(date(2026, 9, 30)) == datetime(2026, 9, 30, 18, 59, 59, tzinfo=timezone.utc)


def test_gap_report_threshold() -> None:
    d = [date(2026, 1, 1), date(2026, 1, 22), date(2026, 2, 13)]  # 21 days, then 22
    assert mtb.gap_report(d) == [{"from": "2026-01-22", "to": "2026-02-13", "days": 22}]


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{C3}/meta", 404),
        (f"/api/v1/series/{W12}/meta", 403),
        (f"/api/v1/series/{C6}/data", 500),  # retried, then fails
        (f"/api/v1/series/{W3}/data", OSError("connection reset")),
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
    assert mtb.run(upload=False) == 1
    out = capsys.readouterr()
    assert ed.REDACTED in caplog.text
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert mtb.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{W12}/data"] = data_body(W12, **{"Status Comments": CANARY})
    assert mtb.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(mtb, "run", lambda *, upload: seen.append(upload) or 0)
    assert mtb.main(["--no-upload"]) == 0
    assert mtb.main([]) == 0
    assert seen == [False, True]
