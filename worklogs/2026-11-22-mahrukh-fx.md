# Session 2026-11-22 — macro_fx loader and first upload

**Who:** Mahrukh Shariq
**Span:** 2026-11-22

## Decisions taken

All by Mahrukh:

- Dataset `macro_fx`: EasyData "Bank Floating Daily Average Exchange Rates
  (PKR per National Currency)" (`TS_GP_ES_FADERPKR_M`), all 23 currencies,
  long format, `currency_code` from a fixed table; local staging under
  `data/macro/fx/`, HF `macro/fx/`.
- Daily rather than the monthly average dataset (also on EasyData, from
  1947), so backtest windows such as the 2022 currency crisis keep daily
  moves.
- `published_at` = 10th of the month after `observation_date`, 23:59:59
  Asia/Karachi, stored in UTC; basis `"monthly_release_10th_next_month"`.
  Chosen over same-day dating because this exact series is released monthly
  (CLAUDE.md Rule 6); backtests see FX values up to about 40 days late.
  SBP's daily Weighted Average Customer Exchange Rates (a different,
  same-day series) are not collected.
- `status_changes` and `listed()` moved to `easydata.py` (shared with
  `macro_inflation`).
- An empty value fails the run; weekend dates other than four known
  Saturdays warn; gaps over 14 days warn; a change over 10% against the
  previous observation warns. JPY is per 1 yen.

## Work done

All by Mahrukh:

- Commits on `data/macro`:
  - `411fc8d`: refactor, `status_changes` to `easydata.py`;
  - `8e9e89c`: loader and 56 tests;
  - `a409511`: `docs/data-sources.md` entry;
  - `8a21fb9`: manifest.

  `411fc8d`, `8e9e89c` and `a409511` are pushed and join the open PR.
- Tests: `pytest tests/jobs` 241 passed. The 185 earlier tests pass
  unchanged; 56 are new.
- Evidence for `published_at`:
  - the EasyData dataset page says "This daily data is compiled and
    disseminated on monthly basis";
  - Oct-2026 data refreshed 05-Nov-2026;
  - SBP publishes the same series as a monthly table ("Daily Average Banks'
    Floating Exchange Rates", BFER_Daily.pdf).
- Manual EasyData Long Format CSV (downloaded 2026-11-22) checked: 75,325
  rows (23 x 3,275 dates), no empty values, all status "Normal".
- Runs:
  - 2026-11-22 14:47-14:48 `--no-upload`: all 23 `/meta` names matched (the
    API uses "Hong Kong"; "Hon Kong" is only a typo on the web page); 3,275
    rows per series; 3 move warnings as expected.
  - 2026-11-22 15:00-15:02 uploaded run: 75,325 rows; 49 files to HF
    `macro/fx/` (46 raw, 1 interim, 1 processed, 1 run log).
- HF commits:
  - data: `b54a7163ef41c8c5a780d95bda8b7a71d304941b`;
  - manifest copy: `3e55ca3f4be98a5f0ed9bdd01a9f275577c3aeda`.
- Manifest `git_commit` `a409511c84da1a95cd653161b3114b0d9ece6fed`,
  `git_dirty` false.

Datasets touched: `macro_fx`.
- Source: SBP EasyData API, dataset `TS_GP_ES_FADERPKR_M`, 23 series.
- Date range: 2013-07-02 to 2026-10-30; portal last refresh 2026-11-05.
- Size: 75,325 rows (23 x 3,275 dates), 49 files on HF.
- Gaps and warnings (from the run log and `docs/data-sources.md`):
  - four Saturdays (2019-08-17, 2022-04-16, 2022-04-23, 2022-04-30),
    allowed;
  - holiday gaps up to 10 days (e.g. 2021-05-07 to 2021-05-17), not warned;
  - five moves above 10% against the previous observation, warned (expected):
    TRY 2018-08-13, 2021-11-24, 2021-12-21; NOK 2020-03-19; CHF 2015-01-16;
  - 29 rows with status "Normal" and an unrelated status comment (XRDAVG0030
    2022-07-13 to 2022-08-01, XRDAVG0130 2016-12-05 to 2016-12-26), kept as
    returned;
  - values carry 6-9 decimals, stored as returned;
  - no data before Jul-2013;
  - monthly release, so backtests see values up to about 40 days late.

## Problems found

- The first version of the facts gave the three TRY move dates one day
  early (the day before each move); corrected in the docs before commit.
- `test_common.py::test_http_get_rate_limits_per_host` failed once during a
  full test run (a timing check in shared code not touched here) and passed
  on every rerun; flaky.
- Each warning is logged twice (loader, then manifest writing). Cosmetic.
- black, ruff and mypy are not installed, so the new code is not linted.

## Still open

- Policy-rate `published_at`, to be done with the SBP monetary policy
  statement (PDF) work.
- The FX `published_at` rule rests on one observed refresh date (05-Nov);
  not checked against historical release dates.
- Same-day FX (SBP Weighted Average Customer Exchange Rates), only if the
  team wants it; the data is in daily PDFs.
- CPI `published_at`: spot-check a few months against PBS release dates.
- T-bill `published_at`: check a 2020 and a pre-2010 auction.
- Falah reported fixes on `main`: fetch and bring `main` into `data/macro`.
- Raise the `redact_url()` proposal for `common.py` with Falah and Maryam.
- The EasyData API key expires 2027-02-07.
- The open PR from `data/macro`, to be merged later.

## Next session should

1. Fetch `main`, review Falah's changes, and bring them into `data/macro`.
2. Start the reserves and money supply dataset (EasyData).
