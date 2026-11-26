from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
)


SectorName = Literal[
    "Commercial Banks",
    "Oil & Gas Exploration",
    "Cement",
    "Fertilizer",
    "Technology & Communication",
    "Textile",
]


class SectorMarketView(ContractModel):
    """Comparable market view for one PSX sector."""

    sector: SectorName

    stance: Literal[
        "overweight",
        "neutral",
        "underweight",
    ]

    confidence_score: ConfidenceScore

    source_citation_ids: list[NonEmptyString] = Field(
        min_length=1
    )


class OverallStockView(ContractModel):
    """Overall view on the PSX stock market."""

    view: Literal[
        "reduce",
        "hold",
        "increase",
    ]

    confidence_score: ConfidenceScore

    source_citation_ids: list[NonEmptyString] = Field(
        min_length=1
    )


class MarketView(ContractModel):
    """Shared market proposition produced by Macro and Sector agents."""

    schema_version: str = "2.0"

    universe_version: Literal["psx_v1"] = "psx_v1"

    sector_views: list[SectorMarketView] = Field(
        min_length=6,
        max_length=6,
    )

    overall_stock_view: OverallStockView

    @model_validator(mode="after")
    def validate_sector_coverage(self):
        sectors = [
            item.sector
            for item in self.sector_views
        ]

        if len(sectors) != len(set(sectors)):
            raise ValueError(
                "Duplicate sector views are not allowed"
            )

        expected = {
            "Commercial Banks",
            "Oil & Gas Exploration",
            "Cement",
            "Fertilizer",
            "Technology & Communication",
            "Textile",
        }

        if set(sectors) != expected:
            raise ValueError(
                "Market view must cover every sector in psx_v1"
            )

        return self
