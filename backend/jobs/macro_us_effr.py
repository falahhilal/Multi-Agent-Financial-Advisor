"""US effective federal funds rate (daily) -> dataset ``us_effr`` (stream ``macro``).

Source: New York Fed Markets Data API (no key),
``https://markets.newyorkfed.org/api/rates/unsecured/effr/...``. Replaces
FRED, whose terms forbid storing its content (Proposed ADR-0007).

Each run:

1. fetches ``last/1.json`` (the probe) and aborts, before the full request
   and before writing anything, unless ``refRates`` holds exactly one record
   with ``type == "EFFR"``, the required fields, and only known field names
   (the probe's shape is fixed from the manual download of 2026-11-23);
2. fetches ``search.json?startDate=REQUEST_START&endDate=<today PKT>``, the
   full history (the API returns what it has);
3. saves both responses unmodified to ``raw/fetch_date=D/<run>_effr_{last1,search}.json``;
4. writes parsed, unvalidated rows (values as the JSON text, unknown fields
   kept as JSON) to ``interim/fetch_date=D/observations-<run>.parquet``;
5. validates and writes ``processed/us_effr.parquet``:

   * fails on: a record whose ``type`` is not EFFR, a missing
     ``effectiveDate`` or ``percentRate``, unparseable or duplicate dates,
     dates after today, fewer than two rows, a value that is empty or
     non-numeric (also in the optional numeric fields);
   * warns on: weekend dates not in ``KNOWN_WEEKEND_DATES``, a gap of more
     than 4 calendar days, a rate below 0 or above 20, a daily change above
     0.30 pp from 2016-03-01 (above 1.00 pp before) on a day the target did
     not change against the previous record and that is not in the reviewed
     ``KNOWN_MOVES`` (both kinds are listed in the run log only), a rate
     outside the target range +/- 0.10 pp from
     2008-12-16, percentiles out of order, volume <= 0, unknown field names,
     a first date different from ``EXPECTED_START``, the latest date more
     than 7 days old, the probe record disagreeing with the full history,
     revisions and revision-indicator or footnote changes since the previous
     run; records carrying a revision indicator are listed in the run log;

6. writes a run log, uploads this run's files to HF ``macro/us_effr/`` in one
   commit, then writes ``backend/data_manifests/macro_us_effr.json`` and
   uploads a copy.

``published_at``: the NY Fed publishes the EFFR for the prior business day at
about 9:00 a.m. ET and may revise it at about 2:30 p.m. ET the same day. The
release date R of an observation is taken as the next EFFR date in the series
(EFFR dates are exactly the business days, so R is never earlier than the
true release); ``published_at`` = R + 1 day at 23:59:59 PKT, in UTC (basis
``effr_next_business_day_plus_1d``). The +1 day covers the 2:30 p.m. ET
revision, which is 00:30 PKT the next day in winter. The latest row has no
next date: it gets the fetch date (PKT) at 23:59:59 (basis
``effr_latest_row_fetch_date``) and is recomputed on the next run. See
``jobs/us_macro.py`` for why the US-date-plus-one-day step needs no DST logic.

Each run makes 2 requests. No rate limit is documented; requests are spaced
by 2 seconds.

Run from ``backend/``::

    python -m jobs.macro_us_effr              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_us_effr --no-upload  # local only: stage under data/macro/us_effr/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from jobs import easydata as ed
from jobs import us_macro as um
from jobs.common import DATA_DIR, REPO_ROOT, git_info, hf_download, upload_to_hf
from jobs.us_macro import UsMacroError

log = logging.getLogger("jobs.macro_us_effr")

STREAM = "macro"
DATASET = "us_effr"
HF_PREFIX = "macro/us_effr"
LOCAL_ROOT = DATA_DIR / "macro" / "us_effr"
PROCESSED_REL = "processed/us_effr.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

API_BASE = "https://markets.newyorkfed.org/api/rates/unsecured/effr"
PROBE_URL = f"{API_BASE}/last/1.json"
SEARCH_URL = f"{API_BASE}/search.json"
MIN_INTERVAL = 2.0  # seconds between requests; no documented limit
TIMEOUT = 120.0  # the full history is one large response

#: Sent as startDate: earlier than any EFFR history, so the API returns all
#: it has. Accepted by the API on the first run (2026-11-23).
REQUEST_START = date(1954, 7, 1)
#: First date of the API's EFFR history, confirmed on the first run
#: (2026-11-23, 6,630 records); a different first date warns.
EXPECTED_START = date(2000, 7, 3)

SERIES_KEY = "EFFR"
SERIES_NAME = "Effective Federal Funds Rate"
EXPECTED_TYPE = "EFFR"
UNIT = "Percent"
PUBLISHED_AT_BASIS = "effr_next_business_day_plus_1d"
LATEST_ROW_BASIS = "effr_latest_row_fetch_date"
RELEASE_LAG_DAYS = 1  # published_at = release date + 1 day, 23:59:59 PKT

MAX_GAP_DAYS = 4  # calendar days between consecutive dates; more warns
KNOWN_GAPS: frozenset[date] = frozenset()  # first dates of reviewed gaps (none in the first run)
#: Weekend dates reviewed and kept as returned, not warned again.
#: 2003-07-20 (Sunday): one record whose values (1.02, intraday 0.94-1.13,
#: std dev 0.02, target 1.00) are identical to Monday 2003-07-21's; Friday
#: 2003-07-18 and Monday 2003-07-21 are both present, nothing is duplicated
#: by date. Looks like a misdated copy of the Monday record (raw search file
#: of 2026-11-23, reviewed by Mahrukh).
KNOWN_WEEKEND_DATES: frozenset[date] = frozenset({date(2003, 7, 20)})
#: Large moves with no target change, reviewed as real and not warned again
#: (Mahrukh, 2026-11-23, first run): 2000-12-29 (-1.12 pp, year end),
#: 2001-01-02 (+1.26), 2001-09-20 (+1.03, after 9/11), 2008-01-02 (+1.05),
#: 2008-10-07 (+1.01); all from the brokered-trade period before 2016.
KNOWN_MOVES: frozenset[date] = frozenset(
    date.fromisoformat(d) for d in ("2000-12-29", "2001-01-02", "2001-09-20", "2008-01-02", "2008-10-07")
)
RATE_MIN, RATE_MAX = 0.0, 20.0  # percent; outside warns
NEW_METHOD_FROM = date(2016, 3, 1)  # FR 2420 volume-weighted median from here
MOVE_WARN_PP = 0.30  # daily change from NEW_METHOD_FROM
MOVE_WARN_PP_BEFORE = 1.00  # daily change before NEW_METHOD_FROM
TARGET_RANGE_FROM = date(2008, 12, 16)  # FOMC target range era
TARGET_BAND_PP = 0.10  # tolerance outside the target range
STALE_AFTER_DAYS = 7

#: Field names in the probe (manual download 2026-11-23). ``footnoteId`` is
#: in the API spec and appears on footnoted records.
PROBE_FIELDS = frozenset(
    {
        "effectiveDate",
        "type",
        "percentRate",
        "percentPercentile1",
        "percentPercentile25",
        "percentPercentile75",
        "percentPercentile99",
        "targetRateFrom",
        "targetRateTo",
        "volumeInBillions",
        "revisionIndicator",
        "footnoteId",
    }
)
#: Fields of records before 2016-03-01. The first run (2026-11-23) had
#: intraDayLow, intraDayHigh and stdDeviation, with targetRateFrom only
#: before 2008-12-16 and targetRateFrom/targetRateTo after; targetRange (API
#: spec) did not occur but is still accepted. Not allowed in the probe.
OLD_FIELDS = frozenset({"targetRange", "intraDayLow", "intraDayHigh", "stdDeviation"})
KNOWN_FIELDS = PROBE_FIELDS | OLD_FIELDS
REQUIRED_FIELDS = ("effectiveDate", "type", "percentRate")

#: API field -> interim/processed column. Numeric unless listed in TEXT_COLUMNS.
COLUMNS = {
    "effectiveDate": "effective_date",
    "type": "type",
    "percentRate": "percent_rate",
    "percentPercentile1": "percentile_1",
    "percentPercentile25": "percentile_25",
    "percentPercentile75": "percentile_75",
    "percentPercentile99": "percentile_99",
    "targetRateFrom": "target_rate_from",
    "targetRateTo": "target_rate_to",
    "volumeInBillions": "volume_billions",
    "revisionIndicator": "revision_indicator",
    "footnoteId": "footnote_id",
    "targetRange": "target_range",
    "intraDayLow": "intraday_low",
    "intraDayHigh": "intraday_high",
    "stdDeviation": "std_deviation",
}
TEXT_COLUMNS = frozenset({"effective_date", "type", "revision_indicator", "footnote_id", "target_range"})
OPTIONAL_NUMERIC = tuple(
    c for c in COLUMNS.values() if c not in TEXT_COLUMNS and c != "percent_rate"
)

CITATION = (
    "Federal Reserve Bank of New York, Effective Federal Funds Rate (EFFR), daily, {first} to "
    "{last}, New York Fed Markets Data API (markets.newyorkfed.org), retrieved {retrieved}. "
    "© {year} Federal Reserve Bank of New York. Content from the New York Fed subject to the "
    "Terms of Use at newyorkfed.org."
)
#: Required by the NY Fed Terms of Use (Reference Rates) wherever the EFFR is
#: shown to users.
REFERENCE_RATE_NOTICE = (
    "The Effective Federal Funds Rate (EFFR) is subject to the Terms of Use posted at "
    "newyorkfed.org. The New York Fed is not responsible for publication of the EFFR by this "
    "project, does not sanction or endorse any particular republication, and has no liability "
    "for your use."
)

NOTES = (
    "New York Fed Markets Data API, unsecured reference rate EFFR, daily (business days), "
    "percent. One row per date with the rate, percentiles, target range and volume where the "
    "API gives them (records before 2016-03-01 carry intraDayLow/intraDayHigh/stdDeviation "
    "instead of percentiles and volume; before 2008-12-16 a single target in targetRateFrom). "
    "2003-07-20 is a Sunday record identical to 2003-07-21, kept as returned. observation_status = revisionIndicator, status_comments = "
    "footnoteId, as returned (empty -> null). Not FRED (Proposed ADR-0007). Re-queryable; the NY "
    "Fed revises only on the same day except in extraordinary circumstances, but each run's raw "
    "responses are kept (append_only_snapshot) and revisions against the previous run are "
    "warned. published_at = next EFFR date + 1 day 23:59:59 Asia/Karachi in UTC (basis "
    "effr_next_business_day_plus_1d); latest row: fetch date 23:59:59 PKT (basis "
    "effr_latest_row_fetch_date). Showing the EFFR to users requires the NY Fed "
    "reference-rate notice (extra.reference_rate_notice)."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = pa.schema(
    [(c, pa.string()) for c in COLUMNS.values()]
    + [("extra_fields", pa.string()), ("raw_file", pa.string()), ("run_id", pa.string())]
)
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),
        ("series_code", pa.string()),
        ("observation_date", pa.date32()),
        ("value", pa.float64()),  # percent
        ("unit", pa.string()),
        ("percentile_1", pa.float64()),
        ("percentile_25", pa.float64()),
        ("percentile_75", pa.float64()),
        ("percentile_99", pa.float64()),
        ("target_rate_from", pa.float64()),
        ("target_rate_to", pa.float64()),
        ("volume_billions", pa.float64()),  # billions of USD
        ("target_range", pa.string()),  # records before 2016, as returned
        ("intraday_low", pa.float64()),
        ("intraday_high", pa.float64()),
        ("std_deviation", pa.float64()),
        ("observation_status", pa.string()),  # revisionIndicator
        ("status_comments", pa.string()),  # footnoteId
        ("published_at", _TS),
        ("published_at_basis", pa.string()),
        ("source_endpoint", pa.string()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return um.today_pkt()


def _blank(v: str | None) -> bool:
    return v is None or not v.strip()


def _text(v: Any) -> str | None:
    """JSON value -> text. Numbers arrive as their JSON text (parse_float=str)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _load(body: bytes, what: str) -> Any:
    try:
        return json.loads(body, parse_float=str, parse_int=str)
    except ValueError:
        raise UsMacroError(f"{what}: response is not JSON") from None


