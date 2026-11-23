"""US dollar indexes, nominal broad, AFE and EME (daily) -> dataset
``us_dollar_index`` (stream ``macro``).

Source: Federal Reserve Board H.10 release XML (no key),
``https://www.federalreserve.gov/releases/h10/data/FRB_h10_xml.zip``, one
request per run. Not the Data Download Program (being retired) and not FRED,
whose terms forbid storing its content (Proposed ADR-0006).

The zip (manual download 2026-11-23) holds ``H10_data.xml`` (SDMX-ML compact,
all 87 H.10/G.5 series), ``H10_struct.xml``, ``H10_H10.xsd`` and
``frb_common.xsd``. Only the three daily nominal dollar indexes are processed:

* ``JRXWTFB_N.B`` Nominal Broad Dollar Index (FX ``BRD``);
* ``JRXWTFN_N.B`` Nominal Advanced Foreign Economies Dollar Index (``AFE``);
* ``JRXWTFO_N.B`` Nominal Emerging Market Economies Dollar Index (``EME``).

Each run:

1. fetches the zip and, in memory, before writing anything, aborts unless the
   zip holds ``H10_data.xml`` (other members differing from the expected four
   warn), the message ID is ``H10``, and each of the three series appears
   exactly once with the expected attributes (``CURRENCY``, ``FREQ``, ``FX``,
   ``UNIT``, ``UNIT_MULT``) and "Short Description" annotation;
2. saves the zip unmodified to ``raw/fetch_date=D/<run>_FRB_h10_xml.zip`` (the
   Board overwrites the file at each release and keeps no archive, so these
   snapshots are the only record of past vintages);
3. writes the three series' observations as returned (strings, ``ND`` rows
   included) to ``interim/fetch_date=D/observations-<run>.parquet``;
4. validates and writes ``processed/us_dollar_index.parquet`` (``A`` rows only):

   * fails on: unparseable or duplicate dates, dates after today, an
     ``OBS_STATUS`` other than ``A`` or ``ND``, an ``A`` value that is
     non-numeric, fewer than two ``A`` rows;
   * warns on: weekend dates, a missing weekday in the date grid, an ``ND``
     value other than ``-9999``, more than 16 ``ND`` days in a year, a run
     of more than 4 consecutive ``ND`` days, a value outside 80-160, a daily
     move above 2% that is not in ``KNOWN_MOVES``, a first date different
     from 2006-01-02, series with different date sets, the latest value more
     than 14 days old, a release prepared later than the Tuesday after the
     latest week, unexpected extra series attributes or a different "Long
     Description", and revisions since the previous run (expected when the
     Board revises the trade weights, about once a year);

5. writes a run log, uploads this run's files to HF ``macro/us_dollar_index/``
   in one commit, then writes ``backend/data_manifests/macro_us_dollar_index.json``
   and uploads a copy.

``published_at`` = the Monday after the observation's week + 2 days
(Wednesday) at 23:59:59 PKT, in UTC (basis ``h10_following_monday_plus_2d``).
H.10 is "updated weekly on Mondays at 4:15 p.m." (ET), which is 01:15 or
02:15 PKT on Tuesday; when the Monday is a holiday the release moves to
Tuesday (to confirm), which lands early on Wednesday PKT. See
``jobs/us_macro.py`` for why no DST logic is needed.

Run from ``backend/``::

    python -m jobs.macro_us_dollar_index              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_us_dollar_index --no-upload  # local only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from jobs import easydata as ed
from jobs import us_macro as um
from jobs.common import DATA_DIR, REPO_ROOT, git_info, hf_download, upload_to_hf
from jobs.us_macro import UsMacroError

log = logging.getLogger("jobs.macro_us_dollar_index")

STREAM = "macro"
DATASET = "us_dollar_index"
HF_PREFIX = "macro/us_dollar_index"
LOCAL_ROOT = DATA_DIR / "macro" / "us_dollar_index"
PROCESSED_REL = "processed/us_dollar_index.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

ZIP_URL = "https://www.federalreserve.gov/releases/h10/data/FRB_h10_xml.zip"
MIN_INTERVAL = 2.0
TIMEOUT = 120.0
EXPECTED_MEMBERS = frozenset({"frb_common.xsd", "H10_data.xml", "H10_H10.xsd", "H10_struct.xml"})
DATA_MEMBER = "H10_data.xml"
MAX_MEMBER_BYTES = 200 * 1024 * 1024  # H10_data.xml was 27.4 MB on 2026-11-23

MESSAGE = "{http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message}"
COMMON = "{http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common}"
FRB = "{http://www.federalreserve.gov/structure/compact/common}"
KF = "{http://www.federalreserve.gov/structure/compact/H10_H10}"
EXPECTED_MESSAGE_ID = "H10"

#: Attributes shared by the three series, as in the 2026-11-23 zip. The unit
#: code reads "Index: 1997 Jan = 100" in H10_struct.xml, although the H.10
#: Technical Q&As give January 2006 as the base; it is checked and stored as
#: returned.
COMMON_ATTRS = {"CURRENCY": "NA", "FREQ": "9", "UNIT": "Index:_1997_Jan_100", "UNIT_MULT": "1"}
KNOWN_ATTRS = frozenset(COMMON_ATTRS) | {"FX", "SERIES_NAME"}
STATUS_OK = "A"
STATUS_NO_DATA = "ND"
ND_VALUE = "-9999"
PUBLISHED_AT_BASIS = "h10_following_monday_plus_2d"
RELEASE_LAG_DAYS = 2  # after the Monday that follows the observation's week

EXPECTED_START = date(2006, 1, 2)
MAX_ND_PER_YEAR = 16  # 2006-2026 maximum was 15
MAX_ND_RUN = 4  # 2010-02-08 to 2010-02-11 is the longest run (4)
VALUE_MIN, VALUE_MAX = 80.0, 160.0
MOVE_WARN_PCT = 2.0
STALE_AFTER_DAYS = 14


@dataclass(frozen=True)
class Series:
    key: str  # SERIES_NAME
    fx: str
    name: str  # "Short Description" annotation
    known_moves: frozenset[date] = field(default_factory=frozenset)


def _dates(*isos: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(s) for s in isos)


#: Daily moves above 2% in the 2026-11-23 zip (against the previous A value),
#: reviewed and not warned again.
SERIES: tuple[Series, ...] = (
    Series(
        "JRXWTFB_N.B",
        "BRD",
        "Nominal Broad Dollar Index",
        _dates("2008-10-29", "2008-11-24", "2009-03-19", "2016-03-17"),
    ),
    Series(
        "JRXWTFN_N.B",
        "AFE",
        "Nominal Advanced Foreign Economies Dollar Index",
        _dates(
            "2008-10-29", "2008-11-24", "2008-12-08", "2008-12-11", "2008-12-17", "2008-12-19",
            "2009-01-21", "2009-03-19", "2011-09-22", "2016-03-17", "2016-12-15",
        ),
    ),
    Series(
        "JRXWTFO_N.B",
        "EME",
        "Nominal Emerging Market Economies Dollar Index",
        _dates("2008-10-06", "2008-10-08", "2008-10-09", "2008-10-14", "2016-11-09", "2020-03-12"),
    ),
)
BY_KEY = {s.key: s for s in SERIES}

CITATION = (
    "Board of Governors of the Federal Reserve System, H.10 Foreign Exchange Rates, Nominal "
    "Broad, Advanced Foreign Economies and Emerging Market Economies U.S. Dollar Indexes "
    "(JRXWTFB_N.B, JRXWTFN_N.B, JRXWTFO_N.B), daily, {first} to {last}, release prepared "
    "{prepared}, federalreserve.gov H.10 release XML, retrieved {retrieved}."
)

NOTES = (
    "Federal Reserve Board H.10 release XML (FRB_h10_xml.zip), the three daily nominal dollar "
    "indexes (broad, AFE, EME), long format, one row per series per date with status A; ND (no "
    "data, value -9999) rows are kept in interim and listed in the run log, not in processed. "
    "Unit code as returned (Index:_1997_Jan_100, although the Technical Q&As give a January 2006 "
    "base). Effective 2019-06-24 the Board changed the indexation of the daily broad, AFE and "
    "EME indexes (H.10 Technical Q&As). The Board overwrites the zip at each release and keeps "
    "no archive, and revises past values when trade weights change, so each run's zip is kept "
    "(append_only_snapshot) and revisions against the previous run are warned; history before "
    "our first run is the latest vintage. Not the Data Download Program and not FRED (Proposed "
    "ADR-0006). Public domain unless otherwise indicated; cite the Board. published_at = the "
    "Monday after the observation's week + 2 days 23:59:59 Asia/Karachi in UTC (basis "
    "h10_following_monday_plus_2d)."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("time_period", pa.string()),
        ("obs_status", pa.string()),
        ("obs_value", pa.string()),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),
        ("series_code", pa.string()),  # FX code: BRD, AFE, EME
        ("observation_date", pa.date32()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("unit_mult", pa.string()),
        ("observation_status", pa.string()),
        ("published_at", _TS),
        ("published_at_basis", pa.string()),
        ("release_prepared", pa.string()),  # message:Prepared, as returned
        ("source_endpoint", pa.string()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return um.today_pkt()


@dataclass
class Parsed:
    message_id: str | None
    prepared: str | None
    message_name: str | None
    series_count: int
    attrs: dict[str, dict[str, str]]
    annotations: dict[str, dict[str, str]]
    obs: dict[str, list[tuple[str | None, str | None, str | None]]]  # (TIME_PERIOD, OBS_STATUS, OBS_VALUE)


def parse_data(data: bytes) -> Parsed:
    """The header and the three target series; other series are skipped."""
    what = DATA_MEMBER
    header: dict[str, str | None] = {}
    seen: Counter[str] = Counter()
    attrs: dict[str, dict[str, str]] = {}
    annotations: dict[str, dict[str, str]] = {}
    obs: dict[str, list[tuple[str | None, str | None, str | None]]] = {}
    count = 0
    for _event, elem in um.iterparse_xml(data, what):
        if elem.tag == f"{MESSAGE}Header":
            header = {
                "id": elem.findtext(f"{MESSAGE}ID"),
                "prepared": elem.findtext(f"{MESSAGE}Prepared"),
                "name": elem.findtext(f"{MESSAGE}Name"),
            }
        elif elem.tag == f"{KF}Series":
            count += 1
            name = elem.get("SERIES_NAME", "")
            seen[name] += 1
            if name in BY_KEY:
                attrs[name] = dict(elem.attrib)
                annotations[name] = {
                    (a.findtext(f"{COMMON}AnnotationType") or ""): (a.findtext(f"{COMMON}AnnotationText") or "")
                    for a in elem.iter(f"{COMMON}Annotation")
                }
                obs[name] = [
                    (o.get("TIME_PERIOD"), o.get("OBS_STATUS"), o.get("OBS_VALUE")) for o in elem.iter(f"{FRB}Obs")
                ]
            elem.clear()
    for s in SERIES:
        if seen[s.key] != 1:
            raise UsMacroError(f"{what}: series {s.key} appears {seen[s.key]} time(s); expected once")
    return Parsed(
        message_id=header.get("id"),
        prepared=header.get("prepared"),
        message_name=header.get("name"),
        series_count=count,
        attrs=attrs,
        annotations=annotations,
        obs=obs,
    )


def check_metadata(parsed: Parsed) -> list[str]:
    """Message ID, series attributes and names; mismatches stop the run."""
    warnings: list[str] = []
    if (parsed.message_id or "").strip() != EXPECTED_MESSAGE_ID:
        raise UsMacroError(f"message ID {parsed.message_id!r} != {EXPECTED_MESSAGE_ID!r}")
    for s in SERIES:
        a = parsed.attrs[s.key]
        expected = {**COMMON_ATTRS, "FX": s.fx}
        wrong = {k: a.get(k) for k, v in expected.items() if a.get(k) != v}
        if wrong:
            raise UsMacroError(f"{s.key}: attribute mismatch: got {wrong}, expected {expected}")
        extra = sorted(set(a) - KNOWN_ATTRS)
        if extra:
            warnings.append(f"{s.key}: unexpected series attributes {extra}")
        short = parsed.annotations[s.key].get("Short Description")
        if short != s.name:
            raise UsMacroError(f"{s.key}: series name mismatch: expected {s.name!r}, got {short!r}")
        long = parsed.annotations[s.key].get("Long Description")
        if long != s.name:
            warnings.append(f"{s.key}: Long Description {long!r} differs from {s.name!r}")
    return warnings


def interim_rows(parsed: Parsed, raw_file: str, run_id: str) -> list[dict[str, Any]]:
    return [
        {"series_key": s.key, "time_period": t, "obs_status": st, "obs_value": v, "raw_file": raw_file, "run_id": run_id}
        for s in SERIES
        for t, st, v in parsed.obs[s.key]
    ]


def _missing_weekdays(dates: list[date]) -> list[str]:
    out = []
    for a, b in zip(dates, dates[1:]):
        d = a + timedelta(days=1)
        while d < b:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d += timedelta(days=1)
    return out


def _nd_runs(statuses: list[tuple[date, str]], longer_than: int) -> list[str]:
    runs, current = [], []
    for d, st in statuses + [(date.max, STATUS_OK)]:
        if st == STATUS_NO_DATA:
            current.append(d)
        else:
            if len(current) > longer_than:
                runs.append(f"{current[0]} to {current[-1]} ({len(current)} days)")
            current = []
    return runs


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(
    interim: list[dict[str, Any]],
    parsed: Parsed,
    *,
    end_date: date,
    fetched_at: datetime,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    warnings: list[str] = []
    report: dict[str, Any] = {"series": {}}
    out: list[dict[str, Any]] = []
    grids: dict[str, list[date]] = {}
    latest_ok: list[date] = []
    for s in SERIES:
        rows = [r for r in interim if r["series_key"] == s.key]
        typed: list[tuple[date, str, str | None, dict[str, Any]]] = []
        for r in rows:
            d = um.parse_iso_date(r["time_period"], f"{s.key} TIME_PERIOD")
            st = (r["obs_status"] or "").strip()
            if st not in (STATUS_OK, STATUS_NO_DATA):
                raise UsMacroError(f"{s.key} {d}: unknown OBS_STATUS {r['obs_status']!r}")
            typed.append((d, st, r["obs_value"], r))
        typed.sort(key=lambda t: t[0])
        dates = [t[0] for t in typed]
        dupes = um.duplicates(dates)
        if dupes:
            raise UsMacroError(f"{s.key}: duplicate dates {dupes}")
        if dates and dates[-1] > end_date:
            raise UsMacroError(f"{s.key}: observation after end_date: {dates[-1]}")
        points: list[tuple[date, float]] = []
        nd_dates: list[str] = []
        odd_nd: list[str] = []
        for d, st, raw, _r in typed:
            if st == STATUS_NO_DATA:
                nd_dates.append(d.isoformat())
                if (raw or "").strip() != ND_VALUE:
                    odd_nd.append(f"{d} ({raw!r})")
                continue
            try:
                points.append((d, ed.parse_value(raw)))
            except ed.EasyDataError:
                raise UsMacroError(f"{s.key} {d}: non-numeric value {raw!r} with status A") from None
        if len(points) < 2:
            raise UsMacroError(f"{s.key}: {len(points)} value(s) with status A")
        grids[s.key] = dates
        latest_ok.append(points[-1][0])

        if dates[0] != EXPECTED_START:
            warnings.append(f"{s.key}: first date {dates[0]} differs from expected {EXPECTED_START}")
        um.warn_list(warnings, f"{s.key}: weekend date(s)", um.weekend_dates(dates))
        missing = _missing_weekdays(dates)
        um.warn_list(warnings, f"{s.key}: weekday(s) missing from the date grid", missing)
        um.warn_list(warnings, f"{s.key}: ND value(s) other than {ND_VALUE}", odd_nd)
        nd_per_year = Counter(date.fromisoformat(x).year for x in nd_dates)
        heavy = [f"{y} ({n})" for y, n in sorted(nd_per_year.items()) if n > MAX_ND_PER_YEAR]
        um.warn_list(warnings, f"{s.key}: year(s) with more than {MAX_ND_PER_YEAR} ND days", heavy)
        runs = _nd_runs([(t[0], t[1]) for t in typed], MAX_ND_RUN)
        um.warn_list(warnings, f"{s.key}: run(s) of more than {MAX_ND_RUN} ND days", runs)
        bad = [f"{d} ({v:g})" for d, v in points if not VALUE_MIN <= v <= VALUE_MAX]
        um.warn_list(warnings, f"{s.key}: value(s) outside {VALUE_MIN:g}-{VALUE_MAX:g}", bad)
        moves = um.large_moves(points, MOVE_WARN_PCT, relative=True, known=s.known_moves)
        um.warn_list(warnings, f"{s.key}: daily move(s) above {MOVE_WARN_PCT:g}% not in KNOWN_MOVES", moves)

        report["series"][s.key] = {
            "rows_a": len(points),
            "rows_nd": len(nd_dates),
            "first": dates[0].isoformat(),
            "last": dates[-1].isoformat(),
            "last_a": points[-1][0].isoformat(),
            "nd_dates": nd_dates,
            "nd_per_year": {str(y): n for y, n in sorted(nd_per_year.items())},
            "missing_weekdays": missing,
            "new_large_moves": moves,
            "min": min(v for _, v in points),
            "max": max(v for _, v in points),
            "max_decimals": max(um.decimals(t[2]) for t in typed if t[1] == STATUS_OK),
        }
        attrs = parsed.attrs[s.key]
        values = dict(points)
        for d, st, _raw, r in typed:
            if st != STATUS_OK:
                continue
            out.append(
                {
                    "series_key": s.key,
                    "series_name": s.name,
                    "series_code": s.fx,
                    "observation_date": d,
                    "value": values[d],
                    "unit": attrs["UNIT"],
                    "unit_mult": attrs["UNIT_MULT"],
                    "observation_status": st,
                    "published_at": um.published_at_us(um.following_monday(d), RELEASE_LAG_DAYS),
                    "published_at_basis": PUBLISHED_AT_BASIS,
                    "release_prepared": parsed.prepared,
                    "source_endpoint": ZIP_URL,
                    "fetched_at": fetched_at,
                    "raw_file": r["raw_file"],
                    "run_id": r["run_id"],
                }
            )

    if len({tuple(g) for g in grids.values()}) > 1:
        each = ", ".join(f"{k}: {len(g)} dates, {g[0]} to {g[-1]}" for k, g in grids.items())
        warnings.append(f"series have different date sets ({each})")
    latest = max(latest_ok)
    staleness = um.stale(latest, end_date, STALE_AFTER_DAYS)
    if staleness:
        warnings.append(staleness)
    latest_grid = max(g[-1] for g in grids.values())
    rule_release = um.following_monday(latest_grid)
    report["latest_week_rule_release"] = rule_release.isoformat()
    if parsed.prepared:
        try:
            prepared_day = date.fromisoformat(parsed.prepared.strip()[:10])
        except ValueError:
            warnings.append(f"release prepared {parsed.prepared!r} is not a date")
        else:
            if prepared_day > rule_release + timedelta(days=1):
                warnings.append(
                    f"release prepared {parsed.prepared} is later than the Tuesday after the latest week "
                    f"({rule_release + timedelta(days=1)}): the published_at rule may be too early"
                )
    else:
        warnings.append("message header has no Prepared timestamp")
    return out, warnings, report


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def run(*, upload: bool) -> int:
    try:
        return _run(upload=upload)
    except (UsMacroError, ed.EasyDataError) as exc:
        log.error("%s", exc)
        return 1


def _run(*, upload: bool) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    end_date = _today()
    warnings: list[str] = []

    # 1. Fetch and check in memory before writing anything.
    resp = um.get(ZIP_URL, what="H.10 zip", min_interval=MIN_INTERVAL, timeout=TIMEOUT)
    members, zip_warnings = um.read_zip(
        resp.content,
        expected=EXPECTED_MEMBERS,
        required=(DATA_MEMBER,),
        max_member_bytes=MAX_MEMBER_BYTES,
        what="H.10 zip",
    )
    warnings += zip_warnings
    parsed = parse_data(members[DATA_MEMBER])
    warnings += check_metadata(parsed)
    log.info("H.10 %s prepared %s, %d series in file", parsed.message_id, parsed.prepared, parsed.series_count)

    # 2. Raw, unmodified.
    raw_path = ed.write_new(LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}" / f"{run_id}_FRB_h10_xml.zip", resp.content)
    raw_rel = raw_path.relative_to(LOCAL_ROOT).as_posix()
    run_files = [raw_path]

    # 3. Interim.
    interim = interim_rows(parsed, raw_rel, run_id)
    run_files.append(
        ed.write_table(
            pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA),
            LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet",
        )
    )

    # 4. Validate.
    processed, check_warnings, report = validate(interim, parsed, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

    # Revisions against the previous run.
    previous = ed.load_previous(
        upload=upload,
        hf_path=f"{HF_PREFIX}/{PROCESSED_REL}",
        local_path=LOCAL_ROOT / PROCESSED_REL,
        downloader=hf_download,
        warnings=warnings,
    )
    revisions, revision_warnings = ed.diff_revisions(previous, processed)
    warnings += revision_warnings
    run_files.append(ed.write_table(pa.Table.from_pylist(processed, schema=PROCESSED_SCHEMA), LOCAL_ROOT / PROCESSED_REL))

    # 5. Run log.
    rows_per_series = {s.key: report["series"][s.key]["rows_a"] for s in SERIES}
    obs_dates = [r["observation_date"] for r in processed]
    first, last = min(obs_dates), max(obs_dates)
    citation = CITATION.format(
        first=first.strftime("%d-%b-%Y"),
        last=last.strftime("%d-%b-%Y"),
        prepared=parsed.prepared or "unknown",
        retrieved=fetched_at.strftime("%d-%b-%Y"),
    )
    summary: dict[str, Any] = {
        "run_id": run_id,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "end_date": end_date.isoformat(),
        "git": git_info(),
        "request": {"url": ZIP_URL, "http_status": resp.status, "bytes": len(resp.content), "raw_file": raw_rel},
        "release": {
            "message_id": parsed.message_id,
            "message_name": parsed.message_name,
            "prepared": parsed.prepared,
            "series_in_file": parsed.series_count,
        },
        "series_attributes": parsed.attrs,
        "series_annotations": parsed.annotations,
        "rows": len(processed),
        "rows_per_series": rows_per_series,
        "checks": report,
        "revisions": revisions,
        "published_at_basis": PUBLISHED_AT_BASIS,
        "citation": citation,
        "warnings": warnings,
    }
    run_files.append(
        ed.write_new(
            LOCAL_ROOT / "runs" / f"fetch_date={fetch_date}" / f"run-{run_id}.json",
            (json.dumps(summary, indent=2, default=str) + "\n").encode(),
        )
    )
    for w in warnings:
        log.warning("%s", w)

    # 6. Upload, manifest.
    exit_code = ed.finish_run(
        key="",
        upload=upload,
        stream=STREAM,
        dataset=DATASET,
        local_root=LOCAL_ROOT,
        hf_prefix=HF_PREFIX,
        run_id=run_id,
        run_files=run_files,
        source=(
            f"Federal Reserve Board H.10 release XML <{ZIP_URL}>, {DATA_MEMBER}, series "
            + ", ".join(s.key for s in SERIES)
        ),
        row_count=len(processed),
        date_range=(first, last),
        notes=NOTES,
        extra={
            "rows_per_series": rows_per_series,
            "release_prepared": parsed.prepared,
            "series_attributes": parsed.attrs,
            "published_at_basis": PUBLISHED_AT_BASIS,
            "citation": citation,
            "last_run": {
                k: v
                for k, v in summary.items()
                if k not in ("warnings", "checks", "series_attributes", "series_annotations", "rows_per_series")
            },
        },
        warnings=warnings,
        script=SCRIPT,
        uploader=upload_to_hf,
        job_log=log,
    )
    log.info("%s; %d warning(s); exit code %d", ed.rows_summary(rows_per_series), len(warnings), exit_code)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--no-upload", action="store_true", help="stage under data/macro/us_dollar_index/ only; no HF_TOKEN needed"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
