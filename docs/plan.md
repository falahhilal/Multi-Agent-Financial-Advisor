# Project Plan

Multi-Agent Financial Advisor for PSX Retail Investors.

This plan has no dates. It says what gets done, in what order, and what
"done" means at each step. Owners are assigned only for Step 1 (data
collection), because that is where the team starts. Later steps get owners
once Step 1 is under way.

Two views live in this file:

- **Part A, high-level view:** the steps in order, one screen.
- **Part B, detailed view:** every step broken into tasks, with a definition
  of done.

`proposal.md` stays the submitted scope. Accepted ADRs record approved
deviations. `specs.md` and this plan record working decisions and open
questions. `CLAUDE.md` governs Claude Code-specific instructions; root
`AGENTS.md` is Codex's primary instruction file. Shared requirements in both
must stay aligned, and neither tool-specific file overrides these project
sources. If the proposal, an accepted ADR, the specification, or this plan
conflict without a clear resolution, raise the issue rather than treating
`CLAUDE.md` or `AGENTS.md` as the authority.

---

## Corrections to older documents

- **No fixed calendar.** Month-level Gantt and milestone dates are dropped.
- **Data collection is Step 1.** Everything downstream reads from it.
- **Django is dropped** (ADR-0001). Backend is FastAPI + SQLAlchemy + Alembic.
- **Universe is individual PSX stocks plus a `CASH` line** (ADR-0004).
- **Debate proposition is still undefined** (`market_view.py`). It blocks the
  debate and must be settled before any agent is built.
- **"More conservative" is still undefined** as a scalar. It blocks the
  tiebreaker.
- **Ablation runs in `log_only` mode** (ADR-0003).
- **Embedding model is chosen by benchmark** (ADR-0002). FinBERT stays for
  headline sentiment only.
- **FYP-I evaluation needs all five agents and a working front end**, so a
  thin end-to-end slice matters more than polish in any one agent.

---

## Part A. High-level view

```
Step 1   Data collection       three streams in parallel: company, SBP/macro, news and text
Step 2   Foundations           settle blocking decisions, repo skeleton, invariant tests, ethics
Step 3   Retrieval (RAG)       ingest documents, chunk, embed, benchmark, retrieve
Step 4   Quant engine          selection, allocation, metrics (no LLM anywhere)
Step 5   Grounding layer       claim extractor, verifier, enforcement (block | log_only)
Step 6   Agents                Risk Profiler, Macro, Sector, Facilitator, Constructor
Step 7   Orchestration         parallel run, 3-round debate, tiebreaker, audit trail
Step 8   Backend API           FastAPI, Postgres, SSE trace, audit table
Step 9   Frontend              onboarding, allocation, audit trail, developer trace view
Step 10  Documents             SRS, SDS (written against what actually works)
Step 11  Evaluation            ablation, survey, backtest, personalisation
Step 12  Thesis and demo       chapters, limitations, demo
```

Dependencies:

```
Step 1 -> Step 3 -> Step 5 -> Step 6 -> Step 7 -> Step 8 -> Step 9
   |                    ^         ^
   +-> Step 4 ----------+---------+
Step 2 starts alongside Step 1 (its decisions must land before Step 6).
Step 10 runs alongside Steps 6 to 9 and is finalised after the first full run.
Step 11 starts only after a code freeze. Step 12 overlaps Step 11.
```

Three things are time-sensitive for reasons outside our control, so they
start first inside their step:

1. **News archiving** (Step 1, Stream C). Free NewsAPI keeps about a month of
   history. Every day not archived is lost for backtests.
2. **Debate proposition schema** (Step 2). Later work sits downstream.
3. **Survey ethics approval** (Step 2). Lead time at FAST is unknown.

---

## Part B. Detailed view

### Step 1. Data collection

This is where the team starts. Three streams run in parallel, one per person.
Every stream follows the same rules (below). The full list of datasets, with
purpose, format and consuming agent, is in the data catalogue, which becomes
`docs/data-sources.md`.

#### Rules for all streams

1. **Raw data is immutable.** Save exactly what the source returned, before
   any parsing or cleaning, and never overwrite it. Cleaned data is a
   separate output.
2. **Every record carries provenance:** source, URL or endpoint, `fetched_at`,
   and (for macro and news) `published_at`, the date it became public.
