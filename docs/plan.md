# Project Plan

Step-by-step plan from 14 Oct 2026 to final submission in May 2027.

This plan sits between two other documents. `proposal.md` §7 and §8 fix the
task ownership and the month-level Gantt that was submitted; this plan keeps
those assignments but adds sequencing, definitions of done, and gates the
proposal does not specify. `specs.md` holds the open questions and known
problems that several phases below exist to close out.

**Stated starting point:** finalise a prototype, then produce the SRS and the
SDS. Everything after that is planned here.

---

## How this plan is ordered, and one assumption to check

The prototype comes **before** the SRS and SDS. That is deliberate: eleven of
the twelve open questions in `specs.md` §4 are answerable only by touching
the real data sources, and the debate protocol has a known hole (§3.1b) that
a document cannot close. Requirements written after a working vertical slice
are grounded; written before, they are guesses that the SRS then locks in.
FR-11's latency target is explicitly "confirmed during implementation rather
than assumed a priori", which only works in this order.

**Assumption to verify with the supervisor this week:** that the department
accepts SRS and SDS submitted in December rather than before implementation
begins. If it requires them earlier, swap Phases 1 and 2 — the cost is an SRS
written against unverified data sources, which will need a revision pass in
January, and that revision should then be budgeted.

---

## Milestones

| # | Milestone | Target | Gate |
|---|---|---|---|
| M0 | Blockers cleared, repo runs | 31 Oct 2026 | Every `specs.md` §4 question answered or deferred in writing |
| M1 | Prototype: one recommendation end-to-end | 12 Dec 2026 | Real sources, blocked claims logged, measured latency |
| M2 | SRS complete | 24 Dec 2026 | Reviewed against the prototype, not the proposal |
| M3 | SDS complete | 9 Jan 2027 | Every interface in it exists as a Pydantic schema |
| M4 | FYP-I report submitted | per department | — |
| M5 | All five agents implemented | 28 Feb 2027 | Full pipeline passes contract + isolation tests |
| M6 | Frontend complete, system demoable | 20 Mar 2027 | 45s target met or breach documented |
| M7 | **Code freeze for evaluation** | 22 Mar 2027 | No prompt, retriever or schema changes after this |
| M8 | Evaluation complete | 30 Apr 2027 | All four studies, results committed |
| M9 | Thesis submitted, demo ready | per department | — |

M7 is the one date that cannot slip quietly. Any prompt, retriever, chunking
or embedding change after it invalidates ablation runs already completed
(`CLAUDE.md` Rule 4), and there is no time to re-run everything in April.

---

## Phase 0 — Clear the blockers (14–31 Oct 2026)

Nothing downstream is safe to build until these close. Three are
time-sensitive for reasons outside our control.

### Tasks

1. **Start the daily news archive job.** *(Falah — do this first, today if
   possible.)* The free NewsAPI tier holds roughly a month of history, so
   every week of delay permanently removes a week of point-in-time news from
   the backtests. A cron job writing raw JSON to `data/news/YYYY-MM-DD/` is
   enough; parsing can come later. This is the only task on the plan whose
   cost grows while it waits.
2. **Settle `schemas/market_view.py`** — the shared debate proposition.
   *(Mahrukh, with Falah.)* Macro and Sector currently output in different
   spaces and so can neither agree nor contradict each other (`specs.md`
   §3.1b). Both agents must commit to one comparable structure: a proposed
   tilt across the sector buckets plus an equity-exposure score, each line
   carrying its citations and a confidence. Until this exists, `winning_agent`
   is undefined and the debate cannot be implemented.
3. **Define "more conservative" as a single scalar** in `quant/`. *(Mahrukh.)*
   The tiebreaker is the system's one deterministic guarantee and currently
   has no operational meaning (`specs.md` §3.1c).
4. **Write the four ADRs** — 0001 FastAPI only, 0002 embedding model
   (*Proposed*), 0003 log-only ablation, 0004 stock-level universe.
   *(Owners per `specs.md` §2.)*
5. **Answer the twelve open questions** in `specs.md` §4. *(Split by area.)*
   The four universe questions gate the universe file; the KSE-100
   constituent-history question gates whether survivorship bias can be
   handled honestly, which affects what the thesis can claim.
6. **Scaffold the repository** to the layout in `specs.md` §5. *(Maryam.)*
   `docker-compose` with Postgres and Chroma, `pyproject.toml`, Alembic
   initial migration, ruff + mypy + pytest wired into CI, Next.js app
   skeleton, `.env.example`. Empty packages with `__init__.py` are fine.
