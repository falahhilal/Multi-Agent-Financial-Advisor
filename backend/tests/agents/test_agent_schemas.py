from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import (
    AllocationLine,
    AuditTrailEntry,
    DebateArgument,
    DebateInput,
    DebateOutput,
    InvestorQuestionnaire,
    MacroAgentInput,
    MacroAgentOutput,
    MacroFinding,
    MacroIndicator,
    MarketView,
    OverallStockView,
    PortfolioBuildContext,
    PortfolioConstructorOutput,
    PricedExistingHolding,
    RiskConstraints,
    SectorAgentOutput,
    SectorMarketView,
    SourceCitation,
)


AS_OF_DATE = date(2026, 11, 26)
AS_OF_DATETIME = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)

SECTORS = [
    "Commercial Banks",
    "Oil & Gas Exploration",
    "Cement",
    "Fertilizer",
    "Technology & Communication",
    "Textile",
]


def make_citation(source_id="src-1"):
    return SourceCitation(
        source_id=source_id,
        source_name="Test source",
        url="https://example.com/source",
        published_at=AS_OF_DATETIME,
        retrieved_at=AS_OF_DATETIME,
    )


def make_market_view():
    return MarketView(
        universe_version="psx_v1",
        sector_views=[
            SectorMarketView(
                sector=sector,
                stance="neutral",
                confidence_score=Decimal("0.80"),
                source_citation_ids=["src-1"],
            )
            for sector in SECTORS
        ],
        overall_stock_view=OverallStockView(
            view="hold",
            confidence_score=Decimal("0.80"),
            source_citation_ids=["src-1"],
        ),
    )


def make_constraints():
    return RiskConstraints(
        risk_level="moderate",
        max_equity_pct=Decimal("70"),
        min_liquidity_pct=Decimal("30"),
        max_single_stock_pct=Decimal("20"),
        max_sector_pct=Decimal("40"),
        time_horizon="medium",
    )


def make_macro_output():
    return MacroAgentOutput(
        status="success",
        as_of=AS_OF_DATETIME,
        macro_stance="neutral",
        key_findings=[
            MacroFinding(
                finding="Inflation trend is moderating",
                impact="positive",
                explanation="This is a test explanation",
                source_citation_ids=["src-1"],
            )
        ],
        market_view=make_market_view(),
        confidence_score=Decimal("0.80"),
        source_citations=[make_citation()],
    )


def make_sector_output():
    return SectorAgentOutput(
        status="success",
        as_of_date=AS_OF_DATE,
        market_view=make_market_view(),
        sentiment_summary="Overall sentiment is neutral",
        confidence_score=Decimal("0.80"),
        source_citations=[make_citation()],
    )


def make_debate_output():
    return DebateOutput(
        status="success",
        as_of_date=AS_OF_DATE,
        debate_summary="Both agents broadly agree",
        arguments=[
            DebateArgument(
                argument_id="arg-1",
                round_number=1,
                agent="macro",
                claim="The market outlook is neutral",
                supporting_reason="Macro conditions are mixed",
                evidence_source_ids=["src-1"],
            ),
            DebateArgument(
                argument_id="arg-2",
                round_number=2,
                agent="sector",
                claim="Sector evidence supports a neutral outlook",
                supporting_reason="Company and sentiment evidence is mixed",
                reply_to_argument_id="arg-1",
                evidence_source_ids=["src-1"],
            ),
        ],
        market_view=make_market_view(),
        winning_agent="consensus",
        unresolved_sectors=[],
        confidence_score=Decimal("0.80"),
        source_citations=[make_citation()],
    )


def make_allocation(ticker, sector, weight, amount, rationale="Test allocation"):
    return AllocationLine(
        ticker=ticker,
        sector=sector,
        weight_pct=Decimal(weight),
        amount_pkr=Decimal(amount),
        rationale=rationale,
    )


def make_valid_allocations():
    return [
        make_allocation("HBL", "Commercial Banks", "20.00", "20000.00"),
        make_allocation("UBL", "Commercial Banks", "20.00", "20000.00"),
        make_allocation("EFERT", "Fertilizer", "20.00", "20000.00"),
        make_allocation("FFC", "Fertilizer", "10.00", "10000.00"),
        make_allocation("CASH", None, "30.00", "30000.00", "Liquidity reserve"),
    ]


