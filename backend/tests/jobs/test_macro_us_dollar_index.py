"""Tests for backend/jobs/macro_us_dollar_index.py.

Offline only: every HTTP request goes to a fake ``_fetch_once``. The zip is
built in the test, shaped like the manual download of FRB_h10_xml.zip
(2026-11-23): members ``frb_common.xsd``, ``H10_data.xml``, ``H10_H10.xsd``,
``H10_struct.xml``; ``H10_data.xml`` is SDMX-ML compact with a
``message:Header`` (ID ``H10``, ``Prepared``) and ``kf:Series`` elements
whose attributes are CURRENCY, FREQ, FX, SERIES_NAME, UNIT, UNIT_MULT, with
"Short/Long Description" annotations and ``frb:Obs`` rows
(``OBS_STATUS`` A, or ND with ``OBS_VALUE`` -9999). Values are synthetic.
"""

from __future__ import annotations

import dataclasses
import io
import json
import shutil
import sys
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402
from jobs import macro_us_dollar_index as dx  # noqa: E402

TODAY = date(2026, 11, 23)
START = date(2026, 1, 5)
LAST = date(2026, 11, 13)
ND_DAYS = {date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11)}
ZIP_PATH = "/releases/h10/data/FRB_h10_xml.zip"
BASE = {"JRXWTFB_N.B": 120.0, "JRXWTFN_N.B": 110.0, "JRXWTFO_N.B": 130.0}
KEYS = list(BASE)
REAL_SERIES = dx.SERIES

