"""Pakistan gold and FX reserves (monthly) -> dataset ``macro_reserves`` (stream ``macro``).

Fetches the 19 series of the SBP EasyData dataset "Gold and Foreign Exchange
Reserves of Pakistan" (``TS_GP_EXT_PAKRES_M``, source SBP), unit Million USD,
keys ``Z00010`` to ``Z00070``. Values before 1972 are annual (30 June); from
1972 they are month-end.

Long format: one row per series per date, with ``series_code`` (``Z00010``
etc.) from the fixed table ``SERIES``. Data is kept as returned: the two
headline totals ``Z00060`` and ``Z00070`` end at 2024-09 and are **not**
derived afterwards (consumers can rebuild them, see ``IDENTITIES``).

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
5. validates and writes ``processed/reserves.parquet``:

   * fails on: wrong series key or name, unparseable, duplicate or
     non-month-end dates, dates after today, fewer than two rows, a value
     that is empty or non-numeric (negative values are allowed);
   * warns on: a span different from ``Series.start``/``Series.end`` (first
     observation, an ongoing series ending before the latest month, an ended
     series ending earlier than expected or reappearing after its end), a
     skipped month from 1972 on (the annual values before 1972 are not
     checked), a negative value outside ``NEGATIVE_OK``, an identity in
     ``IDENTITIES`` off by more than 1 million USD, latest month more than
     60 days old, first observation != metadata start, unexpected dataset
     name, a unit other than "Million USD", a status other than "Normal",
     revisions and status-only changes since the previous run; status
     comments ("." etc.) are kept as returned and never warned about;

6. writes a run log (row count per series, identity check counts), uploads
   this run's files to HF ``macro/reserves/`` in one commit, then writes
   ``backend/data_manifests/macro_reserves.json`` and uploads a copy.

``published_at`` = the 25th of the month after ``observation_date``, 23:59:59
Pakistan time, stored in UTC. Basis ``bop_release_25th_next_month``. Evidence
(Mahrukh, 2026-11-22): SBP Advance Release Calendar 2026 releases liquid FX
reserves weekly (Thursdays) and "Month end data is also released after
dissemination of Balance of Payments"; Balance of Payments no later than the
16th-20th of the following month; Oct-2026 data was refreshed on EasyData
18-Nov-2026. The same rule is applied to the annual values before 1972.

Each run makes 38 requests (19 /meta + 19 /data), about 2 minutes. EasyData
allows 250 per hour and 2,000 per day per key: keep all EasyData runs within
those limits.

The API key is handled as in ``macro_policy_rate`` (helpers in
``jobs/easydata.py``): never logged, stored or uploaded.

Run from ``backend/``::

    python -m jobs.macro_reserves              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_reserves --no-upload  # local only: stage under data/macro/reserves/
"""

from __future__ import annotations

import argparse
import calendar
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

log = logging.getLogger("jobs.macro_reserves")

STREAM = "macro"
DATASET = "reserves"
HF_PREFIX = "macro/reserves"
LOCAL_ROOT = DATA_DIR / "macro" / "reserves"
PROCESSED_REL = "processed/reserves.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Gold and Foreign Exchange Reserves of Pakistan"
EXPECTED_UNIT = "Million USD"
NORMAL_STATUS = "normal"  # compared case-insensitively, trimmed
PUBLISHED_AT_BASIS = "bop_release_25th_next_month"
RELEASE_DAY = 25  # day of the month after observation_date

MONTHLY_FROM = date(1972, 1, 31)  # skipped months are checked between dates from here on
IDENTITY_TOLERANCE = 1.0  # million USD; a larger |total - sum(parts)| warns
STALE_AFTER_DAYS = 60  # latest month older than this (vs today) warns

_NAME_PREFIX = re.compile(r"^[.\s]+")
_BASE = "TS_GP_EXT_PAKRES_M"


@dataclass(frozen=True)
class Series:
    key: str
    code: str  # Z00010 etc.
    name: str  # expected name after removing leading dots and spaces
    start: date  # expected first observation (manual CSV, 2026-11-22)
    end: date | None  # expected last observation; None = ongoing


def _me(year: int, month: int) -> date:
    """Month end."""
    return date(year, month, calendar.monthrange(year, month)[1])


