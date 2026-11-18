# CLAUDE.md — Multi-Agent Financial Advisor for PSX Retail Investors

FYP, FAST National University Karachi, session FALL 2026. Team: Falah Hilal
(lead), Mahrukh Shariq, Maryam Asif. Full proposal: `docs/proposal.md`.
Working specification and open questions: `docs/specs.md`.
Order of work and definitions of done: `docs/plan.md`.
Decisions that override the proposal are in `docs/adr/`. Read these before
changing architecture.

## Tool-specific instructions and project authority

Claude Code uses `CLAUDE.md` for its repository instructions. Codex uses the
root `AGENTS.md` as its primary repository instruction file; it must not rely
on `CLAUDE.md` as its primary instructions. Shared project requirements in
these files must stay aligned. The project documents govern shared decisions:
the proposal defines approved scope, accepted ADRs record approved deviations,
and `docs/specs.md` plus `docs/plan.md` record working decisions and open
questions. A tool-specific instruction file does not override those project
sources or the other tool's specific instructions. If the project documents
leave a conflict unresolved, raise it instead of choosing an instruction file
as the authority.

This is a **research** project before it is a product. The deliverable that
matters most is a controlled ablation measuring hallucination rate and
explanation quality. Code that makes the demo prettier but the experiment
weaker is a net loss.

---

## The ten rules that must never be broken

Architectural invariants, not style preferences. Where the relevant code and
tests exist, these invariants must remain covered and their tests must not be
skipped or xfailed. For planned components, add the corresponding tests with
the implementation. If a change requires breaking an invariant, stop and
raise it with the team.

### 1. Only the Risk Profiler reads the investor profile

The Macro Agent and the Sector Analyst Agent **never** see the investor's
profile, in any form, direct or paraphrased. They analyse the market, full
stop. The Debate Facilitator and the Portfolio Constructor see the derived
**constraints object only** — `risk_level`, `max_equity_pct`,
`min_liquidity_pct`, `time_horizon`, `behavioral_flags` — never the raw
profile, never the questionnaire answers, never the capital amount.

Enforcement:
- When implemented, construct `InvestorProfile` only in the onboarding
  path and the Risk Profiler. No other module may import it.
- Agents receive narrowed views from `orchestration/state.py`.
  `MarketAgentView` has no profile field and no constraints field.
- The profile is never embedded, never written to the vector store, never
  placed in a prompt template reachable by a market agent.
- **Facilitator messages to Macro or Sector in Rounds 2 and 3 must be
  constraint-free.** Quoting a constraint back at a market agent breaks this
  rule even though no object moved. Pass rebuttal targets only.
- Add and maintain `tests/isolation/` for these boundaries when the agent
  implementation is introduced. The invariant tests are mandatory and must
  never be skipped or xfailed.

Why: the proposal's claim is that market analysis is unbiased by risk
appetite (FR-03). If the profile leaks, RQ3 is unanswerable.

### 2. Unsupported claims are blocked and logged

When the planned claim-enforcement layer is implemented, every claim in
agent output is extracted, matched against that run's retrieved passages, and
recorded in the claim registry with its verdict. A claim with no matching
passage is **blocked** — it does not reach the user —
and the block is **logged** with agent, round, claim text, timestamp and
model version.

Scope, because the proposal is ambiguous (FR-06 says "quantitative", §5 says
"any numerical or factual statement"):
- **Hard-block**: numeric claims, and factual claims attributed to a named
  source — a policy rate, a P/E, an earnings figure, "SBP raised…", "the
  filing states…".
- **Log and flag, do not block**: qualitative reasoning and hedged language.
  Blocking these produces false positives that destroy usable output.
- State this split in the thesis. Do not leave it implicit.

The enforcement layer has two modes, `block` and `log_only`, set in config
and recorded in run metadata. `block` is production. `log_only` is the
ablation (see Rule 4). Never silently drop a blocked claim. Never retry an
agent until something passes. Never widen the matcher to make a demo work.

### 3. Derived numbers come from code, never from a model

Per-ticker weights, cash weight, Sharpe ratio, volatility, drawdown,
correlations, expected returns, ticker selection — all produced by
`app/quant/`. The LLM writes prose *about* numbers the module returned, and
may not state a figure the module did not return.

