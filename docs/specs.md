# Specs — Multi-Agent Financial Advisor for PSX Retail Investors

Working specification derived from `proposal.md` during the planning pass of
11–12 Oct 2026. This file records what the system is, what the proposal left
unresolved, which decisions have been taken since, and what is still open.

`../CLAUDE.md` governs Claude Code-specific instructions; the root
`../AGENTS.md` is Codex's primary instruction file. Their shared requirements
must stay aligned, and neither tool-specific file overrides the other or
settles a project-level disagreement. The proposal defines scope, accepted
ADRs record approved deviations, and this specification and `plan.md` record
working decisions and open questions. If these project sources conflict or
leave a question unresolved, raise it for resolution rather than inferring an
answer from `CLAUDE.md` or `AGENTS.md`.

- **Project**: FYP, FAST National University Karachi, session FALL 2026
- **Team**: Falah Hilal (lead, 23K-3031), Mahrukh Shariq (23K-3023), Maryam Asif (23K-0505)
- **Schedule**: FYP-I Sep–Dec 2026, FYP-II Jan–May 2027
- **Status at time of writing**: repository has no commits yet; `docs/` untracked

---

## 1. What the system does

A user fills in a questionnaire. That becomes a JSON profile. Five agents
then run in a fixed pipeline, two of them deliberately blindfolded to who the
user is, and the output is a portfolio allocation plus a readable explanation
of why.

### 1.1 The five agents

| Agent | Takes in | Gives out | Sees the profile? |
|---|---|---|---|
| **Risk Profiler** | Onboarding JSON only (risk tolerance, horizon, goal, existing holdings, investable capital) | `risk_level`, `max_equity_pct`, `min_liquidity_pct`, `time_horizon`, `behavioral_flags` — a **hard** constraints object, not a preference | Yes — the only one |
| **Macro Agent** | SBP monetary policy PDFs, World Bank API, FRED, news | `macro_stance` (hawkish/neutral/dovish), `key_signals[]`, `confidence_score`, `source_citations[]` | **No** |
| **Sector Analyst** | psxdata (OHLCV, sector summaries), Yahoo `.KA` fallback, PSX filings, FinBERT-classified headlines | `sector_recommendations[]`, `sentiment_summary`, `source_citations[]` | **No** |
| **Debate Facilitator** | Macro + Sector outputs, plus the constraints as hard boundaries. No new external data | `debate_verdict`, `winning_agent`, `confidence_score`, `tie_breaker_triggered` | Limits only |
| **Portfolio Constructor** | Outputs of the other three. No new external data | `final_allocation{}`, `audit_trail[]`, `debate_summary{}` | Limits only |

Macro and Sector run **in parallel**, and both run *after* the Risk Profiler
so the constraints already exist before any market opinion is formed — but
they never receive them. The stated reason is to stop the investor's risk
appetite from biasing market signal analysis.

### 1.2 The debate protocol

- **Round 1** — Macro and Sector each state a position with supporting
  retrieved evidence. The Facilitator is handed the risk constraints as hard
  boundaries.
- **Round 2** — Each agent must attack *the other's specific claims*.
  Restating your own position is not allowed. The Facilitator tracks both
  arguments.
- **Round 3** — Both submit a final position with a fresh confidence score.
  Arbitration begins.
- **Tiebreaker** — If there is no consensus after three rounds, the
  Facilitator picks whichever path best satisfies the risk constraints,
  **defaulting to the more conservative allocation**, and records that as its
  verdict.
- **Handoff** — The verdict (plus winner, confidence, tiebreaker flag) goes
  to the Constructor, which combines it with the constraints to produce the
  allocation and the audit trail.

### 1.3 The two grounding mechanisms

1. **Retrieved facts** (a policy rate, a sector P/E, a filing value) must
   match a passage in the retrieved documents. An automated checker flags
   anything unmatched.
2. **Derived numbers** (portfolio weights, Sharpe ratio, volatility) are
   computed by a deterministic Python module. The LLM only writes prose about
   numbers the module actually returned.

### 1.4 Functional requirements, condensed

