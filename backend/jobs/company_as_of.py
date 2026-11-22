"""Small point-in-time filters for normalized company records."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo
from typing import TypeVar

from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV

Record = TypeVar("Record", DailyOHLCV, CorporateAction, CompanyFundamental)


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
            available = record.available_date
        else:
            available = record.available_date
            if available is None and record.published_at is not None:
                available = record.published_at.astimezone(ZoneInfo("Asia/Karachi")).date()
        if available is not None and available <= as_of:
            selected.append(record)
    return tuple(selected)
