from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    NonEmptyString,
    Percentage,
)


class ExistingHolding(ContractModel):
    """An existing stock holding reported by the investor."""

    ticker: NonEmptyString

    quantity: Decimal = Field(gt=0)

    average_purchase_price_pkr: Decimal | None = Field(
        default=None,
        gt=0,
    )

    must_keep: bool = False


class InvestorQuestionnaire(ContractModel):
    """Answers submitted by the investor during onboarding."""

    risk_tolerance: Literal[
        "low",
        "medium",
        "high",
    ]

    time_horizon: Literal[
        "short",
        "medium",
        "long",
    ]

    investment_goal: Literal[
        "capital_preservation",
        "income",
        "balanced_growth",
        "capital_growth",
    ]

    investment_experience: Literal[
        "beginner",
        "intermediate",
        "experienced",
    ]

    maximum_acceptable_loss_pct: Percentage

    liquidity_need: Literal[
        "low",
        "medium",
        "high",
    ]

    portfolio_mode: Literal[
        "new",
        "rebalance",
    ]

    investable_capital_pkr: Decimal = Field(
        ge=0,
    )

    existing_holdings: list[ExistingHolding] = Field(
        default_factory=list
    )

    reaction_to_20pct_loss: Literal[
        "sell_all",
        "sell_some",
        "hold",
        "buy_more",
    ]

    @model_validator(mode="after")
    def validate_portfolio_details(self):
        if (
            self.portfolio_mode == "new"
            and self.investable_capital_pkr <= 0
        ):
            raise ValueError(
                "A new portfolio requires positive investable capital"
            )

        if (
            self.portfolio_mode == "rebalance"
            and not self.existing_holdings
        ):
            raise ValueError(
                "Rebalancing requires at least one existing holding"
            )

        tickers = [
            holding.ticker
            for holding in self.existing_holdings
        ]

        if len(tickers) != len(set(tickers)):
            raise ValueError(
                "Duplicate existing holdings are not allowed"
            )

        return self


class RiskProfilerInput(ContractModel):
    """Input to the Risk Profiler Agent."""

    schema_version: str = "2.0"

    questionnaire: InvestorQuestionnaire


class RiskConstraints(ContractModel):
    """Derived constraints passed to portfolio construction."""

    risk_level: Literal[
        "conservative",
        "moderate",
        "aggressive",
    ]

    max_equity_pct: Percentage

    min_liquidity_pct: Percentage

    max_single_stock_pct: Percentage

    max_sector_pct: Percentage

    time_horizon: Literal[
        "short",
        "medium",
        "long",
    ]

    behavioral_flags: list[NonEmptyString] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_constraints(self):
        if (
            self.max_equity_pct + self.min_liquidity_pct
            > Decimal("100")
        ):
            raise ValueError(
                "Maximum equity and minimum liquidity cannot exceed 100%"
            )

        if self.max_single_stock_pct > self.max_equity_pct:
            raise ValueError(
                "Maximum single-stock allocation cannot exceed maximum equity"
            )

        if self.max_sector_pct > self.max_equity_pct:
            raise ValueError(
                "Maximum sector allocation cannot exceed maximum equity"
            )

        return self


class RiskProfilerOutput(ContractModel):
    """Output from Risk Profiler Agent."""

    schema_version: str = "2.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    constraints: RiskConstraints

    rationale: NonEmptyString

    warnings: list[str] = Field(
        default_factory=list
    )