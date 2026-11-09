"""GDELT DOC 2.0 article index -> dataset ``news_gdelt_index`` (stream ``news``).

Lists PSX/KSE-100 articles from brecorder.com, dawn.com and tribune.com.pk
that GDELT has seen since 2017-01-01. GDELT gives URL, title and the time it
*saw* the article (``seendate``), not article text or publish time; the text
is recovered later (Wayback, dataset ``news_wayback_backfill``).

API facts (checked 2026-11-08, see docs/data-sources.md):

* ``https://api.gdeltproject.org/api/v2/doc/doc``, ``mode=artlist``,
  ``format=json``, ``startdatetime``/``enddatetime`` as ``YYYYMMDDHHMMSS``.
* Searchable from 2017-01-01 (blog, 2018-05-18; confirmed: a 2017-01 window
  returned 45 articles). Article lists only consider the last 3 months of a
  window, so windows here are 30 days (a 2017 month is ~150-250 articles;
  full windows are split, see below).
* At most 250 records per query: a full window is split in half until it
  is under the cap (or marked ``capped`` at one hour).
* Phrases under 4 characters are rejected ("PSX" fails).
* Rate limit "one request every 5 seconds". Shared IPs (cloud machines and
  GitHub's runners) still get many 429s: on 2026-11-09 one-week windows ran
  at ~1.6 min each with ~35% failing. So this job waits 10 s between
  requests, retries a 429 only twice, leaves the window ``failed`` for the
  next run, and stops itself after ``--max-minutes`` (default 200) so the
  4-hour Actions timeout never cuts off the final save.

State is kept on HF, so runs resume: every window has a row in
``windows.parquet`` (``ok`` / ``empty`` / ``capped`` / ``failed``). A run
queries only windows that are new, ``failed``, incomplete, or end within the
last 3 days. Changing the query or the window length starts a fresh set of
windows (both are in ``query_sha``); articles already found are kept.

Run from ``backend/``::

    python -m jobs.news_gdelt                        # all pending weeks, upload
    python -m jobs.news_gdelt --max-windows 3 --no-upload   # quick local test
    python -m jobs.news_gdelt --pull-manifest        # copy HF manifest into git
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import shutil
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from jobs.common import (
    DATA_DIR,
    MANIFEST_DIR,
    HttpError,
    RobotsDisallowedError,
    git_info,
    hf_download,
    http_get,
    upload_to_hf,
    write_manifest,
)
from jobs.news_common import article_id, canonical_url
from jobs.news_rss import load_config

log = logging.getLogger("jobs.news_gdelt")

STREAM = "news"
DATASET = "gdelt_index"
HF_PREFIX = "news/backfill/gdelt"
LOCAL_ROOT = DATA_DIR / "news" / "backfill" / "gdelt"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
DOMAINS = ("brecorder.com", "dawn.com", "tribune.com.pk")
TERMS = ('"KSE-100"', '"KSE100"', '"Pakistan Stock Exchange"')
QUERY = f"({' OR '.join(TERMS)}) ({' OR '.join(f'domainis:{d}' for d in DOMAINS)})"
WINDOW = timedelta(days=30)
# Window length is part of the key: a different grid must not reuse old rows.
QUERY_SHA = hashlib.sha256(f"{QUERY}|window={WINDOW.days}d".encode()).hexdigest()[:12]
START = datetime(2017, 1, 1, tzinfo=timezone.utc)
MIN_SPLIT = timedelta(hours=1)
MAX_RECORDS = 250
MIN_INTERVAL = 10.0
RETRIES = 2  # per request on 429/5xx; the window is retried next run anyway
BACKOFF = 20.0
MAX_MINUTES = 200.0  # stop and save before the 240-minute Actions timeout
PARTIAL_DAYS = 3  # weeks ending this recently are re-queried next run
CHECKPOINT_EVERY = 20  # windows between intermediate uploads

NOTES = (
    "GDELT DOC 2.0 article lists (title, URL, seendate) for PSX/KSE-100 terms "
    "on brecorder.com, dawn.com and tribune.com.pk, 30-day windows from "
    "2017-01-01. seendate is when GDELT saw the article, not its publish time "
    "(usually within hours, not guaranteed). No article text: see "
    "news_wayback_backfill. Coverage is limited to what GDELT crawled; on "
    "2026-11-08 Business Recorder appeared in 2017 but not in 2018 or 2026 "
    "test windows, and the latest week returned nothing. GDELT treats "
    "enddatetime as covering that whole day, so items seen on a window's last "
    "day also appear in the next window (deduped; window_start records the "
    "first window that returned the article). Failed windows are "
    "listed in windows.parquet and retried on the next run. GDELT data: "
    "unlimited use with citation to the GDELT Project and a link to "
    "https://www.gdeltproject.org/."
)

_TS = pa.timestamp("s", tz="UTC")
ARTICLE_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),  # sha256(canonical_url)[:16], as in news_rss
        ("url", pa.string()),
        ("canonical_url", pa.string()),
        ("title", pa.string()),
        ("seendate", _TS),
        ("domain", pa.string()),
        ("language", pa.string()),
        ("sourcecountry", pa.string()),
        ("socialimage", pa.string()),
        ("url_mobile", pa.string()),
        ("query_sha", pa.string()),
        ("window_start", _TS),
        ("first_fetched_at", _TS),
    ]
)
WINDOW_SCHEMA = pa.schema(
    [
        ("query_sha", pa.string()),
        ("window_start", _TS),
        ("window_end", _TS),
        ("status", pa.string()),  # ok | empty | capped | failed
        ("n_articles", pa.int32()),
        ("n_requests", pa.int32()),
        ("attempts", pa.int32()),
        ("error", pa.string()),
        ("last_attempt_at", _TS),
    ]
)
STATUS_RANK = {"ok": 0, "empty": 0, "capped": 1, "failed": 2}

# Response body, or an error message. Injected in tests.
Fetcher = Callable[[datetime, datetime], tuple[bytes | None, str | None]]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def make_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    s = start
    while s < end:
        out.append((s, min(s + WINDOW, end)))
        s += WINDOW
    return out


def gdelt_fetch(s: datetime, e: datetime) -> tuple[bytes | None, str | None]:
    params = {
        "query": QUERY,
        "mode": "artlist",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "dateasc",
        "startdatetime": _fmt(s),
        "enddatetime": _fmt(e),
    }
    try:
        resp = http_get(
            API,
            params=params,
            min_interval=MIN_INTERVAL,
            retries=RETRIES,
            backoff=BACKOFF,
            timeout=90,
        )
    except (HttpError, RobotsDisallowedError) as exc:
        return None, str(exc)[:300]
    return resp.content, None


def parse_articles(body: bytes) -> list[dict[str, Any]]:
    """GDELT artlist JSON -> list of article dicts. Raises ValueError on non-JSON
    (GDELT returns plain-text errors such as 'The specified phrase is too short.')."""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        raise ValueError(f"non-JSON response: {text[:200]!r}") from exc
    arts = data.get("articles", []) if isinstance(data, dict) else []
    return [a for a in arts if isinstance(a, dict) and a.get("url")]


def parse_seendate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class WindowResult:
    status: str
    articles: list[dict[str, Any]] = field(default_factory=list)
    bodies: list[tuple[datetime, datetime, bytes]] = field(default_factory=list)
    n_requests: int = 0
    error: str | None = None


def query_window(s: datetime, e: datetime, fetch: Fetcher) -> WindowResult:
    """Query one window, splitting it while results hit the 250 cap."""
    body, err = fetch(s, e)
    if body is None:
        return WindowResult("failed", n_requests=1, error=err)
    try:
        arts = parse_articles(body)
    except ValueError as exc:
        return WindowResult("failed", n_requests=1, error=str(exc))
    if len(arts) < MAX_RECORDS:
        return WindowResult("ok" if arts else "empty", arts, [(s, e, body)], 1)
    if e - s <= MIN_SPLIT:
        return WindowResult("capped", arts, [(s, e, body)], 1, "250-record cap at 1 h")
    mid = s + (e - s) / 2
    left, right = query_window(s, mid, fetch), query_window(mid, e, fetch)
    worst = max((left, right), key=lambda r: STATUS_RANK[r.status])
    status = worst.status if worst.status in ("failed", "capped") else "ok"
    return WindowResult(
        status,
        left.articles + right.articles,
        left.bodies + right.bodies,
        1 + left.n_requests + right.n_requests,
        left.error or right.error,
    )


# --------------------------------------------------------------------------
# State and outputs
# --------------------------------------------------------------------------


def _read(name: str, schema: pa.Schema, remote: bool) -> list[dict[str, Any]]:
    if remote:
        local = hf_download(f"{HF_PREFIX}/{name}")
    else:
        p = LOCAL_ROOT / name
        local = p if p.is_file() else None
    return pq.read_table(local, schema=schema).to_pylist() if local else []


def needs_query(row: dict[str, Any] | None, now: datetime) -> bool:
    """New, failed, incomplete (queried before the week ended), or recent weeks."""
    if row is None or row["status"] == "failed":
        return True
    if row["window_end"] < row["window_start"] + WINDOW:
        return True
    return bool(row["window_end"] > now - timedelta(days=PARTIAL_DAYS))


def _canonical_patterns() -> list[Any]:
    """Slug-dropping patterns from news_feeds.yaml, so ids match news_rss."""
    return [f.canonical_id_re for f in load_config().feeds if f.canonical_id_re]


def gdelt_canonical(url: str, patterns: list[Any]) -> str:
    canon = canonical_url(url)
    for pat in patterns:
        m = pat.match(canon)
        if m:
            return str(m.group(1))
    return canon


def coverage_rows(articles: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    c = Counter(
        (
            a["seendate"].strftime("%Y-%m") if a["seendate"] else "unknown",
            a["domain"] or "",
        )
        for a in articles
    )
    return sorted((m, d, n) for (m, d), n in c.items())


@dataclass
class Run:
    upload: bool
    run_id: str
    fetched_at: datetime
    articles: dict[str, dict[str, Any]]
    windows: dict[datetime, dict[str, Any]]  # keyed by window_start
    pending_files: list[Path] = field(default_factory=list)
    revision: str | None = None

    def write_outputs(self) -> list[Path]:
        LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
        arts = sorted(
            self.articles.values(), key=lambda a: (a["seendate"] is None, a["seendate"])
        )
        a_path = LOCAL_ROOT / "articles.parquet"
        pq.write_table(
            pa.Table.from_pylist(arts, schema=ARTICLE_SCHEMA),
            a_path,
            compression="zstd",
        )
        wins = [self.windows[k] for k in sorted(self.windows)]
        w_path = LOCAL_ROOT / "windows.parquet"
        pq.write_table(
            pa.Table.from_pylist(wins, schema=WINDOW_SCHEMA), w_path, compression="zstd"
        )
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["month", "domain", "articles"])
        writer.writerows(coverage_rows(arts))
        c_path = LOCAL_ROOT / "coverage_by_month.csv"
        c_path.write_text(buf.getvalue(), encoding="utf-8")
        return [a_path, w_path, c_path]

    def checkpoint(self, extra: list[Path] | None = None) -> None:
        files = [*self.pending_files, *self.write_outputs(), *(extra or [])]
        if self.upload:
            with tempfile.TemporaryDirectory(prefix="gdelt_upload_") as tmp:
                for f in files:
                    dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                self.revision = upload_to_hf(
                    tmp,
                    f"{HF_PREFIX}/",
                    stream=STREAM,
                    commit_message=f"{HF_PREFIX}: run {self.run_id} checkpoint "
                    f"({len(self.articles)} articles)",
                )
        self.pending_files = []


def run(
    *,
    upload: bool,
    start: datetime,
    end: datetime | None,
    max_windows: int | None,
    fetch: Fetcher = gdelt_fetch,
    max_minutes: float = MAX_MINUTES,
) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    deadline = time.monotonic() + max_minutes * 60
    end = min(end or fetched_at, fetched_at)
    r = Run(
        upload=upload,
        run_id=fetched_at.strftime("%Y%m%dT%H%M%SZ"),
        fetched_at=fetched_at,
        articles={
            a["article_id"]: a
            for a in _read("articles.parquet", ARTICLE_SCHEMA, upload)
        },
        windows={
            w["window_start"]: w
            for w in _read("windows.parquet", WINDOW_SCHEMA, upload)
            if w["query_sha"] == QUERY_SHA
        },
    )
    patterns = _canonical_patterns()
    todo = [
        (s, e)
        for s, e in make_windows(start, end)
        if needs_query(r.windows.get(s), fetched_at)
    ]
    if max_windows is not None:
        todo = todo[:max_windows]
    log.info("query %s: %d windows to query", QUERY_SHA, len(todo))

    new_articles = 0
    failed_this_run = 0
    stopped_early = False
    queried = 0
    for i, (s, e) in enumerate(todo, 1):
        if time.monotonic() > deadline:
            stopped_early = True
            log.warning(
                "time budget of %.0f min reached after %d windows", max_minutes, i - 1
            )
            break
        res = query_window(s, e, fetch)
        queried += 1
        failed_this_run += res.status == "failed"
        prev = r.windows.get(s)
        r.windows[s] = {
            "query_sha": QUERY_SHA,
            "window_start": s,
            "window_end": e,
            "status": res.status,
            "n_articles": len(res.articles),
            "n_requests": res.n_requests,
            "attempts": (prev["attempts"] if prev else 0) + 1,
            "error": res.error,
            "last_attempt_at": fetched_at,
        }
        for bs, be, body in res.bodies:
            raw = (
                LOCAL_ROOT
                / "raw"
                / f"q={QUERY_SHA}"
                / f"run={r.run_id}"
                / f"{_fmt(bs)}_{_fmt(be)}.json.gz"
            )
            raw.parent.mkdir(parents=True, exist_ok=True)
            with raw.open("xb") as fh:
                fh.write(gzip.compress(body, mtime=0))
            r.pending_files.append(raw)
        for a in res.articles:
            canon = gdelt_canonical(str(a["url"]), patterns)
            aid = article_id(canon)
            if aid in r.articles:
                continue
            r.articles[aid] = {
                "article_id": aid,
                "url": a.get("url"),
                "canonical_url": canon,
                "title": a.get("title"),
                "seendate": parse_seendate(a.get("seendate")),
                "domain": a.get("domain"),
                "language": a.get("language"),
                "sourcecountry": a.get("sourcecountry"),
                "socialimage": a.get("socialimage") or None,
                "url_mobile": a.get("url_mobile") or None,
                "query_sha": QUERY_SHA,
                "window_start": s,
                "first_fetched_at": fetched_at,
            }
            new_articles += 1
        log.info(
            "%d/%d %s..%s %s: %d articles (%d requests)%s",
            i,
            len(todo),
            s.date(),
            e.date(),
            res.status,
            len(res.articles),
            res.n_requests,
            f" error: {res.error}" if res.error else "",
        )
        if i % CHECKPOINT_EVERY == 0:
            r.checkpoint()

    statuses = Counter(w["status"] for w in r.windows.values())
    summary = {
        "run_id": r.run_id,
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "query": QUERY,
        "query_sha": QUERY_SHA,
        "windows_planned": len(todo),
        "windows_queried": queried,
        "stopped_early": stopped_early,
        "new_articles": new_articles,
        "windows_failed_this_run": failed_this_run,
        "total_articles": len(r.articles),
        "window_status_totals": dict(statuses),
    }
    runlog = LOCAL_ROOT / "runs" / f"run-{r.run_id}.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    r.checkpoint(extra=[runlog])

    warnings = []
    if stopped_early:
        warnings.append(
            f"time budget reached: {len(todo) - queried} windows left; "
            "re-run to continue"
        )
    if statuses.get("failed"):
        warnings.append(f"{statuses['failed']} windows failed; re-run to retry them")
    if statuses.get("capped"):
        warnings.append(
            f"{statuses['capped']} windows hit the 250 cap at 1 h; some missing"
        )
    seen = [a["seendate"] for a in r.articles.values() if a["seendate"]]
    manifest = write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source=f"GDELT DOC 2.0 API <{API}>, query: {QUERY}",
        fetch_mode="reproducible",
        row_count=len(r.articles),
        file_count=len(r.windows),
        date_range=(min(seen), max(seen)) if seen else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=r.revision,
        notes=NOTES,
        extra={"last_run": summary},
        files=[LOCAL_ROOT / "articles.parquet", LOCAL_ROOT / "windows.parquet", runlog],
        warnings=warnings,
    )
    if upload:
        upload_to_hf(
            manifest,
            f"{HF_PREFIX}/manifest.json",
            stream=STREAM,
            commit_message=f"{HF_PREFIX}: manifest for run {r.run_id}",
        )
    log.info(
        "run %s: %d windows, %d new articles, totals %s",
        r.run_id,
        len(todo),
        new_articles,
        dict(statuses),
    )
    return 1 if queried and failed_this_run == queried else 0


def pull_manifest() -> int:
    local = hf_download(f"{HF_PREFIX}/manifest.json")
    if local is None:
        log.error("no manifest on HF yet")
        return 1
    dest = MANIFEST_DIR / f"{STREAM}_{DATASET}.json"
    shutil.copyfile(local, dest)
    log.info("wrote %s", dest)
    return 0


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--start", type=_date, default=START, help="YYYY-MM-DD")
    parser.add_argument("--end", type=_date, help="YYYY-MM-DD (default: now)")
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--max-minutes", type=float, default=MAX_MINUTES)
    parser.add_argument("--pull-manifest", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.pull_manifest:
        return pull_manifest()
    return run(
        upload=not args.no_upload,
        start=args.start,
        end=args.end,
        max_windows=args.max_windows,
        max_minutes=args.max_minutes,
    )


if __name__ == "__main__":
    sys.exit(main())
