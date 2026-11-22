"""Provider-neutral schemas for normalized company data."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _decimal_from_source(value: object) -> Decimal:
    """Parse decimal-like provider values without binary-float arithmetic."""
    if isinstance(value, bool) or value is None:
        raise ValueError("value must be a finite decimal number")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("value must be a finite decimal number") from exc
    if not parsed.is_finite():
        raise ValueError("value must be a finite decimal number")
    return parsed


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


DecimalValue = Annotated[Decimal, BeforeValidator(_decimal_from_source)]
PositiveDecimal = Annotated[DecimalValue, Field(gt=0)]
NonNegativeDecimal = Annotated[DecimalValue, Field(ge=0)]
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9.-]*$", max_length=20)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
MetricName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UtcTimestamp = Annotated[datetime, AfterValidator(_utc_datetime)]


class CompanyRecord(BaseModel):
    """Common validation policy; provider-only columns are rejected."""

    model_config = ConfigDict(extra="forbid")


class DailyOHLCV(CompanyRecord):
    ticker: Ticker
    trade_date: date
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: int = Field(ge=0)
    currency: CurrencyCode
    price_unit: Literal["per_share"]
    volume_unit: Literal["shares"]
    adjusted_close: PositiveDecimal | None = None
    adjustment_factor: PositiveDecimal | None = None
    dividend: NonNegativeDecimal | None = None
    dividend_basis: Literal["per_adjusted_share"] | None = None
    source: NonEmptyString
    source_endpoint: NonEmptyString | None = None
    source_url: NonEmptyString | None = None
    source_record_id: NonEmptyString | None = None
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_ohlc(self) -> DailyOHLCV:
        if self.low > self.high:
            raise ValueError("low must be less than or equal to high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be between low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be between low and high")
        return self


class CorporateAction(CompanyRecord):
    ticker: Ticker
    action_type: Literal["cash_dividend", "stock_split", "bonus_issue", "rights_issue", "other"]
    announcement_date: date | None = None
    ex_date: date
    record_date: date | None = None
    payment_date: date | None = None
    published_at: UtcTimestamp | None = None
    available_date: date | None = None
    amount_per_share: PositiveDecimal | None = None
    currency: CurrencyCode | None = None
    ratio_new_shares: PositiveDecimal | None = None
    ratio_old_shares: PositiveDecimal | None = None
    provider_price_factor: DecimalValue | None = None
    provider_total_factor: DecimalValue | None = None
    provider_applied: bool | None = None
    provider_fields: dict[str, str | None] = Field(default_factory=dict)
    source: NonEmptyString
    source_endpoint: NonEmptyString | None = None
    source_url: NonEmptyString | None = None
    source_record_id: NonEmptyString | None = None
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_action_fields(self) -> CorporateAction:
        if self.action_type == "cash_dividend":
            if self.amount_per_share is not None and self.currency is None:
                raise ValueError("cash dividend amount requires a currency")
            if self.ratio_new_shares is not None or self.ratio_old_shares is not None:
                raise ValueError("cash dividends cannot have a share ratio")
        else:
            if (self.ratio_new_shares is None) != (self.ratio_old_shares is None):
                raise ValueError("share actions must provide both ratio fields or neither")
            if self.amount_per_share is not None or self.currency is not None:
                raise ValueError("share actions cannot have dividend amount or currency")
        return self


class CompanyFundamental(CompanyRecord):
    ticker: Ticker
    metric: MetricName
    value: DecimalValue | None
    unit: NonEmptyString | None = None
    currency: CurrencyCode | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_type: Literal["annual", "quarterly", "other"] | None = None
    period_label: NonEmptyString | None = None
    statement_type: NonEmptyString | None = None
    source_item: NonEmptyString | None = None
    report_date: date | None = None
    published_at: UtcTimestamp | None = None
    available_date: date | None = None
    as_of_date: date | None = None
    provider_fields: dict[str, str | None] = Field(default_factory=dict)
    basis: Literal["reported", "vendor_calculated", "project_calculated"]
    source: NonEmptyString
    source_endpoint: NonEmptyString | None = None
    source_url: NonEmptyString | None = None
    source_record_id: NonEmptyString | None = None
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_period(self) -> CompanyFundamental:
        if self.period_start is not None and self.period_end is None:
            raise ValueError("period_start requires period_end")
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must be on or before period_end")
        if self.period_end is None and self.as_of_date is None and self.period_label is None:
            raise ValueError("a period_end, as_of_date, or source period_label is required")
        return self


class DatasetManifest(CompanyRecord):
    """Run metadata contract; templates may be uncollected and contain no files."""

    dataset_name: NonEmptyString
    ticker: Ticker | None = None
    source: NonEmptyString
    source_endpoint: NonEmptyString | None = None
    fetched_at: UtcTimestamp | None = None
    requested_start: date | None = None
    requested_end: date | None = None
    actual_first_date: date | None = None
    actual_last_date: date | None = None
    row_count: int | None = Field(default=None, ge=0)
    file_count: int | None = Field(default=None, ge=0)
    file_paths: tuple[NonEmptyString, ...] = ()
    checksums_sha256: dict[str, str] = Field(default_factory=dict)
    schema_version: NonEmptyString
    collection_status: Literal["not_started", "complete", "partial", "failed"]
    error: str | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> DatasetManifest:
        if self.requested_start and self.requested_end and self.requested_start > self.requested_end:
            raise ValueError("requested_start must be on or before requested_end")
        if (self.actual_first_date is None) != (self.actual_last_date is None):
            raise ValueError("actual first and last dates must both be set or both be null")
        if self.actual_first_date and self.actual_first_date > self.actual_last_date:
            raise ValueError("actual_first_date must be on or before actual_last_date")
        if len(self.file_paths) != len(set(self.file_paths)):
            raise ValueError("file_paths must be unique")
        if self.file_count is not None and self.file_count != len(self.file_paths):
            raise ValueError("file_count must match file_paths length")
        if any(path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/") for path in self.file_paths):
            raise ValueError("file_paths must be relative and cannot traverse parent directories")
        if self.file_paths and set(self.checksums_sha256) != set(self.file_paths):
            raise ValueError("checksums must cover every listed file path")
        for path, checksum in self.checksums_sha256.items():
            if path not in self.file_paths or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
                raise ValueError("checksums must be lowercase SHA-256 values for listed file paths")
        if self.collection_status == "not_started" and (self.row_count is not None or self.file_paths):
            raise ValueError("not_started manifests cannot claim collected rows or files")
        if self.collection_status == "not_started" and (
            self.fetched_at is not None
            or self.file_count is not None
            or self.checksums_sha256
            or self.actual_first_date is not None
            or self.actual_last_date is not None
            or self.error is not None
        ):
            raise ValueError("not_started manifests cannot contain collection results")
        if self.collection_status != "not_started" and self.fetched_at is None:
            raise ValueError("collected manifests require fetched_at")
        if self.collection_status == "failed" and not self.error:
            raise ValueError("failed manifests require error information")
        if self.collection_status == "complete":
            if self.error is not None:
                raise ValueError("complete manifests cannot contain an error")
            if self.row_count is None or self.file_count is None or self.file_count < 1:
                raise ValueError("complete manifests require row_count and at least one file")
            if not self.file_paths or set(self.checksums_sha256) != set(self.file_paths):
                raise ValueError("complete manifests require a checksum for every file path")
            if self.row_count > 0 and (self.requested_start is not None or self.requested_end is not None) and self.actual_first_date is None:
                raise ValueError("complete date-range manifests require actual first/last dates")
        if self.collection_status in {"partial", "failed"} and self.row_count is not None and self.file_count is None:
            raise ValueError("manifests with collected rows require file_count")
        return self


class SectorMappingRow(CompanyRecord):
    universe_version: NonEmptyString
    ticker: Ticker
    name: NonEmptyString
    sector: NonEmptyString
    source: Literal["backend/universe/psx_v1.yaml"]
