from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from jobs.company_normalization import (
    DuplicateRecordError,
    merge_records,
    normalize_corporate_actions,
    normalize_fundamentals,
    normalize_prices,
)
from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "company_records.json"


@pytest.fixture
def records() -> dict[str, list[dict[str, object]]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_provider_neutral_normalizers_parse_test_fixture(records: dict[str, list[dict[str, object]]]) -> None:
    prices = normalize_prices(records["prices"])
    actions = normalize_corporate_actions(records["actions"])
    fundamentals = normalize_fundamentals(records["fundamentals"])

    assert (len(prices), len(actions), len(fundamentals)) == (2, 1, 1)
    assert prices[0].close == Decimal("11.00")
    assert prices[0].currency == "PKR"
    assert prices[0].fetched_at.utcoffset().total_seconds() == 0
    assert actions[0].action_type == "cash_dividend"
    assert fundamentals[0].published_at.date() == date(2026, 2, 2)


def test_price_schema_rejects_inconsistent_ohlc(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["prices"][0], "high": "8.00"}
    with pytest.raises(ValidationError) as error:
        DailyOHLCV.model_validate(row)
    assert "low must be less than or equal to high" in str(error.value)


def test_price_schema_rejects_negative_volume(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["prices"][0], "volume": -1}
    with pytest.raises(ValidationError):
        DailyOHLCV.model_validate(row)


def test_action_schema_requires_event_specific_fields(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["actions"][0]}
    del row["amount_per_share"]
    with pytest.raises(ValidationError, match="require amount_per_share and currency"):
        CorporateAction.model_validate(row)


def test_fundamental_schema_requires_publication_provenance(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["fundamentals"][0]}
    del row["published_at"]
    with pytest.raises(ValidationError):
        CompanyFundamental.model_validate(row)


def test_as_of_metric_does_not_require_an_invented_fiscal_period(records: dict[str, list[dict[str, object]]]) -> None:
    row = {
        **records["fundamentals"][0],
        "metric": "pe_ratio",
        "value": "8.25",
        "unit": "ratio",
        "currency": None,
        "period_start": None,
        "period_end": None,
        "report_date": None,
        "as_of_date": "2026-02-02",
        "basis": "vendor_calculated",
    }

    assert CompanyFundamental.model_validate(row).period_end is None


def test_fundamental_requires_period_or_as_of_date(records: dict[str, list[dict[str, object]]]) -> None:
    row = {
        **records["fundamentals"][0],
        "period_start": None,
        "period_end": None,
        "as_of_date": None,
    }
    with pytest.raises(ValidationError, match="period_end or as_of_date"):
        CompanyFundamental.model_validate(row)


def test_merge_is_idempotent_and_reports_revisions(records: dict[str, list[dict[str, object]]]) -> None:
    prices = normalize_prices(records["prices"])
    first = merge_records((), prices, key_fields=("ticker", "trade_date"))
    repeated = merge_records(first.records, prices, key_fields=("ticker", "trade_date"))

    assert len(first.records) == len(repeated.records) == 2
    assert len(repeated.duplicate_keys) == 2
    assert repeated.revised_keys == ()

    changed = DailyOHLCV.model_validate({**records["prices"][0], "close": "11.50"})
    revised = merge_records(first.records, (changed,), key_fields=("ticker", "trade_date"))
    assert len(revised.records) == 2
    assert len(revised.revised_keys) == 1
    assert revised.records[0].close == Decimal("11.50")


def test_merge_rejects_conflicting_duplicate_keys(records: dict[str, list[dict[str, object]]]) -> None:
    row = DailyOHLCV.model_validate(records["prices"][0])
    changed = DailyOHLCV.model_validate({**records["prices"][0], "close": "11.50"})

    with pytest.raises(DuplicateRecordError, match="incoming batch"):
        merge_records((), (row, changed), key_fields=("ticker", "trade_date"))
