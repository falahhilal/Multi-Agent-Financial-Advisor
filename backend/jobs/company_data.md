# Company data contracts and local validation

The provider-neutral contracts and injectable adapter were prepared before
collection. On 2026-11-22, the explicit ingestion command fetched prices for
the 18-company active universe and uploaded company-only outputs to the
existing private HF dataset. The resulting run is partial and Stream A remains
incomplete; see “2026-11-22 price run” below. Tests remain offline and never
invoke pyPSX.

## Canonical sector mapping

`backend/universe/psx_v1.yaml` remains the sole source of the supplied sector
labels. From `backend/`, run `python -m jobs.company_sector_mapping` to write a
deterministic CSV to the gitignored `data/processed/company/` staging area and
refresh `backend/data_manifests/company_sector_mapping.json`. The exporter itself writes local data and a manifest without making a
Hugging Face call. The 2026-11-22 company ingestion workflow published the
reference under `company/reference/sector_mapping.csv` using
`jobs.common.upload_to_hf`; the canonical mapping manifest records its path
and revision.

The CSV carries `universe_version`, `ticker`, `name`, `sector`, and `source`.
It is generated in YAML order; values are not inferred, normalized, or
reclassified. The exporter rejects invalid universe YAML, a non-18-company
`psx_v1`, and any mapping that does not have exactly one row per ticker.

## Normalized record schemas

`jobs.company_schemas` defines strict-extra-field Pydantic contracts:

- `DailyOHLCV`: ticker, exchange trade date, as-traded OHLC, non-negative
  share volume, currency, units, nullable `adjusted_close`, `adjustment_factor`
  and `dividend`, source identifiers/URL, and UTC fetch timestamp. Raw close
  is never copied into adjusted fields. OHLC consistency and positive prices
  are schema constraints.
- `CorporateAction`: ticker, action type, event dates, explicit nullable
  `available_date`, source provenance and fetch time. Cash dividends require a positive per-share amount and currency;
  splits/bonus issues require both sides of a positive share ratio.
- `CompanyFundamental`: ticker, metric slug, decimal value, unit, optional
  currency, optional fiscal period or as-of date, optional report date,
  nullable publication timestamp or availability date, basis (reported or
  calculated), and source provenance. Period end and actual public availability
  remain separate.
- `DatasetManifest`: typed run contract for source, request and returned date
  ranges, ticker, row/file counts, paths, SHA-256 checksums, schema version,
  collection status, and errors. The template in
  `backend/data_manifests/company_prices.template.json` is explicitly
  `not_started`; it records no fetched data.
- `SectorMappingRow`: versioned ticker/name/sector values tied to the canonical
  YAML path.

Decimal source values are parsed through decimal strings to avoid binary-float
arithmetic. Trading dates are ISO dates on the Asia/Karachi market calendar;
fetch timestamps are timezone-aware UTC; tickers are uppercase; currency is
PKR where applicable. Nulls stay explicit. Provider fields not represented by the contract are
rejected until a deliberate schema update is reviewed.

`jobs.company_normalization` provides source-neutral record validators and
`merge_records`. Natural-key repeats do not add rows; conflicting records in
one input batch fail; changed values for an existing key replace that row and
are returned in `revised_keys` for review. No records are interpolated or
silently corrected. Local layout and storage decisions are documented in
`data/README.md`; raw snapshots are immutable and validated tabular outputs
are Parquet. Manifests are committed under `backend/data_manifests/`; data
payloads are gitignored. The per-record provenance fields are `source`,
`source_url`, `source_record_id` where supplied, and `fetched_at`.

## Point-in-time access and provider adapter

`jobs.company_as_of.records_as_of(records, as_of)` is the smallest read
interface prepared for backtests. It requires a date. Prices use exchange
trade date; corporate actions require `available_date`; fundamentals use
explicit `available_date` or their public `published_at` converted to the
Asia/Karachi calendar date. Missing action/fundamental availability is
excluded, never backfilled from ex-date, period end, or report-period dates.

`jobs.company_pypsx.collect_prices` is injectable for offline testing and
imports pyPSX only during explicit fetch calls. It deduplicates tickers, checks
uppercase symbols and date ranges, spaces requests, retries with bounded
backoff, records per-ticker failures, and writes content-addressed snapshots
without overwriting. Snapshots preserve the SDK-returned DataFrame table and
attrs, not the original HTTP wire bytes. `jobs.company_ingest` keeps provider
fetching separate from normalization/quality checks, writes normalized
Parquet, manifests and reports, and uses the shared uploader. No pyPSX method
is assumed to be a complete or point-in-time action/fundamental source.

## 2026-11-22 price run

Command: `python -m jobs.company_ingest` from `backend/`, using
`2015-01-01` through `2026-11-20`, bounded per-ticker retries and request
spacing. All 18 requests returned data; 53,012 SDK rows were snapshotted and
52,950 rows passed normalized schema validation. The other 62 rows (across 16
tickers) failed OHLC consistency checks and were excluded from normalized
Parquet while remaining present in immutable raw snapshots. This run is
`partial`, not a clean pass. Every accepted price date spans
`2015-01-01`–`2026-11-20`, except SYS, whose first accepted date is
`2015-02-06`. Per-ticker source/accepted counts and errors are in
`backend/data_manifests/company_company_prices.json`. There are 18 raw JSON
snapshots and 18 normalized Parquet files. Repeated natural keys are merged
idempotently; the run reported no revised records.

