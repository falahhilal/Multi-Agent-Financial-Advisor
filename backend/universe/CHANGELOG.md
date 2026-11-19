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
