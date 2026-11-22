"""Collect pyPSX corporate actions and annual statements for company Stream A."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Sequence, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd
from huggingface_hub import HfApi, hf_hub_download
from pydantic import BaseModel

from jobs import common
from jobs.company_as_of import corporate_action_available_date, fundamental_available_at
from jobs.company_quality import (
    QualityIssue,
    QualityReport,
    TickerQualityReport,
    build_per_ticker_reports,
    validate_corporate_actions,
    validate_dataset_coverage,
    validate_fundamentals,
    validate_prices,
    write_per_ticker_reports,
    write_quality_report,
)
from jobs.company_pypsx import HistoricalFrame, _json_safe, archive_frame
from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV
from jobs.company_ingest import read_active_universe

log = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Karachi")
MODEL = TypeVar("MODEL", bound=BaseModel)


def _source_text(value: object) -> str | None:
    safe = _json_safe(value)
    return None if safe is None else str(safe)


def _source_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    safe = _json_safe(value)
    if safe is None:
        return None
    try:
        parsed = Decimal(str(safe))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _source_date(value: object) -> date | None:
    if value is None:
        return None
    safe = _json_safe(value)
    if safe is None:
        return None
    if isinstance(safe, datetime):
        if safe.tzinfo is not None and safe.utcoffset() is not None:
            return safe.astimezone(MARKET_TZ).date()
        return safe.date()
    if isinstance(safe, date):
        return safe
    if isinstance(safe, str):
        candidate = safe.strip()
        if not candidate:
            return None
        try:
            if len(candidate) == 10:
                return date.fromisoformat(candidate)
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(MARKET_TZ).date()
    return parsed.date()


def _source_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    safe = _json_safe(value)
    if isinstance(safe, datetime):
        if safe.tzinfo is None or safe.utcoffset() is None:
            return None
        return safe.astimezone(timezone.utc)
    if not isinstance(safe, str) or not safe.strip():
        return None
    try:
        parsed = datetime.fromisoformat(safe.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _provider_fields(row: dict[str, object]) -> dict[str, str | None]:
    return {str(key): _source_text(value) for key, value in row.items()}


def normalize_action_rows(
    rows: Sequence[dict[str, object]], ticker: str, fetched_at: datetime
) -> tuple[tuple[CorporateAction, ...], list[str]]:
    """Normalize documented ex-date events while retaining all source fields."""
    kind_map = {
        "dividend": "cash_dividend",
        "bonus": "bonus_issue",
        "split": "stock_split",
        "right": "rights_issue",
        "rights": "rights_issue",
    }
    output: list[CorporateAction] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        symbol = _source_text(row.get("symbol"))
        if symbol not in {ticker, None}:
            errors.append(f"row {index}: returned symbol mismatch")
            continue
        event_date = _source_date(row.get("ex_date"))
        if event_date is None:
            errors.append(f"row {index}: missing or invalid ex_date")
            continue
        source_kind = (_source_text(row.get("kind")) or "").strip().lower()
        action_type = kind_map.get(source_kind, "other")
        dividend = _source_decimal(row.get("dividend_rs"))
        amount = dividend if action_type == "cash_dividend" and dividend is not None and dividend > 0 else None
        currency = "PKR" if amount is not None else None
        source_id = next(
            (_source_text(row.get(key)) for key in ("event_id", "action_id", "record_id", "id") if row.get(key) is not None),
            None,
        )
        url = next(
            (_source_text(row.get(key)) for key in ("source_url", "url") if row.get(key) is not None),
            None,
        )
        try:
            output.append(CorporateAction(
                ticker=ticker,
                action_type=action_type,
                announcement_date=_source_date(row.get("announcement_date")),
                published_at=_source_datetime(row.get("published_at")),
                available_date=_source_date(row.get("available_date")),
                ex_date=event_date,
                record_date=_source_date(row.get("record_date")),
                payment_date=_source_date(row.get("payment_date")),
                amount_per_share=amount,
                currency=currency,
                provider_price_factor=_source_decimal(row.get("price_factor")),
                provider_total_factor=_source_decimal(row.get("total_factor")),
                provider_applied=row.get("applied") if isinstance(row.get("applied"), bool) else None,
                provider_fields=_provider_fields(row),
                source="pyPSX",
                source_endpoint="pypsx.get_corporate_actions",
                source_url=url,
                source_record_id=source_id,
                fetched_at=fetched_at,
            ))
        except (TypeError, ValueError) as exc:
            errors.append(f"row {index}: normalization validation failed ({type(exc).__name__})")
    return tuple(output), errors


def _metric_slug(item: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_")
    if not slug:
        slug = "item"
    if not slug[0].isalpha():
        slug = "item_" + slug
    return slug[:64]


def _period_year(value: object) -> int | None:
    text = _source_text(value)
    if text is None:
        return None
    match = re.fullmatch(r"(\d{4})(?:\.0+)?", text.strip())
    return int(match.group(1)) if match else None


def normalize_fundamental_rows(
    rows: Sequence[dict[str, object]],
    ticker: str,
    fetched_at: datetime,
    *,
    from_year: int = 2015,
) -> tuple[tuple[CompanyFundamental, ...], list[str]]:
    """Keep source fiscal labels, statement/item names, and unknown availability."""
    output: list[CompanyFundamental] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        year = _period_year(row.get("year"))
        statement = _source_text(row.get("statement"))
        item = _source_text(row.get("item"))
        if year is None or not statement or not item:
            errors.append(f"row {index}: missing or unsupported year/statement/item")
            continue
        if year < from_year:
            continue
        value = _source_decimal(row.get("value"))
        try:
            output.append(CompanyFundamental(
                ticker=ticker,
                metric=_metric_slug(item),
                source_item=item,
                statement_type=statement,
                value=value,
                unit=None,
                currency=None,
                period_type="annual",
                period_label=str(year),
                period_end=None,
                report_date=None,
                published_at=None,
                available_date=None,
                basis="reported",
                provider_fields=_provider_fields(row),
                source="pyPSX",
                source_endpoint="pypsx.get_fundamentals",
                source_record_id=next(
                    (_source_text(row.get(key)) for key in ("id", "record_id") if row.get(key) is not None),
                    None,
                ),
                fetched_at=fetched_at,
            ))
        except (TypeError, ValueError) as exc:
            errors.append(f"row {index}: normalization validation failed ({type(exc).__name__})")
    return tuple(output), errors


def _write_records(records: Sequence[MODEL], path: Path, model: type[MODEL]) -> Path:
    if not records:
        raise ValueError("refusing to write an empty normalized Parquet file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    frame = pd.DataFrame([record.model_dump(mode="python") for record in records])
    frame.to_parquet(temporary, index=False, engine="pyarrow")
    restored = pd.read_parquet(temporary, engine="pyarrow")
    parsed = tuple(model.model_validate(row) for row in restored.to_dict("records"))
    if parsed != tuple(records):
        temporary.unlink(missing_ok=True)
        raise ValueError("Parquet round-trip did not preserve normalized records")
    temporary.replace(path)
    return path


def _atomic_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _collect_one(
    method: Callable[[str], HistoricalFrame],
    ticker: str,
    raw_directory: Path,
    *,
    retries: int,
    pause: Callable[[float], None],
    request_interval: float,
) -> tuple[HistoricalFrame | None, Path | None, str | None, datetime]:
    for attempt in range(retries + 1):
        try:
            frame = method(ticker)
            fetched_at = datetime.now(timezone.utc)
            path, _ = archive_frame(frame, ticker, raw_directory)
            return frame, path, None, fetched_at
        except Exception as exc:
            if attempt < retries:
                pause(request_interval * (2**attempt))
            else:
                log.error("pyPSX company dataset failed for %s (%s)", ticker, type(exc).__name__)
                return None, None, type(exc).__name__, datetime.now(timezone.utc)
    return None, None, "RetryExhausted", datetime.now(timezone.utc)


def _rows(frame: HistoricalFrame | None) -> tuple[dict[str, object], ...]:
    return tuple(frame.to_dict("records")) if frame is not None else ()


def _date_range(records: Sequence[CorporateAction]) -> tuple[date | None, date | None]:
    dates = [row.ex_date for row in records]
    return (min(dates), max(dates)) if dates else (None, None)


def _fundamentals_coverage(records: Sequence[CompanyFundamental]) -> dict[str, object]:
    by_ticker: dict[str, object] = {}
    groups: dict[str, list[CompanyFundamental]] = defaultdict(list)
    for record in records:
        groups[record.ticker].append(record)
    for ticker, ticker_records in sorted(groups.items()):
        by_year: dict[str, dict[str, object]] = {}
        for year in sorted({row.period_label or "unknown" for row in ticker_records}):
            year_rows = [row for row in ticker_records if row.period_label == year]
            by_year[year] = {
                "row_count": len(year_rows),
                "statement_counts": dict(sorted(Counter(row.statement_type or "unknown" for row in year_rows).items())),
                "items_by_statement": {
                    statement: sorted({row.source_item or row.metric for row in year_rows if row.statement_type == statement})
                    for statement in sorted({row.statement_type or "unknown" for row in year_rows})
                },
                "null_value_rows": sum(row.value is None for row in year_rows),
                "unit_supplied_rows": sum(row.unit is not None for row in year_rows),
                "currency_supplied_rows": sum(row.currency is not None for row in year_rows),
                "publication_date_rows": sum(fundamental_available_at(row) is not None for row in year_rows),
            }
        by_ticker[ticker] = by_year
    return by_ticker


def _copy_stage(files: Sequence[tuple[Path, Path]], stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=False)
    for source, relative in files:
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        if common.sha256_file(source) != common.sha256_file(target):
            raise OSError(f"staged file checksum mismatch: {source.name}")


def _upload_and_verify(dataset: str, files: Sequence[tuple[Path, Path]], run_id: str) -> tuple[str, str]:
    api = HfApi(token=common.get_secret("HF_TOKEN"))
    info = api.repo_info(repo_id=common.HF_REPO, repo_type="dataset")
    if info.private is not True:
        raise RuntimeError("configured Hugging Face dataset is not private")
    remote_root = f"company/{dataset}/"
    with tempfile.TemporaryDirectory(dir=common.DATA_DIR / "interim") as temp_root:
        stage = Path(temp_root) / dataset
        _copy_stage(files, stage)
        data_revision = common.upload_to_hf(
            stage, remote_root, stream="company",
            commit_message=f"company: publish pyPSX {dataset} {run_id}",
        )
    return data_revision, remote_root


def _verify_remote(dataset: str, files: Sequence[tuple[Path, Path]], revision: str) -> None:
    api = HfApi(token=common.get_secret("HF_TOKEN"))
    paths = [f"company/{dataset}/{relative.as_posix()}" for _, relative in files]
    remote = api.get_paths_info(repo_id=common.HF_REPO, paths=paths, repo_type="dataset")
    by_path = {item.path: item for item in remote}
    for path, (local, _) in zip(paths, files, strict=True):
        item = by_path.get(path)
        if item is None or item.size != local.stat().st_size:
            raise RuntimeError(f"Hugging Face size verification failed: {path}")
        digest = common.sha256_file(local)
        lfs_sha = getattr(getattr(item, "lfs", None), "sha256", None)
        if lfs_sha:
            if lfs_sha != digest:
                raise RuntimeError(f"Hugging Face LFS checksum mismatch: {path}")
        else:
            downloaded = hf_hub_download(
                repo_id=common.HF_REPO, repo_type="dataset", filename=path,
                revision=revision, token=common.get_secret("HF_TOKEN"),
            )
            if common.sha256_file(Path(downloaded)) != digest:
                raise RuntimeError(f"Hugging Face content checksum mismatch: {path}")


def collect_company_auxiliary(
    *,
    start: date = date(2015, 1, 1),
    end: date = date(2026, 11, 20),
    retries: int = 2,
    requests_per_second: float = 0.25,
    upload: bool = True,
    tickers: Sequence[str] | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Fetch actions/statements; price records are read locally and never refetched."""
    if start > end or retries < 0 or requests_per_second <= 0:
        raise ValueError("invalid date range, retry count, or request rate")
    active = tuple(tickers or read_active_universe())
    if len(active) != 18 or len(set(active)) != 18 or any(t != t.upper() for t in active):
        raise ValueError("auxiliary collection requires the canonical 18 uppercase active tickers")
    if upload:
        common.get_secret("PYPSX_API_KEY_ID")
        common.get_secret("PYPSX_API_SECRET_KEY")
        common.get_secret("HF_TOKEN")
        api = HfApi(token=common.get_secret("HF_TOKEN"))
        if api.repo_info(repo_id=common.HF_REPO, repo_type="dataset").private is not True:
            raise RuntimeError("configured Hugging Face dataset is not private")

    import pypsx  # type: ignore[import-not-found]  # optional, loader-only dependency

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    interval = 1 / requests_per_second
    last_request = [0.0]

    def paced_call(method: Callable[[str], HistoricalFrame], ticker: str, raw_path: Path):
        wait = interval - (time.monotonic() - last_request[0])
        if last_request[0] and wait > 0:
            pause(wait)
        result = _collect_one(method, ticker, raw_path, retries=retries, pause=pause, request_interval=interval)
        last_request[0] = time.monotonic()
        return result

    actions_root = common.DATA_DIR / "corporate_actions"
    fundamentals_root = common.DATA_DIR / "fundamentals"
    actions_raw = common.DATA_DIR / "raw" / "corporate_actions"
    fundamentals_raw = common.DATA_DIR / "raw" / "fundamentals"
    actions_raw.mkdir(parents=True, exist_ok=True)
    fundamentals_raw.mkdir(parents=True, exist_ok=True)
    actions_stage = actions_root / "normalized"
    fundamentals_stage = fundamentals_root / "normalized"
    run_dir_actions = actions_root / "quality_reports" / run_id
    run_dir_fundamentals = fundamentals_root / "quality_reports" / run_id
    actions_stats: dict[str, dict[str, object]] = {}
    fundamentals_stats: dict[str, dict[str, object]] = {}
    all_actions: list[CorporateAction] = []
    all_fundamentals: list[CompanyFundamental] = []
    action_files: list[Path] = []
    fundamental_files: list[Path] = []
    raw_action_files: list[Path] = []
    raw_fundamental_files: list[Path] = []

    for ticker in active:
        actions_frame, actions_path, actions_error, actions_fetched_at = paced_call(
            lambda symbol: pypsx.get_corporate_actions(
                symbol, start=start.isoformat(), end=end.isoformat(), include_quarantined=True
            ),
            ticker,
            actions_raw,
        )
        action_rows = _rows(actions_frame)
        actions, action_errors = normalize_action_rows(action_rows, ticker, actions_fetched_at)
        action_type_counts = dict(sorted(Counter(a.action_type for a in actions).items()))
        all_actions.extend(actions)
        raw_action_files.extend([Path(actions_path)] if actions_path else [])
        first_action, last_action = _date_range(actions)
        actions_stats[ticker] = {
            "request_status": "failed" if actions_error else "empty" if not action_rows else "complete",
            "source_row_count": len(action_rows), "normalized_row_count": len(actions),
            "raw_path": actions_path.relative_to(common.DATA_DIR).as_posix() if actions_path else None,
            "raw_sha256": common.sha256_file(Path(actions_path)) if actions_path else None,
            "actual_first_ex_date": first_action.isoformat() if first_action else None,
            "actual_last_ex_date": last_action.isoformat() if last_action else None,
            "event_type_counts": action_type_counts,
            "public_availability_rows": sum(corporate_action_available_date(a) is not None for a in actions),
            "errors": ([actions_error] if actions_error else []) + action_errors,
        }
        pause(interval)

        fundamentals_frame, fundamentals_path, fundamentals_error, fundamentals_fetched_at = paced_call(
            lambda symbol: pypsx.get_fundamentals(symbol, wide=False),
            ticker,
            fundamentals_raw,
        )
        fundamental_rows = _rows(fundamentals_frame)
        facts, fundamental_errors = normalize_fundamental_rows(
            fundamental_rows, ticker, fundamentals_fetched_at, from_year=start.year
        )
        all_fundamentals.extend(facts)
        raw_fundamental_files.extend([Path(fundamentals_path)] if fundamentals_path else [])
        by_year = Counter(fact.period_label for fact in facts)
        by_statement = Counter(fact.statement_type for fact in facts)
        fundamentals_stats[ticker] = {
            "request_status": "failed" if fundamentals_error else "empty" if not fundamental_rows else "complete",
            "source_row_count_all_years": len(fundamental_rows),
            "normalized_row_count_target_range": len(facts),
            "raw_path": fundamentals_path.relative_to(common.DATA_DIR).as_posix() if fundamentals_path else None,
            "raw_sha256": common.sha256_file(Path(fundamentals_path)) if fundamentals_path else None,
            "actual_first_year": min(by_year) if by_year else None,
            "actual_last_year": max(by_year) if by_year else None,
            "row_count_by_year": dict(sorted(by_year.items())),
            "row_count_by_statement": dict(sorted(by_statement.items())),
            "source_item_count": len({fact.source_item for fact in facts}),
            "null_value_rows": sum(fact.value is None for fact in facts),
            "unit_supplied_rows": sum(fact.unit is not None for fact in facts),
            "currency_supplied_rows": sum(fact.currency is not None for fact in facts),
            "publication_date_rows": sum(fundamental_available_at(fact) is not None for fact in facts),
            "errors": ([fundamentals_error] if fundamentals_error else []) + fundamental_errors,
        }
        pause(interval)

    # Merge by source event/fiscal identity, ignoring only the fetch timestamp.
    from jobs.company_normalization import DuplicateRecordError, merge_records
    stored_actions: list[CorporateAction] = []
    stored_fundamentals: list[CompanyFundamental] = []
    for ticker in active:
        action_path = actions_stage / f"ticker={ticker}" / "events.parquet"
        new_actions = [a for a in all_actions if a.ticker == ticker]
        existing_actions = tuple(CorporateAction.model_validate(r) for r in pd.read_parquet(action_path).to_dict("records")) if action_path.exists() else ()
        merged = None
        if new_actions:
            try:
                merged = merge_records(existing_actions, new_actions,
                    key_fields=("ticker", "action_type", "ex_date", "provider_fields"), ignore_fields=("fetched_at",))
                revised = [list(key) for key in merged.revised_keys]
                _write_records(merged.records, action_path, CorporateAction)
                actions_stats[ticker]["revised_keys"] = revised
            except DuplicateRecordError:
                actions_stats[ticker]["errors"].append("conflicting corporate-action identities require review")
        elif existing_actions:
            merged = None
        if new_actions:
            stored_actions.extend(merged.records if merged is not None else existing_actions)
        else:
            stored_actions.extend(existing_actions)
        if action_path.is_file():
            action_files.append(action_path)
        fundamental_path = fundamentals_stage / f"ticker={ticker}" / "statements.parquet"
        new_facts = [f for f in all_fundamentals if f.ticker == ticker]
        existing_facts = tuple(CompanyFundamental.model_validate(r) for r in pd.read_parquet(fundamental_path).to_dict("records")) if fundamental_path.exists() else ()
        merged_facts = None
        if new_facts:
            try:
                merged_facts = merge_records(existing_facts, new_facts,
                    key_fields=("ticker", "statement_type", "source_item", "period_label"), ignore_fields=("fetched_at",))
                revised_facts = [list(key) for key in merged_facts.revised_keys]
                _write_records(merged_facts.records, fundamental_path, CompanyFundamental)
                fundamentals_stats[ticker]["revised_keys"] = revised_facts
            except DuplicateRecordError:
                fundamentals_stats[ticker]["errors"].append("conflicting fundamental identities require review")
        if new_facts:
            stored_fundamentals.extend(merged_facts.records if merged_facts is not None else existing_facts)
        else:
            stored_fundamentals.extend(existing_facts)
        if fundamental_path.is_file():
            fundamental_files.append(fundamental_path)

    action_report = validate_corporate_actions(
        [row.model_dump(mode="python") for row in stored_actions], set(active)
    )
    fundamental_report = validate_fundamentals(
        [row.model_dump(mode="python") for row in stored_fundamentals], set(active)
    )
    prices_report_path = next((common.DATA_DIR / "quality_reports" / "company_prices").glob("*/dataset_prices.json"))
    prices_report = QualityReport.model_validate(json.loads(prices_report_path.read_text(encoding="utf-8")))
    coverage = validate_dataset_coverage(
        {
            "prices": prices_report.ticker_coverage,
            "corporate_actions": sorted({row.ticker for row in all_actions}),
            "fundamentals": sorted({row.ticker for row in all_fundamentals}),
        },
        set(active),
        required_tickers_by_dataset={"prices": set(active), "corporate_actions": set(), "fundamentals": set(active)},
    )
    reports = (prices_report, action_report, fundamental_report, coverage)
    ticker_reports = build_per_ticker_reports(reports, set(active))

    action_quality_files = [
        write_quality_report(action_report, run_dir_actions / "dataset_quality.json"),
        write_quality_report(coverage, run_dir_actions / "ticker_coverage.json"),
        *write_per_ticker_reports(ticker_reports, run_dir_actions / "tickers"),
    ]
    fundamental_quality_files = [
        write_quality_report(fundamental_report, run_dir_fundamentals / "dataset_quality.json"),
        write_quality_report(coverage, run_dir_fundamentals / "ticker_coverage.json"),
        *write_per_ticker_reports(ticker_reports, run_dir_fundamentals / "tickers"),
    ]
    action_type_coverage = dict(sorted(Counter(row.action_type for row in stored_actions).items()))
    fundamental_coverage = _fundamentals_coverage(stored_fundamentals)
    action_summary = {
        "run_id": run_id, "dataset": "corporate_actions", "source": "pyPSX",
        "source_endpoint": "pypsx.get_corporate_actions", "requested_start": start.isoformat(),
        "requested_end": end.isoformat(), "fetched_at": datetime.now(timezone.utc).isoformat(),
        "collection_status": "partial" if any(v["request_status"] != "complete" for v in actions_stats.values()) else "complete",
        "event_type_counts": action_type_coverage, "per_ticker": actions_stats,
        "normalized_row_count": len(stored_actions),
        "public_availability_rows": sum(corporate_action_available_date(a) is not None for a in stored_actions),
        "not_evaluated": action_report.not_evaluated,
        "price_rejection_date_overlap": _price_action_overlap(all_actions),
        "adjusted_prices_created": False,
    }
    fundamental_summary = {
        "run_id": run_id, "dataset": "fundamentals", "source": "pyPSX",
        "source_endpoint": "pypsx.get_fundamentals(statement=None, wide=False)",
        "requested_start_year": start.year, "requested_end_date": end.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "collection_status": "partial" if any(v["request_status"] != "complete" for v in fundamentals_stats.values()) else "complete",
        "per_ticker": fundamentals_stats, "coverage_by_ticker_year_statement": fundamental_coverage,
        "normalized_row_count": len(stored_fundamentals),
        "publication_date_rows": sum(fundamental_available_at(f) is not None for f in stored_fundamentals),
        "not_evaluated": fundamental_report.not_evaluated,
        "point_in_time_status": "unavailable for source rows without actual publication/availability dates",
    }
    action_run = _atomic_json(actions_root / "runs" / f"{run_id}.json", action_summary)
    fundamental_run = _atomic_json(fundamentals_root / "runs" / f"{run_id}.json", fundamental_summary)
    normalized_action_files = list(action_files)
    normalized_fundamental_files = list(fundamental_files)
    action_files = list(dict.fromkeys([*raw_action_files, *action_files, *action_quality_files, action_run]))
    fundamental_files = list(dict.fromkeys([*raw_fundamental_files, *fundamental_files, *fundamental_quality_files, fundamental_run]))

    # Confirm no pyPSX API has been called for prices: this job only adds the two new datasets.
    action_manifest = common.write_manifest(
        stream="company", dataset="company_actions", source="pyPSX History archive get_corporate_actions",
        fetch_mode="reproducible", row_count=len(stored_actions), file_count=len(action_files),
        date_range=_date_range(stored_actions), notes="Ex-date events; no announcement/public-availability date was supplied in the inspected response.",
        files=action_files,
        extra={"schema_version": "company_actions_v1", "collection_status": action_summary["collection_status"],
               "requested_start": start.isoformat(), "requested_end": end.isoformat(),
               "event_type_counts": action_type_coverage, "per_ticker": actions_stats,
               "not_evaluated": action_report.not_evaluated, "adjusted_price_series": "not created"},
        script="backend/jobs/company_aux_ingest.py",
    )
    fundamental_manifest = common.write_manifest(
        stream="company", dataset="company_fundamentals", source="pyPSX fundamentals get_fundamentals",
        fetch_mode="reproducible", row_count=len(stored_fundamentals), file_count=len(fundamental_files),
        notes="Yearly reported statement items; source response has no publication/availability date, units or currency.",
        files=fundamental_files,
        extra={"schema_version": "company_fundamentals_v1", "collection_status": fundamental_summary["collection_status"],
               "requested_start_year": start.year, "per_ticker": fundamentals_stats,
               "coverage_by_ticker_year_statement": fundamental_coverage,
               "not_evaluated": fundamental_report.not_evaluated,
               "point_in_time_status": fundamental_summary["point_in_time_status"]},
        script="backend/jobs/company_aux_ingest.py",
    )

    outputs: dict[str, object] = {}
    for dataset, root, manifest, included in (
        ("corporate_actions", actions_root, action_manifest,
         [(p, Path("raw") / p.name) for p in raw_action_files]
         + [(p, Path("normalized") / p.relative_to(actions_stage)) for p in normalized_action_files]
         + [(p, Path("quality_reports") / run_id / p.relative_to(run_dir_actions)) for p in action_quality_files]
         + [(action_run, Path("runs") / action_run.name)]),
        ("fundamentals", fundamentals_root, fundamental_manifest,
         [(p, Path("raw") / p.name) for p in raw_fundamental_files]
         + [(p, Path("normalized") / p.relative_to(fundamentals_stage)) for p in normalized_fundamental_files]
         + [(p, Path("quality_reports") / run_id / p.relative_to(run_dir_fundamentals)) for p in fundamental_quality_files]
         + [(fundamental_run, Path("runs") / fundamental_run.name)]),
    ):
        local_copy = root / "manifest.json"
        local_copy.parent.mkdir(parents=True, exist_ok=True)
        local_copy.write_bytes(manifest.read_bytes())
        included = [*included, (local_copy, Path("manifest.json"))]
        data_revision = None
        remote_root = f"company/{dataset}/"
        if upload:
            data_revision, remote_root = _upload_and_verify(dataset, included, run_id)
            common.write_manifest(
                stream="company", dataset="company_actions" if dataset == "corporate_actions" else "company_fundamentals",
                source="pyPSX History archive get_corporate_actions" if dataset == "corporate_actions" else "pyPSX fundamentals get_fundamentals",
                fetch_mode="reproducible", row_count=len(stored_actions) if dataset == "corporate_actions" else len(stored_fundamentals),
                file_count=len(included)-1, date_range=_date_range(stored_actions) if dataset == "corporate_actions" else None,
                hf_path=remote_root, hf_revision=data_revision,
                notes="Raw/normalized files and quality reports; publication-date limits remain explicit.",
                files=[local for local, _ in included[:-1]],
                extra=json.loads(manifest.read_text(encoding="utf-8"))["extra"],
                script="backend/jobs/company_aux_ingest.py",
            )
            manifest.write_bytes((common.MANIFEST_DIR / manifest.name).read_bytes())
            local_copy.write_bytes(manifest.read_bytes())
            final_manifest_revision = common.upload_to_hf(
                local_copy, f"company/{dataset}/manifest.json", stream="company",
                commit_message=f"company: publish {dataset} manifest {run_id}",
            )
            _verify_remote(dataset, included, final_manifest_revision)
        else:
            final_manifest_revision = None
        outputs[dataset] = {
            "collection_status": action_summary["collection_status"] if dataset == "corporate_actions" else fundamental_summary["collection_status"],
            "row_count": len(stored_actions) if dataset == "corporate_actions" else len(stored_fundamentals),
            "files": len(included), "manifest": str(manifest),
            "data_revision": data_revision, "manifest_revision": final_manifest_revision,
            "remote_root": remote_root,
        }
    result = {
        "run_id": run_id,
        "corporate_actions": {**action_summary, "upload": outputs["corporate_actions"]},
        "fundamentals": {**fundamental_summary, "upload": outputs["fundamentals"]},
        "stream_a_status": "incomplete",
    }
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return result


