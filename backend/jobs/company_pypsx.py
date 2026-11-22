"""Offline-testable pyPSX price loader orchestration (never called at import)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

from jobs.company_schemas import DailyOHLCV
from pydantic import ValidationError

log = logging.getLogger(__name__)


class HistoricalFrame(Protocol):
    """Subset of pandas DataFrame used by the SDK adapter."""

    def to_json(self, **kwargs: object) -> str: ...

    def to_dict(self, orient: str) -> list[dict[str, object]]: ...

    @property
    def columns(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class TickerCollection:
    ticker: str
    status: str
    raw_path: str | None
    sha256: str | None
    row_count: int | None
    error: str | None = None
    rows: tuple[dict[str, object], ...] = ()
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class CollectionResult:
    fetched_at: datetime
    requested_start: date
    requested_end: date
    tickers: tuple[TickerCollection, ...]


def _default_fetch(ticker: str, start: str, end: str) -> HistoricalFrame:
    """Import the optional SDK only when a future collection is explicitly run."""
    import pypsx  # type: ignore[import-not-found]  # loader-only dependency

    return pypsx.get_historical(
        ticker, start=start, end=end, adjust_splits=False, dividends=True
    )


def get_archive_status() -> object:
    """Read SDK archive metadata only when a future operator calls this helper."""
    import pypsx  # type: ignore[import-not-found]  # loader-only dependency

    return pypsx.get_archive_status()


def archive_frame(frame: HistoricalFrame, ticker: str, raw_dir: Path) -> tuple[Path, str]:
    """Persist the SDK table response and DataFrame metadata without overwriting."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    table = json.loads(
        frame.to_json(orient="table", date_format="iso", double_precision=15)
    )
    payload = json.dumps(
        {"sdk_dataframe": table, "sdk_attrs": _json_safe(getattr(frame, "attrs", {}))},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    target = raw_dir / f"{ticker}_{digest}.json"
    temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload.encode("utf-8"))
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise OSError(f"content-addressed raw snapshot checksum mismatch: {target.name}")
    finally:
        temporary.unlink(missing_ok=True)
    return target, digest


def _json_safe(value: object) -> object:
    """Convert SDK metadata into JSON scalars without NaN or secret-bearing reprs."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _source_decimal(value: object, field: str, *, nullable: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        if nullable:
            return None
        raise ValueError(f"{field} is missing from the pyPSX row")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        if nullable:
            return None
        raise ValueError(f"{field} must be finite")
    return number


def _source_date(value: object) -> date:
    market_tz = ZoneInfo("Asia/Karachi")
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(market_tz).date()
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        raise ValueError("datetime is missing from the pyPSX row")
    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(market_tz).date()
        return parsed.date()
    except ValueError as exc:
        raise ValueError(f"datetime is not an ISO exchange date: {text!r}") from exc


def normalize_pypsx_prices(
    rows: Sequence[dict[str, object]],
    *,
    requested_ticker: str,
    fetched_at: datetime,
    errors: list[str] | None = None,
) -> tuple[DailyOHLCV, ...]:
    """Map verified `get_historical(adjust_splits=False)` rows to DailyOHLCV.

    `close` remains the as-traded close. `adj_factor` is retained as the SDK's
    split/bonus/rights factor; it is never applied here. Adjusted close and
    total-return close are not synthesized. Provider NaN/null stays null.
    """
    normalized: list[DailyOHLCV] = []
    endpoint = "pypsx.get_historical(adjust_splits=False, dividends=True)"
    for row_number, row in enumerate(rows):
        try:
            symbol = row.get("symbol")
            if symbol != requested_ticker:
                raise ValueError(
                    f"row {row_number}: returned symbol {symbol!r} does not match {requested_ticker}"
                )
            trade_date = _source_date(row.get("datetime"))
            volume = _source_decimal(row.get("volume"), "volume")
            if volume is None or volume != volume.to_integral_value() or volume < 0:
                raise ValueError(f"row {row_number}: volume must be a non-negative integer")
            dividend = _source_decimal(row.get("dividend"), "dividend", nullable=True)
            normalized.append(DailyOHLCV.model_validate({
                    "ticker": requested_ticker,
                    "trade_date": trade_date,
                    "open": _source_decimal(row.get("open"), "open"),
                    "high": _source_decimal(row.get("high"), "high"),
                    "low": _source_decimal(row.get("low"), "low"),
                    "close": _source_decimal(row.get("close"), "close"),
                    "volume": int(volume),
                    "currency": "PKR",
                    "price_unit": "per_share",
                    "volume_unit": "shares",
                    "adjusted_close": None,
                    "adjustment_factor": _source_decimal(
                        row.get("adj_factor"), "adj_factor", nullable=True
                    ),
                    "dividend": dividend,
                    "dividend_basis": "per_adjusted_share" if dividend is not None else None,
                    "source": "pyPSX",
                    "source_endpoint": endpoint,
                    "source_record_id": f"{symbol}:{trade_date.isoformat()}",
                    "fetched_at": fetched_at,
                }))
        except (ValueError, ValidationError) as exc:
            if errors is None:
                raise
            if isinstance(exc, ValidationError):
                details = "; ".join(
                    str(error["msg"]) for error in exc.errors(include_input=False)
                )
                errors.append(f"row {row_number}: {type(exc).__name__}: {details}")
            else:
                # Never persist provider row values in logs or quality reports.
                errors.append(f"row {row_number}: {type(exc).__name__}: invalid source row")
    return tuple(normalized)


def collect_prices(
    tickers: Sequence[str],
    start: date,
    end: date,
    raw_dir: Path,
    *,
    fetch: Callable[[str, str, str], HistoricalFrame] = _default_fetch,
    requests_per_second: float = 0.25,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CollectionResult:
    """Collect one ticker at a time with bounded retries and isolated failures.

    Intentionally performs no normalization/upload. A later explicit command
    can wire this adapter after source terms and paths have been approved.
    """
    if start > end or requests_per_second <= 0 or retries < 0:
        raise ValueError("invalid date range, request rate, or retry count")
    ordered = tuple(dict.fromkeys(tickers))
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", ticker) for ticker in ordered):
        raise ValueError("tickers must be uppercase PSX symbols")
    spacing = 1 / requests_per_second
    results: list[TickerCollection] = []
    previous_request = False
    for ticker in ordered:
        if previous_request:
            sleep(spacing)
        previous_request = True
        for attempt in range(retries + 1):
            try:
                frame = fetch(ticker, start.isoformat(), end.isoformat())
                ticker_fetched_at = now().astimezone(timezone.utc)
                break
            except Exception as exc:  # per-ticker failure is logged and retained
                if attempt < retries:
                    sleep(spacing * (2**attempt))
                    continue
                log.error("pyPSX collection failed for %s (%s)", ticker, type(exc).__name__)
                results.append(TickerCollection(ticker, "failed", None, None, None, type(exc).__name__))
        else:
            continue
        try:
            path, digest = archive_frame(frame, ticker, raw_dir)
            rows = tuple(frame.to_dict("records"))
            results.append(
                TickerCollection(
                    ticker, "complete", str(path), digest, len(rows), rows=rows,
                    fetched_at=ticker_fetched_at,
                )
            )
        except Exception as exc:
            log.error("pyPSX response could not be archived for %s (%s)", ticker, type(exc).__name__)
            results.append(TickerCollection(ticker, "failed", None, None, None, type(exc).__name__))
    return CollectionResult(now().astimezone(timezone.utc), start, end, tuple(results))
