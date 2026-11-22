# Session 2026-11-19 — PSX universe v1

**Who:** Falah Hilal (requester); Codex (implementation)

## Decisions taken

- Falah Hilal supplied names, tickers, and sector groups only; missing board lots, listing dates, and data-availability values are represented as `null`.
- Falah Hilal requested a versioned YAML universe with a `CASH` line; structure and validation details are documented in `backend/universe/README.md`.

## Work done

- Codex created `backend/universe/psx_v1.yaml` with the 18 supplied companies and `CASH`.
- Codex added a YAML validator, validator tests, the universe README, and a v1 changelog entry.
- `python backend/universe/validate_universe.py` passed: 18 companies.
- From `backend/`, `pytest tests/universe -q` passed: 4 tests. Exact supplied name/ticker comparison passed, and `git diff --check` passed.
- Ruff could not run because the `ruff` command is not installed in the active environment.

## Problems found

- No existing universe file or validator was present. Required metadata values were not supplied and remain unresolved.

## Still open

- Confirm board lots, listing dates, and data-availability flags before using this file for ticker eligibility or data coverage decisions.

## Next session should

- Confirm unresolved metadata from approved project sources.
- Add confirmed values and record the change in the universe changelog.

---

## Company data pipeline source review — 2026-11-19

**Who:** Falah Hilal (requester); Codex (repository and source review)

### Reviewed

- Codex confirmed the current branch is `feature/company-data-pipeline`; existing universe files remain untracked and were preserved.
- Codex inspected the universe YAML/validator/docs, `backend/jobs/common.py`, existing manifest and common-helper tests, backend dependencies, company-data plan/spec notes, and the existing worklog.
- Codex reviewed PSX Data Services, Deutsche Börse PSX market-data access, EODHD KAR coverage/API/terms/academic offer, and the yfinance project's Yahoo terms guidance. No market-data API was queried and no data was collected.

### Findings and open decisions

- Falah Hilal's supplied sector labels already map all 18 tickers in the universe YAML. The YAML should remain the canonical sector mapping unless Falah approves a different taxonomy.
- Direct PSX EOD/history/corporate-announcement data remains blocked by the project's written-permission requirement. PSX advertises historical and EOD products and says usage requires rights/licensing; its page lists Deutsche Börse and licensed data vendors.
- EODHD documents a Karachi (`KAR`) exchange page, EOD OHLCV, split/dividend endpoints, and broad fundamentals APIs. Its standard personal-use terms prohibit team sharing/redistribution. Its academic page advertises university access and student distribution tools, but permission for this project's private HF dataset, team access, retention, and any publication must be confirmed in writing first. Provider completeness/accuracy is not guaranteed and exact coverage of all 18 tickers is unverified.
- Yahoo `.KA` / yfinance is not selected: its package describes itself as unofficial, for personal use, and does not establish complete coverage or redistribution permission for our shared research archive.
- Official issuer annual/quarterly reports are the preferred primary source for fundamentals if each issuer's site terms allow acquisition and retention. Coverage and machine-readable access remain unverified per issuer. PSX PUCARS/portal automation is not an allowed fallback without permission.
- Price files should preserve as-traded OHLCV separately from vendor adjusted-close values and corporate actions. Per-ticker gap checks require a licensed/approved PSX trading calendar and status data; weekdays alone cannot distinguish holidays, halts, suspensions, and missing observations.

### Proposed artifacts (pending Falah's approval and source rights)

- Prices: `backend/jobs/company_prices_<source>.py`; HF `company/prices/raw/<source>/` and normalized Parquet under `company/prices/processed/`; manifest `backend/data_manifests/company_prices.json`.
- Corporate actions: `backend/jobs/company_actions_<source>.py`; HF `company/corporate_actions/raw/<source>/` and normalized events under `company/corporate_actions/processed/`; manifest `backend/data_manifests/company_corporate_actions.json`.
- Fundamentals: `backend/jobs/company_fundamentals_<source>.py`; immutable permitted source reports under `company/fundamentals/raw/<source>/` and period facts under `company/fundamentals/processed/`; manifest `backend/data_manifests/company_fundamentals.json`.
- Sector mapping: keep the supplied mapping in `backend/universe/psx_v1.yaml`; validate coverage and references as part of quality checks rather than create a duplicate source dataset.
- Quality: `backend/jobs/company_quality.py`; per-ticker reports under `company/quality/`; manifest `backend/data_manifests/company_quality.json`.
- Reuse `jobs.common` for polite retrieval where allowed, HF path guarding/upload/download, and manifests; use existing PyYAML, pandas, and PyArrow requirements. Any source requiring an API key in a query string needs explicit secret-redaction handling before using the helper so credentials cannot appear in URLs/logs.

### Approval gates

