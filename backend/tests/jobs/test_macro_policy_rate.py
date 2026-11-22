"""Tests for backend/jobs/macro_policy_rate.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All rates below are synthetic.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402  (needs the sys.path line above)
from jobs import macro_policy_rate as mpr  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_7f3a+/="  # has characters that URL-encoding changes
TARGET, REPO, REVERSE = (s.key for s in mpr.SERIES)
NAMES = {s.key: s.name for s in mpr.SERIES}
DATASET_NAME = mpr.EXPECTED_DATASET

# Synthetic observations: (date, value). Not real SBP data.
OBS: dict[str, list[tuple[str, str]]] = {
    TARGET: [("2015-05-25", "6.50"), ("2016-05-23", "5.75")],
    REPO: [("2009-08-17", "9.00"), ("2015-05-25", "5.00")],
    REVERSE: [("2009-08-17", "12.50"), ("2015-05-25", "7.00")],
}


def _portal_date(iso: str) -> str:
    """2015-05-25 -> 25-May-2015, the format the EasyData portal shows."""
    return date.fromisoformat(iso).strftime("%d-%b-%Y")


def meta_body(key: str, **override: Any) -> dict[str, Any]:
    # Field names as returned by the real /meta (first real run, 2026-11-12).
    # Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Short Name": NAMES[key].replace("State Bank of Pakistan's ", ""),
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": DATASET_NAME,
        "Available Since": _portal_date(OBS[key][0][0]),
        "Available Upto": _portal_date(OBS[key][-1][0]),
        "Last Refresh Date": "28-Oct-2026",
        "Data Frequency": "As-Needed",
        "Unit": "Percent",
        "Variable Type": "Stock",
    }
    body.update(override)
    return {k: v for k, v in body.items() if v is not None}


def data_body(key: str, obs: list[tuple[str, str]] | None = None, **override: Any) -> list[dict[str, Any]]:
    rows = []
    for d, v in OBS[key] if obs is None else obs:
        row = {
            "Dataset Name": DATASET_NAME,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": d,
            "Observation Value": v,
            "Unit": "Percent",
            "Observation Status": "Normal",
            "Status Comments": "",
        }
        row.update(override)
        rows.append(row)
    return rows


class FakeApi:
    """Routes requests by path. Values are a JSON-able body, bytes, a status
    int, an exception, or a callable returning one of those."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[str] = []
        for s in mpr.SERIES:
            self.routes[f"/api/v1/series/{s.key}/meta"] = meta_body(s.key)
            self.routes[f"/api/v1/series/{s.key}/data"] = data_body(s.key)

    def __call__(self, url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/robots.txt":
            return common.HttpResponse(url, 404, {}, b"")
        self.requests.append(url)
        route = self.routes.get(parts.path)
        if callable(route):
            route = route()
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
    monkeypatch.setattr(mpr, "LOCAL_ROOT", tmp_path / "data" / "macro" / "policy_rate")
    monkeypatch.setenv(mpr.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(mpr, "upload_to_hf", no_upload)
    monkeypatch.setattr(mpr, "hf_download", lambda *a, **k: None)
    return api


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_policy_rate.json").read_text("utf-8"))


def processed(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data" / "macro" / "policy_rate" / mpr.PROCESSED_REL
    return pq.read_table(path).to_pylist()


def all_output_bytes(tmp_path: Path) -> list[tuple[Path, bytes]]:
    return [(p, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()]


def assert_no_canary(tmp_path: Path, *texts: str) -> None:
    forms = mpr._secret_forms(CANARY)
    for text in texts:
        assert not any(f in text for f in forms), text
    for path, data in all_output_bytes(tmp_path):
        if path.suffix == ".parquet":
            data = json.dumps(pq.read_table(path).to_pylist(), default=str).encode()
        assert not any(f.encode() in data for f in forms), path


# --------------------------------------------------------------------------
# Happy path, requests, layout
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi) -> None:
    assert mpr.run(upload=False) == 0

    # metadata for all three series first, then data
    paths = env.paths()
    assert paths[:3] == [f"/api/v1/series/{s.key}/meta" for s in mpr.SERIES]
    assert paths[3:] == [f"/api/v1/series/{s.key}/data" for s in mpr.SERIES]
    for i, s in enumerate(mpr.SERIES):
        meta_params = env.params(i)
        assert meta_params == {"api_key": CANARY, "format": "json"}
        p = env.params(3 + i)
        assert p["start_date"] == OBS[s.key][0][0]  # read from /meta, per series
        assert p["end_date"] and p["format"] == "json"

    root = tmp_path / "data" / "macro" / "policy_rate"
    raw = sorted(p.name for p in (root / "raw").rglob("*.json"))
    assert len(raw) == 6 and all(n.endswith(("_meta.json", "_data.json")) for n in raw)
    assert len(list((root / "interim").rglob("*.parquet"))) == 1
    assert len(list((root / "runs").rglob("run-*.json"))) == 1

    rows = processed(tmp_path)
    assert len(rows) == 6
    assert {r["series_key"] for r in rows} == {TARGET, REPO, REVERSE}
    assert {r["series_role"] for r in rows} == {"policy_target", "repo_floor", "reverse_repo_ceiling"}
    assert all(r["published_at"] is None and r["published_at_basis"] == "unknown" for r in rows)
    assert all(r["portal_last_refresh_date"] == date(2026, 10, 28) for r in rows)
    assert all(r["source_endpoint"].endswith(f"/series/{r['series_key']}/data") for r in rows)
    assert isinstance(rows[0]["value"], float)

    m = manifest(tmp_path)
    assert m["id"] == "macro_policy_rate"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["hf_revision"] is None
    assert m["row_count"] == 6
    assert m["date_range"] == {"min": "2009-08-17", "max": "2016-05-23"}
    assert m["extra"]["series"][TARGET]["citation"] == (
        "State Bank of Pakistan, (2026), As-Needed data on State Bank of Pakistan's "
        "Policy (Target) Rate from Structure of Interest Rate: State Bank of Pakistan "
        "Policy Rates, Source EasyData"
    )


def test_raw_saved_unmodified_and_immutable(tmp_path: Path, env: FakeApi) -> None:
    expected = json.dumps(data_body(TARGET), indent=3).encode() + b"  \r\n"  # odd layout must survive
    env.routes[f"/api/v1/series/{TARGET}/data"] = expected
    assert mpr.run(upload=False) == 0
    raw = next((tmp_path / "data").rglob(f"*_{TARGET}_data.json"))
    assert raw.read_bytes() == expected
    with pytest.raises(FileExistsError):
        mpr._write_new(raw, b"overwrite")


def test_upload_paths(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def fake_upload(local: Any, path_in_repo: str, stream: str, **kw: Any) -> str:
        common.check_hf_path(path_in_repo, stream)
        local = Path(local)
        names = sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()) if local.is_dir() else [local.name]
        calls.append((path_in_repo, stream, names))
        return "rev123"

    monkeypatch.setattr(mpr, "upload_to_hf", fake_upload)
    assert mpr.run(upload=True) == 0
    assert [c[:2] for c in calls] == [
        ("macro/policy_rate/", "macro"),
        ("macro/policy_rate/manifest.json", "macro"),
    ]
    uploaded = calls[0][2]
    assert "processed/policy_rate.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 6
    m = manifest(tmp_path)
    assert m["hf_path"] == "macro/policy_rate/" and m["hf_revision"] == "rev123"


# --------------------------------------------------------------------------
# Aborts
# --------------------------------------------------------------------------


def test_name_mismatch_aborts_before_any_data_or_file(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{REPO}/meta"] = meta_body(REPO, **{"Series Name": "Repo Rate"})
    assert mpr.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "manifests").exists()
    assert "series name mismatch" in caplog.text
    assert "\"State Bank of Pakistan's Repo Rate\"" in caplog.text and "'Repo Rate'" in caplog.text


def test_missing_start_date_aborts(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.routes[f"/api/v1/series/{TARGET}/meta"] = meta_body(TARGET, **{"Available Since": None})
    assert mpr.run(upload=False) == 1
    assert "no start date" in caplog.text
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (data_body(REVERSE, **{"Series Key": TARGET}), "contains a row for series"),
        (data_body(REVERSE, **{"Series Name": "Something else"}), "row series name"),
        (data_body(REVERSE, obs=[("2026-10-26", "11.00")]), "1 row(s) returned"),
        (data_body(REVERSE, obs=[("2009-08-17", "12.5"), ("2009-08-17", "12.0")]), "duplicate observation dates"),
        (data_body(REVERSE, obs=[("2009-08-17", "12.5"), ("2010-01-01", "n/a")]), "non-numeric"),
        (data_body(REVERSE, obs=[("2009-08-17", "12.5"), ("01/02/2010", "12.0")]), "unrecognised date"),
        ({"message": "bad"}, "unrecognised response shape"),
    ],
)
def test_bad_data_aborts(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, body: Any, message: str
) -> None:
    env.routes[f"/api/v1/series/{REVERSE}/data"] = body
    assert mpr.run(upload=False) == 1
    assert message in caplog.text
    assert not (tmp_path / "manifests").exists()
    assert not (tmp_path / "data" / "macro" / "policy_rate" / "processed").exists()


def test_missing_key_env_names_variable_and_makes_no_request(
    env: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(mpr.KEY_ENV)
    with pytest.raises(RuntimeError, match=mpr.KEY_ENV):
        mpr.run(upload=False)
    assert env.requests == []


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{TARGET}/meta", 404),
        (f"/api/v1/series/{REPO}/meta", 403),
        (f"/api/v1/series/{REVERSE}/data", 500),  # retried, then fails
        (f"/api/v1/series/{REPO}/data", OSError("connection reset")),
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
    assert mpr.run(upload=False) == 1
    out = capsys.readouterr()
    assert mpr.REDACTED in caplog.text  # the URL was logged, with the key removed
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert mpr.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_get_reraises_without_key_or_context(env: FakeApi) -> None:
    env.routes["/x"] = 404
    with pytest.raises(mpr.PolicyRateError) as info:
        mpr._get("https://easydata.sbp.org.pk/x", {"api_key": CANARY}, CANARY, "probe")
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert not mpr.contains_secret(str(exc), CANARY)
    assert mpr.REDACTED in str(exc)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{TARGET}/data"] = data_body(TARGET, **{"Status Comments": CANARY})
    assert mpr.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_log_filter_redacts_key_from_common_logger(caplog: pytest.LogCaptureFixture) -> None:
    with mpr._key_redaction(CANARY):
        logging.getLogger("jobs.common").warning("GET %s failed", f"https://h/?api_key={urllib.parse.quote_plus(CANARY)}")
    assert mpr.REDACTED in caplog.text
    assert not mpr.contains_secret(caplog.text, CANARY)


# --------------------------------------------------------------------------
# Policy-rate flag, parsing, revisions
# --------------------------------------------------------------------------


def test_policy_flags(tmp_path: Path) -> None:
    assert mpr.run(upload=False) == 0
    flag = {(r["series_key"], r["observation_date"].isoformat()): r["is_policy_rate"] for r in processed(tmp_path)}
    assert flag[(REVERSE, "2009-08-17")] is True  # in effect 2009-08-17..2015-05-24
    assert flag[(REVERSE, "2015-05-25")] is False  # after the window: corridor ceiling only
    assert flag[(TARGET, "2015-05-25")] is True and flag[(TARGET, "2016-05-23")] is True
    assert not flag[(REPO, "2009-08-17")] and not flag[(REPO, "2015-05-25")]
    rows = {(r["series_key"], r["observation_date"].isoformat()): r for r in processed(tmp_path)}
    assert rows[(REVERSE, "2009-08-17")]["effective_to"] == date(2015, 5, 25)
    assert rows[(TARGET, "2016-05-23")]["effective_to"] is None


def test_policy_windows_never_overlap() -> None:
    day, end = date(2005, 1, 1), date(2030, 12, 31)
    while day <= end:
        holders = [k for k, (lo, hi) in mpr.POLICY_WINDOWS.items() if lo <= day and (hi is None or day <= hi)]
        assert len(holders) <= 1
        if day >= date(2009, 8, 17):
            assert len(holders) == 1
        assert mpr.policy_series_for(day) == (holders[0] if holders else None)
        day += timedelta(days=1)
    assert mpr.policy_series_for(date(2015, 5, 24)) == REVERSE
    assert mpr.policy_series_for(date(2015, 5, 25)) == TARGET
    assert REPO not in mpr.POLICY_WINDOWS


def test_columns_rows_shape() -> None:
    payload = {
        "columns": ["Series Key", "Observation Date"],
        "rows": [[TARGET, "2015-05-25"], [TARGET, "2016-05-23"]],
    }
    assert mpr.records(payload) == [
        {"Series Key": TARGET, "Observation Date": "2015-05-25"},
        {"Series Key": TARGET, "Observation Date": "2016-05-23"},
    ]
    assert mpr.records({"data": [{"a": 1}]}) == [{"a": 1}]
    with pytest.raises(mpr.PolicyRateError, match="top-level keys"):
        mpr.records({"status": "ok"})


def test_parse_meta_reads_available_since_in_portal_format() -> None:
    meta = mpr.parse_meta(meta_body(TARGET, **{"Available Since": "25-May-2015"}), mpr.SERIES[0])
    assert meta.start_date == date(2015, 5, 25)
    assert meta.last_refresh == date(2026, 10, 28)
    assert meta.dataset_name == DATASET_NAME  # from "Reference Dataset Name"


def test_available_upto_is_not_a_start_date() -> None:
    body = meta_body(TARGET, **{"Available Since": None, "Available Upto": "26-Oct-2026"})
    with pytest.raises(mpr.PolicyRateError, match="no start date"):
        mpr.parse_meta(body, mpr.SERIES[0])


def test_meta_dataset_name_recorded_in_manifest(tmp_path: Path) -> None:
    assert mpr.run(upload=False) == 0
    series = manifest(tmp_path)["extra"]["series"]
    assert all(s["meta_dataset_name"] == DATASET_NAME for s in series.values())


def test_meta_in_columns_rows_shape() -> None:
    payload = {
        "columns": ["Series Key", "Series Name", "Observation Start Date"],
        "rows": [[TARGET, NAMES[TARGET], "25-May-2015"]],
    }
    meta = mpr.parse_meta(payload, mpr.SERIES[0])
    assert meta.start_date == date(2015, 5, 25) and meta.last_refresh is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2015-05-25", date(2015, 5, 25)),
        ("2015-05-25T00:00:00", date(2015, 5, 25)),
        ("2015-05-25T00:00:00Z", date(2015, 5, 25)),
        ("25-May-2015", date(2015, 5, 25)),
        ("25-MAY-15", date(2015, 5, 25)),
    ],
)
def test_parse_date_ok(raw: str, expected: date) -> None:
    assert mpr.parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["05/06/2015", "2015-05-25T13:00:00", "", None, 20150525])