def make_portfolio_output(**overrides):
    data = {
        "status": "success",
        "as_of_date": AS_OF_DATE,
        "portfolio_mode": "new",
        "total_portfolio_value_pkr": Decimal("100000.00"),
        "allocations": make_valid_allocations(),
        "holding_actions": [],
        "constraints_applied": make_constraints(),
        "metrics": None,
        "debate_summary": "The agents reached a broad consensus",
        "winning_agent": "consensus",
        "tie_breaker_triggered": False,
        "tie_breaker_reason": None,
        "unresolved_sectors_resolved": [],
        "confidence_score": Decimal("0.80"),
        "source_citations": [make_citation()],
        "audit_trail": [
            AuditTrailEntry(
                sequence=1,
                timestamp=AS_OF_DATETIME,
                actor="quant",
                step="Portfolio allocation",
                explanation="Calculated allocations using deterministic rules",
                source_citation_ids=["src-1"],
            )
        ],
        "warnings": [],
    }
    data.update(overrides)
    return PortfolioConstructorOutput(**data)


# ---------------------------------------------------------------------
# Risk Profiler and questionnaire tests
# ---------------------------------------------------------------------


def test_valid_risk_constraints():
    constraints = make_constraints()

    assert constraints.max_equity_pct == Decimal("70")
    assert constraints.min_liquidity_pct == Decimal("30")
    assert constraints.max_sector_pct == Decimal("40")


def test_risk_constraints_reject_equity_and_liquidity_over_100():
    with pytest.raises(ValidationError):
        RiskConstraints(
            risk_level="moderate",
            max_equity_pct=Decimal("80"),
            min_liquidity_pct=Decimal("30"),
            max_single_stock_pct=Decimal("20"),
            max_sector_pct=Decimal("40"),
            time_horizon="medium",
        )


def test_risk_constraints_reject_single_stock_limit_above_equity_limit():
    with pytest.raises(ValidationError):
        RiskConstraints(
            risk_level="moderate",
            max_equity_pct=Decimal("50"),
            min_liquidity_pct=Decimal("30"),
            max_single_stock_pct=Decimal("60"),
            max_sector_pct=Decimal("40"),
            time_horizon="medium",
        )


def test_risk_constraints_reject_sector_limit_above_equity_limit():
    with pytest.raises(ValidationError):
        RiskConstraints(
            risk_level="moderate",
            max_equity_pct=Decimal("50"),
            min_liquidity_pct=Decimal("30"),
            max_single_stock_pct=Decimal("20"),
            max_sector_pct=Decimal("60"),
            time_horizon="medium",
        )


def test_new_portfolio_questionnaire_requires_positive_capital():
    with pytest.raises(ValidationError):
        InvestorQuestionnaire(
            risk_tolerance="medium",
            time_horizon="medium",
            investment_goal="balanced_growth",
            investment_experience="beginner",
            maximum_acceptable_loss_pct=Decimal("20"),
            liquidity_need="medium",
            portfolio_mode="new",
            investable_capital_pkr=Decimal("0"),
            reaction_to_20pct_loss="hold",
        )


def test_rebalance_questionnaire_requires_existing_holdings():
    with pytest.raises(ValidationError):
        InvestorQuestionnaire(
            risk_tolerance="medium",
            time_horizon="medium",
            investment_goal="balanced_growth",
            investment_experience="beginner",
            maximum_acceptable_loss_pct=Decimal("20"),
            liquidity_need="medium",
            portfolio_mode="rebalance",
            investable_capital_pkr=Decimal("0"),
            existing_holdings=[],
            reaction_to_20pct_loss="hold",
        )


def test_questionnaire_rejects_duplicate_existing_holdings():
    with pytest.raises(ValidationError):
        InvestorQuestionnaire(
            risk_tolerance="medium",
            time_horizon="medium",
            investment_goal="balanced_growth",
            investment_experience="beginner",
            maximum_acceptable_loss_pct=Decimal("20"),
            liquidity_need="medium",
            portfolio_mode="rebalance",
            investable_capital_pkr=Decimal("0"),
            existing_holdings=[
                {"ticker": "HBL", "quantity": Decimal("10")},
                {"ticker": "HBL", "quantity": Decimal("5")},
            ],
            reaction_to_20pct_loss="hold",
        )


# ---------------------------------------------------------------------
# Shared MarketView tests
# ---------------------------------------------------------------------


def test_market_view_accepts_all_required_sectors():
    market_view = make_market_view()

    assert len(market_view.sector_views) == 6
    assert {item.sector for item in market_view.sector_views} == set(SECTORS)


def test_market_view_rejects_missing_sector():
    with pytest.raises(ValidationError):
        MarketView(
            universe_version="psx_v1",
            sector_views=[
                SectorMarketView(
                    sector=sector,
                    stance="neutral",
                    confidence_score=Decimal("0.80"),
                    source_citation_ids=["src-1"],
                )
                for sector in SECTORS[:-1]
            ],
            overall_stock_view=OverallStockView(
                view="hold",
                confidence_score=Decimal("0.80"),
                source_citation_ids=["src-1"],
            ),
        )


