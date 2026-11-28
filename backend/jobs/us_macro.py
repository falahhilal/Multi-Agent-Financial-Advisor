"""Shared helpers for the US macro loaders (``macro_us_effr.py``,
``macro_us_treasury_yields.py``, ``macro_us_dollar_index.py``).

These sources need no API key, so nothing here redacts secrets. The generic
file, revision and manifest helpers come from ``jobs/easydata.py`` and are
imported by the loaders as they are (``write_new``, ``write_table``,
``load_previous``, ``diff_revisions``, ``status_changes``, ``rows_summary``,
``finish_run`` with ``key=""``).

``published_at`` rule shared by all three loaders: a US release date plus a
fixed number of days, at 23:59:59 Pakistan time, stored in UTC. US Eastern
time is UTC-5 (EST) or UTC-4 (EDT); Pakistan time is UTC+5 with no daylight
saving time, so PKT is 10 or 9 hours ahead of ET. Every instant of US date D
therefore falls between D 09:00 PKT and D+1 09:59:59 PKT, and
D+1 23:59:59 PKT is after all of it whether or not DST is in effect. No DST
table is needed.

HTTP: :func:`get` wraps ``common.http_get``; :func:`get_bounded` adds a total
time limit per response with retries (see the comment above it), used by the
Treasury loader after its first run stalled.

XML is parsed with the standard library after refusing any document with a
DTD (``<!DOCTYPE`` or ``<!ENTITY``), so entity expansion cannot happen. Zip
members are read in memory, never extracted to disk, with a size cap.
"""

from __future__ import annotations

import http.client
import io
import logging
import time as _time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from jobs import common
from jobs.common import RETRY_STATUSES, HttpError, HttpResponse, RobotsDisallowedError, http_get
from jobs.easydata import PKT, listed

log = logging.getLogger("jobs.us_macro")

RELEASE_TIME = time(23, 59, 59)  # Pakistan time, on the published_at date
READ_SIZE = 64 * 1024  # bytes per read1() call in get_bounded

#: Monotonic clock used by get_bounded; a module attribute so tests can fake it.
_clock = _time.monotonic


class UsMacroError(RuntimeError):
    """The run must stop."""


class Stalled(Exception):
    """A response did not complete within its total time limit."""

    def __init__(self, received: int, seconds: float) -> None:
        super().__init__(f"stalled: {received} bytes after {seconds:.0f} s")
        self.received = received
        self.seconds = seconds


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def get(
    url: str,
    params: Mapping[str, str] | None = None,
    *,
    what: str,
    min_interval: float,
    timeout: float,
) -> HttpResponse:
    """``http_get`` (robots.txt, User-Agent, rate limit, retries) with every
    failure re-raised as ``UsMacroError``."""
    try:
        return http_get(url, params=params, min_interval=min_interval, timeout=timeout)
    except HttpError as exc:
        message = f"{what} failed: {exc}"
        if exc.status == 403:
            message += " (do not change the User-Agent to get around this; raise it with the team)"
    except RobotsDisallowedError as exc:
        message = f"{what} refused by robots.txt: {exc}"
    except (OSError, ValueError) as exc:
        message = f"{what} failed: {type(exc).__name__}: {exc}"
    raise UsMacroError(message) from None


# Why get_bounded exists (2026-11-23): common.http_get passes ``timeout`` to
# urlopen, which sets the *socket* timeout. That bounds each connect and each
# recv separately, not the whole response: a chunked body that keeps
# trickling bytes (a CDN tarpitting a flagged client, or a slow server-side
# stream) never times out. The first Treasury run hung for over 10 minutes in
# r.read() with no retry warning, which fits a trickle, not a silent server.
# get_bounded keeps common's policy (robots.txt, the project User-Agent,
# per-host spacing, the https-only redirect handler) but reads with read1()
# and checks a total deadline between reads, closes the connection when it
# passes, and retries. It also asks for gzip (as browsers do), which cuts the
# bytes on the wire about tenfold; the body is decompressed before it is
# returned, so callers store the same XML document. It uses common's private
# helpers (_prepare, _wait_for_host, _opener) rather than editing common.py;
# the cleaner long-term fix is a total_timeout parameter in common.http_get,
# to be agreed with the team.


def _decode_gzip(body: bytes, max_bytes: int, what: str) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(body, max_bytes + 1)
    except zlib.error as exc:
        raise UsMacroError(f"{what}: gzip body does not decompress: {exc}") from None
    if len(out) > max_bytes or d.unconsumed_tail:
        raise UsMacroError(f"{what}: decompressed body is larger than {max_bytes} bytes")
    return out


