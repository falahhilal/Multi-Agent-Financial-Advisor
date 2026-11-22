"""Shared helpers for macro-stream loaders that read the SBP EasyData API.

Used by ``macro_policy_rate.py``, ``macro_tbill_auctions.py``,
``macro_inflation.py`` and ``macro_fx.py``. Not part of
``common.py`` (shared by all streams); this module belongs to the macro stream.

EasyData passes the API key (``EASYDATA_API_KEY``) in the query string, and
``common.http_get`` puts the full URL into error messages and retry log lines.
So everything here keeps the key out of outputs:

* :func:`get` re-raises HTTP errors with the key replaced by ``REDACTED`` and
  with no exception chain;
* :func:`key_redaction` filters the key out of log records;
* :func:`assert_no_secret` is the canary scan for responses, rows, run logs,
  warnings and manifests;
* ``HttpResponse.url`` is never stored.

Endpoints: ``<API_BASE>/series/<series_key>/meta`` and ``/data``. Without
``start_date`` the data endpoint returns only the latest observation, so
:func:`fetch_series` always sends the start date read from ``/meta``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq

from jobs.common import (
    HttpError,
    HttpResponse,
    RobotsDisallowedError,
    http_get,
    write_manifest,
)

log = logging.getLogger("jobs.easydata")

API_BASE = "https://easydata.sbp.org.pk/api/v1"
KEY_ENV = "EASYDATA_API_KEY"
MIN_INTERVAL = 2.0  # seconds between requests; limits are 250/hour, 2,000/day
PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time, no DST
REDACTED = "REDACTED"
MAX_LISTED = 10  # items listed in one warning; full lists are in the run log

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
_ROW_LIST_KEYS = ("data", "rows", "items", "observations", "results", "records")

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


class EasyDataError(RuntimeError):
    """The run must stop. Messages never contain the API key."""


class SeriesLike(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def name(self) -> str: ...


S = TypeVar("S", bound=SeriesLike)


def _identity(name: str) -> str:
    return name


def listed(items: list[str]) -> str:
    """At most ``MAX_LISTED`` items, comma-separated, with a count of the rest."""
    more = f" (+{len(items) - MAX_LISTED} more, see run log)" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more


# --------------------------------------------------------------------------
# Keeping the API key out of everything
# --------------------------------------------------------------------------


def secret_forms(key: str) -> tuple[str, ...]:
    forms = {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}
    return tuple(sorted((f for f in forms if f), key=len, reverse=True))


def redact(text: str, key: str) -> str:
    """Replace every form of ``key`` (plain and URL-encoded) in ``text``."""
    for form in secret_forms(key) if key else ():
        text = text.replace(form, REDACTED)
    return text


def contains_secret(data: str | bytes, key: str) -> bool:
    if not key:
        return False
    if isinstance(data, bytes):
        return any(f.encode() in data for f in secret_forms(key))
    return any(f in data for f in secret_forms(key))


def assert_no_secret(data: str | bytes, key: str, where: str) -> None:
    if contains_secret(data, key):
        raise EasyDataError(
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
def key_redaction(key: str, *job_loggers: logging.Logger) -> Iterator[None]:
    """Filter the key out of records logged by jobs.common, this module and
    the calling job's loggers."""
    filt = _RedactKeyFilter(key)
    loggers = [logging.getLogger("jobs.common"), log, *job_loggers]
    for lg in loggers:
        lg.addFilter(filt)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeFilter(filt)


