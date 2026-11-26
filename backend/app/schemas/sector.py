from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    SourceCitation,
)

from .market_view import MarketView


class CompanySnapshot(ContractModel):
    """Company-level financial and market data."""

    ticker: NonEmptyString

    company_name: NonEmptyString

    sector: NonEmptyString

    closing_price: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
    )

    price_change_pct: Decimal | None = None

    pe_ratio: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
    )

    earnings_growth_pct: Decimal | None = None

    source_citation_ids: list[NonEmptyString] = Field(
        default_factory=list
    )


class NewsSentimentItem(ContractModel):
    """Sentiment output produced by the FinBERT pipeline."""

    news_id: NonEmptyString

    headline: NonEmptyString

    published_at: date

    sentiment_label: str

    sentiment_score: Decimal = Field(
        ge=Decimal("-1"),
        le=Decimal("1"),
    )

    related_tickers: list[NonEmptyString] = Field(
        default_factory=list
    )

    related_sectors: list[NonEmptyString] = Field(
        default_factory=list
    )

    source_citation_id: NonEmptyString


class SectorAgentInput(ContractModel):
    """Input to Sector Analysis Agent."""

    schema_version: str = "2.0"

    as_of_date: date

    companies: list[CompanySnapshot] = Field(
        min_length=1
    )

    news_sentiment: list[NewsSentimentItem] = Field(
        default_factory=list
    )

    source_citations: list[SourceCitation] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_sources_and_dates(self):
        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        for company in self.companies:
            missing = (
                set(company.source_citation_ids) - citation_ids
            )

            if missing:
                raise ValueError(
                    f"Unknown citations for company {company.ticker}: {missing}"
                )

        for item in self.news_sentiment:
            if item.published_at > self.as_of_date:
                raise ValueError(
                    "News cannot be published after the analysis date"
                )

            if item.source_citation_id not in citation_ids:
                raise ValueError(
                    f"Missing citation for news item {item.news_id}"
                )

        return self


class SectorAgentOutput(ContractModel):
    """Output from Sector Analysis Agent."""

    schema_version: str = "2.0"

    status: str

    as_of_date: date

    market_view: MarketView

    sentiment_summary: NonEmptyString

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

        for sector in self.market_view.sector_views:
            referenced_ids.update(sector.source_citation_ids)

        referenced_ids.update(
            self.market_view.overall_stock_view.source_citation_ids
        )

        missing = referenced_ids - citation_ids

        if missing:
            raise ValueError(
                f"Unknown source citations in Sector output: {missing}"
            )

        return self