7. **Write the four invariant tests before the code they guard.**
   *(Maryam.)* `tests/isolation/`, `tests/determinism/`, `tests/grounding/`,
   `tests/universe/`. They will fail initially — that is correct. Writing
   them first is what stops Rules 1, 3 and 10 from being discovered as
   violations in March.
8. **Universe file v1** — `backend/universe/psx_v1.yaml`, 20–40 liquid
   KSE-100 tickers with name, sector, board lot, `listed_from`, `listed_to`,
   data-availability flag, plus the `CASH` line. *(Falah.)* Start small; the
   file is versioned precisely so it can grow.
9. **Open the ethics process for the survey.** *(Mahrukh.)* Approval lead
   time is unknown and it gates recruitment in April. Find out what FAST
   requires and start it now, not in March.

### Definition of done
Repo clones, `docker compose up` works, CI runs green except the four
invariant suites, every §4 question has an answer or a written deferral,
`market_view.py` exists and is reviewed by all three, news archiving has been
running for at least a week.

---

## Phase 1 — Prototype: the walking skeleton (1 Nov – 12 Dec 2026)

A prototype's job is to retire risk, not to show features. Scope it to the
four things that could still sink the project, and stub everything else.

**The four risks it must retire:** that grounded retrieval over SBP PDFs and
PSX data actually produces citable passages; that a three-round debate
produces a usable verdict rather than two agents talking past each other;
that deterministic allocation can satisfy all constraint caps at once; and
that the whole cycle fits in 45 seconds.

### In scope
- Onboarding form → `InvestorProfile` → Risk Profiler → `RiskConstraints`
- A **fixed, pre-ingested corpus**: 3–5 SBP monetary policy statements, World
  Bank indicators for Pakistan, one month of archived headlines, OHLCV and
  whatever fundamentals exist for the 20–40 ticker universe
- Macro and Sector agents running in parallel, both emitting `MarketView`,
  both profile-blind
- Claim extraction, verification against retrieved passages, enforcement in
  `block` mode with every block logged
- Three-round debate with rebuttal forcing and the deterministic tiebreaker
- `quant/selection.py` and `quant/allocation.py` producing per-ticker weights
  plus cash, all caps respected, weights summing to 1.0
- Plain-English audit trail with agent attribution and citations
- A minimal Next.js page: allocation table, audit trail, agent trace
- LangSmith tracing from the first LLM call, not retrofitted

### Deliberately out of scope until Phase 4
Auth and user accounts; nightly ingestion scheduling (ingest by hand); the
full ticker universe; FinBERT sentiment (stub it with neutral labels);
readability scoring; the `/runs` inspector; mobile layout; cost-tiered model
routing (use one cheap model throughout).

### Order of work
1. RAG ingestion for the fixed corpus, and the labelled retrieval query set
   built while reading those documents anyway *(Falah)*
2. Embedding benchmark → ADR-0002 flips to *Accepted* with real numbers
   *(Falah)*
3. Risk Profiler + constraints, with the isolation test now passing
   *(Mahrukh)*
4. Macro and Sector against the fixed corpus *(Falah)*
5. Claim extractor and verifier, measured on a hand-built fixture set so
   precision and recall are known before the ablation depends on them
   *(Falah)*
6. Debate protocol and tiebreaker *(Mahrukh)*
7. `quant/` selection, allocation, metrics *(Mahrukh)*
8. FastAPI endpoints, SSE trace, minimal frontend *(Maryam)*
9. **Latency measurement** — 20 runs, record p50 and p95, identify where time
   actually goes *(Maryam)*

### Definition of done
One synthetic profile produces one complete recommendation from real
retrieved sources. Blocked claims appear in the log and not in the output.
Every number in the allocation traces to `quant/`. All four invariant suites
pass. Measured latency is written into the worklog whether or not it meets
45s. A screen recording exists for the FYP-I demo.

### What the prototype is expected to reveal
Budget time for these rather than treating them as surprises: retrieval
quality on scanned SBP PDFs; whether the claim verifier's false-positive rate
is tolerable in `block` mode; whether three rounds converge or just restate;
and whether constraint caps are jointly satisfiable often enough that
`constraint_solver.py` is rarely needed.

---

## Phase 2 — SRS (12–24 Dec 2026)

Written against the prototype, not the proposal. *(Lead: Mahrukh; Maryam on
interfaces; Falah on data requirements.)*

Structure: purpose and scope; stakeholders and user characteristics;
functional requirements FR-01 to FR-13 **as corrected**; non-functional
requirements NFR-01 to NFR-08 with measured values where Phase 1 produced
them; external interface requirements per data source, each with its licence,
coverage window and known gaps; constraints and assumptions; a traceability
matrix from requirement to test.

