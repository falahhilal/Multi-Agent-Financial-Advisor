"""Tests for backend/jobs/macro_inflation.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``; the real API
is never called and no real key is used. All values below are synthetic,
shaped like cases seen in the real data (leading empty months in 12MMA and
trimmed-core series, a value with status "Missing value", ".5"-style values,
7-9 decimal values, the 2019 rebase) but not copied from it.
"""

from __future__ import annotations

import json
import logging
import re
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
from jobs import macro_inflation as mi  # noqa: E402

CANARY = "EASYDATA_TEST_CANARY_9c1d+/="  # has characters that URL-encoding changes
TODAY = date(2019, 11, 20)
KEYS = [s.key for s in mi.SERIES]
NAMES = {s.key: s.name for s in mi.SERIES}
MISSING = "Missing value"

# A few series by position: block (0 yoy, 1 mma12, 2 period avg, 3 mom) * 15 + measure.
NAT_YOY = KEYS[0]
NAT_MMA = KEYS[15]
TRIM_U_YOY = KEYS[13]
TRIM_U_MMA = KEYS[15 + 13]
TRIM_U_PA = KEYS[30 + 13]
TRIM_U_MOM = KEYS[45 + 13]
WPI_MOM = KEYS[45 + 7]


def month_ends(start: date, end: date) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        out.append(date.fromordinal(nxt.toordinal() - 1).isoformat())
        y, m = nxt.year, nxt.month
    return out


DATES = month_ends(date(2019, 5, 1), date(2019, 10, 1))  # 2019-05-31 .. 2019-10-31

Obs = list[tuple[str, str | None, str]]  # date, value, status


def value_for(key: str, i: int) -> str:
    return f"{2 + KEYS.index(key) * 0.1 + i * 0.05:.2f}"


def obs(key: str, dates: list[str] | None = None) -> Obs:
    return [(d, value_for(key, i), "Normal") for i, d in enumerate(dates or DATES)]


def portal(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%b-%Y")


def meta_body(key: str, **override: Any) -> dict[str, Any]:
    # Field names as returned by EasyData /meta. Values are synthetic.
    body: dict[str, Any] = {
        "Series Name": NAMES[key],
        "Series Description": "Synthetic description.",
        "Reference Dataset Name": mi.EXPECTED_DATASET,
        "Available Since": portal(DATES[0]),
        "Available Upto": portal(DATES[-1]),
        "Last Refresh Date": "05-Nov-2019",
        "Data Frequency": "Monthly",
        "Unit": "Percent",
    }
    body.update(override)
    return {k: v for k, v in body.items() if v is not None}


def data_body(key: str, rows: Obs | None = None, comment: str = "0", **override: Any) -> dict[str, Any]:
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
            "Dataset Name": mi.EXPECTED_DATASET,
            "Series Key": key,
            "Series Name": NAMES[key],
            "Observation Date": portal(d),
            "Observation Value": "" if v is None else v,
            "Unit": "Percent",
            "Observation Status": status,
            "Status Comments": comment,
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
    monkeypatch.setattr(mi, "LOCAL_ROOT", tmp_path / "data" / "macro" / "inflation")
    monkeypatch.setattr(mi, "_today", lambda: TODAY)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)  # manifest warnings independent of the real tree
    monkeypatch.setattr(mi, "git_info", clean_git)
    monkeypatch.setenv(mi.KEY_ENV, CANARY)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(mi, "upload_to_hf", no_upload)
    monkeypatch.setattr(mi, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "inflation"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_inflation.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / mi.PROCESSED_REL).to_pylist()
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