Prices used `adjust_splits=False`, `dividends=True`, and `total_return=False`.
Raw close remains as-traded close; adjusted close is null; source adjustment
factor and nullable dividend are retained with provider basis metadata. No
corporate-action adjustment was applied.

The dataset-level report and 18 ticker reports identify unresolved checks.
Sixteen ticker reports are `issues` because of rejected OHLC rows; two are
`incomplete`; none is `pass`. Session gaps, price-move thresholds, action
consistency, and point-in-time fundamentals are `not_evaluated` because
approved calendars/policies/sources are unavailable. No weekday-based missing
session claims are made.

Uploads to private `fyp-psx-advisor/fyp-data` are rooted under `company/`.
The price upload has final manifest revision `645da426e39959b0562dc98ad9e99fd3c58de0fb`; its run ID is `20261122T083353Z-cd11b23a`. Corporate actions and fundamentals were collected on 2026-11-22 and uploaded without re-fetching prices. Action data revision: `f36551e562b4c1db46553bc4d9b6ca96f67c23fd`; manifest revision: `3f25136328dc3a0d3f595061a8613229cbb1c75d`. Fundamentals data revision: `ae5d72543dbfbcef7f4adbd4bf81bb26eb921929`; manifest revision: `64ea2cddd199496f7d0de61aecf7f2133b91b8f9`. Each auxiliary dataset has 18 raw JSON snapshots, 18 normalized Parquet files, 20 quality/coverage reports, one run record and one manifest. Remote sizes and SHA-256 content checks were verified for all 58 files in each folder.

The action endpoint yielded 386 rows (360 dividends, 2 bonus issues, 3 splits, 5 rights issues, 16 other). No row has a source publication/availability date. Fundamentals yielded 13,379 annual statement-item rows, with observed years 2015–2025/2026 by ticker; the source provides no publication date, units or currency. Thus neither dataset is suitable for historical point-in-time use. No current values were backfilled and Stream A is not complete.

| Ticker | Source rows | Accepted rows | Accepted date range |
|---|---:|---:|---|
| DGKC | 2949 | 2948 | 2015-01-01–2026-11-20 |
| EFERT | 2949 | 2946 | 2015-01-01–2026-11-20 |
| FATIMA | 2946 | 2939 | 2015-01-01–2026-11-20 |
| FFC | 2949 | 2945 | 2015-01-01–2026-11-20 |
| GATM | 2949 | 2947 | 2015-01-01–2026-11-20 |
| HBL | 2949 | 2946 | 2015-01-01–2026-11-20 |
| LUCK | 2944 | 2944 | 2015-01-01–2026-11-20 |
| MARI | 2949 | 2945 | 2015-01-01–2026-11-20 |
| MEBL | 2924 | 2913 | 2015-01-01–2026-11-20 |
| MLCF | 2949 | 2948 | 2015-01-01–2026-11-20 |
| NCL | 2949 | 2949 | 2015-01-01–2026-11-20 |
| NETSOL | 2948 | 2945 | 2015-01-01–2026-11-20 |
| NML | 2949 | 2944 | 2015-01-01–2026-11-20 |
| OGDC | 2949 | 2948 | 2015-01-01–2026-11-20 |
| PPL | 2949 | 2947 | 2015-01-01–2026-11-20 |
| SYS | 2913 | 2905 | 2015-02-06–2026-11-20 |
| TRG | 2949 | 2946 | 2015-01-01–2026-11-20 |
| UBL | 2949 | 2945 | 2015-01-01–2026-11-20 |

## Quality checks and limits

`jobs.company_quality` validates sector coverage, price records, corporate
actions, fundamentals, and per-dataset ticker coverage. Invalid and duplicate
records, missing mappings, future-published point-in-time facts, missing or
stale configured metrics, and suspicious configured price moves are reported;
input rows are not deleted or repaired.
`build_per_ticker_reports` assembles one report per universe ticker and marks
the result `incomplete` whenever a required dataset check or required external
policy is unavailable. Sparse event coverage can be declared optional per
dataset so a ticker with no corporate action is not automatically treated as
missing data.

Some checks deliberately stay unevaluated until project inputs are approved:

- Missing/unexpected price sessions require a licensed or otherwise approved
  PSX trading calendar and applicable listing/suspension status. Weekdays are
  not used as a substitute.
- Suspicious price-move checks require an explicit price-limit policy or a
  reviewed threshold; they only flag observations. Corporate-action context
  is attached when supplied, but no adjustment is applied.
- Corporate-action completeness requires an approved source and its stated
  historical coverage. Cross-checking adjusted values requires an agreed
  methodology and source semantics.
- Missing/stale fundamental checks require expected metrics per ticker, a
  timezone-aware `as_of` timestamp, and approved age limits. These are not
  invented here.
- Actual ticker coverage, source gaps, halts, suspensions, and data-quality
  outcomes cannot be established until approved datasets are available.

Run offline checks from `backend/`:

```powershell
python -m jobs.company_sector_mapping
pytest tests/company tests/universe -q
```

Test fixture values are synthetic test scaffolding only, not PSX data, and are
never uploaded.