When `app/quant/` is implemented, it must not import any LLM client,
LangChain, or any agent module. Add and maintain an import-graph test under
the determinism tests to enforce this. (NFR-08.)

### 4. The ablation measures architecture, not plumbing

All three LLM configurations run on the same fixed backbone, the same
retrieved context, the same prompts where shared, and **enforcement in
`log_only` mode**. The full debate system must not be trivially advantaged
over baselines that have no blocker. Blocked-claim rate is reported
separately as a deployment-safety result, not folded into RQ1.

If you change a prompt, a retriever, an embedding model, or a chunking
parameter, every affected configuration must be re-run. Partial results
across mixed code versions are not publishable. Every run records git SHA,
model version, embedding model, universe version, and config name.

### 5. Agents exchange validated schemas, not prose

Every inter-agent message must be a Pydantic model from `app/schemas/`; no
agent may parse another agent's free text. Once these components are
implemented, treat schema changes as breaking: update `docs/agent-contracts.md`
(or establish that planned contract document), contract tests, and generated
frontend types together.

### 6. No lookahead in anything historical

Backtests and point-in-time evaluation read only data that existed at the
simulated date. `app/data/cache.py` is the only sanctioned read path and
requires an `as_of` date. Never pass a full price series into a backtest
step. Ticker eligibility at `as_of` comes from `listed_from`/`listed_to` in
the universe file — a backtest must not hold a stock that wasn't tradeable.

Macro and news values are keyed by the date they became **public**
(`published_at`), not the period they describe. A CPI figure for March
released in April is invisible before April.

Known unresolved risks, to be stated as limitations: the backbone's training
data covers the historical windows, and news archive depth is limited.

### 7. The audit trail is append-only

Audit rows are never updated or deleted. Each row carries a hash of the
previous row. Every entry is timestamped, agent-attributed, and carries its
source citations. No DB grant for UPDATE or DELETE on that table. (NFR-07.)

### 8. Recommendations only — never execution

No order placement, no broker integration, no live trading, not even behind
a flag. Every recommendation surface carries a visible educational-use
disclaimer. This project is not SECP-licensed.

### 9. Secrets and user data

API keys live in `.env`, never in code, never in a commit, never in a trace.
Real user financial data never enters a test fixture, a log line, or
`docs/`. Use synthetic profiles. Collected datasets contain no user data of
any kind.

### 10. The universe is a versioned file, not a runtime guess

The investable universe is a committed YAML file in `backend/universe/`
listing PSX tickers with name, sector, board lot, `listed_from`,
`listed_to`, and a data-availability flag — plus a `CASH` line. Code never
invents a ticker, never falls back to "whatever the API returned today", and
never allocates to an instrument absent from the file.

Every result file records the universe version that produced it. Changing
the universe is an entry in `universe/CHANGELOG.md` and invalidates affected
results.

---

## Repository status and source of truth

The committed tree currently contains `backend/jobs/`, its news-ingestion
tests in `backend/tests/jobs/`, data manifests, GitHub Actions ingestion
workflows, and project documentation. It does not yet contain the planned
agent implementation, `app/quant/`, agent schemas, frontend, or experiment
packages. Treat those as planned architecture, not completed code. There is
no root README.md in the current tree. Check that a documented path or command
exists before relying on it; update plans only when implementation actually
changes.

Use `docs/proposal.md` for the approved project scope, `docs/specs.md` and
`docs/plan.md` for current working decisions and open questions, and accepted
ADRs in `docs/adr/` for changes that override the proposal. A Proposed ADR is
not an accepted decision. In particular, ADR-0005 is Proposed: do not remove
FinBERT requirements or change the current documented design on its authority
alone. Do not invent features or mark planned work complete.

## Architecture

```
User
  → Onboarding questionnaire            (frontend)
  → Risk Profiler Agent                 (sees profile; emits hard constraints)
  → Macro Agent  ⟷  Sector Analyst      (parallel, RAG-grounded, profile-blind)
  → Structured Debate, max 3 rounds     (Facilitator holds constraints)
  → Deterministic tiebreaker if no consensus
  → Portfolio Constructor               (verdict + constraints → allocation)
  → Per-ticker allocation + CASH, plus plain-English audit trail
  → Frontend: allocation, live agent trace, collapsible audit trail
```