def test_series_keys_and_exact_names() -> None:
    assert len(KEYS) == 60 == len(set(KEYS))
    assert KEYS[0] == "TS_GP_PT_CPI_M.P00011516"
    assert KEYS[-1] == "TS_GP_PT_CPI_M.P00601516"
    assert all(k == f"TS_GP_PT_CPI_M.P{n:04d}1516" for n, k in enumerate(KEYS, start=1))
    assert NAMES[KEYS[0]] == "National CPI; an Inflation Measure (Year-on-Year basis)"
    assert NAMES[KEYS[10]] == "SPI; an Inflation Measure (Year-on-Year basis)"
    assert NAMES[KEYS[15]] == "National CPI; an Inflation Measure (12-Months Moving average basis)"
    assert NAMES[KEYS[30]] == "National CPI; an Inflation Measure (Period average Since July basis)"
    assert NAMES[KEYS[45]] == "National CPI; an Inflation Measure (Month-on-Month basis)"
    assert NAMES[KEYS[-1]] == "Rural Trimmed Core Inflation; an Inflation Measure (Month-on-Month basis)"


_NAME_RE = re.compile(
    r"^(?:(?P<region>National|Urban|Rural) )?(?:(?P<pre>Food|Non-Food) )?"
    r"(?P<group>CPI|WPI|SPI|NFNE Core Inflation|Trimmed Core Inflation)"
    r"(?: (?P<post>Food|Non-Food))?; an Inflation Measure \("
    r"(?P<base>Year-on-Year|12-Months Moving average|Period average Since July|Month-on-Month) basis\)$"
)
_GROUPS = {
    "CPI": "cpi",
    "WPI": "wpi",
    "SPI": "spi",
    "NFNE Core Inflation": "core_nfne",
    "Trimmed Core Inflation": "core_trimmed",
}
_BASES = {
    "Year-on-Year": "yoy",
    "12-Months Moving average": "mma12",
    "Period average Since July": "period_avg_since_july",
    "Month-on-Month": "mom",
}


def test_series_attributes_match_names() -> None:
    """Independent parse of each name agrees with the fixed table."""
    for s in mi.SERIES:
        m = _NAME_RE.match(s.name)
        assert m, s.name
        comp = m["pre"] or m["post"]
        assert s.index_group == _GROUPS[m["group"]], s.name
        assert s.region == (m["region"] or "national").lower(), s.name
        assert s.component == ({"Food": "food", "Non-Food": "non_food"}[comp] if comp else "general"), s.name
        assert s.measure_base == _BASES[m["base"]], s.name
    combos = {(s.index_group, s.region, s.component, s.measure_base) for s in mi.SERIES}
    assert len(combos) == 60
    assert len({(s.index_group, s.region, s.component) for s in mi.SERIES}) == 15
    blocks = [{s.measure_base for s in mi.SERIES[i : i + 15]} for i in range(0, 60, 15)]
    assert blocks == [{"yoy"}, {"mma12"}, {"period_avg_since_july"}, {"mom"}]


def test_expected_leading_empty_months() -> None:
    by_key = {s.key: s.leading_empty_until for s in mi.SERIES}
    assert by_key[NAT_YOY] is None
    assert by_key[WPI_MOM] is None
    assert by_key[NAT_MMA] == date(2017, 5, 31)
    assert by_key[TRIM_U_YOY] == date(2017, 6, 30)
    assert by_key[TRIM_U_PA] == date(2017, 6, 30)
    assert by_key[TRIM_U_MMA] == date(2018, 5, 31)
    assert by_key[TRIM_U_MOM] == date(2016, 7, 31)


