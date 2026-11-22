"""The Nation's archive, 2017 onward -> dataset ``news_nation_archive``.

The Nation (nation.com.pk) publishes one sitemap per day back to 2008
(``/sitemap_YYYY-MM-DD.xml``, listed in ``/sitemap.xml``), each with that
day's article links. Its terms allow academic, non-commercial use and its
robots.txt allows our User-Agent (checked 2026-11-20; see
``docs/data-sources.md``). This fills years the other news datasets miss,
above all Business Recorder's 2018-2022 gap in GDELT.

Two steps, each resumable, one request every ``MIN_INTERVAL`` seconds:

``index``
    Reads the sitemap index, then every daily sitemap from ``--start`` to
    ``--end`` not yet read. Each response is saved unmodified
    (``raw/sitemaps/sitemap_<day>.xml.gz``). Every listed link goes into
    ``links.parquet`` (all sections: sport, politics, ...). Per-day status is
    in ``days.parquet``. The index leaves out, in every month, the day
    whose number is today's date (the 20th when read on 2026-11-20, the 21st
    on 2026-11-21), and 2022-02-17, although those daily sitemaps exist. So
    every calendar day in range is read; ``in_index`` records whether the
    last index run's copy listed the day (it changes from run to run), and
    an unlisted day that answers 404 is ``gone``.

``text``
    Picks links whose URL slug matches ``SLUG_RE`` (market words: psx, kse,
    stocks, shares, bourse, ...), fetches each page
    (``raw/pages/<article_id>.html.gz``) and parses it with
    ``jobs.news_common.parse_article``. ``is_market`` marks pages whose title
    or text names the PSX, the KSE indices or a stock exchange/market, so
    off-topic slug matches can be dropped by readers. ``SLUG_RULE`` records
    which filter chose a row. A page that redirects to a different article
    path is ``redirected``, not ``ok``: its text belongs to another story.

Dates: ``published_at`` is the page's own date. The URL carries the day the
article was filed under (``/DD-Mon-YYYY/slug``, the print-edition day), so
``first_public_at = min(published_at, end of that day in PKT)`` and
``published_after_url_day`` flags pages dated later than their URL day
(updates). Use ``first_public_at`` as the point-in-time key.

Run from ``backend/``::

    python -m jobs.news_nation_archive index            # resume, upload
    python -m jobs.news_nation_archive text --max-minutes 105
    python -m jobs.news_nation_archive index --no-upload --limit 3
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
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from jobs.news_common import article_id, canonical_url, parse_article

log = logging.getLogger("jobs.news_nation_archive")

STREAM = "news"
DATASET = "nation_archive"
HF_PREFIX = "news/backfill/nation"
LOCAL_ROOT = DATA_DIR / "news" / "backfill" / "nation"
BASE = "https://www.nation.com.pk"
INDEX_URL = f"{BASE}/sitemap.xml"
START = date(2017, 1, 1)
MIN_INTERVAL = 3.0  # seconds between requests
CHECKPOINT_EVERY = 300  # requests between uploads
MAX_MINUTES = 105.0
BREAKER = 10  # consecutive failures before the run stops
PKT = timezone(timedelta(hours=5))

#: Market words in the URL slug. Broad on purpose (pages are checked with
#: MARKET_RE afterwards); change SLUG_RULE whenever this changes.
SLUG_RE = re.compile(
    r"(?:^|-)(psx|kse|kse100|stocks?|shares?|bourse|equit(?:y|ies)|bull(?:ish|s)?"
    r"|bear(?:ish|s)?|index|points?|investors?|trading|traded|market|markets)(?:-|$)"
)
SLUG_RULE = "slug-v1"
MARKET_RE = re.compile(
    r"\b(PSX|KSE[- ]?(100|30|All)?|Pakistan Stock Exchange|Karachi Stock Exchange"
    r"|stock exchange|stock market|bourse)\b",
    re.I,
)
_DAY_IN_URL = re.compile(r"nation\.com\.pk/(\d{2}-[A-Za-z]{3}-\d{4})/([^/?#]+)")
_SITEMAP_DAY = re.compile(r"sitemap_(\d{4}-\d{2}-\d{2})\.xml")
_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S)

NOTES = (
    "The Nation's daily sitemaps (one per day, 2017 onward) and the market "
    "articles picked from them by URL slug (SLUG_RE, rule in slug_rule), "
    "fetched from nation.com.pk. links.parquet lists every article link in "
    "the sitemaps (all sections), including days the sitemap index omits "
    "(in_index in days.parquet); articles.parquet one row per picked link "
    "with a status: ok, no_text, gone (404/410), redirected (landed on a "
    "different article; text kept but not this URL's) or failed (retried). "
    "is_market marks pages naming the PSX/KSE/a stock exchange; the slug "
    "filter misses market stories whose URL has none of its words. "
    "first_public_at = min(page published_at, end of the URL's day in PKT) is "
    "the point-in-time key. Terms: academic, non-commercial use allowed; "
    "private dataset, not redistributed."
)

_TS = pa.timestamp("s", tz="UTC")
DAYS_SCHEMA = pa.schema(
    [
        ("day", pa.string()),
        ("status", pa.string()),  # ok | failed | gone
        ("http_status", pa.int32()),
        ("error", pa.string()),
        ("n_links", pa.int32()),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("attempts", pa.int32()),
        ("in_index", pa.bool_()),  # listed in /sitemap.xml at the last index run
    ]
)
LINKS_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("url", pa.string()),
        ("sitemap_day", pa.string()),
        ("url_day", pa.string()),
        ("slug", pa.string()),
        ("lastmod", _TS),
    ]
)
ARTICLES_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("url", pa.string()),
        ("final_url", pa.string()),
        ("url_day", pa.string()),
        ("lastmod", _TS),
        ("slug_rule", pa.string()),
        ("status", pa.string()),
        ("http_status", pa.int32()),
        ("error", pa.string()),
        ("title", pa.string()),
        ("published_at", _TS),
        ("published_at_raw", pa.string()),
        ("published_tz", pa.string()),
        ("modified_at", _TS),
        ("first_public_at", _TS),
        ("published_after_url_day", pa.bool_()),
        ("is_market", pa.bool_()),
        ("authors", pa.list_(pa.string())),
        ("summary", pa.string()),
        ("full_text", pa.string()),
        ("language", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("fetched_at", _TS),
        ("raw_file", pa.string()),
        ("attempts", pa.int32()),
    ]
)
ARTICLE_FINAL = frozenset({"ok", "no_text", "gone", "redirected"})

Fetch = Callable[[str], HttpResponse]


def _fetch(url: str) -> HttpResponse:
    return http_get(url, min_interval=MIN_INTERVAL, retries=1)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_sitemap_index(xml: bytes) -> list[str]:
    """Days (YYYY-MM-DD) that have a daily sitemap, sorted."""
    return sorted(set(_SITEMAP_DAY.findall(xml.decode("utf-8", "replace"))))


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_day_sitemap(xml: bytes, day: str) -> list[dict[str, Any]]:
    """One row per article link in a daily sitemap."""
    rows = []
    for block in _URL_BLOCK.findall(xml.decode("utf-8", "replace")):
        loc = _LOC.search(block)
        if not loc:
            continue
        m = _DAY_IN_URL.search(loc.group(1))
        if not m:  # not an article URL
            continue
        canon = canonical_url(loc.group(1))
        lm = _LASTMOD.search(block)
        rows.append(
            {
                "article_id": article_id(canon),
                "url": canon,
                "sitemap_day": day,
                "url_day": datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat(),
                "slug": m.group(2),
                "lastmod": _ts(lm.group(1) if lm else None),
            }
        )
    return rows


def slug_matches(slug: str) -> bool:
    return bool(SLUG_RE.search(slug.lower()))


def first_public(
    published_at: datetime | None, url_day: str
) -> tuple[datetime | None, bool]:
    """(point-in-time key, whether the page date is after the URL's day)."""
    d = date.fromisoformat(url_day)
    day_end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=PKT).astimezone(
        timezone.utc
    )
    if published_at is None:
        return day_end, False
    return min(published_at, day_end), published_at > day_end


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def _read(name: str, schema: pa.Schema, upload: bool) -> list[dict[str, Any]]:
    local: Path | None
    if upload:
        local = hf_download(f"{HF_PREFIX}/{name}")
    else:
        p = LOCAL_ROOT / name
        local = p if p.is_file() else None
    return pq.read_table(local, schema=schema).to_pylist() if local else []