def _fetch_bounded(
    url: str, headers: Mapping[str, str], timeout: float, total_timeout: float, max_bytes: int
) -> HttpResponse:
    """One GET whose whole response must arrive within ``total_timeout``
    seconds. Raises ``Stalled`` (connection closed) when it does not, and
    ``OSError``/``HTTPException`` on network errors. Non-2xx statuses are
    returned, not raised."""
    start = _clock()
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        r = common._opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        hdrs = exc.headers.items() if exc.headers is not None else []
        exc.close()
        return HttpResponse(url=exc.geturl() or url, status=exc.code, headers={k.lower(): v for k, v in hdrs}, content=b"")
    with r:
        chunks: list[bytes] = []
        received = 0
        while True:
            if _clock() - start > total_timeout:
                raise Stalled(received, _clock() - start)
            chunk = r.read1(READ_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > max_bytes:
                raise UsMacroError(f"response is larger than {max_bytes} bytes")
        resp_headers = {k.lower(): v for k, v in r.headers.items()}
        status, final_url = r.status, r.geturl()
    body = b"".join(chunks)
    if resp_headers.get("content-encoding", "").strip().lower() == "gzip":
        body = _decode_gzip(body, max_bytes, url)
    return HttpResponse(url=final_url, status=status, headers=resp_headers, content=body)


def get_bounded(
    url: str,
    params: Mapping[str, str],
    *,
    what: str,
    min_interval: float,
    timeout: float,
    total_timeout: float,
    attempts: int,
    retry_wait: float,
    max_bytes: int,
    accept: str = "*/*",
) -> HttpResponse:
    """GET with a total time limit per attempt, retries on stalls, network
    errors and 429/5xx, and a clear failure naming ``what`` after the last
    attempt. robots.txt, User-Agent and per-host spacing are common's."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    try:
        headers, interval = common._prepare(full, {"Accept": accept, "Accept-Encoding": "gzip"}, timeout, min_interval, True)
    except RobotsDisallowedError as exc:
        raise UsMacroError(f"{what} refused by robots.txt: {exc}") from None
    except (HttpError, OSError, ValueError) as exc:
        raise UsMacroError(f"{what} failed before the request: {exc}") from None
    host = urllib.parse.urlsplit(full).netloc.lower()
    last = "no attempt"
    for attempt in range(1, attempts + 1):
        common._wait_for_host(host, interval)
        try:
            resp = _fetch_bounded(full, headers, timeout, total_timeout, max_bytes)
        except Stalled as exc:
            last = f"{exc} (limit {total_timeout:g} s)"
        except (OSError, http.client.HTTPException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        except UsMacroError as exc:
            raise UsMacroError(f"{what}: {exc}") from None
        else:
            if 200 <= resp.status < 300:
                return resp
            last = f"HTTP {resp.status}"
            if resp.status not in RETRY_STATUSES:
                hint = " (do not change the User-Agent to get around this; raise it with the team)" if resp.status == 403 else ""
                raise UsMacroError(f"{what} failed: HTTP {resp.status}{hint}")
        if attempt < attempts:
            log.warning("%s: %s; retry %d/%d in %.0f s", what, last, attempt, attempts - 1, retry_wait)
            _time.sleep(retry_wait)
    raise UsMacroError(f"{what}: no complete response after {attempts} attempt(s) (last: {last})")


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def today_pkt() -> date:
    return datetime.now(PKT).date()


def published_at_us(us_date: date, days_after: int) -> datetime:
    """``us_date + days_after`` at 23:59:59 Pakistan time, as UTC."""
    local = datetime.combine(us_date + timedelta(days=days_after), RELEASE_TIME, tzinfo=PKT)
    return local.astimezone(timezone.utc)


def end_of_day_pkt(d: date) -> datetime:
    """``d`` at 23:59:59 Pakistan time, as UTC."""
    return published_at_us(d, 0)


def parse_iso_date(value: object, what: str) -> date:
    """``YYYY-MM-DD`` or ``YYYY-MM-DDT00:00:00`` only; anything else stops the run."""
    if not isinstance(value, str) or not value.strip():
        raise UsMacroError(f"{what}: unrecognised date {value!r}")
    s = value.strip()
    if "T" in s:
        s, _, clock = s.partition("T")
        if clock not in ("00:00:00", "00:00:00.000", "00:00"):
            raise UsMacroError(f"{what}: date has a time of day: {value!r}")
    try:
        if len(s) != 10:
            raise ValueError
        return date.fromisoformat(s)
    except ValueError:
        raise UsMacroError(f"{what}: unrecognised date {value!r}") from None


def following_monday(d: date) -> date:
    """The Monday after the Monday-to-Sunday week that contains ``d``."""
    return d + timedelta(days=7 - d.weekday())


# --------------------------------------------------------------------------
# XML and zip
# --------------------------------------------------------------------------


def check_no_dtd(data: bytes, what: str) -> None:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise UsMacroError(f"{what}: XML with a DTD or entity declaration is refused")


def parse_xml(data: bytes, what: str) -> ET.Element:
    check_no_dtd(data, what)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UsMacroError(f"{what}: XML does not parse: {exc}") from None


def iterparse_xml(data: bytes, what: str) -> Iterable[tuple[str, ET.Element]]:
    """``ET.iterparse`` over ``data`` (end events), after the DTD check.
    Parse errors stop the run."""
    check_no_dtd(data, what)
    try:
        yield from ET.iterparse(io.BytesIO(data), events=("end",))
    except ET.ParseError as exc:
        raise UsMacroError(f"{what}: XML does not parse: {exc}") from None


def read_zip(
    data: bytes,
    *,
    expected: frozenset[str],
    required: Sequence[str],
    max_member_bytes: int,
    what: str,
) -> tuple[dict[str, bytes], list[str]]:
    """Read the ``required`` members of a zip held in memory.

    Stops the run on a file that is not a zip, a member name with a path
    part, a required member that is missing or larger than
    ``max_member_bytes`` (checked on the bytes actually read, not only the
    header). A member set different from ``expected`` is a warning."""
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UsMacroError(f"{what}: response is not a zip file") from None
    with zf:
        names = [i.filename for i in zf.infolist()]
        bad = [n for n in names if "/" in n or "\\" in n or n.startswith(".") or ":" in n]
        if bad:
            raise UsMacroError(f"{what}: zip member names with path parts: {bad}")
        if len(set(names)) != len(names):
            raise UsMacroError(f"{what}: duplicate zip member names: {sorted(names)}")
        missing = [n for n in required if n not in names]
        if missing:
            raise UsMacroError(f"{what}: zip lacks {missing}; members: {sorted(names)}")
        if set(names) != expected:
            warnings.append(
                f"{what}: zip members {sorted(names)} differ from expected {sorted(expected)}"
            )
        out: dict[str, bytes] = {}
        for name in required:
            info = zf.getinfo(name)
            if info.file_size > max_member_bytes:
                raise UsMacroError(f"{what}: {name} is {info.file_size} bytes (limit {max_member_bytes})")
            with zf.open(info) as fh:
                body = fh.read(max_member_bytes + 1)
            if len(body) > max_member_bytes:
                raise UsMacroError(f"{what}: {name} is larger than {max_member_bytes} bytes")
            out[name] = body
    return out, warnings


# --------------------------------------------------------------------------
# Checks (each returns a list of short strings for warnings and the run log)
# --------------------------------------------------------------------------


def weekend_dates(dates: Iterable[date]) -> list[str]:
    return [d.isoformat() for d in dates if d.weekday() >= 5]


def gaps(dates: Sequence[date], max_days: int, known: frozenset[date] = frozenset()) -> list[str]:
    """Gaps of more than ``max_days`` calendar days between consecutive
    (sorted) dates. ``known`` holds the first date of gaps already reviewed."""
    return [
        f"{a} to {b} ({(b - a).days} days)"
        for a, b in zip(dates, dates[1:])
        if (b - a).days > max_days and a not in known
    ]


def largest_gap(dates: Sequence[date]) -> dict[str, Any] | None:
    if len(dates) < 2:
        return None
    a, b = max(zip(dates, dates[1:]), key=lambda p: (p[1] - p[0]).days)
    return {"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days}


def large_moves(
    points: Sequence[tuple[date, float]],
    threshold: float,
    *,
    relative: bool,
    known: frozenset[date] = frozenset(),
) -> list[str]:
    """Changes against the previous point above ``threshold`` (percentage
    points when ``relative`` is false, percent when true). Dates in ``known``
    are moves already reviewed and are not listed."""
    out = []
    for (_, prev), (d, cur) in zip(points, points[1:]):
        if d in known:
            continue
        if relative:
            if prev == 0:
                continue
            change = (cur / prev - 1) * 100
            if abs(change) > threshold:
                out.append(f"{d} ({prev:g} -> {cur:g}, {change:+.2f}%)")
        else:
            change = cur - prev
            if abs(change) > threshold:
                out.append(f"{d} ({prev:g} -> {cur:g}, {change:+.2f} pp)")
    return out


def stale(latest: date, today: date, max_days: int) -> str | None:
    days = (today - latest).days
    if days > max_days:
        return f"latest date {latest} is {days} days before {today} (more than {max_days})"
    return None


def duplicates(dates: Iterable[date]) -> list[str]:
    seen: set[date] = set()
    dupes: set[date] = set()
    for d in dates:
        (dupes if d in seen else seen).add(d)
    return sorted(d.isoformat() for d in dupes)


def warn_list(warnings: list[str], label: str, items: list[str]) -> None:
    """Append ``label: N item(s): a, b, ...`` when ``items`` is non-empty."""
    if items:
        warnings.append(f"{label}: {len(items)}: {listed(items)}")


def decimals(raw: str | None) -> int:
    if raw is None or "." not in raw:
        return 0
    return len(raw.strip().split(".", 1)[1])
