# Session 2026-11-12 — macro_policy_rate loader (SBP EasyData)

**Who:** Mahrukh Shariq
**Span:** 2026-11-12

## Decisions taken

- Dataset id `macro_policy_rate`: `STREAM="macro"`, `DATASET="policy_rate"`,
  following the `news` + `rss_articles` pattern (Mahrukh).
- `backend/jobs/common.py` is **not** changed. Key protection lives inside the
  macro job (Mahrukh). See the `redact_url()` proposal below.
- `published_at`: option A. Null, with `published_at_basis = "unknown"`;
  `observation_date`, `fetched_at` and the portal last-refresh date are stored
  instead. Option E (an MPC announcement/effective-date calendar) will be a
  separate follow-up dataset (Mahrukh). Recorded as a known gap in
  `docs/data-sources.md`. This does not override the proposal, but it leaves
  Stream B item 10 (publication-date tagging) open for this series.
- `fetch_mode = append_only_snapshot`, with revision warnings between runs,
  because SBP may revise or remove data without notice (Mahrukh).
- The three series stay separate in long format. `is_policy_rate` marks the
  official policy rate: the Reverse Repo from 2009-08-17 to 2015-05-24 and the
  Target Rate from 2015-05-25, per the dataset description (Mahrukh).
- The job uses only `easydata.sbp.org.pk`. `sbp.org.pk` blocks automated
  requests and is not a source (Mahrukh).

## Work done

All by Mahrukh, not yet committed:

- `backend/jobs/macro_policy_rate.py`, the loader. It:
  - fetches `/meta` for all three series first and aborts on a name mismatch
    or a missing start date;
  - fetches `/data` from each series' own metadata start date;
  - saves raw responses unmodified (`open("xb")`);
  - writes interim parquet, then validates (series key and name per row,
    dates, numeric values, duplicates, more than one row per series) and
    writes processed parquet;
  - writes a revision diff against the previous run, a run log and the
    manifest, and uploads to HF `macro/policy_rate/`;
  - has a `--no-upload` flag.
- Key protection in the job:
  - HTTP errors are re-raised with the key replaced by `REDACTED`, raised
    outside the `except` block with `from None`, so no exception chain
    carries the URL;
  - a logging filter on `jobs.common` and the job's own logger;
  - `resp.url` is never stored;
  - a canary scan of every response body, the interim and processed rows,
    the run log, the warnings, the manifest fields and the written manifest.
    The run stops if the key appears anywhere.
- `backend/tests/jobs/test_macro_policy_rate.py`: 39 offline tests.
  - All HTTP goes through a fake `_fetch_once`, and a dummy canary key is used.
  - Coverage: request order and params, the metadata and data aborts, raw
    immutability, upload paths, `--no-upload`, policy-flag windows,
    date/value parsing, response layouts, revision warnings, and key leaks
    (404, 403, 500 with retries, a network error, a key echoed in the body,
    the success path).
  - Mutation check: with the log filter disabled, the leak tests fail as
    expected.
  - `pytest tests/jobs`: 83 passed.
- `docs/data-sources.md`: new section "SBP & macro stream", entry
  `macro_policy_rate`.
- `.env.example`: added `EASYDATA_API_KEY=`.
- First real run with `--no-upload` on 2026-11-12 returned 194 rows (97
  Reverse Repo, 57 Repo, 40 Policy Target), no warnings; values for 2024-12-17
  to 2026-10-26 match the EasyData portal for all three series.
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

Datasets touched: `macro_policy_rate` (local `--no-upload` run only, nothing
uploaded to HF yet).
- Source: SBP EasyData API, three series (Policy Target, Repo, Reverse Repo).
- Rows: 194 (97 Reverse Repo from 1956-01-01, 57 Repo from 2009-08-17, 40
  Policy Target from 2015-05-25), last observation 2026-10-26, portal last
  refresh 2026-10-28.
- Gaps and differences against the OVR Repo History PDF (see Work done above):
  Reverse Repo missing 1991-03-25 (and 1991-02-26, a repeat); extra
  2020-04-16 and 2020-05-16 in all three series; `published_at` null.

## Problems found

- **`http_get` leaks query-string secrets.** The full URL, including
  `api_key`, appears in:
  - the `HttpError` message and `.url`;
  - the retry `log.warning`;
  - `RobotsDisallowedError`;
  - the https→http redirect-refusal message;
  - `HttpResponse.url`.

  A job that copies `str(exc)` into its warnings, as `news_rss.py` does,
  would write the key into a committed manifest. The macro job works around
  this locally.
- The EasyData metadata field names were confirmed on the first real run on
  2026-11-12: `/meta` uses "Available Since", "Available Upto", "Last Refresh
  Date" and "Reference Dataset Name". The parser was fixed and tests went from
  83 to 86.
- It is not known whether `observation_date` is the announcement date or the
  effective date. This matters for look-ahead. The OVR Repo History PDF
  labels its dates "with effect from" and EasyData's dates equal the PDF's
  except for two extra 2020 dates (see Work done), so the dates are mostly
  effective dates but not consistently.
- No formatter or linter (black, ruff, mypy) is installed in the venv, so
  the new code has not been run through them.

### Proposal for Falah and Maryam: `redact_url()` in `common.py` (separate PR)

Add `redact_url(url)` to `common.py`. It replaces the values of the query
parameters `api_key`, `apikey`, `key`, `token` and `access_token` with
`REDACTED`. Apply it in:
- `HttpError` (both the message and `.url`);
- the retry `log.warning`;
- `RobotsDisallowedError`;
- the redirect-refusal error;
- `HttpResponse.url`.

URLs without those parameters behave exactly as now, so `news_rss.py` is
unaffected. Add tests to `test_common.py` using the existing local-server
fixture.

Once merged, the macro job keeps its canary scan as a second layer but can
drop its own log filter.

## Still open

- Real uploaded run, commit and PR once HF write access is upgraded.
- Check the 2020-04-16 and 2020-05-16 extra rows against the MPC statements,
  and hand-check a sample of other observation dates there to settle
  announcement vs effective date.
- MPC announcement/effective-date calendar dataset (option E).
- Raise the `redact_url()` proposal with Falah and Maryam.
- Get supervisor confirmation of the EasyData handling (private, academic,
  never redistributed), as for news.

## Next session should

1. Commit on `data/macro`, do the real uploaded run from a clean tree, commit
   the manifest, and open a PR.
2. Start the MPC calendar dataset, or move on to T-bill auction results
   (Stream B item 3).