NS = (
    'xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message" '
    'xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" '
    'xmlns:frb="http://www.federalreserve.gov/structure/compact/common" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)


def weekdays(start: date = START, last: date = LAST) -> list[date]:
    d, out = start, []
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def value(key: str, d: date) -> str:
    return f"{BASE[key] + (d.toordinal() % 5) * 0.05:.4f}"


def default_obs(key: str) -> dict[date, tuple[str, str]]:
    return {d: ("ND", "-9999") if d in ND_DAYS else ("A", value(key, d)) for d in weekdays()}


def series_xml(
    key: str, fx: str, name: str, obs: dict[date, tuple[str, str]], attrs: dict[str, str] | None = None, long: str | None = None
) -> str:
    a = {"CURRENCY": "NA", "FREQ": "9", "FX": fx, "SERIES_NAME": key, "UNIT": "Index:_1997_Jan_100", "UNIT_MULT": "1"}
    a.update(attrs or {})
    attr_text = " ".join(f'{k}="{v}"' for k, v in sorted(a.items()))
    ann = "".join(
        f"<common:Annotation><common:AnnotationType>{t}</common:AnnotationType>"
        f"<common:AnnotationText>{x}</common:AnnotationText></common:Annotation>"
        for t, x in (("Short Description", name), ("Long Description", long if long is not None else name))
    )
    rows = "".join(
        f'<frb:Obs OBS_STATUS="{st}" OBS_VALUE="{v}" TIME_PERIOD="{d.isoformat()}" />' for d, (st, v) in sorted(obs.items())
    )
    return f"<kf:Series {attr_text}><frb:Annotations>{ann}</frb:Annotations>{rows}</kf:Series>"


class FakeApi:
    def __init__(self) -> None:
        self.obs = {k: default_obs(k) for k in KEYS}
        self.attrs: dict[str, dict[str, str]] = {}
        self.names = {s.key: s.name for s in REAL_SERIES}
        self.longs: dict[str, str] = {}
        self.extra_series: list[str] = []
        self.skip: set[str] = set()
        self.message_id = "H10"
        self.prepared: str | None = "2026-11-16T16:47:01"
        self.prolog = ""
        self.members: dict[str, bytes] = {"frb_common.xsd": b"<xs/>", "H10_H10.xsd": b"<xs/>", "H10_struct.xml": b"<s/>"}
        self.body: bytes | int | None = None
        self.served: bytes | int | None = None
        self.requests: list[str] = []

    def data_xml(self) -> bytes:
        fx = {s.key: s.fx for s in REAL_SERIES}
        other = series_xml("RXI_N.B.EU", "EUR", "U.S. Dollars to One Euro", {START: ("A", "1.1000"), START + timedelta(1): ("A", "1.1100")})
        monthly = series_xml("JRXWTFB_N.M", "BRD", "Nominal Broad Dollar Index", {date(2026, 1, 31): ("A", "120.0")}, {"FREQ": "129"})
        targets = [
            series_xml(k, fx[k], self.names[k], self.obs[k], self.attrs.get(k), self.longs.get(k))
            for k in KEYS
            if k not in self.skip
        ]
        prepared = f"<message:Prepared>{self.prepared}</message:Prepared>" if self.prepared else ""
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>' + self.prolog
            + f"<message:MessageGroup {NS}><message:Header><message:ID>{self.message_id}</message:ID>"
            "<message:Test>false</message:Test><message:Name>G.5/H.10 - Foreign Exchange Rates</message:Name>"
            f"{prepared}</message:Header>"
            '<frb:DataSet id="H10" xmlns:kf="http://www.federalreserve.gov/structure/compact/H10_H10">'
            + other + "".join(targets) + "".join(self.extra_series) + monthly
            + "</frb:DataSet></message:MessageGroup>"
        ).encode()

    def zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in {**self.members, "H10_data.xml": self.data_xml()}.items():
                if data is not None:
                    zf.writestr(name, data)
        return buf.getvalue()

    def __call__(self, url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "www.federalreserve.gov", url
        if parts.path == "/robots.txt":
            return common.HttpResponse(url, 404, {}, b"")
        self.requests.append(url)
        assert parts.path == ZIP_PATH and not parts.query, url
        out = self.zip_bytes() if self.body is None else self.body
        self.served = out  # zip entries carry the build time, so rebuilding later can differ
        if isinstance(out, int):
            return common.HttpResponse(url, out, {}, b"error")
        return common.HttpResponse(url, 200, {"content-type": "application/zip"}, out)


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
    monkeypatch.setattr(dx, "LOCAL_ROOT", tmp_path / "data" / "macro" / "us_dollar_index")
    monkeypatch.setattr(dx, "_today", lambda: TODAY)
    monkeypatch.setattr(dx, "EXPECTED_START", START)
    clean_git = lambda: {"commit": "0" * 40, "dirty": False}  # noqa: E731
    monkeypatch.setattr(common, "git_info", clean_git)
    monkeypatch.setattr(dx, "git_info", clean_git)

    def no_upload(*a: Any, **k: Any) -> str:
        raise AssertionError("upload_to_hf must not be called")

    monkeypatch.setattr(dx, "upload_to_hf", no_upload)
    monkeypatch.setattr(dx, "hf_download", lambda *a, **k: None)
    return api


def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "macro" / "us_dollar_index"


def manifest(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "manifests" / "macro_us_dollar_index.json").read_text("utf-8"))


