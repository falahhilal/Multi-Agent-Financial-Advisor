# Data sources

One entry per dataset: purpose, format, consuming agent, where it lives, how
it is fetched, and the source's terms (checked **before** collecting; see
Rule 4 in `CLAUDE.md` and `AGENTS.md`). A dataset is not "collected" until it has
an entry here. Data lives in the private HF dataset `fyp-psx-advisor/fyp-data`
(`news/`, `company/`, `macro/`, one folder per stream); manifests are in
`backend/data_manifests/<stream>_<dataset>.json`.

Entries are grouped by stream. Each stream owner edits only their section.

---

## Company data stream (owner: Falah) — prices, actions and annual statements collected; Stream A incomplete

The canonical 18-ticker sector mapping is supplied in
`backend/universe/psx_v1.yaml`. Its deterministic CSV export is generated from
that file; sector names must not be inferred or replaced.

**Permission and current status.** The team confirms written permission for the intended PSX/pyPSX historical-price, corporate-action and company-fundamentals collection and sharing to its private Hugging Face dataset. This records only that permission as described by the team; it does not assert unrelated licensing or redistribution rights. On 2026-11-22, the 18 active-company price datasets, 386 corporate-action rows and 13,379 annual statement rows were published under `company/` in the existing private dataset. Prices remain partial after 62 of 53,012 source rows failed OHLC validation. Actions and fundamentals were successfully fetched from their SDK endpoints, but their source dates/fields do not support point-in-time use. Stream A remains incomplete.

