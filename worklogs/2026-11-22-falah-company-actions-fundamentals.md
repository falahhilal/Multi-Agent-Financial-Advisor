# Session 2026-11-22 — Company actions and fundamentals

**Who:** Falah Hilal

## Decisions taken

- Falah kept pyPSX action and annual statement records out of point-in-time reads because the provider responses omit actual public-availability dates.
- Falah left derived adjusted and total-return price series unavailable because the existing price adjustment fields and action history do not establish a validated adjustment methodology.

## Work done

- Falah added/updated the Stream A auxiliary schemas, normalizers, PIT handling, quality summaries and offline tests. The loader now separates normalized output files when assembling its upload staging paths.
- Falah reused saved collection artifacts and did not call the price endpoint again. The 2026-11-22 pyPSX auxiliary run fetched 18 tickers for each endpoint over the requested 2015-01-01–2026-11-20 window. Actions: 386 rows (360 cash dividends, 2 bonus issues, 3 splits, 5 rights issues, 16 other); no public availability dates. Fundamentals: 13,379 annual statement-item rows, observed year coverage 2015–2025/2026 per ticker; no publication dates, units or currency.
- Falah published 18 raw JSON snapshots, 18 normalized Parquet files, 20 quality/coverage reports, one run record and one manifest for each dataset. Both folders have 58 files including the manifest. Remote sizes and SHA-256 content were verified.
- Corporate-action data/manifest revisions: `f36551e562b4c1db46553bc4d9b6ca96f67c23fd` / `3f25136328dc3a0d3f595061a8613229cbb1c75d`. Fundamentals data/manifest revisions: `ae5d72543dbfbcef7f4adbd4bf81bb26eb921929` / `64ea2cddd199496f7d0de61aecf7f2133b91b8f9`.
- Falah updated `docs/data-sources.md`, `docs/plan.md`, and `backend/jobs/company_data.md` to record actual results, source limits, remote paths/revisions and unresolved work.
- Falah ran `python -m pytest tests/company tests/universe -q` from `backend/`: 54 passed, one AnyIO deprecation warning. Falah ran `python universe/validate_universe.py`: valid 18-company universe.

## Problems found

- Corporate actions include ex-dates and provider factor fields but no announcement/publication/availability dates; event completeness is not verified.
- Fundamentals contain annual statement item/year/value but no publication/availability dates, units or currency. PIT reads exclude these records.
- The failed first upload attempt was caused by normalized paths being computed for mixed raw/report/run files. The path-listing bug was fixed and saved artifacts were uploaded successfully.
- The existing 62 rejected OHLC rows remain unresolved without independent authoritative evidence. There is no approved PSX trading calendar/suspension source, and adjusted prices remain unavailable.

## Still open

- Select a trustworthy source for corporate actions and historical fundamentals with genuine public-availability dates; verify coverage and terms before relying on it.
- Obtain/approve exchange-session and listing/suspension evidence; finish price gap classification and independent verification of rejected OHLC rows.
- Validate action history and a documented adjustment methodology before publishing adjusted or total-return prices. Stream A remains incomplete.

## Next session should

1. Resolve approved source/calendar decisions and evidence requirements.
2. Keep price and fundamentals PIT access gated on proven availability and validations.
3. Re-run only targeted Stream A tests after further changes.
