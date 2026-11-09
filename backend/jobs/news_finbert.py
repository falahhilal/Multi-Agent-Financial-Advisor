"""FinBERT headline sentiment -> dataset ``news_finbert_sentiment`` (stream ``news``).

Reads articles from the news datasets on HF (read-only; raw data is never
modified), scores each with ProsusAI/finbert at a pinned revision, and writes
``news/sentiment/finbert_<rev8>_t<N>/``:

* ``scores.parquet``: one row per (source_dataset, article_id): label, the
  three class probabilities, input kind and size, model name + revision.
* ``label_by_month.csv``: label counts per source and month.
* ``spotcheck_sample.csv``: a seeded, label-stratified sample with an empty
  ``human_label`` column, for the hand spot-check in ``plan.md`` Step 1.

Input text is the title plus the start of the article body (full text, else
summary), truncated to ``MAX_TOKENS`` FinBERT tokens. GDELT has titles only.
Non-English rows (Urdu by declared language or mostly Arabic script) are
excluded and counted. Re-runs score only rows not already scored with the
same model revision and token limit, so they never duplicate.

FinBERT is English-only and trained on financial news/filings that are not
Pakistani; its labels here are an unvalidated proxy until the spot-check is
done.

Run from ``backend/`` (needs torch + transformers)::

    python -m jobs.news_finbert                   # score new rows, upload
    python -m jobs.news_finbert --no-upload --limit 50   # quick local test
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import random
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from jobs.common import (
    DATA_DIR,
    HF_REPO,
    HF_REPO_TYPE,
    get_secret,
    git_info,
    hf_download,
    upload_to_hf,
    write_manifest,
)
from jobs.news_common import detect_language

log = logging.getLogger("jobs.news_finbert")

STREAM = "news"
DATASET = "finbert_sentiment"
MODEL = "ProsusAI/finbert"
MODEL_REVISION = "0bc2ac10dda6929bcb1f77ccc43c7ec5c424f031"  # pinned 2026-11-09
MAX_TOKENS = 128
BODY_CHARS = 2000  # body characters passed to the tokenizer before truncation
BATCH_SIZE = 32
LABELS = ("positive", "negative", "neutral")  # model config id2label 0, 1, 2
SPOTCHECK_PER_LABEL = 20
SEED = 20261109
RUN_NAME = f"finbert_{MODEL_REVISION[:8]}_t{MAX_TOKENS}"
HF_PREFIX = f"news/sentiment/{RUN_NAME}"
LOCAL_ROOT = DATA_DIR / "news" / "sentiment" / RUN_NAME

#: dataset id -> (HF path, file or folder of parquet partitions)
SOURCES: dict[str, tuple[str, str]] = {
    "news_rss_articles": ("news/rss/articles", "folder"),
    "news_aaj_psx_archive": ("news/backfill/aaj_psx/articles.parquet", "file"),
    "news_gdelt_index": ("news/backfill/gdelt/articles.parquet", "file"),
}

NOTES = (
    f"ProsusAI/finbert at revision {MODEL_REVISION} (model licence CC BY-NC-SA "
    "4.0: non-commercial, share-alike). Input = title + start of body, "
    f"truncated to {MAX_TOKENS} tokens; GDELT rows are title only (GDELT "
    "titles have tokenised spacing, e.g. 'shares , oil'). FinBERT is "
    "English-only and was not trained on Pakistani text: these labels are an "
    "UNVALIDATED PROXY until the hand spot-check (spotcheck_sample.csv) is "
    "done. Urdu and other non-English rows are excluded. Source datasets are "
    "read-only; nothing in them is modified. The same article can appear in "
    "more than one source dataset with different inputs; each is scored "
    "separately (key: source_dataset + article_id)."
)

_TS = pa.timestamp("s", tz="UTC")
SCORE_SCHEMA = pa.schema(
    [
        ("source_dataset", pa.string()),
        ("article_id", pa.string()),
        ("published_at", _TS),  # publish time, or GDELT seendate
        ("input_kind", pa.string()),  # "title+body" | "title"
        ("input_sha256", pa.string()),
        ("n_input_tokens", pa.int32()),  # before truncation, incl. special tokens
        ("truncated", pa.bool_()),
        ("label", pa.string()),
        ("score_positive", pa.float32()),
        ("score_negative", pa.float32()),
        ("score_neutral", pa.float32()),
        ("model_name", pa.string()),
        ("model_revision", pa.string()),
        ("max_tokens", pa.int32()),
        ("scored_at", _TS),
        ("run_id", pa.string()),
    ]
)

# texts -> list of (probabilities in LABELS order, token count before truncation)
Scorer = Callable[[Sequence[str]], list[tuple[list[float], int]]]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _is_english(language: str | None, title: str) -> bool:
    if detect_language(None, title) == "ur":
        return False
    if not language:
        return True
    lang = language.lower()
    return lang.startswith("en") or lang == "english"


def build_input(title: str | None, body: str | None) -> tuple[str, str] | None:
    """(text, kind) for one article, or None if there is nothing to score."""
    title = (title or "").strip()
    body = (body or "").strip()[:BODY_CHARS]
    if title and body:
        return f"{title}\n{body}", "title+body"
    if title:
        return title, "title"
    if body:
        return body, "body"
    return None


def _rows_from_table(t: pa.Table) -> list[dict[str, Any]]:
    out = []
    for r in t.to_pylist():  # GDELT has no body columns: body is None
        out.append(
            {
                "article_id": r["article_id"],
                "title": r.get("title"),
                "body": r.get("full_text") or r.get("summary"),
                "language": r.get("language"),
                "published_at": r.get("published_at") or r.get("seendate"),
            }
        )
    return out


def load_source(dataset: str) -> list[dict[str, Any]]:
    path, kind = SOURCES[dataset]
    if kind == "file":
        local = hf_download(path)
        return _rows_from_table(pq.read_table(local)) if local else []
    from huggingface_hub import HfApi

    api = HfApi(token=get_secret("HF_TOKEN"))
    entries = api.list_repo_tree(
        HF_REPO, path_in_repo=path, repo_type=HF_REPO_TYPE, recursive=True
    )
    rows: list[dict[str, Any]] = []
    for part in sorted(e.path for e in entries if e.path.endswith(".parquet")):
        local = hf_download(part)
        if local:
            rows.extend(_rows_from_table(pq.read_table(local)))
    return rows


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


def finbert_scorer() -> Scorer:
    """Load the pinned FinBERT once and return a batch scorer (CPU)."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL, revision=MODEL_REVISION)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, revision=MODEL_REVISION
    )
    model.eval()
    id2label = {int(k): v for k, v in model.config.id2label.items()}
    if tuple(id2label[i] for i in range(3)) != LABELS:
        raise RuntimeError(f"unexpected FinBERT labels {id2label}")

    def score(texts: Sequence[str]) -> list[tuple[list[float], int]]:
        lengths = [
            len(ids) for ids in tok(list(texts), add_special_tokens=True)["input_ids"]
        ]
        enc = tok(
            list(texts),
            truncation=True,
            max_length=MAX_TOKENS,
            padding=True,
            return_tensors="pt",
        )
        with torch.no_grad():
            probs = torch.softmax(model(**enc).logits, dim=-1).tolist()
        return list(zip(probs, lengths, strict=True))

    return score


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


