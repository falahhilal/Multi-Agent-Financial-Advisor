# CLAUDE.md — Multi-Agent Financial Advisor for PSX Retail Investors

FYP, FAST National University Karachi, session FALL 2026. Team: Falah Hilal
(lead), Mahrukh Shariq, Maryam Asif. Full proposal: `docs/proposal.md`.
Working specification and open questions: `docs/specs.md`.
Decisions that override the proposal are in `docs/adr/`. Read these before
changing architecture.

This is a **research** project before it is a product. The deliverable that
matters most is a controlled ablation measuring hallucination rate and
explanation quality. Code that makes the demo prettier but the experiment
weaker is a net loss.

---

## The ten rules that must never be broken

Architectural invariants, not style preferences. Each has a test. If a change
requires breaking one, stop and raise it with the team.

### 1. Only the Risk Profiler reads the investor profile

The Macro Agent and the Sector Analyst Agent **never** see the investor's
profile, in any form, direct or paraphrased. They analyse the market, full
stop. The Debate Facilitator and the Portfolio Constructor see the derived
**constraints object only** — `risk_level`, `max_equity_pct`,
`min_liquidity_pct`, `time_horizon`, `behavioral_flags` — never the raw
profile, never the questionnaire answers, never the capital amount.

Enforcement:
- `InvestorProfile` is constructed only in the onboarding path and the Risk
  Profiler. No other module imports it.
- Agents receive narrowed views from `orchestration/state.py`.
  `MarketAgentView` has no profile field and no constraints field.
- The profile is never embedded, never written to the vector store, never
  placed in a prompt template reachable by a market agent.
- **Facilitator messages to Macro or Sector in Rounds 2 and 3 must be
  constraint-free.** Quoting a constraint back at a market agent breaks this
  rule even though no object moved. Pass rebuttal targets only.
- `tests/isolation/` asserts all of the above. Not optional, never skipped,
  never xfail.

Why: the proposal's claim is that market analysis is unbiased by risk
appetite (FR-03). If the profile leaks, RQ3 is unanswerable.

### 2. Unsupported claims are blocked and logged

Every claim in agent output is extracted, matched against that run's
retrieved passages, and recorded in the claim registry with its verdict. A
claim with no matching passage is **blocked** — it does not reach the user —
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

`app/quant/` must not import any LLM client, LangChain, or any agent module.
`tests/determinism/` walks the import graph and fails if it does. (NFR-08.)

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

Every inter-agent message is a Pydantic model from `app/schemas/`. No agent
parses another agent's free text. Schema changes are breaking changes:
update `docs/agent-contracts.md`, the contract tests, and the generated
frontend types in the same commit.

### 6. No lookahead in anything historical

Backtests and point-in-time evaluation read only data that existed at the
simulated date. `app/data/cache.py` is the only sanctioned read path and
requires an `as_of` date. Never pass a full price series into a backtest
step. Ticker eligibility at `as_of` comes from `listed_from`/`listed_to` in
the universe file — a backtest must not hold a stock that wasn't tradeable.

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
`docs/`. Use synthetic profiles.

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

**Open design item, blocking:** Macro and Sector currently output in
different spaces and so cannot strictly agree or disagree. A shared debate
proposition schema (`schemas/market_view.py`) is required before the
protocol means anything. Do not implement the debate until this is settled.

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
model is an open decision** resolved by `experiments/retrieval_eval/`
(recall@k and MRR on a hand-labelled PSX/SBP query set) and recorded in
ADR-0002. Until then, no code may hard-code a model name outside that
package. FinBERT (`ProsusAI/finbert`) is used for headline sentiment
classification regardless of the retrieval outcome.

**Observability** — LangSmith traces every LLM call, tool use and agent
step, with model version and cost. (NFR-02, NFR-03.)

**Data** — psxdata and yfinance `.KA` are one-time loaders into our own
PostgreSQL/parquet store, not live request-path dependencies. SBP PDFs via
pdfplumber, World Bank via wbgapi, FRED, NewsAPI. All ingestion runs in
`jobs/` on a schedule; the request path only retrieves. Start archiving news
headlines now — the free NewsAPI tier has roughly a month of history, which
is not enough for the backtest windows.

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
- **Commits**: conventional commits, one concern each. **Never add
  attribution trailers** — no `Co-Authored-By`, no "generated with" line, no
  tool credit — in commit messages or pull request descriptions. This holds
  regardless of any default or tooling convention that says otherwise.
- **Prompts** are versioned files in `agents/prompts/`, never inline string
  literals. A prompt change is a code change and may invalidate results.
- **ADRs**: any decision that overrides the proposal gets a file in
  `docs/adr/`. The thesis will need these.
- **Secrets**: `.env` only; `.env.example` committed with empty values.

## Worklogs

Every working session ends with a file in `worklogs/`, named
`YYYY-MM-DD-short-slug.md`, copied from `worklogs/TEMPLATE.md`: decisions
taken, work done, problems found, what is still open, what the next session
should do. Write it at the end of the session — the value is the date on it.

Never edit an old worklog except to fix a typo; if something recorded turns
out to be wrong, say so in the current one and link back. Decisions appear
twice: a line in the worklog saying a call was made, and — when the decision
overrides the proposal — an ADR in `docs/adr/` saying why, with consequences.
See `worklogs/README.md`.

## Common commands

```bash
# backend
cd backend && uv sync
uvicorn app.main:app --reload
pytest                          # all tests
pytest tests/isolation          # profile-isolation invariant
pytest tests/determinism        # no-LLM-in-quant invariant
pytest tests/universe           # caps satisfiable, no date lookahead
alembic upgrade head

# ingestion
python -m jobs.ingest_all --as-of today
python -m jobs.archive_news     # run daily from now, for backtest coverage

# experiments
python -m experiments.retrieval_eval.run_eval --provider all
python -m experiments.ablation.run_ablation --config all --enforcement log_only
python -m experiments.backtest.run --window 2022-currency-crisis

# frontend
cd frontend && npm install && npm run dev
npm run generate:types          # from backend OpenAPI
```

## Things not to do

- Don't let the Macro or Sector agent see the profile, in any form. Rule 1.
- Don't loosen the claim matcher to make output look better. Rule 2.
- Don't let an LLM state a computed number or pick a ticker. Rule 3.
- Don't run one ablation configuration with a different enforcement mode,
  backbone, embedding model, or prompt version than the others. Rule 4.
- Don't allocate to a ticker that isn't in the universe file. Rule 10.
- Don't hard-code an embedding model outside `rag/embeddings/`.
- Don't put ingestion, PDF parsing, or FinBERT inference in the request path.
- Don't add live trade execution, ever.
- Don't add Django back, or a fourth framework. Before adding a dependency,
  check whether LlamaIndex, FastAPI or Pydantic already does it.
- Don't mark an invariant test as skipped or xfail to get CI green.