3. **A manifest per dataset:** source, fetch date, row or file count, and a
   checksum. Manifests are committed; the data folder is not.
4. **Read the terms before collecting.** Note licence, storage limits and
   automated-access rules in `data-sources.md`. If a source forbids storing
   article text, store headline, link and date only.
5. **One loader script per source**, rerunnable, with rate limiting and retry.
   Libraries such as `psxdata` are loaders, not dependencies: once the data
   is in our store, nothing reads from the library at runtime.
6. **Dates are the point-in-time key.** Nothing is stored in a way that lets a
   later step see data from after its simulated date.
7. **Gaps are recorded, not hidden.** Missing days, failed fetches and
   unavailable history go in a coverage log that the thesis later cites.
8. **No user data in any dataset.** Synthetic profiles only.

#### Shared setup (completed 2026-11-22 for company Stream A)

The company-stream setup is implemented in `data/README.md`, `backend/jobs/company_schemas.py`, `backend/jobs/company_as_of.py`, `backend/jobs/company_pypsx.py`, and `backend/data_manifests/`. This does not mark collection complete or authorize a fetch by itself.

1. Company Stream A layout and shared conventions are documented; raw snapshots remain immutable and normalized tabular datasets use Parquet.
2. Company schemas, provenance, manifest contract, and required `as_of` filters are implemented with offline tests. Missing event/fundamental availability is excluded.
3. Team reports written permission for intended PSX/pyPSX historical-price, corporate-action and company-fundamentals collection and private HF sharing; this does not grant unrelated rights.
4. Local `data/` is gitignored. Operational backup/access procedures for future runs remain open.

#### Stream A. Company data

Covers everything about the listed companies and their prices.

1. **Freeze universe v1 — company selection complete (2026-11-22).**
   `backend/universe/psx_v1.yaml` contains the 18 selected tickers, sector,
   board lot, `listed_from`, `listed_to`, data-availability fields, and `CASH`.
   FATIMA replaces ENGRO and NCL replaces ILP. ENGRO's historical identity is
   retained separately; its exact delisting date and other unverified metadata
   remain null. See `backend/universe/CHANGELOG.md`.
2. **KSE-100 constituent history.** Find out whether history exists or only the
   current snapshot. This decides how honestly survivorship bias can be
   handled.
3. **Daily OHLCV — initial collection run 2026-11-22.** pyPSX returned
   price history for all 18 active tickers (2015-01-01–2026-11-20; SYS begins
   2015-02-06). Of 53,012 source rows, 52,950 passed normalization; 62
   inconsistent OHLC rows were excluded and reported. The private HF upload
   was verified. Prices are therefore partial pending review and remaining
   quality policies. Do not use `psxdata` or Yahoo as a fallback without
   source-term and access review.
4. **Corporate actions — initial pyPSX collection 2026-11-22.** 386 rows were
   archived and uploaded for the 18 active tickers (cash dividends, bonus,
   split, rights and other event types). The response has ex-dates but no
   verified publication/availability dates, so actions are excluded from PIT
   reads and do not validate adjusted prices.
5. **Fundamentals — initial pyPSX collection 2026-11-22.** 13,379 annual
   statement-item rows were archived and uploaded, covering observed years
   2015–2025/2026 by ticker. Publication dates, units and currency are absent;
   do not use these rows for PIT backtests or derive historical metrics.
   Select a source that provides point-in-time publication dates before
   fundamentals can be treated as complete.
6. **Sector map and sector summary series:** one sector label per ticker.
7. **Data quality report per ticker:** missing days, halts, suspensions, stale
   prices, outliers. This decides each ticker's data-availability flag.

Done when: every ticker has a quality report, the fundamentals question is
answered, and prices read back by `as_of` return only data available then. The
2026-11-22 run produced the ticker reports and passed as-of regression tests,
but Stream A remains open: OHLC exceptions need review; action publication
dates and event completeness, point-in-time fundamentals, approved session
calendar, adjusted-price methodology and several quality policies are
unresolved.

#### Stream B. SBP and macro data

Covers everything about the Pakistan economy and monetary policy.

1. **SBP monetary policy statements** as PDFs, enough of them to cover the
   retrieval corpus and the backtest windows.
2. **Policy rate history** as a clean series with announcement and effective
   dates.
3. **T-bill auction results** so `CASH` earns a real return and the fixed-rule
   baseline is credible.