def _write_csv(
    path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]
) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def spotcheck_rows(
    scores: list[dict[str, Any]], inputs: dict[tuple[str, str], str]
) -> list[list[Any]]:
    """Seeded sample of SPOTCHECK_PER_LABEL rows per label, stratified."""
    rng = random.Random(SEED)
    out = []
    for label in LABELS:
        pool = sorted(
            (s for s in scores if s["label"] == label),
            key=lambda s: (s["source_dataset"], s["article_id"]),
        )
        for s in rng.sample(pool, min(SPOTCHECK_PER_LABEL, len(pool))):
            text = inputs.get((s["source_dataset"], s["article_id"]), "")
            out.append(
                [
                    s["source_dataset"],
                    s["article_id"],
                    text.split("\n", 1)[0][:300],
                    s["label"],
                    round(s[f"score_{label}"], 3),
                    "",
                ]
            )
    return out


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _load_existing(upload: bool) -> list[dict[str, Any]]:
    if upload:
        local = hf_download(f"{HF_PREFIX}/scores.parquet")
    else:
        p = LOCAL_ROOT / "scores.parquet"
        local = p if p.is_file() else None
    return pq.read_table(local, schema=SCORE_SCHEMA).to_pylist() if local else []


def run(*, upload: bool, limit: int | None, scorer: Scorer | None = None) -> int:
    scored_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = scored_at.strftime("%Y%m%dT%H%M%SZ")
    existing = _load_existing(upload)
    done = {(s["source_dataset"], s["article_id"]) for s in existing}

    todo: list[tuple[str, dict[str, Any], str, str]] = []
    inputs: dict[tuple[str, str], str] = {}
    source_stats: dict[str, dict[str, int]] = {}
    for dataset in SOURCES:
        rows = load_source(dataset)
        st = Counter(rows=len(rows))
        seen: set[str] = set()
        for r in rows:
            key = (dataset, r["article_id"])
            if r["article_id"] in seen:
                st["duplicate_rows"] += 1
                continue
            seen.add(r["article_id"])
            if not _is_english(r["language"], r["title"] or ""):
                st["excluded_non_english"] += 1
                continue
            built = build_input(r["title"], r["body"])
            if built is None:
                st["excluded_empty"] += 1
                continue
            inputs[key] = built[0]
            if key in done:
                st["already_scored"] += 1
                continue
            todo.append((dataset, r, built[0], built[1]))
        st["to_score"] = sum(1 for t in todo if t[0] == dataset)
        source_stats[dataset] = dict(st)
        log.info("%s: %s", dataset, dict(st))
    if limit is not None:
        todo = todo[:limit]

    new: list[dict[str, Any]] = []
    if todo:
        score = scorer or finbert_scorer()
        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i : i + BATCH_SIZE]
            results = score([t[2] for t in batch])
            for (dataset, r, text, kind), (probs, n_tok) in zip(
                batch, results, strict=True
            ):
                new.append(
                    {
                        "source_dataset": dataset,
                        "article_id": r["article_id"],
                        "published_at": r["published_at"],
                        "input_kind": kind,
                        "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "n_input_tokens": n_tok,
                        "truncated": n_tok > MAX_TOKENS,
                        "label": LABELS[max(range(3), key=lambda j: probs[j])],
                        "score_positive": probs[0],
                        "score_negative": probs[1],
                        "score_neutral": probs[2],
                        "model_name": MODEL,
                        "model_revision": MODEL_REVISION,
                        "max_tokens": MAX_TOKENS,
                        "scored_at": scored_at,
                        "run_id": run_id,
                    }
                )
            if (i // BATCH_SIZE) % 20 == 0:
                log.info("scored %d/%d", min(i + BATCH_SIZE, len(todo)), len(todo))

    scores = sorted(
        existing + new, key=lambda s: (s["source_dataset"], s["article_id"])
    )
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    s_path = LOCAL_ROOT / "scores.parquet"
    pq.write_table(
        pa.Table.from_pylist(scores, schema=SCORE_SCHEMA), s_path, compression="zstd"
    )
    months = Counter(
        (
            s["source_dataset"],
            s["published_at"].strftime("%Y-%m") if s["published_at"] else "unknown",
            s["label"],
        )
        for s in scores
    )
    m_path = LOCAL_ROOT / "label_by_month.csv"
    _write_csv(
        m_path,
        ["source_dataset", "month", "label", "articles"],
        [[d, m, lab, n] for (d, m, lab), n in sorted(months.items())],
    )
    sc_path = LOCAL_ROOT / "spotcheck_sample.csv"
    _write_csv(
        sc_path,
        [
            "source_dataset",
            "article_id",
            "title",
            "finbert_label",
            "finbert_prob",
            "human_label",
        ],
        spotcheck_rows(scores, inputs),
    )
    labels = Counter(s["label"] for s in scores)
    summary = {
        "run_id": run_id,
        "scored_at": scored_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "model": MODEL,
        "model_revision": MODEL_REVISION,
        "max_tokens": MAX_TOKENS,
        "new_scores": len(new),
        "total_scores": len(scores),
        "labels": dict(labels),
        "truncated": sum(1 for s in scores if s["truncated"]),
        "sources": source_stats,
        "limit": limit,
    }
    runlog = LOCAL_ROOT / "runs" / f"run-{run_id}.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    log.info(
        "run %s: %d new, %d total, labels %s",
        run_id,
        len(new),
        len(scores),
        dict(labels),
    )

    revision = None
    files = [s_path, m_path, sc_path, runlog]
    if upload:
        with tempfile.TemporaryDirectory(prefix="finbert_upload_") as tmp:
            for f in files:
                dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
            revision = upload_to_hf(
                tmp,
                f"{HF_PREFIX}/",
                stream=STREAM,
                commit_message=f"{HF_PREFIX}: run {run_id} (+{len(new)} scores)",
            )
    dates = [s["published_at"] for s in scores if s["published_at"]]
    warnings = [
        "unvalidated proxy: hand spot-check (spotcheck_sample.csv) not done yet"
    ]
    if limit is not None:
        warnings.append(f"run limited to {limit} rows (test run)")
    write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source=f"{MODEL}@{MODEL_REVISION} over " + ", ".join(SOURCES),
        fetch_mode="reproducible",
        row_count=len(scores),
        file_count=len(files),
        date_range=(min(dates), max(dates)) if dates else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra={"last_run": summary},
        files=files,
        warnings=warnings,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument(
        "--limit", type=int, help="max rows to score this run (testing)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return run(upload=not args.no_upload, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