# --------------------------------------------------------------------------
# Happy path, requests, layout, schema
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi) -> None:
    assert mi.run(upload=False) == 0

    paths = env.paths()
    assert len(paths) == 120
    assert paths[:60] == [f"/api/v1/series/{k}/meta" for k in KEYS]
    assert paths[60:] == [f"/api/v1/series/{k}/data" for k in KEYS]
    for i in range(60):
        assert env.params(i) == {"api_key": CANARY, "format": "json"}
        p = env.params(60 + i)
        assert p["start_date"] == DATES[0]  # "Available Since", read from /meta
        assert p["end_date"] == TODAY.isoformat() and p["format"] == "json"

    assert len(list((root(tmp_path) / "raw").rglob("*.json"))) == 120
    assert len(list((root(tmp_path) / "interim").rglob("*.parquet"))) == 1

    rows = processed(tmp_path)
    assert len(rows) == 360
    r = rows[(WPI_MOM, "2019-10-31")]
    assert r["series_name"] == "WPI; an Inflation Measure (Month-on-Month basis)"
    assert (r["index_group"], r["region"], r["component"], r["measure_base"]) == ("wpi", "national", "general", "mom")
    assert r["base_year"] == "2015-16"
    assert r["value"] == float(value_for(WPI_MOM, 5)) and r["is_missing"] is False
    assert r["is_back_calculated"] is False
    assert r["published_at"] == datetime(2019, 11, 10, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at_basis"] == "conservative_10th_next_month_rebase_2019_09"
    assert r["portal_last_refresh_date"] == date(2019, 11, 5)
    assert r["source_endpoint"].endswith(f"/series/{WPI_MOM}/data")
    assert r["status_comments"] == "0"

    floor = datetime(2019, 9, 10, 18, 59, 59, tzinfo=timezone.utc)
    jul, aug, sep = (rows[(NAT_YOY, d)] for d in ("2019-07-31", "2019-08-31", "2019-09-30"))
    assert (jul["is_back_calculated"], jul["published_at"]) == (True, floor)
    assert (aug["is_back_calculated"], aug["published_at"]) == (False, floor)
    assert (sep["is_back_calculated"], sep["published_at"]) == (False, datetime(2019, 10, 10, 18, 59, 59, tzinfo=timezone.utc))

    m = manifest(tmp_path)
    assert m["id"] == "macro_inflation"
    assert m["script"] == "backend/jobs/macro_inflation.py"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["hf_path"] is None and m["hf_revision"] is None
    assert m["row_count"] == 360
    assert m["date_range"] == {"min": "2019-05-31", "max": "2019-10-31"}
    assert m["warnings"] == []
    assert m["extra"]["latest_month"] == "2019-10-31"
    assert m["extra"]["citation"] == (
        "State Bank of Pakistan, (2019), Monthly data from Inflation Snapshot (New Base: 2015-16), "
        "for 31-May-2019 to 31-Oct-2019, Source EasyData, Inflation Snapshot (New Base: 2015-16), 05-Nov-2019"
    )
    assert runlog(tmp_path)["back_calculated_rows"] == 60 * 3  # May, Jun, Jul 2019


def test_citation_year_from_last_refresh_not_run_date(
    tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mi, "_today", lambda: date(2020, 1, 3))  # run in a later year
    env.routes[f"/api/v1/series/{NAT_YOY}/meta"] = meta_body(NAT_YOY, **{"Last Refresh Date": "08-Nov-2019"})
    assert mi.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (2019), ")
    assert citation.endswith(", 08-Nov-2019")  # newest refresh across series
    assert runlog(tmp_path)["citation"] == citation


def test_citation_unknown_without_refresh_date(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Last Refresh Date": None})
    assert mi.run(upload=False) == 0
    citation = manifest(tmp_path)["extra"]["citation"]
    assert citation.startswith("State Bank of Pakistan, (unknown), ")
    assert citation.endswith(", unknown")
    assert f"{NAT_YOY}: metadata has no last-refresh date" in warnings_text(tmp_path)


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

    monkeypatch.setattr(mi, "upload_to_hf", fake_upload)
    assert mi.run(upload=True) == 0
    assert [c[:2] for c in calls] == [
        ("macro/inflation/", "macro"),
        ("macro/inflation/manifest.json", "macro"),
    ]
    uploaded = calls[0][2]
    assert "processed/inflation.parquet" in uploaded
    assert sum(n.startswith("raw/") for n in uploaded) == 120
    m = manifest(tmp_path)
    assert m["hf_path"] == "macro/inflation/" and m["hf_revision"] == "rev789"


# --------------------------------------------------------------------------
# Names with leading dots
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (". National CPI, x", "National CPI, x"),
        ("............. Urban CPI, x", "Urban CPI, x"),
        ("  .. . SPI, x", "SPI, x"),
        ("WPI; an Inflation Measure (Year-on-Year basis)", "WPI; an Inflation Measure (Year-on-Year basis)"),
        ("X. Y", "X. Y"),
    ],
)
def test_clean_name(raw: str, clean: str) -> None:
    assert mi.clean_name(raw) == clean


