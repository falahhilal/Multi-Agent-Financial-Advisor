# Session 2026-11-16 — macro_tbill_auctions loader and first upload

**Who:** Mahrukh Shariq
**Span:** 2026-11-16

## Decisions taken

All by Mahrukh:

- Shared EasyData helpers moved to `backend/jobs/easydata.py`, a macro-stream
  module. `backend/jobs/common.py` is not changed.
- Dataset `macro_tbill_auctions`: long format, one row per series per auction
  date; local staging under `data/macro/tbill_auctions/`.
- `published_at` = auction date 23:59:59 Asia/Karachi, stored in UTC, with
  basis `"auction_date_end_of_day_pkt"`. Checked on 2026 auctions only.
- A value with status "Missing value" warns and is kept; it is not a failure.
- Stale-data warning after 45 days.
- Run `--no-upload` against the real API before committing; the uploaded run
  only after committing, from a clean tree.

## Work done

All by Mahrukh:

- Commits on `data/macro`:
  - `7486346`: refactor, shared helpers in `easydata.py`;
  - `db52b02`: loader and 38 offline tests;
  - `6798747`: `docs/data-sources.md` entry;
  - `20c4b9c`: manifest.

  The first three were pushed and join the open policy-rate PR.
- Tests: `pytest tests/jobs` 124 passed. The 39 policy-rate tests pass
  unchanged after the refactor. Mutation check: with the key filter off, the
  leak tests in both jobs fail.
- Evidence for `published_at` (checked 2026-11-16):
  - SBP bid report for the 16-Sep-2026 auction (settlement 17-Sep-2026);
  - EasyData dates 30-Sep-2026 and 14-Oct-2026 are SBP auction dates;
  - EasyData values match SBP's published cut-offs (3-month 11.9944 → 11.99,
    11.9999 → 12);
  - news reports of the results appeared the same afternoon (about
    17:30 PKT).
- Manual EasyData CSV (downloaded 2026-11-14) checked: 3,516 rows, 586 dates
  per series, no duplicates; keys `TS_GP_BAM_SIRTBIL_AH.TB0010` to `TB0060`.
- `--no-upload` run on 2026-11-16 at 18:53: 3,516 rows, warnings as expected.
- Uploaded run on 2026-11-16 at 19:12 (run `20261116T141211Z`):
  - 3,516 rows;
  - 15 files to HF `macro/tbill_auctions/`: 12 raw, 1 interim, 1 processed,
    1 run log.
- HF commits:
  - data: `60142073e235a77cd093cff5d9f40d3f167a64e7`;
  - manifest copy: `907473088e94127606201164973f2c085f3c6e8d`.
- Manifest `git_commit` `6798747b6e7ebf2491210bd415d8b5fa5838bffd`,
  `git_dirty` false.

Datasets touched: `macro_tbill_auctions`.
- Source: SBP EasyData API, dataset `TS_GP_BAM_SIRTBIL_AH`, six series
  (Cut-off and Weighted Average Yield, 3, 6 and 12 months).
- Date range: 2004-06-24 to 2026-10-14; portal last refresh 2026-10-16.
- Size: 3,516 rows (586 per series), 15 files on HF.
- Gaps and warnings (from run log `run-20261116T141211Z.json` and
  `docs/data-sources.md`):
  - 507 rows with status "Missing value": 503 empty (kept as null; TB0010 42,
    TB0020 82, TB0030 130, TB0040 41, TB0050 79, TB0060 129) and 4 with a
    value (TB0030 and TB0060 on 2021-12-01 and 2021-12-15, kept, warned);
  - TB0050 2007-12-05 is .1215, outside [1, 35] (warned, not corrected);
  - weighted average above cut-off by more than 0.01 on 5 dates: 3-month
    2008-01-02 and 2013-05-29, 6-month 2009-10-07, 12-month 2011-09-21 and
    2026-02-04;
  - cut-off and weighted average not both present: 3-month 2024-10-02;
    6-month 2007-01-17, 2007-09-13 and 2024-10-02; 12-month 2007-01-17;
  - the 2026-09-16 auction is missing from EasyData;
  - no 1-month tenor in this dataset;
  - precision varies (older rows more decimals, 2026 rows 2 or fewer);
  - gaps over 21 days between auctions: 2009-06-17 to 2009-07-15 (28 days),
    2010-06-16 to 2010-07-14 (28), 2024-01-10 to 2024-02-06 (27),
    2026-03-16 to 2026-04-15 (30), 2026-09-02 to 2026-09-30 (28).

## Problems found

- The 2026-09-16 auction is missing from EasyData. The earlier note "no
  auction between 2026-09-02 and 2026-09-30" was wrong.
- SBP bid reports list all bids received, including rejected ones; the
  cut-off is not the last row.
- Each warning is logged twice (once by the loader, once when the manifest is
  written). Cosmetic.
- black, ruff and mypy are not installed, so the new code is not linted.
- The first version of the `docs/data-sources.md` entry overstated the empty
  missing-value rows (507; really 503 empty + 4 with a value) and the
  precision ("up to 4 decimals"). Corrected in a follow-up docs commit,
  together with a note that the ". " prefix is only in the manual CSV.

## Still open

- The `published_at` rule was checked on 2026 auctions only; check a 2020
  auction and a pre-2010 auction.
- Raise the `redact_url()` proposal for `common.py` with Falah and Maryam.
- MPC announcement/effective-date calendar dataset.
- Check the 2020-04-16 and 2020-05-16 policy-rate rows against the MPC
  statements.
- The EasyData API key expires 2027-02-07.
- The open PR from `data/macro`, to be merged later.

## Next session should

1. Start the next Stream B dataset (to be chosen).
2. Check older T-bill auctions for the `published_at` rule.
