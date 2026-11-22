# Universe changelog

## psx_v1 — 2026-11-19

- Added the 18 company names, tickers, and sector groups supplied for the
  initial universe.
- Added the required `CASH` entry.
- Board lots, listing dates, and data-availability values remain unresolved
  (`null`); none were inferred.

## 2026-11-19 — sector export and offline company-data contracts

- Added a deterministic sector-map export generated only from the supplied
  YAML labels; the canonical mapping was not changed.
- Added provider-neutral schemas, normalization helpers, and offline quality
  checks. No external market data was collected and no Hugging Face upload was
  performed.

## psx_v1 — 2026-11-22 — final v1 company selection

- Replaced ENGRO with FATIMA and ILP with NCL in the 18-company investable
  list. The supplied final list and sectors are now enforced by tests.
- Retained ENGRO's historical identity in `historical_companies`; it is not an
  investable v1 ticker. Its reported January 2025 delisting is recorded, but
  exact `listed_to` and `listed_from` remain null pending verification.
- NCL and FATIMA were reported by the team as returning historical records
  from 2015-01-01 in initial pyPSX checks. This is availability evidence only,
  not a data-quality validation. `data_available: true` records the reported
  initial retrieval success for all 18 tickers, not completeness or quality.
  Board lots and exact listing metadata remain unresolved.
- No company financial data was collected.