def test_leading_dots_stripped_in_processed_kept_in_interim(tmp_path: Path, env: FakeApi) -> None:
    for k in KEYS:
        env.routes[f"/api/v1/series/{k}/meta"] = meta_body(k, **{"Series Name": "............. " + NAMES[k]})
        env.routes[f"/api/v1/series/{k}/data"] = data_body(k, **{"Series Name": ". " + NAMES[k]})
    assert mi.run(upload=False) == 0
    assert all(r["series_name"] == NAMES[r["series_key"]] for r in processed(tmp_path).values())
    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert all(r["series_name"].startswith(". ") for r in interim)
    log = runlog(tmp_path)
    assert log["series"][NAT_YOY]["meta_name_as_returned"] == "............. " + NAMES[NAT_YOY]
    assert log["series"][NAT_YOY]["rows_with_name_prefix"] == 6
    assert manifest(tmp_path)["warnings"] == []


@pytest.mark.parametrize(
    "returned",
    [
        # "Moving Average" capitalised differently from the expected name
        ". National CPI; an Inflation Measure (12-Months Moving average basis)".replace("average", "Average"),
        # the manual CSV's comma form; the API uses "; " and the check stays exact
        "National CPI, an Inflation Measure (12-Months Moving average basis)",
    ],
)
def test_name_mismatch_after_strip_aborts_before_any_data(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, returned: str
) -> None:
    env.routes[f"/api/v1/series/{NAT_MMA}/meta"] = meta_body(NAT_MMA, **{"Series Name": returned})
    assert mi.run(upload=False) == 1
    assert not any(p.endswith("/data") for p in env.paths())
    assert not (tmp_path / "data").exists()
    assert "series name mismatch" in caplog.text


# --------------------------------------------------------------------------
# Empty values
# --------------------------------------------------------------------------


def test_expected_leading_empty_values_kept_as_null_without_warning(tmp_path: Path, env: FakeApi) -> None:
    long_dates = month_ends(date(2016, 7, 1), date(2019, 10, 1))
    for key in (NAT_MMA, TRIM_U_YOY, TRIM_U_PA, TRIM_U_MMA, TRIM_U_MOM):
        until = next(s.leading_empty_until for s in mi.SERIES if s.key == key)
        assert until is not None
        rows: Obs = [
            (d, None, MISSING) if d <= until.isoformat() else (d, value_for(key, i), "Normal")
            for i, d in enumerate(long_dates)
        ]
        env.routes[f"/api/v1/series/{key}/meta"] = meta_body(key, **{"Available Since": portal(long_dates[0])})
        env.set_data(key, rows)
    assert mi.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    rows_out = processed(tmp_path)
    assert rows_out[(TRIM_U_MMA, "2018-05-31")]["value"] is None
    assert rows_out[(TRIM_U_MMA, "2018-05-31")]["is_missing"] is True
    assert rows_out[(TRIM_U_MMA, "2018-06-30")]["value"] is not None
    assert rows_out[(TRIM_U_MOM, "2016-07-31")]["is_missing"] is True
    assert rows_out[(TRIM_U_MOM, "2016-08-31")]["is_missing"] is False
    log = runlog(tmp_path)
    assert log["series"][NAT_MMA]["leading_missing"] == 11  # 2016-07 .. 2017-05
    assert log["series"][TRIM_U_YOY]["leading_missing"] == 12  # 2016-07 .. 2017-06
    assert log["series"][TRIM_U_MMA]["leading_missing"] == 23  # 2016-07 .. 2018-05
    assert log["series"][TRIM_U_MOM]["leading_missing"] == 1


