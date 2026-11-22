"""Validate the committed PSX universe YAML file."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).with_name("psx_v1.yaml")
TOP_LEVEL_FIELDS = {"version", "companies", "historical_companies", "cash"}
COMPANY_FIELDS = {
    "ticker",
    "name",
    "sector",
    "board_lot",
    "listed_from",
    "listed_to",
    "data_available",
}
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]*$")


def validate_universe(data: Any) -> list[str]:
    """Return validation errors for a parsed universe document."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["document must be a YAML mapping"]

    missing_top_level = TOP_LEVEL_FIELDS - data.keys()
    extra_top_level = data.keys() - TOP_LEVEL_FIELDS
    if missing_top_level:
        errors.append(f"missing top-level fields: {', '.join(sorted(missing_top_level))}")
    if extra_top_level:
        errors.append(f"unexpected top-level fields: {', '.join(sorted(extra_top_level))}")

    if "version" in data and (not isinstance(data["version"], str) or not data["version"].strip()):
        errors.append("version must be a non-empty string")

    companies = data.get("companies")
    seen_tickers: set[str] = set()
    if not isinstance(companies, list) or not companies:
        errors.append("companies must be a non-empty list")
        companies = []

    for index, company in enumerate(companies):
        prefix = f"companies[{index}]"
        if not isinstance(company, dict):
            errors.append(f"{prefix} must be a mapping")
            continue

        missing_fields = COMPANY_FIELDS - company.keys()
        extra_fields = company.keys() - COMPANY_FIELDS
        if missing_fields:
            errors.append(f"{prefix} missing fields: {', '.join(sorted(missing_fields))}")
        if extra_fields:
            errors.append(f"{prefix} has unexpected fields: {', '.join(sorted(extra_fields))}")

        ticker = company.get("ticker")
        if not isinstance(ticker, str) or not TICKER_PATTERN.fullmatch(ticker):
            errors.append(f"{prefix}.ticker must be uppercase letters, digits, dots, or hyphens")
        elif ticker in seen_tickers:
            errors.append(f"{prefix}.ticker duplicates {ticker}")
        else:
            seen_tickers.add(ticker)

        for field in ("name", "sector"):
            value = company.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{field} must be a non-empty string")

        board_lot = company.get("board_lot")
        if board_lot is not None and (isinstance(board_lot, bool) or not isinstance(board_lot, int) or board_lot <= 0):
            errors.append(f"{prefix}.board_lot must be a positive integer or null")

        for field in ("listed_from", "listed_to"):
            value = company.get(field)
            if value is not None:
                if not isinstance(value, str):
                    errors.append(f"{prefix}.{field} must be an ISO date string or null")
                else:
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        errors.append(f"{prefix}.{field} must be an ISO date string or null")

        available = company.get("data_available")
        if available is not None and not isinstance(available, bool):
            errors.append(f"{prefix}.data_available must be a boolean or null")

    historical = data.get("historical_companies", [])
    if not isinstance(historical, list):
        errors.append("historical_companies must be a list")
        historical = []
    seen_historical: set[str] = set()
    for index, company in enumerate(historical):
        prefix = f"historical_companies[{index}]"
        if not isinstance(company, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        expected = {"ticker", "name", "sector", "listed_from", "listed_to", "status_note"}
        if company.keys() != expected:
            errors.append(f"{prefix} fields must be: {', '.join(sorted(expected))}")
        ticker = company.get("ticker")
        if not isinstance(ticker, str) or not TICKER_PATTERN.fullmatch(ticker):
            errors.append(f"{prefix}.ticker must be uppercase letters, digits, dots, or hyphens")
        elif ticker in seen_tickers or ticker in seen_historical:
            errors.append(f"{prefix}.ticker duplicates {ticker}")
        else:
            seen_historical.add(ticker)
        if not all(isinstance(company.get(field), str) and company[field].strip() for field in ("name", "sector", "status_note")):
            errors.append(f"{prefix} name, sector, and status_note must be non-empty strings")
        for field in ("listed_from", "listed_to"):
            value = company.get(field)
            if value is not None:
                try:
                    if not isinstance(value, str):
                        raise ValueError
                    date.fromisoformat(value)
                except ValueError:
                    errors.append(f"{prefix}.{field} must be an ISO date string or null")

    cash = data.get("cash")
    if not isinstance(cash, dict):
        errors.append("cash must be a mapping")
    elif set(cash) != {"ticker"} or cash.get("ticker") != "CASH":
        errors.append("cash must contain only ticker: CASH")
    elif "CASH" in seen_tickers:
        errors.append("company ticker CASH conflicts with the cash entry")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()

    try:
        data = yaml.safe_load(args.path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"Invalid universe: {exc}", file=sys.stderr)
        return 1

    errors = validate_universe(data)
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Valid universe: {args.path} ({len(data['companies'])} companies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