# (code, exact "Series name" from the manual CSV, expected start, expected end
# or None if ongoing), from the manual CSV downloaded 2026-11-22.
_TABLE: tuple[tuple[str, str, date, date | None], ...] = (
    ("Z00010", "Gold reserves", _me(1948, 6), None),
    ("Z00011", "Special Drawing rights holdings", _me(1971, 6), None),
    ("Z00012", "Cash Foreign Currency with SBP-BSC", _me(2005, 7), None),
    ("Z00013", "Nostro balances of SBP", _me(1948, 6), None),
    ("Z00014", "Net Asian clearing unit balances", _me(2020, 6), None),
    ("Z00015", "IMF reserve position", _me(2020, 6), None),
    (
        "Z00016",
        "Cash reserve requirement and special cash reserve requirement (CRR/SCRR)",
        _me(1999, 6),
        _me(2005, 6),
    ),
    ("Z00017", "FE-25 deposits of banks", _me(1998, 7), None),
    ("Z00018", "Trade Nostro of banks", _me(1971, 6), None),
    ("Z00019", "Place abroad(other than FE-25) of banks", _me(2007, 3), None),
    ("Z00020", "Total SBP Reserves", _me(1948, 6), None),
    ("Z00021", "Trade Finance from FCA", _me(2005, 7), None),
    ("Z00022", "FE-25 Placements of banks in Pakistan", _me(2005, 7), None),
    ("Z00023", "FE-25 utilization-Others", _me(2005, 7), None),
    ("Z00030", "Net Reserves with SBP", _me(2010, 7), None),
    ("Z00040", "Total Banks Reserves", _me(1971, 6), None),
    ("Z00050", "Net Reserves With Banks", _me(2010, 7), None),
    ("Z00060", "Total Liquid FX Reserves", _me(2010, 7), _me(2024, 9)),
    ("Z00070", "Total Reserve Assets", _me(1948, 6), _me(2024, 9)),
)

SERIES: tuple[Series, ...] = tuple(
    Series(key=f"{_BASE}.{code}", code=code, name=name, start=start, end=end)
    for code, name, start, end in _TABLE
)
_BY_CODE = {s.code: s.key for s in SERIES}

#: Series where negative values are real (Z00018 224 months and Z00040 10
#: months in the 2026-11-22 CSV; Z00014 is a net balance). Elsewhere a
#: negative value warns (and is kept).
NEGATIVE_OK = frozenset(_BY_CODE[c] for c in ("Z00014", "Z00018", "Z00040"))

#: total = sum(parts), checked wherever all exist (0 mismatches in the CSV:
#: 171 months and 640 months). Used to check, never to derive.
IDENTITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_BY_CODE["Z00060"], (_BY_CODE["Z00030"], _BY_CODE["Z00050"])),
    (_BY_CODE["Z00070"], (_BY_CODE["Z00010"], _BY_CODE["Z00020"], _BY_CODE["Z00040"])),
)

CITATION = (
    "State Bank of Pakistan, ({year}), Monthly data from Gold and Foreign Exchange "
    "Reserves of Pakistan, for {first} to {last}, Source EasyData, Gold and Foreign "
    "Exchange Reserves of Pakistan, {refresh}"
)