# Synthetic comment shaped like EasyData's, with the same unescaped-quote defect.
MIS_COMMENT = 'Synthetic: the observation status is missing "MIS" due to trimmed inflation.'


def defective_body(key: str, rows: Obs, comment: str = MIS_COMMENT, on_missing_only: bool = True) -> bytes:
    """A /data body whose comment is written with its quotes unescaped, as
    EasyData does: invalid JSON."""
    body = data_body(key, rows)
    for rec in body["rows"]:
        if not on_missing_only or rec[4] == "":
            rec[7] = comment
    text = json.dumps(body).encode()
    escaped = json.dumps(comment).encode()[1:-1]
    broken = text.replace(escaped, comment.encode())
    with pytest.raises(ValueError):
        json.loads(broken)
    return broken


def leading_empty_rows(key: str, dates: list[str]) -> Obs:
    until = next(s.leading_empty_until for s in mi.SERIES if s.key == key)
    assert until is not None
    return [
        (d, None, MISSING) if d <= until.isoformat() else (d, value_for(key, i), "Normal")
        for i, d in enumerate(dates)
    ]


def test_unescaped_mis_comment_repaired_for_parsing_raw_unchanged(tmp_path: Path, env: FakeApi) -> None:
    long_dates = month_ends(date(2016, 7, 1), date(2019, 10, 1))
    served = {}
    for key in (NAT_MMA, TRIM_U_MMA, TRIM_U_MOM):
        served[key] = defective_body(key, leading_empty_rows(key, long_dates))
        env.routes[f"/api/v1/series/{key}/meta"] = meta_body(key, **{"Available Since": portal(long_dates[0])})
        env.routes[f"/api/v1/series/{key}/data"] = served[key]
    assert mi.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    for key, body in served.items():  # raw keeps exactly what was served
        raw = next((root(tmp_path) / "raw").rglob(f"*_{key}_data.json"))
        assert raw.read_bytes() == body
    r = processed(tmp_path)[(TRIM_U_MMA, "2017-01-31")]
    assert r["is_missing"] is True and r["status_comments"] == MIS_COMMENT
    log = runlog(tmp_path)
    assert log["series"][NAT_MMA]["json_repairs"] == 11
    assert log["series"][TRIM_U_MMA]["json_repairs"] == 23
    assert log["series"][TRIM_U_MOM]["json_repairs"] == 1
    assert log["series"][NAT_YOY]["json_repairs"] == 0
    assert log["json_repairs"] == {"series": 3, "total": 35}


def test_other_unescaped_quote_still_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    rows = leading_empty_rows(TRIM_U_MOM, month_ends(date(2016, 7, 1), date(2019, 10, 1)))
    env.routes[f"/api/v1/series/{TRIM_U_MOM}/meta"] = meta_body(TRIM_U_MOM, **{"Available Since": "31-Jul-2016"})
    env.routes[f"/api/v1/series/{TRIM_U_MOM}/data"] = defective_body(
        TRIM_U_MOM, rows, comment='Synthetic: value is "MISSING" for now.'
    )
    assert mi.run(upload=False) == 1
    assert f"{TRIM_U_MOM}: data response is not JSON" in caplog.text
    assert not (root(tmp_path) / "processed").exists()
    assert not (tmp_path / "manifests").exists()


