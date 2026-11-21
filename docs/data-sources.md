# Data sources

One entry per dataset: purpose, format, consuming agent, where it lives, how
it is fetched, and the source's terms (checked **before** collecting; see
Rule 4 in `CLAUDE.md` and `AGENTS.md`). A dataset is not "collected" until it has
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
| nation.com.pk/rss/business (added 2026-11-20) | primary | 100 | ~6.5 days | no: fetched from each article page (`fetch_pages`) |

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
- **The Nation** (`/copyrights`, read 2026-11-20): "Re-use, republication,
  distribution and modification of any content … for commercial purposes is
  strictly prohibited without written consent"; "Content can be
  used/consulted/printed for personal, academic or non-commercial purposes."
  robots.txt allows our User-Agent on feeds and article pages (disallows only
  admin and ajax paths). Its AI-use policy covers only The Nation's own
  newsroom use of AI.

Handling: the dataset is private, used only for non-commercial academic
research, and never redistributed; no article text is published in the thesis
or app beyond short quotations with citation. Per-feed `store_full_text: false`
switches a feed to headline, link and dates only (no summary, no raw snapshot)
if the team decides a source's terms require it.

**Decision (Maryam, 2026-11-08): keep full text for all seven feeds**, on the
basis above (private, non-commercial academic research, never redistributed).
The same applies to The Nation (added by Maryam, 2026-11-20), whose terms
explicitly allow academic, non-commercial use.
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
| Purpose | Index of historical PSX/KSE-100 articles (2017 onward) on Dawn, Tribune and Business Recorder: which articles existed and when GDELT saw them. Input to `news_gdelt_fulltext`, which fetches the article text. No article text here. |
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

### news_gdelt_fulltext

| | |
|---|---|
| Purpose | Article text for the links in `news_gdelt_index`: the historical (2017 onward) retrieval corpus for the Sector Analyst and the Macro Agent, covering the backtest windows the RSS archive cannot reach. |
| Loader | `backend/jobs/news_gdelt_text.py` (run on demand; re-runs fetch only links not yet settled and re-read stored pages for `no_text` rows) |
| Fetch mode | `reproducible`: pages can be fetched again, but publishers edit and remove them |
| Location | HF `news/backfill/gdelt_text/`: `articles.parquet`, `coverage_by_month.csv`, `raw/<domain>/<article_id>.html.gz` (unmodified pages), `runs/` |
| Format | Parquet, one row per GDELT link: `article_id` (same as `news_gdelt_index`), `domain`, `url`, `final_url`, `status`, `http_status`, `error`, `gdelt_title`, `title`, `seendate`, `published_at` (UTC, from the page), `published_at_raw`, `published_tz`, `modified_at`, `first_public_at`, `published_after_seen`, `authors`, `summary`, `full_text`, `language`, `tags`, `recovery_source` (`live`), `fetched_at`, `raw_file`, `attempts` |
| Point-in-time key | `first_public_at` = the earlier of the page's `published_at` and GDELT's `seendate` (Tribune stamps the last update as `datePublished`) |

Collected 2026-11-16 from the publishers' own sites (three runs, about
3.5 hours, one request every 3 s per site, 0 failed requests left).

| Status | Business Recorder | Dawn | Tribune | Total |
|---|---|---|---|---|
| ok (text recovered) | 1,266 | 3,823 | 3,692 | **8,781** |
| gone (404, or "page not found" / redirect to home page) | 5 | 2 | 21 | 28 |
| no_text (page loaded, text not parsed) | 0 | 0 | 5 | 5 |
| skipped (not an article: author, section, daily stock pages) | 78 | 43 | 11 | 132 |