NOTES = (
    "SBP EasyData API, Gold and Foreign Exchange Reserves of Pakistan. 19 series, "
    "Million USD, one row per series per date (long format; series_code identifies "
    "the series). Annual values (30 June) 1948-1970, partial 1971, month-end from "
    "1972; series have different spans (fixed table in the loader). Data kept as "
    "returned: Z00060 (Total Liquid FX Reserves) and Z00070 (Total Reserve Assets) "
    "end at 2024-09 and are not derived; consumers can rebuild them as Z00060 = "
    "Z00030 + Z00050 and Z00070 = Z00010 + Z00020 + Z00040 (checked wherever all "
    "parts exist, tolerance 1). Negative values are real in Z00018 and Z00040. "
    "Re-queryable, but SBP may revise or remove data without notice, so each run's "
    "raw responses are kept (append_only_snapshot); revisions and status-only "
    "changes against the previous run are logged as warnings. EasyData returns the "
    "latest revised values, not first-release vintages. Series names have leading "
    "dots and spaces removed in processed; interim keeps them as returned. "
    "published_at = 25th of the month after observation_date 23:59:59 Asia/Karachi "
    "in UTC (basis bop_release_25th_next_month)."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),  # leading dots and spaces removed
        ("series_code", pa.string()),
        ("observation_date", pa.date32()),  # month end (30 June before 1972)
        ("value", pa.float64()),  # million USD
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
    "Series Display Name"); everything else must match exactly."""
    return _NAME_PREFIX.sub("", name.strip())


def published_at_for(observation_date: date) -> datetime:
    """25th of the following month at 23:59:59 Asia/Karachi, as UTC."""
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


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


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
        if not _is_month_end(day):
            raise EasyDataError(f"{series.key}: observation date {day} is not a month end")
        raw_value = row["observation_value"]
        if _is_blank(raw_value):
            raise EasyDataError(f"{series.key} {day}: empty value (status {row['observation_status']!r})")
        by_series[series.key].append({**row, "_date": day, "_value": ed.parse_value(raw_value)})

    report: dict[str, Any] = {"series": {}}
    out: list[dict[str, Any]] = []
    last_dates: dict[str, date] = {}
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
        last_dates[series.key] = dates[-1]
        for r in rows:
            by_date.setdefault(r["_date"], {})[series.key] = r["_value"]

        if dates[0] != meta.start_date:
            warnings.append(
                f"{series.key}: first observation {dates[0]} differs from metadata start date {meta.start_date}"
            )
        if dates[0] != series.start:
            warnings.append(f"{series.key}: first observation {dates[0]} differs from expected start {series.start}")
        after_end: list[str] = []
        if series.end is not None:
            if dates[-1] < series.end:
                warnings.append(
                    f"{series.key}: ended series' last observation {dates[-1]} is earlier than expected {series.end}"
                )
            after_end = [d.isoformat() for d in dates if d > series.end]
            if after_end:
                warnings.append(
                    f"{series.key}: ended series reappears after its expected end {series.end}: "
                    f"{len(after_end)} row(s): {listed(after_end)}"
                )
        datasets = {r["dataset_name"] for r in rows if r["dataset_name"]}
        if datasets and datasets != {EXPECTED_DATASET}:
            warnings.append(f"{series.key}: dataset name {sorted(datasets)} != {EXPECTED_DATASET!r}")
        units = {r["unit"] for r in rows}
        if units != {EXPECTED_UNIT}:
            warnings.append(f"{series.key}: unit(s) {sorted(map(str, units))} != {EXPECTED_UNIT!r}")
        odd = [r for r in rows if (r["observation_status"] or "").strip().casefold() != NORMAL_STATUS]
        odd_status_rows = [r["_date"].isoformat() for r in odd]
        if odd_status_rows:
            warnings.append(
                f"{series.key}: {len(odd_status_rows)} row(s) with status other than 'Normal' "
                f"{sorted({str(r['observation_status']) for r in odd})}: {listed(odd_status_rows)}"
            )
        skipped = [
            f"{a} to {b}"
            for a, b in zip(dates, dates[1:])
            if a >= MONTHLY_FROM and _month_index(b) - _month_index(a) > 1
        ]
        if skipped:
            warnings.append(f"{series.key}: {len(skipped)} skipped month gap(s) from 1972 on: {listed(skipped)}")
        negatives = [r["_date"].isoformat() for r in rows if r["_value"] < 0]
        unexpected_negatives = [] if series.key in NEGATIVE_OK else negatives
        if unexpected_negatives:
            warnings.append(
                f"{series.key}: {len(unexpected_negatives)} negative value(s) (kept): {listed(unexpected_negatives)}"
            )
        report["series"][series.key] = {
            "rows": len(rows),
            "annual_rows_before_1972": sum(d < MONTHLY_FROM for d in dates),
            "skipped_months": skipped,
            "negative_values": len(negatives),
            "unexpected_negative_values": unexpected_negatives,
            "rows_after_expected_end": after_end,
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

    latest = max(last_dates.values())
    ongoing = {s.key for s in SERIES if s.end is None}
    behind = sorted(f"{k} ({d})" for k, d in last_dates.items() if k in ongoing and d != latest)
    if behind:
        warnings.append(f"{len(behind)} ongoing series end before the latest month {latest}: {listed(behind)}")
    report["latest_month"] = latest.isoformat()
    report["series_behind_latest"] = behind
    if (end_date - latest).days > STALE_AFTER_DAYS:
        warnings.append(
            f"latest month {latest} is {(end_date - latest).days} days before {end_date} "
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
            "expected_start": s.start.isoformat(),
            "expected_end": s.end.isoformat() if s.end else None,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": fetched.data[s.key][1],
            "bytes": len(fetched.data[s.key][0]),
            "rows": len(rows),
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "annual_rows_before_1972": checks["annual_rows_before_1972"],
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
            "latest_month": report["latest_month"],
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
        help="stage under data/macro/reserves/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
