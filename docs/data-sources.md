# Data sources

One entry per dataset: purpose, format, consuming agent, where it lives, how
it is fetched, and the source's terms (checked **before** collecting; see
CLAUDE.md, Data collection rule 4). A dataset is not "collected" until it has
an entry here. Data lives in the private HF dataset `fyp-psx-advisor/fyp-data`
(`news/`, `company/`, `macro/`, one folder per stream); manifests are in
`backend/data_manifests/<stream>_<dataset>.json`.

Entries are grouped by stream. Each stream owner edits only their section.

---

## News & text stream (owner: Maryam)

### news_rss_articles

| | |
|---|---|
| Purpose | Point-in-time news text for the Sector Analyst (FinBERT sentiment, retrieval) and the Macro Agent (policy/macro coverage, mostly Dawn). Builds the archive the backtests need, since free news APIs keep only ~1 month. |
| Loader | `backend/jobs/news_rss.py`, feeds in `backend/jobs/config/news_feeds.yaml`; hourly via `.github/workflows/news_rss_hourly.yml` |
| Fetch mode | `append_only_snapshot`: RSS cannot be re-fetched historically; the HF copy is the archive of record |
| Location | HF `news/rss/`: `raw/fetch_date=D/<run>_<feed>.xml.gz` (unmodified feed responses), `articles/fetch_date=D/part-<run>.parquet` (new articles only), `index/article_ids.parquet` (dedupe index), `runs/fetch_date=D/run-<run>.json` (per-run log), `manifest.json` |
| Format | Parquet, one row per article: `article_id` (sha256 of canonical URL, 16 hex), `url`, `canonical_url`, `source`, `feed_name`, `feed_url`, `feed_role`, `guid`, `title`, `published_at` (UTC), `published_at_raw`, `published_tz` (original offset), `fetched_at` (UTC), `summary`, `full_text` (plain text from `content:encoded`), `full_text_source`, `language`, `categories`, `author`, `tickers_mentioned` (empty until tagging), `run_id` |
| Point-in-time key | `published_at`; `fetched_at` is the latest time the item could have been known to us |
| Coverage start | 2026-11-08. Earlier items exist only as far back as each feed still showed on that day |

Feeds (checked 2026-11-08):

| Feed | Role | Items | Window | Full text |
|---|---|---|---|---|
| brecorder.com/feeds/latest-news | primary | 30 | ~16 h (Sunday) | yes |
| tribune.com.pk/feed/business | primary | 25 | ~67 days | yes |
| dawn.com/feeds/business/ | primary | 30 | ~3 days | yes |
| brecorder.com/feeds/markets/stocks | filter (Pakistan/PSX terms) | 30, 3 kept | ~2 days | yes |
| thefinancialdaily.com/feed/ | test | 30 | ~6 days | no (summary) |
| pakistangulfeconomist.com/feed/ | test | 12 | weekly | no (summary) |
| propakistani.pk/feed/ | test | 10 | ~11 h | yes |

Not used: brecorder `feeds/pakistan/markets` and `feeds/business-finance/pakistan`
(reported stale), Profit / Pakistan Today (reported dead).

