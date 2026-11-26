# ADR-0006: Shared Market View and Deterministic Quant Tie-Breaker

Status: Proposed — pending team review.

## Context

The Macro and Sector agents currently produce different output formats.
This prevents the Debate Facilitator from comparing their conclusions
against a common proposition.

The original proposal also gives the Debate Facilitator access to investor
risk constraints and assigns it the conservative-default tie-breaker.

This creates a risk that investor preferences influence market debate.

The project also requires individual stock selection and portfolio
allocation to be deterministic and reproducible.

## Proposed decision

1. Macro and Sector will produce a shared MarketView.
2. MarketView will contain a view for every sector in the active PSX universe
   and an overall stock-market view.
3. Debate will receive Macro and Sector outputs only.
4. Debate will decide sector-level views, not individual stocks.
5. Debate will record arguments, round numbers, reply references,
   source citations and the winning agent.
6. Unresolved sector decisions will be passed to deterministic quant.
7. Quant will apply the conservative-default tie-breaker to unresolved
   decisions and record whether it was triggered.
8. Quant will select individual tickers and calculate portfolio weights.
9. PortfolioBuildContext will contain investment capital and enriched
   existing holdings. It will be consumed by deterministic quant and
   never sent to an LLM.

## Consequences

- Market analysis remains independent of investor risk preferences.
- Debate decisions become comparable and testable.
- Stock selection and allocation become reproducible.
- The Portfolio Constructor LLM does not receive raw investor financial data.
- The original proposal's Debate Facilitator responsibilities and FR-07
  tie-breaker wording must be reconciled if this ADR is accepted.
- The orchestration and audit trail must record the quant tie-breaker.

## Approval

This ADR is a proposal for team review. It does not override the original
proposal until formally accepted.
