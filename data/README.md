# Local data layout

This directory is ignored by Git except for this guide. Keep real collected
files local/private or in an explicitly approved storage destination; never
commit them. No dataset or placeholder payload is included here.

```text
data/
  raw/
    company_prices/       immutable provider-response snapshots
    corporate_actions/    immutable source responses
    fundamentals/         immutable source responses
  interim/                parsed/normalized staging files
  processed/
    company_prices/       validated daily OHLCV Parquet
    corporate_actions/    validated events Parquet
    fundamentals/         validated point-in-time metrics Parquet
    company/              generated reference exports such as sector mapping
  manifests/              local run copies; committed contracts/templates live in backend/data_manifests/
  quality_reports/        per-dataset validation and coverage reports
  _logs/                  collection status, errors, and gaps
```

Raw snapshots are content-addressed and never overwritten. Corrections and
normalization outputs go to `interim/` or `processed/`. Preserve decimal
precision, explicit nulls, source identifiers, source endpoint, UTC
`fetched_at`, and ISO dates. PSX exchange dates use the Asia/Karachi market
calendar. Normalized tabular data should use Parquet; retain the original SDK
response representation separately. `backend/data_manifests/` holds reviewed,
versioned manifests and templates, not raw data.
