from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from jobs.company_pypsx import archive_frame, collect_prices, normalize_pypsx_prices


class FakeFrame:
    def to_json(self, **kwargs: object) -> str:
        return '{"synthetic":"test only"}'

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return [{"symbol": "HBL", "close": 1}]


def test_loader_is_injectable_offline_and_idempotent(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    def fetch(ticker: str, start: str, end: str) -> FakeFrame:
        calls.append((ticker, start, end))
        return FakeFrame()

    result = collect_prices(
        ["HBL", "HBL"], date(2015, 1, 1), date(2026, 11, 20), tmp_path,
        fetch=fetch, sleep=lambda _: None,
    )
    repeated = collect_prices(
        ["HBL"], date(2015, 1, 1), date(2026, 11, 20), tmp_path,
        fetch=fetch, sleep=lambda _: None,
    )
    assert calls == [("HBL", "2015-01-01", "2026-11-20")] * 2
    assert result.tickers[0].status == "complete"
    assert repeated.tickers[0].raw_path == result.tickers[0].raw_path
    assert len(list(tmp_path.iterdir())) == 1


def test_loader_records_per_ticker_failures_and_continues(tmp_path: Path) -> None:
    def fetch(ticker: str, start: str, end: str) -> FakeFrame:
        if ticker == "BAD":
            raise RuntimeError("synthetic failure")
        return FakeFrame()

    result = collect_prices(
        ["BAD", "HBL"], date(2020, 1, 1), date(2020, 1, 2), tmp_path,
        fetch=fetch, retries=0, sleep=lambda _: None,
    )
    assert [item.status for item in result.tickers] == ["failed", "complete"]


def test_raw_snapshot_is_content_addressed_and_never_overwritten(tmp_path: Path) -> None:
    frame = FakeFrame()
    path, digest = archive_frame(frame, "HBL", tmp_path)
    assert path.name == f"HBL_{digest}.json"
    assert archive_frame(frame, "HBL", tmp_path) == (path, digest)
    path.write_bytes(b"tampered")
    with pytest.raises(OSError, match="checksum mismatch"):
        archive_frame(frame, "HBL", tmp_path)
    assert path.read_bytes() == b"tampered"


def test_pypsx_conversion_keeps_as_traded_prices_and_nulls() -> None:
    rows = [{
        "symbol": "HBL", "datetime": datetime(2015, 1, 5),
        "open": 10.125, "high": 11.25, "low": 9.875, "close": 10.5,
        "volume": 1234.0, "adj_factor": 1.0, "tr_close": float("nan"),
        "dividend": float("nan"),
    }]
    record = normalize_pypsx_prices(
        rows, requested_ticker="HBL", fetched_at=datetime(2026, 11, 22, tzinfo=timezone.utc)
    )[0]
    assert record.trade_date == date(2015, 1, 5)
    assert record.open == Decimal("10.125")
    assert record.close == Decimal("10.5")
    assert record.adjusted_close is None
    assert record.adjustment_factor == Decimal("1.0")
    assert record.dividend is None
    assert record.dividend_basis is None
    assert record.source_endpoint == "pypsx.get_historical(adjust_splits=False, dividends=True)"


def test_pypsx_conversion_uses_karachi_exchange_date_for_aware_timestamp() -> None:
    rows = [{
        "symbol": "HBL", "datetime": datetime(2026, 1, 5, 20, tzinfo=timezone.utc),
        "open": "10", "high": "11", "low": "9", "close": "10",
        "volume": 10, "adj_factor": None, "dividend": None,
    }]
    record = normalize_pypsx_prices(
        rows, requested_ticker="HBL", fetched_at=datetime(2026, 1, 6, tzinfo=timezone.utc)
    )[0]
    assert record.trade_date == date(2026, 1, 6)


def test_pypsx_conversion_rejects_unexpected_symbol_and_fractional_volume() -> None:
    base = {
        "symbol": "UBL", "datetime": "2026-01-05", "open": "10", "high": "11",
        "low": "9", "close": "10", "volume": 10, "adj_factor": None,
        "dividend": None,
    }
    fetched = datetime(2026, 1, 6, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="does not match HBL"):
        normalize_pypsx_prices([base], requested_ticker="HBL", fetched_at=fetched)
    fractional = {**base, "symbol": "HBL", "volume": 10.5}
    with pytest.raises(ValueError, match="integer"):
        normalize_pypsx_prices([fractional], requested_ticker="HBL", fetched_at=fetched)


def test_pypsx_dividend_keeps_provider_basis() -> None:
    row = {
        "symbol": "HBL", "datetime": "2026-01-05", "open": "10", "high": "11",
        "low": "9", "close": "10", "volume": 10, "adj_factor": "0.5",
        "dividend": "1.25",
    }
    record = normalize_pypsx_prices(
        [row], requested_ticker="HBL", fetched_at=datetime(2026, 1, 6, tzinfo=timezone.utc)
    )[0]
    assert record.dividend == Decimal("1.25")
    assert record.dividend_basis == "per_adjusted_share"
    assert record.adjusted_close is None


def test_batch_normalization_errors_do_not_persist_source_values() -> None:
    errors: list[str] = []
    row = {
        "symbol": "HBL", "datetime": "2026-01-05", "open": "999.123456",
        "high": "11", "low": "9", "close": "10", "volume": 10,
        "adj_factor": None, "dividend": None,
    }
    result = normalize_pypsx_prices(
        [row], requested_ticker="HBL",
        fetched_at=datetime(2026, 1, 6, tzinfo=timezone.utc), errors=errors,
    )
    assert result == ()
    assert len(errors) == 1
    assert "open must be between low and high" in errors[0]
    assert "999.123456" not in errors[0]
