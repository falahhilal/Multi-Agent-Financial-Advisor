from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    NonEmptyString,
    Percentage,
)


class InvestorQuestionnaire(ContractModel):
    """Answers submitted by the investor."""

    risk_tolerance: Literal["low", "medium", "high"]

    investment_horizon: Literal[
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


class RiskProfilerInput(ContractModel):
    """Input to the Risk Profiler Agent."""

    schema_version: str = "1.0"

    questionnaire: InvestorQuestionnaire


class RiskConstraints(ContractModel):
    """Derived constraints passed to the Portfolio Constructor."""

    risk_level: Literal[
        "conservative",
        "moderate",
        "aggressive",
    ]

    max_equity_pct: Percentage

    min_cash_pct: Percentage

    max_single_stock_pct: Percentage

    investment_horizon: Literal[
        "short",
        "medium",
        "long",
    ]

    behavioral_flags: list[NonEmptyString] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_constraints(self):
        if self.max_equity_pct + self.min_cash_pct > Decimal("100"):
            raise ValueError(
                "Maximum equity and minimum cash cannot exceed 100%"
            )

        if self.max_single_stock_pct > self.max_equity_pct:
            raise ValueError(
                "Maximum single-stock allocation cannot exceed maximum equity allocation"
            )

        return self


class RiskProfilerOutput(ContractModel):
    """Output from Risk Profiler Agent."""

    schema_version: str = "1.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    constraints: RiskConstraints

    rationale: NonEmptyString

    warnings: list[str] = Field(default_factory=list)