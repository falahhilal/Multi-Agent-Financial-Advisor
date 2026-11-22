"""End-to-end, rerunnable Stream A company-price ingestion and HF publishing."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import yaml
from huggingface_hub import HfApi, hf_hub_download
from pydantic import ValidationError

from jobs import common
from jobs.company_normalization import DuplicateRecordError, merge_records
from jobs.company_pypsx import (
    CollectionResult,
    TickerCollection,
    collect_prices,
    normalize_pypsx_prices,
)
from jobs.company_quality import (
    QualityIssue,
    build_per_ticker_reports,
    validate_dataset_coverage,
    validate_prices,
    write_per_ticker_reports,
    write_quality_report,
)
from jobs.company_schemas import DailyOHLCV
from jobs.company_sector_mapping import export_sector_mapping
from universe.validate_universe import validate_universe

log = logging.getLogger(__name__)


def read_active_universe(path: Path | None = None) -> tuple[str, ...]:
    universe_path = path or common.BACKEND_DIR / "universe" / "psx_v1.yaml"
    data: Any = yaml.safe_load(universe_path.read_text(encoding="utf-8"))
    errors = validate_universe(data)
    if errors:
        raise ValueError("invalid universe: " + "; ".join(errors))
    if data.get("version") != "psx_v1" or len(data.get("companies", [])) != 18:
        raise ValueError("company ingestion requires the canonical 18-company psx_v1 universe")
    return tuple(company["ticker"] for company in data["companies"])


def write_price_parquet(records: Sequence[DailyOHLCV], path: Path) -> Path:
    """Write exact Decimal-backed normalized records and verify the Parquet file."""
    if not records:
        raise ValueError("refusing to write an empty price Parquet file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".parquet.tmp")
    frame = pd.DataFrame([record.model_dump(mode="python") for record in records])
    frame.to_parquet(temporary, index=False, engine="pyarrow")
    verified = pd.read_parquet(temporary, engine="pyarrow")
    parsed = tuple(DailyOHLCV.model_validate(row) for row in verified.to_dict("records"))
    if parsed != tuple(records):
        temporary.unlink(missing_ok=True)
        raise ValueError("Parquet round-trip did not preserve normalized records")
    temporary.replace(path)
    return path


def read_price_parquet(path: Path) -> tuple[DailyOHLCV, ...]:
    frame = pd.read_parquet(path, engine="pyarrow")
    return tuple(DailyOHLCV.model_validate(row) for row in frame.to_dict("records"))


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _local_file_report(paths: Sequence[Path]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = common.sha256_file(path)
        if common.sha256_file(path) != digest:
            raise OSError(f"checksum changed while verifying {path.name}")
        output.append(
            {
                "path": path.relative_to(common.DATA_DIR).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
    return output


def _stage_files(files: Sequence[tuple[Path, Path]], directory: Path) -> Path:
    """Create an upload-only tree with hardlinks where supported."""
    directory.mkdir(parents=True, exist_ok=False)
    for source, relative in files:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        if common.sha256_file(source) != common.sha256_file(target):
            raise OSError(f"staged upload checksum mismatch: {source.name}")
    return directory


def _private_hf_api() -> HfApi:
    api = HfApi(token=common.get_secret("HF_TOKEN"))
    info = api.repo_info(repo_id=common.HF_REPO, repo_type="dataset")
    if info.private is not True:
        raise RuntimeError("configured Hugging Face dataset is not private")
    return api


def _records_for_ticker(
    collection: TickerCollection,
) -> tuple[tuple[DailyOHLCV, ...], list[str]]:
    errors: list[str] = []
    if collection.status != "complete" or collection.fetched_at is None:
        return (), [collection.error or "collection failed"]
    records = normalize_pypsx_prices(
        collection.rows,
        requested_ticker=collection.ticker,
        fetched_at=collection.fetched_at,
        errors=errors,
    )
    return records, errors


def _collect_and_store_prices(
    tickers: Sequence[str], start: date, end: date, *, retries: int = 2
) -> tuple[CollectionResult, dict[str, dict[str, object]], list[Path], Path, Path, Path]:
    raw_dir = common.DATA_DIR / "raw" / "company_prices"
    result = collect_prices(tickers, start, end, raw_dir, retries=retries)
    run_id = result.fetched_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    processed_dir = common.DATA_DIR / "processed" / "company_prices"
    quality_dir = common.DATA_DIR / "quality_reports" / "company_prices" / run_id
    local_run_path = common.DATA_DIR / "manifests" / "company_prices" / run_id / "run.json"
    local_manifest_copy = common.DATA_DIR / "manifests" / "company_prices" / run_id / "manifest.json"

    stats: dict[str, dict[str, object]] = {}
    candidate_records: list[DailyOHLCV] = []
    ticker_errors: dict[str, list[str]] = {}
    raw_paths: list[Path] = []
    normalized_paths: list[Path] = []
    revised_keys: dict[str, list[str]] = {}
    idempotence: dict[str, bool] = {}

    for item in result.tickers:
        if item.raw_path:
            raw_paths.append(Path(item.raw_path))
        records, errors = _records_for_ticker(item)
        ticker_errors[item.ticker] = errors
        candidate_records.extend(records)
        previous_path = processed_dir / f"ticker={item.ticker}" / "prices.parquet"
        existing: tuple[DailyOHLCV, ...] = read_price_parquet(previous_path) if previous_path.exists() else ()
        merged_count = len(existing)
        if records:
            try:
                merged = merge_records(
                    existing,
                    records,
                    key_fields=("ticker", "trade_date"),
                    ignore_fields=("fetched_at",),
                )
                verification = merge_records(
                    merged.records,
                    records,
                    key_fields=("ticker", "trade_date"),
                    ignore_fields=("fetched_at",),
                )
                idempotence[item.ticker] = verification.records == merged.records
                merged_count = len(merged.records)
                revised_keys[item.ticker] = [key[1] for key in merged.revised_keys]
                normalized_paths.append(write_price_parquet(
                    merged.records, previous_path
                ))
            except DuplicateRecordError as exc:
                errors.append(f"conflicting ticker/date records require review ({type(exc).__name__})")
                ticker_errors[item.ticker] = errors
                idempotence[item.ticker] = False
        price_dates = [record.trade_date for record in records]
        stats[item.ticker] = {
            "collection_status": item.status if records or item.status != "complete" else "empty",
            "source_row_count": item.row_count,
            "valid_normalized_row_count": len(records),
            "stored_unique_row_count": merged_count,
            "actual_first_date": min(price_dates).isoformat() if price_dates else None,
            "actual_last_date": max(price_dates).isoformat() if price_dates else None,
            "raw_path": str(Path(item.raw_path).relative_to(common.DATA_DIR).as_posix()) if item.raw_path else None,
            "raw_sha256": item.sha256,
            "normalized_path": previous_path.relative_to(common.DATA_DIR).as_posix() if previous_path.exists() else None,
            "revised_dates": revised_keys.get(item.ticker, []),
            "idempotent_merge_check": idempotence.get(item.ticker, False),
            "errors": errors,
        }

    prices_report = validate_prices(
        [record.model_dump(mode="python") for record in candidate_records],
        set(tickers),
        expected_sessions=None,
        max_abs_daily_return=None,
        corporate_actions=None,
    )
    extra_issues = list(prices_report.issues)
    for ticker, errors in ticker_errors.items():
        for error in errors:
            extra_issues.append(QualityIssue(
                code="collection_or_normalization_error",
                severity="error",
                message=error,
                ticker=ticker,
            ))
    if extra_issues != prices_report.issues:
        prices_report = prices_report.model_copy(update={"issues": extra_issues})

    coverage_tickers = sorted({record.ticker for record in candidate_records})
    coverage_report = validate_dataset_coverage(
        {"prices": coverage_tickers}, set(tickers),
        required_tickers_by_dataset={"prices": set(tickers)},
    )
    ticker_reports = build_per_ticker_reports(
        (prices_report, coverage_report), set(tickers)
    )
    quality_file_paths = [
        write_quality_report(prices_report, quality_dir / "dataset_prices.json"),
        write_quality_report(coverage_report, quality_dir / "dataset_ticker_coverage.json"),
        *write_per_ticker_reports(ticker_reports, quality_dir / "tickers"),
    ]

    raw_count = sum(item.row_count or 0 for item in result.tickers)
    normalized_count = len(candidate_records)
    collection_complete = all(
        item.status == "complete"
        and (item.row_count or 0) > 0
        and not ticker_errors[item.ticker]
        for item in result.tickers
    ) and len(result.tickers) == len(tickers)
    run_summary = {
        "run_id": run_id,
        "source": "pyPSX",
        "source_endpoint": "pypsx.get_historical",
        "options": {"adjust_splits": False, "dividends": True, "total_return": False},
        "fetched_at": result.fetched_at.isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "collection_status": "complete" if collection_complete else "partial",
        "raw_row_count": raw_count,
        "valid_normalized_row_count": normalized_count,
        "active_tickers": list(tickers),
        "per_ticker": stats,
        "quality_not_evaluated": sorted(set(prices_report.not_evaluated)),
        "stream_a_status": "incomplete",
        "stream_a_unresolved": [
            "No permitted point-in-time corporate-action source with public availability dates was verified.",
            "No permitted point-in-time fundamentals source with publication dates was verified.",
        ],
    }
    run_file = _write_json(local_run_path, run_summary)
    all_files = list(dict.fromkeys([
        *raw_paths, *normalized_paths, *quality_file_paths, run_file,
    ]))
    local_file_report = _local_file_report(all_files)
    summary_path = _write_json(
        quality_dir / "dataset_summary.json",
        {
            **run_summary,
            "quality_report_files": [path.relative_to(common.DATA_DIR).as_posix() for path in quality_file_paths],
            "ticker_quality_status": {report.ticker: report.status for report in ticker_reports},
            "checksum_verified": True,
            "raw_to_normalized_row_reconciliation": {
                "source_rows": raw_count,
                "valid_rows_before_merge": normalized_count,
                "difference": raw_count - normalized_count,
            },
            "files": local_file_report,
        },
    )
    all_files.append(summary_path)
    return result, stats, all_files, quality_dir, run_file, local_manifest_copy


def ingest_company_prices(
    *,
    tickers: Sequence[str] | None = None,
    start: date = date(2015, 1, 1),
    end: date = date(2026, 11, 20),
    retries: int = 2,
    upload: bool = True,
) -> dict[str, object]:
    """Fetch, archive, normalize, quality-report, manifest, and upload prices."""
    active = tuple(tickers or read_active_universe())
    if len(active) != 18 or len(set(active)) != 18 or any(ticker != ticker.upper() for ticker in active):
        raise ValueError("ingestion tickers must be the 18 unique uppercase active universe symbols")
    if start > end:
        raise ValueError("start must be on or before end")

    if upload:
        # Verify both source credentials and private target before contacting pyPSX.
        common.get_secret("PYPSX_API_KEY_ID")
        common.get_secret("PYPSX_API_SECRET_KEY")
        _private_hf_api()

    result, stats, files, quality_dir, run_file, manifest_copy = _collect_and_store_prices(
        active, start, end, retries=retries
    )
    if not any(int(item.get("valid_normalized_row_count", 0)) > 0 for item in stats.values()):
        raise RuntimeError("no valid company price records were returned; no upload attempted")

    run_id = json.loads(run_file.read_text(encoding="utf-8"))["run_id"]
    files = [path for path in files if path.exists()]
    normalized_files = sorted((common.DATA_DIR / "processed" / "company_prices").glob("ticker=*/*.parquet"))
    files.extend(path for path in normalized_files if path not in files)
    requested_actual_dates = [
        date.fromisoformat(str(value))
        for item in stats.values()
        for value in (item.get("actual_first_date"), item.get("actual_last_date"))
        if value is not None
    ]
    total_source_rows = sum(int(value.get("source_row_count") or 0) for value in stats.values())
    collection_status = json.loads(run_file.read_text(encoding="utf-8"))["collection_status"]

    remote_paths = {
        "raw": "company/prices/raw/",
        "normalized": "company/prices/normalized/",
        "quality_reports": f"company/quality_reports/prices/{run_id}/",
        "run_metadata": f"company/prices/runs/{run_id}/run.json",
        "reference_mapping": "company/reference/sector_mapping.csv",
        "price_manifest": f"company/prices/manifests/{run_id}.json",
        "reference_manifest": "company/reference/manifest.json",
    }
    revisions: dict[str, str] = {}
    if upload:
        ticker_set = set(active)
        unexpected_partition_dirs = {
            path.name.removeprefix("ticker=")
            for path in (common.DATA_DIR / "processed" / "company_prices").glob("ticker=*")
            if path.is_dir()
        } - ticker_set
        if unexpected_partition_dirs:
            raise RuntimeError(
                "unexpected historical/non-universe price partitions; refusing upload: "
                + ", ".join(sorted(unexpected_partition_dirs))
            )
        active_normalized = [
            (common.DATA_DIR / "processed" / "company_prices" / f"ticker={ticker}" / "prices.parquet",
             Path(f"ticker={ticker}") / "prices.parquet")
            for ticker in active
            if (common.DATA_DIR / "processed" / "company_prices" / f"ticker={ticker}" / "prices.parquet").is_file()
        ]
        (common.DATA_DIR / "interim" / "company_upload").mkdir(parents=True, exist_ok=True)
        if raw_paths := [Path(item["raw_path"]) for item in stats.values() if item.get("raw_path")]:
            with tempfile.TemporaryDirectory(dir=common.DATA_DIR / "interim" / "company_upload") as temp_root:
                raw_stage = _stage_files(
                    [(common.DATA_DIR / path, Path(path).name) for path in raw_paths],
                    Path(temp_root) / "raw",
                )
                revisions["raw"] = common.upload_to_hf(
                    raw_stage, remote_paths["raw"], stream="company",
                    commit_message=f"company: archive pyPSX raw prices {run_id}",
                )
        if active_normalized:
            with tempfile.TemporaryDirectory(dir=common.DATA_DIR / "interim") as temp_root:
                normalized_stage = _stage_files(active_normalized, Path(temp_root) / "normalized")
                revisions["normalized"] = common.upload_to_hf(
                    normalized_stage, remote_paths["normalized"], stream="company",
                    commit_message=f"company: publish normalized prices {run_id}",
                )
        revisions["quality_reports"] = common.upload_to_hf(
            quality_dir, remote_paths["quality_reports"], stream="company",
            commit_message=f"company: publish price quality reports {run_id}",
        )
        revisions["run_metadata"] = common.upload_to_hf(
            run_file, remote_paths["run_metadata"], stream="company",
            commit_message=f"company: publish price run metadata {run_id}",
        )
        sector_csv, _ = export_sector_mapping()
        revisions["reference_mapping"] = common.upload_to_hf(
            sector_csv, remote_paths["reference_mapping"], stream="company",
            commit_message=f"company: publish canonical v1 sector mapping {run_id}",
        )
        common.write_manifest(
            stream="company", dataset="sector_mapping",
            source="Project-supplied canonical sector labels in backend/universe/psx_v1.yaml",
            fetch_mode="reproducible", row_count=18, file_count=1, files=[sector_csv],
            hf_path=remote_paths["reference_mapping"],
            hf_revision=revisions["reference_mapping"],
            notes="Canonical reference uploaded to the private HF company/ prefix.",
            extra={"universe_version": "psx_v1", "storage_status": "uploaded"},
            script="backend/jobs/company_sector_mapping.py",
        )
        ref_manifest = common.MANIFEST_DIR / "company_sector_mapping.json"
        revisions["reference_manifest"] = common.upload_to_hf(
            ref_manifest, remote_paths["reference_manifest"], stream="company",
            commit_message=f"company: publish reference manifest {run_id}",
        )

    local_manifest = common.write_manifest(
        stream="company", dataset="company_prices",
        source="pyPSX History archive get_historical",
        fetch_mode="reproducible", row_count=sum(
            int(value.get("valid_normalized_row_count", 0)) for value in stats.values()
        ),
        file_count=len(files),
        date_range=(min(requested_actual_dates), max(requested_actual_dates)) if requested_actual_dates else (None, None),
        hf_path=remote_paths["price_manifest"] if upload else None,
        hf_revision=revisions.get("run_metadata"),
        notes=(
            "Prices were requested with adjust_splits=False; adjusted_close remains null. "
            "Collection status describes this price run; Stream A remains incomplete because "
            "point-in-time actions and fundamentals are unresolved."
        ),
        files=files,
        extra={
            "dataset_schema_version": "company_prices_v1",
            "collection_status": collection_status,
            "raw_source_row_count": total_source_rows,
            "valid_normalized_row_count": sum(
                int(value.get("valid_normalized_row_count", 0)) for value in stats.values()
            ),
            "quality_status": "incomplete",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "per_ticker": stats,
            "remote_paths": remote_paths if upload else {},
            "upload_revisions": revisions,
            "not_evaluated": [
                "complete PSX trading-session calendar and ticker-specific suspensions",
                "approved suspicious-movement threshold or price-limit policy",
                "verified corporate actions and adjustment validation",
                "point-in-time company fundamentals and approved age rules",
            ],
            "stream_a_status": "incomplete",
        },
        script="backend/jobs/company_ingest.py",
    )
    manifest_copy.parent.mkdir(parents=True, exist_ok=True)
    manifest_copy.write_bytes(local_manifest.read_bytes())
    if upload:
        revisions["price_manifest"] = common.upload_to_hf(
            local_manifest, remote_paths["price_manifest"], stream="company",
            commit_message=f"company: publish price manifest {run_id}",
        )
        extra_uploads = {
            "company/reference/sector_mapping.csv": common.DATA_DIR / "processed" / "company" / "sector_mapping.csv",
            "company/reference/manifest.json": common.MANIFEST_DIR / "company_sector_mapping.json",
            remote_paths["price_manifest"]: local_manifest,
        }
        verify_hf_uploads(files, remote_paths, run_id, revisions, extra_uploads)

    result_summary = {
        "run_id": run_id,
        "collection_status": collection_status,
        "source_row_count": total_source_rows,
        "per_ticker": stats,
        "manifest": str(local_manifest),
        "hf_revisions": revisions,
        "hf_paths": remote_paths if upload else {},
        "stream_a_status": "incomplete",
    }
    print(json.dumps(result_summary, indent=2, sort_keys=True))
    return result_summary


def verify_hf_uploads(
    local_files: Sequence[Path],
    remote_paths: dict[str, str],
    run_id: str,
    revisions: dict[str, str],
    extra_uploads: dict[str, Path],
) -> None:
    """Verify remote paths, sizes, and content SHA-256 for every uploaded file."""
    api = _private_hf_api()
    names = set(api.list_repo_files(repo_id=common.HF_REPO, repo_type="dataset"))
    expected = {
        f"company/prices/runs/{run_id}/run.json",
        f"company/prices/manifests/{run_id}.json",
        "company/reference/sector_mapping.csv",
        "company/reference/manifest.json",
    }
    expected.update(extra_uploads)
    for local_path in local_files:
        local_relative = local_path.relative_to(common.DATA_DIR).as_posix()
        if local_relative.startswith("raw/company_prices/"):
            expected.add(f"company/prices/raw/{local_path.name}")
        elif local_relative.startswith("processed/company_prices/"):
            expected.add("company/prices/normalized/" + local_relative.split("processed/company_prices/", 1)[1])
        elif local_relative.startswith(f"quality_reports/company_prices/{run_id}/"):
            suffix = local_relative.split(f"quality_reports/company_prices/{run_id}/", 1)[1]
            expected.add(f"company/quality_reports/prices/{run_id}/{suffix}")
    missing = sorted(path for path in expected if path not in names)
    if missing:
        raise RuntimeError("Hugging Face verification missing expected paths: " + ", ".join(missing))
    remote_info = api.get_paths_info(
        repo_id=common.HF_REPO,
        paths=sorted(expected),
        repo_type="dataset",
    )
    by_path = {item.path: item for item in remote_info}
    local_by_remote: dict[str, Path] = dict(extra_uploads)
    for local_path in local_files:
        local_relative = local_path.relative_to(common.DATA_DIR).as_posix()
        if local_relative.startswith("raw/company_prices/"):
            remote = f"company/prices/raw/{local_path.name}"
        elif local_relative.startswith("processed/company_prices/"):
            remote = "company/prices/normalized/" + local_relative.split("processed/company_prices/", 1)[1]
        elif local_relative.startswith(f"quality_reports/company_prices/{run_id}/"):
            suffix = local_relative.split(f"quality_reports/company_prices/{run_id}/", 1)[1]
            remote = f"company/quality_reports/prices/{run_id}/{suffix}"
        elif local_path.name == "run.json":
            remote = f"company/prices/runs/{run_id}/run.json"
        else:
            continue
        local_by_remote[remote] = local_path
    for remote, local_path in local_by_remote.items():
        info = by_path.get(remote)
        if info is None or getattr(info, "size", None) != local_path.stat().st_size:
            raise RuntimeError(f"Hugging Face size verification failed for {remote}")
        if getattr(getattr(info, "lfs", None), "sha256", None):
            if info.lfs.sha256 != common.sha256_file(local_path):
                raise RuntimeError(f"Hugging Face LFS checksum verification failed for {remote}")
        else:
            downloaded = hf_hub_download(
                repo_id=common.HF_REPO,
                repo_type="dataset",
                filename=remote,
                revision=revisions.get("price_manifest"),
                token=common.get_secret("HF_TOKEN"),
            )
            if common.sha256_file(downloaded) != common.sha256_file(local_path):
                raise RuntimeError(f"Hugging Face content checksum verification failed for {remote}")
    revisions["remote_paths_verified"] = "true"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2015, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 20))
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--no-upload", action="store_true", help="collect and validate locally only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        common.get_secret("PYPSX_API_KEY_ID")
        common.get_secret("PYPSX_API_SECRET_KEY")
        ingest_company_prices(
            start=args.start, end=args.end, retries=args.retries, upload=not args.no_upload
        )
    except (OSError, ValueError, RuntimeError, ValidationError) as exc:
        log.error("company price ingestion stopped (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
