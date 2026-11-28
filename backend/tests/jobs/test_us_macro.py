"""Tests for backend/jobs/us_macro.py. Offline only."""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402
from jobs import us_macro as um  # noqa: E402

EST = timezone(timedelta(hours=-5))
EDT = timezone(timedelta(hours=-4))


# --------------------------------------------------------------------------
# published_at
# --------------------------------------------------------------------------


def test_published_at_us_is_23_59_59_pkt_in_utc() -> None:
    assert um.published_at_us(date(2026, 1, 15), 1) == datetime(2026, 1, 16, 18, 59, 59, tzinfo=timezone.utc)
    assert um.published_at_us(date(2026, 7, 15), 0) == datetime(2026, 7, 15, 18, 59, 59, tzinfo=timezone.utc)
    assert um.end_of_day_pkt(date(2026, 12, 31)) == datetime(2026, 12, 31, 18, 59, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("us_date", [date(2026, 1, 15), date(2026, 7, 15), date(2026, 3, 8), date(2026, 11, 1)])
@pytest.mark.parametrize("tz", [EST, EDT])
def test_next_day_rule_is_after_every_instant_of_the_us_date(us_date: date, tz: timezone) -> None:
    last_instant = datetime.combine(us_date, datetime.max.time(), tzinfo=tz)
    assert um.published_at_us(us_date, 1) > last_instant
    # ...and the same-day rule is not (so +1 day is needed).
    assert um.published_at_us(us_date, 0) < last_instant


@pytest.mark.parametrize(
    ("d", "monday"),
    [
        (date(2026, 11, 9), date(2026, 11, 16)),  # Monday
        (date(2026, 11, 13), date(2026, 11, 16)),  # Friday
        (date(2026, 11, 15), date(2026, 11, 16)),  # Sunday
        (date(2026, 12, 31), date(2027, 1, 4)),  # Thursday, year end
    ],
)
def test_following_monday(d: date, monday: date) -> None:
    assert um.following_monday(d) == monday


@pytest.mark.parametrize("value", ["2026-11-20", "2026-11-20T00:00:00", " 2026-11-20 "])
def test_parse_iso_date_ok(value: str) -> None:
    assert um.parse_iso_date(value, "x") == date(2026, 11, 20)


@pytest.mark.parametrize("value", ["", None, "20-Nov-2026", "11/20/2026", "2026-11-20T15:30:00", "2026-13-01", 20261120])
def test_parse_iso_date_refused(value: Any) -> None:
    with pytest.raises(um.UsMacroError):
        um.parse_iso_date(value, "x")


# --------------------------------------------------------------------------
# XML and zip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>',
        b'<?xml version="1.0"?><!DOCTYPE a SYSTEM "http://example.invalid/a.dtd"><a/>',
    ],
)
def test_xml_with_dtd_refused(body: bytes) -> None:
    with pytest.raises(um.UsMacroError, match="DTD"):
        um.parse_xml(body, "x")
    with pytest.raises(um.UsMacroError, match="DTD"):
        list(um.iterparse_xml(body, "x"))


def test_xml_that_does_not_parse() -> None:
    with pytest.raises(um.UsMacroError, match="does not parse"):
        um.parse_xml(b"<a><b></a>", "x")
    with pytest.raises(um.UsMacroError, match="does not parse"):
        list(um.iterparse_xml(b"<a><b></a>", "x"))


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


EXPECTED = frozenset({"a.xml", "b.xsd"})


def test_read_zip_ok_and_extra_member_warns() -> None:
    members, warnings = um.read_zip(
        make_zip({"a.xml": b"<a/>", "b.xsd": b""}), expected=EXPECTED, required=("a.xml",), max_member_bytes=100, what="z"
    )
    assert members == {"a.xml": b"<a/>"} and warnings == []
    _, warnings = um.read_zip(
        make_zip({"a.xml": b"<a/>", "c.txt": b""}), expected=EXPECTED, required=("a.xml",), max_member_bytes=100, what="z"
    )
    assert len(warnings) == 1 and "differ from expected" in warnings[0]


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"not a zip", "not a zip"),
        (make_zip({"b.xsd": b""}), "lacks"),
        (make_zip({"../a.xml": b"<a/>"}), "path parts"),
        (make_zip({"dir/a.xml": b"<a/>"}), "path parts"),
        (make_zip({"a.xml": b"x" * 101}), "limit"),
    ],
)
def test_read_zip_refused(data: bytes, match: str) -> None:
    with pytest.raises(um.UsMacroError, match=match):
        um.read_zip(data, expected=EXPECTED, required=("a.xml",), max_member_bytes=100, what="z")


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def test_checks() -> None:
    d = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 9), date(2026, 11, 14)]
    assert um.weekend_dates(d) == ["2026-11-14"]
    assert um.gaps(d, 4) == ["2026-11-03 to 2026-11-09 (6 days)", "2026-11-09 to 2026-11-14 (5 days)"]
    assert um.gaps(d, 4, frozenset({date(2026, 11, 3)})) == ["2026-11-09 to 2026-11-14 (5 days)"]
    assert um.largest_gap(d) == {"from": "2026-11-03", "to": "2026-11-09", "days": 6}
    assert um.largest_gap(d[:1]) is None
    assert um.duplicates([d[0], d[1], d[0]]) == ["2026-11-02"]
    assert um.stale(date(2026, 11, 13), date(2026, 11, 23), 7) is not None
    assert um.stale(date(2026, 11, 16), date(2026, 11, 23), 7) is None
    assert um.decimals("4.1900") == 4 and um.decimals("100") == 0 and um.decimals(None) == 0


