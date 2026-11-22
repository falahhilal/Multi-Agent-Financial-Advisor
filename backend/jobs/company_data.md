# Company data contracts and local validation

This is pre-ingestion setup. The team reports written permission for intended
PSX/pyPSX historical-price collection and private HF sharing (see
`docs/data-sources.md`). No data has been collected in this task. These
provider-neutral contracts and an injectable pyPSX loader are prepared, but
the loader is never run by tests or at import time.

## Canonical sector mapping

`backend/universe/psx_v1.yaml` remains the sole source of the supplied sector
labels. From `backend/`, run `python -m jobs.company_sector_mapping` to write a
deterministic CSV to the gitignored `data/processed/company/` staging area and
refresh `backend/data_manifests/company_sector_mapping.json`. The manifest
records that the export is local and not uploaded. There is intentionally no
Hugging Face call in this exporter. When an approved workflow publishes this
reference artifact, use `jobs.common.upload_to_hf` under
`company/reference/sector_mapping.csv` and update the manifest with its HF
path and revision.

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
only imports pyPSX inside the future fetch function. It deduplicates tickers,
checks uppercase symbols and date ranges, spaces requests, retries with
bounded exponential backoff, records per-ticker failures, and writes
content-addressed snapshots without overwriting. SDK-returned frame snapshots
are a preserved provider representation, not the original HTTP wire bytes.
The module does not normalize or upload data. A later explicit ingestion
command must normalize into Parquet, produce the committed manifest, and
record gaps/quality reports. Corporate actions and fundamentals have no
assumed pyPSX methods.

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
