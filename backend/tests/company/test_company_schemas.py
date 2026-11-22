from __future__ import annotations

import json
from datetime import date, datetime, timezone
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
from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV, DatasetManifest
from jobs.company_as_of import records_as_of

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "company_records.json"
MANIFEST_TEMPLATE_PATH = Path(__file__).parents[2] / "data_manifests" / "company_prices.template.json"


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


def test_action_schema_preserves_missing_event_amount_and_requires_currency_when_amount_exists(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["actions"][0]}
    del row["amount_per_share"]
    assert CorporateAction.model_validate(row).amount_per_share is None
    row["amount_per_share"] = "1.25"
    row["currency"] = None
    with pytest.raises(ValidationError, match="amount requires a currency"):
        CorporateAction.model_validate(row)


def test_fundamental_schema_retains_unknown_publication_provenance(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["fundamentals"][0]}
    row["published_at"] = None
    row["available_date"] = None
    model = CompanyFundamental.model_validate(row)
    assert model.published_at is None and model.available_date is None


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
        "period_label": None,
    }
    with pytest.raises(ValidationError, match="period_end, as_of_date, or source period_label"):
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


def test_price_merge_ignores_fetch_timestamp_but_retains_latest_provenance(
    records: dict[str, list[dict[str, object]]],
) -> None:
    first = DailyOHLCV.model_validate(records["prices"][0])
    newer = DailyOHLCV.model_validate({
        **records["prices"][0], "fetched_at": "2026-05-01T00:00:00Z"
    })
    merged = merge_records(
        (first,), (newer,), key_fields=("ticker", "trade_date"), ignore_fields=("fetched_at",)
    )
    assert merged.duplicate_keys
    assert not merged.revised_keys
    assert merged.records[0].fetched_at == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_as_of_excludes_future_prices_actions_and_fundamentals(records: dict[str, list[dict[str, object]]]) -> None:
    action = CorporateAction.model_validate({**records["actions"][0], "available_date": "2026-01-08"})
    future_fundamental = CompanyFundamental.model_validate({**records["fundamentals"][0], "published_at": "2026-02-02T10:00:00Z"})
    result = records_as_of(
        [*normalize_prices(records["prices"]), action, future_fundamental], date(2026, 1, 5)
    )
    assert [row.trade_date for row in result if isinstance(row, DailyOHLCV)] == [date(2026, 1, 5)]
    assert not any(isinstance(row, (CorporateAction, CompanyFundamental)) for row in result)


def test_unknown_action_availability_is_excluded(records: dict[str, list[dict[str, object]]]) -> None:
    action = CorporateAction.model_validate(records["actions"][0])
    assert records_as_of([action], date(2026, 12, 31)) == ()


def test_fundamental_without_publication_and_availability_is_excluded_from_as_of(records: dict[str, list[dict[str, object]]]) -> None:
    row = {**records["fundamentals"][0], "published_at": None, "available_date": None}
    model = CompanyFundamental.model_validate(row)
    assert records_as_of([model], date(2026, 12, 31)) == ()


def test_as_of_fundamentals_accept_date_only_availability(records: dict[str, list[dict[str, object]]]) -> None:
    row = CompanyFundamental.model_validate({
        **records["fundamentals"][0], "published_at": None, "available_date": "2026-02-02"
    })
    assert records_as_of([row], date(2026, 2, 1)) == ()
    assert records_as_of([row], date(2026, 2, 2)) == (row,)


def test_as_of_uses_market_date_for_utc_publication_timestamp(records: dict[str, list[dict[str, object]]]) -> None:
    row = CompanyFundamental.model_validate({
        **records["fundamentals"][0], "published_at": "2026-04-15T22:00:00Z",
        "available_date": None,
    })
    assert records_as_of([row], date(2026, 4, 15)) == ()
    assert records_as_of([row], date(2026, 4, 16)) == (row,)


def test_both_availability_fields_use_the_later_known_date(records: dict[str, list[dict[str, object]]]) -> None:
    row = CompanyFundamental.model_validate({
        **records["fundamentals"][0], "published_at": "2026-04-15T18:59:00Z",
        "available_date": "2026-04-16",
    })
    assert records_as_of([row], date(2026, 4, 15)) == ()
    assert records_as_of([row], date(2026, 4, 16)) == (row,)


def test_raw_close_never_populates_adjusted_close(records: dict[str, list[dict[str, object]]]) -> None:
    row = DailyOHLCV.model_validate(records["prices"][0])
    assert row.close == Decimal("11.00")
    assert row.adjusted_close is None
    assert row.adjustment_factor is None


def test_manifest_template_contract_and_checksum_validation() -> None:
    template = DatasetManifest.model_validate_json(MANIFEST_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert template.collection_status == "not_started"
    assert template.file_paths == ()
    base = {
        "dataset_name": "company_prices", "source": "pypsx",
        "schema_version": "company_prices_v1", "collection_status": "not_started",
    }
    assert DatasetManifest.model_validate(base).row_count is None
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate({**base, "checksums_sha256": {"prices.parquet": "bad"}})


def test_complete_manifest_requires_consistent_paths_counts_and_checksums() -> None:
    base = {
        "dataset_name": "company_prices", "source": "pypsx",
        "fetched_at": datetime.now(timezone.utc), "schema_version": "company_prices_v1",
        "collection_status": "complete", "row_count": 5, "file_count": 1,
        "file_paths": ["prices.parquet"],
        "checksums_sha256": {"prices.parquet": "a" * 64},
    }
    assert DatasetManifest.model_validate(base).row_count == 5
    for change in (
        {"file_count": 2},
        {"checksums_sha256": {}},
        {"row_count": None},
    ):
        with pytest.raises(ValidationError):
            DatasetManifest.model_validate({**base, **change})