def test_large_moves() -> None:
    pts = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 103.0), (date(2026, 1, 5), 102.0)]
    assert um.large_moves(pts, 2.0, relative=True) == ["2026-01-02 (100 -> 103, +3.00%)"]
    assert um.large_moves(pts, 2.0, relative=True, known=frozenset({date(2026, 1, 2)})) == []
    assert um.large_moves(pts, 0.5, relative=False) == [
        "2026-01-02 (100 -> 103, +3.00 pp)",
        "2026-01-05 (103 -> 102, -1.00 pp)",
    ]


def test_warn_list() -> None:
    w: list[str] = []
    um.warn_list(w, "label", [])
    um.warn_list(w, "label", ["a", "b"])
    assert w == ["label: 2: a, b"]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    status = {"code": 200}

    def fetch(url: str, headers: Any, timeout: float, body: bytes | None = None) -> common.HttpResponse:
        if url.endswith("/robots.txt"):
            return common.HttpResponse(url, 404, {}, b"")
        assert headers["User-Agent"] == common.USER_AGENT
        return common.HttpResponse(url, status["code"], {}, b"ok")

    monkeypatch.setattr(common, "_fetch_once", fetch)
    monkeypatch.setattr(common, "_robots_cache", {})
    monkeypatch.setattr(common, "_wait_for_host", lambda host, interval: None)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    return status


def test_get_ok(fake_http: dict[str, int]) -> None:
    assert um.get("https://example.invalid/x", {"a": "1"}, what="x", min_interval=0, timeout=1).content == b"ok"


def test_get_403_says_not_to_change_user_agent(fake_http: dict[str, int]) -> None:
    fake_http["code"] = 403
    with pytest.raises(um.UsMacroError, match="do not change the User-Agent"):
        um.get("https://example.invalid/x", what="thing", min_interval=0, timeout=1)


def test_get_404(fake_http: dict[str, int]) -> None:
    fake_http["code"] = 404
    with pytest.raises(um.UsMacroError, match="thing failed: HTTP 404"):
        um.get("https://example.invalid/x", what="thing", min_interval=0, timeout=1)


# --------------------------------------------------------------------------
# get_bounded / _fetch_bounded (total time limit per response)
# --------------------------------------------------------------------------