def get(url: str, params: Mapping[str, str], key: str, what: str) -> HttpResponse:
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
    raise EasyDataError(message) from None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def norm(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _column_name(col: object) -> str:
    if isinstance(col, str):
        return col
    if isinstance(col, Mapping) and isinstance(col.get("name"), str):
        return str(col["name"])
    raise EasyDataError(f"unrecognised column descriptor of type {type(col).__name__}")


def records(payload: Any) -> list[dict[str, Any]]:
    """Rows of an API response as dicts.

    Accepts a list of objects, ``{"columns": [...], "rows"|"data": [[...]]}``,
    or an object holding a list of objects under a usual key. Anything else
    stops the run, listing only the response's top-level structure.
    """
    if isinstance(payload, list) and all(isinstance(r, Mapping) for r in payload):
        return [dict(r) for r in payload]
    if isinstance(payload, Mapping):
        by_norm = {norm(k): v for k, v in payload.items()}
        cols = by_norm.get("columns")
        if isinstance(cols, list):
            names = [_column_name(c) for c in cols]
            for rows_key in ("rows", "data"):
                rows = by_norm.get(rows_key)
                if isinstance(rows, list) and all(isinstance(r, list) for r in rows):
                    if any(len(r) != len(names) for r in rows):
                        raise EasyDataError("response rows and columns differ in length")
                    return [dict(zip(names, r, strict=True)) for r in rows]
        for k in _ROW_LIST_KEYS:
            v = by_norm.get(k)
            if isinstance(v, list) and all(isinstance(r, Mapping) for r in v):
                return [dict(r) for r in v]
        shape = {str(k): type(v).__name__ for k, v in payload.items()}
        raise EasyDataError(f"unrecognised response shape; top-level keys: {shape}")
    raise EasyDataError(f"unrecognised response shape: {type(payload).__name__}")


def _flatten(obj: Any) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if isinstance(v, (Mapping, list)):
                out.extend(_flatten(v))
            else:
                out.append((norm(k), v))
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
        raise EasyDataError(f"unrecognised date {value!r}")
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
            raise EasyDataError(f"date has a time of day: {value!r}")
        return dt.date()
    raise EasyDataError(f"unrecognised date {value!r}")


def parse_value(value: object) -> float:
    """A number, or a numeric string. ``".1215"`` (no leading zero) is accepted."""
    if isinstance(value, bool):
        raise EasyDataError(f"non-numeric observation value {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            pass
    raise EasyDataError(f"non-numeric observation value {value!r}")


@dataclass(frozen=True)
class SeriesMeta(Generic[S]):
    series: S
    name: str  # after the job's name cleaning
    start_date: date
    last_refresh: date | None
    dataset_name: str | None
    raw_name: str = ""  # exactly as returned


def _lookup(
    pairs: list[tuple[str, Any]], keys: frozenset[str], what: str, key_name: str
) -> str | None:
    values = {
        str(v).strip() for k, v in pairs if k in keys and v is not None and str(v).strip()
    }
    if len(values) > 1:
        raise EasyDataError(f"{key_name}: metadata has several values for {what}: {sorted(values)}")
    return values.pop() if values else None


def parse_meta(
    payload: Any, series: S, clean_name: Callable[[str], str] = _identity
) -> SeriesMeta[S]:
    """Check the series name (after ``clean_name``) and read the start date,
    refresh date and dataset name from /meta."""
    pairs = _flatten(payload)
    with contextlib.suppress(EasyDataError):  # columns/rows layout -> named fields
        pairs += _flatten(records(payload))
    available = sorted({k for k, _ in pairs})
    raw_name = _lookup(pairs, META_NAME_KEYS, "series name", series.key)
    if raw_name is None:
        raise EasyDataError(f"{series.key}: metadata has no series name; fields: {available}")
    name = clean_name(raw_name)
    if name != series.name:
        raise EasyDataError(
            f"{series.key}: series name mismatch: expected {series.name!r}, got {raw_name!r}"
        )
    start = _lookup(pairs, META_START_KEYS, "start date", series.key)
    if start is None:
        raise EasyDataError(f"{series.key}: metadata has no start date; fields: {available}")
    refresh = _lookup(pairs, META_REFRESH_KEYS, "last refresh date", series.key)
    dataset = _lookup(pairs, META_DATASET_KEYS, "dataset name", series.key)
    return SeriesMeta(
        series=series,
        name=name,
        start_date=parse_date(start),
        last_refresh=parse_date(refresh) if refresh else None,
        dataset_name=dataset,
        raw_name=raw_name,
    )


def data_rows(payload: Any, series: SeriesLike, raw_file: str, run_id: str) -> list[dict[str, Any]]:
    """Rows of a /data response as interim records (strings, not validated)."""
    out = []
    for rec in records(payload):
        fields = {DATA_FIELDS[norm(k)]: v for k, v in rec.items() if norm(k) in DATA_FIELDS}
        missing = [f for f in REQUIRED_DATA_FIELDS if f not in fields]
        if missing:
            raise EasyDataError(f"{series.key}: data row lacks {missing}; fields: {sorted(rec)}")
        row: dict[str, Any] = {
            name: (None if fields.get(name) is None else str(fields[name]))
            for name in DATA_FIELDS.values()
        }
        row.update(requested_series_key=series.key, raw_file=raw_file, run_id=run_id)
        out.append(row)
    return out


def diff_revisions(
    previous: pa.Table | None, current: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Compare with the previous processed table. New observations after the
    previous latest date are expected; anything else is a revision warning.
    A value changing from or to null counts as a revision."""
    if previous is None:
        return {"previous": None}, []
    prev: dict[tuple[str, str], float | None] = {}
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
            f"({len(changed)} row(s)): {listed(changed)}"
        )
    return {"status_changed": len(changed), "status_changes": changed}, warnings


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def write_new(path: Path, data: bytes) -> Path:
    """Write a file that must not already exist (raw data is immutable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(data)
    return path


def write_table(table: pa.Table, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)
    return path


def load_previous(
    *,
    upload: bool,
    hf_path: str,
    local_path: Path,
    downloader: Callable[[str], Path | None],
    warnings: list[str],
) -> pa.Table | None:
    """Previous processed table: from HF when uploading, else from local disk."""
    try:
        if upload:
            remote = downloader(hf_path)
            return None if remote is None else pq.read_table(remote)
        return pq.read_table(local_path) if local_path.is_file() else None
    except Exception as exc:  # noqa: BLE001 - revision check is best-effort
        warnings.append(
            f"could not load previous processed file, revision check skipped: {type(exc).__name__}"
        )
        return None


# --------------------------------------------------------------------------
# Run steps shared by the EasyData jobs
# --------------------------------------------------------------------------


@dataclass
class Fetched(Generic[S]):
    metas: dict[str, SeriesMeta[S]]
    data: dict[str, tuple[bytes, int]]  # body, HTTP status
    raw_names: dict[str, dict[str, str]]  # key -> {"meta": rel, "data": rel}
    files: list[Path] = field(default_factory=list)


def fetch_series(
    series: Sequence[S],
    key: str,
    *,
    local_root: Path,
    raw_dir: Path,
    run_id: str,
    end_date: date,
    job_log: logging.Logger,
    clean_name: Callable[[str], str] = _identity,
) -> Fetched[S]:
    """Fetch /meta for every series (all checked before any data request),
    then /data from each series' own start date, then save every response
    unmodified. Nothing is written if any check fails."""
    metas: dict[str, SeriesMeta[S]] = {}
    meta_bodies: dict[str, bytes] = {}
    for s in series:
        resp = get(f"{API_BASE}/series/{s.key}/meta", {"api_key": key, "format": "json"}, key, f"{s.key} metadata")
        assert_no_secret(resp.content, key, f"{s.key} metadata response")
        try:
            payload = resp.json()
        except ValueError:
            raise EasyDataError(f"{s.key}: metadata response is not JSON") from None
        metas[s.key] = parse_meta(payload, s, clean_name)
        meta_bodies[s.key] = resp.content
        m = metas[s.key]
        job_log.info("%s: %r, start %s, last refresh %s", s.key, m.raw_name, m.start_date, m.last_refresh)

    data: dict[str, tuple[bytes, int]] = {}
    for s in series:
        params = {
            "api_key": key,
            "start_date": metas[s.key].start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "format": "json",
        }
        resp = get(f"{API_BASE}/series/{s.key}/data", params, key, f"{s.key} data")
        assert_no_secret(resp.content, key, f"{s.key} data response")
        data[s.key] = (resp.content, resp.status)

    fetched = Fetched(metas=metas, data=data, raw_names={})
    for s in series:
        meta_path = write_new(raw_dir / f"{run_id}_{s.key}_meta.json", meta_bodies[s.key])
        data_path = write_new(raw_dir / f"{run_id}_{s.key}_data.json", data[s.key][0])
        fetched.files += [meta_path, data_path]
        fetched.raw_names[s.key] = {
            "meta": meta_path.relative_to(local_root).as_posix(),
            "data": data_path.relative_to(local_root).as_posix(),
        }
    return fetched


def interim_rows(fetched: Fetched[S], series: Sequence[S], run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in series:
        try:
            payload = json.loads(fetched.data[s.key][0])
        except ValueError:
            raise EasyDataError(f"{s.key}: data response is not JSON") from None
        rows.extend(data_rows(payload, s, fetched.raw_names[s.key]["data"], run_id))
    return rows


def upload_run(
    files: Iterable[Path],
    *,
    local_root: Path,
    hf_prefix: str,
    stream: str,
    run_id: str,
    uploader: Callable[..., str],
) -> str:
    """Upload this run's files under ``hf_prefix/`` in one HF commit."""
    with tempfile.TemporaryDirectory(prefix="easydata_upload_") as tmp:
        for f in files:
            dest = Path(tmp) / f.relative_to(local_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return uploader(tmp, f"{hf_prefix}/", stream=stream, commit_message=f"{hf_prefix}: run {run_id}")


def finish_run(
    *,
    key: str,
    upload: bool,
    stream: str,
    dataset: str,
    local_root: Path,
    hf_prefix: str,
    run_id: str,
    run_files: list[Path],
    source: str,
    row_count: int,
    date_range: tuple[date, date],
    notes: str,
    extra: Mapping[str, Any],
    warnings: list[str],
    script: str,
    uploader: Callable[..., str],
    job_log: logging.Logger,
) -> int:
    """Scan outputs for the key, upload, write and upload the manifest."""
    for f in run_files:
        if f.suffix == ".json":
            assert_no_secret(f.read_bytes(), key, f"output file {f.name}")

    revision = None
    exit_code = 0
    if upload:
        try:
            revision = upload_run(
                run_files,
                local_root=local_root,
                hf_prefix=hf_prefix,
                stream=stream,
                run_id=run_id,
                uploader=uploader,
            )
        except Exception as exc:  # noqa: BLE001 - report and fail the run
            job_log.error("upload failed: %s", redact(str(exc), key))
            return 1

    assert_no_secret(json.dumps([warnings, dict(extra), notes, source], default=str), key, "manifest fields")
    manifest = write_manifest(
        stream=stream,
        dataset=dataset,
        source=source,
        fetch_mode="append_only_snapshot",
        row_count=row_count,
        date_range=date_range,
        hf_path=f"{hf_prefix}/" if upload else None,
        hf_revision=revision,
        notes=notes,
        extra=extra,
        files=run_files,
        warnings=warnings,
        script=script,
    )
    if contains_secret(manifest.read_bytes(), key):
        manifest.unlink()
        raise EasyDataError("the API key appears in the manifest; manifest deleted")
    job_log.info("wrote %s (%d rows)", manifest.name, row_count)
    if upload:
        try:
            uploader(
                manifest,
                f"{hf_prefix}/manifest.json",
                stream=stream,
                commit_message=f"{hf_prefix}: manifest for run {run_id}",
            )
        except Exception as exc:  # noqa: BLE001 - data is already uploaded
            job_log.error("manifest upload failed (data commit %s is fine): %s", revision, redact(str(exc), key))
            exit_code = 1
    return exit_code
