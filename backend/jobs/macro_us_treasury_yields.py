"""US Treasury daily par yield curve (all maturities) -> dataset
``us_treasury_yields`` (stream ``macro``).

Source: US Treasury interest-rate XML feed (no key),
``https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=YYYY``,
one request per year from 1990. Replaces FRED, whose terms forbid storing its
content (Proposed ADR-0007).

Long format: one row per maturity per date where the feed has a value, with
``series_code`` = the feed element (``BC_10YEAR`` etc.) from the fixed table
``SERIES``. ``BC_30YEARDISPLAY`` (a display copy of the 30-year) and ``Id``
are kept in raw and interim only.

Each run:

1. fetches the last complete year first (the probe; it always has about 250
   days, unlike the current year in early January) and aborts, before any
   other request and before writing anything, unless the feed title is
   ``DailyTreasuryYieldCurveRateData``, every entry has ``m:properties`` with
   ``d:NEW_DATE`` typed ``Edm.DateTime`` and maturity elements typed
   ``Edm.Double``, and ``d:BC_10YEAR`` is present (the shape is fixed from the
   manual download of the 2026 feed on 2026-11-23);
2. fetches every other year from 1990 to the current year (Pakistan time);
3. saves every response body to ``raw/fetch_date=D/<run>_par_yield_YYYY.xml``
   (requests ask for gzip; a gzip body is decompressed first, so the file is
   the XML document as served, otherwise unmodified);
4. writes parsed, unvalidated rows (one per entry per element, values as
   text) to ``interim/fetch_date=D/observations-<run>.parquet``;
5. validates and writes ``processed/us_treasury_yields.parquet``:

   * fails on: XML that does not parse or has a DTD, a wrong feed title, an
     entry without ``m:properties`` or ``NEW_DATE``, an unparseable date, a
     date outside the requested year, duplicate dates, dates after today, a
     wrong ``m:type``, a non-numeric value, a completed year with no entries,
     a year where the 10-year is missing on every date;
   * warns on: unknown element names, weekend dates, a gap of more than 4
     calendar days, a completed year with fewer than 240 dates, a year with
     exactly 300 dates (possible cap), an empty current year, a maturity
     missing on a date inside its span (except the reviewed
     ``KNOWN_MISSING_ALL``/``KNOWN_MISSING``), a value before its expected start or
     inside the 30-year gap, a first value different from the expected start,
     a value below -0.5 or above 20, a daily change above 1.00 pp (bills, up
     to 1 year) or 0.35 pp (2 years and longer) that is not in the reviewed
     ``KNOWN_MOVES`` (never measured across a series' publication gap), the latest date more than 7
     days old, and revisions since the previous run;

6. writes a run log, uploads this run's files to HF ``macro/us_treasury_yields/``
   in one commit, then writes ``backend/data_manifests/macro_us_treasury_yields.json``
   and uploads a copy.

``published_at`` = ``observation_date`` + 1 day at 23:59:59 PKT, in UTC
(basis ``treasury_same_day_plus_1d``). Treasury says par yields are usually
available by 6:00 p.m. ET on the same trading day (03:00 or 04:00 PKT the next
day); the rule also covers a delay until about 2 p.m. ET the next day. See
``jobs/us_macro.py`` for why no DST logic is needed. Later corrections are not
covered.

Each run makes one request per year (37 in 2026), spaced by 5 seconds, about
3-4 minutes, with an INFO line before and after each year. Each request goes
through ``us_macro.get_bounded``: the whole response must arrive within
``TOTAL_TIMEOUT`` (120 s), otherwise the connection is closed and the year is
retried after ``RETRY_WAIT`` (30 s), up to ``ATTEMPTS`` (3), then the run fails
naming the year. Nothing is written until every year has arrived. No rate
limit is documented; robots.txt allows everything.

Run from ``backend/``::

    python -m jobs.macro_us_treasury_yields              # fetch + upload (needs HF_TOKEN)
    python -m jobs.macro_us_treasury_yields --no-upload  # local only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from jobs import easydata as ed
from jobs import us_macro as um
from jobs.common import DATA_DIR, REPO_ROOT, git_info, hf_download, upload_to_hf
from jobs.us_macro import UsMacroError

log = logging.getLogger("jobs.macro_us_treasury_yields")

STREAM = "macro"
DATASET = "us_treasury_yields"
HF_PREFIX = "macro/us_treasury_yields"
LOCAL_ROOT = DATA_DIR / "macro" / "us_treasury_yields"
PROCESSED_REL = "processed/us_treasury_yields.parquet"
SCRIPT = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

FEED_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
FEED_DATA = "daily_treasury_yield_curve"
FIRST_YEAR = 1990
# Requests go through us_macro.get_bounded: the first run (2026-11-23) hung
# for over 10 minutes inside one year's read, because common.http_get's
# timeout bounds each socket read, not the whole response.
MIN_INTERVAL = 5.0  # seconds between requests (2.0 in the first run, which stalled)
TIMEOUT = 30.0  # per socket operation (connect, each read)
TOTAL_TIMEOUT = 120.0  # per attempt, whole response; a year is about 350 KB uncompressed
ATTEMPTS = 3
RETRY_WAIT = 30.0  # seconds before retrying a stalled or failed year
MAX_BYTES = 20 * 1024 * 1024  # per year, after decompression
ACCEPT = "application/xml, text/xml;q=0.9, */*;q=0.1"

EXPECTED_TITLE = "DailyTreasuryYieldCurveRateData"
ATOM = "{http://www.w3.org/2005/Atom}"
M = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
D = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
DATE_ELEMENT = "NEW_DATE"
DATE_TYPE = "Edm.DateTime"
VALUE_TYPE = "Edm.Double"
REQUIRED_SERIES = "BC_10YEAR"
UNIT = "Percent"
PUBLISHED_AT_BASIS = "treasury_same_day_plus_1d"
RELEASE_LAG_DAYS = 1

MAX_GAP_DAYS = 4
KNOWN_GAPS: frozenset[date] = frozenset()  # first dates of reviewed gaps (none in the first full run)
MIN_ROWS_FULL_YEAR = 240
PAGE_CAP = 300  # the feed's default page size for "all"; a year this size may be truncated
VALUE_MIN, VALUE_MAX = -0.5, 20.0
MOVE_WARN_PP_BILLS = 1.00  # maturities up to 12 months
MOVE_WARN_PP = 0.35  # 2 years and longer
STALE_AFTER_DAYS = 7


@dataclass(frozen=True)
class Series:
    code: str  # feed element
    label: str  # as on Treasury's web table
    months: float
    start: date  # expected first value
    gap: tuple[date, date] | None = None  # inclusive span with no publication


# Element names and order from the 2026 feed (manual download 2026-11-23).
# Start dates: 1-month (2001-07-31), 2-month (2018-10-16), 20-year
# (1993-10-01) and 1990-01-02 from knowledge, to confirm on the first run;
# 4-month (2022-10-19) and 1.5-month (2025-02-18) from Treasury's developer
# notice; the 30-year was not published from 2002-02-18 to 2006-02-08
# (ceased 18 Feb 2002, resumed 9 Feb 2006, Treasury's interest rate page).
_START = date(1990, 1, 2)
SERIES: tuple[Series, ...] = (
    Series("BC_1MONTH", "1 Mo", 1, date(2001, 7, 31)),
    Series("BC_1_5MONTH", "1.5 Mo", 1.5, date(2025, 2, 18)),
    Series("BC_2MONTH", "2 Mo", 2, date(2018, 10, 16)),
    Series("BC_3MONTH", "3 Mo", 3, _START),
    Series("BC_4MONTH", "4 Mo", 4, date(2022, 10, 19)),
    Series("BC_6MONTH", "6 Mo", 6, _START),
    Series("BC_1YEAR", "1 Yr", 12, _START),
    Series("BC_2YEAR", "2 Yr", 24, _START),
    Series("BC_3YEAR", "3 Yr", 36, _START),
    Series("BC_5YEAR", "5 Yr", 60, _START),
    Series("BC_7YEAR", "7 Yr", 84, _START),
    Series("BC_10YEAR", "10 Yr", 120, _START),
    Series("BC_20YEAR", "20 Yr", 240, date(1993, 10, 1)),
    Series("BC_30YEAR", "30 Yr", 360, _START, (date(2002, 2, 18), date(2006, 2, 8))),
)
BY_CODE = {s.code: s for s in SERIES}


def _dates(*isos: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(s) for s in isos)


# Reviewed by Mahrukh after the first full run (2026-11-23, run
# 20261123T124214Z, 37 years, 100,160 rows); not warned again.
#: Dates listed in the feed with no value for every maturity in span.
#: 2010-10-11 (Columbus Day 2010) is an entry with no values at all.
KNOWN_MISSING_ALL = _dates("2010-10-11")
#: Dates with no value for one maturity: 3-month bill yields not published
#: on three days in December 2008 (crisis, near-zero bill rates).
KNOWN_MISSING: dict[str, frozenset[date]] = {
    "BC_3MONTH": _dates("2008-12-10", "2008-12-18", "2008-12-24"),
}
#: Daily changes above the thresholds, reviewed as real (event names are
#: context added when listing them, not checked against a source): 1994-04-04
#: (bond market sell-off), 2001-09-13 (reopening after 9/11), 2007-08-16
#: (bill flight to quality), 2008-09-15/19/29 (Lehman, the TARP proposal and
#: its first House vote), 2009-03-18 (FOMC announces Treasury purchases),
#: 2009-06-05 (payrolls), 2022-06-13 (CPI surprise; +0.35 pp, just above the
#: threshold in floating point), 2023-03-13 (SVB), 2023-05-04 (debt-ceiling
#: bills).
KNOWN_MOVES: dict[str, frozenset[date]] = {
    "BC_1MONTH": _dates("2007-08-16", "2008-09-15", "2023-05-04"),
    "BC_2YEAR": _dates("1994-04-04", "2001-09-13", "2008-09-15", "2008-09-19", "2008-09-29", "2009-06-05", "2023-03-13"),
    "BC_3YEAR": _dates("1994-04-04", "2001-09-13", "2008-09-15", "2008-09-19", "2008-09-29", "2022-06-13", "2023-03-13"),
    "BC_5YEAR": _dates("1994-04-04", "2001-09-13", "2008-09-15", "2009-03-18"),
    "BC_7YEAR": _dates("1994-04-04", "2009-03-18"),
    "BC_10YEAR": _dates("1994-04-04", "2009-03-18"),
}
OTHER_ELEMENTS = frozenset({"Id", DATE_ELEMENT, "BC_30YEARDISPLAY"})
KNOWN_ELEMENTS = frozenset(BY_CODE) | OTHER_ELEMENTS
EXPECTED_FIRST_DATE = _START

CITATION = (
    "U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates, {first} to {last}, "
    "home.treasury.gov interest rate XML feed (daily_treasury_yield_curve), retrieved {retrieved}."
)

NOTES = (
    "US Treasury interest-rate XML feed, daily Treasury par yield curve, all maturities "
    "(1 month to 30 years), percent, one row per maturity per date with a value (long format; "
    "series_code is the feed element, e.g. BC_10YEAR). BC_30YEARDISPLAY and Id are kept in raw "
    "and interim only. Maturities start at different dates and the 30-year was not published "
    "2002-02-18 to 2006-02-08 (fixed table in the loader). Methodology changed to monotone "
    "convex on 2021-12-06. Not FRED (Proposed ADR-0007). Treasury may correct values without "
    "notice, so each run's raw responses are kept (append_only_snapshot) and revisions against "
    "the previous run are warned. No explicit Treasury licence was found; use rests on 17 U.S.C. "
    "105 (no US copyright in federal works). published_at = observation_date + 1 day 23:59:59 "
    "Asia/Karachi in UTC (basis treasury_same_day_plus_1d)."
)

_TS = pa.timestamp("s", tz="UTC")
INTERIM_SCHEMA = pa.schema(
    [
        ("requested_year", pa.string()),
        ("entry_index", pa.int32()),
        ("entry_id", pa.string()),
        ("new_date", pa.string()),  # as returned
        ("element", pa.string()),
        ("m_type", pa.string()),
        ("m_null", pa.string()),
        ("value", pa.string()),  # as returned
        ("feed_updated", pa.string()),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)
PROCESSED_SCHEMA = pa.schema(
    [
        ("series_key", pa.string()),
        ("series_name", pa.string()),
        ("series_code", pa.string()),
        ("maturity_label", pa.string()),
        ("maturity_months", pa.float64()),
        ("observation_date", pa.date32()),
        ("value", pa.float64()),  # percent
        ("unit", pa.string()),
        ("published_at", _TS),
        ("published_at_basis", pa.string()),
        ("feed_updated", pa.string()),  # the feed's <updated>, as returned
        ("source_endpoint", pa.string()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("run_id", pa.string()),
    ]
)


def _today() -> date:
    """Today in Pakistan time. A function so tests can fix the date."""
    return um.today_pkt()


def year_params(year: int) -> dict[str, str]:
    return {"data": FEED_DATA, "field_tdr_date_value": str(year)}


def year_url(year: int) -> str:
    return f"{FEED_URL}?{urllib.parse.urlencode(year_params(year))}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class Feed:
    year: int
    title: str | None
    updated: str | None
    entries: list[list[tuple[str, str | None, str | None, str | None]]]  # (element, m:type, m:null, text)


def parse_feed(body: bytes, year: int) -> Feed:
    what = f"Treasury feed {year}"
    root = um.parse_xml(body, what)
    if root.tag != f"{ATOM}feed":
        raise UsMacroError(f"{what}: root element {root.tag!r} is not an Atom feed")
    title = root.findtext(f"{ATOM}title")
    updated = root.findtext(f"{ATOM}updated")
    entries = []
    for i, entry in enumerate(root.findall(f"{ATOM}entry")):
        props = entry.find(f"{ATOM}content/{M}properties")
        if props is None:
            raise UsMacroError(f"{what}: entry {i} has no m:properties")
        entries.append(
            [(_local(el.tag), el.get(f"{M}type"), el.get(f"{M}null"), el.text) for el in props]
        )
    return Feed(year=year, title=title, updated=updated, entries=entries)


def check_shape(feed: Feed) -> list[str]:
    """Title, element types and the 10-year element. Unknown element names
    are returned as warnings; everything else stops the run."""
    what = f"Treasury feed {feed.year}"
    if (feed.title or "").strip() != EXPECTED_TITLE:
        raise UsMacroError(f"{what}: feed title {feed.title!r} != {EXPECTED_TITLE!r}")
    unknown: set[str] = set()
    for i, elements in enumerate(feed.entries):
        names = [e[0] for e in elements]
        if names.count(DATE_ELEMENT) != 1:
            raise UsMacroError(f"{what}: entry {i} has {names.count(DATE_ELEMENT)} {DATE_ELEMENT} elements")
        for name, mtype, _null, _text in elements:
            if name == DATE_ELEMENT and mtype != DATE_TYPE:
                raise UsMacroError(f"{what}: {DATE_ELEMENT} has m:type {mtype!r} != {DATE_TYPE!r}")
            if name.startswith("BC_") and mtype != VALUE_TYPE:
                raise UsMacroError(f"{what}: {name} has m:type {mtype!r} != {VALUE_TYPE!r}")
            if name not in KNOWN_ELEMENTS:
                unknown.add(name)
    return [f"{what}: unknown element names {sorted(unknown)} (kept in raw and interim)"] if unknown else []


def check_probe(feed: Feed) -> list[str]:
    warnings = check_shape(feed)
    if not feed.entries:
        raise UsMacroError(f"Treasury feed {feed.year} (probe): no entries")
    if not any(e[0] == REQUIRED_SERIES for entry in feed.entries for e in entry):
        raise UsMacroError(f"Treasury feed {feed.year} (probe): no {REQUIRED_SERIES} element")
    return warnings


def interim_rows(feed: Feed, raw_file: str, run_id: str) -> list[dict[str, Any]]:
    rows = []
    for i, elements in enumerate(feed.entries):
        by_name = {e[0]: e for e in elements}
        new_date = by_name[DATE_ELEMENT][3]
        entry_id = by_name["Id"][3] if "Id" in by_name else None
        for name, mtype, null, text in elements:
            if name in (DATE_ELEMENT, "Id"):
                continue
            rows.append(
                {
                    "requested_year": str(feed.year),
                    "entry_index": i,
                    "entry_id": entry_id,
                    "new_date": new_date,
                    "element": name,
                    "m_type": mtype,
                    "m_null": null,
                    "value": text,
                    "feed_updated": feed.updated,
                    "raw_file": raw_file,
                    "run_id": run_id,
                }
            )
        if len(elements) == 1 + ("Id" in by_name):  # a date with no values at all
            rows.append(
                {
                    "requested_year": str(feed.year),
                    "entry_index": i,
                    "entry_id": entry_id,
                    "new_date": new_date,
                    "element": None,
                    "m_type": None,
                    "m_null": None,
                    "value": None,
                    "feed_updated": feed.updated,
                    "raw_file": raw_file,
                    "run_id": run_id,
                }
            )
    return rows


def _in_gap(s: Series, d: date) -> bool:
    return s.gap is not None and s.gap[0] <= d <= s.gap[1]


def _segments(s: Series, points: list[tuple[date, float]]) -> list[list[tuple[date, float]]]:
    """``points`` split at the series' publication gap (before / after).
    Values inside the gap, if any, are warned about separately and are left
    out here."""
    if s.gap is None:
        return [points]
    return [[p for p in points if p[0] < s.gap[0]], [p for p in points if p[0] > s.gap[1]]]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(
    interim: list[dict[str, Any]],
    *,
    years: list[int],
    end_date: date,
    fetched_at: datetime,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    warnings: list[str] = []
    # (year, entry_index) -> date; (date, code) -> value
    entry_dates: dict[tuple[str, int], date] = {}
    values: dict[str, dict[date, tuple[float, dict[str, Any]]]] = defaultdict(dict)
    for row in interim:
        key = (row["requested_year"], row["entry_index"])
        if key not in entry_dates:
            day = um.parse_iso_date(row["new_date"], f"Treasury feed {row['requested_year']} {DATE_ELEMENT}")
            if day.year != int(row["requested_year"]):
                raise UsMacroError(f"Treasury feed {row['requested_year']}: entry dated {day} is outside the requested year")
            entry_dates[key] = day
        day = entry_dates[key]
        code = row["element"]
        if code not in BY_CODE:
            continue
        if (row["m_null"] or "").strip().lower() == "true" or row["value"] is None or not row["value"].strip():
            continue
        try:
            v = ed.parse_value(row["value"])
        except ed.EasyDataError:
            raise UsMacroError(f"{code} {day}: non-numeric value {row['value']!r}") from None
        values[code][day] = (v, row)

    dupes = um.duplicates(entry_dates.values())
    if dupes:
        raise UsMacroError(f"duplicate dates {dupes}")
    dates = sorted(entry_dates.values())
    if len(dates) < 2:
        raise UsMacroError(f"{len(dates)} date(s) returned")
    if dates[-1] > end_date:
        raise UsMacroError(f"observation after end_date: {dates[-1]}")

    per_year = {y: 0 for y in years}
    for d in dates:
        per_year[d.year] = per_year.get(d.year, 0) + 1
    current = end_date.year
    for y in years:
        n = per_year[y]
        if n == 0:
            if y == current:
                warnings.append(f"no dates for the current year {y} yet")
                continue
            raise UsMacroError(f"completed year {y} has no dates")
        if y < current and n < MIN_ROWS_FULL_YEAR:
            warnings.append(f"year {y} has {n} dates (fewer than {MIN_ROWS_FULL_YEAR})")
        if n == PAGE_CAP:
            warnings.append(f"year {y} has exactly {PAGE_CAP} dates (possible cap)")
        if not any(d.year == y for d in values[REQUIRED_SERIES]):
            raise UsMacroError(f"year {y}: {REQUIRED_SERIES} is missing on every date")

    if dates[0] != EXPECTED_FIRST_DATE:
        warnings.append(f"first date {dates[0]} differs from expected {EXPECTED_FIRST_DATE}")
    weekend = um.weekend_dates(dates)
    um.warn_list(warnings, "weekend date(s)", weekend)
    gap_list = um.gaps(dates, MAX_GAP_DAYS, KNOWN_GAPS)
    um.warn_list(warnings, f"gap(s) of more than {MAX_GAP_DAYS} days", gap_list)
    staleness = um.stale(dates[-1], end_date, STALE_AFTER_DAYS)
    if staleness:
        warnings.append(staleness)

    report: dict[str, Any] = {
        "dates": len(dates),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "dates_per_year": {str(y): per_year[y] for y in sorted(per_year)},
        "weekend_dates": weekend,
        "gaps": gap_list,
        "largest_gap": um.largest_gap(dates),
        "series": {},
    }
    out: list[dict[str, Any]] = []
    for s in SERIES:
        series_values = values[s.code]
        present = sorted(series_values)
        missing_all = [d for d in dates if d >= s.start and not _in_gap(s, d) and d not in series_values]
        known_missing = KNOWN_MISSING_ALL | KNOWN_MISSING.get(s.code, frozenset())
        missing = [d.isoformat() for d in missing_all if d not in known_missing]
        early = [d.isoformat() for d in present if d < s.start or _in_gap(s, d)]
        um.warn_list(warnings, f"{s.code}: missing on date(s) inside its span, not in KNOWN_MISSING", missing)
        um.warn_list(warnings, f"{s.code}: value(s) before its expected start {s.start} or inside its gap", early)
        if present and present[0] != s.start:
            warnings.append(f"{s.code}: first value {present[0]} differs from expected start {s.start}")
        if not present:
            warnings.append(f"{s.code}: no values")
        points = [(d, series_values[d][0]) for d in present]
        bad = [f"{d} ({v:g})" for d, v in points if not VALUE_MIN <= v <= VALUE_MAX]
        um.warn_list(warnings, f"{s.code}: value(s) outside {VALUE_MIN:g} to {VALUE_MAX:g}", bad)
        threshold = MOVE_WARN_PP_BILLS if s.months <= 12 else MOVE_WARN_PP
        known_moves = KNOWN_MOVES.get(s.code, frozenset())
        # Moves are measured within each segment of the series, never across
        # its publication gap: the first value after the gap has no previous
        # value (the 30-year resumed 2006-02-09 at 4.51 against 5.37 in 2002).
        moves: list[str] = []
        reviewed: list[str] = []
        for segment in _segments(s, points):
            for m in um.large_moves(segment, threshold, relative=False):
                (reviewed if date.fromisoformat(m[:10]) in known_moves else moves).append(m)
        um.warn_list(warnings, f"{s.code}: daily change(s) above {threshold:g} pp not in KNOWN_MOVES", moves)
        report["series"][s.code] = {
            "rows": len(present),
            "first": present[0].isoformat() if present else None,
            "last": present[-1].isoformat() if present else None,
            "missing_in_span": missing,
            "known_missing_in_span": [d.isoformat() for d in missing_all if d in known_missing],
            "before_start_or_in_gap": early,
            "large_moves": moves,
            "reviewed_large_moves": reviewed,
            "max_decimals": max((um.decimals(series_values[d][1]["value"]) for d in present), default=0),
        }
        for d in present:
            v, row = series_values[d]
            out.append(
                {
                    "series_key": s.code,
                    "series_name": f"Treasury par yield, {s.label}",
                    "series_code": s.code,
                    "maturity_label": s.label,
                    "maturity_months": float(s.months),
                    "observation_date": d,
                    "value": v,
                    "unit": UNIT,
                    "published_at": um.published_at_us(d, RELEASE_LAG_DAYS),
                    "published_at_basis": PUBLISHED_AT_BASIS,
                    "feed_updated": row["feed_updated"],
                    "source_endpoint": year_url(d.year),
                    "fetched_at": fetched_at,
                    "raw_file": row["raw_file"],
                    "run_id": row["run_id"],
                }
            )
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


def _fetch(year: int, n: int, total: int) -> tuple[bytes, int, str, float]:
    """One year, with a progress line before and after (year, seconds, bytes;
    entries are logged by the caller after parsing)."""
    log.info("Treasury %d: requesting (%d/%d)", year, n, total)
    start = um._clock()
    resp = um.get_bounded(
        FEED_URL,
        year_params(year),
        what=f"Treasury feed {year}",
        min_interval=MIN_INTERVAL,
        timeout=TIMEOUT,
        total_timeout=TOTAL_TIMEOUT,
        attempts=ATTEMPTS,
        retry_wait=RETRY_WAIT,
        max_bytes=MAX_BYTES,
        accept=ACCEPT,
    )
    seconds = um._clock() - start
    encoding = resp.headers.get("content-encoding", "none")
    log.info(
        "Treasury %d: received %d bytes in %.1f s (HTTP %d, content-encoding %s)",
        year,
        len(resp.content),
        seconds,
        resp.status,
        encoding,
    )
    return resp.content, resp.status, encoding, round(seconds, 1)


def _run(*, upload: bool) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    end_date = _today()
    warnings: list[str] = []
    years = list(range(FIRST_YEAR, end_date.year + 1))
    probe_year = end_date.year - 1

    # 1. Probe: the last complete year, checked before any other request.
    log.info(
        "Treasury: %d years (%d-%d), %.0f s apart, %.0f s limit per request, %d attempts",
        len(years), years[0], years[-1], MIN_INTERVAL, TOTAL_TIMEOUT, ATTEMPTS,
    )
    bodies: dict[int, tuple[bytes, int, str, float]] = {probe_year: _fetch(probe_year, 1, len(years))}
    feeds = {probe_year: parse_feed(bodies[probe_year][0], probe_year)}
    warnings += check_probe(feeds[probe_year])
    log.info(
        "Treasury %d (probe): %d entries, feed updated %s",
        probe_year,
        len(feeds[probe_year].entries),
        feeds[probe_year].updated,
    )

    # 2. Every other year; each is parsed and shape-checked before anything is written.
    n = 1
    for y in years:
        if y == probe_year:
            continue
        n += 1
        bodies[y] = _fetch(y, n, len(years))
        feeds[y] = parse_feed(bodies[y][0], y)
        warnings += check_shape(feeds[y])
        log.info("Treasury %d: %d entries", y, len(feeds[y].entries))

    # 3. Raw, unmodified.
    raw_dir = LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}"
    run_files: list[Path] = []
    raw_names: dict[int, str] = {}
    for y in years:
        path = ed.write_new(raw_dir / f"{run_id}_par_yield_{y}.xml", bodies[y][0])
        run_files.append(path)
        raw_names[y] = path.relative_to(LOCAL_ROOT).as_posix()

    # 4. Interim.
    interim = [row for y in years for row in interim_rows(feeds[y], raw_names[y], run_id)]
    run_files.append(
        ed.write_table(
            pa.Table.from_pylist(interim, schema=INTERIM_SCHEMA),
            LOCAL_ROOT / "interim" / f"fetch_date={fetch_date}" / f"observations-{run_id}.parquet",
        )
    )

    # 5. Validate.
    processed, check_warnings, report = validate(interim, years=years, end_date=end_date, fetched_at=fetched_at)
    warnings += check_warnings

    # 6. Revisions against the previous run.
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

    # 7. Run log.
    rows_per_series = {s.code: report["series"][s.code]["rows"] for s in SERIES}
    first = date.fromisoformat(report["first_date"])
    last = date.fromisoformat(report["last_date"])
    citation = CITATION.format(
        first=first.strftime("%d-%b-%Y"), last=last.strftime("%d-%b-%Y"), retrieved=fetched_at.strftime("%d-%b-%Y")
    )
    summary: dict[str, Any] = {
        "run_id": run_id,
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "end_date": end_date.isoformat(),
        "git": git_info(),
        "probe_year": probe_year,
        "requests": {
            str(y): {
                "url": year_url(y),
                "http_status": bodies[y][1],
                "bytes": len(bodies[y][0]),  # after decompression
                "content_encoding": bodies[y][2],
                "seconds": bodies[y][3],
                "entries": len(feeds[y].entries),
                "feed_updated": feeds[y].updated,
                "raw_file": raw_names[y],
            }
            for y in years
        },
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
        source=(
            f"US Treasury interest-rate XML feed <{FEED_URL}?data={FEED_DATA}&field_tdr_date_value=YYYY>, "
            f"daily par yield curve, years {years[0]}-{years[-1]} ({len(years)} requests)"
        ),
        row_count=len(processed),
        date_range=(first, last),
        notes=NOTES,
        extra={
            "rows_per_series": rows_per_series,
            "dates": report["dates"],
            "dates_per_year": report["dates_per_year"],
            "published_at_basis": PUBLISHED_AT_BASIS,
            "citation": citation,
            "last_run": {
                k: v for k, v in summary.items() if k not in ("warnings", "checks", "requests", "rows_per_series")
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
        "--no-upload", action="store_true", help="stage under data/macro/us_treasury_yields/ only; no HF_TOKEN needed"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(upload=not args.no_upload)


if __name__ == "__main__":
    sys.exit(main())
