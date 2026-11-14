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
key before anything is written to the manifest or uploaded.

Run from ``backend/``::

    python -m jobs.macro_policy_rate              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_policy_rate --no-upload  # local only: stage under data/macro/policy_rate/
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import shutil
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from jobs.common import (
    DATA_DIR,
    HttpError,
    HttpResponse,
    RobotsDisallowedError,
    get_secret,
    git_info,
    hf_download,
    http_get,
    upload_to_hf,
    write_manifest,
)

log = logging.getLogger("jobs.macro_policy_rate")

STREAM = "macro"
DATASET = "policy_rate"
HF_PREFIX = "macro/policy_rate"
LOCAL_ROOT = DATA_DIR / "macro" / "policy_rate"
PROCESSED_REL = "processed/policy_rate.parquet"
MANIFEST_REL = "manifest.json"

API_BASE = "https://easydata.sbp.org.pk/api/v1"
KEY_ENV = "EASYDATA_API_KEY"
MIN_INTERVAL = 2.0  # seconds between requests; limits are 250/hour, 2,000/day
PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time, no DST
EXPECTED_DATASET = "Structure of Interest Rate: State Bank of Pakistan Policy Rates"
PUBLISHED_AT_BASIS = "unknown"
REDACTED = "REDACTED"


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
INTERIM_SCHEMA = pa.schema(
    [
        ("requested_series_key", pa.string()),
        ("dataset_name", pa.string()),
        ("series_key", pa.string()),
        ("series_name", pa.string()),
        ("observation_date", pa.string()),  # as returned
        ("observation_value", pa.string()),  # as returned
        ("unit", pa.string()),
        ("observation_status", pa.string()),
        ("status_comments", pa.string()),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)
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

# Field names in API responses, after lower-casing and dropping non-alphanumerics.
DATA_FIELDS = {
    "datasetname": "dataset_name",
    "serieskey": "series_key",
    "seriesname": "series_name",
    "observationdate": "observation_date",
    "observationvalue": "observation_value",
    "unit": "unit",
    "observationstatus": "observation_status",
    "statuscomments": "status_comments",
}
REQUIRED_DATA_FIELDS = ("series_key", "series_name", "observation_date", "observation_value")
META_NAME_KEYS = frozenset({"seriesname"})
META_START_KEYS = frozenset(
    {
        "availablesince",  # EasyData /meta "Available Since" (seen 2026-11-12)
        "observationstartdate",
        "startdate",
        "seriesstartdate",
        "firstobservationdate",
        "observationstart",
        "datastartdate",
        "fromdate",
    }
)
META_REFRESH_KEYS = frozenset(
    {
        "lastrefreshdate",
        "lastrefresh",
        "refreshdate",
        "lastupdated",
        "lastupdatedate",
        "lastupdate",
    }
)
#: EasyData /meta uses "Reference Dataset Name"; /data rows use "Dataset Name".
META_DATASET_KEYS = frozenset({"referencedatasetname", "datasetname"})
_ROW_LIST_KEYS =("data", "rows", "items", "observations", "results", "records")


class PolicyRateError(RuntimeError):
    """The run must stop. Messages never contain the API key."""


# --------------------------------------------------------------------------
# Keeping the API key out of everything
# --------------------------------------------------------------------------


def _secret_forms(key: str) -> tuple[str, ...]:
    forms = {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}
    return tuple(sorted((f for f in forms if f), key=len, reverse=True))


def redact(text: str, key: str) -> str:
    """Replace every form of ``key`` (plain and URL-encoded) in ``text``."""
    for form in _secret_forms(key) if key else ():
        text = text.replace(form, REDACTED)
    return text


def contains_secret(data: str | bytes, key: str) -> bool:
    if not key:
        return False
    if isinstance(data, bytes):
        return any(f.encode() in data for f in _secret_forms(key))
    return any(f in data for f in _secret_forms(key))


def assert_no_secret(data: str | bytes, key: str, where: str) -> None:
    if contains_secret(data, key):
        raise PolicyRateError(
            f"the API key appears in {where}; stopping before it is stored or uploaded"
        )


class _RedactKeyFilter(logging.Filter):
    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if contains_secret(message, self._key):
            record.msg = redact(message, self._key)
            record.args = None
        return True


@contextlib.contextmanager
def _key_redaction(key: str) -> Iterator[None]:
    """Filter the key out of records logged by jobs.common and this module."""
    filt = _RedactKeyFilter(key)
    loggers = [logging.getLogger("jobs.common"), log]
    for lg in loggers:
        lg.addFilter(filt)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeFilter(filt)


def _get(url: str, params: Mapping[str, str], key: str, what: str) -> HttpResponse:
    """``http_get`` with errors re-raised without the key and without context."""
    try:
        return http_get(url, params=params, min_interval=MIN_INTERVAL)
    except HttpError as exc:
        hint = ""
        if exc.status in (401, 403):
            hint = f" (check {KEY_ENV}: keys expire after 90 days)"
        message = f"{what} failed: {redact(str(exc), key)}{hint}"
    except RobotsDisallowedError as exc:
        message = f"{what} refused by robots.txt: {redact(str(exc), key)}"
    except (OSError, ValueError) as exc:
        message = f"{what} failed: {type(exc).__name__}: {redact(str(exc), key)}"
    raise PolicyRateError(message) from None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _norm(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _column_name(col: object) -> str:
    if isinstance(col, str):
        return col
    if isinstance(col, Mapping) and isinstance(col.get("name"), str):
        return str(col["name"])
    raise PolicyRateError(f"unrecognised column descriptor of type {type(col).__name__}")


def records(payload: Any) -> list[dict[str, Any]]:
    """Rows of an API response as dicts.

    Accepts a list of objects, ``{"columns": [...], "rows"|"data": [[...]]}``,
    or an object holding a list of objects under a usual key. Anything else
    stops the run, listing only the response's top-level structure.
    """
    if isinstance(payload, list) and all(isinstance(r, Mapping) for r in payload):
        return [dict(r) for r in payload]
    if isinstance(payload, Mapping):
        by_norm = {_norm(k): v for k, v in payload.items()}
        cols = by_norm.get("columns")
        if isinstance(cols, list):
            names = [_column_name(c) for c in cols]
            for rows_key in ("rows", "data"):
                rows = by_norm.get(rows_key)
                if isinstance(rows, list) and all(isinstance(r, list) for r in rows):
                    if any(len(r) != len(names) for r in rows):
                        raise PolicyRateError("response rows and columns differ in length")
                    return [dict(zip(names, r, strict=True)) for r in rows]
        for k in _ROW_LIST_KEYS:
            v = by_norm.get(k)
            if isinstance(v, list) and all(isinstance(r, Mapping) for r in v):
                return [dict(r) for r in v]
        shape = {str(k): type(v).__name__ for k, v in payload.items()}
        raise PolicyRateError(f"unrecognised response shape; top-level keys: {shape}")
    raise PolicyRateError(f"unrecognised response shape: {type(payload).__name__}")


def _flatten(obj: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if isinstance(v, (Mapping, list)):
                out.extend(_flatten(v))
            else:
                out.append((_norm(k), v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_flatten(item))
    return out


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %B %Y",
)


def parse_date(value: object) -> date:
    """Parse an unambiguous date. Slash formats (DD/MM vs MM/DD) are refused."""
    if not isinstance(value, str) or not value.strip():
        raise PolicyRateError(f"unrecognised date {value!r}")
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = re.sub(r"\.0+$", "", s)
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if (dt.hour, dt.minute, dt.second) != (0, 0, 0):
            raise PolicyRateError(f"date has a time of day: {value!r}")
        return dt.date()
    raise PolicyRateError(f"unrecognised date {value!r}")


def parse_value(value: object) -> float:
    if isinstance(value, bool):
        raise PolicyRateError(f"non-numeric observation value {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            pass
    raise PolicyRateError(f"non-numeric observation value {value!r}")


@dataclass(frozen=True)
class SeriesMeta:
    series: Series
    name: str
    start_date: date
    last_refresh: date | None
    dataset_name: str | None


def _lookup(
    pairs: list[tuple[str, Any]], keys: frozenset[str], field: str, key_name: str
) -> str | None:
    values = {
        str(v).strip() for k, v in pairs if k in keys and v is not None and str(v).strip()
    }
    if len(values) > 1:
        raise PolicyRateError(f"{key_name}: metadata has several values for {field}: {sorted(values)}")
    return values.pop() if values else None


def parse_meta(payload: Any, series: Series) -> SeriesMeta:
    """Check the series name and read start date and refresh date from /meta."""
    pairs = _flatten(payload)
    with contextlib.suppress(PolicyRateError):  # columns/rows layout -> named fields
        pairs += _flatten(records(payload))
    available = sorted({k for k, _ in pairs})
    name = _lookup(pairs, META_NAME_KEYS, "series name", series.key)
    if name is None:
        raise PolicyRateError(
            f"{series.key}: metadata has no series name; fields: {available}"
        )
    if name != series.name:
        raise PolicyRateError(
            f"{series.key}: series name mismatch: expected {series.name!r}, got {name!r}"
        )
    start = _lookup(pairs, META_START_KEYS, "start date", series.key)
    if start is None:
        raise PolicyRateError(
            f"{series.key}: metadata has no start date; fields: {available}"
        )
    refresh = _lookup(pairs, META_REFRESH_KEYS, "last refresh date", series.key)
    dataset = _lookup(pairs, META_DATASET_KEYS, "dataset name", series.key)
    return SeriesMeta(
        series=series,
        name=name,
        start_date=parse_date(start),
        last_refresh=parse_date(refresh) if refresh else None,
        dataset_name=dataset,
    )


def data_rows(payload: Any, series: Series, raw_file: str, run_id: str) -> list[dict[str, Any]]:
    """Rows of a /data response as interim records (strings, not validated)."""
    out = []
    for rec in records(payload):
        fields = {DATA_FIELDS[_norm(k)]: v for k, v in rec.items() if _norm(k) in DATA_FIELDS}
        missing = [f for f in REQUIRED_DATA_FIELDS if f not in fields]
        if missing:
            raise PolicyRateError(
                f"{series.key}: data row lacks {missing}; fields: {sorted(rec)}"
            )
        row: dict[str, Any] = {
            name: (None if fields.get(name) is None else str(fields[name]))
            for name in DATA_FIELDS.values()
        }
        row.update(requested_series_key=series.key, raw_file=raw_file, run_id=run_id)
        out.append(row)
    return out


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
    metas: Mapping[str, SeriesMeta],
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


def diff_revisions(
    previous: pa.Table | None, current: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Compare with the previous processed table. New observations after the
    previous latest date are expected; anything else is a revision warning."""
    if previous is None:
        return {"previous": None}, []
    prev: dict[tuple[str, str], float] = {}
    prev_max: dict[str, str] = {}
    cols = previous.select(["series_key", "observation_date", "value"]).to_pylist()
    for r in cols:
        d = r["observation_date"].isoformat()
        prev[(r["series_key"], d)] = r["value"]
        prev_max[r["series_key"]] = max(prev_max.get(r["series_key"], d), d)
    cur = {(r["series_key"], r["observation_date"].isoformat()): r["value"] for r in current}
    changed = sorted(
        f"{k[0]} {k[1]}: {prev[k]} -> {cur[k]}" for k in prev.keys() & cur.keys() if prev[k] != cur[k]
    )
    removed = sorted(f"{k[0]} {k[1]}" for k in prev.keys() - cur.keys())
    added = sorted(cur.keys() - prev.keys())
    backfilled = sorted(f"{k[0]} {k[1]}" for k in added if k[1] <= prev_max.get(k[0], ""))
    warnings = []
    if changed:
        warnings.append(f"revised values since previous run: {changed}")
    if removed:
        warnings.append(f"observations removed since previous run: {removed}")
    if backfilled:
        warnings.append(f"observations back-filled before previous latest date: {backfilled}")
    summary = {
        "previous": "loaded",
        "changed": len(changed),
        "removed": len(removed),
        "added": len(added),
        "backfilled": len(backfilled),
    }
    return summary, warnings


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def _write_new(path: Path, data: bytes) -> Path:
    """Write a file that must not already exist (raw data is immutable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(data)
    return path


def _write_table(table: pa.Table, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)
    return path


def _load_previous(upload: bool, warnings: list[str]) -> pa.Table | None:
    try:
        if upload:
            remote = hf_download(f"{HF_PREFIX}/{PROCESSED_REL}")
            return None if remote is None else pq.read_table(remote)
        local = LOCAL_ROOT / PROCESSED_REL
        return pq.read_table(local) if local.is_file() else None
    except Exception as exc:  # noqa: BLE001 - revision check is best-effort
        warnings.append(f"could not load previous processed file, revision check skipped: {type(exc).__name__}")
        return None


def _upload_run(files: Iterable[Path], run_id: str) -> str:
    with tempfile.TemporaryDirectory(prefix="macro_policy_rate_upload_") as tmp:
        for f in files:
            dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return upload_to_hf(
            tmp, f"{HF_PREFIX}/", stream=STREAM, commit_message=f"macro/policy_rate: run {run_id}"
        )


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
    raw_dir = LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}"

    # 1. Metadata for every series, all checked before any data request.
    metas: dict[str, SeriesMeta] = {}
    meta_bodies: dict[str, bytes] = {}
    for s in SERIES:
        resp = _get(f"{API_BASE}/series/{s.key}/meta", {"api_key": key, "format": "json"}, key, f"{s.key} metadata")
        assert_no_secret(resp.content, key, f"{s.key} metadata response")
        try:
            payload = resp.json()
        except ValueError:
            raise PolicyRateError(f"{s.key}: metadata response is not JSON") from None
        metas[s.key] = parse_meta(payload, s)
        meta_bodies[s.key] = resp.content
        log.info("%s: %r, start %s, last refresh %s", s.key, metas[s.key].name, metas[s.key].start_date, metas[s.key].last_refresh)

    # 2. Data, from each series' own start date.
    data_bodies: dict[str, tuple[bytes, int]] = {}
    for s in SERIES:
        params = {
            "api_key": key,
            "start_date": metas[s.key].start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "format": "json",
        }
        resp = _get(f"{API_BASE}/series/{s.key}/data", params, key, f"{s.key} data")
        assert_no_secret(resp.content, key, f"{s.key} data response")
        data_bodies[s.key] = (resp.content, resp.status)

    # 3. Raw, unmodified.
    run_files: list[Path] = []
    raw_names: dict[str, dict[str, str]] = {}
    for s in SERIES:
        meta_path = _write_new(raw_dir / f"{run_id}_{s.key}_meta.json", meta_bodies[s.key])
        data_path = _write_new(raw_dir / f"{run_id}_{s.key}_data.json", data_bodies[s.key][0])
        run_files += [meta_path, data_path]
        raw_names[s.key] = {
            "meta": meta_path.relative_to(LOCAL_ROOT).as_posix(),
            "data": data_path.relative_to(LOCAL_ROOT).as_posix(),
        }

    # 4. Interim: parsed, not validated.
    interim: list[dict[str, Any]] = []
    for s in SERIES:
        try:
            payload = json.loads(data_bodies[s.key][0])
        except ValueError:
            raise PolicyRateError(f"{s.key}: data response is not JSON") from None
        interim.extend(data_rows(payload, s, raw_names[s.key]["data"], run_id))
    interim_table = pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA)
    assert_no_secret(json.dumps(interim, default=str), key, "interim rows")
    run_files.append(
        _write_table(interim_table, LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet")
    )

    # 5. Validate and derive.
    processed, check_warnings = validate(interim, metas, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

    # 6. Revisions against the previous processed file.
    previous = _load_previous(upload, warnings)
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

    # 8. Final scan of every raw/JSON output, then upload.
    for f in run_files:
        if f.suffix == ".json":
            assert_no_secret(f.read_bytes(), key, f"output file {f.name}")

    revision = None
    exit_code = 0
    if upload:
        try:
            revision = _upload_run(run_files, run_id)
        except Exception as exc:  # noqa: BLE001 - report and fail the run
            log.error("upload failed: %s", redact(str(exc), key))
            return 1

    # 9. Manifest.
    obs_dates = [r["observation_date"] for r in processed]
    extra = {
        "series": per_series,
        "published_at_basis": PUBLISHED_AT_BASIS,
        "last_run": {k: v for k, v in summary.items() if k not in ("warnings", "series")},
    }
    assert_no_secret(json.dumps([warnings, extra, NOTES], default=str), key, "manifest fields")
    manifest = write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source=(
            "SBP EasyData API <" + API_BASE + "/series/<series_key>/{meta,data}>, dataset "
            f"'{EXPECTED_DATASET}', series " + ", ".join(s.key for s in SERIES)
        ),
        fetch_mode="append_only_snapshot",
        row_count=len(processed),
        date_range=(min(obs_dates), max(obs_dates)),
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra=extra,
        files=run_files,
        warnings=warnings,
    )
    if contains_secret(manifest.read_bytes(), key):
        manifest.unlink()
        raise PolicyRateError("the API key appears in the manifest; manifest deleted")
    log.info("wrote %s (%d rows)", manifest.name, len(processed))
    if upload:
        try:
            upload_to_hf(
                manifest,
                f"{HF_PREFIX}/{MANIFEST_REL}",
                stream=STREAM,
                commit_message=f"macro/policy_rate: manifest for run {run_id}",
            )
        except Exception as exc:  # noqa: BLE001 - data is already uploaded
            log.error("manifest upload failed (data commit %s is fine): %s", revision, redact(str(exc), key))
            exit_code = 1
    return exit_code


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
