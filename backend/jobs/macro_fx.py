"""PKR exchange rates (daily) -> dataset ``macro_fx`` (stream ``macro``).

Fetches the 23 daily series of the SBP EasyData dataset "Bank Floating Daily
Average Exchange Rates (PKR per National Currency)" (``TS_GP_ES_FADERPKR_M``,
source SBP): PAK Rupees per one unit of 23 currencies, keys ``XRDAVG0010`` to
``XRDAVG0230``.

Long format: one row per series per date, with ``currency_code`` and
``currency_name`` taken from the fixed table ``SERIES`` (never parsed from
names at run time).

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
5. validates and writes ``processed/fx.parquet``:

   * fails on: wrong series key or name, unparseable or duplicate dates,
     dates after today, fewer than two rows, a value that is empty,
     non-numeric or <= 0;
   * warns on: weekend dates other than the four known Saturdays, gaps of
     more than 14 days between dates, a change of more than 10% against the
     series' previous observation, series with different date sets or end
     dates, latest date more than 45 days old, first observation != metadata
     start, unexpected dataset name, a unit other than PKR, a status other
     than "Normal", revisions and status-only changes since the previous run;
     status comments are kept as returned and never warned about;

6. writes a run log (with the row count per series, also logged at the end),
   uploads this run's files to HF ``macro/fx/`` in one commit, then writes
   ``backend/data_manifests/macro_fx.json`` and uploads a copy.

``published_at`` = the 10th of the month after ``observation_date``, 23:59:59
Pakistan time, stored in UTC. Basis ``monthly_release_10th_next_month``.
Evidence (Mahrukh, 2026-11-22): the dataset overview says "This daily data is
compiled and disseminated on monthly basis"; Oct-2026 data was refreshed
05-Nov-2026; SBP publishes the same series as a monthly table ("Daily Average
Banks' Floating Exchange Rates", BFER_Daily.pdf). Backtests therefore see a
value up to about 40 days after its date.

Each run makes 46 requests (23 /meta + 23 /data). EasyData allows 250 per hour
and 2,000 per day per key: keep all EasyData runs within those limits.

The API key is handled as in ``macro_policy_rate`` (helpers in
``jobs/easydata.py``): never logged, stored or uploaded.

Run from ``backend/``::

    python -m jobs.macro_fx              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_fx --no-upload  # local only: stage under data/macro/fx/
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
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
from jobs.easydata import API_BASE, KEY_ENV, PKT, EasyDataError, listed

log = logging.getLogger("jobs.macro_fx")

STREAM = "macro"
DATASET = "fx"
HF_PREFIX = "macro/fx"
LOCAL_ROOT = DATA_DIR / "macro" / "fx"
PROCESSED_REL = "processed/fx.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Bank Floating Daily Average Exchange Rates (PKR per National Currency)"
EXPECTED_UNIT = "PKR"
NORMAL_STATUS = "normal"  # compared case-insensitively, trimmed
PUBLISHED_AT_BASIS = "monthly_release_10th_next_month"
RELEASE_DAY = 10  # day of the month after observation_date

#: Saturdays present in the data (manual CSV, 2026-11-22); other weekend dates warn.
ALLOWED_SATURDAYS = frozenset({date(2019, 8, 17), date(2022, 4, 16), date(2022, 4, 23), date(2022, 4, 30)})
MAX_GAP_DAYS = 14  # a gap between consecutive dates longer than this warns
MOVE_WARN_PCT = 10  # |change vs previous observation| above this percent warns
STALE_AFTER_DAYS = 45  # latest date older than this (vs today) warns

_NAME_PREFIX = re.compile(r"^[.\s]+")


@dataclass(frozen=True)
class Series:
    key: str
    name: str  # expected name after removing leading dots and spaces
    currency_code: str  # ISO 4217
    currency_name: str  # as in the series name


_BASE = "TS_GP_ES_FADERPKR_M"
_NAME_STEM = "Daily Average Exchange rate of PAK Rupees per "

# (currency name as in the series name, ISO code), in key order XRDAVG0010, 0020, ...
_CURRENCIES: tuple[tuple[str, str], ...] = (
    ("Australian Dollar", "AUD"),
    ("Bahraini Dinar", "BHD"),
    ("Canadian Dollar", "CAD"),
    ("Chinese Yuan", "CNY"),
    ("Danish Krone", "DKK"),
    ("Hong Kong Dollar", "HKD"),
    ("Japanese Yen", "JPY"),
    ("Kuwaiti Dinar", "KWD"),
    ("Malaysian Ringgit", "MYR"),
    ("New Zealand Dollar", "NZD"),
    ("Norwegian Krone", "NOK"),
    ("Omani Riyal", "OMR"),
    ("Qatari Riyal", "QAR"),
    ("Singapore Dollar", "SGD"),
    ("Swedish Krona", "SEK"),
    ("Swiss Franc", "CHF"),
    ("Saudi Riyal", "SAR"),
    ("Thai Baht", "THB"),
    ("Turkish Lira", "TRY"),
    ("U.A.E. Dirham", "AED"),
    ("U.K. Pound Sterling", "GBP"),
    ("U.S. Dollar", "USD"),
    ("Euro", "EUR"),
)

SERIES: tuple[Series, ...] = tuple(
    Series(
        key=f"{_BASE}.XRDAVG{n * 10:04d}",
        name=_NAME_STEM + currency_name,
        currency_code=code,
        currency_name=currency_name,
    )
    for n, (currency_name, code) in enumerate(_CURRENCIES, start=1)
)

CITATION = (
    "State Bank of Pakistan, ({year}), Daily data from Bank Floating Daily Average "
    "Exchange Rates (PKR per National Currency), for {first} to {last}, Source "
    "EasyData, Bank Floating Daily Average Exchange Rates (PKR per National "
    "Currency), {refresh}"
)

NOTES = (
    "SBP EasyData API, Bank Floating Daily Average Exchange Rates (PKR per "
    "National Currency). 23 daily series, PAK Rupees per one unit of the "
    "currency, one row per series per date (long format; currency_code "
    "identifies the series). Re-queryable, but SBP may revise or remove data "
    "without notice, so each run's raw responses are kept (append_only_snapshot); "
    "revisions and status-only changes against the previous run are logged as "
    "warnings. Dates are working days, plus four Saturdays (2019-08-17, "
    "2022-04-16, 2022-04-23, 2022-04-30). Status comments are kept as returned "
    "(29 rows carry an unrelated comment about the 1990-91 invasion of Kuwait). "
    "Series names have leading dots and spaces removed in processed; interim "
    "keeps them as returned. published_at = 10th of the month after "
    "observation_date 23:59:59 Asia/Karachi in UTC (basis "
    "monthly_release_10th_next_month): the data is disseminated monthly, so "
    "backtests see a value up to about 40 days after its date. SBP's daily "
    "Weighted Average Customer Exchange Rates are a different series and are "
    "not included."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),  # leading dots and spaces removed
        ("currency_code", pa.string()),
        ("currency_name", pa.string()),
        ("observation_date", pa.date32()),
        ("value", pa.float64()),  # PKR per one unit of the currency
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
    """Remove leading dots and spaces (the indentation EasyData uses in
    "Series Display Name")."""
    return _NAME_PREFIX.sub("", name.strip())


def published_at_for(observation_date: date) -> datetime:
    """10th of the following month at 23:59:59 Asia/Karachi, as UTC."""
    y, m = observation_date.year, observation_date.month + 1
    if m == 13:
        y, m = y + 1, 1
    release = datetime.combine(date(y, m, RELEASE_DAY), time(23, 59, 59), tzinfo=PKT)
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


def _is_large_move(previous: float, current: float) -> bool:
    """|current / previous - 1| > MOVE_WARN_PCT percent, without rounding
    trouble at the boundary (previous > 0 is guaranteed by validation)."""
    return abs(current - previous) * 100 > MOVE_WARN_PCT * previous


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
        value = ed.parse_value(raw_value)
        if value <= 0:
            raise EasyDataError(f"{series.key} {day}: value {raw_value!r} is <= 0")
        by_series[series.key].append({**row, "_date": day, "_value": value})

    report: dict[str, Any] = {"series": {}}
    out: list[dict[str, Any]] = []
    date_sets: dict[str, set[date]] = {}
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

        if dates[0] != meta.start_date:
            warnings.append(
                f"{series.key}: first observation {dates[0]} differs from metadata start date {meta.start_date}"
            )
        datasets = {r["dataset_name"] for r in rows if r["dataset_name"]}
        if datasets and datasets != {EXPECTED_DATASET}:
            warnings.append(f"{series.key}: dataset name {sorted(datasets)} != {EXPECTED_DATASET!r}")
        units = {r["unit"] for r in rows}
        if units != {EXPECTED_UNIT}:
            warnings.append(f"{series.key}: unit(s) {sorted(map(str, units))} != {EXPECTED_UNIT!r}")
        odd = [r for r in rows if (r["observation_status"] or "").strip().casefold() != NORMAL_STATUS]
        odd_status = sorted({str(r["observation_status"]) for r in odd})
        odd_status_rows = [r["_date"].isoformat() for r in odd]
        if odd_status_rows:
            warnings.append(
                f"{series.key}: {len(odd_status_rows)} row(s) with status other than 'Normal' "
                f"{odd_status}: {listed(odd_status_rows)}"
            )

        weekend = [d.isoformat() for d in dates if d.weekday() >= 5 and d not in ALLOWED_SATURDAYS]
        if weekend:
            warnings.append(
                f"{series.key}: {len(weekend)} weekend date(s) other than the known Saturdays: {listed(weekend)}"
            )
        gaps = [(a, b) for a, b in zip(dates, dates[1:])]
        long_gaps = [f"{a} to {b} ({(b - a).days} days)" for a, b in gaps if (b - a).days > MAX_GAP_DAYS]
        if long_gaps:
            warnings.append(
                f"{series.key}: {len(long_gaps)} gap(s) of more than {MAX_GAP_DAYS} days: {listed(long_gaps)}"
            )
        moves = [
            f"{b['_date']} ({(b['_value'] / a['_value'] - 1) * 100:+.2f}%)"
            for a, b in zip(rows, rows[1:])
            if _is_large_move(a["_value"], b["_value"])
        ]
        if moves:
            warnings.append(
                f"{series.key}: {len(moves)} change(s) of more than {MOVE_WARN_PCT}% against the "
                f"previous observation: {listed(moves)}"
            )
        max_gap = max(gaps, key=lambda g: (g[1] - g[0]).days)
        report["series"][series.key] = {
            "rows": len(rows),
            "max_gap_days": (max_gap[1] - max_gap[0]).days,
            "max_gap": f"{max_gap[0]} to {max_gap[1]}",
            "long_gaps": long_gaps,
            "allowed_saturdays": sorted(d.isoformat() for d in dates if d in ALLOWED_SATURDAYS),
            "other_weekend_dates": weekend,
            "large_moves": moves,
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
                    "currency_code": series.currency_code,
                    "currency_name": series.currency_name,
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
    return out, warnings, report


def rows_summary(rows_per_series: Mapping[str, int]) -> str:
    """One line for the final log: the common count, or every series' count."""
    counts = set(rows_per_series.values())
    total = sum(rows_per_series.values())
    if len(counts) == 1:
        return f"rows per series: {counts.pop()} for all {len(rows_per_series)} series ({total} total)"
    each = ", ".join(f"{k.rsplit('.', 1)[-1]}={n}" for k, n in rows_per_series.items())
    return f"rows per series differ ({total} total): {each}"


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

    # 5. Validate and derive.
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
            "currency_code": s.currency_code,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": fetched.data[s.key][1],
            "bytes": len(fetched.data[s.key][0]),
            "rows": len(rows),
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "max_gap_days": checks["max_gap_days"],
            "max_gap": checks["max_gap"],
            "allowed_saturdays": checks["allowed_saturdays"],
            "large_moves": checks["large_moves"],
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
            "published_at_basis": PUBLISHED_AT_BASIS,
            "citation": citation,
            "last_run": {
                k: v
                for k, v in summary.items()
                if k not in ("warnings", "series", "checks", "status_changes", "rows_per_series")
            },
        },
        warnings=warnings,
        script=SCRIPT,
        uploader=upload_to_hf,
        job_log=log,
    )
    log.info("%s; %d warning(s); exit code %d", rows_summary(rows_per_series), len(warnings), exit_code)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="stage under data/macro/fx/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
