# PSX company universe

`psx_v1.yaml` is the versioned ticker list plus the required `CASH` entry.
Each company has a `ticker`, `name`, and supplied `sector`. The required
`board_lot`, `listed_from`, `listed_to`, and `data_available` fields are set
to `null` until their values are confirmed; do not infer them.

Validate from the repository root with:

```powershell
python backend/universe/validate_universe.py
```

The validator checks YAML syntax, required and unexpected fields, ticker
format and uniqueness, non-empty names and sectors, and the types of optional
metadata values. PyYAML is already listed in `backend/requirements.txt`.

Export the canonical sector mapping from `backend/` with:

```powershell
python -m jobs.company_sector_mapping
```

This writes a stable CSV under the gitignored `data/processed/company/`
directory and refreshes `backend/data_manifests/company_sector_mapping.json`.
It does not contact Hugging Face or upload the export. See
`backend/jobs/company_data.md` for normalized data schemas and offline quality
checks.
