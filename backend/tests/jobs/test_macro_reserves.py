"""Tests for backend/jobs/macro_reserves.py (and ``easydata.check_identities``).

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All values below are synthetic,
shaped like cases seen in the real data (annual 30-June values before 1972,
month-end dates after, series with different spans, two totals that end in
2024-09, real negative values, "." status comments) but not copied from it.
"""

from __future__ import annotations

import calendar
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
from jobs import macro_reserves as rs  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_9c1d+/="  # has characters that URL-encoding changes
TODAY = date(2026, 11, 22)
LATEST = date(2026, 10, 31)
KEYS = [s.key for s in rs.SERIES]
NAMES = {s.key: s.name for s in rs.SERIES}
K = {s.code: s.key for s in rs.SERIES}
SPANS = {s.key: (s.start, s.end or LATEST) for s in rs.SERIES}


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def all_dates(latest: date = LATEST) -> list[date]:
    """Annual 30 June 1948-1971, one more 1971 value (partial year), then
    month ends from 1972."""
    out = [date(y, 6, 30) for y in range(1948, 1972)] + [date(1971, 12, 31)]
    y, m = 1972, 1
    while month_end(y, m) <= latest:
        out.append(month_end(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


ALL = all_dates()

Obs = dict[date, str]  # date -> value as served


def part_value(key: str, d: date) -> float:
    return (KEYS.index(key) + 1) * 100 + (d.year * 12 + d.month) % 37 * 0.5


def fmt(v: float) -> str:
    return f"{v:.2f}"


def default_obs() -> dict[str, Obs]:
    """Every series on its expected span; totals equal the sum of their parts
    wherever all parts exist."""
    data: dict[str, Obs] = {}
    for s in rs.SERIES:
        start, end = SPANS[s.key]
        data[s.key] = {d: fmt(part_value(s.key, d)) for d in ALL if start <= d <= end}
    for total, parts in rs.IDENTITIES:
        for d in data[total]:
            if all(d in data[p] for p in parts):
                data[total][d] = fmt(sum(float(data[p][d]) for p in parts))
    return data


def portal(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def meta_body(key: str, start: date | None = None, **override: Any) -> dict[str, Any]:
    # Field names as returned by EasyData /meta. Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": rs.EXPECTED_DATASET,
        "Available Since": portal(start or SPANS[key][0]),
        "Last Refresh Date": "18-Nov-2026",
        "Data Frequency": "Monthly",
        "Unit": "Million USD",
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


def data_body(
    key: str, rows: Obs, comment: str = ".", status: str = "Normal", **override: Any
) -> dict[str, Any]:
    """columns/rows layout, as in the Developer Guide."""
    out = []
    for d, v in sorted(rows.items()):
        rec = {
            "Dataset Name": rs.EXPECTED_DATASET,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": portal(d),
            "Observation Value": v,
            "Unit": "Million USD",
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
    monkeypatch.setattr(rs, "LOCAL_ROOT", tmp_path / "data" / "macro" / "reserves")
    monkeypatch.setattr(rs, "_today", lambda: TODAY)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)  # manifest warnings independent of the real tree
    monkeypatch.setattr(rs, "git_info", clean_git)
    monkeypatch.setenv(rs.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(rs, "upload_to_hf", no_upload)
    monkeypatch.setattr(rs, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "reserves"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_reserves.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / rs.PROCESSED_REL).to_pylist()
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


def without(rows: Obs, *days: date) -> Obs:
    return {d: v for d, v in rows.items() if d not in days}


# --------------------------------------------------------------------------
# Series table
# --------------------------------------------------------------------------


def test_series_table() -> None:
    codes = ["Z000" + str(n) for n in range(10, 24)] + ["Z00030", "Z00040", "Z00050", "Z00060", "Z00070"]
    assert [s.code for s in rs.SERIES] == codes
    assert KEYS == [f"TS_GP_EXT_PAKRES_M.{c}" for c in codes]
    assert len(set(KEYS)) == 19
    assert NAMES[K["Z00010"]] == "Gold reserves"
    assert NAMES[K["Z00016"]] == "Cash reserve requirement and special cash reserve requirement (CRR/SCRR)"
    assert NAMES[K["Z00019"]] == "Place abroad(other than FE-25) of banks"
    assert NAMES[K["Z00050"]] == "Net Reserves With Banks"
    assert NAMES[K["Z00070"]] == "Total Reserve Assets"
    ended = {s.code: (s.start, s.end) for s in rs.SERIES if s.end is not None}
    assert ended == {
        "Z00016": (date(1999, 6, 30), date(2005, 6, 30)),
        "Z00060": (date(2010, 7, 31), date(2024, 9, 30)),
        "Z00070": (date(1948, 6, 30), date(2024, 9, 30)),
    }
    assert {s.code for s in rs.SERIES if s.start == date(1948, 6, 30)} == {"Z00010", "Z00013", "Z00020", "Z00070"}
    assert {s.code for s in rs.SERIES if s.start == date(2020, 6, 30)} == {"Z00014", "Z00015"}
    assert rs.NEGATIVE_OK == {K["Z00014"], K["Z00018"], K["Z00040"]}
    assert rs.IDENTITIES == (
        (K["Z00060"], (K["Z00030"], K["Z00050"])),
        (K["Z00070"], (K["Z00010"], K["Z00020"], K["Z00040"])),
    )


# --------------------------------------------------------------------------
# Happy path, requests, layout, schema
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert rs.run(upload=False) == 0

    paths = env.paths()
    assert len(paths) == 38
    assert paths[:19] == [f"/api/v1/series/{k}/meta" for k in KEYS]
    assert paths[19:] == [f"/api/v1/series/{k}/data" for k in KEYS]
    for i, k in enumerate(KEYS):
        assert env.params(i) == {"api_key": CANARY, "format": "json"}
        p = env.params(19 + i)
        assert p["start_date"] == SPANS[k][0].isoformat()  # "Available Since", read from /meta
        assert p["end_date"] == TODAY.isoformat() and p["format"] == "json"

    assert len(list((root(tmp_path) / "raw").rglob("*.json"))) == 38
    assert len(list((root(tmp_path) / "interim").rglob("*.parquet"))) == 1

    rows = processed(tmp_path)
    assert len(rows) == sum(len(v) for v in env.obs.values())
    r = rows[(K["Z00060"], "2024-09-30")]
    assert r["series_name"] == "Total Liquid FX Reserves" and r["series_code"] == "Z00060"
    assert r["value"] == float(env.obs[K["Z00060"]][date(2024, 9, 30)])
    assert r["unit"] == "Million USD" and r["observation_status"] == "Normal" and r["status_comments"] == "."
    assert r["dataset_name"] == rs.EXPECTED_DATASET
    assert r["published_at"] == datetime(2024, 10, 25, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "bop_release_25th_next_month"
    assert r["portal_last_refresh_date"] == date(2026, 11, 18)
    assert r["source_endpoint"] == f"https://easydata.sbp.org.pk/api/v1/series/{K['Z00060']}/data"
    assert r["raw_file"].endswith(f"_{K['Z00060']}_data.json")
    assert (K["Z00060"], "2024-10-31") not in rows  # totals are not derived after they end
    assert (K["Z00010"], "1950-06-30") in rows and (K["Z00010"], "1950-07-31") not in rows

    m = manifest(tmp_path)
    assert m["id"] == "macro_reserves"
    assert m["script"] == "backend/jobs/macro_reserves.py"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["hf_revision"] is None
    assert m["date_range"] == {"min": "1948-06-30", "max": "2026-10-31"}
    assert m["warnings"] == []  # annual years before 1972, ended series, "." comments: all expected
    assert m["extra"]["latest_month"] == "2026-10-31"
    assert m["extra"]["citation"] == (
        "State Bank of Pakistan, (2026), Monthly data from Gold and Foreign Exchange Reserves of "
        "Pakistan, for 30-Jun-1948 to 31-Oct-2026, Source EasyData, Gold and Foreign Exchange "
        "Reserves of Pakistan, 18-Nov-2026"
    )
    ident = m["extra"]["identities"]
    assert ident["Z00060 = Z00030 + Z00050"] == {"checked": 171, "skipped_incomplete": 25, "mismatches": []}
    z70 = ident["Z00070 = Z00010 + Z00020 + Z00040"]
    assert z70["mismatches"] == [] and z70["checked"] == 2 + (2024 - 1972) * 12 + 9
    assert z70["skipped_incomplete"] == 23 + 25  # before Z00040 starts; after Z00070 ends


def test_rows_per_series_differ_in_final_log_line(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert rs.run(upload=False) == 0
    counts = runlog(tmp_path)["rows_per_series"]
    assert counts == {k: len(env.obs[k]) for k in KEYS}
    assert counts[K["Z00016"]] == 73  # 1999-06 to 2005-06
    assert manifest(tmp_path)["extra"]["rows_per_series"] == counts
    last = [r for r in caplog.records if r.name == "jobs.macro_reserves"][-1]
    assert last.getMessage().startswith(f"rows per series differ ({sum(counts.values())} total): Z00010=")
    assert last.getMessage().endswith("; 0 warning(s); exit code 0")
    assert runlog(tmp_path)["series"][K["Z00010"]]["annual_rows_before_1972"] == 25


def test_citation_unknown_without_refresh_date(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Last Refresh Date": None})
    assert rs.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (unknown), ") and citation.endswith(", unknown")
    assert f"{K['Z00010']}: metadata has no last-refresh date" in warnings_text(tmp_path)


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

    monkeypatch.setattr(rs, "upload_to_hf", fake_upload)
    assert rs.run(upload=True) == 0
    assert [c[:2] for c in calls] == [("macro/reserves/", "macro"), ("macro/reserves/manifest.json", "macro")]
    uploaded = calls[0][2]
    assert "processed/reserves.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 38
    assert len(uploaded) == 41  # 38 raw, interim, processed, run log
    m = manifest(tmp_path)
    assert m["hf_path"] == "macro/reserves/" and m["hf_revision"] == "rev789"


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "returned"),
    [
        ("Z00060", "Total Liquid FX reserves"),  # capitalisation
        ("Z00019", "Place abroad (other than FE-25) of banks"),  # spacing
        ("Z00016", "Cash reserve requirement and special cash reserve requirement (CRR; SCRR)"),
    ],
)
def test_name_mismatch_aborts_before_any_data(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, code: str, returned: str
) -> None:
    env.routes[f"/api/v1/series/{K[code]}/meta"] = meta_body(K[code], **{"Series Name": returned})
    assert rs.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert "series name mismatch" in caplog.text


def test_leading_dots_stripped_in_processed_kept_in_interim(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00020"]
    env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Series Name": "....... " + NAMES[k]})
    env.set_data(k, **{"Series Name": "....... " + NAMES[k]})
    assert rs.run(upload=False) == 0
    assert all(r["series_name"] == NAMES[r["series_key"]] for r in processed(tmp_path).values())
    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert all(r["series_name"].startswith("....... ") for r in interim if r["series_key"] == k)
    assert runlog(tmp_path)["series"][k]["rows_with_name_prefix"] == len(env.obs[k])
    assert manifest(tmp_path)["warnings"] == []


# --------------------------------------------------------------------------
# Dates: annual years, gaps, spans, staleness
# --------------------------------------------------------------------------


def test_skipped_month_from_1972_warns_annual_years_do_not(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00013"]  # not part of an identity
    env.set_data(k, without(env.obs[k], date(1980, 5, 31), date(1955, 6, 30)))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{k}: 1 skipped month gap(s) from 1972 on: 1980-04-30 to 1980-06-30"
    ]  # 1954-06-30 to 1956-06-30 (two years) is before 1972: not checked
    assert runlog(tmp_path)["checks"]["series"][k]["skipped_months"] == ["1980-04-30 to 1980-06-30"]


def test_first_month_of_1972_after_partial_1971_does_not_warn(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00013"]
    env.set_data(k, without(env.obs[k], date(1971, 12, 31)))  # 1971-06-30 -> 1972-01-31
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []


def test_non_month_end_date_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    k = K["Z00010"]
    env.set_data(k, {**without(env.obs[k], LATEST), date(2026, 10, 30): "100.00"})
    assert rs.run(upload=False) == 1
    assert f"{k}: observation date 2026-10-30 is not a month end" in caplog.text
    assert not (root(tmp_path) / "processed").exists()


def test_ended_total_reappearing_warns(tmp_path: Path, env: FakeApi) -> None:
    k60, k30, k50 = K["Z00060"], K["Z00030"], K["Z00050"]
    d = date(2024, 10, 31)
    env.set_value(k60, d, fmt(float(env.obs[k30][d]) + float(env.obs[k50][d])))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{k60}: ended series reappears after its expected end 2024-09-30: 1 row(s): 2024-10-31"
    ]


def test_ended_series_ending_early_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00016"]
    env.set_data(k, without(env.obs[k], date(2005, 6, 30)))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{k}: ended series' last observation 2005-05-31 is earlier than expected 2005-06-30"
    ]


def test_ongoing_series_ending_early_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00012"]
    env.set_data(k, without(env.obs[k], LATEST))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"1 ongoing series end before the latest month 2026-10-31: {k} (2026-09-30)"
    ]


def test_first_observation_differs_from_expected_start_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00019"]
    env.set_data(k, without(env.obs[k], date(2007, 3, 31)))
    env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, start=date(2007, 4, 30))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{k}: first observation 2007-04-30 differs from expected start 2007-03-31"
    ]


def test_first_observation_differs_from_meta_start_warns(tmp_path: Path, env: FakeApi) -> None:
    k = K["Z00019"]
    env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, start=date(2007, 2, 28))
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [
        f"{k}: first observation 2007-03-31 differs from metadata start date 2007-02-28"
    ]


def test_stale_data_warns_after_60_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_today", lambda: date(2026, 12, 30))  # 60 days after 2026-10-31
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    clear_run_files(tmp_path)
    monkeypatch.setattr(rs, "_today", lambda: date(2026, 12, 31))
    assert rs.run(upload=False) == 0
    assert "latest month 2026-10-31 is 61 days before 2026-12-31" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Values: negatives, identities, bad rows
# --------------------------------------------------------------------------


def test_negative_values_allowed_and_warned_outside_known_series(tmp_path: Path, env: FakeApi) -> None:
    d = date(2025, 3, 31)  # after Z00070 ends, so no identity involves Z00040 here
    for code in ("Z00014", "Z00018", "Z00040"):
        env.set_value(K[code], d, "-12.50")
    env.set_value(K["Z00018"], date(1990, 1, 31), "-3")
    env.set_value(K["Z00013"], d, "-0.40")
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == [f"{K['Z00013']}: 1 negative value(s) (kept): 2025-03-31"]
    rows = processed(tmp_path)
    assert rows[(K["Z00018"], "2025-03-31")]["value"] == -12.5
    assert rows[(K["Z00013"], "2025-03-31")]["value"] == -0.4
    assert runlog(tmp_path)["series"][K["Z00018"]]["negative_values"] == 2


@pytest.mark.parametrize(("total", "warns"), [("1500", False), ("1501", False), ("1499", False), ("1501.01", True)])
def test_identity_z00060_tolerance_1(tmp_path: Path, env: FakeApi, total: str, warns: bool) -> None:
    d = date(2020, 1, 31)
    env.set_value(K["Z00030"], d, "1000")
    env.set_value(K["Z00050"], d, "500")
    env.set_value(K["Z00060"], d, total)
    assert rs.run(upload=False) == 0
    expected = (
        "identity Z00060 = Z00030 + Z00050 differs by more than 1 on 1 date(s): "
        "2020-01-31 (Z00060 1501.01, sum 1500.00, diff +1.01)"
    )
    assert manifest(tmp_path)["warnings"] == ([expected] if warns else [])
    assert processed(tmp_path)[(K["Z00060"], "2020-01-31")]["value"] == float(total)  # kept as returned


def test_identity_z00070_mismatch_warns(tmp_path: Path, env: FakeApi) -> None:
    d = date(1985, 6, 30)
    env.set_value(K["Z00070"], d, fmt(float(env.obs[K["Z00070"]][d]) - 50))
    assert rs.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert "identity Z00070 = Z00010 + Z00020 + Z00040 differs by more than 1 on 1 date(s): 1985-06-30" in text
    assert "diff -50" in text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({date(2020, 1, 31): ""}, "empty value (status 'Normal')"),
        ({date(2020, 1, 31): "  "}, "empty value"),
        ({date(2020, 1, 31): "n/a"}, "non-numeric"),
        ({date(2026, 11, 30): "1.0"}, "observation after end_date"),
    ],
)
def test_bad_values_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, change: Obs, message: str
) -> None:
    env.set_data(K["Z00020"], {**env.obs[K["Z00020"]], **change})
    assert rs.run(upload=False) == 1
    assert message in caplog.text
    assert not (tmp_path / "manifests").exists()
    assert not (root(tmp_path) / "processed").exists()


