"""Aaj English PSX tag archive -> dataset ``news_aaj_psx_archive`` (stream ``news``).

Backfills historical PSX news from https://english.aaj.tv/trends/pakistan-stock-exchange
(paginated as ``/<page>``; checked 2026-11-08: 14 pages, ~418 articles,
2021-01 to now). Aaj English belongs to the Business Recorder Group and has no
terms page of its own. robots.txt allows /trends/ and /news/ (it disallows
/search, print views and some author pages).

Each run:

1. walks the listing pages until one has no articles, saving each listing
   page (gzipped) to ``raw/listing/fetch_date=D/page-NN.html.gz``;
2. for every article id not already in the dataset, fetches the article page
   and saves it unmodified to ``raw/articles/<site_id>.html.gz`` (never
   overwritten). The publish date comes from the article page's JSON-LD
   ``NewsArticle.datePublished`` (fallback ``article:published_time``), never
   from the listing;
3. rewrites ``articles.parquet`` (all articles, sorted by site id) and
   ``coverage_by_month.csv``, plus a run log;
4. uploads the new files to ``news/backfill/aaj_psx/`` in one HF commit and
   writes the manifest.

Re-running fetches only articles not yet in the dataset, so it never
duplicates. A fresh run from nothing re-downloads the same archive
(``fetch_mode = reproducible``), although the site can edit or remove pages.

Run from ``backend/``::

    python -m jobs.news_aaj_psx                       # full run + upload
    python -m jobs.news_aaj_psx --no-upload --max-pages 1 --limit 5   # quick test
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup

from jobs.common import (
    DATA_DIR,
    HttpError,
    RobotsDisallowedError,
    git_info,
    hf_download,
    http_get,
    upload_to_hf,
    write_manifest,
)
from jobs.news_common import article_id, parse_article

log = logging.getLogger("jobs.news_aaj_psx")

STREAM = "news"
DATASET = "aaj_psx_archive"
HF_PREFIX = "news/backfill/aaj_psx"
LOCAL_ROOT = DATA_DIR / "news" / "backfill" / "aaj_psx"
LISTING_URL = "https://english.aaj.tv/trends/pakistan-stock-exchange"
SOURCE = "Aaj English"
MIN_INTERVAL = 3.0  # seconds between requests to english.aaj.tv
MAX_PAGES = 100  # safety stop; the archive had 14 pages on 2026-11-08
_ARTICLE_URL = re.compile(r"^https://english\.aaj\.tv/news/(\d+)(/|$)")

NOTES = (
    "Aaj English 'pakistan-stock-exchange' tag archive, walked page by page. "
    "Publish dates come from each article page (JSON-LD datePublished, with "
    "the original +05:00 offset kept), not from the listing. Coverage is "
    "whatever Aaj tagged PSX: sparse and uneven by month (see "
    "coverage_by_month.csv); it is not a complete record of PSX news. Some "
    "articles are syndicated from Business Recorder (author field) and may "
    "duplicate BR items in news_rss_articles; canonical URLs differ, so "
    "cross-source dedupe must use title/date, later. Aaj English has no terms "
    "page; it belongs to the Business Recorder Group, whose terms allow "
    "download for non-commercial use and forbid republication. Private, "
    "non-commercial research use only."
)

_TS = pa.timestamp("s", tz="UTC")
SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),  # sha256(canonical_url)[:16], as in news_rss
        ("site_article_id", pa.string()),
        ("url", pa.string()),
        ("canonical_url", pa.string()),
        ("source", pa.string()),
        ("title", pa.string()),
        ("published_at", _TS),
        ("published_at_raw", pa.string()),
        ("published_tz", pa.string()),
        ("modified_at", _TS),
        ("authors", pa.list_(pa.string())),
        ("summary", pa.string()),
        ("full_text", pa.string()),
        ("language", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("tickers_mentioned", pa.list_(pa.string())),
        ("listing_page", pa.int32()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
    ]
)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_listing(html: bytes, base_url: str) -> list[tuple[str, str]]:
    """(site_article_id, url) for the main listing column, in page order."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("div.max-w-screen-md")
    if main is None:
        return []
    out: list[tuple[str, str]] = []
    for art in main.select("article.story"):
        a = art.find("a", href=re.compile(r"/news/\d+"))
        if a is None:
            continue
        url = urllib.parse.urljoin(base_url, str(a["href"]))
        m = _ARTICLE_URL.match(url)
        if m and m.group(1) not in {sid for sid, _ in out}:
            out.append((m.group(1), url))
    return out


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _gz_write_new(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(gzip.compress(data, mtime=0))
    return path


def load_existing(remote: bool) -> pa.Table:
    if remote:
        local = hf_download(f"{HF_PREFIX}/articles.parquet")
        return pq.read_table(local, schema=SCHEMA) if local else SCHEMA.empty_table()
    path = LOCAL_ROOT / "articles.parquet"
    return (
        pq.read_table(path, schema=SCHEMA) if path.is_file() else SCHEMA.empty_table()
    )


def coverage_by_month(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts = Counter(
        r["published_at"].strftime("%Y-%m") if r["published_at"] else "unknown"
        for r in rows
    )
    return sorted(counts.items())


def run(*, upload: bool, max_pages: int, limit: int | None) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    existing = load_existing(remote=upload)
    rows: list[dict[str, Any]] = existing.to_pylist()
    known = {r["site_article_id"] for r in rows}
    new_files: list[Path] = []
    errors: list[str] = []

    # 1. listing pages
    queue: list[tuple[str, str, int]] = []
    pages_seen = 0
    for page in range(1, max_pages + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}/{page}"
        try:
            resp = http_get(url, min_interval=MIN_INTERVAL)
        except (HttpError, RobotsDisallowedError) as exc:
            errors.append(f"listing page {page}: {exc}")
            log.error("listing page %d: %s", page, exc)
            break
        items = parse_listing(resp.content, resp.url)
        if not items:
            break
        pages_seen = page
        listing_path = LOCAL_ROOT / "raw" / "listing" / f"fetch_date={fetch_date}"
        new_files.append(
            _gz_write_new(
                listing_path / f"{run_id}_page-{page:02d}.html.gz", resp.content
            )
        )
        queue.extend((sid, u, page) for sid, u in items if sid not in known)
        log.info("listing page %d: %d articles, %d new", page, len(items), len(queue))
    else:
        errors.append(f"stopped at max_pages={max_pages}; archive may be longer")

    unique: dict[str, tuple[str, str, int]] = {}
    for item in queue:  # an article can appear on two pages if the listing shifts
        unique.setdefault(item[0], item)
    queue = list(unique.values())
    if limit is not None:
        queue = queue[:limit]

    # 2. article pages
    added = 0
    for i, (sid, url, page) in enumerate(queue, 1):
        raw_path = LOCAL_ROOT / "raw" / "articles" / f"{sid}.html.gz"
        if raw_path.is_file():  # fetched by an earlier, interrupted run
            html = gzip.decompress(raw_path.read_bytes())
        else:
            try:
                resp = http_get(url, min_interval=MIN_INTERVAL)
            except (HttpError, RobotsDisallowedError) as exc:
                errors.append(f"article {sid}: {exc}")
                log.error("article %s: %s", sid, exc)
                continue
            html = resp.content
            _gz_write_new(raw_path, html)
        new_files.append(raw_path)
        fields = parse_article(html, url)
        if fields["published_at"] is None:
            errors.append(f"article {sid}: no publish date found")
        rows.append(
            {
                "article_id": article_id(fields["canonical_url"]),
                "site_article_id": sid,
                "url": url,
                "source": SOURCE,
                "tickers_mentioned": [],
                "listing_page": page,
                "fetched_at": fetched_at,
                "raw_file": raw_path.relative_to(LOCAL_ROOT).as_posix(),
                **fields,
            }
        )
        added += 1
        if i % 25 == 0:
            log.info("articles: %d/%d", i, len(queue))

    # 3. outputs
    rows.sort(key=lambda r: int(r["site_article_id"]))
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    articles_path = LOCAL_ROOT / "articles.parquet"
    pq.write_table(table, articles_path, compression="zstd")
    coverage = coverage_by_month(rows)
    coverage_path = LOCAL_ROOT / "coverage_by_month.csv"
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["month", "articles"])
    writer.writerows(coverage)
    coverage_path.write_text(buf.getvalue(), encoding="utf-8")
    summary = {
        "run_id": run_id,
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "listing_pages": pages_seen,
        "new_articles": added,
        "total_articles": table.num_rows,
        "limit": limit,
        "errors": errors,
    }
    runlog = LOCAL_ROOT / "runs" / f"run-{run_id}.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    run_files = [*new_files, articles_path, coverage_path, runlog]
    log.info(
        "run %s: %d pages, %d new, %d total, %d errors",
        run_id,
        pages_seen,
        added,
        table.num_rows,
        len(errors),
    )

    revision = None
    if upload:
        with tempfile.TemporaryDirectory(prefix="aaj_upload_") as tmp:
            for f in run_files:
                dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
            revision = upload_to_hf(
                tmp,
                f"{HF_PREFIX}/",
                stream=STREAM,
                commit_message=f"{HF_PREFIX}: run {run_id} (+{added} articles)",
            )
    dates = [r["published_at"] for r in rows if r["published_at"]]
    write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source=f"{SOURCE} PSX tag archive <{LISTING_URL}>",
        fetch_mode="reproducible",
        row_count=table.num_rows,
        file_count=sum(1 for r in rows if r["raw_file"]),
        date_range=(min(dates), max(dates)) if dates else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra={"last_run": summary, "coverage_by_month": dict(coverage)},
        files=[articles_path, coverage_path, runlog],
        warnings=errors,
    )
    return 0 if not errors or added else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--limit", type=int, help="max new articles this run (testing)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return run(upload=not args.no_upload, max_pages=args.max_pages, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
