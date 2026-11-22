"""Provider-neutral parsing and idempotent merge helpers for company records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

from jobs.company_schemas import CompanyFundamental, CorporateAction, DailyOHLCV

RecordT = TypeVar("RecordT", bound=BaseModel)


class DuplicateRecordError(ValueError):
    """A single batch contains conflicting records with the same natural key."""


@dataclass(frozen=True)
class MergeResult(Generic[RecordT]):
    records: tuple[RecordT, ...]
    duplicate_keys: tuple[tuple[str, ...], ...]
    revised_keys: tuple[tuple[str, ...], ...]


def normalize_prices(rows: Iterable[Mapping[str, object]]) -> tuple[DailyOHLCV, ...]:
    return tuple(DailyOHLCV.model_validate(row) for row in rows)


def normalize_corporate_actions(
    rows: Iterable[Mapping[str, object]],
) -> tuple[CorporateAction, ...]:
    return tuple(CorporateAction.model_validate(row) for row in rows)


def normalize_fundamentals(
    rows: Iterable[Mapping[str, object]],
) -> tuple[CompanyFundamental, ...]:
    return tuple(CompanyFundamental.model_validate(row) for row in rows)


def _natural_key(record: BaseModel, key_fields: Sequence[str]) -> tuple[str, ...]:
    values = record.model_dump(mode="json")
    missing = [field for field in key_fields if field not in values]
    if missing:
        raise ValueError(f"unknown key fields: {', '.join(missing)}")
    return tuple(json.dumps(values[field], sort_keys=True) for field in key_fields)


def _canonical_record(record: BaseModel, ignore_fields: frozenset[str] = frozenset()) -> str:
    values = record.model_dump(mode="json")
    for field in ignore_fields:
        values.pop(field, None)
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def merge_records(
    existing: Sequence[RecordT],
    incoming: Sequence[RecordT],
    *,
    key_fields: Sequence[str],
    ignore_fields: Sequence[str] = (),
) -> MergeResult:
    """Merge without deleting history; return exact duplicates and revisions.

    Exact repeats are idempotent. A new batch with conflicting rows for one
    key is rejected. A changed incoming row replaces that key's stored version
    and is returned in ``revised_keys`` for an explicit quality-review flag.
    """
    if not key_fields:
        raise ValueError("key_fields must not be empty")
    ignored = frozenset(ignore_fields)

    ordered: dict[tuple[str, ...], RecordT] = {}
    canonical: dict[tuple[str, ...], str] = {}
    duplicate_keys: list[tuple[str, ...]] = []
    revised_keys: list[tuple[str, ...]] = []

    for record in existing:
        key = _natural_key(record, key_fields)
        if key in ordered:
            if canonical[key] != _canonical_record(record, ignored):
                raise DuplicateRecordError("existing store has conflicting duplicate keys")
            duplicate_keys.append(key)
            continue
        ordered[key] = record
        canonical[key] = _canonical_record(record, ignored)

    incoming_batch: dict[tuple[str, ...], str] = {}
    for record in incoming:
        key = _natural_key(record, key_fields)
        value = _canonical_record(record, ignored)
        if key in incoming_batch:
            if incoming_batch[key] != value:
                raise DuplicateRecordError("incoming batch has conflicting duplicate keys")
            duplicate_keys.append(key)
            continue
        incoming_batch[key] = value
        if key in ordered:
            if canonical[key] == value:
                duplicate_keys.append(key)
            else:
                revised_keys.append(key)
        ordered[key] = record
        canonical[key] = value

    return MergeResult(
        records=tuple(ordered.values()),
        duplicate_keys=tuple(duplicate_keys),
        revised_keys=tuple(revised_keys),
    )