def test_market_view_rejects_duplicate_sector():
    views = make_market_view().sector_views
    views[-1] = views[0]

    with pytest.raises(ValidationError):
        MarketView(
            universe_version="psx_v1",
            sector_views=views,
            overall_stock_view=OverallStockView(
                view="hold",
                confidence_score=Decimal("0.80"),
                source_citation_ids=["src-1"],
            ),
        )


# ---------------------------------------------------------------------
# Macro Agent tests
# ---------------------------------------------------------------------


def test_macro_input_accepts_timezone_aware_timestamps():
    macro_input = MacroAgentInput(
        as_of=AS_OF_DATETIME,
        indicators=[
            MacroIndicator(
                indicator_name="Policy rate",
                value=Decimal("10.5"),
                unit="percent",
                dataset="sbp_policy_rate",
                series_key="policy_rate",
                observation_date=AS_OF_DATE,
                published_at=AS_OF_DATETIME,
                source_citation_id="src-1",
            )
        ],
        source_citations=[make_citation()],
    )

    assert macro_input.indicators[0].dataset == "sbp_policy_rate"


def test_macro_input_rejects_naive_as_of_timestamp():
    with pytest.raises(ValidationError):
        MacroAgentInput(
            as_of=datetime(2026, 11, 26, 12, 0),
            indicators=[
                {
                    "indicator_name": "Policy rate",
                    "value": Decimal("10.5"),
                    "unit": "percent",
                    "dataset": "sbp_policy_rate",
                    "series_key": "policy_rate",
                    "observation_date": AS_OF_DATE,
                    "published_at": AS_OF_DATETIME,
                    "source_citation_id": "src-1",
                }
            ],
            source_citations=[make_citation()],
        )


def test_macro_input_rejects_indicator_without_matching_citation():
    with pytest.raises(ValidationError):
        MacroAgentInput(
            as_of=AS_OF_DATETIME,
            indicators=[
                {
                    "indicator_name": "Policy rate",
                    "value": Decimal("10.5"),
                    "unit": "percent",
                    "dataset": "sbp_policy_rate",
                    "series_key": "policy_rate",
                    "observation_date": AS_OF_DATE,
                    "published_at": AS_OF_DATETIME,
                    "source_citation_id": "missing-source",
                }
            ],
            source_citations=[make_citation()],
        )


def test_macro_output_rejects_invalid_status():
    data = make_macro_output().model_dump()
    data["status"] = "unknown"

    with pytest.raises(ValidationError):
        MacroAgentOutput(**data)


# ---------------------------------------------------------------------
# Sector Agent tests
# ---------------------------------------------------------------------


def test_sector_output_accepts_valid_market_view():
    output = make_sector_output()

    assert output.status == "success"
    assert len(output.market_view.sector_views) == 6


def test_sector_output_rejects_invalid_status():
    with pytest.raises(ValidationError):
        SectorAgentOutput(
            status="unknown",
            as_of_date=AS_OF_DATE,
            market_view=make_market_view(),
            sentiment_summary="Neutral",
            confidence_score=Decimal("0.80"),
            source_citations=[make_citation()],
        )


# ---------------------------------------------------------------------
# Debate Agent tests
# ---------------------------------------------------------------------


def test_debate_does_not_receive_risk_constraints():
    assert "risk_constraints" not in DebateInput.model_fields


def test_debate_input_accepts_matching_agent_dates():
    debate_input = DebateInput(
        as_of_date=AS_OF_DATE,
        macro_output=make_macro_output(),
        sector_output=make_sector_output(),
    )

    assert debate_input.as_of_date == AS_OF_DATE


def test_debate_input_rejects_mismatched_dates():
    with pytest.raises(ValidationError):
        DebateInput(
            as_of_date=date(2026, 11, 25),
            macro_output=make_macro_output(),
            sector_output=make_sector_output(),
        )


def test_debate_output_accepts_reply_to_earlier_opposing_argument():
    output = make_debate_output()

    assert output.arguments[1].reply_to_argument_id == "arg-1"


def test_debate_output_rejects_reply_to_unknown_argument():
    output = make_debate_output().model_dump()
    output["arguments"][1]["reply_to_argument_id"] = "does-not-exist"

    with pytest.raises(ValidationError):
        DebateOutput(**output)


def test_debate_output_rejects_reply_to_same_agent():
    output = make_debate_output().model_dump()
    output["arguments"][1]["agent"] = "macro"

    with pytest.raises(ValidationError):
        DebateOutput(**output)


