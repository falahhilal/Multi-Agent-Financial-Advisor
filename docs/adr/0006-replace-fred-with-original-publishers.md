# ADR-0006: Replace FRED with the original publishers of US macro series

- **Status:** Proposed (needs agreement from Falah and Maryam)
- **Date:** 2026-11-23
- **Proposed by:** Mahrukh Shariq (SBP and macro stream owner)
- **Overrides:** proposal §5 Step 3 ("Macro Agent: SBP PDFs, World Bank API,
  FRED, news feeds"), the §6 data and tool tables (FRED as a source and a
  tool), the Macro Agent row of the agents table (FRED as an input), and the
  data-pipeline timeline rows that name FRED.
- **Does not change:** which US series the Macro Agent uses as external
  drivers (US policy rate, US long rates, the dollar), Rule 6 (point-in-time
  `published_at`), or the World Bank source.

## Context

The proposal lists FRED (Federal Reserve Bank of St. Louis) as the source of
the US series the Macro Agent uses as external drivers: US rates, oil and the
dollar index.

On 2026-11-22 Mahrukh read the FRED Services Terms of Use (see
[worklog](../../worklogs/2026-11-22-mahrukh-reserves-money-supply.md)). The API
prohibitions forbid using the API in connection with storing, caching or
archiving FRED content, incorporating it in any database, and developing or
training software systems or machine learning. Every macro loader stores raw
snapshots and processed files in the team's private Hugging Face dataset, as
required by the data-collection rules (raw is immutable, a manifest per
dataset, snapshots of sources that cannot be re-fetched). A FRED loader would
break those terms.

FRED republishes series that other institutions produce. Those publishers'
own terms were read on 2026-11-23:

- **Federal Reserve Bank of New York** (newyorkfed.org/privacy/termsofuse):
  a licence "to use, copy, and distribute Content for your personal or
  business purposes" and to "Download, store, and use Content in any format or
  media". Automated access is allowed as long as it does not interfere with the
  website. Anyone republishing a reference rate must show a notice ("The
  [DATA NAME] is subject to the Terms of Use posted at newyorkfed.org") and a
  statement that the New York Fed does not endorse the republication.
- **US Treasury** (home.treasury.gov): no copyright or reuse statement was
  found on the site-policy, privacy, Interest Rate XML or Treasury Open Data
  pages. The basis for use is that works of US federal employees have no US
  copyright (17 U.S.C. §105). The site's robots.txt allows everything. This
  is a weaker basis than an explicit licence and is recorded as such.
- **Federal Reserve Board** (federalreserve.gov/disclaimer.htm): information
  on the site "is in the public domain and may be copied and distributed
  without permission" unless otherwise indicated, and "Please cite to the
  Board as the source of the information." No third-party restriction is
  noted on the H.10 release pages.

The Board is retiring its Data Download Program: "Build Your Package" was
removed on 12 November 2026, and the `datadownload/` URLs stop working after
31 March 2027. The Board names FRED, and each release's XML file, as the
replacements. We use the release XML.

## Decision

Do not use FRED. Collect each US series from the institution that produces it:

| Series | Publisher and access | Loader (written 2026-11-23, not yet run) | Dataset |
|---|---|---|---|
| Effective federal funds rate (EFFR) | New York Fed Markets Data API, `markets.newyorkfed.org/api/rates/unsecured/effr/...`, no key | `backend/jobs/macro_us_effr.py` | `us_effr` |
| Daily Treasury par yield curve, all maturities (10-year required) | US Treasury XML feed, `home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=YYYY`, no key | `backend/jobs/macro_us_treasury_yields.py` | `us_treasury_yields` |
| Nominal broad, advanced foreign economies (AFE) and emerging market economies (EME) US dollar indexes | Federal Reserve Board H.10 release XML zip, `federalreserve.gov/releases/h10/data/FRB_h10_xml.zip` (confirmed by a manual download from the "XML" link on the H.10 current release page, 2026-11-23), no key | `backend/jobs/macro_us_dollar_index.py` | `us_dollar_index` |
| Brent crude oil | World Bank Commodity Price Data (Pink Sheet), in the World Bank loader | not in this ADR's loaders | later |

Brent does not come from the US Energy Information Administration (EIA),
because EIA republishes Refinitiv data with permission. The Pink Sheet's terms
are to be checked when the World Bank loader is planned.

All three US loaders follow the existing macro pattern: unmodified raw
responses, a parsed interim file, a validated processed file, a run log, a
manifest, `fetch_mode = append_only_snapshot`, and revision warnings against
the previous run. `published_at` is the US release date plus a fixed number of
days at 23:59:59 Asia/Karachi, stored in UTC. US date + 1 day is always later
than any time on that US date, whether or not US daylight saving time is in
effect, so no DST table is needed. The exact rule for each series is
documented in `docs/data-sources.md`.

## Consequences

**Simpler or better**
- Every stored series comes from a publisher whose terms allow storing it.
- Provenance is direct: the thesis cites the producing institution, not an
  aggregator.
- No API keys, so there is no secret handling for these loaders.

**Costs and risks**
- Three loaders and formats (JSON, Atom/OData XML, SDMX-style XML in a zip)
  instead of one FRED client.
- The H.10 zip is overwritten at each release and has no archive. Our raw
  snapshots are the only record of past vintages, and history before our
  first run reflects the latest weights (the indexes are revised when the
  trade weights change, about once a year).
- Treasury's basis for reuse is the absence of US copyright, not an explicit
  licence. If the team wants certainty, ask Treasury
  (debt.management@treasury.gov).
- If our system shows EFFR to users, every surface that shows it must carry
  the New York Fed reference-rate notice.
- The proposal named FRED. The thesis must say it was replaced and why
  (Chapter 6, deviations).

**Not affected**
- The SBP and World Bank datasets.
- RQ1 to RQ3 and the ablation: all configurations still receive the same
  retrieved context.

## What changes if accepted

Changed already, with this ADR (2026-11-23):

- `docs/plan.md` Stream B item 8: FRED replaced by the original publishers.

To change only after the team accepts this ADR:

1. `CLAUDE.md` and `AGENTS.md`, kept aligned:
   - the Macro row of the agents table ("SBP PDFs, World Bank, FRED, news");
   - the Data paragraph of the tech stack ("SBP PDFs via pdfplumber, World
     Bank via wbgapi, FRED, NewsAPI are planned sources").
2. `docs/specs.md`:
   - the Macro Agent row of the agents table ("SBP monetary policy PDFs,
     World Bank API, FRED, news");
   - the planned module tree (`fred.py` under the data-source modules).
3. `docs/plan.md` "Who does what" table: "Stream B: SBP and macro data,
   including World Bank and FRED".
4. `backend/requirements.txt`: remove `fredapi>=0.5`. No loader uses it.
   **Ask the team before removing it.**
5. `docs/proposal.md` is the approved proposal and is not edited. This ADR
   records the deviation.

## Alternatives considered

- **Use FRED anyway, without storing anything.** Rejected: the data-collection
  rules require raw snapshots and manifests, and the FRED terms also exclude
  use in developing software systems.
- **Use the Board's Data Download Program for H.10.** Rejected: it is being
  retired, and its URLs stop working after 31 March 2027.
- **Use EIA for Brent.** Rejected: EIA republishes Refinitiv data with
  permission, so EIA's terms do not settle our right to store it.
- **Drop the US series.** Rejected: US rates and the dollar are among the
  external drivers in the plan, and these publishers allow storage.