def _records(payload: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("refRates"), list):
        shape = sorted(payload) if isinstance(payload, Mapping) else type(payload).__name__
        raise UsMacroError(f"{what}: no refRates list; top level: {shape}")
    recs = payload["refRates"]
    if not all(isinstance(r, Mapping) for r in recs):
        raise UsMacroError(f"{what}: refRates holds non-objects")
    return [dict(r) for r in recs]


def check_probe(payload: Any) -> dict[str, Any]:
    """The probe's one record, after the shape checks."""
    recs = _records(payload, "probe")
    if len(recs) != 1:
        raise UsMacroError(f"probe: expected 1 record, got {len(recs)}")
    rec = recs[0]
    missing = [f for f in REQUIRED_FIELDS if f not in rec]
    if missing:
        raise UsMacroError(f"probe: record lacks {missing}; fields: {sorted(rec)}")
    unknown = sorted(set(rec) - PROBE_FIELDS)
    if unknown:
        raise UsMacroError(f"probe: unknown field names {unknown} (expected a subset of {sorted(PROBE_FIELDS)})")
    if rec["type"] != EXPECTED_TYPE:
        raise UsMacroError(f"probe: type {rec['type']!r} != {EXPECTED_TYPE!r}")
    return rec


def interim_rows(payload: Any, raw_file: str, run_id: str) -> list[dict[str, Any]]:
    rows = []
    for rec in _records(payload, "search"):
        row: dict[str, Any] = {col: _text(rec.get(field)) for field, col in COLUMNS.items()}
        extra = {k: v for k, v in rec.items() if k not in COLUMNS}
        row["extra_fields"] = json.dumps(extra, sort_keys=True) if extra else None
        row.update(raw_file=raw_file, run_id=run_id)
        rows.append(row)
    return rows


