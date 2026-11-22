"""SBP T-bill auction yields -> dataset ``macro_tbill_auctions`` (stream ``macro``).

Fetches six As-Needed series from the SBP EasyData API, dataset "Structure of
Interest Rate: T-Bills Auction Result" (``TS_GP_BAM_SIRTBIL_AH``), one row per
auction date:

* ``TB0010`` / ``TB0020`` / ``TB0030``: Cut-off Yield, 3 / 6 / 12 months
* ``TB0040`` / ``TB0050`` / ``TB0060``: Weighted Average Yield, 3 / 6 / 12 months

Long format: one row per series per auction date, with ``tenor_months`` and
``yield_type`` so a wide pivot is one line for consumers.

Each run:

1. fetches ``/meta`` for every series and aborts (before fetching data or
   writing files) if a series name, after removing a leading ". ", differs from
   the expected name, or the metadata has no start date;
2. fetches ``/data`` from each series' metadata start date to today (Pakistan
   time), always sending ``start_date``;
3. saves every response unmodified to ``raw/fetch_date=D/<run>_<key>_{meta,data}.json``;
4. writes parsed, unvalidated rows (names as returned) to
   ``interim/fetch_date=D/observations-<run>.parquet``;
5. validates and writes ``processed/tbill_auctions.parquet``:

   * "Missing value" rows with no value are kept with ``value`` null and
     ``is_missing`` true (counted in the run log, no warning);
   * a value present with status "Missing value" is kept (``is_missing``
     false) with a warning; an empty value with any other status fails;
   * fails on: wrong series key or name, duplicate dates, bad dates,
     non-numeric values, values <= 0 or >= 100, dates after today, fewer than
     two rows;
   * warns on: first observation != metadata start, unexpected dataset name,
     several units, yields outside [1, 35], weighted average above cut-off by
     more than 0.01, cut-off/weighted-average pairs with one side missing,
     latest auction more than 45 days old, revisions since the previous run;
   * gaps of more than 21 days between auction dates are listed in the run log
     and the manifest (``extra.gaps``), not warned about;

6. writes a run log, uploads this run's files to HF ``macro/tbill_auctions/``
   in one commit, then writes ``backend/data_manifests/macro_tbill_auctions.json``
   and uploads a copy.

``published_at`` is the auction date at 23:59:59 Pakistan time, stored in UTC
(basis ``auction_date_end_of_day_pkt``). Evidence (Mahrukh, 2026-11-16): EasyData
dates for 2026 auctions are SBP auction dates (settlement is the next day) and
SBP publishes results the same afternoon. Checked on 2026 auctions only.

The API key is handled as in ``macro_policy_rate`` (helpers in
``jobs/easydata.py``): never logged, stored or uploaded.

Run from ``backend/``::

    python -m jobs.macro_tbill_auctions              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_tbill_auctions --no-upload  # local only: stage under data/macro/tbill_auctions/
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
from jobs.easydata import API_BASE, KEY_ENV, PKT, EasyDataError

log = logging.getLogger("jobs.macro_tbill_auctions")

STREAM = "macro"
DATASET = "tbill_auctions"
HF_PREFIX = "macro/tbill_auctions"
LOCAL_ROOT = DATA_DIR / "macro" / "tbill_auctions"
PROCESSED_REL = "processed/tbill_auctions.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Structure of Interest Rate: T-Bills Auction Result"
PUBLISHED_AT_BASIS = "auction_date_end_of_day_pkt"
MISSING_STATUS = "missing value"  # compared case-insensitively, trimmed

HARD_MIN, HARD_MAX = 0.0, 100.0  # value <= HARD_MIN or >= HARD_MAX fails the run
SANE_MIN, SANE_MAX = 1.0, 35.0  # outside [SANE_MIN, SANE_MAX] warns
WA_ABOVE_CUTOFF_TOLERANCE = 0.01  # percentage points
GAP_REPORT_DAYS = 21  # gaps longer than this go in the gap report
STALE_AFTER_DAYS = 45  # latest auction older than this (vs today) warns
MAX_LISTED = 10  # dates listed in one warning; full lists are in the run log

_NAME_PREFIX = re.compile(r"^\.\s+")


@dataclass(frozen=True)
class Series:
    key: str
    name: str  # expected name after removing a leading ". "
    tenor_months: int
    yield_type: str  # "cutoff" or "weighted_average"


_BASE = "TS_GP_BAM_SIRTBIL_AH"
SERIES: tuple[Series, ...] = (
    Series(f"{_BASE}.TB0010", "Treasury Bills' Cut-off Yield 3-Months", 3, "cutoff"),
    Series(f"{_BASE}.TB0020", "Treasury Bills' Cut-off Yield 6-Months", 6, "cutoff"),
    Series(f"{_BASE}.TB0030", "Treasury Bills' Cut-off Yield 12-Months", 12, "cutoff"),
    Series(f"{_BASE}.TB0040", "Treasury Bills' Weighted Average Yield 3-Months", 3, "weighted_average"),
    Series(f"{_BASE}.TB0050", "Treasury Bills' Weighted Average Yield 6-Months", 6, "weighted_average"),
    Series(f"{_BASE}.TB0060", "Treasury Bills' Weighted Average Yield 12-Months", 12, "weighted_average"),
)

CITATION = (
    "State Bank of Pakistan, (2026), As-Needed data on {name} from Structure of "
    "Interest Rate: T-Bills Auction Result, Source EasyData"
)

NOTES = (
    "SBP EasyData API, T-bill auction results, one row per series per auction "
    "date (long format; tenor_months and yield_type identify the series). "
    "Re-queryable, but SBP may revise or remove data without notice, so each "
    "run's raw responses are kept as the record of what we saw "
    "(append_only_snapshot); revisions against the previous run are logged as "
    "warnings. Rows SBP marks 'Missing value' are kept with value null and "
    "is_missing true. Series names have a leading '. ' removed in processed; "
    "interim keeps them as returned. published_at = auction date 23:59:59 "
    "Asia/Karachi in UTC (basis auction_date_end_of_day_pkt), checked on 2026 "
    "auctions only. Gaps of more than 21 days between auctions are listed in "
    "extra.gaps. 1-month bills are not in this dataset."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),  # leading ". " removed
        ("tenor_months", pa.int8()),
        ("yield_type", pa.string()),
        ("observation_date", pa.date32()),  # auction date
        ("value", pa.float64()),  # percent; null where SBP reports "Missing value"
        ("is_missing", pa.bool_()),
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
    """Remove a leading ". " (seen in EasyData's "Series Display Name")."""
    return _NAME_PREFIX.sub("", name.strip())


def published_at_for(auction_date: date) -> datetime:
    """Auction date at 23:59:59 Asia/Karachi, as UTC."""
    return datetime.combine(auction_date, time(23, 59, 59), tzinfo=PKT).astimezone(timezone.utc)


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return datetime.now(PKT).date()


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _is_missing_status(status: str | None) -> bool:
    return (status or "").strip().casefold() == MISSING_STATUS


def _decimals(raw: str | None) -> int:
    if _is_blank(raw) or "." not in str(raw):
        return 0
    return len(str(raw).strip().split(".", 1)[1])


def _listed(items: list[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED} more, see run log)" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more


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

        missing = [r["_date"].isoformat() for r in rows if r["_missing"]]
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
            "missing_rows": len(missing),
            "first_missing": missing[0] if missing else None,
            "last_missing": missing[-1] if missing else None,
            "missing_dates": missing,
            "value_with_missing_status": value_with_missing_status,
            "out_of_range": odd,
            "max_decimals": max(_decimals(r["observation_value"]) for r in rows),
            "rows_with_name_prefix": sum(bool(_NAME_PREFIX.match((r["series_name"] or "").strip())) for r in rows),
        }
        for r in rows:
            out.append(
                {
                    "series_key": series.key,
                    "series_name": series.name,
                    "tenor_months": series.tenor_months,
                    "yield_type": series.yield_type,
                    "observation_date": r["_date"],
                    "value": r["_value"],
                    "is_missing": r["_missing"],
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

    pair_report, pair_warnings = check_pairs(out)
    warnings += pair_warnings
    report.update(pair_report)

    auction_dates = sorted({r["observation_date"] for r in out})
    report["gaps"] = gap_report(auction_dates)
    report["latest_auction"] = auction_dates[-1].isoformat()
    if (end_date - auction_dates[-1]).days > STALE_AFTER_DAYS:
        warnings.append(
            f"latest auction {auction_dates[-1]} is {(end_date - auction_dates[-1]).days} days "
            f"before {end_date} (more than {STALE_AFTER_DAYS})"
        )
    return out, warnings, report


def check_pairs(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Compare cut-off and weighted-average yields per tenor and date."""
    values: dict[tuple[int, str, date], float | None] = {}
    for r in rows:
        values[(r["tenor_months"], r["yield_type"], r["observation_date"])] = r["value"]
    report: dict[str, Any] = {"wa_above_cutoff": {}, "one_side_missing": {}}
    warnings: list[str] = []
    for tenor in sorted({t for t, _, _ in values}):
        cut = {d: v for (t, y, d), v in values.items() if t == tenor and y == "cutoff"}
        wa = {d: v for (t, y, d), v in values.items() if t == tenor and y == "weighted_average"}
        above = sorted(
            f"{d} (WA {wa[d]} > cut-off {cut[d]})"
            for d in cut.keys() & wa.keys()
            if cut[d] is not None and wa[d] is not None and wa[d] > cut[d] + WA_ABOVE_CUTOFF_TOLERANCE
        )
        one_side = sorted(
            d.isoformat()
            for d in cut.keys() | wa.keys()
            if (cut.get(d) is None) != (wa.get(d) is None)
        )
        report["wa_above_cutoff"][f"{tenor}m"] = above
        report["one_side_missing"][f"{tenor}m"] = one_side
        if above:
            warnings.append(
                f"{tenor}-month: weighted average above cut-off by more than "
                f"{WA_ABOVE_CUTOFF_TOLERANCE} on {len(above)} date(s): {_listed(above)}"
            )
        if one_side:
            warnings.append(
                f"{tenor}-month: cut-off and weighted average not both present on "
                f"{len(one_side)} date(s): {_listed(one_side)}"
            )
    return report, warnings


def gap_report(auction_dates: list[date]) -> list[dict[str, Any]]:
    """Gaps of more than GAP_REPORT_DAYS between consecutive auction dates."""
    return [
        {"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days}
        for a, b in zip(auction_dates, auction_dates[1:])
        if (b - a).days > GAP_REPORT_DAYS
    ]


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

    # 4. Interim: parsed, not validated, names as returned.
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

    # 6. Revisions against the previous processed file.
    previous = ed.load_previous(
        upload=upload,
        hf_path=f"{HF_PREFIX}/{PROCESSED_REL}",
        local_path=LOCAL_ROOT / PROCESSED_REL,
        downloader=hf_download,
        warnings=warnings,
    )
    revisions, revision_warnings = ed.diff_revisions(previous, processed)
    warnings += revision_warnings

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
            "tenor_months": s.tenor_months,
            "yield_type": s.yield_type,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": fetched.data[s.key][1],
            "bytes": len(fetched.data[s.key][0]),
            "rows": len(rows),
            "missing_rows": checks["missing_rows"],
            "first_missing": checks["first_missing"],
            "last_missing": checks["last_missing"],
            "max_decimals": checks["max_decimals"],
            "rows_with_name_prefix": checks["rows_with_name_prefix"],
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "raw_files": fetched.raw_names[s.key],
            "citation": CITATION.format(name=s.name),
        }
        if m.last_refresh is None:
            warnings.append(f"{s.key}: metadata has no last-refresh date")
    summary: dict[str, Any] = {
        "run_id": run_id,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "end_date": end_date.isoformat(),
        "git": git_info(),
        "rows": len(processed),
        "series": per_series,
        "checks": report,
        "revisions": revisions,
        "published_at_basis": PUBLISHED_AT_BASIS,
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
    obs_dates = [r["observation_date"] for r in processed]
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
            f"'{EXPECTED_DATASET}' ({_BASE}), series " + ", ".join(s.key for s in SERIES)
        ),
        row_count=len(processed),
        date_range=(min(obs_dates), max(obs_dates)),
        notes=NOTES,
        extra={
            "series": per_series,
            "gaps": report["gaps"],
            "latest_auction": report["latest_auction"],
            "published_at_basis": PUBLISHED_AT_BASIS,
            "last_run": {k: v for k, v in summary.items() if k not in ("warnings", "series", "checks")},
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
        help="stage under data/macro/tbill_auctions/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
