"""Tests for backend/jobs/macro_money_supply.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All values below are synthetic,
shaped like cases seen in the real data (weekly Friday dates, series that are
negative by construction, two units, names with double spaces, M2 identities
with a small rounding difference) but not copied from it.
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
from jobs import macro_money_supply as ms  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_9c1d+/="  # has characters that URL-encoding changes
TODAY = date(2026, 11, 22)
KEYS = [s.key for s in ms.SERIES]
NAMES = {s.key: s.name for s in ms.SERIES}
UNITS = {s.key: s.unit for s in ms.SERIES}
K = {s.code: s.key for s in ms.SERIES}
M2, M2_ASSETS, CIC, DEP_SBP, DEP_BANKS = (K[c] for c in ("M000070", "M000420", "M000010", "M000020", "M000030"))
NEGATIVE = {K[c] for c in ("M000180", "M000280", "M000300", "M000410")}  # deposits, other items: negative

FRIDAYS = [date(2026, 9, 18) + timedelta(weeks=i) for i in range(8)]  # 2026-09-18 .. 2026-11-06
N = len(FRIDAYS)

Obs = dict[date, str]


def default_value(key: str, i: int) -> str:
    if key == CIC:
        return str(9000 + i)
    if key == DEP_SBP:
        return "40"
    if key == DEP_BANKS:
        return str(30000 + 3 * i)
    if key in (M2, M2_ASSETS):
        return str(9000 + i + 40 + 30000 + 3 * i)
    if UNITS[key] == ms.UNIT_PERCENT:
        return f"{12 + i * 0.1:.1f}"
    v = (KEYS.index(key) + 1) * 100 + i * 0.25
    return f"{-v if key in NEGATIVE else v:.2f}"


def default_obs() -> dict[str, Obs]:
    return {k: {d: default_value(k, i) for i, d in enumerate(FRIDAYS)} for k in KEYS}


def portal(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def meta_body(key: str, start: date | None = None, **override: Any) -> dict[str, Any]:
    # Field names as returned by EasyData /meta. Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": ms.EXPECTED_DATASET,
        "Available Since": portal(start or FRIDAYS[0]),
        "Last Refresh Date": "12-Nov-2026",
        "Data Frequency": "Weekly",
        "Unit": UNITS[key],
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


def data_body(key: str, rows: Obs, comment: str = "", status: str = "Normal", **override: Any) -> dict[str, Any]:
    """columns/rows layout, as in the Developer Guide."""
    out = []
    for d, v in sorted(rows.items()):
        rec = {
            "Dataset Name": ms.EXPECTED_DATASET,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": portal(d),
            "Observation Value": v,
            "Unit": UNITS[key],
            "Observation Status": status,
            "Status Comments": comment,
        }
        rec.update(override)
        out.append([rec[c] for c in COLUMNS])
    return {"columns": COLUMNS, "rows": out}


class FakeApi:
    """Routes requests by path. Values are a JSON-able body, bytes, a status
    int, or an exception."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[str] = []
        self.obs = default_obs()
        for k in KEYS:
            self.routes[f"/api/v1/series/{k}/meta"] = meta_body(k)
            self.routes[f"/api/v1/series/{k}/data"] = data_body(k, self.obs[k])

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

    def set_data(self, key: str, rows: Obs | None = None, **kw: Any) -> None:
        if rows is not None:
            self.obs[key] = rows
        self.routes[f"/api/v1/series/{key}/data"] = data_body(key, self.obs[key], **kw)

    def set_value(self, key: str, d: date, value: str) -> None:
        self.set_data(key, {**self.obs[key], d: value})

    def set_dates_all(self, dates: list[date]) -> None:
        """Every series on ``dates`` (identities still hold)."""
        for k in KEYS:
            self.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, start=dates[0])
            self.set_data(k, {d: default_value(k, i) for i, d in enumerate(dates)})


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
    monkeypatch.setattr(ms, "LOCAL_ROOT", tmp_path / "data" / "macro" / "money_supply")
    monkeypatch.setattr(ms, "_today", lambda: TODAY)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)  # manifest warnings independent of the real tree
    monkeypatch.setattr(ms, "git_info", clean_git)
    monkeypatch.setenv(ms.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(ms, "upload_to_hf", no_upload)
    monkeypatch.setattr(ms, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "money_supply"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_money_supply.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / ms.PROCESSED_REL).to_pylist()
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


def test_series_table() -> None:
    assert KEYS == [f"TS_GP_BAM_M2_W.M{n * 10:06d}" for n in range(1, 51)]
    assert KEYS[0] == "TS_GP_BAM_M2_W.M000010" and KEYS[-1] == "TS_GP_BAM_M2_W.M000500"
    assert [s.code for s in ms.SERIES if s.unit == "Percent"] == ["M000480", "M000490", "M000500"]
    assert all(s.unit == "Million PKR" for s in ms.SERIES[:47])
    assert NAMES[M2] == "Broad Money (liability side)"
    assert NAMES[M2_ASSETS] == "Broad Money (Assets  side)"  # double space kept
    assert NAMES[K["M000430"]] == (
        "Accrued profit on holdings of MRTBs/MTBs  by SBP and its subsidiaries (memorandum Item)"
    )
    assert NAMES[K["M000440"]] == "Outstanding  amount of MTBs realized value in auctions  (memorandum Item)"
    assert NAMES[K["M000470"]].endswith("from Scheduled Banks  (memorandum Item)")
    assert NAMES[K["M000240"]] == "Azad Jammu & Kashmir Government Borrowings From State Bank of Pakistan"
    assert NAMES[K["M000270"]] == "Federal Government Borrowings from scheduled Banks"
    assert NAMES[K["M000500"]] == "Memorandum: Broad Money (M2) - YoY growth"
    assert sum("  " in n for n in NAMES.values()) == 6
    assert ms.IDENTITIES == ((M2, (M2_ASSETS,)), (M2, (CIC, DEP_SBP, DEP_BANKS)))


# --------------------------------------------------------------------------
# Happy path, requests, layout, schema
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert ms.run(upload=False) == 0

    paths = env.paths()
    assert len(paths) == 100
    assert paths[:50] == [f"/api/v1/series/{k}/meta" for k in KEYS]
    assert paths[50:] == [f"/api/v1/series/{k}/data" for k in KEYS]
    for i in range(50):
        assert env.params(i) == {"api_key": CANARY, "format": "json"}
        p = env.params(50 + i)
        assert p["start_date"] == FRIDAYS[0].isoformat()  # "Available Since", read from /meta
        assert p["end_date"] == TODAY.isoformat() and p["format"] == "json"

    assert len(list((root(tmp_path) / "raw").rglob("*.json"))) == 100

    rows = processed(tmp_path)
    assert len(rows) == 50 * N
    r = rows[(M2, "2026-11-06")]
    assert r["series_name"] == "Broad Money (liability side)" and r["series_code"] == "M000070"
    assert r["value"] == float(default_value(M2, N - 1))
    assert r["unit"] == "Million PKR" and r["observation_status"] == "Normal"
    assert r["dataset_name"] == ms.EXPECTED_DATASET
    assert r["published_at"] == datetime(2026, 11, 20, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "weekly_release_friday_plus_14d"
    assert r["portal_last_refresh_date"] == date(2026, 11, 12)
    assert r["source_endpoint"] == f"https://easydata.sbp.org.pk/api/v1/series/{M2}/data"
    assert rows[(K["M000500"], "2026-11-06")]["unit"] == "Percent"
    assert rows[(K["M000180"], "2026-11-06")]["value"] < 0  # negative by construction, no warning

    m = manifest(tmp_path)
    assert m["id"] == "macro_money_supply"
    assert m["script"] == "backend/jobs/macro_money_supply.py"
    assert m["row_count"] == 50 * N
    assert m["date_range"] == {"min": "2026-09-18", "max": "2026-11-06"}
    assert m["warnings"] == []
    assert m["extra"]["latest_date"] == "2026-11-06"
    assert m["extra"]["identities"] == {
        "M000070 = M000420": {"checked": N, "skipped_incomplete": 0, "mismatches": []},
        "M000070 = M000010 + M000020 + M000030": {"checked": N, "skipped_incomplete": 0, "mismatches": []},
    }
    assert m["extra"]["citation"] == (
        "State Bank of Pakistan, (2026), Weekly data from Weekly Broad Money M2, for 18-Sep-2026 to "
        "06-Nov-2026, Source EasyData, Weekly Broad Money M2, 12-Nov-2026"
    )
    last = [x for x in caplog.records if x.name == "jobs.macro_money_supply"][-1]
    assert last.getMessage() == f"rows per series: {N} for all 50 series ({50 * N} total); 0 warning(s); exit code 0"
    assert runlog(tmp_path)["series"][K["M000180"]]["negative_values"] == N


def test_citation_unknown_without_refresh_date(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Last Refresh Date": None})
    assert ms.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (unknown), ") and citation.endswith(", unknown")


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

    monkeypatch.setattr(ms, "upload_to_hf", fake_upload)
    assert ms.run(upload=True) == 0
    assert [c[:2] for c in calls] == [
        ("macro/money_supply/", "macro"),
        ("macro/money_supply/manifest.json", "macro"),
    ]
    uploaded = calls[0][2]
    assert "processed/money_supply.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 100
    assert len(uploaded) == 103  # 100 raw, interim, processed, run log
    assert manifest(tmp_path)["hf_path"] == "macro/money_supply/"


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "returned"),
    [
        ("M000420", "Broad Money (Assets side)"),  # double space collapsed
        ("M000020", "Deposits with SBP excluding banks, IMF and other Intl. Organizations"),  # CSV form
        ("M000070", "Broad Money (Liability side)"),  # capitalisation
    ],
)
def test_name_mismatch_aborts_before_any_data(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, code: str, returned: str
) -> None:
    env.routes[f"/api/v1/series/{K[code]}/meta"] = meta_body(K[code], **{"Series Name": returned})
    assert ms.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert "series name mismatch" in caplog.text


def test_leading_dots_stripped_double_spaces_kept(tmp_path: Path, env: FakeApi) -> None:
    k = K["M000440"]
    env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Series Name": ". " + NAMES[k]})
    env.set_data(k, **{"Series Name": ". " + NAMES[k]})
    assert ms.run(upload=False) == 0
    assert processed(tmp_path)[(k, "2026-11-06")]["series_name"] == (
        "Outstanding  amount of MTBs realized value in auctions  (memorandum Item)"
    )
    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert all(r["series_name"] == ". " + NAMES[k] for r in interim if r["series_key"] == k)
    assert manifest(tmp_path)["warnings"] == []


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_non_friday_date_warns(tmp_path: Path, env: FakeApi) -> None:
    dates = [*FRIDAYS[:4], FRIDAYS[4] - timedelta(days=1), *FRIDAYS[5:]]  # a Thursday (holiday-shifted)
    env.set_dates_all(dates)
    assert ms.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"{M2}: 1 date(s) not a Friday: 2026-10-15" in text
    assert f"{M2}: 2 gap(s) other than 7 days: 2026-10-09 to 2026-10-15 (6 days), 2026-10-15 to 2026-10-23 (8 days)" in (
        text
    )
    assert len(manifest(tmp_path)["warnings"]) == 100  # both warnings for each of the 50 series


def test_skipped_week_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_dates_all([d for d in FRIDAYS if d != FRIDAYS[3]])
    assert ms.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"{CIC}: 1 gap(s) other than 7 days: 2026-10-02 to 2026-10-16 (14 days)" in text
    assert "not a Friday" not in text
    assert runlog(tmp_path)["checks"]["series"][CIC]["irregular_gaps"] == ["2026-10-02 to 2026-10-16 (14 days)"]


def test_series_lacking_a_date_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["M000340"]
    env.set_data(k, {d: v for d, v in env.obs[k].items() if d != FRIDAYS[3]})
    assert ms.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"1 series lack dates present in other series: {k} (1 missing)" in text
    assert f"{k}: 1 gap(s) other than 7 days" in text


def test_series_ending_early_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["M000340"]
    env.set_data(k, {d: v for d, v in env.obs[k].items() if d != FRIDAYS[-1]})
    assert ms.run(upload=False) == 0
    assert f"1 series end before the latest date 2026-11-06: {k} (2026-10-30)" in warnings_text(tmp_path)


def test_first_observation_differs_from_meta_start_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{M2}/meta"] = meta_body(M2, start=date(2014, 7, 4))
    assert ms.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{M2}: first observation 2026-09-18 differs from metadata start date 2014-07-04"
    ]