def _target(row: Mapping[str, Any]) -> tuple[float | None, float | None, str | None]:
    """The FOMC target as returned: (from, to, range text). Before 2008-12-16
    the API gives ``targetRateFrom`` only (a single target); from then on
    ``targetRateFrom`` and ``targetRateTo``."""
    n = row["_nums"]
    text = None if _blank(row["target_range"]) else row["target_range"].strip()
    return n["target_rate_from"], n["target_rate_to"], text


def _num(row: Mapping[str, Any], col: str, day: date) -> float | None:
    raw = row[col]
    if _blank(raw):
        return None
    try:
        return ed.parse_value(raw)
    except ed.EasyDataError:
        raise UsMacroError(f"{day}: non-numeric {col} {raw!r}") from None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(
    interim: list[dict[str, Any]],
    probe: Mapping[str, Any],
    *,
    end_date: date,
    fetched_at: datetime,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    warnings: list[str] = []
    typed: list[dict[str, Any]] = []
    unknown: set[str] = set()
    for row in interim:
        if row["type"] != EXPECTED_TYPE:
            raise UsMacroError(f"record of type {row['type']!r} (expected {EXPECTED_TYPE!r}) on {row['effective_date']!r}")
        if _blank(row["effective_date"]):
            raise UsMacroError("record without effectiveDate")
        day = um.parse_iso_date(row["effective_date"], "effectiveDate")
        if _blank(row["percent_rate"]):
            raise UsMacroError(f"{day}: empty percentRate")
        value = _num(row, "percent_rate", day)
        nums = {c: _num(row, c, day) for c in OPTIONAL_NUMERIC}
        if row["extra_fields"]:
            unknown |= set(json.loads(row["extra_fields"]))
        typed.append({**row, "_date": day, "_value": value, "_nums": nums})

    typed.sort(key=lambda r: r["_date"])
    dates = [r["_date"] for r in typed]
    if len(typed) < 2:
        raise UsMacroError(f"{len(typed)} row(s) returned; expected the full history (was startDate ignored?)")
    dupes = um.duplicates(dates)
    if dupes:
        raise UsMacroError(f"duplicate effectiveDate values {dupes}")
    if dates[-1] > end_date:
        raise UsMacroError(f"observation after end_date: {dates[-1]}")

    if unknown:
        warnings.append(f"unknown field names in the full history (kept in interim extra_fields): {sorted(unknown)}")
    if dates[0] != EXPECTED_START:
        warnings.append(f"first observation {dates[0]} differs from expected start {EXPECTED_START}")
    weekend = um.weekend_dates(dates)
    new_weekend = [d for d in weekend if date.fromisoformat(d) not in KNOWN_WEEKEND_DATES]
    um.warn_list(warnings, "weekend date(s) not in KNOWN_WEEKEND_DATES", new_weekend)
    gap_list = um.gaps(dates, MAX_GAP_DAYS, KNOWN_GAPS)
    um.warn_list(warnings, f"gap(s) of more than {MAX_GAP_DAYS} days", gap_list)
    out_of_range = [f"{r['_date']} ({r['_value']:g})" for r in typed if not RATE_MIN <= r["_value"] <= RATE_MAX]
    um.warn_list(warnings, f"rate(s) outside {RATE_MIN:g}-{RATE_MAX:g}", out_of_range)
    points = [(r["_date"], r["_value"]) for r in typed]
    # A large move on a day the FOMC target changed (against the previous
    # record) is explained and not warned; unexplained moves still warn.
    target_changed = frozenset(cur["_date"] for prev, cur in zip(typed, typed[1:]) if _target(prev) != _target(cur))
    all_moves = um.large_moves(
        [p for p in points if p[0] >= NEW_METHOD_FROM], MOVE_WARN_PP, relative=False
    ) + um.large_moves([p for p in points if p[0] < NEW_METHOD_FROM], MOVE_WARN_PP_BEFORE, relative=False)
    explained = [m for m in all_moves if date.fromisoformat(m[:10]) in target_changed]
    reviewed = [m for m in all_moves if date.fromisoformat(m[:10]) in KNOWN_MOVES - target_changed]
    moves = [m for m in all_moves if date.fromisoformat(m[:10]) not in target_changed | KNOWN_MOVES]
    um.warn_list(
        warnings,
        f"daily change(s) above {MOVE_WARN_PP:g} pp (from {NEW_METHOD_FROM}) or {MOVE_WARN_PP_BEFORE:g} pp "
        "(before) with no target change, not in KNOWN_MOVES",
        moves,
    )
    outside_target = []
    disorder = []
    bad_volume = []
    for r in typed:
        n, d, v = r["_nums"], r["_date"], r["_value"]
        lo, hi = n["target_rate_from"], n["target_rate_to"]
        if d >= TARGET_RANGE_FROM and lo is not None and hi is not None:
            if not lo - TARGET_BAND_PP <= v <= hi + TARGET_BAND_PP:
                outside_target.append(f"{d} ({v:g} vs {lo:g}-{hi:g})")
        chain = [n["percentile_1"], n["percentile_25"], v, n["percentile_75"], n["percentile_99"]]
        present = [x for x in chain if x is not None]
        if any(a > b for a, b in zip(present, present[1:])):
            disorder.append(d.isoformat())
        if n["volume_billions"] is not None and n["volume_billions"] <= 0:
            bad_volume.append(d.isoformat())
    um.warn_list(warnings, f"rate(s) outside the target range +/- {TARGET_BAND_PP:g} pp", outside_target)
    um.warn_list(warnings, "percentiles out of order (p1 <= p25 <= rate <= p75 <= p99)", disorder)
    um.warn_list(warnings, "volume <= 0", bad_volume)
    staleness = um.stale(dates[-1], end_date, STALE_AFTER_DAYS)
    if staleness:
        warnings.append(staleness)

    # The probe's record must agree with the same date in the full history.
    probe_day = um.parse_iso_date(_text(probe["effectiveDate"]), "probe effectiveDate")
    probe_rate = ed.parse_value(_text(probe["percentRate"]))
    match = next((r for r in typed if r["_date"] == probe_day), None)
    if match is None:
        warnings.append(f"probe date {probe_day} is not in the full history")
    elif match["_value"] != probe_rate:
        warnings.append(f"probe rate {probe_rate:g} on {probe_day} differs from the full history ({match['_value']:g})")

    revised = [f"{r['_date']} ({r['revision_indicator']})" for r in typed if not _blank(r["revision_indicator"])]
    out = []
    for i, r in enumerate(typed):
        if i + 1 < len(typed):
            published, basis = um.published_at_us(typed[i + 1]["_date"], RELEASE_LAG_DAYS), PUBLISHED_AT_BASIS
        else:
            published, basis = um.end_of_day_pkt(end_date), LATEST_ROW_BASIS
        out.append(
            {
                "series_key": SERIES_KEY,
                "series_name": SERIES_NAME,
                "series_code": SERIES_KEY,
                "observation_date": r["_date"],
                "value": r["_value"],
                "unit": UNIT,
                **r["_nums"],
                "target_range": None if _blank(r["target_range"]) else r["target_range"],
                "observation_status": None if _blank(r["revision_indicator"]) else r["revision_indicator"],
                "status_comments": None if _blank(r["footnote_id"]) else r["footnote_id"],
                "published_at": published,
                "published_at_basis": basis,
                "source_endpoint": SEARCH_URL,
                "fetched_at": fetched_at,
                "raw_file": r["raw_file"],
                "run_id": r["run_id"],
            }
        )
    report = {
        "rows": len(out),
        "first_observation": dates[0].isoformat(),
        "last_observation": dates[-1].isoformat(),
        "weekend_dates": weekend,
        "known_weekend_dates": sorted(d.isoformat() for d in KNOWN_WEEKEND_DATES),
        "gaps": gap_list,
        "largest_gap": um.largest_gap(dates),
        "large_moves": moves,
        "large_moves_with_target_change": explained,
        "reviewed_large_moves": reviewed,
        "target_changes": len(target_changed),
        "outside_target_range": outside_target,
        "percentile_disorder": disorder,
        "revised_records": revised,
        "unknown_fields": sorted(unknown),
        "rows_with_old_fields": sum(r["_nums"]["intraday_low"] is not None or not _blank(r["target_range"]) for r in typed),
        "max_decimals": max(um.decimals(r["percent_rate"]) for r in typed),
    }
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

    # 1-2. Probe (checked before anything else), then the full history.
    probe_resp = um.get(PROBE_URL, what="EFFR probe", min_interval=MIN_INTERVAL, timeout=TIMEOUT)
    probe = check_probe(_load(probe_resp.content, "probe"))
    log.info("probe: %s %s %s", probe["type"], probe["effectiveDate"], probe["percentRate"])
    params = {"startDate": REQUEST_START.isoformat(), "endDate": end_date.isoformat()}
    search_resp = um.get(SEARCH_URL, params, what="EFFR search", min_interval=MIN_INTERVAL, timeout=TIMEOUT)

    # 3. Raw, unmodified.
    raw_dir = LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}"
    probe_path = ed.write_new(raw_dir / f"{run_id}_effr_last1.json", probe_resp.content)
    search_path = ed.write_new(raw_dir / f"{run_id}_effr_search.json", search_resp.content)
    run_files = [probe_path, search_path]
    search_rel = search_path.relative_to(LOCAL_ROOT).as_posix()

    # 4. Interim. Invalid JSON fails (after the raw file is kept).
    interim = interim_rows(_load(search_resp.content, "search"), search_rel, run_id)
    run_files.append(
        ed.write_table(
            pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA),
            LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet",
        )
    )

    # 5. Validate.
    processed, check_warnings, report = validate(interim, probe, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

    # 6. Revisions and revision-indicator/footnote changes against the previous run.
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
    run_files.append(ed.write_table(pa.Table.from_pylist(processed, schema=PROCESSED_SCHEMA), LOCAL_ROOT / PROCESSED_REL))

    # 7. Run log.
    first, last = processed[0]["observation_date"], processed[-1]["observation_date"]
    citation = CITATION.format(
        first=first.strftime("%d-%b-%Y"),
        last=last.strftime("%d-%b-%Y"),
        retrieved=fetched_at.strftime("%d-%b-%Y"),
        year=fetched_at.year,
    )
    rows_per_series = {SERIES_KEY: len(processed)}
    summary: dict[str, Any] = {
        "run_id": run_id,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "end_date": end_date.isoformat(),
        "git": git_info(),
        "requests": {
            "probe": {"url": PROBE_URL, "http_status": probe_resp.status, "bytes": len(probe_resp.content)},
            "search": {
                "url": SEARCH_URL,
                "params": params,
                "http_status": search_resp.status,
                "bytes": len(search_resp.content),
            },
        },
        "probe_record": {k: _text(v) for k, v in probe.items()},
        "rows": len(processed),
        "rows_per_series": rows_per_series,
        "checks": report,
        "revisions": revisions,
        "status_changes": statuses,
        "published_at_basis": [PUBLISHED_AT_BASIS, LATEST_ROW_BASIS],
        "citation": citation,
        "reference_rate_notice": REFERENCE_RATE_NOTICE,
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

    # 8. Upload, manifest.
    exit_code = ed.finish_run(
        key="",
        upload=upload,
        stream=STREAM,
        dataset=DATASET,
        local_root=LOCAL_ROOT,
        hf_prefix=HF_PREFIX,
        run_id=run_id,
        run_files=run_files,
        source=f"New York Fed Markets Data API <{SEARCH_URL}> and <{PROBE_URL}>, unsecured reference rate EFFR",
        row_count=len(processed),
        date_range=(first, last),
        notes=NOTES,
        extra={
            "rows_per_series": rows_per_series,
            "first_observation": report["first_observation"],
            "last_observation": report["last_observation"],
            "published_at_basis": [PUBLISHED_AT_BASIS, LATEST_ROW_BASIS],
            "citation": citation,
            "reference_rate_notice": REFERENCE_RATE_NOTICE,
            "last_run": {k: v for k, v in summary.items() if k not in ("warnings", "checks", "status_changes")},
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
    parser.add_argument("--no-upload", action="store_true", help="stage under data/macro/us_effr/ only; no HF_TOKEN needed")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
