# Session 2026-11-22 — Stream A company ingestion

**Who:** Falah (project owner; supplied credentials and authorized the run), Codex (implementation and execution)

## Decisions taken

- Falah authorized historical pyPSX price collection and private Hugging Face sharing for the stated scope; no broader source or redistribution permission was inferred.
- Codex kept adjusted close null, preserved as-traded prices and source factors, and did not collect actions or fundamentals without verified point-in-time availability and scope.

## Work done

- Codex repaired fundamentals `as_of` handling for both date and timestamp availability, tightened manifest checks, added tested pyPSX DataFrame normalization and snapshot handling, and implemented the offline-testable company ingestion path, quality reports and shared-helper upload integration.
- Codex verified both pyPSX credentials were loaded without displaying them and confirmed the target Hugging Face dataset was private.
- Codex ran `python -m jobs.company_ingest` from `backend/` for `2015-01-01` through `2026-11-20`. All 18 ticker requests returned data. Source provided 53,012 rows; 52,950 normalized rows passed validation; 62 OHLC-inconsistent source rows from 16 tickers were excluded and reported. Accepted ranges were `2015-01-01`–`2026-11-20` for 17 tickers and `2015-02-06`–`2026-11-20` for SYS.
- Raw snapshots: 18 JSON; normalized outputs: 18 Parquet. The run reported idempotent merges and no revised keys. Quality status: 16 ticker reports `issues`, 2 `incomplete`, none `pass`; session gaps, suspicious-move policy, corporate-action consistency, and point-in-time fundamentals remain unevaluated.
- Codex published under `company/` in `fyp-psx-advisor/fyp-data`. Raw, normalized, reports, run metadata, references and manifests were listed remotely; sizes and SHA-256 were checked. Price manifest path: `company/prices/manifests/20261122T083353Z-cd11b23a.json`; run path: `company/prices/runs/20261122T083353Z-cd11b23a/run.json`.
- Offline checks: `pytest tests/company tests/universe -q` (48 passed, one existing AnyIO deprecation warning); `python universe/validate_universe.py` (valid 18-company universe); `python -m compileall -q jobs/company_as_of.py jobs/company_pypsx.py jobs/company_ingest.py jobs/company_quality.py jobs/company_schemas.py` (passed).
- No corporate actions or fundamentals were collected. pyPSX exposes relevant APIs but inspected evidence did not establish public availability dates suitable for point-in-time backtests.
- Codex updated company source and run documentation, retained the generated canonical manifest, and removed temporary SDK inspection artifacts. No Git commit/push and no public upload occurred.

## Problems found

- pyPSX returned 62 daily bars with internally inconsistent OHLC values. The source snapshots remain unchanged; affected records were not normalized. Review them and determine whether there is a verified source explanation before changing validation.
- Complete PSX session/suspension evidence, suspicious-movement policy, PIT corporate-action source and PIT fundamentals source remain open. Stream A is incomplete.
- The pyPSX SDK emitted a pandas chained-assignment `FutureWarning` on successful requests; this is a compatibility warning, not a collection failure.

## Still open

- Review all flagged OHLC rows and establish whether pyPSX’s source semantics explain them.
- Select a permitted point-in-time corporate-action source and a historical fundamentals source with actual publication/availability dates.
- Approve a PSX session calendar, price-movement policy, backup/access procedure, and then reassess ticker-level completeness.

## Next session should

1. Review the 62 rejected raw rows against a verified source without mutating raw snapshots.
2. Resolve corporate-action and historical fundamental source availability and scope.
3. Re-run only after the above and quality policies are approved; preserve all current raw files and manifests.

## Deferred tasks — revisit before backtesting

These tasks are intentionally deferred, not forgotten. They do not block work on the other FYP streams. Revisit them before historical backtesting and before Stream A is marked complete. Do not repeat the completed ingestion or upload work.

1. Investigate the 62 rejected price rows: 54 OHLC inconsistencies and 8 zero-OHLC rows. Correct a row only with independent evidence; otherwise keep it rejected.
2. Validate trading-session gaps using a reliable PSX calendar and ticker-specific suspension evidence.
3. Find historical fundamentals and corporate-actions sources with genuine publication/availability dates to improve point-in-time coverage.
4. Validate the corporate-action adjustment methodology. Produce adjusted or total-return prices only if reliable inputs are available.
