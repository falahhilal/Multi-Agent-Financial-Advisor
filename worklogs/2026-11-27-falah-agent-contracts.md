# Session 2026-11-27 — Shared Agent Contracts and Schema Validation

**Who:** Falah Hilal (design, implementation and validation); ChatGPT (technical assistance)

## Decisions taken

- Finalized the shared Pydantic input/output contract design for all five agents: Risk Profiler, Macro, Sector, Debate and Portfolio Constructor.
- Standardized `MarketView` as the shared sector-level representation used by Macro, Sector and Debate, covering all six sectors in `psx_v1`.
- Kept Debate independent of investor risk constraints. Debate receives Macro and Sector outputs only.
- Separated `PortfolioBuildContext` from the Portfolio Constructor LLM input so that investment capital and existing holdings remain restricted to deterministic quant calculations.
- Kept individual stock selection, portfolio weights, PKR allocations and constraint enforcement in deterministic Python logic rather than LLM-generated calculations.
- Merged PR #15 directly into `main` on 2026-11-27. The contracts and associated documentation remain a proposed design pending further team review and integration.

## Work done

### Shared Pydantic contracts

- Completed shared schemas for all five agents:
  - `RiskProfilerInput` and `RiskProfilerOutput`
  - `MacroAgentInput` and `MacroAgentOutput`
  - `SectorAgentInput` and `SectorAgentOutput`
  - `DebateInput` and `DebateOutput`
  - `PortfolioConstructorInput` and `PortfolioConstructorOutput`
- Added common models for source citations, retrieved passages, agent metadata, confidence scores and percentage validation.
- Added shared `MarketView`, `SectorMarketView` and `OverallStockView` models to standardize market analysis across agents.
- Added validation for required sector coverage, date consistency, citation references, valid agent statuses, debate argument references and portfolio constraints.
- Added portfolio validation for allocation totals, CASH allocation, stock and sector limits, liquidity requirements, amount consistency and audit-trail references.
- Added `PortfolioBuildContext` and `PricedExistingHolding` to separate deterministic portfolio calculations from LLM inputs.

### Architecture and documentation

- Added `docs/agent-contracts.md` documenting the five agents' responsibilities, inputs, outputs, data flow, boundaries and validation rules.
- Added `docs/adr/0006-market-debate-and-quant-tiebreaker.md` documenting the proposed shared MarketView, sector-level debate and deterministic quant tie-breaker.
- Updated `backend/app/schemas/__init__.py` to expose the shared schema models.
- Added and expanded `backend/tests/agents/test_agent_schemas.py` to validate the shared contracts and reject invalid inputs and outputs.

### News archive bug fix

- Fixed the zero-minute time-budget boundary condition in `backend/jobs/news_nation_archive.py`.
- Changed the deadline check from `>` to `>=` so a zero-minute budget stops immediately instead of potentially processing the first day.
- Verified the fix with the time-budget test.

### Validation and merge

- Full backend test suite: **367 passed, 1 warning** (123.42 seconds).
- `python -m compileall -q app`: passed.
- `git diff --check`: passed.
- Created and pushed `feature/agent-contracts` and opened PR #15, titled **Add shared schemas and contracts for all five agents**.
- PR #15 was successfully merged into `main` on 2026-11-27.

**Merge commit:** `1db628e1e06131f2b9885e419f4619c00f806b98`

**PR:** https://github.com/falahhilal/Multi-Agent-Financial-Advisor/pull/15

## Problems found

- The shared schemas define the expected agent interfaces, but the actual agent implementations have not yet been integrated with all contracts.
- The proposed architecture and ADR still require team review and agreement before being treated as formally approved project decisions.
- End-to-end integration between Risk Profiler, Macro, Sector, Debate and Portfolio Constructor remains outstanding.
- Source citation references are structurally validated, but complete verification against every underlying financial data record remains an integration-level responsibility.

## Still open

- Integrate the shared contracts into the actual implementations of all five agents.
- Implement and test the Sector Analysis Agent using the existing PSX company data and news sentiment inputs.
- Coordinate with Maryam and Mahrukh to ensure their agent implementations follow the shared schemas.
- Obtain team feedback on the proposed contract design and ADR-0006.
- Add integration tests covering the complete agent workflow and data handoffs.

## Next session should

- Begin implementation of the Sector Analysis Agent using the merged schemas and existing PSX company data.
- Coordinate the Sector Agent output format with Maryam so that it can be consumed by the Debate Agent.
- Continue frontend development using the shared contracts as the expected API data structures.