8,781 of 8,814 article links (99.6%) have text. Text by year
(recovered / links): 2017 BR 259/264, Dawn 633/633, Tribune 619/621; no
later year misses more than 13 links on one site (Tribune 2018: 530/543). Median
73 articles a month across the three sites; 2026-10 and 2026-11 have fewer
than 10. `first_public_at` runs from 2016-01-11 (7 older articles GDELT saw
in 2017) to 2026-11-06. Median text length 2,100 to 3,400 characters by
site; the shortest is 316.

Known issues, to be handled by whoever reads this dataset:

- **Coverage is GDELT's**: no Business Recorder text for 78 of 119 months
  (2018 to early 2023, after 2025-11), because GDELT does not list them.
- **Duplicates**: 7 pairs of rows have identical text. Three are 2017 BR
  links with two slug variants of one article, one is a Dawn link that
  redirects to another (1333244 → 1333101), three are adjacent Dawn/Tribune
  ids the publisher posted twice. Deduplicate on `full_text` or `final_url`
  before indexing.
- Article ids for 2017 BR links come from the old `/YYYY/MM/DD/<id>` URL,
  so they do not match the `/news/<id>` ids the RSS archive uses. No
  overlap in dates, so nothing joins on them today.
- 372 Tribune rows (and 5 Dawn) have a page date later than GDELT's
  sighting; `published_after_seen` marks them and `first_public_at` uses
  the sighting.
- 48 Tribune rows have no `language` (page has no `lang` attribute); all
  are English.
- The 5 `no_text` rows are Tribune long-form pages with a different layout;
  the raw pages are kept. The 28 `gone` rows are candidates for the Wayback
  Machine, which this cloud machine cannot reach.

**Terms and access**: same sites and terms as `news_rss_articles` (Dawn,
Tribune, Business Recorder); robots.txt allows the article paths for our
User-Agent. Same full-text decision: private, non-commercial academic
research, never redistributed.

### news_nation_archive

| | |
|---|---|
| Purpose | Historical (2017 onward) market news from The Nation, for the Sector Analyst's and Macro Agent's retrieval corpus. Complements `news_gdelt_fulltext`, above all where GDELT lists no Business Recorder articles (2018 to early 2023). |
| Loader | `backend/jobs/news_nation_archive.py`, two resumable steps run on demand: `index` reads The Nation's daily sitemaps, `text` fetches the article pages picked from them |
| Fetch mode | `reproducible`: sitemaps and pages can be fetched again, but the publisher edits and removes pages |
| Location | HF `news/backfill/nation/`: `days.parquet`, `links.parquet`, `articles.parquet`, `coverage_by_month.csv`, `raw/sitemap_index/`, `raw/sitemaps/` and `raw/pages/<year>/` (unmodified responses), `runs/` |
| Format | `days.parquet`: one row per calendar day (`day`, `status`, `n_links`, `in_index`, ...). `links.parquet`: every article link in the sitemaps, all sections (`article_id`, `url`, `sitemap_day`, `url_day`, `slug`, `lastmod`). `articles.parquet`: one row per picked link: `article_id`, `url`, `final_url`, `url_day`, `slug_rule`, `status`, `http_status`, `error`, `title`, `published_at` (UTC, from the page), `published_at_raw`, `published_tz`, `modified_at`, `first_public_at`, `published_after_url_day`, `is_market`, `authors`, `summary`, `full_text`, `language`, `tags`, `fetched_at`, `raw_file`, `attempts` |
| Which links are fetched | URL slug contains a market word as a whole hyphen-separated word (`SLUG_RE`: psx, kse, stocks, shares, bourse, equities, bullish, bearish, index, points, investors, trading, market, ...); `slug_rule` = `slug-v1`. `is_market` = title or text names the PSX, the KSE indices, a stock exchange or the stock market. |
| Point-in-time key | `first_public_at` = the earlier of the page's `published_at` and the end (23:59:59 PKT) of the day in the URL |

Collected 2026-11-20 to 2026-11-21 (Maryam) from nation.com.pk, one request
every 3 s: the index in two runs (the second for the days the index left
out), the text in six (105-minute chunks; one
chunk was cut short by a machine restart and resumed from its checkpoint).
No request is left failed.

