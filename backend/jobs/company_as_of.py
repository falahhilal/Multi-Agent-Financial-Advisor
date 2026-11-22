"""Small point-in-time filters for normalized company records."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import TypeVar

from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV

Record = TypeVar("Record", DailyOHLCV, CorporateAction, CompanyFundamental)
MARKET_TZ = ZoneInfo("Asia/Karachi")


def fundamental_available_at(record: CompanyFundamental) -> datetime | None:
    """Return the latest known availability timestamp in UTC.

    A date-only availability value means the information was available from
    the start of that Asia/Karachi calendar date. When both forms exist, use
    the later instant so neither can prematurely expose the record.
    """
    candidates: list[datetime] = []
    if record.published_at is not None:
        candidates.append(record.published_at.astimezone(timezone.utc))
    if record.available_date is not None:
        local_start = datetime.combine(record.available_date, time.min, tzinfo=MARKET_TZ)
        candidates.append(local_start.astimezone(timezone.utc))
    return max(candidates) if candidates else None


def fundamental_available_date(record: CompanyFundamental) -> date | None:
    """Return the conservative PSX-market date on which a metric is knowable."""
    candidates: list[date] = []
    if record.available_date is not None:
        candidates.append(record.available_date)
    if record.published_at is not None:
        candidates.append(record.published_at.astimezone(MARKET_TZ).date())
    return max(candidates) if candidates else None


def corporate_action_available_date(record: CorporateAction) -> date | None:
    """Return explicit public availability; never substitute an event date."""
    candidates: list[date] = []
    if record.available_date is not None:
        candidates.append(record.available_date)
    if record.published_at is not None:
        candidates.append(record.published_at.astimezone(MARKET_TZ).date())
    return max(candidates) if candidates else None


def records_as_of(records: tuple[Record, ...] | list[Record], as_of: date) -> tuple[Record, ...]:
    """Return only records knowable by ``as_of``; unknown availability is excluded.

    Prices are keyed by exchange trade date. Events and fundamentals use their
    explicit public/availability date; event dates and fiscal period ends are
    never substituted for publication dates.
    """
    selected: list[Record] = []
    for record in records:
        if isinstance(record, DailyOHLCV):
            available = record.trade_date
        elif isinstance(record, CorporateAction):
            available = corporate_action_available_date(record)
        else:
            available = fundamental_available_date(record)
        if available is not None and available <= as_of:
            selected.append(record)
    return tuple(selected)