# ---------------------------------------------------------------------
# Portfolio Build Context tests
# ---------------------------------------------------------------------


def test_new_portfolio_build_context_accepts_positive_capital():
    context = PortfolioBuildContext(
        as_of_date=AS_OF_DATE,
        portfolio_mode="new",
        investable_capital_pkr=Decimal("100000"),
    )

    assert context.investable_capital_pkr == Decimal("100000")


def test_rebalance_build_context_requires_existing_holdings():
    with pytest.raises(ValidationError):
        PortfolioBuildContext(
            as_of_date=AS_OF_DATE,
            portfolio_mode="rebalance",
            investable_capital_pkr=Decimal("0"),
            existing_holdings=[],
        )


def test_rebalance_build_context_requires_price_citation():
    with pytest.raises(ValidationError):
        PortfolioBuildContext(
            as_of_date=AS_OF_DATE,
            portfolio_mode="rebalance",
            investable_capital_pkr=Decimal("0"),
            existing_holdings=[
                PricedExistingHolding(
                    ticker="HBL",
                    quantity=Decimal("10"),
                    current_price_pkr=Decimal("200"),
                    price_source_citation_id="missing-source",
                )
            ],
            source_citations=[make_citation()],
        )


# ---------------------------------------------------------------------
# Portfolio Constructor output tests
# ---------------------------------------------------------------------


def test_portfolio_accepts_valid_allocations():
    output = make_portfolio_output()

    assert sum(
        item.weight_pct for item in output.allocations
    ) == Decimal("100.00")

    assert sum(
        item.amount_pkr for item in output.allocations
    ) == output.total_portfolio_value_pkr


def test_portfolio_requires_exactly_one_cash_allocation():
    allocations = [
        make_allocation("HBL", "Commercial Banks", "20.00", "20000.00"),
        make_allocation("UBL", "Commercial Banks", "20.00", "20000.00"),
        make_allocation("EFERT", "Fertilizer", "30.00", "30000.00"),
        make_allocation("MEBL", "Commercial Banks", "30.00", "30000.00"),
    ]

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_weights_must_equal_100():
    allocations = make_valid_allocations()
    allocations[-1] = make_allocation(
        "CASH", None, "20.00", "20000.00", "Liquidity reserve"
    )

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_respects_single_stock_limit():
    allocations = [
        make_allocation("HBL", "Commercial Banks", "30.00", "30000.00"),
        make_allocation("UBL", "Commercial Banks", "10.00", "10000.00"),
        make_allocation("EFERT", "Fertilizer", "30.00", "30000.00"),
        make_allocation("CASH", None, "30.00", "30000.00"),
    ]

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_respects_sector_limit():
    allocations = [
        make_allocation("HBL", "Commercial Banks", "15.00", "15000.00"),
        make_allocation("UBL", "Commercial Banks", "15.00", "15000.00"),
        make_allocation("MEBL", "Commercial Banks", "15.00", "15000.00"),
        make_allocation("EFERT", "Fertilizer", "15.00", "15000.00"),
        make_allocation("FFC", "Fertilizer", "10.00", "10000.00"),
        make_allocation("CASH", None, "30.00", "30000.00"),
    ]

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_respects_minimum_liquidity():
    allocations = [
        make_allocation("HBL", "Commercial Banks", "30.00", "30000.00"),
        make_allocation("UBL", "Commercial Banks", "30.00", "30000.00"),
        make_allocation("EFERT", "Fertilizer", "30.00", "30000.00"),
        make_allocation("CASH", None, "10.00", "10000.00"),
    ]

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_allocation_amounts_must_equal_total_value():
    allocations = make_valid_allocations()
    allocations[0] = make_allocation(
        "HBL", "Commercial Banks", "20.00", "19000.00"
    )

    with pytest.raises(ValidationError):
        make_portfolio_output(allocations=allocations)


def test_portfolio_tie_breaker_requires_reason():
    with pytest.raises(ValidationError):
        make_portfolio_output(
            tie_breaker_triggered=True,
            tie_breaker_reason=None,
            unresolved_sectors_resolved=["Commercial Banks"],
        )


def test_portfolio_audit_trail_rejects_unknown_citation():
    audit = [
        AuditTrailEntry(
            sequence=1,
            timestamp=AS_OF_DATETIME,
            actor="quant",
            step="Portfolio allocation",
            explanation="Calculated allocations",
            source_citation_ids=["unknown-source"],
        )
    ]

    with pytest.raises(ValidationError):
        make_portfolio_output(audit_trail=audit)
