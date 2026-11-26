from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .common import (
    ConfidenceScore,
    ContractModel,
    NonEmptyString,
    Percentage,
    SourceCitation,
)

from .risk_profiler import RiskConstraints
from .debate import DebateOutput
from .market_view import SectorName


class PricedExistingHolding(ContractModel):
    """Existing holding enriched with a verified current market price."""

    ticker: NonEmptyString

    quantity: Decimal = Field(gt=0)

    average_purchase_price_pkr: Decimal | None = Field(
        default=None,
        gt=0,
    )

    current_price_pkr: Decimal = Field(gt=0)

    price_source_citation_id: NonEmptyString

    must_keep: bool = False


class PortfolioBuildContext(ContractModel):
    """
    Private deterministic quant input.

    This context is not sent to any LLM.
    """

    schema_version: str = "2.0"

    as_of_date: date

    portfolio_mode: Literal[
        "new",
        "rebalance",
    ]

    investable_capital_pkr: Decimal = Field(
        ge=0,
    )

    existing_holdings: list[PricedExistingHolding] = Field(
        default_factory=list
    )

    source_citations: list[SourceCitation] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_build_context(self):
        if (
            self.portfolio_mode == "new"
            and self.investable_capital_pkr <= 0
        ):
            raise ValueError(
                "New portfolio requires positive investable capital"
            )

        if (
            self.portfolio_mode == "new"
            and self.existing_holdings
        ):
            raise ValueError(
                "New portfolio build context cannot contain rebalance holdings"
            )

        if (
            self.portfolio_mode == "rebalance"
            and not self.existing_holdings
        ):
            raise ValueError(
                "Rebalancing requires existing holdings"
            )

        tickers = [
            holding.ticker
            for holding in self.existing_holdings
        ]

        if len(tickers) != len(set(tickers)):
            raise ValueError(
                "Duplicate existing holdings are not allowed"
            )

        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        for holding in self.existing_holdings:
            if holding.price_source_citation_id not in citation_ids:
                raise ValueError(
                    f"Missing current-price citation for {holding.ticker}"
                )

        return self


class PortfolioConstructorInput(ContractModel):
    """Inputs received by the Portfolio Constructor agent."""

    schema_version: str = "2.0"

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

    sector: SectorName | None = None

    weight_pct: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("100"),
        decimal_places=2,
    )

    amount_pkr: Decimal = Field(
        ge=Decimal("0"),
        decimal_places=2,
    )

    rationale: NonEmptyString


class HoldingAction(ContractModel):
    """Recommended action for an existing or proposed stock holding."""

    ticker: NonEmptyString

    action: Literal[
        "buy",
        "hold",
        "reduce",
        "sell",
    ]

    current_quantity: Decimal = Field(ge=0)

    recommended_quantity: Decimal = Field(ge=0)

    rationale: NonEmptyString


class AuditTrailEntry(ContractModel):
    """One immutable, attributable portfolio audit entry."""

    sequence: int = Field(gt=0)

    timestamp: AwareDatetime

    actor: Literal[
        "risk_profiler",
        "macro",
        "sector",
        "debate_facilitator",
        "quant",
        "portfolio_constructor",
    ]

    step: NonEmptyString

    explanation: NonEmptyString

    source_citation_ids: list[NonEmptyString] = Field(
        default_factory=list
    )

    debate_round: int | None = Field(
        default=None,
        ge=1,
        le=3,
    )

    argument_id: NonEmptyString | None = None


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

    schema_version: str = "2.0"

    status: Literal[
        "success",
        "partial",
        "failed",
    ]

    as_of_date: date

    portfolio_mode: Literal[
        "new",
        "rebalance",
    ]

    total_portfolio_value_pkr: Decimal = Field(gt=0)

    allocations: list[AllocationLine] = Field(
        min_length=1
    )

    holding_actions: list[HoldingAction] = Field(
        default_factory=list
    )

    constraints_applied: RiskConstraints

    metrics: PortfolioMetrics | None = None

    debate_summary: NonEmptyString

    winning_agent: Literal[
        "macro",
        "sector",
        "consensus",
        "mixed",
    ]

    tie_breaker_triggered: bool

    tie_breaker_reason: NonEmptyString | None = None

    unresolved_sectors_resolved: list[SectorName] = Field(
        default_factory=list
    )

    confidence_score: ConfidenceScore

    source_citations: list[SourceCitation] = Field(
        default_factory=list
    )

    audit_trail: list[AuditTrailEntry] = Field(
        min_length=1
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

        total_amount = sum(
            (
                allocation.amount_pkr
                for allocation in self.allocations
            ),
            Decimal("0"),
        )

        if total_amount != self.total_portfolio_value_pkr:
            raise ValueError(
                "Allocation amounts must equal total portfolio value"
            )

        equity_allocations = [
            allocation
            for allocation in self.allocations
            if allocation.ticker != "CASH"
        ]

        cash_allocation = next(
            allocation
            for allocation in self.allocations
            if allocation.ticker == "CASH"
        )

        if cash_allocation.sector is not None:
            raise ValueError(
                "CASH allocation must not have a sector"
            )

        for allocation in equity_allocations:
            if allocation.sector is None:
                raise ValueError(
                    f"Stock {allocation.ticker} must have a sector"
                )

        equity_weight = sum(
            (
                allocation.weight_pct
                for allocation in equity_allocations
            ),
            Decimal("0"),
        )

        if equity_weight > self.constraints_applied.max_equity_pct:
            raise ValueError(
                "Equity allocation exceeds the investor's maximum"
            )

        if (
            cash_allocation.weight_pct
            < self.constraints_applied.min_liquidity_pct
        ):
            raise ValueError(
                "Cash allocation is below the investor's minimum liquidity"
            )

        for allocation in equity_allocations:
            if (
                allocation.weight_pct
                > self.constraints_applied.max_single_stock_pct
            ):
                raise ValueError(
                    f"Allocation for {allocation.ticker} exceeds the maximum single-stock limit"
                )

        sector_weights = {}

        for allocation in equity_allocations:
            sector_weights[allocation.sector] = (
                sector_weights.get(
                    allocation.sector,
                    Decimal("0"),
                )
                + allocation.weight_pct
            )

        for sector, weight in sector_weights.items():
            if weight > self.constraints_applied.max_sector_pct:
                raise ValueError(
                    f"Allocation for sector {sector} exceeds the maximum sector limit"
                )

        holding_tickers = [
            action.ticker
            for action in self.holding_actions
        ]

        if len(holding_tickers) != len(set(holding_tickers)):
            raise ValueError(
                "Duplicate holding actions are not allowed"
            )

        if self.tie_breaker_triggered:
            if self.tie_breaker_reason is None:
                raise ValueError(
                    "A tie-breaker reason is required when the tie-breaker is used"
                )

            if not self.unresolved_sectors_resolved:
                raise ValueError(
                    "Tie-breaker must identify the unresolved sectors it resolved"
                )

        elif self.unresolved_sectors_resolved:
            raise ValueError(
                "Resolved sectors cannot be listed when the tie-breaker was not used"
            )

        audit_sequences = [
            entry.sequence
            for entry in self.audit_trail
        ]

        if len(audit_sequences) != len(set(audit_sequences)):
            raise ValueError(
                "Audit sequence numbers must be unique"
            )

        citation_ids = {
            citation.source_id
            for citation in self.source_citations
        }

        for entry in self.audit_trail:
            missing = (
                set(entry.source_citation_ids) - citation_ids
            )

            if missing:
                raise ValueError(
                    f"Unknown audit citations: {missing}"
                )

        return self