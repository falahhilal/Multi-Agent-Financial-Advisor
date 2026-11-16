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
| Series | `TS_GP_BAM_SIRTBIL_AH.TB0010` / `TB0020` / `TB0030`: Treasury Bills' Cut-off Yield 3-Months / 6-Months / 12-Months; `TB0040` / `TB0050` / `TB0060`: Treasury Bills' Weighted Average Yield 3-Months / 6-Months / 12-Months. Names are checked against `/meta` on every run after removing a leading ". " (EasyData's "Series Display Name" has it, "Series name" does not); a mismatch aborts before any data is fetched. |
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

- 507 rows (in the 2026-11-14 CSV) have status "Missing value" and no value,
  spread through 2004-2024, mostly in the 6- and 12-month series. They are kept
  with `value` null and `is_missing` true; counts and dates are in each run
  log.
- Four rows have a value but status "Missing value": TB0030 and TB0060 on
  2021-12-01 and 2021-12-15. The value is kept (`is_missing` false) and the
  rows are listed in a warning.
- Some values lack a leading zero (e.g. ".1215"); they parse as numbers. TB0050
  on 2007-12-05 is .1215, which is implausible for a yield in percent and is
  flagged by the [1, 35] range warning, not corrected.
- Precision varies: older rows have up to 4 decimals, 2026 rows 2 or fewer
  (SBP's published cut-offs are rounded, e.g. 11.9944 → 11.99). The run log
  records the maximum decimals per series.
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
