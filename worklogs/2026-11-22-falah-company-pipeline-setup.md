# Session 2026-11-22 — company pipeline pre-ingestion setup

**Who:** Falah (project lead; Codex-assisted implementation)

## Decisions taken

- Finalized v1 at the user-supplied 18 active tickers; FATIMA replaces ENGRO
  and NCL replaces ILP. ENGRO remains in historical universe metadata with
  unknown exact listing dates (user direction; documented in the changelog).
- `data/` remains gitignored for payloads; its README is the sole exception.
  Committed manifest templates remain in `backend/data_manifests/`.
- Point-in-time event/fundamental reads require public availability metadata;
  records without it are excluded instead of assigning a date.
- pyPSX is a loader-only optional import. No data retrieval is run as part of
  setup or tests.

## Work done

- Falah requested and reviewed the Stream A changes; Codex updated the
  universe, validator, company schemas, manifest template, price loader
  adapter, point-in-time filters, offline tests, source docs, plan, shared
  instructions, data layout README, and generated sector mapping manifest.
- Ran `python -m jobs.company_sector_mapping` from `backend/`: exported 18
  canonical sector mappings locally. No upload occurred.
- Ran `pytest tests/company tests/universe -q` from `backend/`: 34 passed.
- Ran full `pytest -q` from `backend/`: collection could not complete because
  this environment lacks `beautifulsoup4` and `feedparser`, required by
  existing Stream C test modules.

## Problems found

- Existing source documentation said PSX/pyPSX permission was still pending.
  The user reported written permission for intended PSX/pyPSX collection and
  private HF sharing; docs now state that narrowly, without asserting other
  rights.
- pyPSX sample `adj_factor=1.0`, `tr_close=null`, and `dividend=null` remain
  semantics/quality questions. Initial API availability is not completeness
  or quality evidence.
- Private storage backup/access procedure and source choices for corporate
  actions, fundamentals, sector metadata, and historical listing dates remain
  open.

## Still open

- Verify the exact written permission scope and storage procedure before any
  actual ingestion or sharing.
- Select and validate sources for corporate actions, fundamentals, and
  historical listing metadata.
- Investigate adjustment-factor and total-return fields during a separately
  authorized ingestion/validation phase.

## Next session should

1. Confirm the private data backup/access workflow and source-specific rights.
2. Plan a controlled pyPSX ingestion run with raw snapshot, manifest, and
   quality-report review before treating data as validated.
