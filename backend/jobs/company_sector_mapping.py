"""Export the canonical YAML sector mapping as a deterministic local CSV."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from jobs import common
from jobs.company_quality import validate_sector_mapping
from jobs.company_schemas import SectorMappingRow
from universe.validate_universe import validate_universe

UNIVERSE_PATH = common.BACKEND_DIR / "universe" / "psx_v1.yaml"
OUTPUT_PATH = common.DATA_DIR / "processed" / "company" / "sector_mapping.csv"
EXPECTED_V1_COMPANY_COUNT = 18
CSV_FIELDS = ("universe_version", "ticker", "name", "sector", "source")


def load_sector_mapping(path: str | Path = UNIVERSE_PATH) -> tuple[str, list[SectorMappingRow]]:
    """Load and validate the canonical sector rows from the versioned YAML."""
    source_path = Path(path)
    data: Any = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    errors = validate_universe(data)
    if errors:
        raise ValueError("invalid universe: " + "; ".join(errors))
    if data["version"] == "psx_v1" and len(data["companies"]) != EXPECTED_V1_COMPANY_COUNT:
        raise ValueError(f"psx_v1 must contain exactly {EXPECTED_V1_COMPANY_COUNT} companies")

    rows = [
        SectorMappingRow(
            universe_version=data["version"],
            ticker=company["ticker"],
            name=company["name"],
            sector=company["sector"],
            source="backend/universe/psx_v1.yaml",
        )
        for company in data["companies"]
    ]
    report = validate_sector_mapping(rows, {row.ticker for row in rows})
    if report.issues:
        raise ValueError("invalid sector mapping: " + "; ".join(i.message for i in report.issues))
    return data["version"], rows


def export_sector_mapping(
    universe_path: str | Path = UNIVERSE_PATH,
    output_path: str | Path = OUTPUT_PATH,
) -> tuple[Path, Path]:
    """Write a stable CSV and its local manifest; no network or HF calls occur."""
    version, rows = load_sector_mapping(universe_path)
    expected = {row.ticker for row in rows}
    report = validate_sector_mapping(rows, expected)
    if len(rows) != EXPECTED_V1_COMPANY_COUNT or report.issues:
        raise ValueError("sector mapping must provide exactly one row for each of the 18 v1 tickers")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(row.model_dump(mode="json") for row in rows)
    os.replace(temporary, output)

    manifest = common.write_manifest(
        stream="company",
        dataset="sector_mapping",
        source="Project-supplied canonical sector labels in backend/universe/psx_v1.yaml",
        fetch_mode="reproducible",
        row_count=len(rows),
        file_count=1,
        files=[output],
        notes=(
            "Deterministic reference export only. Generated from the committed universe; "
            "no external data was collected and the file has not been uploaded to Hugging Face."
        ),
        extra={
            "universe_version": version,
            "ticker_coverage": sorted(expected),
            "storage_status": "local_generated_not_uploaded",
        },
        script="backend/jobs/company_sector_mapping.py",
    )
    return output, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=UNIVERSE_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    try:
        output, manifest = export_sector_mapping(args.universe, args.output)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"Sector mapping export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported 18 sector mappings: {output}")
    print(f"Manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
