# Session 2026-11-14 — macro_policy_rate first uploaded run

**Who:** Mahrukh Shariq
**Span:** 2026-11-14

## Decisions taken

None new. The decisions for this dataset are in
[2026-11-12-mahrukh-policy-rate.md](2026-11-12-mahrukh-policy-rate.md).

## Work done

All by Mahrukh:

- Created a fine-grained Hugging Face write token limited to
  `datasets/fyp-psx-advisor/fyp-data`. The token value is not recorded
  anywhere.
- Committed the loader, tests, docs and the 2026-11-12 worklog as commit
  `180d96e` on `data/macro`.
- First real uploaded run of `python -m jobs.macro_policy_rate` on
  2026-11-14:
  - 194 rows: 97 Reverse Repo from 1956-01-01, 57 Repo from 2009-08-17,
    40 Policy Target from 2015-05-25;
  - last observation 2026-10-26, portal last refresh 2026-10-28;
  - no warnings;
  - same row count as the 2026-11-12 local run.
- HF commits:
  - data: `78a04d6f222e30af60c0b242acb6b5265964485e`;
  - manifest copy: `055c0dff80e8c89bac194a43166b5d83a9a028aa`;
  - folder `macro/policy_rate/`, 9 files: 6 raw, 1 interim, 1 processed,
    1 run log.
- The run wrote the manifest `backend/data_manifests/macro_policy_rate.json`
  with `git_commit` `180d96e1644c3d5357269b6ed8b13f855c6ef0ac` and
  `git_dirty` false. The manifest was then committed.

Datasets touched: `macro_policy_rate`.
- Source: SBP EasyData API, three series (Policy Target, Repo, Reverse Repo).
- Date range: 1956-01-01 to 2026-10-26.
- Size: 194 rows, 9 files on HF.
- Gaps: as listed in the 2026-11-12 worklog (Reverse Repo 1991 rows missing,
  two extra 2020 dates, `published_at` null).

## Problems found

- The 2026-11-12 worklog says nothing was uploaded to HF. That was true then;
  this worklog supersedes it. The old worklog is left unedited.

## Still open

- Check the 2020-04-16 and 2020-05-16 extra rows, and announcement vs
  effective date, against the MPC statements.
- MPC announcement/effective-date calendar dataset (for `published_at`).
- Raise the `redact_url()` proposal for `common.py` with Falah and Maryam.
- The EasyData API key expires 90 days after it was issued; note the expiry
  date. February 7, 2027.
- Open the pull request from data/macro.

## Next session should

1. Plan the T-bill job, dataset `macro_tbill_auctions`: EasyData dataset
   `TS_GP_BAM_SIRTBIL_AH`, six series `TB0010` to `TB0060` (Cut-off Yield and
   Weighted Average Yield for 3, 6 and 12 months). Planned, not started.
2. Check the 2020 rows against the MPC statements.
