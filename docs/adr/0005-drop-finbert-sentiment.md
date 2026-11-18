# ADR-0005: Drop FinBERT headline sentiment

- **Status:** Proposed (needs agreement from Falah and Mahrukh)
- **Date:** 2026-11-14
- **Proposed by:** Maryam Asif (news stream owner)
- **Overrides:** proposal FR-05 ("…and FinBERT-classified news"), §6 component
  and tool tables (FinBERT sentiment pipeline), and the Jan–Feb 2027 timeline
  item "Sector Analyst Agent: FinBERT news classification".
- **Does not change:** ADR-0002 (embedding model chosen by benchmark). That
  is about retrieval embeddings, not sentiment.

## Context

The proposal has the Sector Analyst reason over "FinBERT-classified news":
each English headline is labelled positive, negative or neutral by
`ProsusAI/finbert`, and the agent receives those labels.

FinBERT was trained on English financial text that is not Pakistani
(Financial PhraseBank and similar). Nothing in the project measures how well
it reads PSX news.

The job was built and run once (2026-11-09, `backend/jobs/news_finbert.py`,
5,403 headlines/articles, dataset `news/sentiment/finbert_0bc2ac10_t128/`).
What that run showed:

- Aaj's market reports were almost never labelled neutral (2%, against 35% for
  GDELT headlines and 36% for RSS).
- Non-market stories in the RSS feeds (sport, greetings, awards) were
  labelled too, often positive.
- Most historical coverage (GDELT, 2017 onward) is headline-only.

**The planned hand spot-check (60 headlines) was not done.** So FinBERT's
accuracy on Pakistani news is unmeasured: this decision rests on the
training-data mismatch and the symptoms above, not on a measured error rate.

## Decision

Do not use FinBERT sentiment labels anywhere in the system.

- News reaches the agents only as **retrieved passages with citations**,
  like every other document.
- No precomputed sentiment score or label is given to any agent.
- If the Sector Analyst describes market mood, it does so in its own words
  from retrieved passages, and that text goes through claim enforcement like
  any other output (Rule 2: qualitative claims are logged and flagged).

## Consequences

**Simpler**
- One less model, pipeline and dataset to validate, explain and maintain.
- No unvalidated classifier feeding the agents.
- Step 1 no longer depends on a FinBERT spot-check.

**Costs and risks**
- The proposal promised FinBERT (FR-05, High priority). The thesis must say
  it was dropped and why (Chapter 6, deviations).
- FinBERT gave a fixed, repeatable number that could not see the future.
  Any mood judgement now comes from the LLM, whose training data covers the
  backtest windows. This adds to the lookahead limitation already listed
  under Rule 6; it must be stated, not hidden.
- "Not trained on Pakistani news" is a weaker justification in a viva than a
  measured agreement rate. The 60-row sample
  (`spotcheck_sample.csv`) stays available if the team wants the number.

**Not affected**
- The news datasets themselves (`news_rss_articles`, `news_aaj_psx_archive`,
  `news_gdelt_index`): they are the retrieval corpus regardless.
- RQ1–RQ3: none measures sentiment accuracy.
- The ablation: all configurations still receive the same retrieved context.

## What changes if accepted

1. `CLAUDE.md` and `AGENTS.md`: remove "FinBERT news" from the Sector
   Analyst inputs (agents tables), and the sentence requiring FinBERT for
   headline sentiment classification.
2. `docs/specs.md` and `docs/plan.md`: remove FinBERT sentiment from the
   Sector Analyst inputs, Stream C item 7, the Step 1 "done when" condition,
   and the proposed `sentiment/finbert.py` module.
3. `sentiment_summary` in the Sector Analyst output: decide in
   `schemas/market_view.py` whether it stays as agent-written prose or is
   removed. **Open, for the team.**
4. `backend/jobs/news_finbert.py` and the dataset on HF: kept as a record,
   marked "not used (ADR-0005)"; the job is not run again. Nothing is deleted.
5. `docs/data-sources.md`: drop "FinBERT sentiment" from the dataset purposes.

Until this ADR is accepted, none of the above is changed.

## Alternatives considered

- **Keep FinBERT, validate with the spot-check** (about 15 minutes of
  labelling). Would give a measured agreement rate either way. Not chosen by
  the news stream owner; still open to the team.
- **Keep FinBERT as an optional, unvalidated feature.** Rejected: an
  unvalidated signal in the agent inputs is worse than none.
- **Fine-tune or swap in a model trained on Pakistani financial text.** Out
  of scope: no labelled Pakistani dataset exists in the project.