Onboard via questionnaire (FR-01) → Risk Profiler emits hard constraints
*before* market analysis (FR-02) → the profile is invisible to Macro and
Sector (FR-03) → Macro reasons over SBP/inflation/GDP/FX/sentiment from
sources (FR-04) → Sector reasons over PSX performance, P/E, earnings, FinBERT
news (FR-05) → unsupported quantitative claims are blocked (FR-06) →
Facilitator runs ≤3 rounds and applies a deterministic conservative
tiebreaker (FR-07) → Constructor synthesises verdict + constraints into an
allocation and audit trail (FR-08) → output is an allocation JSON with
weights (FR-09) and a plain-English audit trail with agent attribution and
citations (FR-10), under a 45s working latency target (FR-11), at grade 8–10
readability (FR-12), with all weights and risk metrics from deterministic
code (FR-13).

### 1.5 The research payload

The four-way ablation (full debate / single monolithic LLM / multi-agent
voting / fixed rule) on identical inputs, measuring hallucination rate and
explanation quality — plus backtests on four windows, a 20-person
explainability survey, and a four-profile personalisation check.

---

## 2. Decisions taken

| # | Decision | Rationale | ADR |
|---|---|---|---|
| 1 | **Ablation runs with claim-blocking in `log_only` mode** across all three LLM configurations | Blocking removes unmatched claims by construction, so measuring the full system against unblocked baselines measures the blocker, not the architecture. See §3.1(a) | 0003 |
| 2 | **FastAPI only — Django dropped** | Two frameworks over one database is harder to defend than either alone. Admin UI replaced by an internal `/runs` inspector page | 0001 |
| 3 | **Asset universe is individual PSX stocks** from a committed ticker list, plus a `CASH` line | User decision, 12 Oct 2026. "For now" — the universe file is versioned so this can grow | 0004 |
| 4 | **Embedding model to be chosen by measurement**, not assumed | FinBERT is a sentiment classifier, not an embedding model, but a measured answer is worth more in the thesis than either choice asserted. Benchmark in `experiments/retrieval_eval/` | 0002 |

### 2.1 Consequences of the `log_only` ablation

The enforcement layer needs a mode flag (`block` | `log_only`) from the first
commit, read from config and recorded in every run's metadata. Blocked-claim
rate becomes a second, separately-reported result rather than a confound
inside RQ1. This yields two clean findings instead of one contaminated one.

### 2.2 Consequences of dropping Django

SQLAlchemy + Alembic own the schema. Budget a small internal `/runs`
inspector page in the frontend for debugging agent runs — cheaper than
carrying a second framework for nine months.

### 2.3 Consequences of the stock-level universe

1. **A cash line is mandatory.** `min_liquidity_pct` has nowhere to go in a
   stocks-only universe. The universe is therefore *N PSX tickers + CASH*,
   and the fixed-rule baseline becomes 60% equal-weighted basket / 40% cash.
   If an SBP T-bill yield series can be sourced, cash earns a real return and
   the baseline becomes much more credible — SBP publishes auction results.
2. **There is a gap between the Sector Analyst and the output.** It emits
   `sector_recommendations[]`; the Constructor needs per-ticker weights.
   Something must choose *which* stocks inside a favoured sector. Either the
   Sector Analyst emits per-ticker views (more grounding work, more claims to
   verify, richer debate), or `quant/` picks deterministically by a
   documented rule. **Recommended: the latter** — it keeps the LLM out of
   selection, which Rule 3 already demands for weights, and keeps the claim
   surface small.
3. **Weights need a deterministic algorithm.** The verdict gives tilts;
   `quant/allocation.py` must turn tilts into weights identically every time.
   Recommended: score → weight normalisation with hard caps (max per name,
   max per sector, `cash ≥ min_liquidity_pct`, `equity ≤ max_equity_pct`),
   with a constrained solver only if simple normalisation cannot satisfy all
   caps at once. Must be unit-testable with no network and no model.
4. **Backtesting gets harder in specific ways.** Per-ticker series,
   survivorship bias (PSX has delistings and suspensions), trading halts, and
   board lots. The universe file therefore carries `listed_from` and
   `listed_to` per ticker so a backtest at `as_of` only sees what was
   actually tradeable. Cheap to add now, expensive to retrofit in April.

### 2.4 Consequences of benchmarking the embedding model