def test_unexpected_repair_warns(tmp_path: Path, env: FakeApi) -> None:
    # NAT_YOY has no expected leading empties; the defect on a normal row warns.
    env.routes[f"/api/v1/series/{NAT_YOY}/data"] = defective_body(
        NAT_YOY, obs(NAT_YOY), on_missing_only=False
    )
    assert mi.run(upload=False) == 0
    assert runlog(tmp_path)["series"][NAT_YOY]["json_repairs"] == 6
    assert (
        f'unexpected JSON repairs (unescaped "MIS" in status comment) in 1 series: '
        f"{NAT_YOY} (6 repaired, 0 leading empty)"
    ) in warnings_text(tmp_path)


def test_repair_body_leaves_valid_json_alone() -> None:
    body = json.dumps({"comment": 'already \\"MIS\\" escaped'}).encode()
    assert mi._repair_body(body) == (body, 0)
    assert mi._repair_body(b"not json") == (b"not json", 0)


def test_leading_empty_longer_than_expected_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(NAT_YOY, with_value(NAT_YOY, DATES[0], None))  # no leading empties expected
    assert mi.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"{NAT_YOY}: 1 leading empty value(s) beyond the expected none: 2019-05-31" in text


def test_empty_after_first_value_warns_and_is_null(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-08-31", None))
    assert mi.run(upload=False) == 0
    r = processed(tmp_path)[(WPI_MOM, "2019-08-31")]
    assert r["value"] is None and r["is_missing"] is True
    assert f"{WPI_MOM}: 1 empty value(s) after the first value (kept as null): 2019-08-31" in warnings_text(tmp_path)


def test_value_with_missing_status_warns_and_is_kept(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(NAT_YOY, with_value(NAT_YOY, "2019-09-30", "3.1", MISSING))
    assert mi.run(upload=False) == 0
    r = processed(tmp_path)[(NAT_YOY, "2019-09-30")]
    assert r["value"] == 3.1 and r["is_missing"] is False and r["observation_status"] == MISSING
    assert r["status_comments"] == "0"
    assert f"{NAT_YOY}: 1 row(s) have a value but status 'Missing value'" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Values, dates, aborts
# --------------------------------------------------------------------------


def test_value_formats_and_precision(tmp_path: Path, env: FakeApi) -> None:
    rows = obs(WPI_MOM)
    rows[1] = (DATES[1], ".5", "Normal")
    rows[2] = (DATES[2], "-.3", "Normal")
    rows[3] = (DATES[3], "12.34567891", "Normal")
    env.set_data(WPI_MOM, rows)
    assert mi.run(upload=False) == 0
    out = processed(tmp_path)
    assert out[(WPI_MOM, DATES[1])]["value"] == 0.5
    assert out[(WPI_MOM, DATES[2])]["value"] == -0.3
    assert out[(WPI_MOM, DATES[3])]["value"] == 12.34567891
    assert runlog(tmp_path)["series"][WPI_MOM]["max_decimals"] == 8
    assert manifest(tmp_path)["warnings"] == []


def test_value_outside_sane_range_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(NAT_YOY, with_value(NAT_YOY, "2019-06-30", "120.5"))
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-06-30", "-50"))  # boundary, allowed
    assert mi.run(upload=False) == 0
    text = warnings_text(tmp_path)
    assert f"{NAT_YOY}: 1 value(s) outside [-50.0, 100.0]: 2019-06-30 (120.5)" in text
    assert WPI_MOM not in text


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (with_value(NAT_YOY, "2019-06-30", None, "Normal"), "empty value with status 'Normal'"),
        (with_value(NAT_YOY, "2019-06-30", "-100"), "outside (-100.0, 1000.0)"),
        (with_value(NAT_YOY, "2019-06-30", "-150"), "outside (-100.0, 1000.0)"),
        (with_value(NAT_YOY, "2019-06-30", "1000"), "outside (-100.0, 1000.0)"),
        (with_value(NAT_YOY, "2019-06-30", "1500"), "outside (-100.0, 1000.0)"),
        (with_value(NAT_YOY, "2019-06-30", "n/a"), "non-numeric"),
        ([*obs(NAT_YOY), (DATES[-1], "3.0", "Normal")], "duplicate observation dates"),
        ([(DATES[-1], "3.0", "Normal")], "1 row(s) returned"),
        ([*obs(NAT_YOY), ("2019-11-30", "3.0", "Normal")], "observation after end_date"),
        ([*obs(NAT_YOY)[:-1], ("2019-10-15", "3.0", "Normal")], "is not a month end"),
    ],
)
def test_bad_rows_abort(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, rows: Obs, message: str
) -> None:
    env.set_data(NAT_YOY, rows)
    assert mi.run(upload=False) == 1
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
    env.routes[f"/api/v1/series/{NAT_YOY}/data"] = data_body(NAT_YOY, **override)
    assert mi.run(upload=False) == 1
    assert message in caplog.text