Corrections the prototype will have forced, carried in from `specs.md`:

- **FR-06** splits into FR-06a hard-block (numeric and source-attributed
  claims) and FR-06b log-and-flag (qualitative reasoning). The proposal's
  single requirement is unimplementable as written.
- **FR-09** becomes per-ticker weights plus a cash line, not asset classes.
- **FR-11** carries the measured p95 from Phase 1, with mitigations if the
  target was missed.
- **New FR**: a user-visible educational-use disclaimer on every
  recommendation surface — currently only a scope note, which is not enough
  given the system stores financial situations and is not SECP-licensed.
- **New FR**: `existing_holdings` is either used by the Constructor or
  removed from onboarding. It is currently collected and ignored.
- **New NFR**: data retention and consent for stored profiles.

**Definition of done:** every requirement is testable, every requirement maps
to at least one test in the traceability matrix, and nothing in it is known
to be false of the prototype.

---

## Phase 3 — SDS (24 Dec 2026 – 9 Jan 2027)

*(Lead: Maryam; Mahrukh on agent and debate design; Falah on data and RAG.)*

Contents: three-tier architecture with the layer responsibilities;
component design per module in `specs.md` §5; the Pydantic schemas as
interface specifications, lifted from code rather than drawn separately;
a sequence diagram for the full cycle and a second one for the three-round
debate including the tiebreaker branch; the PostgreSQL data model including
the append-only audit table and its hash chain; the RAG pipeline with
chunking parameters and the chosen embedding model; deployment topology; and
how each of the ten invariants in `CLAUDE.md` is enforced, naming the test.

The four ADRs feed this directly — each deviation from the proposal appears
here with its recorded justification rather than silently.

**Definition of done:** every interface named in the SDS exists in
`app/schemas/`, and a reader could rebuild the system from the document.

**M4, the FYP-I report,** assembles from what Phases 0–3 already produced:
literature review (all three, Sep–Oct per the Gantt), the specification work,
the prototype, and the demo recording. It should require writing, not new
work.

---

## Phase 4 — Full implementation (12 Jan – 28 Feb 2027)

Follows the proposal's Gantt and ownership. Each agent moves from prototype
quality to final: full prompts, complete output schemas, source-citation
validation, error handling, and its own contract tests.

| Weeks | Work | Owner |
|---|---|---|
| Jan wk 1–3 | Macro Agent complete: prompts, retrieval, schema, citation validation | Falah |
| Jan wk 3 – Feb wk 2 | Sector Analyst complete, including real FinBERT news classification and the fundamentals pipeline | Falah |
| Jan wk 2 – Feb wk 2 | Debate Facilitator complete: arbitration logic, evidence-quality weighting, constraint-compliance checking | Mahrukh |
| Feb wk 1–3 | Portfolio Constructor complete: tiebreaker application, full `quant/` integration, audit trail generation | Mahrukh |
| Feb wk 2–4 | Debate protocol hardened: adversarial forcing, protocol-violation handling, kill switch | Mahrukh |
| Jan–Feb | Nightly ingestion jobs, full ticker universe, caching, cost-tiered model routing | Falah + Maryam |
| Feb | Audit trail serialisation and readability scoring | Mahrukh |

**Definition of done (M5):** the full five-agent pipeline runs on real data
for all four investor profiles. Contract, isolation, determinism, grounding
and universe suites all pass. The claim verifier's precision and recall are
measured and documented.

---

## Phase 5 — Frontend, observability, hardening (1–20 Mar 2027)

*(Lead: Maryam.)* Onboarding flow with validation; allocation display with
constraint badges showing which caps bound the result; live agent trace over
SSE; collapsible audit trail with citation popovers; the `/runs` inspector;
desktop and mobile layouts (NFR-06); LangSmith cost tracking per cycle
against the NFR-05 budget; the disclaimer surface; and latency work if
Phase 1's measurement showed the 45s target at risk.

**Definition of done (M6):** a stranger can use the system unaided, on a
phone, and the measured p95 either meets 45s or the breach is documented with
its cause.

---

## Phase 6 — Evaluation (23 Mar – 30 Apr 2027)

**M7 code freeze lands first, on 22 Mar.** After it, no prompt, retriever,
chunking, embedding or schema change without re-running every affected
configuration. Tag the commit.

Four studies, in this order, because the first two are the contribution and
the others depend on a stable system.

