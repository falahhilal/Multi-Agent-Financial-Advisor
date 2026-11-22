"""Offline, provider-neutral company-data quality checks.

Checks report issues; they never repair, interpolate, or discard input rows.
Calendar, staleness, and price-move policies must be supplied explicitly.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobs.company_as_of import corporate_action_available_date, fundamental_available_at
from jobs.company_schemas import (
    CompanyFundamental,
    CorporateAction,
    DailyOHLCV,
    SectorMappingRow,
    Ticker,
)

REQUIRED_QUALITY_DATASETS = frozenset(
    {"sector_mapping", "prices", "corporate_actions", "fundamentals", "ticker_coverage"}
)


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    ticker: str | None = None
    record_date: date | None = None


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    generated_at: datetime
    record_count: int = Field(ge=0)
    ticker_coverage: list[str]
    ticker_record_counts: dict[str, int]
    issues: list[QualityIssue]
    not_evaluated: list[str]


class TickerQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Ticker
    status: Literal["pass", "issues", "incomplete"]
    dataset_record_counts: dict[str, int]
    issues: list[QualityIssue]
    not_evaluated: dict[str, list[str]]


def _report(
    dataset: str,
    count: int,
    tickers: Set[str],
    issues: list[QualityIssue],
    not_evaluated: list[str],
    ticker_record_counts: Mapping[str, int] | None = None,
) -> QualityReport:
    return QualityReport(
        dataset=dataset,
        generated_at=datetime.now(timezone.utc),
        record_count=count,
        ticker_coverage=sorted(tickers),
        ticker_record_counts=dict(sorted((ticker_record_counts or {}).items())),
        issues=issues,
        not_evaluated=not_evaluated,
    )


def _validation_issue(dataset: str, row: Mapping[str, object], exc: ValidationError) -> QualityIssue:
    ticker_value = row.get("ticker")
    ticker = ticker_value if isinstance(ticker_value, str) else None
    details = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_input=False)
    )
    return QualityIssue(
        code=f"invalid_{dataset}_record",
        severity="error",
        message=details or "record failed schema validation",
        ticker=ticker,
    )


def _mapping_issues(
    observed: Iterable[str], known_tickers: Set[str], dataset: str
) -> list[QualityIssue]:
    return [
        QualityIssue(
            code="unknown_ticker",
            severity="error",
            message=f"ticker is absent from the versioned universe ({dataset})",
            ticker=ticker,
        )
        for ticker in sorted(set(observed) - known_tickers)
    ]


def validate_sector_mapping(
    rows: Sequence[SectorMappingRow], expected_tickers: Set[str]
) -> QualityReport:
    issues: list[QualityIssue] = []
    counts = Counter(row.ticker for row in rows)
    for ticker, count in sorted(counts.items()):
        if count != 1:
            issues.append(
                QualityIssue(
                    code="duplicate_sector_mapping",
                    severity="error",
                    message=f"ticker has {count} sector mappings; expected exactly one",
                    ticker=ticker,
                )
            )
    for ticker in sorted(expected_tickers - counts.keys()):
        issues.append(
            QualityIssue(
                code="missing_sector_mapping",
                severity="error",
                message="ticker has no sector mapping",
                ticker=ticker,
            )
        )
    issues.extend(_mapping_issues(counts.keys(), expected_tickers, "sector mapping"))
    return _report(
        "sector_mapping", len(rows), set(counts), issues, [], dict(counts)
    )


def validate_prices(
    rows: Iterable[Mapping[str, object]],
    known_tickers: Set[str],
    *,
    expected_sessions: Mapping[str, Set[date]] | None = None,
    max_abs_daily_return: Decimal | None = None,
    corporate_actions: Sequence[CorporateAction] | None = None,
) -> QualityReport:
    issues: list[QualityIssue] = []
    deferred: list[str] = []
    parsed: list[DailyOHLCV] = []
    raw_rows = list(rows)
    raw_ticker_counts = Counter(
        row["ticker"] for row in raw_rows if isinstance(row.get("ticker"), str)
    )
    for row in raw_rows:
        try:
            parsed.append(DailyOHLCV.model_validate(row))
        except ValidationError as exc:
            issues.append(_validation_issue("price", row, exc))

    keys = [(row.ticker, row.trade_date) for row in parsed]
    duplicate_counts = Counter(keys)
    for (ticker, trade_date), count in sorted(duplicate_counts.items()):
        if count > 1:
            issues.append(
                QualityIssue(
                    code="duplicate_price_record",
                    severity="error",
                    message=f"ticker/date key occurs {count} times",
                    ticker=ticker,
                    record_date=trade_date,
                )
            )
    issues.extend(_mapping_issues((row.ticker for row in parsed), known_tickers, "prices"))

    by_ticker: dict[str, list[DailyOHLCV]] = defaultdict(list)
    for row in parsed:
        by_ticker[row.ticker].append(row)

    if expected_sessions is None:
        deferred.append("expected and missing trading sessions require an approved PSX calendar")
    else:
        for ticker in sorted(known_tickers):
            observed_dates = {row.trade_date for row in by_ticker.get(ticker, [])}
            expected = expected_sessions.get(ticker, set())
            for missing in sorted(expected - observed_dates):
                issues.append(
                    QualityIssue(
                        code="missing_price_session",
                        severity="warning",
                        message="no price row for an expected exchange session",
                        ticker=ticker,
                        record_date=missing,
                    )
                )
            for unexpected in sorted(observed_dates - expected):
                issues.append(
                    QualityIssue(
                        code="unexpected_price_session",
                        severity="warning",
                        message="price row date is absent from the supplied exchange calendar",
                        ticker=ticker,
                        record_date=unexpected,
                    )
                )

    if max_abs_daily_return is None:
        deferred.append("suspicious-movement checks require an approved explicit threshold or PSX price-limit policy")
    elif max_abs_daily_return <= 0:
        raise ValueError("max_abs_daily_return must be positive")
    else:
        action_dates = {
            (action.ticker, action.ex_date)
            for action in corporate_actions or []
        }
        for ticker, ticker_rows in by_ticker.items():
            ordered = sorted(ticker_rows, key=lambda row: row.trade_date)
            for previous, current in zip(ordered, ordered[1:]):
                movement = abs(current.close / previous.close - Decimal(1))
                if movement > max_abs_daily_return:
                    action_note = (
                        "; a corporate action is recorded on this date and should be reviewed"
                        if (ticker, current.trade_date) in action_dates
                        else ""
                    )
                    issues.append(
                        QualityIssue(
                            code="suspicious_price_movement",
                            severity="warning",
                            message=f"close-to-close move exceeds the supplied threshold{action_note}",
                            ticker=ticker,
                            record_date=current.trade_date,
                        )
                    )

    if corporate_actions is None:
        deferred.append("price/action consistency requires verified corporate-action records and an approved adjustment method")
    return _report(
        "prices",
        len(raw_rows),
        {row.ticker for row in parsed},
        issues,
        deferred,
        dict(raw_ticker_counts),
    )


def validate_corporate_actions(
    rows: Iterable[Mapping[str, object]], known_tickers: Set[str]
) -> QualityReport:
    issues: list[QualityIssue] = []
    parsed: list[CorporateAction] = []
    raw_rows = list(rows)
    raw_ticker_counts = Counter(
        row["ticker"] for row in raw_rows if isinstance(row.get("ticker"), str)
    )
    for row in raw_rows:
        try:
            parsed.append(CorporateAction.model_validate(row))
        except ValidationError as exc:
            issues.append(_validation_issue("corporate_action", row, exc))

    keys = [
        (r.ticker, r.action_type, r.ex_date, r.source, r.source_record_id)
        for r in parsed
    ]
    counts = Counter(keys)
    for key, count in counts.items():
        if count > 1:
            issues.append(
                QualityIssue(
                    code="duplicate_corporate_action",
                    severity="error",
                    message=f"action identity occurs {count} times",
                    ticker=key[0],
                    record_date=key[2],
                )
            )
    for action in parsed:
        if action.announcement_date and action.announcement_date > action.ex_date:
            issues.append(
                QualityIssue(
                    code="announcement_after_ex_date",
                    severity="warning",
                    message="announcement date is after ex-date; verify source dates",
                    ticker=action.ticker,
                    record_date=action.ex_date,
                )
            )
    issues.extend(_mapping_issues((row.ticker for row in parsed), known_tickers, "corporate actions"))
    not_evaluated = ["event completeness requires an approved source and its historical coverage"]
    if any(corporate_action_available_date(action) is None for action in parsed):
        not_evaluated.append(
            "point-in-time action access excludes events without published_at or available_date"
        )
    return _report(
        "corporate_actions",
        len(raw_rows),
        {row.ticker for row in parsed},
        issues,
        not_evaluated,
        dict(raw_ticker_counts),
    )


def validate_fundamentals(
    rows: Iterable[Mapping[str, object]],
    known_tickers: Set[str],
    *,
    as_of: datetime | None = None,
    required_metrics: Mapping[str, Set[str]] | None = None,
    stale_after_days: Mapping[str, int] | None = None,
) -> QualityReport:
    issues: list[QualityIssue] = []
    deferred: list[str] = []
    parsed: list[CompanyFundamental] = []
    raw_rows = list(rows)
    raw_ticker_counts = Counter(
        row["ticker"] for row in raw_rows if isinstance(row.get("ticker"), str)
    )
    for row in raw_rows:
        try:
            parsed.append(CompanyFundamental.model_validate(row))
        except ValidationError as exc:
            issues.append(_validation_issue("fundamental", row, exc))

    unknown_availability: Counter[str] = Counter(
        record.ticker for record in parsed if fundamental_available_at(record) is None
    )
    for ticker, count in sorted(unknown_availability.items()):
        issues.append(
            QualityIssue(
                code="fundamental_availability_unknown",
                severity="warning",
                message=f"{count} source records lack public availability and are excluded from PIT reads",
                ticker=ticker,
            )
        )

    keys = [
        (r.ticker, r.statement_type, r.source_item, r.period_label, r.metric, r.source)
        for r in parsed
    ]
    counts = Counter(keys)
    for key, count in counts.items():
        if count > 1:
            issues.append(
                QualityIssue(
                    code="duplicate_fundamental_record",
                    severity="error",
                    message=f"fundamental identity occurs {count} times",
                    ticker=key[0],
                    record_date=key[2],
                )
            )

    as_of_utc: datetime | None = None
    if as_of is not None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        as_of_utc = as_of.astimezone(timezone.utc)
        for record in parsed:
            availability = fundamental_available_at(record)
            if availability is not None and availability > as_of_utc:
                issues.append(
                    QualityIssue(
                        code="fundamental_not_public_as_of_date",
                        severity="error",
                        message="record's publication or availability date is after the requested point-in-time",
                        ticker=record.ticker,
                        record_date=record.period_end,
                    )
                )
    else:
        deferred.append("point-in-time publication checks require a timezone-aware as_of timestamp")

    point_in_time_records = parsed
    if as_of_utc is not None:
        point_in_time_records = [
            record
            for record in parsed
            if (availability := fundamental_available_at(record)) is not None
            and availability <= as_of_utc
        ]

    if required_metrics is None:
        deferred.append("missing-metric checks require an approved per-ticker metric expectation")
    else:
        observed: dict[str, set[str]] = defaultdict(set)
        for record in point_in_time_records:
            observed[record.ticker].add(record.metric)
        for ticker, metrics in required_metrics.items():
            for metric in sorted(metrics - observed.get(ticker, set())):
                issues.append(
                    QualityIssue(
                        code="missing_fundamental_metric",
                        severity="warning",
                        message=f"expected metric {metric} is absent",
                        ticker=ticker,
                    )
                )

    if stale_after_days is None or as_of_utc is None:
        deferred.append("stale-fundamental checks require an as_of timestamp and approved per-metric age limits")
    else:
        if any(days < 0 for days in stale_after_days.values()):
            raise ValueError("stale_after_days values must be non-negative")
        latest: dict[tuple[str, str], CompanyFundamental] = {}
        for record in point_in_time_records:
            key = (record.ticker, record.metric)
            availability = fundamental_available_at(record)
            prior_availability = (
                fundamental_available_at(latest[key]) if key in latest else None
            )
            if key not in latest or (
                availability is not None
                and (prior_availability is None or availability > prior_availability)
            ):
                latest[key] = record
        for (ticker, metric), record in latest.items():
            max_age = stale_after_days.get(metric)
            availability = fundamental_available_at(record)
            if (
                max_age is not None
                and availability is not None
                and (as_of_utc - availability).days > max_age
            ):
                issues.append(
                    QualityIssue(
                        code="stale_fundamental",
                        severity="warning",
                        message=f"latest publication is older than the configured {max_age}-day limit",
                        ticker=ticker,
                        record_date=record.period_end,
                    )
                )

    issues.extend(_mapping_issues((row.ticker for row in parsed), known_tickers, "fundamentals"))
    if unknown_availability:
        deferred.append("records without actual publication/availability dates are excluded from point-in-time reads")
    return _report(
        "fundamentals",
        len(raw_rows),
        {row.ticker for row in parsed},
        issues,
        deferred,
        dict(raw_ticker_counts),
    )


def validate_dataset_coverage(
    coverage: Mapping[str, Sequence[str]],
    expected_tickers: Set[str],
    *,
    required_tickers_by_dataset: Mapping[str, Set[str]] | None = None,
) -> QualityReport:
    """Check ticker coverage, with explicit requirements for event datasets.

    If no per-dataset requirement is supplied, each dataset is expected to
    cover every ticker. For sparse event data (such as corporate actions),
    pass an empty/smaller set because a ticker may legitimately have no event.
    """
    issues: list[QualityIssue] = []
    observed_all: set[str] = set()
    entry_count = 0
    if required_tickers_by_dataset is not None:
        for dataset in sorted(required_tickers_by_dataset.keys() - coverage.keys()):
            issues.append(
                QualityIssue(
                    code="missing_dataset_coverage",
                    severity="error",
                    message=f"required dataset {dataset} has no coverage report",
                )
            )
    for dataset, tickers in sorted(coverage.items()):
        entry_count += len(tickers)
        counts = Counter(tickers)
        for ticker, count in counts.items():
            if count != 1:
                issues.append(
                    QualityIssue(
                        code="duplicate_ticker_coverage",
                        severity="error",
                        message=f"{dataset} reports ticker {count} times",
                        ticker=ticker,
                    )
                )
        required = (
            required_tickers_by_dataset.get(dataset, expected_tickers)
            if required_tickers_by_dataset is not None
            else expected_tickers
        )
        for ticker in sorted(required - counts.keys()):
            issues.append(
                QualityIssue(
                    code="missing_ticker_coverage",
                    severity="warning",
                    message=f"{dataset} has no record for a universe ticker",
                    ticker=ticker,
                )
            )
        for ticker in sorted(counts.keys() - expected_tickers):
            issues.append(
                QualityIssue(
                    code="unknown_ticker_coverage",
                    severity="error",
                    message=f"{dataset} contains a ticker outside the universe",
                    ticker=ticker,
                )
            )
        observed_all.update(tickers)
    if not coverage:
        issues.append(
            QualityIssue(
                code="no_dataset_coverage",
                severity="info",
                message="no company datasets have been collected; coverage is not evaluated",
            )
        )
    coverage_counts = Counter(ticker for tickers in coverage.values() for ticker in tickers)
    return _report(
        "ticker_coverage", entry_count, observed_all, issues, [], dict(coverage_counts)
    )


def build_per_ticker_reports(
    reports: Sequence[QualityReport], expected_tickers: Set[str]
) -> tuple[TickerQualityReport, ...]:
    """Assemble one reviewable summary for each universe ticker."""
    if len({report.dataset for report in reports}) != len(reports):
        raise ValueError("dataset quality reports must have unique dataset names")
    output: list[TickerQualityReport] = []
    supplied_datasets = {report.dataset for report in reports}
    for ticker in sorted(expected_tickers):
        ticker_issues = [
            issue
            for report in reports
            for issue in report.issues
            if issue.ticker == ticker
        ]
        counts = {
            report.dataset: report.ticker_record_counts.get(ticker, 0)
            for report in reports
        }
        not_evaluated = {
            report.dataset: report.not_evaluated
            for report in reports
            if report.not_evaluated
        }
        for missing_dataset in sorted(REQUIRED_QUALITY_DATASETS - supplied_datasets):
            not_evaluated[missing_dataset] = ["dataset quality report was not supplied"]
        status: Literal["pass", "issues", "incomplete"]
        if ticker_issues:
            status = "issues"
        elif not_evaluated:
            status = "incomplete"
        else:
            status = "pass"
        output.append(
            TickerQualityReport(
                ticker=ticker,
                status=status,
                dataset_record_counts=counts,
                issues=ticker_issues,
                not_evaluated=not_evaluated,
            )
        )
    return tuple(output)


def write_per_ticker_reports(
    reports: Sequence[TickerQualityReport], directory: str | Path
) -> tuple[Path, ...]:
    """Write review reports atomically without changing any dataset rows."""
    output_dir = Path(directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for report in reports:
        target = output_dir / f"{report.ticker}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        paths.append(target)
    return tuple(paths)


def write_quality_report(report: QualityReport, path: str | Path) -> Path:
    """Write a quality report atomically; caller decides when it may be published."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return output
