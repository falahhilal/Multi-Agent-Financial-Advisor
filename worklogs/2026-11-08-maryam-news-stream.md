# Session 2026-11-08 — News stream: shared job helpers, RSS archive, Aaj and GDELT backfill

**Who:** Maryam Asif
**Span:** 8–9 Nov 2026

First working session on Stream C (news, filings, KSE-100). Plan: Tasks 1–7
of the news stream. Done: Task 1 (shared helpers), Task 2 (hourly RSS
archive, live). Task 4 partly done (Aaj complete, GDELT running, Wayback not
started). Task 3 skipped on PSX's terms. Tasks 5–7 not started.

## Decisions taken

All by Maryam.

1. **Storage: private HF dataset `fyp-psx-advisor/fyp-data`**, one folder per
   stream (`news/`, `company/`, `macro/`), written only through
   `jobs.common.upload_to_hf`, which refuses paths outside the caller's
   stream. Data never in git; manifests in git. Resolves `plan.md` Step 1,
   shared setup item 5 (where raw data is shared and backed up).
2. **RSS feeds:** keep all three trial feeds (Financial Daily, Pakistan &
   Gulf Economist, ProPakistani); filter only the BR stocks feed (Pakistan/PSX
   keywords). Details in `docs/data-sources.md`.
3. **Keep full article text** for all feeds and for Aaj, despite BR, Dawn and
   Tribune terms restricting reproduction: private, non-commercial academic
   research, never redistributed. **Supervisor confirmation still needed.**
   Recorded in `docs/data-sources.md`.
4. **Skip the PSX Data Portal** (Task 3, and anything else by script from
   dps.psx.com.pk) until PSX gives written permission. PSX's Terms of Use
   forbid "software, spiders, robots … or other devices" without it. The
   earlier PSX PDFs in the separate `fyp-financial-agent` repo were also
   downloaded by script without permission, so they are **not used**.
5. **GDELT backfill runs from GitHub Actions** (manual workflow), because
   GDELT rate-limits the shared cloud machine.
6. **Business Recorder: also read its news sitemap each run**, and move the
   hourly cron from minute 7 to minute 23 (see Problems found).
7. Branch `maryam/news-stream` (not `data/news` as in CLAUDE.md), merged to
   `main` by pull request.

No ADR needed: none of these overrides the proposal. Decision 1 fills an
open item in `plan.md`; decisions 3 and 4 are recorded in `data-sources.md`.

## Work done

All by Maryam. Commits on `maryam/news-stream` (merged to `main` via PRs):

- `37a4d04` **`backend/jobs/common.py`** shared by all three streams:
  `http_get` (User-Agent, robots.txt, per-host rate limit, retries),
  `upload_to_hf` (stream-folder guard, never deletes), `write_manifest`,
  `get_secret`. Plus `.env.example`, `huggingface_hub` in requirements.
- `25ef95e` same-host https→http redirects stay on https (BR and Dawn do
  this); `hf_download`. `642e7f0` `http_post` for read-only form endpoints.
- `10a3fc4` **hourly RSS archiver** `jobs/news_rss.py` +
  `config/news_feeds.yaml` + `.github/workflows/news_rss_hourly.yml`.
  `dc197ec` data-sources entry. `0feee89` first manifest.
- `9bfccbc` **Aaj English PSX archive** `jobs/news_aaj_psx.py`; `028337a`
  first run, data-sources entries (Aaj, PSX not collected).
- `a773ae1` **GDELT index backfill** `jobs/news_gdelt.py` +
  `.github/workflows/news_gdelt_backfill.yml` (manual, resumable).
- `e1dd594` shared parsing helpers moved to `jobs/news_common.py`.
- `eb57751` **BR news sitemap** read each run; cron to minute 23.
  `0b71d52` manifest after the catch-up run.
- Tests: 61 offline tests in `backend/tests/jobs/`, all passing; ruff,
  black, mypy --strict clean.
- HF: uploaded `news/README.md` and the datasets below. Nothing written
  outside `news/`.