**1. The ablation (Maryam, 23 Mar – 15 Apr) — the primary contribution.**
Four configurations on identical inputs: full debate, single monolithic LLM,
multi-agent voting, fixed rule (60% equal-weighted basket / 40% cash). All
three LLM configurations on one fixed backbone with enforcement in
`log_only` mode, so architecture is the variable and not the blocker
(ADR-0003). Measure unmatched-claim rate per configuration; score explanation
quality on a written rubric, blind to condition; manually annotate a random
15% subsample of flagged and unflagged claims with two annotators and report
Cohen's kappa. Report blocked-claim rate separately as a deployment result.

**2. Explainability survey (Mahrukh, 1–25 Apr).** Twenty non-finance
participants, ethics approval already in hand from Phase 0. Within-subject
design — twenty participants across multiple conditions has little power
otherwise. Comprehension questions rather than satisfaction Likerts alone.
Flesch-Kincaid scoring alongside, with the caveat that the metric penalises
unavoidable finance vocabulary.

**3. Backtest (Falah, 23 Mar – 20 Apr).** Four named Pakistani market
windows, walk-forward, strictly point-in-time via `as_of`, survivorship
handled through the universe file's listing dates. Benchmark against KSE-100
total return and the fixed rule. State the two known threats to validity
plainly: the backbone's training data covers the windows, and news coverage
is limited to what archiving captured from Phase 0 onward.

**4. Personalisation (Maryam, 15 Apr – 5 May).** Four profiles across several
market snapshots. Report pairwise allocation distance **and** monotonicity —
more conservative profiles must hold less equity and more cash. Variation
alone is not success; variation in the right direction is.

**Definition of done (M8):** every result committed to
`experiments/results/`, each file recording git SHA, model version, embedding
model, universe version and configuration. No number exists in the thesis
that cannot be regenerated from a committed file.

---

## Phase 7 — Thesis and demo (April – May 2027)

Writing runs in parallel with evaluation; chapters that do not depend on
results should be drafted in April.

| Chapter | Source | Owner |
|---|---|---|
| 1 Introduction | proposal §1–4 | Falah |
| 2 Literature Review | Sep–Oct 2026 review, all three sections | Falah |
| 3 Methodology | SDS + the ADRs + worklogs | Mahrukh |
| 4 Data | data-sources.md, ingestion, universe file | Falah |
| 5 Evaluation | Phase 6 results | Mahrukh |
| 6 Discussion | ADRs for every deviation; limitations | all three |
| 7 Conclusion | — | Falah |

Chapter 6 is where the ADRs and worklogs pay for themselves: every deviation
from the proposal needs a dated justification, and the honest limitations
(training-data contamination, news coverage, FinBERT substitution, English
only, no SECP licensing) read far better stated by us than discovered by a
panel.

Demo preparation, poster and final integration testing run alongside
*(Maryam, May)*.

---

## Critical path

```
news archiving ──────────────────────────────────┐ (expires weekly)
market_view schema → debate → Constructor → full pipeline → freeze → ablation → ch.5
                                                  │
ethics approval ──────────────────────────────────┴→ survey → ch.5
```

Everything evaluative depends on the full pipeline working by **28 Feb**. The
proposal's Gantt allows no slack there, so the realistic buffer has to come
from Phase 5's scope: if February slips, cut frontend polish and the `/runs`
inspector, never the evaluation window.

## Risks, in order of how much they would cost

| Risk | Mitigation | Decide by |
|---|---|---|
| News history unavailable for backtests | Archive from Phase 0; otherwise disable the news channel in backtests and disclose it | 31 Oct 2026 |
| Debate does not converge, or agents restate rather than engage | `market_view.py` gives them something comparable to argue over; rebuttal forcing with violation logging; the tiebreaker guarantees an output either way | Phase 1 |
| Claim verifier false positives make `block` mode unusable | Measure precision and recall on a fixture set in Phase 1, before anything depends on it; the FR-06a/b split limits blocking to numeric and attributed claims | Phase 1 |
| Ethics approval delays recruitment | Started in Phase 0, five months ahead | 31 Oct 2026 |
| Pipeline not ready by 28 Feb | Cut Phase 5 scope, not Phase 6 | 28 Feb 2027 |
| `psxdata` breaks mid-project | Ingest once into our own store; treat the library as a loader, not a dependency | Phase 1 |
| Post-freeze change tempts a re-run | M7 is tagged; `CLAUDE.md` Rule 4 makes the cost explicit | 22 Mar 2027 |
| API costs overrun | NFR-05 needs a number; LangSmith cost tracking from Phase 1 | Phase 0 |

## Running conventions

Each session ends with a worklog in `worklogs/` (`CLAUDE.md`, "Worklogs").
Each decision that overrides the proposal gets an ADR in `docs/adr/`. This
plan is itself revisable — when a phase changes shape, update it and say so
in that session's worklog rather than letting the plan quietly diverge from
what is happening.
