"""Tests for backend/jobs/news_finbert.py with a fake scorer (no torch, no network)."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common, news_finbert  # noqa: E402  (needs the sys.path line above)

T = datetime(2026, 11, 1, tzinfo=timezone.utc)
FAKE_SOURCES: dict[str, list[dict[str, Any]]] = {
    "news_rss_articles": [
        {
            "article_id": "a1",
            "title": "PSX gains",
            "body": "Stocks rose.",
            "language": "en-us",
            "published_at": T,
        },
        {
            "article_id": "a2",
            "title": "اسٹاک ایکسچینج میں تیزی",
            "body": None,
            "language": "en",
            "published_at": T,
        },
        {
            "article_id": "a1",
            "title": "PSX gains",
            "body": "dup",
            "language": "en",
            "published_at": T,
        },
    ],
    "news_aaj_psx_archive": [
        {
            "article_id": "b1",
            "title": "KSE-100 falls",
            "body": "Selling.",
            "language": "en",
            "published_at": T,
        },
        {
            "article_id": "b2",
            "title": "",
            "body": "",
            "language": "en",
            "published_at": T,
        },
    ],
    "news_gdelt_index": [
        {
            "article_id": "a1",
            "title": "PSX gains",
            "body": None,
            "language": "English",
            "published_at": T,
        },
    ],
}


def _fake_scorer(calls: list[str]) -> news_finbert.Scorer:
    def score(texts: Sequence[str]) -> list[tuple[list[float], int]]:
        calls.extend(texts)
        return [
            ([0.7, 0.2, 0.1] if "gains" in t else [0.1, 0.8, 0.1], 200) for t in texts
        ]

    return score


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(news_finbert, "LOCAL_ROOT", tmp_path / "sent")
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(news_finbert, "load_source", lambda d: FAKE_SOURCES[d])
    return tmp_path


def test_build_input() -> None:
    assert news_finbert.build_input("T", "B") == ("T\nB", "title+body")
    assert news_finbert.build_input("T", None) == ("T", "title")
    assert news_finbert.build_input(" ", "") is None


def test_run_scores_filters_and_is_incremental(setup: Path) -> None:
    calls: list[str] = []
    assert news_finbert.run(upload=False, limit=None, scorer=_fake_scorer(calls)) == 0
    rows = pq.read_table(setup / "sent" / "scores.parquet").to_pylist()
    keys = [(r["source_dataset"], r["article_id"]) for r in rows]
    # Urdu (a2), empty (b2) and the duplicate a1 are skipped; a1 scored per dataset
    assert keys == [
        ("news_aaj_psx_archive", "b1"),
        ("news_gdelt_index", "a1"),
        ("news_rss_articles", "a1"),
    ]
    by = {k: r for k, r in zip(keys, rows, strict=True)}
    assert by[("news_rss_articles", "a1")]["label"] == "positive"
    assert by[("news_aaj_psx_archive", "b1")]["label"] == "negative"
    assert by[("news_gdelt_index", "a1")]["input_kind"] == "title"
    assert by[("news_rss_articles", "a1")]["truncated"] is True  # 200 > 128 tokens
    assert all(r["model_revision"] == news_finbert.MODEL_REVISION for r in rows)

    m = json.loads((setup / "manifests" / "news_finbert_sentiment.json").read_text())
    src = m["extra"]["last_run"]["sources"]["news_rss_articles"]
    assert src["excluded_non_english"] == 1 and src["duplicate_rows"] == 1
    assert any("unvalidated proxy" in w for w in m["warnings"])

    with (setup / "sent" / "spotcheck_sample.csv").open() as fh:
        sample = list(csv.DictReader(fh))
    assert {r["finbert_label"] for r in sample} == {"positive", "negative"}
    assert all(r["human_label"] == "" for r in sample)

    calls.clear()
    news_finbert.run(upload=False, limit=None, scorer=_fake_scorer(calls))
    assert calls == []  # nothing re-scored