Code against an `EmbeddingProvider` interface so the model is one line to
swap. Build the retrieval evaluation harness with a hand-labelled PSX/SBP
query set scored on recall@k and MRR. Build the labelled set while ingesting
documents in Oct–Nov, since those PDFs are being read anyway. Result goes in
ADR-0002. FinBERT is used for headline sentiment classification regardless of
the retrieval outcome.

---

## 3. Problems found in the proposal

### 3.1 Blocking — these threaten the research contribution itself

**(a) The hallucination comparison was unfair by construction.** *(Resolved
by decision 1.)* FR-06 *blocks* unsupported claims from being emitted.
Hallucination rate is *defined* as the proportion of claims the checker
cannot match. So the full system's rate approaches zero mechanically, while
the single-agent and voting baselines have no blocker attached. That measures
the claim-blocker, not the debate architecture — and RQ1 asks about the
architecture. This was the most serious issue in the document.

**(b) Macro and Sector have nothing to disagree about.** *(Still open —
blocking.)* Macro outputs a stance (`hawkish/neutral/dovish`). Sector outputs
sector recommendations. These live in *different output spaces*. They can
never be "perfectly identical" (so the debate always triggers, as the
proposal admits) but they also cannot strictly contradict each other — there
is no shared proposition to adjudicate. `winning_agent` presumes a binary
winner over a comparison that is not defined.

A **debate proposition schema** is needed: a common quantity both agents must
commit to and defend. The natural candidate is a proposed tilt over the same
buckets — both emit a target weight sketch across the sector universe plus an
equity-exposure score. Then disagreement is measurable, Round 2 rebuttals
have a target, and the tiebreaker has something to order. Without this the
debate is theatre and RQ2 has no teeth. **Do not implement the debate until
this is settled.**

**(c) "More conservative" is undefined.** The tiebreaker is the one
deterministic guarantee in the system, so it needs an operational scalar:
lowest equity %? highest cash? lowest ex-ante volatility from the quant
module? Pick one, define it in code, test it.

**(d) NewsAPI's free tier serves roughly a month of history.** The backtests
span four historical windows. If news sentiment is a real input to the Sector
Analyst, it cannot be reconstructed historically on the free tier — and paid
archive access has commercial-use restrictions worth reading. Options: start
archiving headlines now so that by Apr 2027 there are ~6 months of genuine
point-in-time news; run backtests with the news channel disabled and disclose
it; or find an alternative archive. **Decide within weeks** — the archiving
option only works if it starts early.

**(e) Backtest lookahead from the LLM's own training data.** The backbone
knows how 2020 and 2022 turned out in Pakistan. A standard and well-known
threat to validity for LLM backtests, not mentioned in the proposal.
Mitigations: prefer windows after the backbone's cutoff, strip dates from
retrieved context, state it plainly in the limitations chapter.

### 3.2 Design under-specification

**(f) The asset universe did not exist.** *(Resolved by decision 3 — now
individual PSX stocks plus cash. The ticker list itself is still to be
written; see §4.)*

**(g) `existing_holdings` is collected and never used.** No agent's output
schema touches it. Is the Constructor proposing a portfolio from scratch or
rebalancing from where the user already is? These are different products, and
the second needs concentration and turnover logic.

**(h) The isolation rule has a leak path.** The Facilitator holds the
constraints *and* talks to Macro and Sector during Rounds 2 and 3. If its
prompts quote or paraphrase the constraints, isolation is broken in substance
even though the profile object never moved. Hence Rule 1 in both instruction files explicitly forbids sending profile
constraints to market agents; add tests for this boundary with orchestration.

**(i) "Any claim" vs "any quantitative claim."** The shared Rule 2 in `CLAUDE.md` and `AGENTS.md`
covers *any* claim; FR-06 says *quantitative*; §5 defines a claim as "any
numerical or factual statement." Enforcing hard-block on arbitrary factual
prose requires atomic claim decomposition (reference [7] is exactly about
this) and produces false positives that block valid output. Resolved in
Rule 2 in `CLAUDE.md` and `AGENTS.md`: hard-block numeric and directly-attributable claims,
log-and-flag qualitative ones, and state the distinction in the thesis.

