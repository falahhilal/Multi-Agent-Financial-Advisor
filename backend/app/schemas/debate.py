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
from .market_view import MarketView, SectorName


class DebateInput(ContractModel):
    """Inputs received by Debate Agent."""

    schema_version: str = "2.0"

    as_of_date: date

    macro_output: MacroAgentOutput

    sector_output: SectorAgentOutput

    @model_validator(mode="after")
    def validate_analysis_dates(self):
        if self.macro_output.as_of.date() != self.as_of_date:
            raise ValueError(
                "Macro output timestamp does not match debate date"
            )

        if self.sector_output.as_of_date != self.as_of_date:
            raise ValueError(
                "Sector output date does not match debate date"
            )

        return self


class DebateArgument(ContractModel):
    """One argument presented during the debate."""

    argument_id: NonEmptyString

    round_number: int = Field(
        ge=1,
        le=3,
    )

    agent: Literal[
        "macro",
        "sector",
    ]

    claim: NonEmptyString

    supporting_reason: NonEmptyString

    reply_to_argument_id: NonEmptyString | None = None

    evidence_source_ids: list[NonEmptyString] = Field(
        default_factory=list
    )


class DebateOutput(ContractModel):
    """Final output from Debate Agent."""

    schema_version: str = "2.0"

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

    market_view: MarketView

    winning_agent: Literal[
        "macro",
        "sector",
        "consensus",
        "mixed",
    ]

    unresolved_sectors: list[SectorName] = Field(
        default_factory=list
    )

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        min_length=1
    )

    warnings: list[str] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_debate(self):
        arguments_by_id = {
            argument.argument_id: argument
            for argument in self.arguments
        }

        if len(arguments_by_id) != len(self.arguments):
            raise ValueError(
                "Debate argument IDs must be unique"
            )

        for argument in self.arguments:
            if argument.reply_to_argument_id is None:
                continue

            target = arguments_by_id.get(
                argument.reply_to_argument_id
            )

            if target is None:
                raise ValueError(
                    "Reply references an unknown argument"
                )

            if target.round_number >= argument.round_number:
                raise ValueError(
                    "A reply must reference an argument from an earlier round"
                )

            if target.agent == argument.agent:
                raise ValueError(
                    "An agent cannot reply to its own argument"
                )

        if len(self.unresolved_sectors) != len(
            set(self.unresolved_sectors)
        ):
            raise ValueError(
                "Unresolved sectors must be unique"
            )

        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        referenced_ids = set()

        for argument in self.arguments:
            referenced_ids.update(argument.evidence_source_ids)

        for sector in self.market_view.sector_views:
            referenced_ids.update(sector.source_citation_ids)

        referenced_ids.update(
            self.market_view.overall_stock_view.source_citation_ids
        )

        missing = referenced_ids - citation_ids

        if missing:
            raise ValueError(
                f"Unknown debate citations: {missing}"
            )

        return self
