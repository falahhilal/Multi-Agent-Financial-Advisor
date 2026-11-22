from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    Percentage,
    SourceCitation,
)

from .risk_profiler import RiskConstraints
from .debate import DebateOutput


class PortfolioConstructorInput(ContractModel):
    """Inputs received by Portfolio Constructor."""

    schema_version: str = "1.0"

    as_of_date: date

    risk_constraints: RiskConstraints

    debate_output: DebateOutput

    @model_validator(mode="after")
    def validate_debate_date(self):
        if self.debate_output.as_of_date != self.as_of_date:
            raise ValueError(
                "Debate output date does not match portfolio date"
            )

        return self


class AllocationLine(ContractModel):
    """Allocation assigned to one stock or cash."""

    ticker: NonEmptyString

    weight_pct: Percentage

    rationale: NonEmptyString


class PortfolioMetrics(ContractModel):
    """Quantitative portfolio metrics calculated in Python."""

    expected_return_pct: Decimal | None = None

    annualized_volatility_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
    )

    sharpe_ratio: Decimal | None = None

    maximum_drawdown_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("100"),
    )


class PortfolioConstructorOutput(ContractModel):
    """Final portfolio recommendation."""

    schema_version: str = "1.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    as_of_date: date

    allocations: list[AllocationLine] = Field(
        min_length=1
    )

    constraints_applied: RiskConstraints

    metrics: PortfolioMetrics | None = None

    debate_summary: NonEmptyString

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        default_factory=list
    )

    warnings: list[str] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_portfolio(self):
        tickers = [
            allocation.ticker
            for allocation in self.allocations
        ]

        if len(tickers) != len(set(tickers)):
            raise ValueError(
                "Duplicate tickers are not allowed"
            )

        if tickers.count("CASH") != 1:
            raise ValueError(
                "Portfolio must contain exactly one CASH allocation"
            )

        total_weight = sum(
            (
                allocation.weight_pct
                for allocation in self.allocations
            ),
            Decimal("0"),
        )

        if total_weight != Decimal("100"):
            raise ValueError(
                "Portfolio allocations must sum to exactly 100%"
            )

        equity_weight = sum(
            (
                allocation.weight_pct
                for allocation in self.allocations
                if allocation.ticker != "CASH"
            ),
            Decimal("0"),
        )

        cash_weight = next(
            allocation.weight_pct
            for allocation in self.allocations
            if allocation.ticker == "CASH"
        )

        if equity_weight > self.constraints_applied.max_equity_pct:
            raise ValueError(
                "Equity allocation exceeds the investor's maximum"
            )

        if cash_weight < self.constraints_applied.min_cash_pct:
            raise ValueError(
                "Cash allocation is below the investor's minimum"
            )

        for allocation in self.allocations:
            if allocation.ticker != "CASH":
                if (
                    allocation.weight_pct
                    > self.constraints_applied.max_single_stock_pct
                ):
                    raise ValueError(
                        f"Allocation for {allocation.ticker} exceeds the maximum single-stock limit"
                    )

        return self