def test_parse_date_rejects(raw: Any) -> None:
    with pytest.raises(mpr.PolicyRateError):
        mpr.parse_date(raw)


def test_interim_keeps_strings_as_returned(tmp_path: Path) -> None:
    assert mpr.run(upload=False) == 0
    interim = pq.read_table(next((tmp_path / "data").rglob("observations-*.parquet"))).to_pylist()
    assert {r["observation_value"] for r in interim} >= {"6.50", "12.50"}
    assert all(r["requested_series_key"] == r["series_key"] for r in interim)


def _rerun(tmp_path: Path) -> int:
    root = tmp_path / "data" / "macro" / "policy_rate"
    for sub in ("raw", "interim", "runs"):  # same-second run ids would collide
        shutil.rmtree(root / sub)
    return mpr.run(upload=False)


def test_revision_warnings_between_runs(tmp_path: Path, env: FakeApi) -> None:
    assert mpr.run(upload=False) == 0
    assert manifest(tmp_path)["extra"]["last_run"]["revisions"] == {"previous": None}
    env.routes[f"/api/v1/series/{TARGET}/data"] = data_body(
        TARGET, obs=[("2015-05-25", "6.25"), ("2016-05-23", "5.75"), ("2017-01-01", "5.75")]
    )
    env.routes[f"/api/v1/series/{REPO}/data"] = data_body(
        REPO, obs=[("2009-08-17", "9.00"), ("2012-01-01", "8.00"), ("2015-05-25", "5.00")]
    )
    assert _rerun(tmp_path) == 0
    m = manifest(tmp_path)
    rev = m["extra"]["last_run"]["revisions"]
    assert rev == {"previous": "loaded", "changed": 1, "removed": 0, "added": 2, "backfilled": 1}
    text = " ".join(m["warnings"])
    assert f"{TARGET} 2015-05-25: 6.5 -> 6.25" in text
    assert f"{REPO} 2012-01-01" in text and "back-filled" in text


def test_removed_observation_is_warned() -> None:
    prev = pa.Table.from_pylist(
        [
            {"series_key": TARGET, "observation_date": date(2015, 5, 25), "value": 6.5},
            {"series_key": TARGET, "observation_date": date(2016, 5, 23), "value": 5.75},
        ]
    )
    cur = [{"series_key": TARGET, "observation_date": date(2015, 5, 25), "value": 6.5}]
    summary, warnings = mpr.diff_revisions(prev, cur)
    assert summary["removed"] == 1
    assert any("removed" in w and "2016-05-23" in w for w in warnings)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(mpr, "run", lambda *, upload: seen.append(upload) or 0)
    assert mpr.main(["--no-upload"]) == 0
    assert mpr.main([]) == 0
    assert seen == [False, True]