def _write(name: str, rows: list[dict[str, Any]], schema: pa.Schema) -> Path:
    path = LOCAL_ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")
    return path


def _save_raw(rel: str, content: bytes) -> Path:
    path = LOCAL_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    return path


def _upload(files: list[Path], run_id: str, what: str) -> str:
    with tempfile.TemporaryDirectory(prefix="nation_upload_") as tmp:
        for f in files:
            dest = Path(tmp) / f.relative_to(LOCAL_ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return upload_to_hf(
            tmp,
            f"{HF_PREFIX}/",
            stream=STREAM,
            commit_message=f"{HF_PREFIX}: {what} run {run_id}",
        )


def coverage_rows(
    links: list[dict[str, Any]], arts: list[dict[str, Any]]
) -> list[list[Any]]:
    """Per month: links in sitemaps, slug matches, fetched with text, market."""
    c: Counter[tuple[str, str]] = Counter()
    for r in links:
        c[(r["url_day"][:7], "links")] += 1
    for r in arts:
        m = r["url_day"][:7]
        c[(m, "picked")] += 1
        if r["status"] == "ok":
            c[(m, "ok")] += 1
        if r["is_market"]:
            c[(m, "market")] += 1
    months = sorted({m for m, _ in c})
    return [
        [m, c[(m, "links")], c[(m, "picked")], c[(m, "ok")], c[(m, "market")]]
        for m in months
    ]


def _write_coverage(links: list[dict[str, Any]], arts: list[dict[str, Any]]) -> Path:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["month", "links", "picked", "ok", "market"])
    w.writerows(coverage_rows(links, arts))
    path = LOCAL_ROOT / "coverage_by_month.csv"
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class _Budget:
    def __init__(self, max_minutes: float, limit: int | None) -> None:
        self.deadline = time.monotonic() + max_minutes * 60
        self.limit = limit
        self.done = 0

    def stop_reason(self) -> str | None:
        if time.monotonic() > self.deadline:
            return "time budget reached"
        if self.limit is not None and self.done >= self.limit:
            return f"limit {self.limit} reached"
        return None


