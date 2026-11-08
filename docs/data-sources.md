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
