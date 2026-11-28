"""Pakistan broad money M2 (weekly) -> dataset ``macro_money_supply`` (stream ``macro``).

Fetches the 50 series of the SBP EasyData dataset "Weekly Broad Money M2"
(``TS_GP_BAM_M2_W``, source SBP), keys ``M000010`` to ``M000500`` in steps of
10: M2 and its components (liability side), net foreign and domestic assets,
government borrowing, credit to the private and public sectors, memorandum
items, and three year-on-year growth rates. Weekly, as on Fridays. Unit
"Million PKR" for ``M000010``-``M000470`` and "Percent" for
``M000480``-``M000500``.

Long format: one row per series per date, with ``series_code`` (``M000010``
etc.) from the fixed table ``SERIES``. Expected names are the manual CSV's
"Series name", including its double spaces; only leading dots and spaces are
removed before the exact comparison.

Each run:

1. fetches ``/meta`` for every series and aborts (before fetching data or
   writing files) if a series name, after removing leading dots and spaces,
   differs from the expected name, or the metadata has no start date;
2. fetches ``/data`` from each series' metadata start date to today (Pakistan
   time), always sending ``start_date``;
3. saves every response unmodified to ``raw/fetch_date=D/<run>_<key>_{meta,data}.json``;
4. writes parsed, unvalidated rows (names as returned) to
   ``interim/fetch_date=D/observations-<run>.parquet``. A /data body that is
   not valid JSON fails the run (no repair);
5. validates and writes ``processed/money_supply.parquet``:

   * fails on: wrong series key or name, unparseable or duplicate dates,
     dates after today, fewer than two rows, a value that is empty or
     non-numeric (negative values are allowed: many series are negative by
     construction);
   * warns on: a date that is not a Friday, a gap other than 7 days between
     consecutive dates, series with different date sets or end dates, an
     identity in ``IDENTITIES`` off by more than 5 million PKR, latest date
     more than 30 days old, first observation != metadata start, unexpected
     dataset name, a unit other than the series' expected unit, a status
     other than "Normal", revisions and status-only changes since the
     previous run; status comments are kept as returned and never warned
     about;

6. writes a run log (row count per series, identity check counts), uploads
   this run's files to HF ``macro/money_supply/`` in one commit, then writes
   ``backend/data_manifests/macro_money_supply.json`` and uploads a copy.

``published_at`` = ``observation_date`` (the Friday) + 14 days, 23:59:59
Pakistan time, stored in UTC. Basis ``weekly_release_friday_plus_14d``.
Evidence (Mahrukh, 2026-11-22): SBP Advance Release Calendar 2026 releases
weekly Broad Money (M2) no later than 10 days after the reporting period (as
on the previous Friday), except revisions and public holidays; 14 days leaves
a margin. EasyData returns the latest revised values, not first-release
vintages (M2 is revised, e.g. after SBP's annual accounts in October).

Each run makes 100 requests (50 /meta + 50 /data), about 5 minutes. EasyData
allows 250 per hour and 2,000 per day per key: keep all EasyData runs within
those limits (not in the same hour as ``macro_inflation``).

The API key is handled as in ``macro_policy_rate`` (helpers in
``jobs/easydata.py``): never logged, stored or uploaded.

Run from ``backend/``::

    python -m jobs.macro_money_supply              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_money_supply --no-upload  # local only: stage under data/macro/money_supply/
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
from jobs.easydata import API_BASE, KEY_ENV, PKT, EasyDataError, listed

log = logging.getLogger("jobs.macro_money_supply")

STREAM = "macro"
DATASET = "money_supply"
HF_PREFIX = "macro/money_supply"
LOCAL_ROOT = DATA_DIR / "macro" / "money_supply"
PROCESSED_REL = "processed/money_supply.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Weekly Broad Money M2"
UNIT_PKR = "Million PKR"
UNIT_PERCENT = "Percent"
NORMAL_STATUS = "normal"  # compared case-insensitively, trimmed
PUBLISHED_AT_BASIS = "weekly_release_friday_plus_14d"
RELEASE_LAG = timedelta(days=14)  # after the reporting Friday

FRIDAY = 4  # date.weekday()
WEEK_DAYS = 7  # expected gap between consecutive dates
IDENTITY_TOLERANCE = 5.0  # million PKR; a larger |total - sum(parts)| warns
STALE_AFTER_DAYS = 30  # latest date older than this (vs today) warns

_NAME_PREFIX = re.compile(r"^[.\s]+")
_BASE = "TS_GP_BAM_M2_W"


@dataclass(frozen=True)
class Series:
    key: str
    code: str  # M000010 etc.
    name: str  # expected name after removing leading dots and spaces
    unit: str  # expected unit


# Exact "Series name" from the manual CSV (2026-11-22), in key order M000010,
# M000020, ... Double spaces are kept as in the CSV. M000020 corrected from the
# first --no-upload run (2026-11-22): the API returns "; " where the CSV
# export shows ", " (as for CPI).
_NAMES: tuple[str, ...] = (
    "Currency in Circulation",
    "Deposits with SBP excluding banks; IMF and other Intl. Organizations",  # API uses ";", CSV ","
    "Total Deposits with Scheduled Banks",
    "Demand Deposits",
    "Time Deposits",
    "Residents Foreign Currency Deposits (RFCDs)",
    "Broad Money (liability side)",
    "Net Foreign Assets of the Banking System",
    "Net Foreign Assets of State Bank of Pakistan",
    "Net Foreign Assets of Scheduled Banks",
    "Net Domestic Assets of the Banking System",
    "Net Domestic Assets of State Bank of Pakistan",
    "Net Domestic Assets of Scheduled Banks",
    "Net Government Sector Borrowings",
    "Government Borrowings for Budgetary Support",
    "Government Borrowings from State Bank of Pakistan",
    "Federal Government Borrowings from State Bank of Pakistan",
    "Federal Government Deposits with State Bank of Pakistan",
    "Provincial Government Borrowings From State Bank of Pakistan",
    "Balochistan Government Borrowings From State Bank of Pakistan",
    "Khyber Pakhtunkhwa Government Borrowings From State Bank of Pakistan",
    "Punjab Government Borrowings From State Bank of Pakistan",
    "Sindh Government Borrowings From State Bank of Pakistan",
    "Azad Jammu & Kashmir Government Borrowings From State Bank of Pakistan",
    "Gilgit-Baltistan Government Borrowings From State Bank of Pakistan",
    "Government Borrowings from Scheduled Banks",
    "Federal Government Borrowings from scheduled Banks",
    "Federal Government Deposits with Scheduled Banks",
    "Provincial Government Borrowings from Scheduled Banks",
    "Provincial Government Deposits with Scheduled Banks",
    "Commodity Operations",
    "Net effect of Zakat Fund etc.",
    "Credit to Non-Government Sector",
    "Credit to Private Sector",
    "Credit to Private Sector from Conventional Banking Branches",
    "Credit to Private Sector from Islamic Banks",
    "Credit to Private Sector from Islamic Banking Branches of Conventional Banks",
    "Credit to Public Sectors Enterprises (PSEs)",
    "PSEs Special Account-Debt Repayment with SBP",
    "Credit to Non-Bank Financial Institutions",
    "Other Items (net)",
    "Broad Money (Assets  side)",
    "Accrued profit on holdings of MRTBs/MTBs  by SBP and its subsidiaries (memorandum Item)",
    "Outstanding  amount of MTBs realized value in auctions  (memorandum Item)",
    "Net Government Budgetary Borrowings on cash basis from Banking Sector  (memorandum Item)",
    "Net Government Budgetary Borrowings on cash basis from State Bank of Pakistan  (memorandum Item)",
    "Net Government Budgetary Borrowings on cash basis from Scheduled Banks  (memorandum Item)",
    "Memorandum: Credit to Private Sector - YoY growth",
    "Memorandum: Total Deposits with Banks - YoY growth",
    "Memorandum: Broad Money (M2) - YoY growth",
)
_PERCENT_FROM = 48  # M000480 onwards are growth rates in percent

SERIES: tuple[Series, ...] = tuple(
    Series(
        key=f"{_BASE}.M{n * 10:06d}",
        code=f"M{n * 10:06d}",
        name=name,
        unit=UNIT_PERCENT if n >= _PERCENT_FROM else UNIT_PKR,
    )
    for n, name in enumerate(_NAMES, start=1)
)
_BY_CODE = {s.code: s.key for s in SERIES}

#: total = sum(parts), checked on every date where all exist. Used to check,
#: never to derive. Manual CSV (2026-11-22): M000070 = M000420 differs once by
#: 2.01 (2020-02-21, rounding); M000070 = M000010 + M000020 + M000030 exact.
IDENTITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_BY_CODE["M000070"], (_BY_CODE["M000420"],)),
    (_BY_CODE["M000070"], (_BY_CODE["M000010"], _BY_CODE["M000020"], _BY_CODE["M000030"])),
)

CITATION = (
    "State Bank of Pakistan, ({year}), Weekly data from Weekly Broad Money M2, for "
    "{first} to {last}, Source EasyData, Weekly Broad Money M2, {refresh}"
)

NOTES = (
    "SBP EasyData API, Weekly Broad Money M2. 50 weekly series (as on Fridays), "
    "Million PKR (M000010-M000470) and Percent (M000480-M000500, YoY growth), one "
    "row per series per date (long format; series_code identifies the series). "
    "Many series are negative by construction (deposits, net positions). "
    "Identities checked, not derived: M000070 = M000420 and M000070 = M000010 + "
    "M000020 + M000030 (tolerance 5 million PKR). Re-queryable, but SBP may revise "
    "or remove data without notice, so each run's raw responses are kept "
    "(append_only_snapshot); revisions and status-only changes against the "
    "previous run are logged as warnings. EasyData returns the latest revised "
    "values, not first-release vintages (M2 is revised, e.g. after SBP's annual "
    "accounts in October), so point-in-time use sees revised numbers. Series names "
    "have leading dots and spaces removed in processed (double spaces kept); "
    "interim keeps them as returned. published_at = observation_date + 14 days "
    "23:59:59 Asia/Karachi in UTC (basis weekly_release_friday_plus_14d)."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),  # leading dots and spaces removed
        ("series_code", pa.string()),
        ("observation_date", pa.date32()),  # Friday
        ("value", pa.float64()),  # million PKR, or percent for M000480-M000500
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
    """Remove leading dots and spaces only; inner double spaces are kept."""
    return _NAME_PREFIX.sub("", name.strip())


def published_at_for(observation_date: date) -> datetime:
    """``observation_date`` + 14 days at 23:59:59 Asia/Karachi, as UTC."""
    release = datetime.combine(observation_date + RELEASE_LAG, time(23, 59, 59), tzinfo=PKT)
    return release.astimezone(timezone.utc)


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return datetime.now(PKT).date()


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _decimals(raw: str | None) -> int:
    if _is_blank(raw) or "." not in str(raw):
        return 0
    return len(str(raw).strip().split(".", 1)[1])


# --------------------------------------------------------------------------
# Validation
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
        raw_value = row["observation_value"]
        if _is_blank(raw_value):
            raise EasyDataError(f"{series.key} {day}: empty value (status {row['observation_status']!r})")
        by_series[series.key].append({**row, "_date": day, "_value": ed.parse_value(raw_value)})

    report: dict[str, Any] = {"series": {}}
    out: list[dict[str, Any]] = []
    date_sets: dict[str, set[date]] = {}
    by_date: dict[date, dict[str, float]] = {}
    for series in SERIES:
        rows = sorted(by_series[series.key], key=lambda r: r["_date"])
        meta = metas[series.key]
        if len(rows) < 2:
            raise EasyDataError(
                f"{series.key}: {len(rows)} row(s) returned; expected the full history "
                f"from {meta.start_date} (was start_date ignored?)"
            )
        dates = [r["_date"] for r in rows]
        dupes = sorted({a.isoformat() for a, b in zip(dates, dates[1:]) if a == b})
        if dupes:
            raise EasyDataError(f"{series.key}: duplicate observation dates {dupes}")
        if dates[-1] > end_date:
            raise EasyDataError(f"{series.key}: observation after end_date: {dates[-1]}")
        date_sets[series.key] = set(dates)
        for r in rows:
            by_date.setdefault(r["_date"], {})[series.key] = r["_value"]

        if dates[0] != meta.start_date:
            warnings.append(
                f"{series.key}: first observation {dates[0]} differs from metadata start date {meta.start_date}"
            )
        datasets = {r["dataset_name"] for r in rows if r["dataset_name"]}
        if datasets and datasets != {EXPECTED_DATASET}:
            warnings.append(f"{series.key}: dataset name {sorted(datasets)} != {EXPECTED_DATASET!r}")
        units = {r["unit"] for r in rows}
        if units != {series.unit}:
            warnings.append(f"{series.key}: unit(s) {sorted(map(str, units))} != {series.unit!r}")
        odd = [r for r in rows if (r["observation_status"] or "").strip().casefold() != NORMAL_STATUS]
        odd_status_rows = [r["_date"].isoformat() for r in odd]
        if odd_status_rows:
            warnings.append(
                f"{series.key}: {len(odd_status_rows)} row(s) with status other than 'Normal' "
                f"{sorted({str(r['observation_status']) for r in odd})}: {listed(odd_status_rows)}"
            )
        not_friday = [d.isoformat() for d in dates if d.weekday() != FRIDAY]
        if not_friday:
            warnings.append(f"{series.key}: {len(not_friday)} date(s) not a Friday: {listed(not_friday)}")
        irregular = [
            f"{a} to {b} ({(b - a).days} days)" for a, b in zip(dates, dates[1:]) if (b - a).days != WEEK_DAYS
        ]
        if irregular:
            warnings.append(
                f"{series.key}: {len(irregular)} gap(s) other than {WEEK_DAYS} days: {listed(irregular)}"
            )
        report["series"][series.key] = {
            "rows": len(rows),
            "not_friday": not_friday,
            "irregular_gaps": irregular,
            "negative_values": sum(r["_value"] < 0 for r in rows),
            "non_normal_status": odd_status_rows,
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
                    "series_code": series.code,
                    "observation_date": r["_date"],
                    "value": r["_value"],
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

    all_dates = set().union(*date_sets.values())
    lacking = {k: sorted(d.isoformat() for d in all_dates - ds) for k, ds in date_sets.items() if all_dates - ds}
    if lacking:
        warnings.append(
            f"{len(lacking)} series lack dates present in other series: "
            f"{listed([f'{k} ({len(v)} missing)' for k, v in lacking.items()])}"
        )
    report["dates_missing_vs_other_series"] = lacking

    last_dates = {k: max(ds) for k, ds in date_sets.items()}
    latest = max(last_dates.values())
    behind = sorted(f"{k} ({d})" for k, d in last_dates.items() if d != latest)
    if behind:
        warnings.append(f"{len(behind)} series end before the latest date {latest}: {listed(behind)}")
    report["latest_date"] = latest.isoformat()
    report["series_behind_latest"] = behind
    report["distinct_dates"] = len(all_dates)
    if (end_date - latest).days > STALE_AFTER_DAYS:
        warnings.append(
            f"latest date {latest} is {(end_date - latest).days} days before {end_date} "
            f"(more than {STALE_AFTER_DAYS})"
        )
    identities, identity_warnings = ed.check_identities(by_date, IDENTITIES, IDENTITY_TOLERANCE)
    warnings += identity_warnings
    report["identities"] = identities
    return out, warnings, report


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

    # 4. Interim: parsed, not validated, names as returned. Invalid JSON fails.
    interim = ed.interim_rows(fetched, SERIES, run_id)
    ed.assert_no_secret(json.dumps(interim, default=str), key, "interim rows")
    run_files.append(
        ed.write_table(
            pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA),
            LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet",
        )
    )

    # 5. Validate.
    processed, check_warnings, report = validate(interim, fetched.metas, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

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
    statuses, status_warnings = ed.status_changes(previous, processed)
    warnings += status_warnings
    revisions.update(status_changed=statuses.get("status_changed", 0))

    ed.assert_no_secret(json.dumps(processed, default=str), key, "processed rows")
    run_files.append(ed.write_table(pa.Table.from_pylist(processed, schema=PROCESSED_SCHEMA), LOCAL_ROOT / PROCESSED_REL))

    # 7. Run log.
    per_series: dict[str, dict[str, Any]] = {}
    rows_per_series: dict[str, int] = {}
    for s in SERIES:
        rows = [r for r in processed if r["series_key"] == s.key]
        m = fetched.metas[s.key]
        checks = report["series"][s.key]
        rows_per_series[s.key] = len(rows)
        per_series[s.key] = {
            "name": m.name,
            "meta_name_as_returned": m.raw_name,
            "series_code": s.code,
            "expected_unit": s.unit,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": fetched.data[s.key][1],
            "bytes": len(fetched.data[s.key][0]),
            "rows": len(rows),
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "negative_values": checks["negative_values"],
            "max_decimals": checks["max_decimals"],
            "rows_with_name_prefix": checks["rows_with_name_prefix"],
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
        "rows_per_series": rows_per_series,
        "identities": report["identities"],
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
    exit_code = ed.finish_run(
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
            f"'{EXPECTED_DATASET}' ({_BASE}), series {SERIES[0].key} to {SERIES[-1].key} ({len(SERIES)})"
        ),
        row_count=len(processed),
        date_range=(min(obs_dates), max(obs_dates)),
        notes=NOTES,
        extra={
            "series": per_series,
            "rows_per_series": rows_per_series,
            "latest_date": report["latest_date"],
            "identities": report["identities"],
            "published_at_basis": PUBLISHED_AT_BASIS,
            "citation": citation,
            "last_run": {
                k: v
                for k, v in summary.items()
                if k not in ("warnings", "series", "checks", "status_changes", "rows_per_series", "identities")
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
        "--no-upload",
        action="store_true",
        help="stage under data/macro/money_supply/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
