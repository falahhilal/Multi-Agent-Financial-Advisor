"""Article text for the GDELT index -> dataset ``news_gdelt_fulltext``.

GDELT (``news_gdelt_index``) gives only titles and links. This job fetches
each linked article from the publisher's own site (Dawn, The Express Tribune,
Business Recorder), keeps the raw page, and extracts title, dates and text.
Checked 2026-11-16: robots.txt allows article pages on all three sites, and
pages from 2017 still load, so the Wayback Machine is only a later fallback
for links that are gone (status ``gone``).

Each run:

1. reads ``news/backfill/gdelt/articles.parquet`` (read-only);
2. skips links that are not articles (author and section pages, Dawn's daily
   ``/stocks/`` pages), recording them as ``skipped_not_article``;
3. fetches the rest, one request per 3 s per site, taking the sites in turn;
   saves each page unmodified to ``raw/<domain>/<article_id>.html.gz``;
4. parses with ``jobs.news_common.parse_article``;
5. writes ``articles.parquet`` (one row per GDELT link, with a ``status``),
   ``coverage_by_month.csv`` (links, recovered, missing per site and month)
   and a run log; uploads to ``news/backfill/gdelt_text/`` every
   ``CHECKPOINT_EVERY`` pages.

Dates: ``published_at`` is the page's own date. The Express Tribune stamps
articles with their last update, which can be later than first publication,
so ``first_public_at = min(published_at, seendate)`` (GDELT's ``seendate``
is an upper bound on when the article first appeared) and
``published_after_seen`` flags the rows where the page date was the later
one. Use ``first_public_at`` as the point-in-time key.

Runs resume: rows with status ``ok``, ``no_text``, ``gone`` or
``skipped_not_article`` are final; ``failed`` rows are retried. A site that
fails ``BREAKER`` times in a row is left alone for the rest of the run.

Run from ``backend/``::

    python -m jobs.news_gdelt_text                         # resume, upload
    python -m jobs.news_gdelt_text --no-upload --limit 9   # quick local test
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
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from jobs.common import (
    DATA_DIR,
    HttpError,
    HttpResponse,
    RobotsDisallowedError,
    git_info,
    hf_download,
    http_get,
    upload_to_hf,
    write_manifest,
)
from jobs.news_common import parse_article

log = logging.getLogger("jobs.news_gdelt_text")

STREAM = "news"
DATASET = "gdelt_fulltext"
HF_PREFIX = "news/backfill/gdelt_text"
GDELT_ARTICLES = "news/backfill/gdelt/articles.parquet"
LOCAL_ROOT = DATA_DIR / "news" / "backfill" / "gdelt_text"
MIN_INTERVAL = 3.0  # seconds between requests to one site
CHECKPOINT_EVERY = 300  # pages between uploads
MAX_MINUTES = 105.0
BREAKER = 10  # consecutive failures before a site is skipped for the run
FINAL_STATUSES = frozenset({"ok", "no_text", "gone", "skipped_not_article"})

#: Canonical URL shapes that are articles. Everything else is skipped.
ARTICLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "dawn.com": re.compile(r"^https://www\.dawn\.com/news/\d+$"),
    "tribune.com.pk": re.compile(r"^https://tribune\.com\.pk/story/\d+$"),
    # /news/<id>, and the 2017 form /YYYY/MM/DD/<id>[/slug], which redirects to it
    "brecorder.com": re.compile(
        r"^https://www\.brecorder\.com/(news/\d+|\d{4}/\d{2}/\d{2}/\d+(/[^/]*)?)$"
    ),
}

NOTES = (
    "Full text for the links in news_gdelt_index, fetched from the publishers' "
    "own sites (recovery_source='live'). One row per GDELT link, with a "
    "status: ok, no_text (page loaded, no text found), gone (404/410, or a "
    "'soft 404': the site answered 200 with its not-found page or redirected "
    "to a non-article page; candidates for the Wayback Machine), failed "
    "(retried next run) or "
    "skipped_not_article. published_at is the page's own date; The Express "
    "Tribune stamps the last update, so first_public_at = min(published_at, "
    "seendate) is the point-in-time key and published_after_seen flags rows "
    "where the page date was later than GDELT's sighting. Coverage is bounded "
    "by GDELT's index: Business Recorder is absent from it for most of "
    "2018-2022 and after 2025-11. Site terms restrict reproduction (see "
    "docs/data-sources.md): private, non-commercial research use only."
)

_TS = pa.timestamp("s", tz="UTC")
SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),  # same id as in news_gdelt_index
        ("domain", pa.string()),
        ("url", pa.string()),  # canonical URL from the GDELT index
        ("final_url", pa.string()),
        ("status", pa.string()),
        ("http_status", pa.int32()),
        ("error", pa.string()),
        ("gdelt_title", pa.string()),
        ("title", pa.string()),
        ("seendate", _TS),
        ("published_at", _TS),
        ("published_at_raw", pa.string()),
        ("published_tz", pa.string()),
        ("modified_at", _TS),
        ("first_public_at", _TS),
        ("published_after_seen", pa.bool_()),
        ("authors", pa.list_(pa.string())),
        ("summary", pa.string()),
        ("full_text", pa.string()),
        ("language", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("recovery_source", pa.string()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("attempts", pa.int32()),
    ]
)


def is_article(domain: str, url: str) -> bool:
    pattern = ARTICLE_PATTERNS.get(domain)
    return bool(pattern and pattern.match(url))


def first_public(
    published_at: datetime | None, seendate: datetime | None
) -> tuple[datetime | None, bool]:
    """(earliest known public time, whether the page date was after the sighting)."""
    if published_at and seendate:
        return min(published_at, seendate), published_at > seendate
    return published_at or seendate, False


def _base_row(
    link: dict[str, Any], fetched_at: datetime, attempts: int
) -> dict[str, Any]:
    return {
        "article_id": link["article_id"],
        "domain": link["domain"],
        "url": link["canonical_url"],
        "final_url": None,
        "status": "failed",
        "http_status": None,
        "error": None,
        "gdelt_title": link.get("title"),
        "title": None,
        "seendate": link.get("seendate"),
        "published_at": None,
        "published_at_raw": None,
        "published_tz": None,
        "modified_at": None,
        "first_public_at": link.get("seendate"),
        "published_after_seen": False,
        "authors": [],
        "summary": None,
        "full_text": None,
        "language": None,
        "tags": [],
        "recovery_source": None,
        "fetched_at": fetched_at,
        "raw_file": None,
        "attempts": attempts,
    }


_NOT_FOUND_TITLE = re.compile(rb"<title>\s*(page not found|404)\b", re.I)


#: Where an article request ends up on all three sites (slug optional).
_ARTICLE_FINAL = re.compile(r"^https?://[^/]+/(news|story)/\d+(/|$)")


def soft_gone(final_url: str, html: bytes) -> str | None:
    """Why a 200 response is not the article (a 'soft 404'), or None."""
    if not _ARTICLE_FINAL.match(final_url):
        return "redirected to a non-article page"
    if _NOT_FOUND_TITLE.search(html[:50_000]):
        return "the page says it was not found"
    return None


def row_from_page(
    link: dict[str, Any],
    html: bytes,
    final_url: str,
    fetched_at: datetime,
    attempts: int,
) -> dict[str, Any]:
    row = _base_row(link, fetched_at, attempts)
    f = parse_article(html, final_url)
    first, after = first_public(f["published_at"], link.get("seendate"))
    status, error = ("ok" if f["full_text"] else "no_text"), None
    if status == "no_text":  # never downgrade a page that has article text
        reason = soft_gone(final_url, html)
        if reason:
            status, error = "gone", f"soft 404: {reason}"
    row.update(
        final_url=final_url,
        status=status,
        error=error,
        http_status=200,
        title=f["title"],
        published_at=f["published_at"],
        published_at_raw=f["published_at_raw"],
        published_tz=f["published_tz"],
        modified_at=f["modified_at"],
        first_public_at=first,
        published_after_seen=after,
        authors=f["authors"],
        summary=f["summary"],
        full_text=f["full_text"],
        language=f["language"],
        tags=f["tags"],
        recovery_source="live",
    )
    return row


def fetch_row(
    link: dict[str, Any], fetched_at: datetime, attempts: int
) -> dict[str, Any]:
    """Fetch and parse one link. Never raises: failures become a row."""
    raw_rel = f"raw/{link['domain']}/{link['article_id']}.html.gz"
    raw_path = LOCAL_ROOT / raw_rel
    if raw_path.is_file():  # fetched by an earlier, interrupted run
        row = row_from_page(
            link,
            gzip.decompress(raw_path.read_bytes()),
            link["canonical_url"],
            fetched_at,
            attempts,
        )
        row["raw_file"] = raw_rel
        return row
    try:
        resp: HttpResponse = http_get(
            link["canonical_url"], min_interval=MIN_INTERVAL, retries=1
        )
    except HttpError as exc:
        row = _base_row(link, fetched_at, attempts)
        row.update(
            http_status=exc.status,
            error=str(exc)[:300],
            status="gone" if exc.status in (404, 410) else "failed",
        )
        return row
    except RobotsDisallowedError as exc:
        row = _base_row(link, fetched_at, attempts)
        row.update(error=str(exc)[:300])
        return row
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("xb") as fh:
        fh.write(gzip.compress(resp.content, mtime=0))
    row = row_from_page(link, resp.content, resp.url, fetched_at, attempts)
    row["raw_file"] = raw_rel
    return row


def coverage_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    c: Counter[tuple[str, str, str]] = Counter()
    for r in rows:
        if r["status"] == "skipped_not_article":
            continue
        t = r["first_public_at"]
        month = t.strftime("%Y-%m") if t else "unknown"
        c[(month, r["domain"], "links")] += 1
        c[(month, r["domain"], "recovered" if r["status"] == "ok" else "missing")] += 1
    keys = sorted({(m, d) for m, d, _ in c})
    return [
        [m, d, c[(m, d, "links")], c[(m, d, "recovered")], c[(m, d, "missing")]]
        for m, d in keys
    ]


def _write_outputs(rows: list[dict[str, Any]]) -> list[Path]:
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["domain"], r["article_id"]))
    a_path = LOCAL_ROOT / "articles.parquet"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=SCHEMA), a_path, compression="zstd"
    )
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["month", "domain", "gdelt_links", "recovered", "missing"])
    w.writerows(coverage_rows(rows))
    c_path = LOCAL_ROOT / "coverage_by_month.csv"
    c_path.write_text(buf.getvalue(), encoding="utf-8")
    return [a_path, c_path]


def _upload(files: list[Path], run_id: str, n_ok: int) -> str:
    with tempfile.TemporaryDirectory(prefix="gdelt_text_upload_") as tmp:
        for f in files:
            dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return upload_to_hf(
            tmp,
            f"{HF_PREFIX}/",
            stream=STREAM,
            commit_message=f"{HF_PREFIX}: run {run_id} ({n_ok} articles with text)",
        )


def _read_existing(upload: bool) -> list[dict[str, Any]]:
    if upload:
        local = hf_download(f"{HF_PREFIX}/articles.parquet")
    else:
        p = LOCAL_ROOT / "articles.parquet"
        local = p if p.is_file() else None
    return pq.read_table(local, schema=SCHEMA).to_pylist() if local else []


def _reparse_no_text(
    rows: dict[str, dict[str, Any]], links: dict[str, dict[str, Any]], upload: bool
) -> int:
    """Re-read stored raw pages for 'no_text' rows (no request to the site).

    Picks up parser fixes and soft-404 detection added after the page was
    fetched. Returns how many rows changed status.
    """
    changed = 0
    for aid, prev in list(rows.items()):
        link = links.get(aid)
        if prev["status"] != "no_text" or not prev["raw_file"] or link is None:
            continue
        raw: Path | None = LOCAL_ROOT / prev["raw_file"]
        if raw is not None and not raw.is_file():
            raw = hf_download(f"{HF_PREFIX}/{prev['raw_file']}") if upload else None
        if raw is None:
            continue
        row = row_from_page(
            link,
            gzip.decompress(raw.read_bytes()),
            prev["final_url"] or link["canonical_url"],
            prev["fetched_at"],
            prev["attempts"],
        )
        row["raw_file"] = prev["raw_file"]
        if row["status"] != prev["status"]:
            changed += 1
        rows[aid] = row
    if changed:
        log.info("re-read stored pages: %d 'no_text' rows changed status", changed)
    return changed


def run(
    *,
    upload: bool,
    limit: int | None,
    max_minutes: float = MAX_MINUTES,
    links: list[dict[str, Any]] | None = None,
) -> int:
    started = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    deadline = time.monotonic() + max_minutes * 60
    if links is None:
        local = hf_download(GDELT_ARTICLES)
        links = pq.read_table(local).to_pylist() if local else []
    rows = {r["article_id"]: r for r in _read_existing(upload)}

    reparsed = _reparse_no_text(rows, {x["article_id"]: x for x in links}, upload)

    queues: dict[str, list[dict[str, Any]]] = {}
    for link in sorted(links, key=lambda x: (x["domain"] or "", x["article_id"])):
        prev = rows.get(link["article_id"])
        if prev and prev["status"] in FINAL_STATUSES:
            continue
        if not is_article(link["domain"] or "", link["canonical_url"]):
            row = _base_row(link, started, 0)
            row.update(status="skipped_not_article")
            rows[link["article_id"]] = row
            continue
        queues.setdefault(link["domain"], []).append(link)
    planned = sum(len(q) for q in queues.values())
    log.info("to fetch: %s", {d: len(q) for d, q in queues.items()})

    pending_raw: list[Path] = []
    fails: Counter[str] = Counter()
    revision: str | None = None
    fetched = 0
    stopped = None

    def checkpoint(extra: list[Path] | None = None) -> None:
        nonlocal revision, pending_raw
        files = [*pending_raw, *_write_outputs(list(rows.values())), *(extra or [])]
        if upload:
            n_ok = sum(1 for r in rows.values() if r["status"] == "ok")
            revision = _upload(files, run_id, n_ok)
        pending_raw = []

    while any(queues.values()):
        if time.monotonic() > deadline:
            stopped = "time budget reached"
            break
        if limit is not None and fetched >= limit:
            stopped = f"limit {limit} reached"
            break
        for domain in list(queues):  # one page per site in turn
            q = queues[domain]
            if not q or (limit is not None and fetched >= limit):
                continue
            link = q.pop(0)
            prev = rows.get(link["article_id"])
            row = fetch_row(
                link,
                datetime.now(timezone.utc).replace(microsecond=0),
                (prev["attempts"] if prev else 0) + 1,
            )
            rows[link["article_id"]] = row
            fetched += 1
            if row["raw_file"]:
                pending_raw.append(LOCAL_ROOT / row["raw_file"])
            if row["status"] == "failed":
                fails[domain] += 1
                if fails[domain] >= BREAKER:
                    log.error(
                        "%s: %d failures in a row (%s); skipping it for this run",
                        domain,
                        BREAKER,
                        row["error"],
                    )
                    queues[domain] = []
            else:
                fails[domain] = 0
            if fetched % 50 == 0:
                log.info(
                    "fetched %d/%d | %s",
                    fetched,
                    planned,
                    dict(Counter(r["status"] for r in rows.values())),
                )
            if fetched % CHECKPOINT_EVERY == 0:
                checkpoint()

    statuses = Counter(r["status"] for r in rows.values())
    remaining = sum(len(q) for q in queues.values())
    summary = {
        "run_id": run_id,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "planned": planned,
        "fetched": fetched,
        "remaining": remaining,
        "stopped": stopped,
        "reparsed_no_text": reparsed,
        "status_totals": dict(statuses),
        "by_domain": {
            d: dict(Counter(r["status"] for r in rows.values() if r["domain"] == d))
            for d in sorted({r["domain"] for r in rows.values()})
        },
    }
    runlog = LOCAL_ROOT / "runs" / f"run-{run_id}.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    checkpoint(extra=[runlog])
    log.info(
        "run %s: fetched %d, remaining %d, totals %s",
        run_id,
        fetched,
        remaining,
        dict(statuses),
    )

    warnings = []
    if remaining or statuses.get("failed"):
        warnings.append(
            f"incomplete: {remaining} links not yet tried, "
            f"{statuses.get('failed', 0)} failed; re-run to continue"
        )
    if statuses.get("gone"):
        warnings.append(
            f"{statuses['gone']} links are gone (404/410 or soft 404): "
            "Wayback candidates"
        )
    if statuses.get("no_text"):
        warnings.append(f"{statuses['no_text']} pages loaded but no text was found")
    ok = [r for r in rows.values() if r["status"] == "ok"]
    dates = [r["first_public_at"] for r in ok if r["first_public_at"]]
    write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source="Article pages on dawn.com, tribune.com.pk and brecorder.com "
        "for the links in news_gdelt_index",
        fetch_mode="reproducible",
        row_count=len(rows),
        file_count=sum(1 for r in rows.values() if r["raw_file"]),
        date_range=(min(dates), max(dates)) if dates else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra={"last_run": summary},
        files=[
            LOCAL_ROOT / "articles.parquet",
            LOCAL_ROOT / "coverage_by_month.csv",
            runlog,
        ],
        warnings=warnings,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument(
        "--limit", type=int, help="max pages to fetch this run (testing)"
    )
    parser.add_argument("--max-minutes", type=float, default=MAX_MINUTES)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return run(
        upload=not args.no_upload, limit=args.limit, max_minutes=args.max_minutes
    )


if __name__ == "__main__":
    sys.exit(main())
