"""SBP policy-rate series -> dataset ``macro_policy_rate`` (stream ``macro``).

Fetches three As-Needed series from the SBP EasyData API (dataset "Structure
of Interest Rate: State Bank of Pakistan Policy Rates"), a row per rate change:

* ``TS_GP_IR_SIRPR_AH.SBPOL0030`` Policy (Target) Rate (from 2015-05-25)
* ``TS_GP_IR_SIRPR_AH.SBPOL0020`` Repo Rate (corridor floor)
* ``TS_GP_IR_SIRPR_AH.SBPOL0010`` Reverse Repo Rate (corridor ceiling; the
  policy rate 2009-08-17 to 2015-05-24, per the dataset description)

The three series are kept separate (long format). ``is_policy_rate`` flags,
per row, whether that value was the official policy rate for at least part of
the time it was in effect; nothing is merged into one column.

Each run:

1. fetches ``/meta`` for every series and aborts (before fetching any data or
   writing any file) if a series name differs from the expected name or the
   metadata has no start date;
2. fetches ``/data`` from each series' metadata start date to today (Pakistan
   time). ``start_date`` is always sent: without it the API returns only the
   latest observation;
3. saves every response unmodified to ``raw/fetch_date=D/<run>_<key>_{meta,data}.json``;
4. writes the parsed, unvalidated rows to ``interim/fetch_date=D/observations-<run>.parquet``;
5. validates them (series key and name per row, dates, numeric values, no
   duplicates, more than one row per series) and writes
   ``processed/policy_rate.parquet``;
6. compares with the previous processed file and warns about changed, removed
   or back-filled observations (SBP may revise data without notice);
7. writes a run log ``runs/fetch_date=D/run-<run>.json``, uploads this run's
   files to HF ``macro/policy_rate/`` in one commit, then writes
   ``backend/data_manifests/macro_policy_rate.json`` and uploads a copy.

``published_at`` is null with ``published_at_basis = "unknown"``: the API has
no publication date (team decision 2026-11-12). Announcement dates come later
from a separate MPC calendar dataset.

The API key (``EASYDATA_API_KEY``) travels in the query string, so this job
never stores ``resp.url``, re-raises HTTP errors with the key removed, filters
the key out of log records, and scans every output file and warning for the
key before anything is written to the manifest or uploaded. The EasyData
helpers for this live in ``jobs/easydata.py``, shared with
``macro_tbill_auctions``.

Run from ``backend/``::

    python -m jobs.macro_policy_rate              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_policy_rate --no-upload  # local only: stage under data/macro/policy_rate/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone
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

# Names kept on this module for callers and tests written before the helpers
# moved to jobs/easydata.py.
from jobs.easydata import (  # noqa: F401
    API_BASE,
    KEY_ENV,
    PKT,
    REDACTED,
    contains_secret,
    diff_revisions,
    parse_date,
    parse_meta,
    parse_value,
    records,
    redact,
)
from jobs.easydata import EasyDataError as PolicyRateError
from jobs.easydata import assert_no_secret
from jobs.easydata import get as _get
from jobs.easydata import secret_forms as _secret_forms  # noqa: F401
from jobs.easydata import write_new as _write_new
from jobs.easydata import write_table as _write_table

log = logging.getLogger("jobs.macro_policy_rate")

STREAM = "macro"
DATASET = "policy_rate"
HF_PREFIX = "macro/policy_rate"
LOCAL_ROOT = DATA_DIR / "macro" / "policy_rate"
PROCESSED_REL = "processed/policy_rate.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

EXPECTED_DATASET = "Structure of Interest Rate: State Bank of Pakistan Policy Rates"
PUBLISHED_AT_BASIS = "unknown"


@dataclass(frozen=True)
class Series:
    key: str
    name: str  # must equal the name returned by /meta, exactly
    role: str


SERIES: tuple[Series, ...] = (
    Series(
        "TS_GP_IR_SIRPR_AH.SBPOL0030",
        "State Bank of Pakistan's Policy (Target) Rate",
        "policy_target",
    ),
    Series(
        "TS_GP_IR_SIRPR_AH.SBPOL0020",
        "State Bank of Pakistan's Repo Rate",
        "repo_floor",
    ),
    Series(
        "TS_GP_IR_SIRPR_AH.SBPOL0010",
        "State Bank of Pakistan's Reverse Repo Rate",
        "reverse_repo_ceiling",
    ),
)

#: Days on which each series was the official SBP policy rate (inclusive;
#: ``None`` = open-ended). From the dataset description: the O/N Reverse Repo
#: was the policy rate 2009-08-17 to 2015-05-24; the Target Rate from 2015-05-25.
POLICY_WINDOWS: dict[str, tuple[date, date | None]] = {
    "TS_GP_IR_SIRPR_AH.SBPOL0010": (date(2009, 8, 17), date(2015, 5, 24)),
    "TS_GP_IR_SIRPR_AH.SBPOL0030": (date(2015, 5, 25), None),
}
TARGET_RATE_EXPECTED_START = date(2015, 5, 25)

CITATION = (
    "State Bank of Pakistan, (2026), As-Needed data on {name} from Structure of "
    "Interest Rate: State Bank of Pakistan Policy Rates, Source EasyData"
)

NOTES = (
    "SBP EasyData API, As-Needed series (a row only when a rate changes). "
    "Re-queryable, but SBP may revise or remove data without notice, so each "
    "run's raw responses are kept as the record of what we saw "
    "(append_only_snapshot); revisions against the previous run are logged as "
    "warnings. Long format, one row per series per observation; the three "
    "series are never merged. To get the policy rate on day d: take the series "
    "whose policy window contains d (Reverse Repo 2009-08-17..2015-05-24, "
    "Target Rate from 2015-05-25), then its latest row with observation_date "
    "<= d. effective_to is the next observation date of the same series "
    "(exclusive). published_at is null (basis 'unknown'): the API has no "
    "publication date; announcement dates will come from a separate MPC "
    "calendar dataset. Do not back-fill before a series' first observation."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = ed.INTERIM_SCHEMA
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),
        ("series_role", pa.string()),
        ("observation_date", pa.date32()),
        ("effective_to", pa.date32()),  # next observation of same series, exclusive
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("observation_status", pa.string()),
        ("status_comments", pa.string()),
        ("dataset_name", pa.string()),
        ("is_policy_rate", pa.bool_()),
        ("policy_rate_basis", pa.string()),
        ("published_at", _TS),  # null: the API has no publication date
        ("published_at_basis", pa.string()),
        ("portal_last_refresh_date", pa.date32()),  # from /meta, null if absent
        ("source_endpoint", pa.string()),  # no key
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)

SeriesMeta = ed.SeriesMeta


def _key_redaction(key: str) -> AbstractContextManager[None]:
    """Filter the key out of records logged by jobs.common, jobs.easydata and this module."""
    return ed.key_redaction(key, log)


# --------------------------------------------------------------------------
# Validation and derived columns
# --------------------------------------------------------------------------


def policy_series_for(day: date) -> str | None:
    """Key of the series that was the official policy rate on ``day``."""
    for key, (lo, hi) in POLICY_WINDOWS.items():
        if day >= lo and (hi is None or day <= hi):
            return key
    return None


def _policy_flag(key: str, start: date, effective_to: date | None) -> tuple[bool, str]:
    window = POLICY_WINDOWS.get(key)
    if window is None:
        return False, "corridor rate; never the policy rate in this dataset"
    lo, hi = window
    span = f"{lo.isoformat()}..{hi.isoformat() if hi else ''}"
    overlaps = (hi is None or start <= hi) and (effective_to is None or effective_to > lo)
    if overlaps:
        return True, f"in effect within policy-rate window {span} (dataset description)"
    return False, f"outside policy-rate window {span}"


def validate(
    interim: list[dict[str, Any]],
    metas: Mapping[str, ed.SeriesMeta[Series]],
    *,
    end_date: date,
    fetched_at: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Typed, checked rows for ``processed/``, plus warnings. Raises on bad data."""
    warnings: list[str] = []
    by_series: dict[str, list[dict[str, Any]]] = {s.key: [] for s in SERIES}
    for row in interim:
        series = metas[row["requested_series_key"]].series
        if (row["series_key"] or "").strip() != series.key:
            raise PolicyRateError(
                f"{series.key}: response contains a row for series {row['series_key']!r}"
            )
        if (row["series_name"] or "").strip() != series.name:
            raise PolicyRateError(
                f"{series.key}: row series name {row['series_name']!r} != {series.name!r}"
            )
        by_series[series.key].append(
            {
                **row,
                "_date": parse_date(row["observation_date"]),
                "_value": parse_value(row["observation_value"]),
            }
        )

    out: list[dict[str, Any]] = []
    for series in SERIES:
        rows = sorted(by_series[series.key], key=lambda r: r["_date"])
        meta = metas[series.key]
        if len(rows) < 2:
            raise PolicyRateError(
                f"{series.key}: {len(rows)} row(s) returned; expected the full history "
                f"from {meta.start_date} (was start_date ignored?)"
            )
        dates = [r["_date"] for r in rows]
        dupes = sorted({d.isoformat() for d in dates if dates.count(d) > 1})
        if dupes:
            raise PolicyRateError(f"{series.key}: duplicate observation dates {dupes}")
        if dates[-1] > end_date:
            raise PolicyRateError(f"{series.key}: observation after end_date: {dates[-1]}")
        if dates[0] != meta.start_date:
            warnings.append(
                f"{series.key}: first observation {dates[0]} differs from metadata "
                f"start date {meta.start_date}"
            )
        if series.key == "TS_GP_IR_SIRPR_AH.SBPOL0030" and dates[0] != TARGET_RATE_EXPECTED_START:
            warnings.append(
                f"{series.key}: Target Rate starts {dates[0]}, expected "
                f"{TARGET_RATE_EXPECTED_START}; check the policy-rate windows"
            )
        datasets = {r["dataset_name"] for r in rows if r["dataset_name"]}
        if datasets and datasets != {EXPECTED_DATASET}:
            warnings.append(f"{series.key}: dataset name {sorted(datasets)} != {EXPECTED_DATASET!r}")
        units = {r["unit"] for r in rows}
        if len(units) > 1:
            warnings.append(f"{series.key}: several units {sorted(map(str, units))}")
        odd = [r for r in rows if not 0.0 < r["_value"] < 100.0]
        if odd:
            warnings.append(
                f"{series.key}: {len(odd)} value(s) outside (0, 100), first on {odd[0]['_date']}"
            )
        for i, r in enumerate(rows):
            effective_to = rows[i + 1]["_date"] if i + 1 < len(rows) else None
            flag, basis = _policy_flag(series.key, r["_date"], effective_to)
            out.append(
                {
                    "series_key": series.key,
                    "series_name": series.name,
                    "series_role": series.role,
                    "observation_date": r["_date"],
                    "effective_to": effective_to,
                    "value": r["_value"],
                    "unit": r["unit"],
                    "observation_status": r["observation_status"],
                    "status_comments": r["status_comments"],
                    "dataset_name": r["dataset_name"],
                    "is_policy_rate": flag,
                    "policy_rate_basis": basis,
                    "published_at": None,
                    "published_at_basis": PUBLISHED_AT_BASIS,
                    "portal_last_refresh_date": meta.last_refresh,
                    "source_endpoint": f"{API_BASE}/series/{series.key}/data",
                    "fetched_at": fetched_at,
                    "raw_file": r["raw_file"],
                    "run_id": r["run_id"],
                }
            )
    return out, warnings


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def run(*, upload: bool) -> int:
    key = get_secret(KEY_ENV)  # raises naming the variable; no request made yet
    with _key_redaction(key):
        try:
            return _run(key, upload=upload)
        except PolicyRateError as exc:
            log.error("%s", redact(str(exc), key))
            return 1