def test_missing_key_env_names_variable_and_makes_no_request(env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mi.KEY_ENV)
    with pytest.raises(RuntimeError, match=mi.KEY_ENV):
        mi.run(upload=False)
    assert env.requests == []


def test_skipped_month_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(NAT_YOY, [r for r in obs(NAT_YOY) if r[0] != "2019-07-31"])
    assert mi.run(upload=False) == 0
    assert f"{NAT_YOY}: 1 skipped month gap(s): 2019-06-30 to 2019-08-31" in warnings_text(tmp_path)


def test_series_ending_early_warns(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(WPI_MOM, obs(WPI_MOM)[:-1])
    assert mi.run(upload=False) == 0
    assert f"1 series end before the latest month 2019-10-31: {WPI_MOM} (2019-09-30)" in warnings_text(tmp_path)


def test_first_observation_differs_from_meta_start_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{NAT_YOY}/meta"] = meta_body(NAT_YOY, **{"Available Since": "30-Apr-2019"})
    assert mi.run(upload=False) == 0
    assert f"{NAT_YOY}: first observation 2019-05-31 differs from metadata start date 2019-04-30" in warnings_text(
        tmp_path
    )


def test_unexpected_dataset_name_warns(tmp_path: Path, env: FakeApi) -> None:
    env.routes[f"/api/v1/series/{NAT_YOY}/data"] = data_body(NAT_YOY, **{"Dataset Name": "Inflation Snapshot"})
    assert mi.run(upload=False) == 0
    assert f"{NAT_YOY}: dataset name ['Inflation Snapshot']" in warnings_text(tmp_path)


def test_stale_data_warns_after_45_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mi, "_today", lambda: date(2019, 12, 15))  # 45 days after 2019-10-31
    assert mi.run(upload=False) == 0
    assert manifest(tmp_path)["warnings"] == []
    clear_run_files(tmp_path)
    monkeypatch.setattr(mi, "_today", lambda: date(2019, 12, 16))  # 46 days
    assert mi.run(upload=False) == 0
    assert "latest month 2019-10-31 is 46 days before 2019-12-16" in warnings_text(tmp_path)


# --------------------------------------------------------------------------
# Changes between runs
# --------------------------------------------------------------------------


