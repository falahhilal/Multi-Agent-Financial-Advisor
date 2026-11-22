from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    SourceCitation,
)


class MacroIndicator(ContractModel):
    """One macroeconomic data point."""

    indicator_name: NonEmptyString

    value: Decimal

    unit: NonEmptyString

    period: NonEmptyString

    published_at: date

    source_citation_id: NonEmptyString


class MacroAgentInput(ContractModel):
    """Input to Macro Analysis Agent."""

    schema_version: str = "1.0"

    as_of_date: date

    indicators: list[MacroIndicator] = Field(min_length=1)

    source_citations: list[SourceCitation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources_and_dates(self):
        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        for indicator in self.indicators:
            if indicator.published_at > self.as_of_date:
                raise ValueError(
                    "An indicator cannot be published after the analysis date"
                )

            if indicator.source_citation_id not in citation_ids:
                raise ValueError(
                    f"Missing citation for {indicator.indicator_name}"
                )

        return self


class MacroFinding(ContractModel):
    """An important conclusion from macroeconomic analysis."""

    finding: NonEmptyString

    impact: Literal[
        "positive",
        "negative",
        "mixed",
        "neutral",
    ]

    explanation: NonEmptyString

    source_citation_ids: list[NonEmptyString] = Field(
        min_length=1
    )


class SectorImplication(ContractModel):
    """Macro Agent's view of a sector."""

    sector: NonEmptyString

    outlook: Literal[
        "positive",
        "negative",
        "neutral",
    ]

    explanation: NonEmptyString

    confidence_score: ConfidenceScore

    source_citation_ids: list[NonEmptyString] = Field(
        min_length=1
    )


class MacroAgentOutput(ContractModel):
    """Output from Macro Analysis Agent."""

    schema_version: str = "1.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    as_of_date: date

    macro_outlook: Literal[
        "bullish",
        "bearish",
        "neutral",
    ]

    key_findings: list[MacroFinding] = Field(
        min_length=1
    )

    sector_implications: list[SectorImplication] = Field(
        default_factory=list
    )

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        min_length=1
    )

    warnings: list[str] = Field(
        default_factory=list
    )