- **Days**: all 3,610 calendar days from 2017-01-01 to 2026-11-19 read.
  The sitemap index (`/sitemap.xml`) leaves out, in every month, the day
  whose number is the date it is read (the 20th of every month in the copy
  read on 2026-11-20, the 21st in the copy read on 2026-11-21), plus
  2022-02-17. Those daily sitemaps exist, so the job reads every calendar
  day; `in_index` only says whether the last copy of the index listed the
  day. 2022-02-17's sitemap is empty (0 links), the only day with none.
  Median 148 links a day.
- **Links**: 543,422 (all sections). 8,252 picked by the slug rule (1.5%).
- **Articles**: 8,250 `ok` with text, 2 `redirected` (below). 2,693 of
  the `ok` rows are `is_market` (about a third; the rest are slug matches
  such as "flour-market-prices" or "polio-drops-at-transit-points").

| Year | Links | Picked | ok | is_market |
|---|---|---|---|---|
| 2017 | 70,478 | 943 | 943 | 350 |
| 2018 | 66,152 | 900 | 900 | 324 |
| 2019 | 48,648 | 609 | 609 | 234 |
| 2020 | 50,002 | 791 | 791 | 192 |
| 2021 | 54,546 | 799 | 799 | 210 |
| 2022 | 53,474 | 893 | 893 | 287 |
| 2023 | 51,681 | 806 | 806 | 254 |
| 2024 | 54,277 | 813 | 813 | 277 |
| 2025 | 51,010 | 854 | 852 | 305 |
| 2026 (to 11-19) | 43,154 | 844 | 844 | 260 |

Every month from 2017-01 to 2026-11 has `is_market` articles: median 22 a
month, fewest 9 (2020-11), most 36 (2018-10). Median text length 1,632
characters (1,273 for `is_market` rows).

Known issues, to be handled by whoever reads this dataset:

- **The slug filter misses market stories whose URL has none of its
  words.** A sample of days found the daily market report was picked
  whenever one was filed, but this was not measured over the whole
  archive. `links.parquet` keeps every link, so a wider rule can be added
  later as `slug-v2` without reading the sitemaps again.
- **Redirects**: 2 links from 2025-01-28 (including
  `sbp-cuts-policy-rate-by-100-basis-points-to-12pc`) redirect to an
  unrelated story (`cold-forecast-for-lahore`). They are `redirected`, not
  `ok`; their stored text is the other story's. Use `status == "ok"`.
- **Duplicates**: 10 groups of `ok` rows (21 rows) share identical text:
  the same story under two slugs (one with a numeric suffix such as
  `-886763`), or posted again the next day (a 2022-11-23 PSX report also
  filed under 2022-11-24). Deduplicate on `full_text` and keep the earliest
  `first_public_at`.
- **Very short pages**: 36 `ok` rows have under 300 characters, 12 under
  100. Most are cartoon or photo pages whose only text is a caption
  ("STOCK MARKET"); 7 are `is_market`. Drop them before indexing.
- **Dates**: 11 rows carry a page date later than their URL day (updated
  pages); `published_after_url_day` marks them and `first_public_at` caps
  the date at the URL day. Use `first_public_at`, not `published_at`.
- The Nation's bylines are mostly wire and desk names (APP 1,605, Our Staff
  Reporter 1,325, NEWS WIRE 823, Agencies 744, Web Desk 606), so the same
  wire story may also appear in Dawn or Tribune rows of
  `news_gdelt_fulltext`.

**Terms and access**: The Nation's terms (`/copyrights`, read 2026-11-20)
allow use for "personal, academic or non-commercial purposes" (see the
RSS section above). robots.txt allows the sitemaps and article paths for
our User-Agent. The same full-text decision applies: private,
non-commercial academic research, never redistributed.

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