**(j) No defined instruments for the two subjective measures.** "Explanation
quality" (RQ2) and "varies meaningfully" (RQ3) are both unoperationalised.
RQ2 needs a rubric, scorers, blinding, and an inter-rater agreement
statistic. RQ3 needs a distance metric *and* a direction check — allocations
varying randomly across profiles is a failure, not a success; the target is
monotonicity (more conservative profile ⇒ less equity, more cash).

**(k) The 15% manual annotation needs a rubric and a kappa.** §5 says "the
two team members" — there are three. Who annotates, and how is disagreement
resolved?

### 3.3 Stack and engineering concerns

**(l) FastAPI *and* Django.** *(Resolved by decision 2.)*

**(m) FinBERT as the retrieval embedding model.** *(Deferred to measurement
by decision 4.)* `ProsusAI/finbert` is a BERT classifier fine-tuned for
sentiment polarity. It was never trained to produce sentence embeddings for
semantic search and is expected to retrieve worse than a purpose-built
embedding model.

**(n) LangChain + LlamaIndex overlap.** Boundary: LlamaIndex owns ingestion,
chunking and retrieval; LangChain owns agents and tool binding. Separately,
the debate is a fixed three-round state machine with deterministic handoffs,
which is a poor fit for free-form LangChain agent loops — LangGraph or plain
typed Python will be easier to test and to replay deterministically for the
ablation. Worth settling before Mahrukh's Feb 2027 protocol work.

**(o) 45 seconds is reachable only if ingestion is offline.** Three rounds
across two agents plus facilitator plus constructor is roughly 10–14 LLM
calls. If SBP PDF parsing, FinBERT batch classification and embedding happen
inside the request, the target is missed badly. Pre-ingest nightly; the
request path should only retrieve and reason.

**(p) The "live agent trace" implies streaming** — SSE or WebSocket — which
appears nowhere in the proposal's stack tables.

**(q) `psxdata` is a small community scraper.** When PSX changes its site the
data layer dies, possibly in April 2027. Treat these libraries as one-time
loaders into our own store so the thesis stays reproducible. Also worth
reading PSX's terms on automated access.

**(r) NFR-07 says "immutable" without a mechanism.** Append-only table,
revoked UPDATE/DELETE grants, per-row hash chain over the previous row. Cheap
to build, looks strong in a compliance chapter.

**(s) No auth, no privacy policy, no user-visible disclaimer requirement.**
The system stores people's financial situations and issues investment-shaped
output while explicitly not SECP-licensed. The educational framing should be
a *functional requirement* with a disclaimer rendered on every
recommendation, not a line in the scope section. Separately, the 20-person
survey almost certainly needs FAST ethics approval and consent forms — not
mentioned anywhere, and on the critical path for Apr 2027.

**(t) The ablation backbone is not the production backbone.** Fixing
GPT-4o-mini for the experiment and Claude Sonnet/Haiku for the deployed
system means the thing evaluated is never the thing shipped. Defensible if
stated as a limitation; running the ablation on one weak and one strong
backbone would be stronger. Also GPT-4o-mini is now an old model — if the
backbone is being fixed in Oct 2026 for work running to May 2027, re-check
what the cheap-tier options actually are.

**(u) NFR-05 has no budget number.** Put a figure per cycle and per ablation
run in it, or it is unfalsifiable.

### 3.4 Document-level fixes for the proposal itself

- Two sections are both numbered **5.1** (System Flow, Functional Requirements).
- "TradingAgents" vs "Trading Agents", "FinBERT" vs "Fin BERT" are inconsistent.
- Several reference IDs should be verified to resolve as cited — particularly
  [4] (the abstract and §2.4 cite Choi, Zhu & Li as NeurIPS 2025 while the
  entry gives arXiv:2508.17536), [6], [7] and [12]. Worth a pass before
  submission: citations that do not resolve are the fastest way to lose a
  panel's trust.
- **Schedule check.** The Gantt puts Maryam's "Environment setup: repository
  structure" and Falah's data pipeline in Sep–Oct 2026. As of 12 Oct 2026 the
  repository has no commits. Claim-blocking enforcement is scheduled Oct–Nov
  2026 but the first agent that could exercise it (Risk Profiler) lands Dec
  2026 and the retrieval agents not until Jan 2027 — so the enforcement layer
  gets built with nothing to enforce against. Consider a thin fake-agent
  harness in Nov so the blocker is tested on arrival.

