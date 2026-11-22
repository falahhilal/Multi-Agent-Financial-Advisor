from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from jobs.company_quality import (
    validate_corporate_actions,
    validate_dataset_coverage,
    validate_fundamentals,
    validate_prices,
    validate_sector_mapping,
    build_per_ticker_reports,
)
from jobs.company_sector_mapping import load_sector_mapping
from jobs.company_schemas import CorporateAction, SectorMappingRow

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "company_records.json"
UNIVERSE_PATH = Path(__file__).parents[2] / "universe" / "psx_v1.yaml"


@pytest.fixture
def records() -> dict[str, list[dict[str, object]]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def universe_rows() -> list[SectorMappingRow]:
    return load_sector_mapping(UNIVERSE_PATH)[1]


def test_sector_mapping_has_exactly_one_row_per_universe_ticker(
    universe_rows: list[SectorMappingRow],
) -> None:
    expected = {row.ticker for row in universe_rows}

    report = validate_sector_mapping(universe_rows, expected)

    assert len(universe_rows) == 18
    assert report.issues == []
    assert report.ticker_coverage == sorted(expected)


def test_sector_mapping_flags_missing_and_duplicate_tickers(
    universe_rows: list[SectorMappingRow],
) -> None:
    expected = {row.ticker for row in universe_rows}
    rows = [*universe_rows[:-1], universe_rows[0]]

    codes = {issue.code for issue in validate_sector_mapping(rows, expected).issues}

    assert codes == {"duplicate_sector_mapping", "missing_sector_mapping"}


def test_price_quality_flags_duplicates_calendar_gaps_and_large_moves(
    records: dict[str, list[dict[str, object]]],
) -> None:
    rows = [*records["prices"], records["prices"][0]]
    calendar = {
        "HBL": {date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)},
        "UBL": {date(2026, 1, 5)},
    }
    actions = [
        # The test fixture action is on HBL's second observed date.
        CorporateAction.model_validate(records["actions"][0])
    ]
    report = validate_prices(
        rows,
        {"HBL", "UBL"},
        expected_sessions=calendar,
        max_abs_daily_return=Decimal("0.05"),
        corporate_actions=actions,
    )
    codes = {issue.code for issue in report.issues}

    assert "duplicate_price_record" in codes
    assert "missing_price_session" in codes
    assert "suspicious_price_movement" in codes
    move = next(issue for issue in report.issues if issue.code == "suspicious_price_movement")
    assert "corporate action is recorded" in move.message
    assert report.not_evaluated == []


def test_price_quality_does_not_assume_weekdays_are_sessions(
    records: dict[str, list[dict[str, object]]],
) -> None:
    report = validate_prices(records["prices"], {"HBL"})

    assert any("approved PSX calendar" in item for item in report.not_evaluated)
    assert not any(issue.code == "missing_price_session" for issue in report.issues)


def test_price_quality_flags_invalid_values_and_universe_mappings(
    records: dict[str, list[dict[str, object]]],
) -> None:
    bad_volume = {**records["prices"][0], "volume": -2}
    other_ticker = {**records["prices"][1], "ticker": "UNKNOWN"}

    codes = {
        issue.code
        for issue in validate_prices(
            [bad_volume, other_ticker],
            {"HBL"},
            expected_sessions={"HBL": {date(2026, 1, 5)}},
        ).issues
    }

    assert "invalid_price_record" in codes
    assert "unknown_ticker" in codes


def test_corporate_action_quality_flags_duplicates_and_dates(
    records: dict[str, list[dict[str, object]]],
) -> None:
    action = records["actions"][0]
    late_announcement = {**action, "ticker": "UBL", "announcement_date": "2026-01-07"}
    report = validate_corporate_actions(
        [action, action, late_announcement], {"HBL", "UBL"}
    )
    codes = {issue.code for issue in report.issues}

    assert "duplicate_corporate_action" in codes
    assert "announcement_after_ex_date" in codes


def test_fundamental_quality_flags_stale_missing_and_future_publication(
    records: dict[str, list[dict[str, object]]],
) -> None:
    future = {
        **records["fundamentals"][0],
        "ticker": "UBL",
        "published_at": "2026-04-16T00:00:00Z",
        "fetched_at": "2026-04-17T00:00:00Z",
    }
    report = validate_fundamentals(
        [records["fundamentals"][0], future],
        {"HBL", "UBL"},
        as_of=datetime(2026, 4, 15, tzinfo=timezone.utc),
        required_metrics={"HBL": {"eps_basic", "pe_ratio"}},
        stale_after_days={"eps_basic": 30},
    )
    codes = {issue.code for issue in report.issues}

    assert "stale_fundamental" in codes
    assert "missing_fundamental_metric" in codes
    assert "fundamental_not_public_as_of_date" in codes


def test_fundamental_quality_handles_date_only_availability_and_excludes_future(
    records: dict[str, list[dict[str, object]]],
) -> None:
    available = {
        **records["fundamentals"][0], "ticker": "HBL", "published_at": None,
        "available_date": "2026-04-01", "fetched_at": "2026-04-02T00:00:00Z",
    }
    future = {
        **records["fundamentals"][0], "ticker": "UBL", "published_at": None,
        "available_date": "2026-04-16", "fetched_at": "2026-04-17T00:00:00Z",
    }
    report = validate_fundamentals(
        [available, future], {"HBL", "UBL"},
        as_of=datetime(2026, 4, 15, tzinfo=timezone.utc),
        required_metrics={"HBL": {"eps_basic"}, "UBL": {"eps_basic"}},
        stale_after_days={"eps_basic": 30},
    )
    future_issue = next(issue for issue in report.issues if issue.ticker == "UBL")
    assert future_issue.code == "fundamental_not_public_as_of_date"
    assert any(issue.code == "missing_fundamental_metric" and issue.ticker == "UBL" for issue in report.issues)


def test_dataset_coverage_flags_missing_and_unknown_tickers() -> None:
    report = validate_dataset_coverage(
        {"prices": ["HBL", "OUTSIDE"]}, {"HBL", "UBL"}
    )
    codes = {issue.code for issue in report.issues}

    assert codes == {"missing_ticker_coverage", "unknown_ticker_coverage"}


def test_per_ticker_reports_include_deferred_checks_and_are_complete(records) -> None:
    prices = validate_prices(records["prices"], {"HBL", "UBL"})
    actions = validate_corporate_actions(records["actions"], {"HBL", "UBL"})
    reports = build_per_ticker_reports([prices, actions], {"HBL", "UBL"})

    assert [report.ticker for report in reports] == ["HBL", "UBL"]
    assert all(report.status == "incomplete" for report in reports)
    assert reports[0].dataset_record_counts == {"prices": 2, "corporate_actions": 1}
    assert "approved PSX calendar" in reports[0].not_evaluated["prices"][0]


def test_sparse_event_coverage_can_be_declared_optional() -> None:
    report = validate_dataset_coverage(
        {"prices": ["HBL"], "corporate_actions": []},
        {"HBL", "UBL"},
        required_tickers_by_dataset={"prices": {"HBL", "UBL"}, "corporate_actions": set()},
    )

    missing = [issue for issue in report.issues if issue.code == "missing_ticker_coverage"]
    assert len(missing) == 1
    assert missing[0].ticker == "UBL"


def test_dataset_coverage_flags_entirely_missing_required_dataset() -> None:
    report = validate_dataset_coverage(
        {}, {"HBL"}, required_tickers_by_dataset={"prices": {"HBL"}}
    )

    assert any(issue.code == "missing_dataset_coverage" for issue in report.issues)
