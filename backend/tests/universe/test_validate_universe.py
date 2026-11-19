from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

import yaml

UNIVERSE_PATH = Path(__file__).parents[2] / "universe" / "psx_v1.yaml"
VALIDATOR_PATH = UNIVERSE_PATH.with_name("validate_universe.py")
VALIDATOR_SPEC = importlib.util.spec_from_file_location("validate_universe", VALIDATOR_PATH)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR)
validate_universe = VALIDATOR.validate_universe


def load_universe() -> dict[str, object]:
    return yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))


def test_committed_universe_is_valid() -> None:
    assert validate_universe(load_universe()) == []


def test_duplicate_ticker_is_detected() -> None:
    data = load_universe()
    data["companies"].append(deepcopy(data["companies"][0]))

    assert any("duplicates HBL" in error for error in validate_universe(data))


def test_malformed_ticker_is_detected() -> None:
    data = load_universe()
    data["companies"][0]["ticker"] = "hbl"

    assert any("ticker must be uppercase" in error for error in validate_universe(data))


def test_missing_required_field_is_detected() -> None:
    data = load_universe()
    del data["companies"][0]["sector"]

    assert any("missing fields: sector" in error for error in validate_universe(data))