def run_index(
    *,
    upload: bool,
    start: date,
    end: date,
    budget: _Budget,
    fetch: Fetch,
    run_id: str,
) -> dict[str, Any]:
    days = {r["day"]: r for r in _read("days.parquet", DAYS_SCHEMA, upload)}
    links = {
        (r["article_id"], r["sitemap_day"]): r
        for r in _read("links.parquet", LINKS_SCHEMA, upload)
    }
    files: list[Path] = []
    revision: str | None = None
    try:
        resp = fetch(INDEX_URL)
    except (HttpError, RobotsDisallowedError) as exc:
        log.error("index: sitemap index failed: %s", exc)
        return {
            "step": "index",
            "days_remaining": None,
            "links": len(links),
            "stopped": f"sitemap index failed ({exc}); 1 failures in a row",
            "revision": None,
            "_links": list(links.values()),
        }
    files.append(_save_raw(f"raw/sitemap_index/{run_id}.xml.gz", resp.content))
    listed = [
        d
        for d in parse_sitemap_index(resp.content)
        if start.isoformat() <= d <= end.isoformat()
    ]
    calendar = (end - start).days + 1
    listed_set = set(listed)
    unlisted = [
        d
        for d in (
            (start + timedelta(days=i)).isoformat() for i in range(calendar - 1, -1, -1)
        )
        if d not in listed_set
    ]
    for d, day_row in days.items():
        day_row["in_index"] = d in listed_set
    todo = [
        d
        for d in listed + unlisted
        if days.get(d, {}).get("status") not in ("ok", "gone")
    ]
    log.info(
        "index: %d days listed in range, %d calendar days not listed, %d to read",
        len(listed),
        len(unlisted),
        len(todo),
    )
    fails = 0
    stopped = None

    def checkpoint() -> None:
        nonlocal files, revision
        out = [
            *files,
            _write(
                "days.parquet",
                sorted(days.values(), key=lambda r: r["day"]),
                DAYS_SCHEMA,
            ),
            _write(
                "links.parquet",
                sorted(links.values(), key=lambda r: (r["url_day"], r["url"])),
                LINKS_SCHEMA,
            ),
        ]
        if upload:
            revision = _upload(out, run_id, "index")
        files = []

    for day in todo:
        stopped = budget.stop_reason()
        if stopped:
            break
        prev = days.get(day)
        row: dict[str, Any] = {
            "day": day,
            "status": "failed",
            "http_status": None,
            "error": None,
            "n_links": None,
            "fetched_at": datetime.now(timezone.utc).replace(microsecond=0),
            "raw_file": None,
            "attempts": (prev["attempts"] if prev else 0) + 1,
            "in_index": day in listed_set,
        }
        try:
            r = fetch(f"{BASE}/sitemap_{day}.xml")
        except HttpError as exc:
            row.update(
                http_status=exc.status,
                error=str(exc)[:300],
                status="gone" if exc.status in (404, 410) else "failed",
            )
        except RobotsDisallowedError as exc:
            row.update(error=str(exc)[:300])
        else:
            rel = f"raw/sitemaps/sitemap_{day}.xml.gz"
            files.append(_save_raw(rel, r.content))
            parsed = parse_day_sitemap(r.content, day)
            for link in parsed:
                links[(link["article_id"], day)] = link
            row.update(
                status="ok", http_status=r.status, n_links=len(parsed), raw_file=rel
            )
        days[day] = row
        budget.done += 1
        fails = fails + 1 if row["status"] == "failed" else 0
        if fails >= BREAKER:
            stopped = f"{BREAKER} failures in a row ({row['error']})"
            log.error("index: %s; stopping", stopped)
            break
        if budget.done % 50 == 0:
            log.info(
                "index: %d/%d days read, %d links", budget.done, len(todo), len(links)
            )
        if budget.done % CHECKPOINT_EVERY == 0:
            checkpoint()
    checkpoint()
    statuses = Counter(r["status"] for r in days.values())
    in_range = [d for d in days if start.isoformat() <= d <= end.isoformat()]
    return {
        "step": "index",
        "days_listed_in_range": len(listed),
        "calendar_days_in_range": calendar,
        "days_not_in_sitemap_index": len(unlisted),
        "unlisted_days_ok": sum(
            1 for d in unlisted if days.get(d, {}).get("status") == "ok"
        ),
        "days_read_this_run": budget.done,
        "days_remaining": sum(
            1 for d in todo if days.get(d, {}).get("status") not in ("ok", "gone")
        ),
        "day_status": dict(statuses),
        "days_in_range_ok": sum(1 for d in in_range if days[d]["status"] == "ok"),
        "links": len(links),
        "stopped": stopped,
        "revision": revision,
        "_links": list(links.values()),
    }