def test_duplicate_date_aborts(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    k = K["Z00020"]
    body = data_body(k, env.obs[k])
    body["rows"].append(list(body["rows"][-1]))
    env.routes[f"/api/v1/series/{k}/data"] = body
    assert rs.run(upload=False) == 1
    assert "duplicate observation dates ['2026-10-31']" in caplog.text


def test_single_row_aborts(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.set_data(K["Z00020"], {LATEST: "1.0"})
    assert rs.run(upload=False) == 1
    assert "1 row(s) returned" in caplog.text


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"Series Key": "TS_GP_EXT_PAKRES_M.Z00011"}, "contains a row for series"),
        ({"Series Name": "Something else"}, "row series name"),
        ({"Observation Date": "01/02/2019"}, "unrecognised date"),
    ],
)
def test_bad_fields_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, override: dict[str, str], message: str
) -> None:
    env.set_data(K["Z00010"], **override)
    assert rs.run(upload=False) == 1
    assert message in caplog.text
    assert not (root(tmp_path) / "processed").exists()


def test_invalid_json_fails_without_repair(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    k = K["Z00010"]
    comment = 'Synthetic: the observation status is missing "MIS" due to a test.'
    body = json.dumps(data_body(k, env.obs[k], comment=comment)).encode()
    broken = body.replace(json.dumps(comment).encode()[1:-1], comment.encode())
    with pytest.raises(ValueError):
        json.loads(broken)
    env.routes[f"/api/v1/series/{k}/data"] = broken
    assert rs.run(upload=False) == 1
    assert f"{k}: data response is not JSON" in caplog.text
    assert not (root(tmp_path) / "processed").exists()
    raw = next((root(tmp_path) / "raw").rglob(f"*_{k}_data.json"))
    assert raw.read_bytes() == broken  # raw kept exactly as served


def test_missing_key_env_names_variable_and_makes_no_request(env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.KEY_ENV)
    with pytest.raises(RuntimeError, match=rs.KEY_ENV):
        rs.run(upload=False)
    assert env.requests == []


# --------------------------------------------------------------------------
# Status, unit, dataset name
# --------------------------------------------------------------------------


def test_dotted_status_comments_kept_without_warning(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(K["Z00011"], comment=".......")
    assert rs.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    assert processed(tmp_path)[(K["Z00011"], "2000-01-31")]["status_comments"] == "......."


def test_status_other_than_normal_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(K["Z00015"], status="Provisional")
    assert rs.run(upload=False) == 0
    assert f"{K['Z00015']}: 77 row(s) with status other than 'Normal' ['Provisional']" in warnings_text(tmp_path)


def test_unit_other_than_million_usd_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(K["Z00015"], **{"Unit": "Billion USD"})
    assert rs.run(upload=False) == 0
    assert f"{K['Z00015']}: unit(s) ['Billion USD'] != 'Million USD'" in warnings_text(tmp_path)


def test_unexpected_dataset_name_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(K["Z00015"], **{"Dataset Name": "Reserves"})
    assert rs.run(upload=False) == 0
    assert f"{K['Z00015']}: dataset name ['Reserves']" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Changes between runs
# --------------------------------------------------------------------------


def test_revision_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert rs.run(upload=False) == 0
    k, d = K["Z00013"], date(2026, 9, 30)
    old = float(env.obs[k][d])
    env.set_value(k, d, "4321.5")
    clear_run_files(tmp_path)
    assert rs.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1 and rev["status_changed"] == 0
    assert f"{k} 2026-09-30: {old} -> 4321.5" in warnings_text(tmp_path)


def test_status_only_change_is_warned(tmp_path: Path, env: FakeApi) -> None:
    assert rs.run(upload=False) == 0
    env.set_data(K["Z00014"], comment="Revised")  # values unchanged, comment changed
    clear_run_files(tmp_path)
    assert rs.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 0 and rev["status_changed"] == 77
    assert "status or status comment changed with value unchanged since previous run (77 row(s))" in (
        warnings_text(tmp_path)
    )


# --------------------------------------------------------------------------
# published_at
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs_date", "expected"),
    [
        (date(2026, 10, 31), datetime(2026, 11, 25, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2026, 12, 31), datetime(2027, 1, 25, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2024, 2, 29), datetime(2024, 3, 25, 18, 59, 59, tzinfo=timezone.utc)),
        (date(1948, 6, 30), datetime(1948, 7, 25, 18, 59, 59, tzinfo=timezone.utc)),  # annual value
    ],
)
def test_published_at(obs_date: date, expected: datetime) -> None:
    assert rs.published_at_for(obs_date) == expected


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{KEYS[0]}/meta", 404),
        (f"/api/v1/series/{KEYS[-1]}/meta", 403),
        (f"/api/v1/series/{KEYS[5]}/data", 500),  # retried, then fails
        (f"/api/v1/series/{KEYS[7]}/data", OSError("connection reset")),
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
    assert rs.run(upload=False) == 1
    out = capsys.readouterr()
    assert ed.REDACTED in caplog.text
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert rs.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.set_data(KEYS[-1], **{"Status Comments": CANARY})
    assert rs.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(rs, "run", lambda *, upload: seen.append(upload) or 0)
    assert rs.main(["--no-upload"]) == 0
    assert rs.main([]) == 0
    assert seen == [False, True]


# --------------------------------------------------------------------------
# easydata.check_identities and rows_summary (shared helpers)
# --------------------------------------------------------------------------


def test_check_identities_skips_incomplete_dates_and_warns_above_tolerance() -> None:
    d1, d2, d3, d4 = date(2020, 1, 31), date(2020, 2, 29), date(2020, 3, 31), date(2020, 4, 30)
    by_date = {
        d1: {"X.T": 3.0, "X.A": 1.0, "X.B": 2.0},  # exact
        d2: {"X.T": 10.0, "X.A": 1.0, "X.B": 2.0},  # off by 7
        d3: {"X.A": 1.0, "X.B": 2.0},  # no total: skipped
        d4: {"X.Q": 1.0},  # unrelated: neither checked nor skipped
    }
    report, warnings = ed.check_identities(by_date, [("X.T", ["X.A", "X.B"])], 5)
    assert report == {
        "T = A + B": {
            "checked": 2,
            "skipped_incomplete": 1,
            "mismatches": ["2020-02-29 (T 10.00, sum 3.00, diff +7.00)"],
        }
    }
    assert warnings == [
        "identity T = A + B differs by more than 5 on 1 date(s): 2020-02-29 (T 10.00, sum 3.00, diff +7.00)"
    ]
    big = {d1: {"X.T": 40_123_456.78, "X.A": 40_000_000.0, "X.B": 0.0}}  # M2-sized: no exponent notation
    assert ed.check_identities(big, [("X.T", ["X.A", "X.B"])], 5)[0]["T = A + B"]["mismatches"] == [
        "2020-01-31 (T 40123456.78, sum 40000000.00, diff +123456.78)"
    ]
    assert ed.check_identities(by_date, [("X.T", ["X.A", "X.B"])], 7)[1] == []


def test_rows_summary_shared_with_macro_fx() -> None:
    from jobs import macro_fx

    assert macro_fx.rows_summary is ed.rows_summary
    assert ed.rows_summary({"A.X": 2, "A.Y": 2}) == "rows per series: 2 for all 2 series (4 total)"
