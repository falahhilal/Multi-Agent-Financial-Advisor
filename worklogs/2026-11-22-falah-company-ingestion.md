# Session 2026-11-22 — Stream A and shared agent contracts

**Who:** Falah Hilal

## Decisions taken

- Finalized v1 at the 18 user-supplied active tickers: FATIMA replaces ENGRO and NCL replaces ILP. ENGRO remains historical universe metadata; unknown dates and other unverified metadata were not invented.
- Kept `data/` payloads gitignored, with `data/README.md` as the documented exception. Canonical manifests remain in `backend/data_manifests/`.
- Required public availability metadata for point-in-time action and fundamental reads. Records without it are retained in storage but excluded from `as_of` results.
- Kept adjusted close unavailable and preserved the provider's as-traded prices and adjustment fields without applying an unverified adjustment method.
- Kept the five shared agent contracts typed with Pydantic and defined explicit input boundaries and data flow between Risk Profiler, Macro, Sector, Debate and Portfolio Constructor.
- Kept `feature/agent-contracts` and PR #15 open for teammate review; the PR remains unmerged.

## Work done

### Company universe and pre-ingestion setup

- Finalized the 18-company universe, preserved ENGRO's historical identity, updated validation and generated the sector mapping manifest.
- Prepared company schemas, manifest conventions, price adapter, point-in-time filters, offline tests, source documentation, data layout README and project plan.
- Ran `python -m jobs.company_sector_mapping` from `backend/`: exported 18 canonical sector mappings locally; no upload occurred in that setup step.
- Ran `pytest tests/company tests/universe -q`: 34 passed at setup. A full `pytest -q` collection did not complete because this environment lacked `beautifulsoup4` and `feedparser`, which existing Stream C tests require.

### Historical prices

- Ran `python -m jobs.company_ingest` from `backend/` for `2015-01-01` through `2026-11-20`. All 18 ticker requests returned data: 53,012 source rows, 52,950 accepted normalized rows and 62 rejected OHLC rows across 16 tickers. Seventeen tickers' accepted dates span `2015-01-01`–`2026-11-20`; SYS begins `2015-02-06`.
- Retained 18 immutable raw JSON snapshots and 18 normalized Parquet files. The run reported idempotent merges and no revised keys. Quality statuses: 16 tickers `issues`, 2 `incomplete`, none `pass`; session gaps, suspicious-movement policy, corporate-action consistency and PIT fundamentals were unevaluated.
- Published company outputs to private `fyp-psx-advisor/fyp-data`; remote paths, sizes and SHA-256 checks were verified. Price manifest: `company/prices/manifests/20261122T083353Z-cd11b23a.json`; run: `company/prices/runs/20261122T083353Z-cd11b23a/run.json`.
- The 62 rejected rows comprise 54 OHLC inconsistencies (50 open-outside-low/high and 4 close-outside-low/high) and 8 zero-OHLC rows despite positive closes (6 MEBL, 2 SYS). No independent price correction was established; rejected rows remain rejected.

### Corporate actions and fundamentals

- Inspected and used the installed pyPSX 2.8.3 methods. The auxiliary run fetched 18 tickers for each endpoint over the requested 2015-01-01–2026-11-20 window without repeating price collection.
- Corporate actions: 386 rows (360 cash dividends, 2 bonus issues, 3 stock splits, 5 rights issues, 16 other). No source publication/availability dates were returned; ex-date was not substituted for public availability.
- Fundamentals: 13,379 annual statement-item rows, with observed year coverage 2015–2025 or 2015–2026 by ticker. The inspected source fields were statement type, item, year and value; publication dates, units and currency were absent. These data remain excluded from PIT reads.
- Published 18 raw snapshots, 18 normalized Parquet files, 20 quality/coverage reports, a run record and a manifest for each dataset. All 58 remote files per dataset were checked for size and SHA-256. Action data/manifest revisions: `f36551e562b4c1db46553bc4d9b6ca96f67c23fd` / `3f25136328dc3a0d3f595061a8613229cbb1c75d`. Fundamentals revisions: `ae5d72543dbfbcef7f4adbd4bf81bb26eb921929` / `64ea2cddd199496f7d0de61aecf7f2133b91b8f9`.

### Shared agent schemas and contracts

- Created shared Pydantic input/output contracts for all five FYP agents: Risk Profiler, Macro, Sector, Debate and Portfolio Constructor.
- Added common models and validation rules and defined agent data flow and boundaries, including each agent's permitted inputs.
- Added schema tests; all 6 focused schema tests passed. Added `docs/agent-contracts.md`.
- Created and pushed `feature/agent-contracts` and opened PR #15 for teammate review. The PR remains unmerged.

### Checks and documentation

- Later targeted checks: `python -m pytest tests/company tests/universe -q` — 54 passed with one AnyIO deprecation warning; `python universe/validate_universe.py` — valid 18-company universe.
- `python -m compileall -q jobs/company_as_of.py jobs/company_pypsx.py jobs/company_ingest.py jobs/company_quality.py jobs/company_schemas.py` passed.
- Updated `docs/data-sources.md`, `docs/plan.md`, `backend/jobs/company_data.md`, `AGENTS.md` and `CLAUDE.md` with implementation status, limitations and session continuity requirements.

## Problems found

- pyPSX returned 62 internally inconsistent OHLC rows. Raw snapshots remain unchanged; affected rows were not normalized or repaired.
- No verified PSX trading calendar or ticker-specific suspension evidence was available to classify price gaps.
- Corporate actions have ex-dates and provider action/factor fields but no public-availability dates; event completeness is unverified.
- Fundamentals lack publication/availability dates, units and currency. Do not use them in historical point-in-time backtests.
- `adj_factor` was 1.0 on the 53,012 price rows and `tr_close` was null throughout. Adjusted and total-return prices remain unavailable pending reliable inputs and a validated method.
- The SDK emitted a pandas chained-assignment `FutureWarning` after successful requests; this is a compatibility warning, not a failed request.

## Deferred tasks — revisit before backtesting

These tasks are intentionally deferred, not forgotten. They do not block work on the other FYP streams. Revisit them before historical backtesting and before Stream A is marked complete. Do not repeat completed ingestion or upload work.

1. Investigate the 62 rejected price rows: 54 OHLC inconsistencies and 8 zero-OHLC rows. Correct only with independent evidence; otherwise keep them rejected.
2. Validate trading-session gaps using a reliable PSX calendar and ticker-specific suspension evidence.
3. Find historical fundamentals and corporate-actions sources with genuine publication/availability dates to improve PIT coverage.
4. Validate corporate-action adjustment methodology and produce adjusted or total-return prices only if reliable inputs are available.

## Still open

- Select trustworthy corporate-action and historical-fundamentals sources with genuine availability dates; verify scope and coverage before PIT use.
- Obtain approved exchange-session and suspension evidence and independently investigate rejected OHLC rows.
- Validate action history and a documented price-adjustment methodology. Stream A remains incomplete.