- Obtain written PSX rights for the intended acquisition, retention, team use, and HF storage, or written EODHD academic/vendor terms that expressly cover the same use. No source has been authorized for collection yet.
- Falah Hilal later confirmed the 18 supplied sector labels as the project's
  canonical taxonomy; see the implementation entry below.
- Confirm the historical date range and accepted completeness threshold after a licensed source can be evaluated; no coverage figures have been measured against the project's exact 18 tickers.
- At the source-review checkpoint, implementation and collection had not
  started. The later offline implementation began after Falah Hilal's
  authorization and is recorded below.

## Offline company-data framework implementation — 2026-11-19

**Who:** Falah Hilal (requester); Codex (implementation)

### Work completed

- Codex added `backend/jobs/company_sector_mapping.py`. It reads only
  `backend/universe/psx_v1.yaml`, validates exactly 18 unique v1 ticker/sector
  rows, and deterministically exports the user-supplied mapping.
- Falah Hilal approved the existing user-supplied sector labels as the
  canonical taxonomy; Codex kept every label unchanged.
- Codex generated the local, gitignored
  `data/processed/company/sector_mapping.csv` (18 rows) and
  `backend/data_manifests/company_sector_mapping.json` (18 rows, 1 file,
  SHA-256 recorded, `hf_path: null`, upload status explicitly local-only).
  The data was not uploaded to Hugging Face.
- Codex added provider-neutral Pydantic schemas for daily OHLCV, corporate
  actions, fundamentals, and sector mapping, plus decimal/date normalization
  and idempotent natural-key merge helpers. Changed existing records are
  returned as revisions for review; conflicting duplicates in one batch fail.
- Codex added offline quality checks for all five company-data areas, including
  assembled per-ticker reports. Validators flag invalid rows, duplicate keys,
  unmapped tickers, configured gaps/moves, event-date anomalies, and missing,
  stale, or not-yet-public fundamentals. They do not correct or discard data.
- Codex documented schemas, local export, storage layout, approval gates, and
  quality checks that remain deferred in `backend/jobs/company_data.md`,
  `backend/universe/README.md`, and Falah's section of `docs/data-sources.md`.
  Updated `backend/universe/CHANGELOG.md`; existing sector labels were not
  changed.
- No source API calls, financial data downloads, HF API calls/uploads, or costs.
  No Mahrukh or Maryam files were modified. Falah Hilal later explicitly
  authorized a commit; no push was authorized or performed.

### Dataset and files touched

- Derived sector map: source `backend/universe/psx_v1.yaml`; generated
  2026-11-19; 18 rows in one local CSV; 18/18 ticker coverage; no market-data
  date range; no gaps against the 18 supplied universe tickers. Not uploaded.
- Test-only synthetic fixtures: 2 price rows, 1 corporate-action row, and 1
  fundamental row in `backend/tests/company/fixtures/company_records.json`;
  these are not collected observations and have no market-data date range.
- Implementation/docs/tests touched: `backend/jobs/company_sector_mapping.py`,
  `company_schemas.py`, `company_normalization.py`, `company_quality.py`,
  `company_data.md`, `backend/tests/company/`, `backend/tests/universe/`,
  `backend/universe/`, `backend/data_manifests/company_sector_mapping.json`,
  `docs/data-sources.md`, and this worklog.

### Validation

- From `backend/`, `pytest tests/company tests/universe tests/jobs/test_common.py -q`:
  **63 passed**, one pre-existing AnyIO/Pytest deprecation warning.
- `python -m jobs.company_sector_mapping`: passed; emitted 18 rows and the
  local-only manifest. Repeat execution produced identical CSV bytes in tests.
- `python -m universe.validate_universe`: passed (18 companies).
- `git diff --check`: passed.
- Ruff was unavailable (`python -m ruff`: module not installed); formatting/lint
  was not checked by Ruff.

### Still open

- No market-data collectors or published quality reports exist until an
  approved provider, usage/storage rights, and actual coverage are established.
- Written rights for acquisition, team use, retention, and HF storage remain
  unresolved for PSX, EODHD, and any alternative provider. No provider was
  selected; no external calls, downloads, uploads, or costs are authorized.
- Exact source coverage and historical date range for all 18 tickers remain
  unverified. The PSX calendar/listing/halting status, price-move policy,
  corporate-action event completeness/adjustment methodology, expected
  fundamental metrics, and metric age limits also remain unresolved.
- Board lots, `listed_from`, `listed_to`, and `data_available` in the universe
  remain `null`. Sector taxonomy approval is resolved: retain the supplied
  labels as canonical.
- Calendar-based gaps, price-limit outliers, event/adjusted-close consistency,
  expected fundamental metrics, and staleness remain unevaluated until those
  policies and datasets are approved.
- The sector CSV remains local and gitignored pending an allowed publication
  step; the manifest records no HF path or revision.
