# Session 2026-11-09 — GDELT backfill, FinBERT sentiment, scheduler gap

**Who:** Maryam Asif

Continues [2026-11-08-maryam-news-stream.md](2026-11-08-maryam-news-stream.md),
which ended with the GDELT run still in progress.

## Decisions taken

All by Maryam.

1. **GDELT runs nightly** (21:41 UTC) as well as on demand, so failed
   windows are retried automatically.
2. **Task 5 (KSE-100 levels) is blocked.** Every source checked runs
   through PSX (terms forbid scripted access without permission), and SBP
   EasyData has no KSE-100 series (checked by Maryam in a browser). Waiting
   on the PSX permission email; a one-off manual browser download is the
   fallback.
3. **FinBERT pinned** to ProsusAI/finbert revision `0bc2ac10`, input = title
   + start of body, max 128 tokens; GDELT is title only.

## Work done

All by Maryam. Commits on `maryam/news-stream`:

- `c94527e` GDELT in **30-day windows** with a 200-minute time budget
  (one-week windows took ~1.6 min each with ~35% failing on 429s, even from
  GitHub's runners). `5743c14` wording, window-overlap note. `f1b713f`
  **nightly schedule**.
- `9c9f523` **FinBERT job** `jobs/news_finbert.py`; `30614c7` notes;
  `a760e88` first manifest. 65 tests pass.
- GDELT run (Actions, 2 h 34 min): 120 monthly windows, 56 ok, 64 failed on
  429s; 3,664 new articles.
- FinBERT run (CPU, here): 5,403 scores, uploaded to
  `news/sentiment/finbert_0bc2ac10_t128/`.
- Manual RSS run on Actions at 20:30 UTC after the scheduler gap: 27 new.

### Datasets touched

| Dataset | Range | Rows | Gaps / notes |
|---|---|---|---|
| `news_gdelt_index` | seen 2017-01-01 to 2026-11-06 | 4,691 (Dawn 1,971, Tribune 1,853, BR 867) | 64 of 120 months failed (retried nightly); **no BR coverage in GDELT for 2018–2022 or 2026**; titles and URLs only |
| `news_finbert_sentiment` | 2017-01 to 2026-11-09 | 5,403 (RSS 299, Aaj 413, GDELT 4,691) | 5 Urdu Aaj rows excluded; unvalidated until the spot-check |
| `news_rss_articles` | to 2026-11-09 | 326 | **ProPakistani: items from ~11:36–15:45 UTC on 11-09 likely lost** (no runs for 14 h); BR fully recovered via sitemap |

FinBERT label shares: RSS 40% neg / 36% neu / 24% pos; Aaj 37% / **2%** /
60%; GDELT titles 35 / 35 / 30. GDELT net tone by year: most negative 2019
(−0.41), positive 2024 (+0.22) and 2025 (+0.15).

## Problems found

- **GitHub stopped running the hourly RSS schedule** after the 10:50 PKT
  run on 11-09: no scheduled runs for ~14 h (all runs that did happen were
  green). A manual run restarted collection. Cause unknown.
- GDELT rate-limits GitHub's runners as well as cloud machines.
- SBP EasyData's Cloudflare blocked an automated browser visit; not
  retried.
- FinBERT gives Aaj's long market reports almost no "neutral" labels, and
  labels non-market RSS stories (sport, greetings), often "positive".

## Still open

- **Check overnight** whether news-rss-hourly ran on its own (around :23)
  and whether news-gdelt-backfill ran at 21:41 UTC. If not, set up an
  external hourly trigger (cron-job.org calling workflow_dispatch with a
  fine-grained token from Falah). *Time-sensitive: ProPakistani loses items.*
- **FinBERT spot-check:** Maryam to fill `human_label` in
  `spotcheck_sample.csv` (60 rows); then compute agreement.
- PSX permission email (Tasks 3 and 5, and Falah's prices); tell Falah.
- GDELT: pull manifest and write the data-sources entry once failed months
  are filled. Wayback recovery not started.
- Data-sources entry for `news_finbert_sentiment`.
- Commits `9c9f523` onward are not merged to `main` yet.

## Next session should

1. Confirm the schedulers ran overnight; if not, set up the external trigger.
2. Score the spot-check and record FinBERT agreement in the manifest.
3. GDELT coverage report + data-sources entries (GDELT, FinBERT), then the
   Wayback recovery job.