The Risk Profiler runs **first**, so constraints exist as a hard boundary
rather than a post-hoc filter — but the market agents never receive them.

### Agents

| Agent | In | Out | Profile access |
|---|---|---|---|
| Risk Profiler | onboarding JSON | `risk_level`, `max_equity_pct`, `min_liquidity_pct`, `time_horizon`, `behavioral_flags` | full |
| Macro | SBP PDFs, World Bank, FRED, news | `macro_stance`, `key_signals[]`, `confidence_score`, `source_citations[]` | **none** |
| Sector Analyst | psxdata, Yahoo `.KA`, PSX filings, FinBERT news | `sector_recommendations[]`, `sentiment_summary`, `source_citations[]` | **none** |
| Debate Facilitator | Macro + Sector outputs | `debate_verdict`, `winning_agent`, `confidence_score`, `tie_breaker_triggered` | constraints only |
| Portfolio Constructor | all three outputs, no new external data | `final_allocation{}`, `audit_trail[]`, `debate_summary{}` | constraints only |

The Macro and Sector outputs above are the proposal's versions and are not
comparable with each other. Once `schemas/market_view.py` is settled, both
agents emit it and this table must be updated in the same commit.

### From sector views to per-ticker weights

The Sector Analyst reasons at sector level. The output is per-ticker. The
bridge is **deterministic and lives in `quant/`**, not in any agent:

1. `selection.py` picks tickers within each sector by a documented rule
   (liquidity, market cap, inverse volatility), from the universe file only.
2. `allocation.py` maps debate tilts to weights, then applies hard caps:
   max per name, max per sector, `cash ≥ min_liquidity_pct`,
   `equity ≤ max_equity_pct`. Weights sum to 1.0 exactly.
3. If caps cannot be satisfied simultaneously, `constraint_solver.py`
   resolves it, and the resolution is recorded in the audit trail. The model
   is never asked to break a tie between constraints.

### Debate protocol

- **R1** — both agents state positions with retrieved evidence. Facilitator
  loads constraints as hard boundaries.
- **R2** — each agent must rebut the *other's specific claims*. Restating
  your own position is a protocol violation: reject and re-prompt once, then
  record the violation and continue.
- **R3** — final positions with updated confidence. Arbitration begins.
- **Tiebreaker** — no consensus after three rounds → the path best
  satisfying the constraints, defaulting to the more conservative
  allocation. "Conservative" is a single defined scalar in `quant/`, not a
  model judgement. `tie_breaker_triggered` is recorded on every run.
- **Kill switch** — hard cap at three rounds. No extensions.

**Open design items, blocking:**
- Macro and Sector currently output in different spaces and so cannot
  strictly agree or disagree. A shared debate proposition schema
  (`schemas/market_view.py`) is required before the protocol means anything.
  Do not implement the debate until this is settled.
- "More conservative" has no operational definition yet. Do not implement
  the tiebreaker until it is a single scalar in `quant/`.

---

## Data collection

Collection is Step 1 of `docs/plan.md`. Three streams run in parallel:
company data, SBP and macro data, and news with filings and the market
index. Every dataset is listed with its purpose, format and consuming agent
in `docs/data-sources.md`. A dataset is not "collected" until it has an entry
there.

### Where things live

```
data/                      gitignored, repo root. Reproducible, never committed.
  raw/<source>/            exactly what the source returned. Never edited.
  raw/news/YYYY-MM-DD/     one folder per fetch day
  interim/                 parsed, not yet validated
  processed/               clean parquet, the only thing the app reads
  _logs/                   run logs and coverage logs
backend/jobs/              committed loaders, one script per source
backend/data_manifests/    committed. One manifest per dataset
backend/universe/          committed. Universe file and changelog
```

### Rules

1. **Raw is immutable.** Save the response before parsing and never overwrite
   it. Cleaned data is a separate file in `interim/` or `processed/`.
2. **Provenance on every record:** source, URL or endpoint, `fetched_at`
   (UTC), and `published_at` for macro and news values.
3. **A manifest per dataset:** source, fetch date, row or file count,
   checksum, loader script and its git SHA. Manifests are committed even
   though the data is not.
