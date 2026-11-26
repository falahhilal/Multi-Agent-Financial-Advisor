from datetime import date
from decimal import Decimal

from pydantic import AwareDatetime, Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    RetrievedPassage,
    SourceCitation,
)

from .market_view import MarketView


class MacroIndicator(ContractModel):
    """One macroeconomic data point linked to its dataset manifest."""

    indicator_name: NonEmptyString

    value: Decimal

    unit: NonEmptyString

    dataset: NonEmptyString

    series_key: NonEmptyString

    observation_date: date

    published_at: AwareDatetime

    source_citation_id: NonEmptyString


class MacroAgentInput(ContractModel):
    """Input to Macro Analysis Agent."""

    schema_version: str = "2.0"

    as_of: AwareDatetime

    indicators: list[MacroIndicator] = Field(
        min_length=1
    )

    source_citations: list[SourceCitation] = Field(
        min_length=1
    )

    retrieved_passages: list[RetrievedPassage] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_sources_and_dates(self):
        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        for indicator in self.indicators:
            if indicator.published_at > self.as_of:
                raise ValueError(
                    "An indicator cannot be published after the analysis timestamp"
                )

            if indicator.source_citation_id not in citation_ids:
                raise ValueError(
                    f"Missing citation for {indicator.indicator_name}"
                )

        for passage in self.retrieved_passages:
            if passage.source_citation_id not in citation_ids:
                raise ValueError(
                    f"Missing citation for passage {passage.passage_id}"
                )

        return self


class MacroFinding(ContractModel):
    """An important conclusion from macroeconomic analysis."""

    finding: NonEmptyString

    impact: str

    explanation: NonEmptyString

    source_citation_ids: list[NonEmptyString] = Field(
        min_length=1
    )


class MacroAgentOutput(ContractModel):
    """Output from Macro Analysis Agent."""

    schema_version: str = "2.0"

    status: str

    as_of: AwareDatetime

    macro_stance: str

    key_findings: list[MacroFinding] = Field(
        min_length=1
    )

    market_view: MarketView

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        min_length=1
    )

    warnings: list[str] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_output_citations(self):
        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        referenced_ids = set()

        for finding in self.key_findings:
            referenced_ids.update(finding.source_citation_ids)

        for sector in self.market_view.sector_views:
            referenced_ids.update(sector.source_citation_ids)

        referenced_ids.update(
            self.market_view.overall_stock_view.source_citation_ids
        )

        missing = referenced_ids - citation_ids

        if missing:
            raise ValueError(
                f"Unknown source citations in Macro output: {missing}"
            )

        return self