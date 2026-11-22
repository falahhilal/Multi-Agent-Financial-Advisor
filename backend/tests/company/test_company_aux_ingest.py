from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from jobs.company_aux_ingest import _write_records
from jobs.company_as_of import records_as_of
from jobs.company_aux_ingest import (
    normalize_action_rows,
    normalize_fundamental_rows,
)
from jobs.company_normalization import merge_records
from jobs.company_schemas import CompanyFundamental, CorporateAction


FETCHED = datetime(2026, 11, 22, tzinfo=timezone.utc)


def test_action_normalization_preserves_rights_and_provider_fields_without_public_date() -> None:
    rows = [{
        "symbol": "HBL", "ex_date": "2020-06-01", "kind": "right", "mode": "x",
        "price_factor": 0.9, "total_factor": 0.91, "right_pct": 10.0,
        "applied": False, "reject_reason": "review", "dividend_rs": None,
    }]
    records, errors = normalize_action_rows(rows, "HBL", FETCHED)
    assert errors == []
    record = records[0]
    assert record.action_type == "rights_issue"
    assert record.ex_date == date(2020, 6, 1)
    assert record.available_date is None and record.published_at is None
    assert record.provider_price_factor == Decimal("0.9")
    assert record.provider_total_factor == Decimal("0.91")
    assert record.provider_applied is False
    assert record.provider_fields["right_pct"] == "10.0"
    assert records_as_of([record], date(2026, 11, 22)) == ()


def test_fundamentals_keep_period_label_null_units_and_missing_availability() -> None:
    rows = [
        {"statement": "income_statement", "item": "EPS Basic", "year": "2014", "value": 2.0},
        {"statement": "income_statement", "item": "EPS Basic", "year": "2015", "value": 2.5},
        {"statement": "ratio", "item": "P/E", "year": "2015", "value": float("nan")},
    ]
    records, errors = normalize_fundamental_rows(rows, "HBL", FETCHED)
    assert errors == []
    assert len(records) == 2
    eps, pe = records
    assert eps.metric == "eps_basic" and eps.source_item == "EPS Basic"
    assert eps.period_type == "annual" and eps.period_label == "2015"
    assert eps.period_end is None and eps.report_date is None
    assert eps.value == Decimal("2.5") and eps.unit is None and eps.currency is None
    assert eps.published_at is None and eps.available_date is None
    assert pe.metric == "p_e" and pe.value is None
    assert records_as_of(records, date(2025, 12, 31)) == ()


def test_auxiliary_records_merge_idempotently_ignoring_fetch_time() -> None:
    action, _ = normalize_action_rows([
        {"symbol": "HBL", "ex_date": "2020-06-01", "kind": "dividend", "dividend_rs": 1.0}
    ], "HBL", FETCHED)
    later = action[0].model_copy(update={"fetched_at": datetime(2026, 11, 23, tzinfo=timezone.utc)})
    merged_action = merge_records(action, [later], key_fields=("ticker", "action_type", "ex_date", "provider_fields"), ignore_fields=("fetched_at",))
    assert len(merged_action.records) == 1
    assert merged_action.records[0].fetched_at == later.fetched_at
    assert merged_action.revised_keys == ()

    facts, _ = normalize_fundamental_rows([
        {"statement": "income_statement", "item": "EPS", "year": "2020", "value": "3.2"}
    ], "HBL", FETCHED)
    merged_facts = merge_records(facts, facts, key_fields=("ticker", "statement_type", "source_item", "period_label"), ignore_fields=("fetched_at",))
    assert merged_facts.records == facts
    assert merged_facts.duplicate_keys


def test_availability_optional_model_still_validates_source_period() -> None:
    row = CompanyFundamental(
        ticker="HBL", metric="eps", value=Decimal("1"), period_type="annual",
        period_label="2025", statement_type="income_statement", source_item="EPS",
        basis="reported", source="pyPSX", fetched_at=FETCHED,
    )
    assert row.published_at is None and row.available_date is None
    try:
        CompanyFundamental(
            ticker="HBL", metric="eps", value=Decimal("1"), basis="reported",
            source="pyPSX", fetched_at=FETCHED,
        )
    except ValueError as exc:
        assert "period_label" in str(exc)
    else:
        raise AssertionError("a source period is required")


def test_actions_with_publication_timestamp_are_available_on_karachi_date() -> None:
    record = CorporateAction(
        ticker="HBL", action_type="cash_dividend", ex_date=date(2026, 5, 2),
        published_at=datetime(2026, 5, 1, 21, tzinfo=timezone.utc),
        amount_per_share=Decimal("1"), currency="PKR", source="test",
        fetched_at=FETCHED,
    )
    assert records_as_of([record], date(2026, 5, 2)) == (record,)


def test_action_and_fundamental_parquet_round_trip_keeps_provider_fields(tmp_path: Path) -> None:
    action, _ = normalize_action_rows([
        {"symbol": "HBL", "ex_date": "2020-06-01", "kind": "right",
         "price_factor": 0.9, "applied": False, "right_pct": 10.0}
    ], "HBL", FETCHED)
    fundamental, _ = normalize_fundamental_rows([
        {"statement": "balance_sheet", "item": "Total Assets", "year": "2020", "value": "10.25"}
    ], "HBL", FETCHED)
    action_path = _write_records(action, tmp_path / "actions.parquet", CorporateAction)
    fundamental_path = _write_records(fundamental, tmp_path / "fundamentals.parquet", CompanyFundamental)
    assert action_path.is_file() and fundamental_path.is_file()