def _run(key: str, *, upload: bool) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    end_date = datetime.now(PKT).date()
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
    )
    metas, data_bodies, raw_names = fetched.metas, fetched.data, fetched.raw_names
    run_files = list(fetched.files)

    # 4. Interim: parsed, not validated.
    interim = ed.interim_rows(fetched, SERIES, run_id)
    interim_table = pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA)
    assert_no_secret(json.dumps(interim, default=str), key, "interim rows")
    run_files.append(
        _write_table(interim_table, LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet")
    )

    # 5. Validate and derive.
    processed, check_warnings = validate(interim, metas, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

    # 6. Revisions against the previous processed file.
    previous = ed.load_previous(
        upload=upload,
        hf_path=f"{HF_PREFIX}/{PROCESSED_REL}",
        local_path=LOCAL_ROOT / PROCESSED_REL,
        downloader=hf_download,
        warnings=warnings,
    )
    revisions, revision_warnings = diff_revisions(previous, processed)
    warnings += revision_warnings

    table = pa.Table.from_pylist(processed, schema=PROCESSED_SCHEMA)
    assert_no_secret(json.dumps(processed, default=str), key, "processed rows")
    run_files.append(_write_table(table, LOCAL_ROOT / PROCESSED_REL))

    # 7. Run log.
    per_series: dict[str, dict[str, Any]] = {}
    for s in SERIES:
        rows = [r for r in processed if r["series_key"] == s.key]
        m = metas[s.key]
        per_series[s.key] = {
            "name": m.name,
            "role": s.role,
            "meta_start_date": m.start_date.isoformat(),
            "portal_last_refresh_date": m.last_refresh.isoformat() if m.last_refresh else None,
            "meta_dataset_name": m.dataset_name,
            "http_status": data_bodies[s.key][1],
            "bytes": len(data_bodies[s.key][0]),
            "rows": len(rows),
            "first_observation": rows[0]["observation_date"].isoformat(),
            "last_observation": rows[-1]["observation_date"].isoformat(),
            "raw_files": raw_names[s.key],
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
        "revisions": revisions,
        "published_at_basis": PUBLISHED_AT_BASIS,
        "warnings": warnings,
    }
    runlog_text = json.dumps(summary, indent=2) + "\n"
    assert_no_secret(runlog_text, key, "run log")
    run_files.append(
        _write_new(LOCAL_ROOT / "runs" / f"fetch_date={fetch_date}" / f"run-{run_id}.json", runlog_text.encode())
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
            f"'{EXPECTED_DATASET}', series " + ", ".join(s.key for s in SERIES)
        ),
        row_count=len(processed),
        date_range=(min(obs_dates), max(obs_dates)),
        notes=NOTES,
        extra={
            "series": per_series,
            "published_at_basis": PUBLISHED_AT_BASIS,
            "last_run": {k: v for k, v in summary.items() if k not in ("warnings", "series")},
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
        help="stage under data/macro/policy_rate/ only; no HF_TOKEN needed",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
