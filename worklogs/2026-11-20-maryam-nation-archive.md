# Session 2026-11-20 — The Nation: hourly feed and 2017–2026 archive backfill

**Who:** Maryam Asif
**Span:** 2026-11-20 to 2026-11-21

Continues [2026-11-17-maryam-news-api-coverage.md](2026-11-17-maryam-news-api-coverage.md),
whose GNews check turned up The Nation as a source we were not collecting.

## Decisions taken

1. **Add The Nation** (Maryam): both the business RSS feed in the hourly
   archive and a historical backfill from its daily sitemaps. Its terms
   allow "personal, academic or non-commercial" use and robots.txt allows
   our User-Agent (`docs/data-sources.md`, RSS section).
2. **Fetch each Nation article page for full text** (Maryam): the feed has
   summaries only. At most 40 pages per hourly run, one every 3 s; pages
   that fail or go over the cap are retried on the next run.
3. **Pick backfill pages by URL slug, rule `slug-v1`** (Maryam): fetch only
   links whose slug has a market word, keep every link in `links.parquet`
   so a wider rule can be added later without reading the sitemaps again.
   Checked on 2017 to 2022 links before the text run: the daily market
   report was picked on the sampled days, under other wording too
   ("market-continues-downward-trend"). About two thirds of picks are off
   topic; `is_market` marks the rest.
4. **Read every calendar day, not only those the sitemap index lists**
   (made during the run, for Maryam to confirm): the index leaves out, in
   every month, the day matching the date it is read (the 20th on 11-20),
   but those daily sitemaps exist. Leaving them out would put
   a regular hole in the backtest windows.
5. **Pages that redirect to a different article are `redirected`, not
   `ok`** (made during the run, for Maryam to confirm): their text belongs
   to another story.

## Work done

All by Maryam. Commits on `maryam/news-stream`:

- `c6399df` The Nation business feed in `news_feeds.yaml`, with
  `fetch_pages` in `jobs/news_rss.py`; Nation body parsing in
  `news_common.parse_article` (drops embedded related-story links, ad slots
  and the tags line).
- `e1d4a9f` `jobs/news_nation_archive.py` (`index` and `text` steps,
  resumable, time-budgeted, checkpoint upload every 300 requests) and its
  tests.
- Both merged into Falah's repo as falahhilal/Multi-Agent-Financial-Advisor#11.
- `896f0bd` the index step also reads days the sitemap index omits
  (`in_index` in `days.parquet`).
- `50b8e05` `redirected` status; stored rows re-marked without new requests.
- `d7f43f6`, `f844572`, `c404131`, `1d21d66`, `100bb84`, `2837b89`:
  interim manifests. Final manifest, `docs/data-sources.md` entry and this
  worklog in the last commit.
- 85 tests pass (`pytest tests/jobs`); ruff, black and mypy clean.
- Collection: index in two runs (the second for the 119 omitted days), text
  in six runs of up to 105 minutes, one request every 3 s. Final HF
  revision `c3318edc`.

### Datasets touched

| Dataset | Range | Rows | Gaps / notes |
|---|---|---|---|
| `news_nation_archive` (new) | URL days 2017-01-01 to 2026-11-19 | 3,610 days; 543,422 links; 8,252 picked, **8,250 with text** (2,693 `is_market`) | 0 failed requests left; 2 `redirected`; 10 duplicate-text groups (21 rows); 36 caption-only pages under 300 characters; 11 pages dated after their URL day |
| `news_rss_articles` (feed `nation_business` added) | from 2026-11-20 | 100 Nation items in the first run | Backlog of 100 cleared in three hourly runs (40 + 40 + 20 pages), then new items as published (13 at 00:50 UTC on 11-21). 0 page failures, 0 pages without text in 9 runs |

Backfill by year (picked / ok / `is_market`):

| Year | Picked | ok | is_market |
|---|---|---|---|
| 2017 | 943 | 943 | 350 |
| 2018 | 900 | 900 | 324 |
| 2019 | 609 | 609 | 234 |
| 2020 | 791 | 791 | 192 |
| 2021 | 799 | 799 | 210 |
| 2022 | 893 | 893 | 287 |
| 2023 | 806 | 806 | 254 |
| 2024 | 813 | 813 | 277 |
| 2025 | 854 | 852 | 305 |
| 2026 | 844 | 844 | 260 |

Every month has `is_market` articles: median 22, fewest 9 (2020-11).

## Problems found

- **The sitemap index leaves out, in every month, the day whose number is
  the date it is read** (the 20th in the copy read on 11-20, the 21st in
  the copy read on 11-21; 118 days each from 2017), plus 2022-02-17. Found
  while checking coverage after the first text pass; the daily sitemaps
  exist (`sitemap_2025-01-20.xml`: 150 links). Fixed in `896f0bd` by
  reading every calendar day; 18,015 links and 282 picked pages added.
  The commit message of `896f0bd` says "the 20th of every month"; that was
  only true of the 11-20 copy. 2022-02-17's sitemap is empty: the only day
  with no links.
- **Two links redirect to unrelated stories** (2025-01-28; the SBP 100 bp
  policy-rate cut lands on a Lahore weather report). They were counted as
  `ok` until `50b8e05`. A claim matcher using them would have matched the
  wrong text.
- A machine restart killed one text chunk; it resumed from the last
  checkpoint, losing about 200 fetched pages, which were fetched again.
- The Nation returned HTTP 429 twice for the same page across runs; it
  succeeded on a later run. No rate change was needed.
- The slug filter's recall is not measured over the whole archive, only on
  sampled days.
- Most bylines are wire or desk names (APP, NEWS WIRE, Agencies), so the
  same wire story may appear in Dawn or Tribune rows of
  `news_gdelt_fulltext`. Cross-dataset deduplication is not done.

## Still open

- Maryam to confirm decisions 4 and 5.
- Push the branch to Falah's repo and open a PR for `d7f43f6` onward.
- From earlier sessions: Task 7 recommendations (no GNews trial, no
  NewsAPI) to confirm; ADR-0005 team decision; PSX permission email;
  supervisor sign-off on full text; Wayback pass for 28 gone GDELT links.

## Next session should

1. Open and merge the PR.
2. Decide whether to measure the slug filter's recall (for example, label
   all links on 20 random weekdays) before the retrieval corpus is built.
3. Chase the ADR-0005 decision and the PSX email.