def redirected(url: str, final_url: str | None) -> bool:
    """True if the page landed on a different article (The Nation sends some
    removed stories to an unrelated one): the text is not this URL's."""
    if not final_url:
        return False
    return urlsplit(url).path.rstrip("/") != urlsplit(final_url).path.rstrip("/")


def mark_redirect(row: dict[str, Any]) -> dict[str, Any]:
    if row["status"] == "ok" and redirected(row["url"], row["final_url"]):
        row["status"] = "redirected"
        row["error"] = f"redirected to {row['final_url']}"[:300]
    return row


def row_from_page(
    link: dict[str, Any], resp: HttpResponse, fetched_at: datetime, attempts: int
) -> dict[str, Any]:
    f = parse_article(resp.content, resp.url)
    first, after = first_public(f["published_at"], link["url_day"])
    text = f["full_text"]
    return mark_redirect(
        {
            **_base(link, fetched_at, attempts),
            "final_url": resp.url,
            "status": "ok" if text else "no_text",
            "http_status": resp.status,
            "title": f["title"],
            "published_at": f["published_at"],
            "published_at_raw": f["published_at_raw"],
            "published_tz": f["published_tz"],
            "modified_at": f["modified_at"],
            "first_public_at": first,
            "published_after_url_day": after,
            "is_market": bool(MARKET_RE.search(f"{f['title'] or ''}\n{text or ''}")),
            "authors": f["authors"],
            "summary": f["summary"],
            "full_text": text,
            "language": f["language"],
            "tags": f["tags"],
        }
    )