**Terms and access** (read 2026-11-08; robots.txt allows our User-Agent on all
seven feed URLs; no site's terms mention RSS, bots or automated access):

- **Business Recorder** (`/term-of-use/`): "You may not modify, copy,
  reproduce, republish, upload, post, transmit, or distribute, in any manner,
  the material…"; "You may print and download portions of material … solely
  for your own non-commercial use"; "Any commercial use … is strictly
  prohibited without express permission".
- **Dawn** (`/terms/`): "You shall not copy, reproduce, republish, …
  download, … or otherwise use the content of this website in any way except
  for your own personal, non-commercial use." Dawn's AI policy covers only
  Dawn's own use of AI.
- **The Express Tribune** (`/copyrights`): "Re-use of The Express Tribune
  content is NOT permitted without prior written consent"; "This material may
  not be published, broadcast, rewritten, redistributed or derived from."
  The RSS page offers feeds with "headlines, summaries and content".
- **The Financial Daily, Pakistan & Gulf Economist, ProPakistani**: no terms
  page found; only privacy policies.

Handling: the dataset is private, used only for non-commercial academic
research, and never redistributed; no article text is published in the thesis
or app beyond short quotations with citation. Per-feed `store_full_text: false`
switches a feed to headline, link and dates only (no summary, no raw snapshot)
if the team decides a source's terms require it.

**Decision (Maryam, 2026-11-08): keep full text for all seven feeds**, on the
basis above (private, non-commercial academic research, never redistributed).
Supervisor confirmation still to be recorded. Removing text from HF later
means rewriting the dataset history, so revisit this early if it changes.

Known gaps and limits: items published and dropped from a feed between runs
are lost (BR and ProPakistani windows are ~11-16 h, so missing more than a few
hourly runs loses items); first sighting wins, so later edits are only in raw
snapshots; the BR stocks filter is a keyword match and can miss or wrongly
keep items; GitHub's cron can be delayed or skip runs at busy times.

### news_aaj_psx_archive

| | |
|---|---|
| Purpose | Historical PSX market news (2021 onward) for backtest windows the RSS archive cannot reach. Sector Analyst (FinBERT, retrieval). |
| Loader | `backend/jobs/news_aaj_psx.py` (run once; re-runs add only new articles) |
| Fetch mode | `reproducible`: the tag archive can be re-walked, though the site may edit or remove pages |
| Location | HF `news/backfill/aaj_psx/`: `articles.parquet`, `coverage_by_month.csv`, `raw/articles/<site_id>.html.gz`, `raw/listing/fetch_date=D/…`, `runs/` |
| Format | Parquet, one row per article: `article_id` (same scheme as news_rss), `site_article_id`, `url`, `canonical_url`, `source`, `title`, `published_at` (UTC, from the article page's JSON-LD), `published_at_raw`, `published_tz`, `modified_at`, `authors`, `summary`, `full_text`, `language`, `tags`, `tickers_mentioned` (empty), `listing_page`, `fetched_at`, `raw_file` |

First run 2026-11-08: 14 listing pages, **418 articles**, 0 errors, published
2021-01-14 to 2026-11-05. Per year: 2021: 9, 2022: 23, 2023: 82, 2024: 122,
2025: 125, 2026: 57. Median 3 articles a month; **11 of 71 months have none**
(2021-02, 05, 06, 07, 12; 2022-01, 10; 2023-02 to 05). This is what Aaj tagged
"pakistan-stock-exchange", not a complete record of PSX news. 56 articles are
syndicated from Business Recorder (author field).

**Terms and access** (read 2026-11-08): english.aaj.tv has no terms page; its
About page says it belongs to the Business Recorder Group, so Business
Recorder's terms are applied (see news_rss_articles). robots.txt allows
`/trends/` and `/news/` (disallows `/search`, print views, some author pages).
Requests at one per 3 s. Same full-text decision as news_rss_articles.

### news_gdelt_index

| | |
|---|---|
| Purpose | Index of historical PSX/KSE-100 articles (2017 onward) on Dawn, Tribune and Business Recorder: which articles existed and when GDELT saw them. Input to the text-recovery step (`news_wayback_backfill`, not built yet). No article text. |
| Loader | `backend/jobs/news_gdelt.py`; nightly at 21:41 UTC and on demand via `.github/workflows/news_gdelt_backfill.yml` |
| Fetch mode | `reproducible`: the API can be re-queried, but GDELT's index can change and it rate-limits heavily |
| Location | HF `news/backfill/gdelt/`: `articles.parquet`, `windows.parquet` (status of every 30-day query window), `coverage_by_month.csv`, `raw/` (API responses), `runs/`, `manifest.json` |
| Format | Parquet, one row per article: `article_id` (same scheme as news_rss), `url`, `canonical_url`, `title`, `seendate` (UTC; when GDELT saw it, **not** publish time), `domain`, `language`, `sourcecountry`, `socialimage`, `url_mobile`, `query_sha`, `window_start`, `first_fetched_at` |
| Query | `("KSE-100" OR "KSE100" OR "Pakistan Stock Exchange")` restricted to `brecorder.com`, `dawn.com`, `tribune.com.pk`; `mode=artlist`, 30-day windows from 2017-01-01 |

State on 2026-11-16: **8,946 articles**, seen 2017-01-01 to 2026-11-06;
119 of 121 windows complete (still failing on rate limits: 2017-06-30 to
07-30, and the current window). No window hit the 250-record cap.

| Year | Business Recorder | Dawn | Tribune |
|---|---|---|---|
| 2017 | 341 | 636 | 623 |
| 2018 | 0 | 591 | 545 |
| 2019 | 0 | 462 | 510 |
| 2020 | 4 | 440 | 411 |
| 2021 | 0 | 451 | 463 |
| 2022 | 0 | 461 | 304 |
| 2023 | 235 | 340 | 128 |
| 2024 | 398 | 270 | 258 |
| 2025 | 371 | 187 | 282 |
| 2026 | 0 | 30 | 205 |

Coverage limits, to be stated wherever this is used:

- **Business Recorder is missing from GDELT for 78 of 119 months**: nothing
  for 2018 to early 2023 (4 articles in 2020) and nothing after 2025-11. This
  is GDELT's coverage, not failed requests.
- Dawn has 9 months with no articles; Tribune has none.
- Coverage thins in 2026 (median 74 articles a month overall; 1 to 22 a month
  from 2026-02 on). The RSS archive is the main source from 2026-11-08.
- July 2017 is incomplete until its window succeeds.
- Only articles matching the three phrases are listed: this is not all PSX
  news, and "PSX" alone cannot be searched (GDELT rejects phrases under 4
  characters).
- GDELT counts a window's end date as a whole day, so items seen on a
  window's last day also come back in the next window (deduplicated).

**Terms and access** (read 2026-11-08, gdeltproject.org/about.html): "all
datasets released by the GDELT Project are available for unlimited and
unrestricted use for any academic, commercial, or governmental use of any
kind without fee"; redistribution allowed; "any use or redistribution of the
data must include a citation to the GDELT Project and a link to
https://www.gdeltproject.org/". The API asks for at most one request every
5 seconds; the job waits 10 s. Requests from shared IPs (cloud machines,
GitHub's runners) are often refused with 429, so the backfill took a week of
nightly retries.

### news_finbert_sentiment (status: use under review, ADR-0005)

| | |
|---|---|
| Purpose | Headline sentiment labels for the Sector Analyst, as in proposal FR-05. **ADR-0005 (Proposed) drops FinBERT**; until the team decides, this dataset is not used by anything. |
| Loader | `backend/jobs/news_finbert.py` (run once, 2026-11-09; not scheduled) |
| Fetch mode | `reproducible`: derived from the news datasets with a pinned model |
| Location | HF `news/sentiment/finbert_0bc2ac10_t128/`: `scores.parquet`, `label_by_month.csv`, `spotcheck_sample.csv`, `runs/` |
| Format | Parquet, one row per (`source_dataset`, `article_id`): `label`, `score_positive`, `score_negative`, `score_neutral`, `input_kind`, `n_input_tokens`, `truncated`, `model_name`, `model_revision`, `max_tokens`, `scored_at` |

5,403 rows (RSS 299, Aaj 413, GDELT 4,691) scored with `ProsusAI/finbert`
at revision `0bc2ac10` (model licence CC BY-NC-SA 4.0), title + start of
body, 128 tokens. **Unvalidated:** the 60-row hand spot-check was not done.
Known symptoms: almost no neutral labels for Aaj (2%); non-market stories
labelled, often positive. Source datasets were read only.

### PSX Data Portal (dps.psx.com.pk): not collected

Company announcements and filings (planned dataset `news_psx_announcements`)
are **not collected**. PSX's Terms of Use (www.psx.com.pk/psx/terms-of-use,
read 2026-11-08) prohibit "software, spiders, robots … or other devices or
mechanisms to navigate, search or determine the PSX Website" without prior
written permission; the Data Portal has no separate terms. Decision (Maryam,
2026-11-08): skip until PSX grants written permission. The same clause
affects anything else fetched from dps.psx.com.pk by script (psxdata prices,
KSE-100 levels). Announcements reported by the press are covered by the
news datasets above.