4. **Inflation (CPI)** from SBP and PBS releases.
5. **FX (PKR/USD)** daily or monthly series.
6. **Reserves and money supply** from SBP releases.
7. **World Bank indicators** for Pakistan through `wbgapi`.
8. **US external drivers from their original publishers, not FRED** (FRED's
   terms forbid storing or archiving its content; Proposed ADR-0006): the
   effective federal funds rate from the New York Fed Markets Data API, the
   daily Treasury par yield curve (all maturities) from the US Treasury XML
   feed, and the nominal broad, AFE and EME dollar indexes from the Federal
   Reserve Board H.10 release XML. Brent comes from the World Bank Pink Sheet
   in the World Bank loader (item 7), not from EIA.
9. **PDF parsing test:** run `pdfplumber` over each SBP document type. Note
   which pages are scanned and need an OCR fallback. Keep page numbers and
   section headings, because citations need them.
10. **Publication-date tagging:** every value carries the date it became
    public, not only the period it describes.

Done when: policy rate, T-bill yield, CPI and FX are clean series with
publication dates, and SBP PDFs parse to text with page references.

#### Stream C. News, filings and market index

Covers text sources and the benchmark index.

1. **Start the daily news archive job first.** A scheduled job writing raw
   JSON to `data/raw/news/YYYY-MM-DD/`. Parsing comes later. Cost grows with
   every day it waits. The job needs a machine that stays on.
2. **Read NewsAPI terms** on storage and commercial use.
3. **Add sources beyond NewsAPI:** RSS feeds from Pakistani business outlets
   and the PSX and SBP announcement pages, so coverage does not hang on one
   provider.
4. **Query design:** keywords per sector, per ticker and per macro topic,
   kept in a versioned file.
5. **Normalise and deduplicate** into one record format. Syndicated copies
   collapse into one.
6. **Tagging:** map each headline to tickers, sectors or macro topics.
7. **FinBERT sentiment** on headlines (positive, neutral, negative with
   scores), with a hand spot-check of a sample.
8. **PSX company filings and announcements** as PDFs for the retrieval corpus,
   for the tickers in the universe.
9. **KSE-100 index daily levels**, used as the backtest benchmark.
10. **Coverage log:** articles per day and per sector, so gaps are visible.
11. **Fallback decision:** if archived history is too thin for the backtest
    windows, disable the news channel in backtests and disclose it.

Done when: archiving runs unattended and has run for a week, headlines are
deduplicated and tagged, FinBERT labels pass the spot-check, filings are
collected for every universe ticker, and the index series is clean.

#### Who does what, and why

| Person | Takes | Why |
|---|---|---|
| Falah | Stream A: company data | Heaviest cleaning work (prices, corporate actions, fundamentals, quality). Keeps no shared infrastructure so the load stays even |
| Mahrukh | Stream B: SBP and macro data, including World Bank and FRED | Several small series plus the SBP PDF parsing. They share one theme, which makes review easier |
| Maryam | Stream C: news, filings, KSE-100 index, plus the shared setup and storage design | News becomes mostly automated once the archive runs, which frees time for the shared setup and the filings |

If Falah's fundamentals task turns into filing scraping, move the index series
and the filings collection to whoever has the lightest load at that point.

**Step 1 done when:** all three streams meet their conditions, every dataset
has a manifest, and `docs/data-sources.md` has an entry per source.

---

### Step 2. Foundations

Starts alongside Step 1. Goal: remove everything that would force rework.

**2.1 Decisions to settle (each becomes an ADR or a short note)**
- Debate proposition: what Macro and Sector both commit to. Recommended: a
  target tilt across sector buckets plus one equity-exposure score, each line
  with citations and a confidence. Output: `schemas/market_view.py`.
- "More conservative" as one scalar. Candidates: lowest equity %, highest
  cash, lowest ex-ante volatility from `quant/`. Pick one, define it in code.
- Allocate fresh or rebalance from `existing_holdings`. Either the Constructor
  uses it or onboarding stops collecting it.
- Single-session demo or logins.
- Per-cycle API cost ceiling, as a number.
- Who annotates claims for the manual 15% sample (two of three people).
- Four backtest windows and the benchmark.
- Ablation backbone: recheck current cheap-tier model options before fixing
  one.
- Orchestration: LangGraph or plain typed Python for the debate state machine.

