# Session 2026-11-22 — macro_reserves and macro_money_supply loaders, first uploads; FRED dropped

**Who:** Mahrukh Shariq
**Span:** 2026-11-22

## Decisions taken

All by Mahrukh:

- Dataset `macro_reserves`: EasyData "Gold and Foreign Exchange Reserves of
  Pakistan" (`TS_GP_EXT_PAKRES_M`), all 19 series, long format,
  `series_code` from a fixed table; local staging under
  `data/macro/reserves/`, HF `macro/reserves/`.
- Z00060 (Total Liquid FX Reserves) and Z00070 (Total Reserve Assets) end at
  2024-09 and are kept as given, **not derived** afterwards. The identities
  Z00060 = Z00030 + Z00050 and Z00070 = Z00010 + Z00020 + Z00040 are only
  checked, never used to fill values.
- Reserves `published_at` = 25th of the month after `observation_date`,
  23:59:59 Asia/Karachi, stored in UTC; basis
  `"bop_release_25th_next_month"` (evidence in `docs/data-sources.md`).
- Dataset `macro_money_supply`: EasyData "Weekly Broad Money M2"
  (`TS_GP_BAM_M2_W`), all 50 series; HF `macro/money_supply/`.
- Money supply `published_at` = the Friday + 14 days, 23:59:59
  Asia/Karachi, stored in UTC; basis `"weekly_release_friday_plus_14d"`.
- `easydata.check_identities` added; `rows_summary` moved from `macro_fx`
  into `easydata.py` (alias kept in `macro_fx`).
- **FRED will not be used** (see Problems found). US macro series will come
  from their original sources instead; terms still to be checked for each.

## Work done

All by Mahrukh:

- Commits on `data/macro`:
  - `9601f6e`: refactor, `easydata.check_identities` added and
    `rows_summary` moved from `macro_fx` to `easydata.py` (alias in
    `macro_fx`);
  - `b873115`: `macro_reserves` and `macro_money_supply` loaders, their
    tests and `docs/data-sources.md` entries;
  - `f70ac09`: manifests from the uploaded runs
    (`backend/data_manifests/macro_reserves.json`,
    `backend/data_manifests/macro_money_supply.json`).

  All three are pushed to `origin/data/macro` (`fe20d07..f70ac09`).
- Tests: full suite 433 passed (333 before + 100 new: 54 reserves, 46 money
  supply).
- Reserves runs (`TS_GP_EXT_PAKRES_M`, 19 series):
  - `--no-upload` test run and uploaded run (run `20261122T182016Z`) both
    7,112 rows, 0 warnings, exit code 0;
  - 41 files to HF `macro/reserves/` (38 raw, 1 interim, 1 processed, 1 run
    log);
  - identities: Z00060 checked on 171 months, Z00070 on 640 months, 0
    mismatches.
- Money supply uploaded run (`TS_GP_BAM_M2_W`, 50 series, run
  `20261122T182322Z`): 32,250 rows (50 x 645 Fridays), 0 warnings, exit
  code 0; 103 files to HF `macro/money_supply/` (100 raw, 1 interim, 1
  processed, 1 run log); both identities checked on all 645 Fridays, 0
  mismatches above the tolerance.
- HF commits:
  - reserves data `3ba9355b1188dedde9dd05a3e995868b1de52c56`, manifest copy
    `9ed4cd93d3ce080f3199247c52d7943dd6a29b46`;
  - money supply data `cee4924d17642354dd99135b50166853e32edfae`, manifest
    copy `fa454742bc27e53b21142aabc11135464b56692c`.
- Both manifests: `git_commit` `b87311509476526fe2250fa33de1c306bedbe77b`,
  `git_dirty` false (no uncommitted-changes warning).
- FRED loader planned (`macro_fred.py`) but not written, then dropped after
  reading the FRED terms.

Datasets touched:

- `macro_reserves`
  - Source: SBP EasyData API, dataset `TS_GP_EXT_PAKRES_M`, 19 series.
  - Date range: 1948-06-30 to 2026-10-31; portal last refresh 2026-11-18.
  - Size: 7,112 rows, 41 files on HF.
  - Gaps: annual values (30 June) only from 1948 to 1970 plus a partial
    1971; Z00016 ended 2005-06; Z00060 and Z00070 end 2024-09 (not
    derived); series spans differ (fixed table in the loader).
- `macro_money_supply`
  - Source: SBP EasyData API, dataset `TS_GP_BAM_M2_W`, 50 series.
  - Date range: 2014-07-04 to 2026-11-06 (645 Fridays); portal last refresh
    2026-11-16.
  - Size: 32,250 rows, 103 files on HF.
  - Gaps: none (every gap exactly 7 days); no data before Jul-2014; values
    are revised, not first-release vintages.

## Problems found

- The first money supply `--no-upload` run stopped at the metadata name
  check on M000020. The API name has "excluding banks; IMF"; the manual CSV
  showed ", IMF". The CSV export turns ';' into ',' (as found for CPI).
  Fixed in the loader, test and docs before committing; only 2 requests
  used.
- Baseline suite before the changes: 1 failure in the known flaky timing
  test `test_common.py::test_http_get_rate_limits_per_host`; it passed in
  the full run afterwards.
- **FRED cannot be used.** The FRED Services Terms of Use (API
  prohibitions) forbid using the API in connection with storing, caching or
  archiving FRED content, incorporating it in any database, and the
  development or training of software systems or machine learning. Our
  loaders store snapshots on HF, so FRED is out. Planned replacements from
  the original sources (terms still to check):
  - NY Fed Markets Data API: effective federal funds rate;
  - US Treasury daily par yield curve: 10-year yield;
  - EIA API v2: Brent;
  - Fed H.10 release XML: broad dollar index (the Fed is retiring its Data
    Download Program).

  The proposal and `CLAUDE.md` list FRED as a source; this needs a team
  note, and an ADR if the team agrees.
- **Correction to earlier worklogs**
  ([2026-11-16-mahrukh-tbill-auctions.md](2026-11-16-mahrukh-tbill-auctions.md),
  [2026-11-22-mahrukh-inflation.md](2026-11-22-mahrukh-inflation.md),
  [2026-11-22-mahrukh-fx.md](2026-11-22-mahrukh-fx.md)): they said the
  commits joined an "open (policy-rate) PR". No PR from `data/macro` had
  been opened then. The first one was #13, merged on 2026-11-22 as merge
  commit `fe20d07`.

## Still open

- `diff_revisions` warnings can get very long after a big revision (shared
  code in `easydata.py`); fix separately.
- US macro loader replacing FRED: get an EIA key, check each source's
  terms, then plan.
- `fredapi` is in `backend/requirements.txt` but no loader uses it; ask the
  team.
- Raise the `redact_url()` proposal for `common.py` with Falah and Maryam.
- CPI `published_at`: spot-check a few months against PBS release dates.
- T-bill `published_at`: check a 2020 and a pre-2010 auction.
- Policy-rate `published_at`, to be done with the SBP monetary policy
  statement (PDF) work.
- Agree the team Python version.
- Reply to Falah about the team split and steps 3, 5, 7 and 8.
- Review Falah's schema draft.
- The EasyData API key expires 2027-02-07 (time-sensitive: refreshes stop
  until it is renewed).

## Next session should

1. Tell the team that FRED is dropped and why; agree whether it needs an
   ADR, and ask about `fredapi` in `requirements.txt`.
2. Get an EIA key and check the terms of the NY Fed, US Treasury, EIA and
   Fed H.10 sources, then plan the US macro loader.
3. Reply to Falah (team split, steps 3, 5, 7, 8) and review his schema
   draft.