4. **Read the terms first.** Record licence, storage limits and
   automated-access rules in `docs/data-sources.md` before the first fetch.
   PSX Data Portal scripted access, including `psxdata` prices and KSE-100
   levels, is paused pending written permission; do not run those loaders
   until permission is recorded in the data-source documentation. If a source
   forbids storing article text, store headline, link and date only. Never
   collect from a source whose terms forbid automated access.
5. **One rerunnable loader per source,** with rate limiting and retries.
   `psxdata`, `yfinance` and similar are loaders, not runtime dependencies.
6. **Gaps are logged, not hidden.** Missing days, failed fetches and
   unavailable history go in `_logs/` and are cited in the thesis.
7. **Not everything is reproducible.** The news archive cannot be re-fetched
   later because free history is short. It, and any other source that cannot
   be re-fetched, is backed up outside the working machine and snapshotted
   at code freeze.
8. **Datasets contain no user data.** Synthetic profiles only (Rule 9).

---

## Tech stack

**Frontend** — Next.js (App Router) + TypeScript + Tailwind. Types generated
from the backend OpenAPI schema, never hand-written. Live agent trace over
SSE.

**Backend** — Python, FastAPI, Pydantic v2, SQLAlchemy + Alembic,
PostgreSQL. **Django is not used** (ADR-0001); the proposal's admin layer is
replaced by an internal `/runs` inspector in the frontend.

**Orchestration** — explicit typed state machine. The pipeline is
deterministic with fixed handoffs, so prefer LangGraph or plain Python over
free-form agent loops. Replayability beats flexibility.

**RAG** — LlamaIndex for ingestion, chunking and retrieval; ChromaDB as the
store. Retrievers namespaced per agent. Embeddings go through the
`EmbeddingProvider` interface in `rag/embeddings/base.py`. **The embedding
model is an open decision** resolved by
`experiments/retrieval_eval/` (recall@k and MRR on a hand-labelled PSX/SBP
query set) and recorded in ADR-0002. Until then, no code may hard-code a model
name outside that package. FinBERT sentiment remains part of the currently
documented approach unless ADR-0005 is accepted; that ADR is still marked
Proposed, so its proposed removal is not yet effective.

**Observability** — LangSmith traces every LLM call, tool use and agent
step, with model version and cost. (NFR-02, NFR-03.)

**Data** — data-source libraries are loaders into our PostgreSQL/parquet
store, not live request-path dependencies. Scripted PSX Data Portal access,
including `psxdata` prices and KSE-100 levels, remains paused pending written
permission (see `docs/data-sources.md`); do not run these loaders until it is
recorded. SBP PDFs via pdfplumber, World Bank via wbgapi, FRED, NewsAPI are
planned sources subject to their terms. All ingestion runs in `jobs/` on a
schedule; the request path only retrieves. Start archiving news headlines
now — the free NewsAPI tier has roughly a month of history, which is not
enough for the backtest windows. The archive job must run on a machine that
stays on, not a laptop that sleeps.

**Models** — production: a strong model for the Portfolio Constructor,
cost-tiered models for Macro and Sector. Ablation: one fixed backbone across
all agents and all three LLM configurations. Confirm current model IDs
before fixing the backbone rather than copying the proposal's.

---

## Evaluation

| RQ | Measures | Lives in |
|---|---|---|
| RQ1 hallucination | unmatched-claim rate, `log_only` enforcement, 15% manual subsample with written rubric and inter-annotator kappa | `experiments/ablation/` |
| RQ2 explanation quality | rubric scoring + Flesch-Kincaid + 20-participant survey, blind to condition | `experiments/survey/` |
| RQ3 personalisation | allocation distance across 4 profiles **and** monotonicity (more conservative ⇒ less equity, more cash) | `experiments/personalisation/` |
| supporting | backtest, 4 windows, walk-forward, point-in-time, survivorship-aware | `experiments/backtest/` |

Four configurations: full multi-agent debate, single monolithic LLM,
multi-agent voting with no debate, and a fixed rule — 60% equal-weighted
basket from the universe, 40% cash. The fixed rule produces no LLM output
and is scored on backtest metrics only.

Results are committed to `experiments/results/` as CSV/JSON. Never report a
number that isn't reproducible from a committed result file, a git SHA, and
a universe version.

---

## Conventions

- **Python**: ruff + black, mypy on `app/`, pytest. Type hints everywhere.
  No bare `except`. No `Any` in schemas.