def _price_action_overlap(actions: Sequence[CorporateAction]) -> dict[str, object]:
    """Same-provider date cross-check only; it is not independent price validation."""
    report_root = common.DATA_DIR / "quality_reports" / "company_prices"
    run_file = next(report_root.glob("*/dataset_summary.json"), None)
    if run_file is None:
        return {"status": "not_evaluated", "reason": "price run summary unavailable"}
    summary = json.loads(run_file.read_text(encoding="utf-8"))
    bad_dates: set[tuple[str, date]] = set()
    for ticker, detail in summary["per_ticker"].items():
        raw_file = common.DATA_DIR / detail["raw_path"]
        frame = json.loads(raw_file.read_text(encoding="utf-8"))["sdk_dataframe"]["data"]
        for error in detail.get("errors", []):
            match = re.match(r"row (\d+):", error)
            if match:
                bad_dates.add((ticker, date.fromisoformat(str(frame[int(match.group(1))]["datetime"])[:10])))
    overlaps = [a for a in actions if (a.ticker, a.ex_date) in bad_dates]
    return {
        "status": "same_provider_context_only",
        "rejected_price_dates": len(bad_dates),
        "action_ex_date_overlaps": len(overlaps),
        "overlap_by_type": dict(sorted(Counter(a.action_type for a in overlaps).items())),
        "verified_corrections": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2015, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 20))
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--requests-per-second", type=float, default=0.25)
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        collect_company_auxiliary(
            start=args.start, end=args.end, retries=args.retries,
            requests_per_second=args.requests_per_second, upload=not args.no_upload,
        )
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        log.error("company auxiliary ingestion stopped (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
