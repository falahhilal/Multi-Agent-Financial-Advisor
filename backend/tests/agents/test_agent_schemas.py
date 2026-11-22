from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import (
    DebateInput,
    PortfolioConstructorOutput,
    RiskConstraints,
)


def make_constraints():
    return RiskConstraints(
        risk_level="moderate",
        max_equity_pct=Decimal("70"),
        min_cash_pct=Decimal("30"),
        max_single_stock_pct=Decimal("20"),
        investment_horizon="medium",
    )


def test_valid_risk_constraints():
    constraints = make_constraints()

    assert constraints.max_equity_pct == Decimal("70")
    assert constraints.min_cash_pct == Decimal("30")


def test_risk_constraints_reject_invalid_limits():
    with pytest.raises(ValidationError):
        RiskConstraints(
            risk_level="moderate",
            max_equity_pct=Decimal("80"),
            min_cash_pct=Decimal("30"),
            max_single_stock_pct=Decimal("20"),
            investment_horizon="medium",
        )


def test_debate_does_not_receive_risk_constraints():
    assert "risk_constraints" not in DebateInput.model_fields


def test_portfolio_requires_exactly_one_cash_allocation():
    with pytest.raises(ValidationError):
        PortfolioConstructorOutput(
            status="success",
            as_of_date=date(2026, 11, 22),
            allocations=[
                {
                    "ticker": "HBL",
                    "weight_pct": Decimal("50"),
                    "rationale": "Test allocation",
                },
                {
                    "ticker": "UBL",
                    "weight_pct": Decimal("50"),
                    "rationale": "Test allocation",
                },
            ],
            constraints_applied=make_constraints(),
            debate_summary="Test debate",
            confidence_score=Decimal("0.8"),
        )


def test_portfolio_weights_must_equal_100():
    with pytest.raises(ValidationError):
        PortfolioConstructorOutput(
            status="success",
            as_of_date=date(2026, 11, 22),
            allocations=[
                {
                    "ticker": "HBL",
                    "weight_pct": Decimal("40"),
                    "rationale": "Test allocation",
                },
                {
                    "ticker": "CASH",
                    "weight_pct": Decimal("50"),
                    "rationale": "Cash reserve",
                },
            ],
            constraints_applied=make_constraints(),
            debate_summary="Test debate",
            confidence_score=Decimal("0.8"),
        )


def test_portfolio_respects_single_stock_limit():
    with pytest.raises(ValidationError):
        PortfolioConstructorOutput(
            status="success",
            as_of_date=date(2026, 11, 22),
            allocations=[
                {
                    "ticker": "HBL",
                    "weight_pct": Decimal("30"),
                    "rationale": "Test allocation",
                },
                {
                    "ticker": "CASH",
                    "weight_pct": Decimal("70"),
                    "rationale": "Cash reserve",
                },
            ],
            constraints_applied=make_constraints(),
            debate_summary="Test debate",
            confidence_score=Decimal("0.8"),
        )