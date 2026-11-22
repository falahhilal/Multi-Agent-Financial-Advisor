from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from jobs import common
from jobs.company_sector_mapping import export_sector_mapping

UNIVERSE_PATH = Path(__file__).parents[2] / "universe" / "psx_v1.yaml"


def test_export_is_deterministic_and_writes_local_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "sector_mapping.csv"
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")

    first_output, manifest_path = export_sector_mapping(UNIVERSE_PATH, output)
    first_bytes = first_output.read_bytes()
    second_output, second_manifest = export_sector_mapping(UNIVERSE_PATH, output)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with second_output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert first_bytes == second_output.read_bytes()
    assert manifest_path == second_manifest
    assert manifest_path.name == "company_sector_mapping.json"
    assert manifest["row_count"] == 18
    assert manifest["file_count"] == 1
    assert manifest["hf_path"] is None
    assert manifest["extra"]["storage_status"] == "local_generated_not_uploaded"
    assert manifest["extra"]["universe_version"] == "psx_v1"
    assert len(rows) == 18
    assert {row["ticker"] for row in rows} == {
        row["ticker"]
        for row in yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))["companies"]
    }


def test_export_rejects_a_v1_universe_missing_a_company(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))
    data["companies"].pop()
    changed_universe = tmp_path / "psx_v1.yaml"
    changed_universe.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")

    with pytest.raises(ValueError, match="exactly 18 companies"):
        export_sector_mapping(changed_universe, tmp_path / "mapping.csv")

    assert not (tmp_path / "mapping.csv").exists()
