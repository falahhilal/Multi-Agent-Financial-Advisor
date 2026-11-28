# Session 2026-11-23 — US macro loaders replacing FRED (EFFR, Treasury yields, H.10 dollar index), first uploads

**Who:** Mahrukh Shariq
**Span:** 2026-11-23 to 2026-11-28 (manifests committed and pushed, tests
re-checked on 2026-11-28)

## Decisions taken

All by Mahrukh:

- **FRED is dropped** for US macro series. They are taken from the original
  publishers instead: NY Fed (EFFR), US Treasury (par yield curve), Federal
  Reserve Board H.10 (dollar indexes). Reasoning in
  `docs/adr/0007-replace-fred-with-original-publishers.md`. ADR-0007 is
  **Proposed** and needs agreement from Falah and Maryam. If accepted, it
  overrides the proposal's FRED source (proposal Step 3, the data stack table
  and the Macro Agent row).
- Dataset `macro_us_effr`: NY Fed Markets Data API, unsecured reference rate
  EFFR, daily; HF `macro/us_effr/`. `published_at` = next EFFR date + 1 day,
  23:59:59 Asia/Karachi, stored in UTC (basis
  `"effr_next_business_day_plus_1d"`); latest row uses the fetch date (basis
  `"effr_latest_row_fetch_date"`).
- Dataset `macro_us_treasury_yields`: US Treasury interest-rate XML feed,
  daily par yield curve, all 14 maturities, long format; HF
  `macro/us_treasury_yields/`. `published_at` = observation date + 1 day,
  23:59:59 Asia/Karachi, stored in UTC (basis `"treasury_same_day_plus_1d"`).
- Dataset `macro_us_dollar_index`: Fed H.10 release XML zip, the three daily
  nominal dollar indexes (broad, AFE, EME); HF `macro/us_dollar_index/`.
  `published_at` = Monday after the observation's week + 2 days, 23:59:59
  Asia/Karachi, stored in UTC (basis `"h10_following_monday_plus_2d"`).
- All three are `append_only_snapshot`: each run's raw responses are kept and
  revisions against the previous run are warned.
- **Brent will not come from EIA.** EIA republishes Refinitiv data with
  permission, so EIA's terms do not settle our right to store it. Brent will
  come from the World Bank Pink Sheet in the World Bank loader (ADR-0007,
  `docs/plan.md`). The EIA key in `.env` is unused.

## Work done

All by Mahrukh:

- Commits on `data/macro`:
  - `341431d`: US macro loaders (`backend/jobs/macro_us_effr.py`,
    `macro_us_treasury_yields.py`, `macro_us_dollar_index.py`), their tests,
    ADR-0007 and docs;
  - `a20eacf`: manifests from the uploaded runs
    (`backend/data_manifests/macro_us_effr.json`,
    `macro_us_treasury_yields.json`, `macro_us_dollar_index.json`).

  Both pushed to `origin/data/macro` on 2026-11-28 (`58f7526..a20eacf`).
- No API keys needed for any of the three sources.
- Treasury site is slow (about 18 s per year). The loader uses a 120 s
  total limit per request (whole response, not per socket read), 3 attempts,
  5 s spacing, asks for gzip, and prints progress lines.
- All three manifests: `git_commit` `341431d8f36a9f6884322b6593359ae605288de5`,
  `git_dirty` false, 0 warnings. The manifests from these runs say ADR-0006;
  it was renumbered to ADR-0007 on 2026-11-28.
- Reviewed and added to the loaders' fixed tables:
  - EFFR: first date 2000-07-03 confirmed; 5 large moves with no target
    change reviewed as real (2000-12-29, 2001-01-02, 2001-09-20, 2008-01-02,
    2008-10-07); the Sunday record 2003-07-20 is identical to Monday
    2003-07-21 and looks like a misdated copy, kept as returned
    (`KNOWN_WEEKEND_DATES`).
  - Treasury: 2010-10-11 missing for all maturities; 3-month missing on
    2008-12-10, 2008-12-18 and 2008-12-24; 25 reviewed large moves across
    six maturities; no move is measured across the 30-year publication gap
    (2002-02-18 to 2006-02-08).