def test_revision_null_to_value_is_warned(tmp_path: Path, env: FakeApi) -> None:
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-08-31", None))
    assert mi.run(upload=False) == 0
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-08-31", "1.2"))
    clear_run_files(tmp_path)
    assert mi.run(upload=False) == 0
    rev = manifest(tmp_path)["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1 and rev["status_changed"] == 0
    assert f"{WPI_MOM} 2019-08-31: None -> 1.2" in warnings_text(tmp_path)


def test_status_only_change_is_warned(tmp_path: Path, env: FakeApi) -> None:
    # First run: value present with status "Missing value", comment "0" (as in April 2026).
    env.set_data(NAT_YOY, with_value(NAT_YOY, "2019-09-30", "3.1", MISSING))
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-09-30", "1.0", MISSING))
    assert mi.run(upload=False) == 0
    # Second run: NAT_YOY status fixed, value unchanged; WPI_MOM status fixed and value revised;
    # NAT_MMA comment changed only.
    env.set_data(NAT_YOY, with_value(NAT_YOY, "2019-09-30", "3.1", "Normal"))
    env.set_data(WPI_MOM, with_value(WPI_MOM, "2019-09-30", "1.1", "Normal"))
    env.set_data(NAT_MMA, obs(NAT_MMA), comment="Revised")
    clear_run_files(tmp_path)
    assert mi.run(upload=False) == 0
    m = manifest(tmp_path)
    rev = m["extra"]["last_run"]["revisions"]
    assert rev["changed"] == 1  # WPI_MOM value only
    assert rev["status_changed"] == 1 + 6  # NAT_YOY one row, NAT_MMA six rows
    text = warnings_text(tmp_path)
    assert "status or status comment changed with value unchanged since previous run (7 row(s))" in text
    assert f"{NAT_YOY} 2019-09-30: status 'Missing value' -> 'Normal', comment '0' -> '0'" in text
    changes = runlog(tmp_path)["status_changes"]["status_changes"]
    assert not any(c.startswith(WPI_MOM) for c in changes)
    assert f"{NAT_MMA} 2019-05-31: status 'Normal' -> 'Normal', comment '0' -> 'Revised'" in changes


def test_status_changes_without_previous() -> None:
    assert mi.status_changes(None, []) == ({"previous": None}, [])


# --------------------------------------------------------------------------
# published_at and back-calculation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs_date", "expected"),
    [
        (date(2026, 10, 31), datetime(2026, 11, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2025, 12, 31), datetime(2026, 1, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2019, 9, 30), datetime(2019, 10, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2019, 8, 31), datetime(2019, 9, 10, 18, 59, 59, tzinfo=timezone.utc)),
        (date(2019, 7, 31), datetime(2019, 9, 10, 18, 59, 59, tzinfo=timezone.utc)),  # floor
        (date(2016, 7, 31), datetime(2019, 9, 10, 18, 59, 59, tzinfo=timezone.utc)),  # floor
    ],
)
def test_published_at(obs_date: date, expected: datetime) -> None:
    assert mi.published_at_for(obs_date) == expected


def test_is_back_calculated() -> None:
    assert mi.is_back_calculated(date(2016, 7, 31)) is True
    assert mi.is_back_calculated(date(2019, 7, 31)) is True
    assert mi.is_back_calculated(date(2019, 8, 31)) is False


# --------------------------------------------------------------------------
# API key never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (f"/api/v1/series/{NAT_YOY}/meta", 404),
        (f"/api/v1/series/{KEYS[-1]}/meta", 403),
        (f"/api/v1/series/{NAT_MMA}/data", 500),  # retried, then fails
        (f"/api/v1/series/{WPI_MOM}/data", OSError("connection reset")),
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
    assert mi.run(upload=False) == 1
    out = capsys.readouterr()
    assert ed.REDACTED in caplog.text
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_never_leaks_on_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    assert mi.run(upload=False) == 0
    out = capsys.readouterr()
    assert_no_canary(tmp_path, caplog.text, out.out, out.err)


def test_key_echoed_in_response_aborts_before_writing(
    tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    env.routes[f"/api/v1/series/{KEYS[-1]}/data"] = data_body(KEYS[-1], **{"Status Comments": CANARY})
    assert mi.run(upload=False) == 1
    assert "API key appears in" in caplog.text
    assert not (tmp_path / "data").exists()
    assert_no_canary(tmp_path, caplog.text)


def test_main_no_upload_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(mi, "run", lambda *, upload: seen.append(upload) or 0)
    assert mi.main(["--no-upload"]) == 0
    assert mi.main([]) == 0
    assert seen == [False, True]
