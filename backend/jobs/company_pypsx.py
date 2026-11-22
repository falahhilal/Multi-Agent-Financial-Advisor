"""Offline-testable pyPSX price loader orchestration (never called at import)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

log = logging.getLogger(__name__)


class HistoricalFrame(Protocol):
    """Subset of pandas DataFrame used by the SDK adapter."""

    def to_json(self, **kwargs: object) -> str: ...

    def to_dict(self, orient: str) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class TickerCollection:
    ticker: str
    status: str
    raw_path: str | None
    sha256: str | None
    row_count: int | None
    error: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    fetched_at: datetime
    requested_start: date
    requested_end: date
    tickers: tuple[TickerCollection, ...]


def _default_fetch(ticker: str, start: str, end: str) -> HistoricalFrame:
    """Import the optional SDK only when a future collection is explicitly run."""
    import pypsx  # type: ignore[import-not-found]  # loader-only dependency

    return pypsx.get_historical(ticker, start=start, end=end)


def get_archive_status() -> object:
    """Read SDK archive metadata only when a future operator calls this helper."""
    import pypsx  # type: ignore[import-not-found]  # loader-only dependency

    return pypsx.get_archive_status()


def archive_frame(frame: HistoricalFrame, ticker: str, raw_dir: Path) -> tuple[Path, str]:
    """Persist a loss-minimizing SDK response snapshot, never overwrite a snapshot."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    payload = frame.to_json(orient="table", date_format="iso", double_precision=15)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    target = raw_dir / f"{ticker}_{digest}.json"
    if not target.exists():
        target.write_text(payload + "\n", encoding="utf-8")
    return target, digest


def collect_prices(
    tickers: Sequence[str],
    start: date,
    end: date,
    raw_dir: Path,
    *,
    fetch: Callable[[str, str, str], HistoricalFrame] = _default_fetch,
    requests_per_second: float = 0.5,
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
                path, digest = archive_frame(frame, ticker, raw_dir)
                rows = frame.to_dict("records")
                results.append(TickerCollection(ticker, "complete", str(path), digest, len(rows)))
                break
            except Exception as exc:  # per-ticker failure is logged and retained
                if attempt < retries:
                    sleep(spacing * (2**attempt))
                    continue
                log.exception("pyPSX collection failed for %s", ticker)
                results.append(TickerCollection(ticker, "failed", None, None, None, str(exc)))
    return CollectionResult(now().astimezone(timezone.utc), start, end, tuple(results))