- **TypeScript**: strict mode, eslint + prettier.
- **Claude Code permissions**: `.claude/settings.json` is Claude-specific.
  Its permission controls remain in force for Claude Code; these instructions
  do not bypass or weaken them, and those controls do not configure Codex.
- **Git workflow**: use sound judgment for the task. Choose sensible
  branch names and group related changes into reviewable commits; do not
  create extra branches or commits just to satisfy a naming or grouping rule.
  Conventional commit messages are welcome but not required. Agents may stage,
  commit, push, and open a pull request when that is the appropriate way to
  deliver the work; do not ask for separate permission for each of those
  steps. Run the relevant tests and checks before committing, and report what
  was run and what passed or failed.
- **Shared work and protected branches**: inspect the working tree before
  editing. Never overwrite, reset, clean, or discard another person's
  uncommitted work. Keep `main` protected and integrate through pull requests;
  never commit directly to `main`. Do not force-push, delete important
  branches, or perform other destructive Git operations without explicit
  approval. Never merge a pull request without the user's approval.
- **Attribution**: never add attribution trailers — no `Co-Authored-By`, no
  "generated with" line, no tool credit — in commit messages or pull request
  descriptions.
- **Prompts** are versioned files in `agents/prompts/`, never inline string
  literals. A prompt change is a code change and may invalidate results.
- **ADRs**: any decision that overrides the proposal gets a file in
  `docs/adr/`. The thesis will need these.
- **Secrets**: `.env` only; `.env.example` committed with empty values.

## Worklogs

Every working session ends with a file in `worklogs/` named
`YYYY-MM-DD-person-short-slug.md` (for example
`2026-11-08-falah-company-prices.md`). Use the date the session started and
the first name of the primary author in the filename; list everyone who
worked in the `Who:` line. Use a distinct short slug if the same person has
multiple sessions on one day. Copy `worklogs/TEMPLATE.md` and write the
worklog at the end of the session. Older worklogs keep their original names.

**Every worklog names people.** A `Who:` line listing whoever worked the
session is mandatory — a worklog with no name on it is incomplete. Beyond
that, attribute each decision and each work item to the person who made or
did it, since FYP contributions are assessed individually and `proposal.md`
§7 already assigns tasks by name. "Debate protocol hardened" is worth less
in May than "Debate protocol hardened (Mahrukh)".

During data collection, each worklog also lists the datasets touched: source,
date range, row or file count, and any gaps found.

Never edit an old worklog except to fix a typo; if something recorded turns
out to be wrong, say so in the current one and link back. Decisions appear
twice: a line in the worklog saying a call was made, and — when the decision
overrides the proposal — an ADR in `docs/adr/` saying why, with consequences.
See `worklogs/README.md`.

## Development checks and commands

For the current implementation, install the backend dependencies from
`backend/requirements.txt`, then run the existing offline job tests from
`backend/` with `pytest tests/jobs`. Inspect the relevant loader and test
before collecting data or running a job; source terms, network access and
secrets affect whether a job is safe to run. Do not run live ingestion merely
to validate a code change.

The isolation, determinism, universe, retrieval, ablation, backtest, API, and
frontend commands described elsewhere in this file are targets for planned
components. Run them only once those packages and tests exist. When adding an
invariant, add its regression test with the implementation. Run only checks
relevant to the change, and report exact commands and results before a commit.


## Things not to do

- Don't let the Macro or Sector agent see the profile, in any form. Rule 1.
- Don't loosen the claim matcher to make output look better. Rule 2.
- Don't let an LLM state a computed number or pick a ticker. Rule 3.
- Don't run one ablation configuration with a different enforcement mode,
  backbone, embedding model, or prompt version than the others. Rule 4.
- Don't allocate to a ticker that isn't in the universe file. Rule 10.
- Don't overwrite or edit raw data. Fix problems in a later stage.
- Don't collect from a source whose terms forbid automated access.
- Don't hard-code an embedding model outside `rag/embeddings/`.
- Don't put ingestion, PDF parsing, or FinBERT inference in the request path.
- Don't add live trade execution, ever.
- Don't add Django back, or a fourth framework. Before adding a dependency,
  check whether LlamaIndex, FastAPI or Pydantic already does it.
- Don't mark an invariant test as skipped or xfail to get CI green.