def processed(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = pq.read_table(root(tmp_path) / dx.PROCESSED_REL).to_pylist()
    return {(r["series_key"], r["observation_date"].isoformat()): r for r in rows}


def warnings_text(tmp_path: Path) -> str:
    return "\n".join(manifest(tmp_path)["warnings"])


def test_series_table() -> None:
    assert [(s.key, s.fx, s.name) for s in REAL_SERIES] == [
        ("JRXWTFB_N.B", "BRD", "Nominal Broad Dollar Index"),
        ("JRXWTFN_N.B", "AFE", "Nominal Advanced Foreign Economies Dollar Index"),
        ("JRXWTFO_N.B", "EME", "Nominal Emerging Market Economies Dollar Index"),
    ]
    assert dx.COMMON_ATTRS == {"CURRENCY": "NA", "FREQ": "9", "UNIT": "Index:_1997_Jan_100", "UNIT_MULT": "1"}
    assert dx.EXPECTED_MEMBERS == {"frb_common.xsd", "H10_data.xml", "H10_H10.xsd", "H10_struct.xml"}
    assert [len(s.known_moves) for s in REAL_SERIES] == [4, 11, 6]


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_run_no_upload(tmp_path: Path, env: FakeApi) -> None:
    assert dx.run(upload=False) == 0
    assert len(env.requests) == 1

    raw = list((root(tmp_path) / "raw").rglob("*_FRB_h10_xml.zip"))
    assert len(raw) == 1 and raw[0].read_bytes() == env.served

    rows = processed(tmp_path)
    a_days = [d for d in weekdays() if d not in ND_DAYS]
    assert len(rows) == 3 * len(a_days)
    assert ("JRXWTFB_N.B", "2026-11-11") not in rows  # ND
    r = rows[("JRXWTFO_N.B", LAST.isoformat())]
    assert r["series_name"] == "Nominal Emerging Market Economies Dollar Index" and r["series_code"] == "EME"
    assert r["value"] == float(value("JRXWTFO_N.B", LAST))
    assert r["unit"] == "Index:_1997_Jan_100" and r["unit_mult"] == "1" and r["observation_status"] == "A"
    # Friday 2026-11-13 and Monday 2026-11-09 -> Monday 2026-11-16 + 2 days, 23:59:59 PKT.
    expected = datetime(2026, 11, 18, 18, 59, 59, tzinfo=timezone.utc)
    assert r["published_at"] == expected
    assert rows[("JRXWTFB_N.B", "2026-11-09")]["published_at"] == expected
    assert r["published_at_basis"] == "h10_following_monday_plus_2d"
    assert r["release_prepared"] == "2026-11-16T16:47:01"
    assert r["source_endpoint"] == "https://www.federalreserve.gov/releases/h10/data/FRB_h10_xml.zip"

    interim = pq.read_table(next((root(tmp_path) / "interim").rglob("*.parquet"))).to_pylist()
    assert len(interim) == 3 * len(weekdays())
    assert {"series_key": "JRXWTFB_N.B", "time_period": "2026-11-11", "obs_status": "ND", "obs_value": "-9999"}.items() <= next(
        x for x in interim if x["series_key"] == "JRXWTFB_N.B" and x["time_period"] == "2026-11-11"
    ).items()

    m = manifest(tmp_path)
    assert m["id"] == "macro_us_dollar_index" and m["fetch_mode"] == "append_only_snapshot"
    assert m["warnings"] == []
    assert m["date_range"] == {"min": START.isoformat(), "max": LAST.isoformat()}
    assert m["extra"]["rows_per_series"] == {k: len(a_days) for k in KEYS}
    assert m["extra"]["release_prepared"] == "2026-11-16T16:47:01"
    assert "release prepared 2026-11-16T16:47:01, federalreserve.gov H.10 release XML" in m["extra"]["citation"]
    log = json.loads(next((root(tmp_path) / "runs").rglob("run-*.json")).read_text("utf-8"))
    assert log["release"]["series_in_file"] == 5
    assert log["checks"]["series"]["JRXWTFB_N.B"]["nd_dates"] == sorted(d.isoformat() for d in ND_DAYS)


def test_upload_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_upload(local: Any, path_in_repo: str, stream: str, **kw: Any) -> str:
        common.check_hf_path(path_in_repo, stream)
        local = Path(local)
        names = sorted(p.relative_to(local).as_posix() for p in local.rglob("*") if p.is_file()) if local.is_dir() else [local.name]
        calls.append((path_in_repo, names))
        return "rev3"

    monkeypatch.setattr(dx, "upload_to_hf", fake_upload)
    assert dx.run(upload=True) == 0
    assert [c[0] for c in calls] == ["macro/us_dollar_index/", "macro/us_dollar_index/manifest.json"]
    assert len(calls[0][1]) == 4 and "processed/us_dollar_index.parquet" in calls[0][1]


# --------------------------------------------------------------------------
# Checks before anything is written
# --------------------------------------------------------------------------


def _unit(api: FakeApi) -> None:
    api.attrs["JRXWTFB_N.B"] = {"UNIT": "Index:_2006_Jan_100"}


def _fx(api: FakeApi) -> None:
    api.attrs["JRXWTFO_N.B"] = {"FX": "OITP"}


def _freq(api: FakeApi) -> None:
    api.attrs["JRXWTFN_N.B"] = {"FREQ": "129"}


def _name(api: FakeApi) -> None:
    api.names["JRXWTFN_N.B"] = "Nominal Advanced Foreign Economies Index"


def _missing(api: FakeApi) -> None:
    api.skip.add("JRXWTFO_N.B")


def _twice(api: FakeApi) -> None:
    api.extra_series.append(series_xml("JRXWTFB_N.B", "BRD", "Nominal Broad Dollar Index", default_obs("JRXWTFB_N.B")))


def _message_id(api: FakeApi) -> None:
    api.message_id = "G19"


def _dtd(api: FakeApi) -> None:
    api.prolog = '<!DOCTYPE m [<!ENTITY x "y">]>'


def _not_zip(api: FakeApi) -> None:
    api.body = b"<html>maintenance</html>"


def _no_data_member(api: FakeApi) -> None:
    api.body = zipfile_bytes({"H10_struct.xml": b"<s/>"})


def zipfile_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in members.items():
            zf.writestr(k, v)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (_unit, "JRXWTFB_N.B: attribute mismatch"),
        (_fx, "JRXWTFO_N.B: attribute mismatch"),
        (_freq, "JRXWTFN_N.B: attribute mismatch"),
        (_name, "JRXWTFN_N.B: series name mismatch"),
        (_missing, "series JRXWTFO_N.B appears 0 time(s)"),
        (_twice, "series JRXWTFB_N.B appears 2 time(s)"),
        (_message_id, "message ID 'G19'"),
        (_dtd, "DTD"),
        (_not_zip, "not a zip file"),
        (_no_data_member, "zip lacks ['H10_data.xml']"),
    ],
)
def test_checks_before_writing(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, change: Any, match: str) -> None:
    change(env)
    assert dx.run(upload=False) == 1
    assert match in caplog.text
    assert not (tmp_path / "data").exists()


