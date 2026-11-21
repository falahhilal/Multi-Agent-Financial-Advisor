"""Pakistan inflation (2015-16 base) -> dataset ``macro_inflation`` (stream ``macro``).

Fetches the 60 monthly series of the SBP EasyData dataset "Inflation Snapshot
(New Base: 2015-16)" (``TS_GP_PT_CPI_M``; original data from the Pakistan
Bureau of Statistics): 15 measures x 4 bases.

* measures: CPI National / Urban / Rural / Urban Food / Rural Food / Urban
  Non-Food / Rural Non-Food; WPI General / Food / Non-Food; SPI; NFNE core
  Urban / Rural; Trimmed core Urban / Rural;
* bases: Year-on-Year (``P0001``-``P0015``), 12-month moving average
  (``P0016``-``P0030``), period average since July (``P0031``-``P0045``),
  Month-on-Month (``P0046``-``P0060``).

Long format: one row per series per month, with ``index_group``, ``region``,
``component`` and ``measure_base`` taken from the fixed table ``SERIES``
(never parsed from names at run time).

Each run:

1. fetches ``/meta`` for every series and aborts (before fetching data or
   writing files) if a series name, after removing leading dots and spaces,
   differs from the expected name, or the metadata has no start date;
2. fetches ``/data`` from each series' metadata start date to today (Pakistan
   time), always sending ``start_date``;
3. saves every response unmodified to ``raw/fetch_date=D/<run>_<key>_{meta,data}.json``;
4. writes parsed, unvalidated rows (names as returned) to
   ``interim/fetch_date=D/observations-<run>.parquet``. A /data body that is
   not valid JSON only because of the known unescaped ``"MIS"`` in a status
   comment is repaired in memory for parsing (exact bytes only; raw files are
   unchanged); repairs are counted per series in the run log, and repairs
   outside a series' expected leading empty rows warn;
5. validates and writes ``processed/inflation.parquet``:

   * fails on: wrong series key or name, duplicate dates, bad or
     non-month-end dates, non-numeric values, values <= -100 or >= 1000,
     dates after today, fewer than two rows, an empty value whose status is
     not "Missing value";
   * empty values before a series' first value are expected (12MMA, trimmed
     core) and kept as null without warning, up to the month in
     ``Series.leading_empty_until``; a longer leading run warns;
   * warns on: an empty value after the first value, a value present with
     status "Missing value" (kept), values outside [-50, 100], skipped
     months, first observation != metadata start, series ending on different
     months, latest month more than 45 days old, unexpected dataset name,
     several units, revisions and status-only changes since the previous run;

6. writes a run log, uploads this run's files to HF ``macro/inflation/`` in
   one commit, then writes ``backend/data_manifests/macro_inflation.json``
   and uploads a copy.

``published_at`` = the 10th of the month after ``observation_date``, 23:59:59
Pakistan time, stored in UTC, but never earlier than 2019-09-10 23:59:59 PKT
(the 2015-16 base was first published in September 2019 with August 2019 data;
earlier months were back-calculated and are flagged ``is_back_calculated``).
Basis ``conservative_10th_next_month_rebase_2019_09``. Evidence (Mahrukh,
2026-11-20): SBP Advance Release Calendar 2026 lists the Inflation Monitor by
the 3rd-6th of the following month; EasyData updates after the PBS release or
the Inflation Monitor; Oct-2026 data was refreshed 05-Nov-2026.

Each run makes 120 requests (60 /meta + 60 /data). EasyData allows 250 per
hour and 2,000 per day per key: wait at least one hour between runs and do
not run other EasyData loaders in the same hour.

The API key is handled as in ``macro_policy_rate`` (helpers in
``jobs/easydata.py``): never logged, stored or uploaded.

Run from ``backend/``::

    python -m jobs.macro_inflation              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_inflation --no-upload  # local only: stage under data/macro/inflation/
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from jobs import easydata as ed
from jobs.common import (
    DATA_DIR,
    REPO_ROOT,
    get_secret,
    git_info,
    hf_download,
    upload_to_hf,
)
from jobs.easydata import API_BASE, KEY_ENV, PKT, EasyDataError

log = logging.getLogger("jobs.macro_inflation")

STREAM = "macro"
DATASET = "inflation"
HF_PREFIX = "macro/inflation"
LOCAL_ROOT = DATA_DIR / "macro" / "inflation"
PROCESSED_REL = "processed/inflation.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Inflation Snapshot (New Base: 2015-16)"
BASE_YEAR = "2015-16"
PUBLISHED_AT_BASIS = "conservative_10th_next_month_rebase_2019_09"
RELEASE_DAY = 10  # day of the month after observation_date
REBASE_FLOOR = datetime(2019, 9, 10, 23, 59, 59, tzinfo=PKT)  # first 2015-16 base release
BACK_CALCULATED_BEFORE = date(2019, 8, 1)  # months before Aug 2019 were back-calculated
MISSING_STATUS = "missing value"  # compared case-insensitively, trimmed

HARD_MIN, HARD_MAX = -100.0, 1000.0  # value <= HARD_MIN or >= HARD_MAX fails the run
SANE_MIN, SANE_MAX = -50.0, 100.0  # outside [SANE_MIN, SANE_MAX] warns
STALE_AFTER_DAYS = 45  # latest month older than this (vs today) warns
MAX_LISTED = 10  # items listed in one warning; full lists are in the run log

_NAME_PREFIX = re.compile(r"^[.\s]+")


@dataclass(frozen=True)
class Series:
    key: str
    name: str  # expected name after removing leading dots and spaces
    index_group: str  # cpi, core_nfne, core_trimmed, wpi, spi
    region: str  # national, urban, rural
    component: str  # general, food, non_food
    measure_base: str  # yoy, mma12, period_avg_since_july, mom
    leading_empty_until: date | None  # last month that may be empty at the start


_BASE = "TS_GP_PT_CPI_M"

# (label as in the series name, index_group, region, component), in key order
# within each block of 15.
_MEASURES: tuple[tuple[str, str, str, str], ...] = (
    ("National CPI", "cpi", "national", "general"),
    ("Urban CPI", "cpi", "urban", "general"),
    ("Rural CPI", "cpi", "rural", "general"),
    ("Urban Food CPI", "cpi", "urban", "food"),
    ("Rural Food CPI", "cpi", "rural", "food"),
    ("Urban Non-Food CPI", "cpi", "urban", "non_food"),
    ("Rural Non-Food CPI", "cpi", "rural", "non_food"),
    ("WPI", "wpi", "national", "general"),
    ("WPI Food", "wpi", "national", "food"),
    ("WPI Non-Food", "wpi", "national", "non_food"),
    ("SPI", "spi", "national", "general"),
    ("Urban NFNE Core Inflation", "core_nfne", "urban", "general"),
    ("Rural NFNE Core Inflation", "core_nfne", "rural", "general"),
    ("Urban Trimmed Core Inflation", "core_trimmed", "urban", "general"),
    ("Rural Trimmed Core Inflation", "core_trimmed", "rural", "general"),
)

# (basis text in the series name, measure_base), in key-block order.
_BASES: tuple[tuple[str, str], ...] = (
    ("Year-on-Year basis", "yoy"),
    ("12-Months Moving average basis", "mma12"),
    ("Period average Since July basis", "period_avg_since_july"),
    ("Month-on-Month basis", "mom"),
)


def _leading_empty_until(index_group: str, measure_base: str) -> date | None:
    """Expected leading empty months (manual CSV, 2026-11-20): every 12MMA
    series to 2017-05; trimmed YoY and period average to 2017-06; trimmed
    12MMA to 2018-05; trimmed MoM 2016-07 only."""
    if index_group == "core_trimmed":
        return {
            "yoy": date(2017, 6, 30),
            "period_avg_since_july": date(2017, 6, 30),
            "mma12": date(2018, 5, 31),
            "mom": date(2016, 7, 31),
        }[measure_base]
    if measure_base == "mma12":
        return date(2017, 5, 31)
    return None


def _build_series() -> tuple[Series, ...]:
    out = []
    for b, (basis_text, measure_base) in enumerate(_BASES):
        for m, (label, group, region, component) in enumerate(_MEASURES):
            n = b * len(_MEASURES) + m + 1
            out.append(
                Series(
                    key=f"{_BASE}.P{n:04d}1516",
                    name=f"{label}; an Inflation Measure ({basis_text})",
                    index_group=group,
                    region=region,
                    component=component,
                    measure_base=measure_base,
                    leading_empty_until=_leading_empty_until(group, measure_base),
                )
            )
    return tuple(out)


SERIES: tuple[Series, ...] = _build_series()

CITATION = (
    "State Bank of Pakistan, ({year}), Monthly data from Inflation Snapshot "
    "(New Base: 2015-16), for {first} to {last}, Source EasyData, Inflation "
    "Snapshot (New Base: 2015-16), {refresh}"
)

NOTES = (
    "SBP EasyData API, Inflation Snapshot (New Base: 2015-16), original data "
    "from the Pakistan Bureau of Statistics. 60 monthly series (15 measures x "
    "4 bases), one row per series per month (long format; index_group, region, "
    "component and measure_base identify the series). Re-queryable, but SBP "
    "may revise or remove data without notice, so each run's raw responses are "
    "kept (append_only_snapshot); revisions and status-only changes against the "
    "previous run are logged as warnings. Empty values are kept with value null "
    "and is_missing true; leading empty months in 12MMA and trimmed-core series "
    "are expected. Series names have leading dots and spaces removed in "
    "processed; interim keeps them as returned. published_at = 10th of the "
    "month after observation_date 23:59:59 Asia/Karachi in UTC, floored at "
    "2019-09-10 23:59:59 PKT (basis conservative_10th_next_month_rebase_2019_09). "
    "Months before 2019-08 were back-calculated at the 2019 rebase "
    "(is_back_calculated). Older bases (2007-08 and earlier) are not included."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),  # leading dots and spaces removed
        ("index_group", pa.string()),
        ("region", pa.string()),
        ("component", pa.string()),
        ("measure_base", pa.string()),
        ("base_year", pa.string()),
        ("observation_date", pa.date32()),  # month end
        ("value", pa.float64()),  # percent; null where empty
        ("is_missing", pa.bool_()),
        ("is_back_calculated", pa.bool_()),
        ("unit", pa.string()),
        ("observation_status", pa.string()),
        ("status_comments", pa.string()),
        ("dataset_name", pa.string()),
        ("published_at", _TS),
        ("published_at_basis", pa.string()),
        ("portal_last_refresh_date", pa.date32()),
        ("source_endpoint", pa.string()),  # no key
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)


def clean_name(name: str) -> str:
    """Remove leading dots and spaces (". " or "............. ", the indentation
    seen in EasyData's "Series Display Name")."""
    return _NAME_PREFIX.sub("", name.strip())


def published_at_for(observation_date: date) -> datetime:
    """10th of the following month at 23:59:59 Asia/Karachi, never before the
    2019-09-10 rebase release, as UTC."""
    y, m = observation_date.year, observation_date.month + 1
    if m == 13:
        y, m = y + 1, 1
    release = datetime.combine(date(y, m, RELEASE_DAY), time(23, 59, 59), tzinfo=PKT)
    return max(release, REBASE_FLOOR).astimezone(timezone.utc)


def is_back_calculated(observation_date: date) -> bool:
    return observation_date < BACK_CALCULATED_BEFORE


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return datetime.now(PKT).date()


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _is_missing_status(status: str | None) -> bool:
    return (status or "").strip().casefold() == MISSING_STATUS


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def _decimals(raw: str | None) -> int:
    if _is_blank(raw) or "." not in str(raw):
        return 0
    return len(str(raw).strip().split(".", 1)[1])


def _listed(items: list[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED} more, see run log)" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more


# --------------------------------------------------------------------------
# Known EasyData JSON defect
# --------------------------------------------------------------------------

# EasyData /data (seen 2026-11-21) returns the status comment of leading empty
# rows in 12MMA and trimmed-core series with unescaped quotes:
#   "The observation status is missing "MIS" due to trimmed inflation ..."
# which makes the whole body invalid JSON. Only these exact bytes are repaired,
# only when the body does not parse, and only in memory: raw files keep the
# response as returned.
_MIS_UNESCAPED = b'missing "MIS" due'
_MIS_ESCAPED = b'missing \\"MIS\\" due'


def _repair_body(body: bytes) -> tuple[bytes, int]:
    """Body to parse and the number of repairs made. A body that is valid JSON
    is returned unchanged. If the repaired body is still invalid, parsing
    fails later as before ("data response is not JSON")."""
    try:
        json.loads(body)
        return body, 0
    except ValueError:
        pass
    n = body.count(_MIS_UNESCAPED)
    return (body.replace(_MIS_UNESCAPED, _MIS_ESCAPED), n) if n else (body, 0)


def check_repairs(repairs: Mapping[str, int], report: Mapping[str, Any]) -> list[str]:
    """Repairs are expected only on a series' expected leading empty rows: warn
    for a series with no expected leading empties, or a count that differs from
    its leading empty rows."""
    unexpected = []
    for s in SERIES:
        n = repairs.get(s.key, 0)
        if not n:
            continue
        leading = report["series"][s.key]["leading_missing"]
        if s.leading_empty_until is None or n != leading:
            unexpected.append(f"{s.key} ({n} repaired, {leading} leading empty)")
    if not unexpected:
        return []
    return [
        f"unexpected JSON repairs (unescaped \"MIS\" in status comment) in {len(unexpected)} "
        f"series: {_listed(unexpected)}"
    ]


# --------------------------------------------------------------------------
# Validation and derived columns
# --------------------------------------------------------------------------


def validate(
    interim: list[dict[str, Any]],
    metas: Mapping[str, ed.SeriesMeta[Series]],
    *,
    end_date: date,
    fetched_at: datetime,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Typed, checked rows for ``processed/``, warnings, and a report for the
    run log. Raises ``EasyDataError`` on data that must not be stored."""
    warnings: list[str] = []
    by_series: dict[str, list[dict[str, Any]]] = {s.key: [] for s in SERIES}
    for row in interim:
        series = metas[row["requested_series_key"]].series
        if (row["series_key"] or "").strip() != series.key:
            raise EasyDataError(f"{series.key}: response contains a row for series {row['series_key']!r}")
        if clean_name(row["series_name"] or "") != series.name:
            raise EasyDataError(f"{series.key}: row series name {row['series_name']!r} != {series.name!r}")
        day = ed.parse_date(row["observation_date"])
        if not _is_month_end(day):
            raise EasyDataError(f"{series.key}: observation date {day} is not a month end")
        raw_value, status = row["observation_value"], row["observation_status"]
        if _is_blank(raw_value):
            if not _is_missing_status(status):
                raise EasyDataError(
                    f"{series.key} {day}: empty value with status {status!r} (expected 'Missing value')"
                )
            value, is_missing = None, True
        else:
            value, is_missing = ed.parse_value(raw_value), False
            if not HARD_MIN < value < HARD_MAX:
                raise EasyDataError(f"{series.key} {day}: value {raw_value!r} outside ({HARD_MIN}, {HARD_MAX})")
        by_series[series.key].append({**row, "_date": day, "_value": value, "_missing": is_missing})

    report: dict[str, Any] = {"series": {}}
    out: list[dict[str, Any]] = []
    last_months: dict[str, date] = {}
    for series in SERIES:
        rows = sorted(by_series[series.key], key=lambda r: r["_date"])
        meta = metas[series.key]
        if len(rows) < 2:
            raise EasyDataError(
                f"{series.key}: {len(rows)} row(s) returned; expected the full history "
                f"from {meta.start_date} (was start_date ignored?)"
            )
        dates = [r["_date"] for r in rows]
        dupes = sorted({d.isoformat() for d in dates if dates.count(d) > 1})
        if dupes:
            raise EasyDataError(f"{series.key}: duplicate observation dates {dupes}")
        if dates[-1] > end_date:
            raise EasyDataError(f"{series.key}: observation after end_date: {dates[-1]}")
        last_months[series.key] = dates[-1]
        if dates[0] != meta.start_date:
            warnings.append(
                f"{series.key}: first observation {dates[0]} differs from metadata start date {meta.start_date}"
            )
        datasets = {r["dataset_name"] for r in rows if r["dataset_name"]}
        if datasets and datasets != {EXPECTED_DATASET}:
            warnings.append(f"{series.key}: dataset name {sorted(datasets)} != {EXPECTED_DATASET!r}")
        units = {r["unit"] for r in rows}
        if len(units) > 1:
            warnings.append(f"{series.key}: several units {sorted(map(str, units))}")

        skipped = [
            f"{a} to {b}"
            for a, b in zip(dates, dates[1:])
            if _month_index(b) - _month_index(a) > 1
        ]
        if skipped:
            warnings.append(f"{series.key}: {len(skipped)} skipped month gap(s): {_listed(skipped)}")

        first_value = next((i for i, r in enumerate(rows) if not r["_missing"]), len(rows))
        leading = [r["_date"].isoformat() for r in rows[:first_value]]
        later_empty = [r["_date"].isoformat() for r in rows[first_value:] if r["_missing"]]
        allowed = series.leading_empty_until
        unexpected_leading = [d for d in leading if allowed is None or d > allowed.isoformat()]
        if unexpected_leading:
            warnings.append(
                f"{series.key}: {len(unexpected_leading)} leading empty value(s) beyond the expected "
                f"{allowed or 'none'}: {_listed(unexpected_leading)}"
            )
        if later_empty:
            warnings.append(
                f"{series.key}: {len(later_empty)} empty value(s) after the first value "
                f"(kept as null): {_listed(later_empty)}"
            )
        value_with_missing_status = [
            r["_date"].isoformat() for r in rows if not r["_missing"] and _is_missing_status(r["observation_status"])
        ]
        if value_with_missing_status:
            warnings.append(
                f"{series.key}: {len(value_with_missing_status)} row(s) have a value but status "
                f"'Missing value' (value kept): {_listed(value_with_missing_status)}"
            )
        odd = [
            f"{r['_date']} ({r['observation_value'].strip()})"
            for r in rows
            if r["_value"] is not None and not SANE_MIN <= r["_value"] <= SANE_MAX
        ]
        if odd:
            warnings.append(
                f"{series.key}: {len(odd)} value(s) outside [{SANE_MIN}, {SANE_MAX}]: {_listed(odd)}"
            )
        report["series"][series.key] = {
            "rows": len(rows),
            "missing_rows": len(leading) + len(later_empty),
            "leading_missing": len(leading),
            "leading_missing_last": leading[-1] if leading else None,
            "leading_missing_allowed_until": allowed.isoformat() if allowed else None,
            "missing_after_first_value": later_empty,
            "value_with_missing_status": value_with_missing_status,
            "out_of_range": odd,
            "skipped_months": skipped,
            "back_calculated_rows": sum(is_back_calculated(d) for d in dates),
            "max_decimals": max(_decimals(r["observation_value"]) for r in rows),
            "rows_with_name_prefix": sum(
                clean_name(r["series_name"] or "") != (r["series_name"] or "").strip() for r in rows
            ),
        }
        for r in rows:
            out.append(
                {
                    "series_key": series.key,
                    "series_name": series.name,
                    "index_group": series.index_group,
                    "region": series.region,
                    "component": series.component,
                    "measure_base": series.measure_base,
                    "base_year": BASE_YEAR,
                    "observation_date": r["_date"],
                    "value": r["_value"],
                    "is_missing": r["_missing"],
                    "is_back_calculated": is_back_calculated(r["_date"]),
                    "unit": r["unit"],
                    "observation_status": r["observation_status"],
                    "status_comments": r["status_comments"],
                    "dataset_name": r["dataset_name"],
                    "published_at": published_at_for(r["_date"]),
                    "published_at_basis": PUBLISHED_AT_BASIS,
                    "portal_last_refresh_date": meta.last_refresh,
                    "source_endpoint": f"{API_BASE}/series/{series.key}/data",
                    "fetched_at": fetched_at,
                    "raw_file": r["raw_file"],
                    "run_id": r["run_id"],
                }
            )

    latest = max(last_months.values())
    behind = sorted(f"{k} ({d})" for k, d in last_months.items() if d != latest)
    if behind:
        warnings.append(f"{len(behind)} series end before the latest month {latest}: {_listed(behind)}")
    report["latest_month"] = latest.isoformat()
    report["series_behind_latest"] = behind
    if (end_date - latest).days > STALE_AFTER_DAYS:
        warnings.append(
            f"latest month {latest} is {(end_date - latest).days} days before {end_date} "
            f"(more than {STALE_AFTER_DAYS})"
        )
    return out, warnings, report


def status_changes(
    previous: pa.Table | None, current: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Rows whose value is unchanged since the previous run but whose status or
    status comment changed (value changes are reported by ``diff_revisions``)."""
    if previous is None:
        return {"previous": None}, []
    cols = ["series_key", "observation_date", "value", "observation_status", "status_comments"]
    prev = {
        (r["series_key"], r["observation_date"].isoformat()): r
        for r in previous.select(cols).to_pylist()
    }
    changed = []
    for r in current:
        k = (r["series_key"], r["observation_date"].isoformat())
        p = prev.get(k)
        if p is None or p["value"] != r["value"]:
            continue
        if (p["observation_status"], p["status_comments"]) != (r["observation_status"], r["status_comments"]):
            changed.append(
                f"{k[0]} {k[1]}: status {p['observation_status']!r} -> {r['observation_status']!r}, "
                f"comment {p['status_comments']!r} -> {r['status_comments']!r}"
            )
    changed.sort()
    warnings = []
    if changed:
        warnings.append(
            f"status or status comment changed with value unchanged since previous run "
            f"({len(changed)} row(s)): {_listed(changed)}"
        )
    return {"status_changed": len(changed), "status_changes": changed}, warnings


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def run(*, upload: bool) -> int:
    key = get_secret(KEY_ENV)  # raises naming the variable; no request made yet
    with ed.key_redaction(key, log):
        try:
            return _run(key, upload=upload)
        except EasyDataError as exc:
            log.error("%s", ed.redact(str(exc), key))
            return 1


def _run(key: str, *, upload: bool) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    end_date = _today()
    warnings: list[str] = []

    # 1-3. Metadata (all checked first), data, raw files.
    fetched = ed.fetch_series(
        SERIES,
        key,
        local_root=LOCAL_ROOT,
        raw_dir=LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}",
        run_id=run_id,
        end_date=end_date,
        job_log=log,
        clean_name=clean_name,
    )
    run_files = list(fetched.files)

    # 4. Interim: parsed, not validated, names as returned. Bodies with the
    # known unescaped-"MIS" defect are repaired in memory for parsing only.
    repairs: dict[str, int] = {}
    to_parse: dict[str, tuple[bytes, int]] = {}
    for s in SERIES:
        body, status = fetched.data[s.key]
        fixed, repairs[s.key] = _repair_body(body)
        to_parse[s.key] = (fixed, status)
    interim = ed.interim_rows(replace(fetched, data=to_parse), SERIES, run_id)
    ed.assert_no_secret(json.dumps(interim, default=str), key, "interim rows")
    run_files.append(
        ed.write_table(
            pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA),
            LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet",
        )
    )

    # 5. Validate and derive.
    processed, check_warnings, report = validate(interim, fetched.metas, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings
    warnings += check_repairs(repairs, report)

    # 6. Revisions and status-only changes against the previous processed file.
    previous = ed.load_previous(
        upload=upload,
        hf_path=f"{HF_PREFIX}/{PROCESSED_REL}",
        local_path=LOCAL_ROOT / PROCESSED_REL,
        downloader=hf_download,
        warnings=warnings,
    )
    revisions, revision_warnings = ed.diff_revisions(previous, processed)
    warnings += revision_warnings
    statuses, status_warnings = status_changes(previous, processed)
    warnings += status_warnings
    revisions.update(status_changed=statuses.get("status_changed", 0))

    ed.assert_no_secret(json.dumps(processed, default=str), key, "processed rows")
    run_files.append(ed.write_table(pa.Table.from_pylist(processed, schema=PROCESSED_SCHEMA), LOCAL_ROOT / PROCESSED_REL))

    # 7. Run log.
    per_series: dict[str, dict[str, Any]] = {}
    for s in SERIES:
        rows = [r for r in processed if r["series_key"] == s.key]
        m = fetched.metas[s.key]
        checks = report["series"][s.key]
        per_series[s.key] = {
            "name": m.name,
            "meta_name_as_returned": m.raw_name,
            "index_group": s.index_group,
            "region": s.region,
            "component": s.component,
            "measure_base": s.measure_base,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": fetched.data[s.key][1],
            "bytes": len(fetched.data[s.key][0]),
            "rows": len(rows),
            "missing_rows": checks["missing_rows"],
            "leading_missing": checks["leading_missing"],
            "leading_missing_last": checks["leading_missing_last"],
            "back_calculated_rows": checks["back_calculated_rows"],
            "max_decimals": checks["max_decimals"],
            "rows_with_name_prefix": checks["rows_with_name_prefix"],
            "json_repairs": repairs[s.key],
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "raw_files": fetched.raw_names[s.key],
        }
        if m.last_refresh is None:
            warnings.append(f"{s.key}: metadata has no last-refresh date")

    obs_dates = [r["observation_date"] for r in processed]
    # Year and refresh date from the newest /meta Last Refresh Date, as in
    # EasyData's own citation; never from the run date.
    refreshes = [m.last_refresh for m in fetched.metas.values() if m.last_refresh is not None]
    latest_refresh = max(refreshes) if refreshes else None
    citation = CITATION.format(
        year=latest_refresh.year if latest_refresh else "unknown",
        first=min(obs_dates).strftime("%d-%b-%Y"),
        last=max(obs_dates).strftime("%d-%b-%Y"),
        refresh=latest_refresh.strftime("%d-%b-%Y") if latest_refresh else "unknown",
    )
    summary: dict[str, Any] = {
        "run_id": run_id,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "end_date": end_date.isoformat(),
        "git": git_info(),
        "rows": len(processed),
        "back_calculated_rows": sum(r["is_back_calculated"] for r in processed),
        "json_repairs": {
            "series": sum(1 for n in repairs.values() if n),
            "total": sum(repairs.values()),
        },
        "series": per_series,
        "checks": report,
        "revisions": revisions,
        "status_changes": statuses,
        "published_at_basis": PUBLISHED_AT_BASIS,
        "citation": citation,
        "warnings": warnings,
    }
    runlog_text = json.dumps(summary, indent=2) + "\n"
    ed.assert_no_secret(runlog_text, key, "run log")
    run_files.append(
        ed.write_new(LOCAL_ROOT / "runs" / f"fetch_date={fetch_date}" / f"run-{run_id}.json", runlog_text.encode())
    )
    for w in warnings:
        log.warning("%s", w)

    # 8-9. Scan, upload, manifest.
    return ed.finish_run(
        key=key,
        upload=upload,
        stream=STREAM,
        dataset=DATASET,
        local_root=LOCAL_ROOT,
        hf_prefix=HF_PREFIX,
        run_id=run_id,
        run_files=run_files,
        source=(
            "SBP EasyData API <" + API_BASE + "/series/<series_key>/{meta,data}>, dataset "
            f"'{EXPECTED_DATASET}' ({_BASE}), original data Pakistan Bureau of Statistics, "
            f"series {SERIES[0].key} to {SERIES[-1].key} ({len(SERIES)})"
        ),
        row_count=len(processed),
        date_range=(min(obs_dates), max(obs_dates)),
        notes=NOTES,
        extra={
            "series": per_series,
            "latest_month": report["latest_month"],
            "published_at_basis": PUBLISHED_AT_BASIS,
            "citation": citation,
            "last_run": {
                k: v for k, v in summary.items() if k not in ("warnings", "series", "checks", "status_changes")
            },
        },
        warnings=warnings,
        script=SCRIPT,
        uploader=upload_to_hf,
        job_log=log,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="stage under data/macro/inflation/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
