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
    source: NonEmptyString
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
    action_type: Literal["cash_dividend", "stock_split", "bonus_issue"]
    announcement_date: date | None = None
    ex_date: date
    record_date: date | None = None
    payment_date: date | None = None
    amount_per_share: PositiveDecimal | None = None
    currency: CurrencyCode | None = None
    ratio_new_shares: PositiveDecimal | None = None
    ratio_old_shares: PositiveDecimal | None = None
    source: NonEmptyString
    source_url: NonEmptyString | None = None
    source_record_id: NonEmptyString | None = None
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_action_fields(self) -> CorporateAction:
        if self.action_type == "cash_dividend":
            if self.amount_per_share is None or self.currency is None:
                raise ValueError("cash dividends require amount_per_share and currency")
            if self.ratio_new_shares is not None or self.ratio_old_shares is not None:
                raise ValueError("cash dividends cannot have a share ratio")
        else:
            if self.ratio_new_shares is None or self.ratio_old_shares is None:
                raise ValueError("share actions require both ratio fields")
            if self.amount_per_share is not None or self.currency is not None:
                raise ValueError("share actions cannot have dividend amount or currency")
        return self


class CompanyFundamental(CompanyRecord):
    ticker: Ticker
    metric: MetricName
    value: DecimalValue
    unit: NonEmptyString
    currency: CurrencyCode | None = None
    period_start: date | None = None
    period_end: date | None = None
    report_date: date | None = None
    published_at: UtcTimestamp
    as_of_date: date | None = None
    basis: Literal["reported", "vendor_calculated", "project_calculated"]
    source: NonEmptyString
    source_url: NonEmptyString | None = None
    source_record_id: NonEmptyString | None = None
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_period(self) -> CompanyFundamental:
        if self.period_start is not None and self.period_end is None:
            raise ValueError("period_start requires period_end")
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must be on or before period_end")
        if self.period_end is None and self.as_of_date is None:
            raise ValueError("a period_end or as_of_date is required")
        return self


class SectorMappingRow(CompanyRecord):
    universe_version: NonEmptyString
    ticker: Ticker
    name: NonEmptyString
    sector: NonEmptyString
    source: Literal["backend/universe/psx_v1.yaml"]