**2.2 Repository skeleton:** monorepo layout from `specs.md` section 5,
docker-compose with Postgres and Chroma, `pyproject.toml`, `.env.example`,
ruff, mypy, pytest, CI.

**2.3 Invariant tests written first** (they fail until the code exists):
isolation, determinism, grounding, universe.

**2.4 Open the survey ethics process.** Find out what FAST requires and start.

**Done when:** decisions are answered or deferred in writing, `market_view.py`
exists and all three members have reviewed it, repo clones and
`docker compose up` works, CI is green except the four invariant suites.

---

### Step 3. Retrieval (RAG)

1. **Corpus:** SBP statements, filings, World Bank notes, archived news. No
   user profile ever enters the vector store.
2. **Ingestion and chunking** with LlamaIndex. Keep source, page and date as
   metadata on every chunk. Choose chunk size and overlap by testing.
3. **Embedding interface:** an `EmbeddingProvider` base class so the model
   swaps in one line.
4. **Labelled query set:** hand-label PSX and SBP questions with the passages
   that answer them. Build it while reading the documents.
5. **Benchmark:** FinBERT versus a dedicated sentence-embedding model on
   recall@k and MRR. Result goes into ADR-0002.
6. **Retrievers** with point-in-time filtering by `as_of` and filters by
   source type.
7. **Offline ingestion:** parsing, embedding and FinBERT batches run in jobs,
   never inside a request.

Done when: a query returns citable passages with source and page, the
benchmark is committed, and the retriever never returns anything dated after
`as_of`.

---

### Step 4. Quant engine (deterministic, no LLM imports)

1. `universe.py`: load and validate the ticker file.
2. `selection.py`: which tickers inside a favoured sector, by a documented
   rule.
3. `allocation.py`: tilts to per-ticker weights, then hard caps (max per name,
   max per sector, `cash >= min_liquidity_pct`, `equity <= max_equity_pct`).
4. `constraint_solver.py`: used only when simple normalisation cannot satisfy
   all caps. Measure how often.
5. `metrics.py`: return, volatility, Sharpe, drawdown, correlation.
6. The "more conservative" scalar from Step 2.
7. Board-lot rounding and minimum order handling.
8. Unit tests with no network and no model, including property tests:
   weights sum to 1, no cap violated, same input gives same output.

Done when: given a tilt and a constraints object, the module returns weights
plus cash that satisfy every cap, identically every run.

---

### Step 5. Grounding layer

1. Claim extractor: pull atomic claims from agent output.
2. Claim types: numeric and source-attributed claims are hard-blocked;
   qualitative reasoning is logged and flagged (FR-06a and FR-06b).
3. Verifier: match each claim to retrieved passages.
4. Enforcement mode flag (`block` or `log_only`) in config and in every run's
   metadata from the first commit.
5. Fixture set with known answers, so precision and recall are measured before
   anything depends on them.
6. Fake-agent harness so the blocker is tested before real agents exist.

Done when: precision and recall on fixtures are recorded, every block is
logged, and blocked claims never reach output in `block` mode.

---

### Step 6. Agents

Agents exchange validated Pydantic objects, never free text. Prompts are
versioned markdown files, one per agent.

1. **Risk Profiler:** the only agent that sees the profile. Emits the five
   constraint fields. Needs questionnaire design and an investor taxonomy.
2. **Macro Agent:** profile-blind. Emits a `MarketView` with citations and
   confidence.
3. **Sector Analyst:** profile-blind. Emits a `MarketView` with citations and
   confidence.
4. **Debate Facilitator:** holds constraints as hard limits and never quotes
   them to Macro or Sector.
5. **Portfolio Constructor:** combines verdict and constraints, calls `quant/`
   for every number, writes the explanation.
6. Per agent: prompts, output schema, citation validation, error handling,
   contract tests.

Done when: each agent passes its contract tests alone and the isolation suite
passes with the real agents.

---

### Step 7. Orchestration

1. Typed run state with narrowed per-agent views. Macro and Sector get a view
   type with no profile field.
2. Flow: Profiler, then Macro and Sector in parallel, then debate, then
   Constructor.
3. Debate: Round 1 positions, Round 2 forced rebuttals, Round 3 final
   positions with new confidence.
4. Violation handling: an agent that restates instead of rebutting is logged
   and re-prompted once.