---

## 4. Open questions

Carried forward. Answers belong in an ADR when they land.

**From the stock-level universe decision**

1. How many tickers in the starting list?
2. Is the list fixed for the whole project, or re-derived per run?
3. Is KSE-100 constituent *history* available, or only the current snapshot?
   (Determines whether survivorship bias can be handled honestly.)
4. Does `psxdata` expose per-ticker fundamentals — P/E, earnings — for all of
   them, or only sector aggregates? This determines whether FR-05's "PE
   ratios, earnings" is answerable at ticker level at all.

**From the proposal**

5. What exactly do Macro and Sector debate over? (§3.1(b) — blocking.)
6. Does the system rebalance from existing holdings, or allocate fresh?
   (§3.2(g).)
7. Which four backtest windows, and what is the benchmark — KSE-100 total
   return?
8. Has the survey been through FAST's ethics process?
9. How much news history is already archived? (§3.1(d).)
10. Is there a login, or is this a single-session demo?
11. What is the per-cycle API cost ceiling? (§3.3(u).)
12. Which two of the three team members do the manual hallucination
    annotation? (§3.2(k).)

---

## 5. Repository layout

Monorepo, Python backend and Next.js frontend side by side. The structure is
deliberately shaped so the hard rules are enforced by *where code lives*,
not by discipline.

```
FYP/
├── CLAUDE.md
├── AGENTS.md
├── README.md
├── docker-compose.yml              # postgres, chroma, backend, frontend
├── .github/workflows/ci.yml        # lint, typecheck, pytest, invariant tests
├── docs/
│   ├── proposal.md
│   ├── specs.md                    # this file
│   ├── adr/
│   │   ├── 0001-fastapi-only.md
│   │   ├── 0002-embedding-model.md             # written after the benchmark
│   │   ├── 0003-log-only-ablation.md
│   │   └── 0004-stock-level-universe.md
│   ├── agent-contracts.md
│   ├── evaluation-plan.md          # RQ → metric → instrument → analysis
│   └── data-sources.md             # per source: licence, coverage, refresh, gaps
│
├── backend/
│   ├── pyproject.toml   .env.example
│   ├── alembic/
│   ├── universe/                   # versioned, committed, auditable
│   │   ├── psx_v1.yaml             #   ticker, name, sector, board_lot,
│   │   │                           #   listed_from, listed_to, data_ok, + CASH
│   │   └── CHANGELOG.md            #   every universe change, dated
│   ├── app/
│   │   ├── main.py  config.py
│   │   ├── api/v1/
│   │   │   ├── onboarding.py  recommendations.py
│   │   │   ├── trace.py                        # SSE live agent trace
│   │   │   ├── audit.py
│   │   │   └── runs.py                         # replaces Django admin
│   │   ├── schemas/                # the inter-agent ABI, Pydantic only
│   │   │   ├── profile.py          #   InvestorProfile (private)
│   │   │   ├── constraints.py      #   RiskConstraints (shareable)
│   │   │   ├── market_view.py      #   shared debate proposition — UNRESOLVED
│   │   │   ├── debate.py
│   │   │   ├── allocation.py       #   per-ticker weights + CASH
│   │   │   └── citations.py        #   Claim, SourcePassage, CitationLink
│   │   ├── agents/
│   │   │   ├── base.py  risk_profiler.py  macro.py  sector.py
│   │   │   ├── facilitator.py  portfolio_constructor.py
│   │   │   └── prompts/            # versioned .md, one per agent
│   │   ├── orchestration/
│   │   │   ├── graph.py            # profiler → parallel → debate → build
│   │   │   ├── debate.py           # 3 rounds, rebuttal forcing, tiebreaker
│   │   │   └── state.py            # typed state + narrowed per-agent views
│   │   ├── grounding/
│   │   │   ├── claim_extractor.py  verifier.py  registry.py
│   │   │   └── enforcement.py      # mode: block | log_only
│   │   ├── rag/
│   │   │   ├── ingest/             # sbp_pdf, worldbank, news, filings
│   │   │   ├── chunking.py  store.py  retrievers.py
│   │   │   └── embeddings/         # pluggable provider
│   │   │       ├── base.py         #   EmbeddingProvider interface
│   │   │       ├── finbert.py
│   │   │       └── sentence.py     #   the dedicated embedding model
│   │   ├── data/
│   │   │   ├── psx.py  yahoo.py  sbp.py  worldbank.py  fred.py  newsapi.py
│   │   │   ├── tbill.py            # cash return proxy, if sourceable
│   │   │   └── cache.py            # point-in-time reads, as_of required
│   │   ├── quant/                  # DETERMINISTIC ONLY. No LLM imports.
│   │   │   ├── universe.py         # loads + validates the ticker list
│   │   │   ├── selection.py        # which tickers inside a favoured sector
│   │   │   ├── allocation.py       # tilts → per-ticker weights, with caps
│   │   │   ├── metrics.py          # sharpe, vol, drawdown, correlation
│   │   │   └── constraint_solver.py
│   │   ├── sentiment/finbert.py    # headline classification only
│   │   ├── audit/  trail.py  readability.py  hashchain.py
│   │   ├── db/  models.py  session.py  repositories/
│   │   └── observability/langsmith.py
│   ├── jobs/                       # nightly ingest, cache warm, news archive
│   ├── experiments/                # never imported by app/
│   │   ├── ablation/configs/       # full_debate | single | voting | fixed_rule
│   │   ├── ablation/run_ablation.py
│   │   ├── retrieval_eval/         # the FinBERT-vs-embedding benchmark
│   │   │   ├── queries.yaml        #   hand-labelled PSX/SBP query set
│   │   │   └── run_eval.py         #   recall@k, MRR
│   │   ├── backtest/               # walk-forward, survivorship-aware
│   │   ├── personalisation/  survey/  results/
│   └── tests/
│       ├── unit/  contract/  e2e/
│       ├── isolation/              # profile never reaches macro/sector
│       ├── grounding/              # blocker precision/recall on fixtures
│       ├── determinism/            # quant/ imports no LLM client
│       └── universe/               # caps satisfiable, no lookahead on dates
│
├── frontend/
│   ├── app/
│   │   ├── onboarding/  portfolio/[runId]/
│   │   └── runs/                   # internal run inspector
│   ├── components/  onboarding/ allocation/ trace/ audit/ ui/
│   ├── lib/  api-client.ts  types.ts   # generated from OpenAPI
│   └── e2e/
└── data/                           # gitignored: pdfs, chroma, parquet, .env
```

