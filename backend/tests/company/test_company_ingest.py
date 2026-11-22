from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from jobs.company_ingest import read_price_parquet, write_price_parquet
from jobs.company_schemas import DailyOHLCV


def test_normalized_parquet_round_trip_preserves_decimal_nulls_and_provenance(tmp_path: Path) -> None:
    record = DailyOHLCV(
        ticker="HBL", trade_date=date(2026, 1, 5), open=Decimal("10.125"),
        high=Decimal("11.25"), low=Decimal("9.875"), close=Decimal("10.5"),
        volume=120, currency="PKR", price_unit="per_share", volume_unit="shares",
        adjusted_close=None, adjustment_factor=Decimal("1"), dividend=None,
        source="pyPSX", source_endpoint="pypsx.get_historical(adjust_splits=False)",
        source_record_id="HBL:2026-01-05", fetched_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
    )
    path = write_price_parquet((record,), tmp_path / "ticker=HBL" / "prices.parquet")
    assert read_price_parquet(path) == (record,)


def test_price_parquet_refuses_empty_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        write_price_parquet((), tmp_path / "empty.parquet")