def test_http_error(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.body = 404
    assert dx.run(upload=False) == 1
    assert "H.10 zip failed: HTTP 404" in caplog.text


# --------------------------------------------------------------------------
# Validation failures (raw kept, no processed)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs", "match"),
    [
        ({date(2026, 3, 3): ("NA", "1")}, "unknown OBS_STATUS 'NA'"),
        ({date(2026, 3, 3): ("A", "-")}, "non-numeric value '-' with status A"),
        ({date(2026, 11, 24): ("A", "120.0")}, "observation after end_date: 2026-11-24"),
    ],
)
def test_bad_observation_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture, obs: dict[date, tuple[str, str]], match: str) -> None:
    env.obs["JRXWTFN_N.B"].update(obs)
    assert dx.run(upload=False) == 1
    assert match in caplog.text
    assert len(list((root(tmp_path) / "raw").rglob("*.zip"))) == 1
    assert not (root(tmp_path) / dx.PROCESSED_REL).exists()


def test_duplicate_date_fails(tmp_path: Path, env: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    env.extra_series = []
    obs = default_obs("JRXWTFB_N.B")
    xml = series_xml("JRXWTFB_N.B", "BRD", "Nominal Broad Dollar Index", obs)
    xml = xml.replace("</kf:Series>", '<frb:Obs OBS_STATUS="A" OBS_VALUE="120.0" TIME_PERIOD="2026-03-03" /></kf:Series>')
    env.skip.add("JRXWTFB_N.B")
    env.extra_series.append(xml)
    assert dx.run(upload=False) == 1
    assert "JRXWTFB_N.B: duplicate dates ['2026-03-03']" in caplog.text


# --------------------------------------------------------------------------
# Warnings
# --------------------------------------------------------------------------


def test_warnings(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    b, n, o = KEYS
    del env.obs[b][date(2026, 3, 4)]  # missing weekday (and different date sets)
    env.obs[b][date(2026, 3, 7)] = ("A", value(b, date(2026, 3, 6)))  # Saturday
    env.obs[n][date(2026, 4, 1)] = ("ND", "")  # ND with a value other than -9999
    env.obs[n][date(2026, 4, 2)] = ("A", "114.0")  # +3.5% move, not known
    env.obs[o][date(2026, 4, 2)] = ("A", "137.0")  # +5% move, made known below
    env.obs[o][date(2026, 6, 1)] = ("A", "170.0")  # out of range
    for d in weekdays(date(2026, 8, 3), date(2026, 8, 7)):
        env.obs[b][d] = ("ND", "-9999")  # run of 5 ND days
    env.longs[n] = "Something else"
    env.attrs[o] = {"NEW_ATTR": "x"}
    env.prepared = "2026-11-18T16:15:00"  # later than the Tuesday after the latest week
    known = tuple(
        dataclasses.replace(s, known_moves=frozenset({date(2026, 4, 2), date(2026, 4, 3)})) if s.key == o else s
        for s in REAL_SERIES
    )
    monkeypatch.setattr(dx, "SERIES", known)
    monkeypatch.setattr(dx, "BY_KEY", {s.key: s for s in known})
    assert dx.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert f"{b}: weekday(s) missing from the date grid: 1: 2026-03-04" in w
    assert f"{b}: weekend date(s): 1: 2026-03-07" in w
    assert f"{n}: ND value(s) other than -9999: 1: 2026-04-01 ('')" in w
    assert f"{n}: daily move(s) above 2% not in KNOWN_MOVES: 2: 2026-04-02" in w
    assert f"{o}: daily move(s) above 2% not in KNOWN_MOVES: 2: 2026-06-01" in w  # 04-02/03 known
    assert f"{o}: value(s) outside 80-160: 1: 2026-06-01 (170)" in w
    assert f"{b}: run(s) of more than 4 ND days: 1: 2026-08-03 to 2026-08-07 (5 days)" in w
    assert f"{n}: Long Description 'Something else' differs" in w
    assert f"{o}: unexpected series attributes ['NEW_ATTR']" in w
    assert "series have different date sets" in w
    assert "release prepared 2026-11-18T16:15:00 is later than the Tuesday after the latest week (2026-11-17)" in w


def test_nd_per_year_and_stale(tmp_path: Path, env: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dx, "MAX_ND_PER_YEAR", 5)
    monkeypatch.setattr(dx, "_today", lambda: date(2026, 11, 30))
    assert dx.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "JRXWTFB_N.B: year(s) with more than 5 ND days: 1: 2026 (7)" in w
    assert "latest date 2026-11-13 is 17 days before 2026-11-30 (more than 14)" in w


def test_missing_prepared_and_extra_member_warn(tmp_path: Path, env: FakeApi) -> None:
    env.prepared = None
    env.members["readme.txt"] = b"x"
    assert dx.run(upload=False) == 0
    w = warnings_text(tmp_path)
    assert "message header has no Prepared timestamp" in w
    assert "zip members" in w and "readme.txt" in w


def test_revisions(tmp_path: Path, env: FakeApi) -> None:
    assert dx.run(upload=False) == 0
    for sub in ("raw", "interim", "runs"):
        shutil.rmtree(root(tmp_path) / sub)
    env.obs["JRXWTFB_N.B"][date(2026, 2, 2)] = ("A", "121.5000")
    assert dx.run(upload=False) == 0
    old = float(value("JRXWTFB_N.B", date(2026, 2, 2)))
    assert f"revised values since previous run: ['JRXWTFB_N.B 2026-02-02: {old} -> 121.5']" in warnings_text(tmp_path)