def _base(link: dict[str, Any], fetched_at: datetime, attempts: int) -> dict[str, Any]:
    first, _ = first_public(None, link["url_day"])
    return {
        "article_id": link["article_id"],
        "url": link["url"],
        "final_url": None,
        "url_day": link["url_day"],
        "lastmod": link["lastmod"],
        "slug_rule": SLUG_RULE,
        "status": "failed",
        "http_status": None,
        "error": None,
        "title": None,
        "published_at": None,
        "published_at_raw": None,
        "published_tz": None,
        "modified_at": None,
        "first_public_at": first,
        "published_after_url_day": False,
        "is_market": False,
        "authors": [],
        "summary": None,
        "full_text": None,
        "language": None,
        "tags": [],
        "fetched_at": fetched_at,
        "raw_file": None,
        "attempts": attempts,
    }


def run_text(
    *,
    upload: bool,
    start: date,
    end: date,
    budget: _Budget,
    fetch: Fetch,
    run_id: str,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if links is None:
        links = _read("links.parquet", LINKS_SCHEMA, upload)
    arts = {  # mark_redirect: rows stored before the redirect rule, no refetch
        r["article_id"]: mark_redirect(r)
        for r in _read("articles.parquet", ARTICLES_SCHEMA, upload)
    }
    picked: dict[str, dict[str, Any]] = {}
    for link in links:
        if start.isoformat() <= link["url_day"] <= end.isoformat() and slug_matches(
            link["slug"]
        ):
            picked.setdefault(link["article_id"], link)
    todo = sorted(
        (
            lk
            for aid, lk in picked.items()
            if arts.get(aid, {}).get("status") not in ARTICLE_FINAL
        ),
        key=lambda r: (r["url_day"], r["url"]),
    )
    log.info("text: %d links picked by slug, %d to fetch", len(picked), len(todo))
    files: list[Path] = []
    revision: str | None = None
    fails = 0
    stopped = None

    def checkpoint() -> None:
        nonlocal files, revision
        rows = sorted(arts.values(), key=lambda r: (r["url_day"], r["url"]))
        out = [
            *files,
            _write("articles.parquet", rows, ARTICLES_SCHEMA),
            _write_coverage(links or [], rows),
        ]
        if upload:
            revision = _upload(out, run_id, "text")
        files = []

    for link in todo:
        stopped = budget.stop_reason()
        if stopped:
            break
        prev = arts.get(link["article_id"])
        attempts = (prev["attempts"] if prev else 0) + 1
        now = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            resp = fetch(link["url"])
        except HttpError as exc:
            row = _base(link, now, attempts)
            row.update(
                http_status=exc.status,
                error=str(exc)[:300],
                status="gone" if exc.status in (404, 410) else "failed",
            )
        except RobotsDisallowedError as exc:
            row = _base(link, now, attempts)
            row.update(error=str(exc)[:300])
        else:
            rel = f"raw/pages/{link['url_day'][:4]}/{link['article_id']}.html.gz"
            files.append(_save_raw(rel, resp.content))
            row = row_from_page(link, resp, now, attempts)
            row["raw_file"] = rel
        arts[link["article_id"]] = row
        budget.done += 1
        fails = fails + 1 if row["status"] == "failed" else 0
        if fails >= BREAKER:
            stopped = f"{BREAKER} failures in a row ({row['error']})"
            log.error("text: %s; stopping", stopped)
            break
        if budget.done % 50 == 0:
            log.info(
                "text: %d/%d | %s",
                budget.done,
                len(todo),
                dict(Counter(r["status"] for r in arts.values())),
            )
        if budget.done % CHECKPOINT_EVERY == 0:
            checkpoint()
    checkpoint()
    rows = list(arts.values())
    return {
        "step": "text",
        "slug_rule": SLUG_RULE,
        "picked": len(picked),
        "fetched_this_run": budget.done,
        "remaining": sum(
            1
            for lk in todo
            if arts.get(lk["article_id"], {}).get("status") not in ARTICLE_FINAL
        ),
        "status_totals": dict(Counter(r["status"] for r in rows)),
        "is_market": sum(1 for r in rows if r["is_market"]),
        "published_after_url_day": sum(1 for r in rows if r["published_after_url_day"]),
        "stopped": stopped,
        "revision": revision,
        "_rows": rows,
        "_links": links,
    }


def run(
    step: str,
    *,
    upload: bool,
    start: date = START,
    end: date | None = None,
    max_minutes: float = MAX_MINUTES,
    limit: int | None = None,
    fetch: Fetch = _fetch,
) -> int:
    started = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    if end is None:  # yesterday in Pakistan: today's sitemap is still growing
        end = datetime.now(PKT).date() - timedelta(days=1)
    budget = _Budget(max_minutes, limit)
    if step == "index":
        summary = run_index(
            upload=upload,
            start=start,
            end=end,
            budget=budget,
            fetch=fetch,
            run_id=run_id,
        )
    else:
        summary = run_text(
            upload=upload,
            start=start,
            end=end,
            budget=budget,
            fetch=fetch,
            run_id=run_id,
        )
    links = summary.pop("_links")
    rows = summary.pop("_rows", None)
    if rows is None:
        rows = _read("articles.parquet", ARTICLES_SCHEMA, upload)
    summary.update(
        run_id=run_id,
        started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        start=start.isoformat(),
        end=end.isoformat(),
        git=git_info(),
    )
    runlog = LOCAL_ROOT / "runs" / f"run-{run_id}-{step}.json"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    runlog.write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    revision = summary["revision"]
    if upload:
        revision = _upload([runlog], run_id, step)
    log.info(
        "%s run %s: %s", step, run_id, {k: summary[k] for k in summary if k != "git"}
    )

    warnings = []
    if summary.get("stopped"):
        warnings.append(
            f"{step}: stopped early ({summary['stopped']}); re-run to continue"
        )
    if step == "index" and summary["days_remaining"]:
        warnings.append(f"index: {summary['days_remaining']} days not yet read")
    if step == "text" and summary["remaining"]:
        warnings.append(f"text: {summary['remaining']} picked links not yet fetched")
    ok = [r for r in rows if r["status"] == "ok"]
    dates = [r["first_public_at"] for r in ok if r["first_public_at"]]
    files = [
        p
        for p in (
            LOCAL_ROOT / "days.parquet",
            LOCAL_ROOT / "links.parquet",
            LOCAL_ROOT / "articles.parquet",
            LOCAL_ROOT / "coverage_by_month.csv",
        )
        if p.is_file()
    ]
    write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source=f"The Nation daily sitemaps <{BASE}/sitemap_YYYY-MM-DD.xml> "
        "and article pages",
        fetch_mode="reproducible",
        row_count=len(rows),
        file_count=len(links),
        date_range=(min(dates), max(dates)) if dates else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra={
            "last_run": summary,
            "slug_re": SLUG_RE.pattern,
            "market_re": MARKET_RE.pattern,
            "row_count_is": "articles.parquet rows (picked links)",
            "file_count_is": "links.parquet rows (all article links in sitemaps)",
        },
        files=[*files, runlog],
        warnings=warnings,
    )
    stopped = str(summary.get("stopped") or "")
    return 1 if "failures in a row" in stopped else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=["index", "text"])
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--max-minutes", type=float, default=MAX_MINUTES)
    parser.add_argument("--limit", type=int, help="max requests this run (testing)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return run(
        args.step,
        upload=not args.no_upload,
        start=args.start,
        end=args.end,
        max_minutes=args.max_minutes,
        limit=args.limit,
    )


if __name__ == "__main__":
    sys.exit(main())