def test_stale_data_warns_after_30_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms, "_today", lambda: date(2026, 12, 6))  # 30 days after 2026-11-06
    assert ms.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    clear_run_files(tmp_path)
    monkeypatch.setattr(ms, "_today", lambda: date(2026, 12, 7))
    assert ms.run(upload=False) == 0
    assert "latest date 2026-11-06 is 31 days before 2026-12-07" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Values: identities, units, bad rows
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("delta", "warns"), [("2.01", False), ("5", False), ("-5", False), ("5.01", True)])
def test_identity_m2_liability_vs_assets_tolerance_5(tmp_path: Path, env: FakeApi, delta: str, warns: bool) -> None:
    d = FRIDAYS[2]
    m2 = float(env.obs[M2][d])
    assets = f"{m2 + float(delta):.2f}"
    env.set_value(M2_ASSETS, d, assets)
    assert ms.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert ("identity M000070 = M000420 differs by more than 5 on 1 date(s): 2026-10-02" in text) is warns
    if not warns:
        assert text == ""
    assert processed(tmp_path)[(M2_ASSETS, d.isoformat())]["value"] == float(assets)  # kept as returned


def test_identity_m2_components_mismatch_warns(tmp_path: Path, env: FakeApi) -> None:
    d = FRIDAYS[5]
    env.set_value(CIC, d, str(int(env.obs[CIC][d]) + 100))
    assert ms.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"identity M000070 = M000010 + M000020 + M000030 differs by more than 5 on 1 date(s): "
        f"2026-10-23 (M000070 {float(env.obs[M2][d]):.2f}, sum {float(env.obs[M2][d]) + 100:.2f}, diff -100.00)"
    ]


