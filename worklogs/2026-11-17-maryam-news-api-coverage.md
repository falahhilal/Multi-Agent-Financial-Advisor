# Session 2026-11-17 — NewsAPI / GNews coverage check (Task 7)

**Who:** Maryam Asif
**Span:** 2026-11-17 to 2026-11-20 (queries run 2026-11-20)

Continues [2026-11-16-maryam-gdelt-fulltext.md](2026-11-16-maryam-gdelt-fulltext.md).
Task 7 asked whether a one-off GNews trial backfill is worth it. Nothing was
built and no dataset was created; this worklog is the only output.

> NewsAPI's terms forbid publicly releasing "data or usage statistics or
> other information regarding the Data" without permission. The NewsAPI
> numbers below are for the team only. Do not quote them in the thesis
> without NewsAPI's permission.

## Decisions taken

1. **Recommended: no GNews trial backfill** (for Maryam to confirm). GNews
   returned no articles from Dawn, Tribune or Business Recorder for our
   queries, so a trial would not fill the Business Recorder 2018–2022 gap.
2. **Recommended: NewsAPI is not used** (for Maryam to confirm). Its free
   plan returned no Pakistani business news for these queries.
3. **Keyed API use despite robots.txt** (Maryam gave the keys after being
   told). Both sites' robots.txt files disallow the API paths (`/v2/`,
   `/api`) for general crawlers. These are documented APIs meant to be
   called with a key, and their terms allow this use. Recorded so the call
   is visible.

## Work done

All by Maryam.

- Terms read **before** the first API call: newsapi.org/terms and /pricing,
  gnews.io/terms and /pricing, the GNews docs (authentication, article
  availability), and both robots.txt files.
- API keys saved as network secrets on the cloud environment (sent as an
  `X-Api-Key` header by the proxy), never in chat, code or `.env` in git.
- 12 requests in total (NewsAPI 3, GNews 9), 3 s apart. Queries `"PSX"`,
  `"KSE-100"`, `"Pakistan Stock Exchange"`, English, last 30 days
  (2026-10-22 to 2026-11-20). Raw responses kept only in the session
  scratchpad, not on HF or in git.
- Compared against our RSS archive (`news/rss/articles/`, 3,011 rows) for
  the same window and phrases (title, summary or text).

### Free-plan limits (from the pricing pages, 2026-11-20)

| | NewsAPI Developer | GNews Free |
|---|---|---|
| Use | development and testing only | personal, academic, non-commercial |
| Requests | 100 a day | 100 a day |
| History | 1 month | 30 days |
| Delay | 24 h | 12 h |
| Per request | 100 | 10, text cut to 250 characters |
| Paid history | (all articles, $449/month) | since 2020 (from €49.99/month; 10-day trial needs a card) |

### Results (last 30 days)

| Source | Articles returned | On topic | Sites |
|---|---|---|---|
| NewsAPI | 21 unique | 1 or 2 | Unity Asset Store (12, "PSX" = PlayStation-style game assets), PyPI (4), GitHub, Crypto Briefing, Biztoc, CNA (India/Pakistan markets), Zaviya.pk |
| GNews | 9 unique (7 + 3 + 5 by query) | 7 | The Nation (5), Lokmat Times, Europe Says; Oilprice and WTOP are about Phillips 66 (NYSE ticker PSX) |
| **Our RSS archive** | **106 unique** (since 2026-11-08 only) | all | Business Recorder, Tribune, The Financial Daily, Dawn, ProPakistani, Pakistan & Gulf Economist |

By phrase, our RSS archive has 75 (`PSX`), 60 (`KSE-100`) and 79
(`Pakistan Stock Exchange`) articles, in about 12 days of collection,
against GNews's 7, 3 and 5 in 30 days.

GNews reports 963, 711 and 645 matches across its whole archive, which
starts in 2020; the free plan only returns the last 30 days.

## Problems found

- **Neither API indexes Dawn, Tribune or Business Recorder** for these
  queries, although those sites published 100+ matching articles in the
  window. So neither can fill the GDELT gaps (no BR text 2018–early 2023).
- "PSX" is ambiguous on general APIs (PlayStation, Phillips 66).
- **The Nation** (nation.com.pk) is the only Pakistani outlet GNews found
  that we don't collect. Worth checking its RSS feed and terms as a
  possible eighth feed.

## Still open

- Whether to add The Nation's RSS feed (check terms and robots.txt first).
- From earlier sessions: ADR-0005 team decision, PSX permission email,
  supervisor sign-off on full text, Wayback pass for 28 gone links.

## Next session should

1. Decide on The Nation as a new feed; if yes, read its terms first.
2. Chase the ADR-0005 decision and the PSX email.
