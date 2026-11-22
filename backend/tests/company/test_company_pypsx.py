from __future__ import annotations

from datetime import date
from pathlib import Path

from jobs.company_pypsx import collect_prices


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
