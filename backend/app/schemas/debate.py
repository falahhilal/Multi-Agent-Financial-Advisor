from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    SourceCitation,
)

from .macro import MacroAgentOutput
from .sector import SectorAgentOutput


class DebateInput(ContractModel):
    """Inputs received by Debate Agent."""

    schema_version: str = "1.0"

    as_of_date: date

    macro_output: MacroAgentOutput

    sector_output: SectorAgentOutput

    @model_validator(mode="after")
    def validate_analysis_dates(self):
        if self.macro_output.as_of_date != self.as_of_date:
            raise ValueError(
                "Macro output date does not match debate date"
            )

        if self.sector_output.as_of_date != self.as_of_date:
            raise ValueError(
                "Sector output date does not match debate date"
            )

        return self


class DebateArgument(ContractModel):
    """One argument presented during the debate."""

    agent: Literal[
        "macro",
        "sector",
    ]

    claim: NonEmptyString

    supporting_reason: NonEmptyString

    counter_argument: str | None = None

    evidence_source_ids: list[NonEmptyString] = Field(
        default_factory=list
    )


class StockDebateDecision(ContractModel):
    """Final debate conclusion for an individual stock."""

    ticker: NonEmptyString

    sector: NonEmptyString

    decision: Literal[
        "favorable",
        "neutral",
        "unfavorable",
    ]

    conviction_score: ConfidenceScore

    rationale: NonEmptyString

    supporting_arguments: list[NonEmptyString] = Field(
        default_factory=list
    )

    opposing_arguments: list[NonEmptyString] = Field(
        default_factory=list
    )

    evidence_source_ids: list[NonEmptyString] = Field(
        default_factory=list
    )


class DebateOutput(ContractModel):
    """Final output from Debate Agent."""

    schema_version: str = "1.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    as_of_date: date

    debate_summary: NonEmptyString

    arguments: list[DebateArgument] = Field(
        min_length=1
    )

    stock_decisions: list[StockDebateDecision] = Field(
        default_factory=list
    )

    overall_market_conclusion: Literal[
        "bullish",
        "bearish",
        "neutral",
    ]

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        default_factory=list
    )

    warnings: list[str] = Field(
        default_factory=list
    )