- Hourly workflow merged and running on `main` (runs #1–#4 green).
- GDELT workflow merged and started on 2026-11-09; **still running at the
  end of this log**.

### Datasets touched

| Dataset | Source | Date range | Rows / files | Gaps |
|---|---|---|---|---|
| `news_rss_articles` (HF `news/rss/`) | 7 RSS feeds + BR news sitemap | published 2026-09-01 to 2026-11-09 (first run 2026-11-08) | 299 articles (139 BR) after 8 runs | BR id 40448020 missed (rotated out of the sitemap before the fix); BR coverage before the sitemap fix only partial |
| `news_aaj_psx_archive` (HF `news/backfill/aaj_psx/`) | english.aaj.tv PSX tag archive, 14 pages | 2021-01-14 to 2026-11-05 | 418 articles, 418 raw pages | sparse: median 3/month; 11 of 71 months empty (2021-02, 05, 06, 07, 12; 2022-01, 10; 2023-02 to 05) |
| `news_gdelt_index` (HF `news/backfill/gdelt/`) | GDELT DOC 2.0 API | from 2017-01-01 | run in progress | see Problems found |
| `news_psx_announcements` | PSX Data Portal | — | **not collected** (terms) | — |

## Problems found

- **PSX's terms forbid automated access without written permission.** This
  blocks Task 3 and Task 5's planned source (dps.psx.com.pk), and it also
  applies to **Falah's `psxdata` pulls**, which hit the same portal. Falah
  needs to know. A permission email has been drafted (in the session chat);
  not sent yet.
- **BR's RSS (30 items) cannot keep up with BR's publishing bursts.** On
  2026-11-09 the archive held only 30 of BR's 100 newest articles. Fixed by
  reading BR's news sitemap; after one catch-up run 0 of the sitemap's 100
  were missing, and only id 40448020 from the morning's gap is lost (BR ids
  are sequential, so it can still be fetched by id).
- **GitHub ran the "hourly" schedule only every 4–6 hours** at minute 7.
  Moved to minute 23; watch whether it improves.
- **Terms were read after the first feed probe**, not before as CLAUDE.md
  rule 4 requires. Seven feed requests were made before the terms check. No
  site forbade automated access, but the order was wrong.
- **GDELT:** searchable from 2017-01-01 (a 2017-01 week returned 50
  records), but phrases under 4 characters are rejected ("PSX"), the shared
  cloud machine got 429s even at one request a minute, BR appeared in 2017
  but not in 2018 or 2026 test weeks, and the most recent week returned 0
  articles twice. GDELT gives title, URL and seen-time only, no text.
- **Wayback Machine** drops connections from the cloud machine (upstream,
  not our network policy). Needs GitHub Actions or a PC.
- **Aaj is too sparse** to back news-based backtesting before 2023.
- HF org role "contributor" cannot commit (fixed: role changed). HF uploads
  also need hosts `cas-server.xethub.hf.co` and `us.aws.cdn.hf.co` in the
  cloud environment.
- `backend/universe/psx_v1.yaml` does not exist yet, so nothing is mapped to
  tickers (`tickers_mentioned` is empty everywhere).

## Still open

- **GDELT run results** (coverage per month and source, failed weeks).
  Re-run the workflow to retry failed weeks.
- **Wayback recovery** (`news_wayback_backfill`) for GDELT URLs not in our
  data. Not started.
- **PSX permission email**: send it (Maryam or supervisor). *Time-sensitive*
  for Task 5 and for Falah's price data.
- **Task 5 KSE-100 levels**: the PSX portal is blocked by its terms; another
  source is needed.
- **Task 6 FinBERT** and optional **Task 7** coverage check: not started.
- **Supervisor sign-off on full-text storage** (decision 3).
- `docs/data-sources.md` entry for `news_gdelt_index` once the run is done.
- Watch the hourly schedule at minute 23; if runs are still 4–6 h apart,
  trigger the workflow from outside GitHub.
- Back up and snapshot the HF `news/` folder at code freeze (append-only
  RSS archive is the record).

## Next session should

1. Check the GDELT run, pull its manifest into git, write its data-sources
   entry, and report coverage honestly.
2. Build the Wayback recovery job to run from GitHub Actions.
3. Tell Falah about the PSX terms finding, send the permission email, and
   pick a KSE-100 source for Task 5.
