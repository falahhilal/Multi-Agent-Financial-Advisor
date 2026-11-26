# Shared Agent Contracts

Status: Draft v2 — proposed for team review.

## Architecture

1. The investor completes the onboarding questionnaire.
2. Risk Profiler receives the questionnaire and produces hard risk constraints.
3. Macro and Sector agents run independently and produce a shared MarketView.
4. Debate receives Macro and Sector outputs only.
5. Debate compares the sector-level market views and returns a final market view.
6. Unresolved sectors are passed to deterministic quant for conservative resolution.
7. Portfolio Constructor receives risk constraints and Debate output.
8. Deterministic quant receives the portfolio build context, risk constraints and final market view.
9. Quant selects individual PSX stocks, calculates allocations, applies constraints and generates audit data.

## Agent responsibilities

| Agent | Input | Output |
|---|---|---|
| Risk Profiler | Investor questionnaire | Risk constraints |
| Macro | Macroeconomic indicators and retrieved passages | Macro stance, findings and MarketView |
| Sector | Company data and news sentiment | Sentiment summary and MarketView |
| Debate | Macro and Sector outputs only | Final MarketView, debate arguments and winning agent |
| Portfolio Constructor | Risk constraints and Debate output | Portfolio explanation |
| Quant | MarketView, risk constraints and PortfolioBuildContext | Stock selection, allocations, metrics and holding actions |

## Important boundaries

- Macro and Sector must never receive investor questionnaire answers or risk constraints.
- Debate must not receive Risk Profiler output or investor constraints.
- Portfolio Constructor must not receive the raw questionnaire, investment capital or existing holdings.
- PortfolioBuildContext is a deterministic quant input and must not be sent to an LLM.
- Debate decides sector-level market views, not individual stock selections.
- Quant selects individual stocks from the versioned PSX universe.
- Quant calculates all portfolio weights, PKR allocations and risk metrics.
- Quant applies single-stock, sector, equity and liquidity constraints.
- All source citations must refer to actual source records.
- All models reject undeclared fields.

## Validation rules

- Confidence scores range from 0 to 1.
- Percentages range from 0 to 100.
- MarketView must cover every sector in the active universe version.
- Portfolio weights must sum to exactly 100%.
- Portfolio weights use two decimal places.
- The rounding remainder is assigned to CASH.
- A portfolio must contain exactly one CASH allocation.
- Equity allocation must respect maximum equity.
- Cash allocation must respect minimum liquidity.
- Each individual stock must respect maximum single-stock allocation.
- Each sector must respect maximum sector allocation.
- Rebalancing must respect must_keep holdings or explicitly report a constraint conflict.
- Every audit entry must have an actor attribution and valid source citation references.

## Schema version

Current proposed version: 2.0.

Changes to required fields or field meanings require team review.