### 5.1 Why the layout is shaped this way

- **`orchestration/state.py` is where the isolation rule actually lives.**
  Each agent is handed a narrowed view built from the run state; Macro and
  Sector receive a view type with no profile field at all. The rule then
  fails at type-check and test time instead of relying on memory. The profile
  also never enters the vector store, so there is no retrieval path to it.
- **`quant/` is a no-LLM zone with a test that enforces it.** A test walks
  the import graph of `app/quant/` and fails if any LLM client appears. That
  is NFR-08 made mechanical.
- **`schemas/` is the contract surface.** Agents exchange validated Pydantic
  objects, never free text, which is what makes the debate replayable and the
  ablation comparable.
- **`experiments/` never imports from itself into `app/`.** It imports `app/`
  freely, but nothing in `app/` imports it. The research harness can flip the
  enforcement layer to `log_only` or swap a backbone without a single
  production code path changing.
- **The universe file is committed YAML, not a database table.** It is a
  research artifact that must be versioned alongside results — a reviewer
  should be able to see exactly which tickers produced a given table.
- **`quant/selection.py` is separate from `allocation.py`.** Picking *which*
  stocks and deciding *how much* are different rules with different tests,
  and one will need to vary without touching the other.

---

## 6. Suggested next steps

Two items should land before any application code, because both get more
expensive the longer they wait:

1. **Settle `schemas/market_view.py`** — the shared debate proposition. The
   debate has nothing to adjudicate without it, and three team members'
   FYP-II work sits downstream.
2. **Start the daily news archive job** — it only pays off if it begins now.
   Every week of delay is a week of backtest coverage lost.

Then, roughly in order: the universe file and its tests; the ingestion jobs
and the labelled retrieval query set; the embedding benchmark and ADR-0002;
the claim extractor and verifier with a fixture set; and a thin fake-agent
harness so the enforcement layer is exercised before real agents exist in Dec.
