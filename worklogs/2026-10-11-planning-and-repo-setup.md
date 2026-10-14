# Session 2026-10-11 — Planning, specification, repo setup

**Who:** Falah Hilal
**Span:** 11–14 Oct 2026

First working session. No application code written by design — the goal was to
turn the proposal into a specification, find what it left unresolved, and get
the repository standing.

## Decisions taken

Four, all recorded in `docs/specs.md` §2. ADRs still to be written.

1. **Ablation runs claim-blocking in `log_only` mode** across all three LLM
   configurations. Overrides the implicit design in proposal FR-06 + §5.
   Reason: blocking removes unmatched claims by construction, so comparing a
   blocked full system against unblocked baselines measures the blocker, not
   the debate — and RQ1 asks about the debate. Blocked-claim rate becomes a
   separate deployment result. → ADR-0003.
2. **FastAPI only; Django dropped.** Overrides proposal §6.1 and §6.4. The
   admin layer is replaced by an internal `/runs` inspector page in the
   frontend. → ADR-0001.
3. **Asset universe is individual PSX tickers plus a `CASH` line**, from a
   committed, versioned YAML file. Resolves an ambiguity rather than
   overriding anything — the proposal says "asset class weights" (FR-09) but
   never names an instrument set. → ADR-0004.
4. **Embedding model chosen by measurement, not assumption.** Proposal §6.4
   specifies FinBERT embeddings; FinBERT is a sentiment classifier and was
   never trained for semantic retrieval. Decision deferred to a recall@k /
   MRR benchmark in `experiments/retrieval_eval/`. FinBERT is kept for
   headline sentiment regardless. → ADR-0002, status *Proposed*.

## Work done

- Read the full proposal; produced a plain-language summary of the five
  agents, their I/O contracts, the three-round debate protocol, and FR-01
  through FR-13.
- Wrote **`CLAUDE.md`** — architecture, tech stack, and ten invariants, each
  with a named enforcement test directory. The two required rules are Rules 1
  (profile isolation) and 2 (claim blocking and logging).
- Wrote **`docs/specs.md`** — system summary, the four decisions with their
  consequences, every problem found grouped by severity, twelve open
  questions, and the full proposed repository layout with rationale.
- **Commit `faf7698`** — `docs: add proposal, working specification, and
  architecture rules`. 3 files, 1273 insertions. Pushed; `main` now tracks
  `origin/main` at `falahhilal/Multi-Agent-Financial-Advisor`. Root commit,
  remote was empty.
- Wrote **`.gitignore`** — notably does *not* ignore `experiments/results/`,
  since those outputs must stay committed for the thesis numbers to be
  reproducible.
- Created `worklogs/` with this convention.

## Problems found

Full list in `docs/specs.md` §3. The four that matter most:

- **The hallucination comparison was unfair by construction** (§3.1a). Fixed
  by decision 1 above. This was the most serious issue in the proposal.
- **Macro and Sector have nothing to disagree about** (§3.1b). They output in
  different spaces — a stance versus sector recommendations — so they can
  neither agree nor contradict each other, and `winning_agent` presumes a
  comparison that is not defined. A shared debate proposition schema
  (`schemas/market_view.py`) is required before the debate protocol means
  anything. **Still unresolved and blocking.**
- **NewsAPI's free tier holds roughly a month of history** (§3.1d). The
  backtests span four historical windows, so news-grounded backtesting is not
  possible as specified. Options: start archiving now, disable the news
  channel in backtests and disclose it, or find another archive.
  **Time-sensitive — the archiving option expires a little more every week.**
- **Backtest lookahead from the backbone's training data** (§3.1e). The model
  knows how 2020 and 2022 turned out. Needs an explicit mitigation and a
  limitations paragraph; not mentioned anywhere in the proposal.

Also worth noting: the Gantt put repository setup and the data pipeline in
Sep–Oct 2026, and the repo had no commits as of 11 Oct. And the enforcement
layer is scheduled Oct–Nov, before any agent exists to exercise it — a thin
fake-agent harness in Nov would fix that.

## Still open

- **Four ADR stubs not written** — `0001-fastapi-only`,
  `0002-embedding-model`, `0003-log-only-ablation`,
  `0004-stock-level-universe`. Decisions are recorded in `specs.md` §2 so
  nothing is lost, but the dated files are the artifact that matters in the
  viva. *(User asked to be reminded about these.)*
- **`.gitignore` is uncommitted.**
- **`schemas/market_view.py` undefined** — blocking the whole debate
  protocol, and three people's FYP-II work sits downstream of it.
- **News archive job not started** — time-sensitive, see above.
- **Twelve open questions** in `docs/specs.md` §4, including four new ones
  raised by the stock-level universe decision: how many tickers, is the list
  fixed or per-run, is KSE-100 constituent history available, and does
  `psxdata` expose per-ticker fundamentals or only sector aggregates.

## Next session should

1. Settle `schemas/market_view.py` — the shared debate proposition. Nothing
   downstream is buildable without it.
2. Start the daily news archive job. Only pays off if it begins now.
3. Write the four ADR stubs and commit them alongside the `.gitignore`.