@pytest.mark.parametrize(
    ("code", "unit"),
    [("M000500", "Million PKR"), ("M000010", "Percent")],
)
def test_wrong_unit_warns(tmp_path: Path, env: FakeApi, code: str, unit: str) -> None:
    env.set_data(K[code], **{"Unit": unit})
    assert ms.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [f"{K[code]}: unit(s) ['{unit}'] != '{UNITS[K[code]]}'"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({FRIDAYS[3]: ""}, "empty value (status 'Normal')"),
        ({FRIDAYS[3]: "n/a"}, "non-numeric"),
        ({date(2026, 11, 27): "1.0"}, "observation after end_date"),
    ],
)
def test_bad_values_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, change: Obs, message: str
) -> None:
    env.set_data(K["M000340"], {**env.obs[K["M000340"]], **change})
    assert ms.run(upload=False) == 1
    assert message in caplog.text
    assert not (tmp_path / "manifests").exists()
    assert not (root(tmp_path) / "processed").exists()


def test_duplicate_date_aborts(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    body = data_body(M2, env.obs[M2])
    body["rows"].insert(0, list(body["rows"][0]))
    env.routes[f"/api/v1/series/{M2}/data"] = body
    assert ms.run(upload=False) == 1
    assert "duplicate observation dates ['2026-09-18']" in caplog.text


def test_single_row_aborts(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.set_data(M2, {FRIDAYS[-1]: "1.0"})
    assert ms.run(upload=False) == 1
    assert "1 row(s) returned" in caplog.text


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"Series Key": "TS_GP_BAM_M2_W.M000080"}, "contains a row for series"),
        ({"Series Name": "Something else"}, "row series name"),
        ({"Observation Date": "11/06/2026"}, "unrecognised date"),
    ],
)
def test_bad_fields_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, override: dict[str, str], message: str
) -> None:
    env.set_data(M2, **override)
    assert ms.run(upload=False) == 1
    assert message in caplog.text
    assert not (root(tmp_path) / "processed").exists()