5. Deterministic tiebreaker using the "more conservative" scalar.
6. Kill switch with a safe default output.
7. Audit trail: plain-English, agent-attributed, with citations.
8. LangSmith tracing from the first LLM call.
9. Latency pass: 20 cycles, record p50 and p95, find where time goes.

Done when: one synthetic profile yields a full recommendation from real
retrieved sources, every number traces to `quant/`, and measured latency is
written down.

---

### Step 8. Backend API

1. SQLAlchemy models and Alembic migrations.
2. Endpoints: onboarding, recommendations, audit, runs, SSE trace.
3. Append-only audit table: revoked UPDATE and DELETE grants plus a per-row
   hash chain.
4. Profile storage with consent and retention rules.
5. Caching and cost-tiered model routing, after measurement.
6. OpenAPI spec, from which frontend types are generated.

---

### Step 9. Frontend

1. Onboarding form with validation.
2. Allocation table with badges showing which cap bound the result.
3. Audit trail with collapsible steps and citation popovers.
4. Developer view, separate from the investor screens: live agent trace and
   run inspector.
5. Educational-use disclaimer on every recommendation screen.
6. Desktop and mobile layouts.
7. Visual direction follows the agreed prototype brief.

---

### Step 10. Documents

Written against what exists, alongside Steps 6 to 9.

- **SRS:** FR-01 to FR-13 as corrected (FR-06 split, FR-09 per-ticker plus
  cash), plus new requirements for the disclaimer, `existing_holdings` and
  data retention. Each requirement maps to a test.
- **SDS:** architecture, module design, schemas lifted from code, sequence
  diagrams (full cycle, debate with tiebreaker branch), data model, RAG
  parameters, deployment, and how each invariant is enforced.
- **Supporting docs:** `data-sources.md`, `agent-contracts.md`,
  `evaluation-plan.md`, ADRs 0001 to 0004, a worklog per session.

---

### Step 11. Evaluation

**Code freeze first.** After it, any prompt, retriever, chunking, embedding or
schema change invalidates finished runs. Tag the commit.

1. **Ablation (primary contribution):** full debate, single LLM, voting, fixed
   rule (60% equal-weighted basket, 40% cash). All three LLM setups on one
   backbone in `log_only` mode. Measure unmatched-claim rate, score
   explanation quality on a blind rubric, annotate a 15% sample with two
   annotators and report Cohen's kappa. Report blocked-claim rate separately.
2. **Explainability survey:** 20 non-finance participants, within-subject
   design, comprehension questions plus Flesch-Kincaid scoring.
3. **Backtest:** four named windows, walk-forward, point-in-time via `as_of`,
   survivorship handled by listing dates. State the two threats to validity:
   backbone training data covers the windows, and news coverage is limited to
   what we archived.
4. **Personalisation:** four profiles across several market snapshots. Report
   pairwise distance and monotonicity.

Every result file records git SHA, model version, embedding model, universe
version and configuration.

---

### Step 12. Thesis and demo

Chapters: Introduction, Literature Review, Methodology, Data, Evaluation,
Discussion, Conclusion. Chapters that do not depend on results can be drafted
early. The Data chapter draws directly on `data-sources.md` and the coverage
logs from Step 1. The Discussion chapter draws on the ADRs and states
limitations plainly: training-data contamination, news coverage, FinBERT
substitution, English only, no SECP licence.

---

## Risks and what to do about them

| Risk | Response |
|---|---|
| News history too thin for backtests | Archive from day one, otherwise disable the news channel and disclose |
| Debate agents talk past each other | `market_view.py` gives them something comparable to argue over |
| Claim verifier false positives in `block` mode | Measure on fixtures first; blocking limited to numeric and attributed claims |
| `psxdata` or a site changes and breaks | Ingest once into our store; library is only a loader |
| Fundamentals only exist at sector level | Decide early whether ticker-level P/E is dropped or scraped |
| Scanned SBP PDFs parse badly | Test parsing in Step 1; OCR fallback |
| A source forbids storing its content | Store headline, link and date only; record it in `data-sources.md` |
| Pipeline too slow | Offline ingestion, parallel Macro and Sector, caching, cheaper models |
| Ethics approval delays survey | Start in Step 2 |
| Late change after freeze | Tag freeze commit; re-run cost is explicit |
| API costs overrun | Set a number in Step 2; LangSmith cost tracking from first call |