- Tests: `pytest tests/jobs` from `backend/`, 506 passed (checked
  2026-11-28).

Datasets touched:

- `macro_us_treasury_yields`
  - Source: US Treasury interest-rate XML feed
    (`daily_treasury_yield_curve`), years 1990-2026 (37 requests).
  - Run `20261123T133345Z`: 100,160 rows, 14 maturities, 9,229 dates,
    0 warnings; 40 files to HF (37 raw, 1 interim, 1 processed, 1 run log),
    HF commit `c60c868cae84047bfa70774afafacbf607c0b3c3`.
  - An earlier run `20261123T124214Z` had the same 100,160 rows and 18
    warnings: the known gaps and large moves above, before they were added
    to the loader's reviewed tables, plus one 30-year move measured across
    the publication gap. Only the clean run is in the manifest.
  - Date range: 1990-01-02 to 2026-11-20.
  - Gaps: 2010-10-11 (all maturities); 3-month on 2008-12-10/18/24;
    30-year not published 2002-02-18 to 2006-02-08; maturities start at
    different dates (1-month 2001, 2-month 2018, 4-month 2022, 1.5-month
    2025, 20-year 1993). Methodology changed to monotone convex on
    2021-12-06. Treasury may correct values without notice.
- `macro_us_effr`
  - Source: NY Fed Markets Data API, EFFR.
  - Run `20261123T132955Z`: 6,630 rows, 0 warnings; 5 files to HF (2 raw,
    1 interim, 1 processed, 1 run log), HF commit
    `129f3b5f39122b8825bd933d7b1b8a5dda5aac6c`.
  - Date range: 2000-07-03 to 2026-11-19.
  - Gaps: no data before 2000-07-03 from the API; record fields change in
    2008-12-16 (target range) and 2016-03-01 (percentiles and volume).
- `macro_us_dollar_index`
  - Source: Fed H.10 release XML (`FRB_h10_xml.zip`), series
    `JRXWTFB_N.B`, `JRXWTFN_N.B`, `JRXWTFO_N.B`; release prepared
    2026-11-16T16:47:01.
  - Run `20261123T133158Z`: 15,693 rows (5,231 per series), 0 warnings;
    4 files to HF (1 raw zip, 1 interim, 1 processed, 1 run log), HF commit
    `0cc6cf979f280377ca7d3ffb9d076e07de5f1edc`.
  - Date range: 2006-01-02 to 2026-11-13.
  - Gaps: ND (no data) rows kept in interim and listed in the run log, not
    in processed.

## Problems found

- **FRED cannot be used** (found 2026-11-22, see
  [2026-11-22-mahrukh-reserves-money-supply.md](2026-11-22-mahrukh-reserves-money-supply.md)):
  the FRED terms forbid storing, caching or archiving FRED content and using
  it to develop or train software or ML systems. Hence ADR-0007 (Proposed).
- The first Treasury run hung for over 10 minutes inside one year's read,
  because `common.http_get`'s timeout bounds each socket read, not the whole
  response. Fixed in the loader with a total limit per request and longer
  spacing (5 s instead of 2 s). A `total_timeout` option in
  `common.http_get` would fix it for everyone (see Still open).
- H.10 unit code says `Index:_1997_Jan_100`, but the H.10 Technical Q&As
  give a January 2006 base. Kept as returned and noted in the manifest.
  The Board changed the indexation of the daily indexes from 2019-06-24.
- H.10 zip is overwritten at every release with no archive, and past values
  are revised when trade weights change. Our snapshots are the only
  vintages; history before our first run is the latest vintage only.
- Brent from EIA is Refinitiv data (see Decisions taken).

## Still open

- `total_timeout` option in `common.http_get` (shared code); raise with the
  team.
- Raise the `redact_url()` proposal for `common.py` with Falah and Maryam.
- `fredapi` is still in `backend/requirements.txt` but no loader uses it;
  ask the team whether to remove it.
- Agree the team Python version (local `.venv` is 3.14; 3.11/3.12 under
  discussion).
- ADR-0007 is Proposed; needs agreement from Falah and Maryam.

## Next session should

1. Write the World Bank loader (`wbgapi`), including Brent from the World
   Bank Pink Sheet.
2. Then start on the SBP PDFs.