class FakeResp:
    """Stands in for http.client.HTTPResponse as returned by the opener."""

    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None, status: int = 200) -> None:
        self.chunks = list(chunks)
        self.headers = email_message(headers or {})
        self.status = status
        self.closed = False

    def read1(self, n: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def geturl(self) -> str:
        return "https://example.invalid/final"

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True


def email_message(headers: dict[str, str]) -> Any:
    from email.message import Message

    m = Message()
    for k, v in headers.items():
        m[k] = v
    return m


class FakeOpener:
    def __init__(self, resp: Any) -> None:
        self.resp = resp
        self.requests: list[Any] = []

    def open(self, req: Any, timeout: float) -> Any:
        self.requests.append((req, timeout))
        if isinstance(self.resp, BaseException):
            raise self.resp
        return self.resp


def test_fetch_bounded_reads_all_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = FakeResp([b"<a>", b"</a>"], {"Content-Type": "text/xml"})
    opener = FakeOpener(resp)
    monkeypatch.setattr(common, "_opener", opener)
    out = um._fetch_bounded("https://example.invalid/x", {"User-Agent": "ua"}, 5, 60, 1000)
    assert out.content == b"<a></a>" and out.status == 200 and out.headers["content-type"] == "text/xml"
    assert resp.closed and opener.requests[0][1] == 5


def test_fetch_bounded_decompresses_gzip(monkeypatch: pytest.MonkeyPatch) -> None:
    import gzip

    body = gzip.compress(b"<feed>" + b"x" * 5000 + b"</feed>")
    monkeypatch.setattr(common, "_opener", FakeOpener(FakeResp([body[:100], body[100:]], {"Content-Encoding": "gzip"})))
    out = um._fetch_bounded("https://example.invalid/x", {}, 5, 60, 10_000)
    assert out.content.startswith(b"<feed>") and len(out.content) == 5013
    monkeypatch.setattr(common, "_opener", FakeOpener(FakeResp([body], {"Content-Encoding": "gzip"})))
    with pytest.raises(um.UsMacroError, match="larger than 1000 bytes"):
        um._fetch_bounded("https://example.invalid/x", {}, 5, 60, 1000)


def test_fetch_bounded_stops_a_trickle(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}

    def tick() -> float:
        clock["t"] += 10.0  # each read takes 10 s
        return clock["t"]

    resp = FakeResp([b"x"] * 1000)
    monkeypatch.setattr(common, "_opener", FakeOpener(resp))
    monkeypatch.setattr(um, "_clock", tick)
    with pytest.raises(um.Stalled) as info:
        um._fetch_bounded("https://example.invalid/x", {}, 5, 60, 10_000)
    assert 0 < info.value.received < 10 and info.value.seconds > 60
    assert resp.closed  # connection closed on the way out


def test_fetch_bounded_size_cap_and_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    monkeypatch.setattr(common, "_opener", FakeOpener(FakeResp([b"x" * 600, b"x" * 600])))
    with pytest.raises(um.UsMacroError, match="larger than 1000"):
        um._fetch_bounded("https://example.invalid/x", {}, 5, 60, 1000)
    err = urllib.error.HTTPError("https://example.invalid/x", 503, "busy", email_message({}), io.BytesIO(b""))
    monkeypatch.setattr(common, "_opener", FakeOpener(err))
    assert um._fetch_bounded("https://example.invalid/x", {}, 5, 60, 1000).status == 503


class Bounded:
    """Fake _fetch_bounded: returns ``plan`` items in order (body bytes, an
    int status, or an exception to raise) and records the calls."""

    def __init__(self) -> None:
        self.plan: list[Any] = []
        self.calls: list[tuple[str, dict[str, str], float, float, int]] = []

    def __call__(self, url: str, headers: Any, timeout: float, total: float, max_bytes: int) -> common.HttpResponse:
        self.calls.append((url, dict(headers), timeout, total, max_bytes))
        item = self.plan.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return common.HttpResponse(url, item, {}, b"")
        return common.HttpResponse(url, 200, {}, item)


@pytest.fixture()
def bounded(monkeypatch: pytest.MonkeyPatch, fake_http: dict[str, int]) -> Bounded:
    fake = Bounded()
    monkeypatch.setattr(um, "_fetch_bounded", fake)
    return fake


def _get_bounded(**kw: Any) -> common.HttpResponse:
    args: dict[str, Any] = dict(
        what="Treasury feed 1990", min_interval=0, timeout=5, total_timeout=60, attempts=3, retry_wait=0, max_bytes=1000
    )
    args.update(kw)
    return um.get_bounded("https://example.invalid/feed", {"year": "1990"}, **args)


def test_get_bounded_ok_sends_policy_headers(bounded: Bounded) -> None:
    bounded.plan.append(b"<a/>")
    assert _get_bounded(accept="text/xml").content == b"<a/>"
    url, headers, timeout, total, max_bytes = bounded.calls[0]
    assert url == "https://example.invalid/feed?year=1990"
    assert headers["User-Agent"] == common.USER_AGENT
    assert headers["Accept-Encoding"] == "gzip" and headers["Accept"] == "text/xml"
    assert (timeout, total, max_bytes) == (5, 60, 1000)


def test_get_bounded_retries_a_stall(bounded: Bounded, caplog: pytest.LogCaptureFixture) -> None:
    bounded.plan.extend([um.Stalled(1234, 61.0), 503, b"<a/>"])
    assert _get_bounded().content == b"<a/>"
    assert "Treasury feed 1990: stalled: 1234 bytes after 61 s (limit 60 s); retry 1/2" in caplog.text
    assert "Treasury feed 1990: HTTP 503; retry 2/2" in caplog.text


def test_get_bounded_fails_naming_what(bounded: Bounded) -> None:
    bounded.plan.extend([um.Stalled(1, 61.0), TimeoutError("timed out"), um.Stalled(2, 62.0)])
    with pytest.raises(um.UsMacroError) as info:
        _get_bounded()
    assert str(info.value) == (
        "Treasury feed 1990: no complete response after 3 attempt(s) (last: stalled: 2 bytes after 62 s (limit 60 s))"
    )


@pytest.mark.parametrize(("status", "match"), [(404, "HTTP 404$"), (403, "do not change the User-Agent")])
def test_get_bounded_non_retry_status_fails_at_once(bounded: Bounded, status: int, match: str) -> None:
    bounded.plan.extend([status, b"never"])
    with pytest.raises(um.UsMacroError, match=match):
        _get_bounded()
    assert len(bounded.calls) == 1  # not retried