**Historical prices — pyPSX 2.8.3 (pandas 2.3.3).** The provider’s [history archive documentation](https://docs.pypsx.com/pypsx-history-archive/) and [history API reference](https://docs.pypsx.com/api-history-archive/) describe the historical endpoint. The [fundamentals and dividends documentation](https://docs.pypsx.com/pypsx-fundamentals-dividends/) describes separate action/fundamentals APIs; their existence does not establish the PIT publication dates needed here. The installed package metadata identifies pyPSX as proprietary ([PyPI release](https://pypi.org/project/pypsx/2.8.3/)). The verified calls were `pypsx.get_archive_status()` and `pypsx.get_historical(ticker, start="2015-01-01", end="2026-11-20", adjust_splits=False, dividends=True)`. The earlier archive status reported daily prices from 1998-06-30 through 2026-11-20 and 1,517 symbols. On 2026-11-22 all 18 active tickers returned a nonempty response. The run contained 53,012 source rows, of which 52,950 passed normalized OHLCV validation; 62 rows across 16 tickers were rejected because open/close values were inconsistent with low/high. No ticker request failed. Accepted dates were 2015-01-01 through 2026-11-20 for 17 tickers; SYS began 2015-02-06. These are source results, not proof of complete sessions or overall data quality. One raw SDK snapshot and one normalized Parquet file were retained per ticker. The HBL sample fields were `symbol`, `datetime`, `open`, `high`, `low`, `close`, `volume`, `adj_factor`, `tr_close`, and `dividend`; the inspected sample had `adj_factor` 1.0 and null `tr_close`/`dividend`. The adapter requested as-traded prices (`adjust_splits=False`); it preserves the provider adjustment factor and dividend separately, leaves `adjusted_close` null, and does not synthesize total-return prices. The SDK emitted a pandas chained-assignment `FutureWarning` after successful calls; it is a compatibility warning, not a failed request.

**Corporate actions — pyPSX 2.8.3.** The installed SDK exposes `get_corporate_actions(symbol, start, end, include_quarantined=True)`. The 18-ticker 2015-01-01–2026-11-20 run returned 386 rows: 360 cash dividends, 2 bonus issues, 3 stock splits, 5 rights issues and 16 other events. These are endpoint results, not proof of event completeness. The inspected response includes ex-date and provider action/factor fields, but no announcement, publication or availability date. Quarantined/unapplied source events are retained. Ex-date is not treated as public availability; actions lacking availability dates are excluded from PIT access and adjustment validation remains unevaluated.

**Fundamentals — pyPSX 2.8.3.** `get_fundamentals(symbol, statement=None, wide=False)` returned 13,379 annual item rows for the 18 tickers, with observed year coverage from 2015 through 2025 or 2026 depending on ticker. The source identifies statement type, item, year and value. It does not supply publication/availability dates, unit or currency in the inspected response, and no quarterly period semantics were established. These records are archived and normalized for reference only; they are excluded from point-in-time reads and must not be backfilled into historical backtests. The endpoint’s returned row count does not establish complete historical statements or the requested derived metrics’ quality.

**Provider limits and open decisions.** pyPSX availability is not guaranteed. Its proprietary package metadata and the team’s permission statement do not establish unrelated licensing or redistribution rights. No approved PSX session calendar or ticker-specific suspension evidence is available in the current project. Adjusted-price validation, company-sector provenance and historical listing/delisting provenance remain unresolved. Do not infer missing sessions from weekdays. ENGRO remains historical metadata; no exact delisting date was invented. Board lots and listing dates remain null unless verified.

The price loader is `backend/jobs/company_ingest.py`; the auxiliary loader is `backend/jobs/company_aux_ingest.py`. Both are run from `backend/`, use per-ticker isolation and the shared HF helper under `company/`, and do not make pyPSX a runtime dependency. Raw SDK DataFrame snapshots preserve the returned table representation and attrs, not original HTTP wire bytes; normalized datasets are Parquet. The 2026-11-22 action upload has data revision `f36551e562b4c1db46553bc4d9b6ca96f67c23fd` and manifest revision `3f25136328dc3a0d3f595061a8613229cbb1c75d`; fundamentals revisions are `ae5d72543dbfbcef7f4adbd4bf81bb26eb921929` and `64ea2cddd199496f7d0de61aecf7f2133b91b8f9`. All 58 files in each dataset folder, including the manifest, were remotely checked for size; content checksums were verified. Canonical manifests are `backend/data_manifests/company_company_actions.json` and `backend/data_manifests/company_company_fundamentals.json`. Price upload details remain in `backend/jobs/company_data.md`.


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

### PSX Data Portal announcements and filings: not collected

Company announcements and filings (planned dataset `news_psx_announcements`)
are **not collected**. The team reports written permission for its intended
PSX/pyPSX historical-price collection and private HF dataset sharing; this
section does not treat that statement as permission for announcements,
filings, KSE-100 levels, unrelated endpoints, or other licensing and
redistribution rights. Verify the source-specific scope before collecting
those datasets. Announcements reported by the press are covered by the news
datasets above.

---

## SBP & macro stream (owner: Mahrukh)

### macro_policy_rate

| | |
|---|---|
| Purpose | SBP policy-rate history for the Macro Agent (monetary-stance signals) and the backtests (point-in-time rate regime; corridor context for `CASH` returns alongside T-bill yields). |
| Loader | `backend/jobs/macro_policy_rate.py`. Run manually after each MPC decision and before each experiment freeze (the series are As-Needed). Not scheduled in GitHub Actions, because the API key expires every 90 days. |
| Source | SBP EasyData API, dataset "Structure of Interest Rate: State Bank of Pakistan Policy Rates". Endpoints `https://easydata.sbp.org.pk/api/v1/series/<series_key>/meta` and `/data`. The key is passed as `api_key`, read from `EASYDATA_API_KEY`, and never logged, stored or uploaded (the job scans every output for it). |
| Series | `TS_GP_IR_SIRPR_AH.SBPOL0030` State Bank of Pakistan's Policy (Target) Rate (role `policy_target`); `TS_GP_IR_SIRPR_AH.SBPOL0020` State Bank of Pakistan's Repo Rate (`repo_floor`); `TS_GP_IR_SIRPR_AH.SBPOL0010` State Bank of Pakistan's Reverse Repo Rate (`reverse_repo_ceiling`). Names are checked against `/meta` on every run; a mismatch aborts before any data is fetched. Each series' start date is read from `/meta`. |
| Fetch mode | `append_only_snapshot`. The API can be queried again, but SBP may revise or remove data without notice, so each run's raw responses are the record of what we saw. Changed, removed and back-filled observations against the previous run are logged as warnings. |
| Location | HF `macro/policy_rate/`: `raw/fetch_date=D/<run>_<series_key>_{meta,data}.json` (unmodified responses), `interim/fetch_date=D/observations-<run>.parquet` (parsed, values as returned), `processed/policy_rate.parquet` (validated; rebuilt each run, HF history keeps earlier versions), `runs/fetch_date=D/run-<run>.json` (per-run log), `manifest.json` |
| Format | Parquet, long format, one row per series per observation: `series_key`, `series_name`, `series_role`, `observation_date`, `effective_to` (next observation of the same series, exclusive), `value` (percent), `unit`, `observation_status`, `status_comments`, `dataset_name`, `is_policy_rate`, `policy_rate_basis`, `published_at`, `published_at_basis`, `portal_last_refresh_date`, `source_endpoint` (no key), `fetched_at` (UTC), `raw_file`, `run_id` |
| Policy-rate flag | The three series are never merged into one column. `is_policy_rate` is true when the value was the official policy rate for at least part of the time it was in effect: the O/N Reverse Repo from 2009-08-17 to 2015-05-24 (per the dataset description), the Policy (Target) Rate from 2015-05-25. The Reverse Repo series itself begins in 1956, but is flagged as the policy rate only from 2009-08-17 to 2015-05-24. The Repo Rate is never flagged. Policy rate on day d: take the series whose window contains d, then its latest row with `observation_date` ≤ d. |
| Point-in-time key | **None yet.** The API has no publication-date column. `published_at` is null with `published_at_basis = "unknown"`; `observation_date`, `fetched_at` and the portal last-refresh date (from `/meta`, when present) are stored instead. Decision (Mahrukh, 2026-11-12): option A now; announcement dates will come from a separate MPC calendar dataset (option E). |
| Coverage | Per-series start dates from `/meta` on 2026-11-12: Policy (Target) Rate 2015-05-25, Repo Rate 2009-08-17, Reverse Repo Rate 1956-01-01; last observation 2026-10-26; portal last refresh 2026-10-28. |
| Citation | State Bank of Pakistan, (2026), As-Needed data on <series name> from Structure of Interest Rate: State Bank of Pakistan Policy Rates, Source EasyData |

**Terms and access** (EasyData disclaimer read 2026-11-12):

- Data are provided for information and reference, with no warranty of
  accuracy or availability; SBP may alter data and methods without notice.
- Intended users include academia and researchers.
- The disclaimer does not address redistribution.
- Machine access is explicitly offered by the EasyData Developer Guide (REST
  API with a personal key; 2,000 requests/day, 250/hour; key valid 90 days).
  `easydata.sbp.org.pk/robots.txt` returned 404 (no restrictions) on 2026-11-12.
- The guide's sample code disables TLS verification (`verify=False`); our
  loader keeps default verification.
- The main site `sbp.org.pk` blocks automated requests and is **not** used as a
  source by any job.

Handling: private dataset, non-commercial academic use, never redistributed;
raw snapshots kept with `fetched_at`.

Known gaps and limits:

- **No publication date.** The API gives only the observation date, and it is
  not yet confirmed whether that is the announcement date or the effective
  date (SBP decisions usually take effect the day after announcement). Until
  the MPC announcement/effective-date calendar (follow-up dataset) exists, no
  backtest may read this series as point-in-time without a documented rule.
  See the date finding below: dates are mostly "with effect from", with two
  2020 exceptions.
- As-Needed frequency: a row exists only when a rate changes. Forward-fill to
  build a daily series; never back-fill before a series' first observation.
- No Target Rate before 2015-05-25, and no designated policy rate before
  2009-08-17, in this dataset.
- SBP may revise history silently; we only detect revisions between our own
  runs.
- Field names confirmed on 2026-11-12 (`/meta`: "Available Since", "Available
  Upto", "Last Refresh Date", "Reference Dataset Name"; `/data`: columns/rows
  layout from the Developer Guide).
- An expired key stops refreshes until it is renewed.
- Compared every row of all three series against SBP's OVR Repo History PDF
  (archive.sbp.org.pk copy, ends 2026-04-28) on 2026-11-12. All values match
  on every shared date. Differences: (a) EasyData's Reverse Repo lacks two
  1991 rows: 1991-02-26 (10%), which repeats the 1977-06-07 value and so
  carries no new information, and 1991-03-25 (12%), the only 1991 row that
  would change a value (EasyData goes from 10% on 1977-06-07 straight to 13%
  on 1991-10-10). Pre-2009 history is not used as the policy rate;
  (b) EasyData has two extra dates in all three series, 2020-04-16 and
  2020-05-16, each with the same values as the PDF rows for 2020-04-17 and
  2020-05-18; (c) EasyData also has 1956-1977 history and the 2026-10-26 row,
  which the PDF does not. Rows where only the Repo floor changed (e.g.
  2013-02-11) repeat the Reverse Repo value; this is genuine. Extra rows are
  kept as returned.
- The PDF labels its dates "with effect from", and EasyData's dates equal the
  PDF's except for the two extra 2020 dates, so `observation_date` is mostly
  the effective date but not consistently (the 2020 extras may be announcement
  dates). Do not treat `observation_date` as purely the effective or the
  announcement date until checked against MPC statements.

### macro_tbill_auctions

| | |
|---|---|
| Purpose | Market T-bill yields so `CASH` earns a realistic return and the fixed-rule baseline is credible; short-rate context for the Macro Agent. |
| Loader | `backend/jobs/macro_tbill_auctions.py` (EasyData helpers shared with `macro_policy_rate` in `backend/jobs/easydata.py`). Run manually after auctions and before each experiment freeze. |
| Source | SBP EasyData API, dataset "Structure of Interest Rate: T-Bills Auction Result" (`TS_GP_BAM_SIRTBIL_AH`). Endpoints `https://easydata.sbp.org.pk/api/v1/series/<series_key>/meta` and `/data`; key from `EASYDATA_API_KEY`, never logged, stored or uploaded (every output is scanned for it). |
| Series | `TS_GP_BAM_SIRTBIL_AH.TB0010` / `TB0020` / `TB0030`: Treasury Bills' Cut-off Yield 3-Months / 6-Months / 12-Months; `TB0040` / `TB0050` / `TB0060`: Treasury Bills' Weighted Average Yield 3-Months / 6-Months / 12-Months. Names are checked against `/meta` on every run after removing a leading ". " (the prefix appears only in the manual CSV's "Series Display Name"; the API names, in `/data` and `/meta`, have none; the strip rule stays); a mismatch aborts before any data is fetched. |
| Fetch mode | `append_only_snapshot`, with revision warnings between runs (same reasoning as `macro_policy_rate`). |
| Location | HF `macro/tbill_auctions/`: `raw/fetch_date=D/<run>_<series_key>_{meta,data}.json` (unmodified responses), `interim/fetch_date=D/observations-<run>.parquet` (values and names as returned), `processed/tbill_auctions.parquet` (validated), `runs/fetch_date=D/run-<run>.json` (per-run log with check details and gap report), `manifest.json` |
| Format | Parquet, long format, one row per series per auction date: `series_key`, `series_name` (". " removed), `tenor_months` (3, 6, 12), `yield_type` (`cutoff`, `weighted_average`), `observation_date` (auction date), `value` (percent; null where SBP reports "Missing value"), `is_missing`, `unit`, `observation_status`, `status_comments`, `dataset_name`, `published_at`, `published_at_basis`, `portal_last_refresh_date`, `source_endpoint` (no key), `fetched_at` (UTC), `raw_file`, `run_id` |
| Point-in-time key | `published_at` = auction date 23:59:59 Asia/Karachi, stored in UTC; `published_at_basis = "auction_date_end_of_day_pkt"`. Evidence (Mahrukh, 2026-11-16): EasyData dates 30-Sep-2026 and 14-Oct-2026 are SBP auction dates (settlement is the next day); EasyData values match SBP's published cut-offs after rounding (11.9944 → 11.99, 11.9999 → 12); SBP results are public the same afternoon. **Checked on 2026 auctions only.** |
| Coverage | From the EasyData dataset web page on 2026-11-16: Available Since 24-Jun-2004, Available Upto 14-Oct-2026, Last Refresh Date 16-Oct-2026, frequency As-Needed. Manual CSV download (2026-11-14): 3,516 rows, 586 dates per series, no duplicates. |
| Citation | State Bank of Pakistan, (2026), As-Needed data on <series name> from Structure of Interest Rate: T-Bills Auction Result, Source EasyData |

**Terms and access:** as for `macro_policy_rate` (same EasyData disclaimer and
Developer Guide; `sbp.org.pk` is not used as a source). Same handling: private
dataset, non-commercial academic use, never redistributed; raw snapshots kept
with `fetched_at`.

Checks: the run **fails** on a wrong series key or name, duplicate dates,
unparseable dates, non-numeric values, values ≤ 0 or ≥ 100, dates after the
run date, fewer than two rows per series, or an empty value whose status is not
"Missing value". It **warns** on: first observation different from Available
Since, an unexpected dataset name, several units, yields outside [1, 35],
weighted average above cut-off by more than 0.01, cut-off/weighted-average
pairs with one side empty, a value present with status "Missing value", the
latest auction more than 45 days old, and revisions since the previous run.

Known gaps and limits:

- 507 rows (in the 2026-11-14 CSV) have status "Missing value", spread through
  2004-2024, mostly in the 6- and 12-month series. 503 of them are empty and
  are kept with `value` null and `is_missing` true; counts and dates are in
  each run log. The other 4 have a value (TB0030 and TB0060 on 2021-12-01 and
  2021-12-15); the value is kept (`is_missing` false) and the rows are listed
  in a warning.
- Some values lack a leading zero (e.g. ".1215"); they parse as numbers. TB0050
  on 2007-12-05 is .1215, which is implausible for a yield in percent and is
  flagged by the [1, 35] range warning, not corrected.
- Precision varies: most older values have up to 4 decimals; a few carry 7-9
  decimals (seen in both the 2026-11-14 CSV and the API; likely
  floating-point noise); 2026 values have 2 or fewer (SBP's published cut-offs
  are rounded, e.g. 11.9944 → 11.99). The run log records the maximum
  decimals per series.
- Expected warnings in the real data: weighted average above cut-off (e.g.
  6-month on 2009-10-07) and pairs with one side empty (e.g. 2024-10-02).
- Gaps of more than 21 days between auction dates (reported in `extra.gaps`,
  not warned): 2009-06-17 to 2009-07-15, 2010-06-16 to 2010-07-14, 2024-01-10
  to 2024-02-06, 2026-03-16 to 2026-04-15, 2026-09-02 to 2026-09-30.
- SBP held an auction on 2026-09-16 (settlement 2026-09-17) that is missing
  from EasyData.
- SBP also auctions 1-month bills; this dataset does not include them.
- The `published_at` rule was checked on 2026 auctions only. Older dates are
  assumed to be auction dates too, but this has not been verified.
- SBP may revise history silently; we only detect revisions between our own
  runs. An expired key stops refreshes until it is renewed.

### macro_inflation

| | |
|---|---|
| Purpose | Monthly inflation (headline CPI, core, WPI, SPI) for the Macro Agent's real-rate and price-pressure signals, and for real-return context in the backtest. |
| Loader | `backend/jobs/macro_inflation.py` (EasyData helpers in `backend/jobs/easydata.py`). Run manually after each monthly refresh (around the 5th) and before each experiment freeze. One run makes 120 requests (60 `/meta` + 60 `/data`; limits are 250/hour and 2,000/day per key): wait at least one hour between runs, and do not run other EasyData loaders in the same hour. |
| Source | SBP EasyData API, dataset "Inflation Snapshot (New Base: 2015-16)" (`TS_GP_PT_CPI_M`). Original data source: Pakistan Bureau of Statistics. Endpoints `https://easydata.sbp.org.pk/api/v1/series/<series_key>/meta` and `/data`; key from `EASYDATA_API_KEY`, never logged, stored or uploaded (every output is scanned for it). |
| Series | 60 series, `TS_GP_PT_CPI_M.P00011516` to `P00601516`: 15 measures (National CPI, Urban CPI, Rural CPI, Urban Food CPI, Rural Food CPI, Urban Non-Food CPI, Rural Non-Food CPI, WPI, WPI Food, WPI Non-Food, SPI, Urban/Rural NFNE Core Inflation, Urban/Rural Trimmed Core Inflation) x 4 bases: Year-on-Year (`P0001`-`P0015`), 12-Months Moving average (`P0016`-`P0030`), Period average Since July (`P0031`-`P0045`), Month-on-Month (`P0046`-`P0060`). API names follow "<measure>; an Inflation Measure (<base> basis)", e.g. "National CPI; an Inflation Measure (Year-on-Year basis)"; the manual CSV's "Series name" column shows ", " instead of "; ". Confirmed 2026-11-21 for `/meta` (all 60 series passed the name check on the first `--no-upload` run) and for `/data` (the saved raw responses of that run). The loader expects the API's "; " form exactly. `index_group`, `region`, `component` and `measure_base` come from a fixed table in the loader, not from parsing names. Names are checked against `/meta` every run after removing leading dots and spaces (the manual CSV's "Series Display Name" indents with ". " or "............. "); a mismatch aborts before any data is fetched. |
| Fetch mode | `append_only_snapshot`, with warnings for value revisions and for status-only changes (status or status comment changed, value unchanged) between runs. |
| Location | HF `macro/inflation/`: `raw/fetch_date=D/<run>_<series_key>_{meta,data}.json` (unmodified responses), `interim/fetch_date=D/observations-<run>.parquet` (values and names as returned), `processed/inflation.parquet` (validated), `runs/fetch_date=D/run-<run>.json` (per-run log with check details), `manifest.json` |
| Format | Parquet, long format, one row per series per month: `series_key`, `series_name` (leading dots and spaces removed), `index_group` (`cpi`, `core_nfne`, `core_trimmed`, `wpi`, `spi`), `region` (`national`, `urban`, `rural`; WPI and SPI are `national`), `component` (`general`, `food`, `non_food`; core and SPI are `general`), `measure_base` (`yoy`, `mma12`, `period_avg_since_july`, `mom`), `base_year` (`2015-16`), `observation_date` (month end), `value` (percent; null where empty), `is_missing`, `is_back_calculated` (`observation_date` before 2019-08-01), `unit`, `observation_status`, `status_comments`, `dataset_name`, `published_at`, `published_at_basis`, `portal_last_refresh_date`, `source_endpoint` (no key), `fetched_at` (UTC), `raw_file`, `run_id` |
| Point-in-time key | `published_at` = the 10th of the month after `observation_date`, 23:59:59 Asia/Karachi, stored in UTC, but never earlier than 2019-09-10 23:59:59 Asia/Karachi; `published_at_basis = "conservative_10th_next_month_rebase_2019_09"`. Evidence (Mahrukh, 2026-11-20): the SBP Advance Release Calendar 2026 (ARC.xls, archive.sbp.org.pk) lists the Inflation Monitor no later than the 3rd-6th of the following month, holidays moving to the next working day; EasyData updates key indicators the next working day after the PBS release and others after the Inflation Monitor; Oct-2026 data was refreshed 05-Nov-2026. The 2015-16 base was first published in September 2019 (August 2019 data); earlier months were back-calculated. National YoY 2019-07 = 8.38 and 2019-08 = 10.49 in the 2026-11-20 CSV match the figures reported at the September 2019 rebase. |
| Coverage | From the EasyData dataset web page on 2026-11-20: Jul-2016 to Oct-2026, monthly, Last Refresh Date 05-Nov-2026. Manual Long Format CSV (2026-11-20): 7,440 rows (60 series x 124 months), month-end dates 2016-07-31 to 2026-10-31, no duplicates, no skipped months. |
| Citation | State Bank of Pakistan, (2026), Monthly data from Inflation Snapshot (New Base: 2015-16), for 31-Jul-2016 to 31-Oct-2026, Source EasyData, Inflation Snapshot (New Base: 2015-16), 05-Nov-2026. Original data source: Pakistan Bureau of Statistics. (The loader writes the citation with the run's actual date range and refresh date to the run log and manifest.) |

**Terms and access:** as for `macro_policy_rate` (same EasyData disclaimer and
Developer Guide; `sbp.org.pk` is not used as a source). Same handling: private
dataset, non-commercial academic use, never redistributed; raw snapshots kept
with `fetched_at`.

Checks: the run **fails** on a wrong series key or name, duplicate dates,
unparseable or non-month-end dates, non-numeric values, values ≤ -100 or
≥ 1000, dates after the run date, fewer than two rows per series, or an empty
value whose status is not "Missing value". It **warns** on: an empty value
after a series' first value, leading empty values beyond the expected months,
a value present with status "Missing value", values outside [-50, 100],
skipped months, first observation different from Available Since, series
ending on different months, the latest month more than 45 days old, an
unexpected dataset name, several units, value revisions since the previous
run, and status-only changes since the previous run.

Known gaps and limits:

- Leading empty values are expected (status "Missing value"; the status
  comment explains the trimmed and 12MMA computation) and are kept with `value`
  null and `is_missing` true, without warning: every 12MMA series 2016-07 to
  2017-05; trimmed core YoY and period average 2016-07 to 2017-06; trimmed core
  12MMA 2016-07 to 2018-05; trimmed core MoM 2016-07. Counts are in each run
  log.
- 30 rows on 2026-04-30 (in the 2026-11-20 CSV) have status "Missing value"
  (comment "0") but a value; the value is kept (`is_missing` false) and the
  rows are listed in a warning. The other 30 April-2026 rows are "Normal" with
  comment "0". A later fix of these statuses is reported as a status-only
  change.
- Values range from about -7.4 (MoM) to 52.4 (Rural Food YoY, May 2023, a real
  value). Precision varies: most values have 1-2 decimals, about 540 have 7-9
  (e.g. 13.37790337); some lack a leading zero (".5", "-.3"). Values are stored
  as returned; the run log records the maximum decimals per series.
- Months before 2019-08 were back-calculated at the 2019 rebase, not published
  at the time (`is_back_calculated`); their `published_at` is floored at the
  rebase release.
- The `published_at` rule is conservative (10th of the next month) and was not
  checked month by month against actual release dates. A release delayed past
  the 10th would make the rule leak.
- Older bases (2007-08 and earlier) are not collected, so this dataset has no
  inflation before Jul-2016; backtest windows before then have no CPI series.
- EasyData `/data` returns invalid JSON for 21 series (seen 2026-11-21): the
  status comment on the leading empty rows, "The observation status is missing
  "MIS" due to trimmed inflation ...", has its quotes around `MIS` unescaped.
  Affected: trimmed core YoY (P0014, P0015), every 12MMA series (P0016-P0030),
  trimmed core period average (P0044, P0045) and trimmed core MoM (P0059,
  P0060). The manual CSV is not affected. The loader repairs only the exact
  bytes `missing "MIS" due` (escaping the quotes), only when the body does not
  parse, and only in memory for parsing; raw files keep the response exactly
  as returned. Repairs are counted per series in the run log (`json_repairs`);
  a repair on a series with no expected leading empties, or a count different
  from its leading empty rows, warns. Any other invalid JSON still fails the
  run.
- The loader requests 60 fixed keys and cannot detect series added to the
  dataset later.
- SBP may revise history silently; we only detect revisions between our own
  runs. An expired key stops refreshes until it is renewed.

### macro_fx

| | |
|---|---|
| Purpose | Daily PKR exchange rates for the Macro Agent's currency signals (PKR/USD above all) and FX context in the backtest. |
| Loader | `backend/jobs/macro_fx.py` (EasyData helpers in `backend/jobs/easydata.py`). Run manually after each monthly refresh (around the 5th) and before each experiment freeze. One run makes 46 requests (23 `/meta` + 23 `/data`); keep all EasyData runs within 250 requests per hour and 2,000 per day per key. The run log and the final log line give the row count per series. |
| Source | SBP EasyData API, dataset "Bank Floating Daily Average Exchange Rates (PKR per National Currency)" (`TS_GP_ES_FADERPKR_M`), source SBP. Endpoints `https://easydata.sbp.org.pk/api/v1/series/<series_key>/meta` and `/data`; key from `EASYDATA_API_KEY`, never logged, stored or uploaded (every output is scanned for it). |
| Series | 23 series, `TS_GP_ES_FADERPKR_M.XRDAVG0010` to `XRDAVG0230`, unit PKR: "Daily Average Exchange rate of PAK Rupees per <currency>" for AUD, BHD, CAD, CNY, DKK, HKD, JPY, KWD, MYR, NZD, NOK, OMR, QAR, SGD, SEK, CHF, SAR, THB, TRY, AED, GBP, USD (`XRDAVG0220`), EUR (`XRDAVG0230`), in key order. Values are PAK Rupees per one unit of the currency (JPY per 1 yen: about 1.76 PKR in Oct-2026). `currency_code` and `currency_name` come from a fixed table in the loader, not from parsing names. Expected names are taken from the manual CSV's "Series name" column (2026-11-22) and are checked exactly against `/meta` on every run, after removing leading dots and spaces; a mismatch aborts before any data is fetched. Confirmed 2026-11-22: all 23 `/meta` names matched the CSV exactly on the first `--no-upload` run (including "Hong Kong Dollar" and "U.A.E. Dirham"). The spelling "Hon Kong" appears only on the EasyData dataset web page, not in the API or the CSV. |
| Fetch mode | `append_only_snapshot`, with warnings for value revisions and for status-only changes (status or status comment changed, value unchanged) between runs. |
| Location | HF `macro/fx/`: `raw/fetch_date=D/<run>_<series_key>_{meta,data}.json` (unmodified responses), `interim/fetch_date=D/observations-<run>.parquet` (values and names as returned), `processed/fx.parquet` (validated), `runs/fetch_date=D/run-<run>.json` (per-run log with row counts and check details), `manifest.json` |
| Format | Parquet, long format, one row per series per date: `series_key`, `series_name` (leading dots and spaces removed), `currency_code` (ISO 4217), `currency_name`, `observation_date`, `value` (PKR per one unit of the currency), `unit`, `observation_status`, `status_comments`, `dataset_name`, `published_at`, `published_at_basis`, `portal_last_refresh_date`, `source_endpoint` (no key), `fetched_at` (UTC), `raw_file`, `run_id` |
| Point-in-time key | `published_at` = the 10th of the month after `observation_date`, 23:59:59 Asia/Karachi, stored in UTC; `published_at_basis = "monthly_release_10th_next_month"`. Evidence (Mahrukh, 2026-11-22): the dataset overview says "This daily data is compiled and disseminated on monthly basis"; Oct-2026 data was refreshed 05-Nov-2026; SBP publishes the same series as a monthly table ("Daily Average Banks' Floating Exchange Rates", BFER_Daily.pdf). **Consequence: backtests see each FX value up to about 40 days after its date** (a value for the 1st of a month becomes visible on the 10th of the next month). SBP's daily "Weighted Average Customer Exchange Rates" are a different series (buying and selling rates, close to but not equal to these values), public the same day, and are not collected. |
| Coverage | From the EasyData dataset web page on 2026-11-22: 02-Jul-2013 to 30-Oct-2026, daily, Last Refresh Date 05-Nov-2026. Manual Long Format CSV (2026-11-22): 75,325 rows (23 series x 3,275 dates), no empty values, all status "Normal", no duplicates. |
| Citation | State Bank of Pakistan, (2026), Daily data from Bank Floating Daily Average Exchange Rates (PKR per National Currency), for 02-Jul-2013 to 30-Oct-2026, Source EasyData, Bank Floating Daily Average Exchange Rates (PKR per National Currency), 05-Nov-2026. (The loader writes the citation with the run's actual date range to the run log and manifest; year and refresh date come from the Last Refresh Date, never the run date.) |

**Terms and access:** as for `macro_policy_rate` (same EasyData disclaimer and
Developer Guide; `sbp.org.pk` is not used as a source). Same handling: private
dataset, non-commercial academic use, never redistributed; raw snapshots kept
with `fetched_at`.

Checks: the run **fails** on a wrong series key or name, a `/data` response
that is not valid JSON (no repair, unlike `macro_inflation`), unparseable or
duplicate dates, dates after the run date, fewer than two rows per series, or
a value that is empty, non-numeric or ≤ 0. It **warns** on: weekend dates
other than the four known Saturdays, gaps of more than 14 days between
consecutive dates, a change of more than 10% against the series' previous
observation, series with different date sets or end dates, the latest date
more than 45 days old, first observation different from Available Since, an
unexpected dataset name, a unit other than PKR, a status other than "Normal",
value revisions since the previous run, and status-only changes since the
previous run. Status comments are kept as returned and never warned about.

Known gaps and limits:

- Dates are working days. Four Saturdays exist (2019-08-17, 2022-04-16,
  2022-04-23, 2022-04-30) and are allowed. Holiday gaps of up to 10 days
  (e.g. 2021-05-07 to 2021-05-17) are normal and not warned; the run log
  records the largest gap per series.
- Large real moves exist. USD +5.65% on 2023-01-26 and +6.35% on 2023-01-27
  (below 10%, no warning). Five moves above 10% against the previous
  observation (checked on all 23 series of the 2026-11-22 CSV) warn on every
  run, as expected: TRY (XRDAVG0190) −14.63% on 2018-08-13, −10.65% on
  2021-11-24, +28.10% on 2021-12-21; NOK (XRDAVG0110) −12.15% on 2020-03-19;
  CHF (XRDAVG0160) +13.84% on 2015-01-16. No other series has a move above
  10%.
- 29 rows have status "Normal" but an unrelated status comment ("Iraqi
  invasion of Kuwait from Aug-1990 till Sep-1991"): XRDAVG0030 2022-07-13 to
  2022-08-01 and XRDAVG0130 2016-12-05 to 2016-12-26. Kept as returned, not
  warned.
- Values carry 6-9 decimals (e.g. USD 2026-10-30 = 277.0589286) and are stored
  as returned; the run log records the maximum decimals per series.
- The `published_at` rule rests on the current monthly dissemination and one
  observed refresh date (05-Nov-2026). It was not checked against historical
  release dates; a release after the 10th would make the rule leak.
- No data before 02-Jul-2013; backtest windows before then have no FX series
  from this dataset.
- The loader requests 23 fixed keys and cannot detect series added to the
  dataset later.
- SBP may revise history silently; we only detect revisions between our own
  runs. An expired key stops refreshes until it is renewed.