def test_invalid_json_fails_without_repair(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    comment = 'Synthetic: the observation status is missing "MIS" due to a test.'
    body = json.dumps(data_body(M2, env.obs[M2], comment=comment)).encode()
    broken = body.replace(json.dumps(comment).encode()[1:-1], comment.encode())
    with pytest.raises(ValueError):
        json.loads(broken)
    env.routes[f"/api/v1/series/{M2}/data"] = broken
    assert ms.run(upload=False) == 1
    assert f"{M2}: data response is not JSON" in caplog.text
    raw = next((root(tmp_path) / "raw").rglob(f"*_{M2}_data.json"))
    assert raw.read_bytes() == broken  # raw kept exactly as served


def test_missing_key_env_names_variable_and_makes_no_request(env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ms.KEY_ENV)
    with pytest.raises(RuntimeError, match=ms.KEY_ENV):
        ms.run(upload=False)
    assert env.requests == []


# --------------------------------------------------------------------------
# Status, dataset name
# --------------------------------------------------------------------------


def test_status_other_than_normal_warns_comment_kept(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(K["M000340"], status="Revised", comment="Synthetic note")
    assert ms.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{K['M000340']}: {N} row(s) with status other than 'Normal' ['Revised']: "
        + ", ".join(d.isoformat() for d in FRIDAYS)
    ]
    assert processed(tmp_path)[(K["M000340"], "2026-11-06")]["status_comments"] == "Synthetic note"


def test_unexpected_dataset_name_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(M2, **{"Dataset Name": "Broad Money"})
    assert ms.run(upload=False) == 0
    assert f"{M2}: dataset name ['Broad Money']" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Changes between runs
# --------------------------------------------------------------------------


def test_revision_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert ms.run(upload=False) == 0
    k, d = K["M000340"], FRIDAYS[1]
    old = float(env.obs[k][d])
    env.set_value(k, d, "123456.78")
    clear_run_files(tmp_path)
    assert ms.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1 and rev["status_changed"] == 0
    assert f"{k} {d}: {old} -> 123456.78" in warnings_text(tmp_path)


def test_status_only_change_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert ms.run(upload=False) == 0
    env.set_data(K["M000340"], comment="Revised")  # values unchanged, comment changed
    clear_run_files(tmp_path)
    assert ms.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 0 and rev["status_changed"] == N


# --------------------------------------------------------------------------
# published_at
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs_date", "expected"),
    [
        (date(2026, 11, 6), datetime(2026, 11, 20, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2025, 12, 26), datetime(2026, 1, 9, 18, 59, 59, tzinfo=timezone.utc)),  # across the year
        (date(2014, 7, 4), datetime(2014, 7, 18, 18, 59, 59, tzinfo=timezone.utc)),  # first date
        (date(2024, 2, 23), datetime(2024, 3, 8, 18, 59, 59, tzinfo=timezone.utc)),  # leap February
    ],
)
def test_published_at(obs_date: date, expected: datetime) -> None:
    assert ms.published_at_for(obs_date) == expected


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{KEYS[0]}/meta", 404),
        (f"/api/v1/series/{KEYS[-1]}/meta", 403),
        (f"/api/v1/series/{M2}/data", 500),  # retried, then fails
        (f"/api/v1/series/{CIC}/data", OSError("connection reset")),
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
    assert ms.run(upload=False) == 1
    out = capsys.readouterr()
    assert ed.REDACTED in caplog.text
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert ms.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.set_data(KEYS[-1], **{"Status Comments": CANARY})
    assert ms.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(ms, "run", lambda *, upload: seen.append(upload) or 0)
    assert ms.main(["--no-upload"]) == 0
    assert ms.main([]) == 0
    assert seen == [False, True]
