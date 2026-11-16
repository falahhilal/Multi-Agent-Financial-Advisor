# Session 2026-11-16 — Hourly trigger, ADR-0005, GDELT article text

**Who:** Maryam Asif
**Span:** 2026-11-14 to 2026-11-16

Continues [2026-11-09-maryam-gdelt-finbert.md](2026-11-09-maryam-gdelt-finbert.md).

## Decisions taken

All by Maryam unless stated.

1. **External hourly trigger for the RSS archive.** GitHub's own schedule ran
   every 4 to 9 hours, so a cron-job.org job now calls the workflow's
   `workflow_dispatch` endpoint at minute 50 of every hour, with a
   fine-grained token from Falah (stored in cron-job.org, not in the repo).
   The `23 * * * *` schedule in the workflow stays as a backup.
2. **Drop FinBERT sentiment**: ADR-0005, status **Proposed**. Needs Falah's
   and Mahrukh's agreement before anything else changes. Overrides proposal
   FR-05. The 60-row spot-check was not done; the ADR says so.
3. **The GDELT backfill job fails only on a systemic block** (at least 5
   windows tried and all failed). A night where the 2 remaining windows hit
   429s is no longer a red run.
4. **Article text comes from the publishers' live pages, not the Wayback
   Machine.** The Wayback Machine refuses this cloud machine, and the live
   pages turned out to be almost all still there. The dataset is named
   `news_gdelt_fulltext` (not `news_wayback_backfill` as planned). Wayback
   is now only a fallback for the 28 links that are gone.
5. **"Page not found" pages count as gone.** Tribune answers removed stories
   with a 200 response showing its not-found page, or redirects to its home
   page; these are recorded as `gone`, not `no_text`.

## Work done

All by Maryam. Commits on `maryam/news-stream`:

- `be81c0a` ADR-0005 (Proposed).
- `58f98e5` GDELT systemic-block rule; data-sources entries for
  `news_gdelt_index` and `news_finbert_sentiment`.
- Branch pushed to Falah's repo from Maryam's PC (this session cannot push
  there; Maryam's fork is now detached from it).
- `90156ad` `jobs/news_gdelt_text.py`: fetches every article link in the
  GDELT index, keeps the raw page, extracts title, dates, authors, text and
  tags. Tribune body parsing added to `news_common.parse_article`.
- `418f4d4` soft-404 handling; earlier `no_text` rows are re-read from the
  stored pages, with no new requests.
- `e909fac` interim manifest; final manifest and data-sources entry in this
  commit.
- 71 tests pass; ruff, black and mypy clean.
- Collection run here in three parts (105 + 105 + 5 minutes), one request
  every 3 s per site, uploads every 300 pages. HF revision `327ac80b`.

### Datasets touched

| Dataset | Range | Rows | Gaps / notes |
|---|---|---|---|
| `news_gdelt_fulltext` (new) | first public 2016-01-11 to 2026-11-06 | 8,946 rows; **8,781 with text** (BR 1,266, Dawn 3,823, Tribune 3,692) | 28 gone (BR 5 from March 2017, Dawn 2, Tribune 21); 5 Tribune long-form pages not parsed; 132 non-article links skipped; 7 duplicate-text pairs; no BR text for 78 of 119 months (GDELT's coverage) |
| `news_gdelt_index` | seen 2017-01-01 to 2026-11-06 | 8,946 | 119 of 121 windows complete; read only this session |
| `news_rss_articles` | to 2026-11-16 | 1,779 | 61 runs in 50 h after the external trigger; 2 runs refused robots.txt by BR/Dawn (next run recovered) |

Text recovered by year (recovered / links, BR | Dawn | Tribune):

| Year | BR | Dawn | Tribune |
|---|---|---|---|
| 2017 | 259/264 | 633/633 | 619/621 |
| 2018 | 0/0 | 591/591 | 530/543 |
| 2019 | 0/0 | 434/434 | 505/509 |
| 2020 | 4/4 | 433/434 | 406/406 |
| 2021 | 0/0 | 449/449 | 460/461 |
| 2022 | 0/0 | 457/457 | 301/303 |
| 2023 | 234/234 | 337/337 | 127/128 |
| 2024 | 398/398 | 269/269 | 257/258 |
| 2025 | 370/370 | 186/187 | 280/282 |
| 2026 | 0/0 | 30/30 | 205/205 |

(2016: 7 articles that GDELT first saw in 2017.)

## Problems found

- The first version of the collector counted Tribune's "page not found"
  pages as `no_text` (fixed in `418f4d4`).
- Dawn dropped a few connections during the run (timeouts, resets); each
  succeeded on retry, so no request is left failed.
- 372 Tribune pages carry a publish date later than GDELT's sighting
  (Tribune stamps updates as `datePublished`). `first_public_at` handles it;
  anyone using `published_at` directly would get lookahead.
- 2017 Business Recorder article ids don't match the RSS id scheme (old URL
  form). Harmless today, but worth knowing before any cross-dataset join.

## Still open

- **Falah's repo does not have `90156ad` onward.** Maryam to push the branch
  from her PC again and open a PR.
- ADR-0005: Falah and Mahrukh to accept or reject.
- PSX permission email (blocks Tasks 3 and 5, and Falah's prices): drafted,
  not sent. Tell Falah about PSX's terms.
- Supervisor sign-off on storing full article text.
- Wayback fallback for the 28 gone links: needs a machine that can reach
  web.archive.org.
- Task 7 (NewsAPI/GNews coverage check), optional, not started.

## Next session should

1. Push the branch to Falah's repo and merge the PR.
2. Get the team's decision on ADR-0005 and send the PSX email.
3. Decide whether the 33 missing articles (28 gone, 5 unparsed) are worth a
   Wayback pass; otherwise record them as a known gap and move on.
