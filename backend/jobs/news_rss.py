"""RSS news archiver -> dataset ``news_rss_articles`` (stream ``news``).

RSS feeds only expose their latest items, so anything not captured in time is
lost for good. This job is an **append-only snapshot**: the copy on Hugging
Face (``news/rss/``) is the archive of record. Run it hourly (GitHub Actions:
``.github/workflows/news_rss_hourly.yml``).

Each run:

1. fetches every feed in ``jobs/config/news_feeds.yaml`` with ``http_get``;
2. saves each response unmodified (gzipped) to
   ``raw/fetch_date=D/<run_id>_<feed>.xml.gz``;
3. parses items into the article schema below. ``filter`` feeds keep only
   items whose title or summary matches the feed's filter;
4. dedupes by ``article_id = sha256(canonical_url)[:16]``, both within the run
   and against ``index/article_ids.parquet`` on HF (first sighting wins);
5. writes new articles to ``articles/fetch_date=D/part-<run_id>.parquet``;
6. writes a run log ``runs/fetch_date=D/run-<run_id>.json`` with per-feed
   status, item counts and newest item, so gaps stay visible;
7. uploads this run's files to ``news/rss/`` in one HF commit;
8. writes ``backend/data_manifests/news_rss_articles.json`` and uploads a copy
   to ``news/rss/manifest.json``. Actions runs cannot commit to git, so
   refresh the committed manifest with ``--pull-manifest``.

Run from ``backend/``::

    python -m jobs.news_rss                  # fetch + upload (needs HF_TOKEN)
    python -m jobs.news_rss --no-upload      # local test: stage under data/news/rss/
    python -m jobs.news_rss --feeds dawn_business,propakistani
    python -m jobs.news_rss --pull-manifest  # copy the HF manifest into git
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import logging
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from jobs.common import (
    DATA_DIR,
    HF_REPO,
    HF_REPO_TYPE,
    MANIFEST_DIR,
    HttpError,
    RobotsDisallowedError,
    get_secret,
    git_info,
    hf_download,
    http_get,
    sha256_file,
    upload_to_hf,
    write_manifest,
)
from jobs.news_common import (  # re-exported for callers and tests
    article_id,
    canonical_url,
    detect_language,
    html_to_text,
    parse_article,
    parse_published,
)

log = logging.getLogger("jobs.news_rss")

STREAM = "news"
DATASET = "rss_articles"
HF_PREFIX = "news/rss"
CONFIG_PATH = Path(__file__).resolve().parent / "config" / "news_feeds.yaml"
LOCAL_ROOT = DATA_DIR / "news" / "rss"
INDEX_REL = "index/article_ids.parquet"
MANIFEST_REL = "manifest.json"
ROLES = frozenset({"primary", "filter", "test"})

NOTES = (
    "RSS feeds expose only their latest items and cannot be re-fetched "
    "historically, so this is an append-only snapshot: the HF copy under "
    "news/rss/ is the archive of record and must be backed up and snapshotted "
    "at code freeze. Coverage before the first run (2026-11-08) is only what "
    "each feed still showed then. row_count = articles in the archive; "
    "file_count = article parquet partitions; files = this run's outputs. "
    "Dedupe is by canonical URL (slug dropped where configured); the first "
    "sighting of an article is kept and later edits are not captured, except "
    "in the raw feed snapshots. full_text is plain text from content:encoded "
    "when the feed includes it. Business Recorder's RSS shows only 30 items and "
    "publishes in bursts, so its news sitemap (100 latest) is also read each run "
    "and missing articles are fetched from their pages (feed_role='sitemap', "
    "full_text_source='article_page'). "
    "language is the feed-declared code, or 'ur' "
    "when the title is mostly Arabic script. tickers_mentioned is empty until "
    "tagging is built."
)

_TS = pa.timestamp("s", tz="UTC")
ARTICLE_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("url", pa.string()),
        ("canonical_url", pa.string()),
        ("source", pa.string()),
        ("feed_name", pa.string()),
        ("feed_url", pa.string()),
        ("feed_role", pa.string()),
        ("guid", pa.string()),
        ("title", pa.string()),
        ("published_at", _TS),  # UTC
        ("published_at_raw", pa.string()),  # exactly as in the feed
        ("published_tz", pa.string()),  # original UTC offset, e.g. +05:00
        ("fetched_at", _TS),
        ("summary", pa.string()),
        ("full_text", pa.string()),
        ("full_text_source", pa.string()),  # "content:encoded" or null
        ("language", pa.string()),
        ("categories", pa.list_(pa.string())),
        ("author", pa.string()),
        ("tickers_mentioned", pa.list_(pa.string())),
        ("run_id", pa.string()),
    ]
)
INDEX_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("canonical_url", pa.string()),
        ("feed_name", pa.string()),
        ("published_at", _TS),
        ("first_fetched_at", _TS),
        ("partition", pa.string()),
    ]
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str
    source: str
    url: str
    role: str
    filter_name: str | None
    filter_re: re.Pattern[str] | None
    canonical_id_re: re.Pattern[str] | None
    store_full_text: bool


@dataclass(frozen=True)
class Sitemap:
    """A Google News sitemap listing a site's latest articles (URL, title, day).

    Used where the RSS feed is too short to catch publishing bursts (Business
    Recorder: 30 RSS items vs 100 sitemap items). Each article not yet in the
    archive is fetched once from its page for the exact time and full text.
    """

    name: str
    source: str
    url: str
    canonical_id_re: re.Pattern[str] | None
    max_fetch: int
    min_interval: float
    store_full_text: bool


@dataclass(frozen=True)
class Config:
    version: int
    sha256: str
    min_interval: float
    stale_after_days: int
    feeds: tuple[Feed, ...]
    sitemaps: tuple[Sitemap, ...] = ()


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    filters = {k: v["pattern"].strip() for k, v in (raw.get("filters") or {}).items()}
    feeds: list[Feed] = []
    for f in raw["feeds"]:
        role = f["role"]
        if role not in ROLES:
            raise ValueError(f"feed {f['name']}: unknown role {role!r}")
        filter_name = f.get("filter")
        if (role == "filter") != bool(filter_name):
            raise ValueError(f"feed {f['name']}: role 'filter' needs a filter (only)")
        if filter_name and filter_name not in filters:
            raise ValueError(f"feed {f['name']}: unknown filter {filter_name!r}")
        if not f["url"].startswith("https://"):
            raise ValueError(f"feed {f['name']}: url must be https")
        pattern = f.get("canonical_id_pattern")
        feeds.append(
            Feed(
                name=f["name"],
                source=f["source"],
                url=f["url"],
                role=role,
                filter_name=filter_name,
                filter_re=(
                    re.compile(filters[filter_name], re.I) if filter_name else None
                ),
                canonical_id_re=re.compile(pattern) if pattern else None,
                store_full_text=bool(f.get("store_full_text", True)),
            )
        )
    sitemaps = []
    for sm in raw.get("sitemaps") or []:
        if not sm["url"].startswith("https://"):
            raise ValueError(f"sitemap {sm['name']}: url must be https")
        pattern = sm.get("canonical_id_pattern")
        sitemaps.append(
            Sitemap(
                name=sm["name"],
                source=sm["source"],
                url=sm["url"],
                canonical_id_re=re.compile(pattern) if pattern else None,
                max_fetch=int(sm.get("max_fetch_per_run", 150)),
                min_interval=float(sm.get("min_interval_seconds", 3.0)),
                store_full_text=bool(sm.get("store_full_text", True)),
            )
        )
    names = [f.name for f in feeds] + [s.name for s in sitemaps]
    if len(names) != len(set(names)):
        raise ValueError("feed and sitemap names must be unique")
    return Config(
        version=int(raw.get("version", 1)),
        sha256=sha256_file(path),
        min_interval=float(defaults.get("min_interval_seconds", 2.0)),
        stale_after_days=int(defaults.get("stale_after_days", 7)),
        feeds=tuple(feeds),
        sitemaps=tuple(sitemaps),
    )


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Per-feed processing
# --------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_feed(
    content: bytes, feed: Feed, fetched_at: datetime, run_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse one feed response into article records plus stats."""
    parsed = feedparser.parse(content)
    feed_language = parsed.feed.get("language")
    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "items": len(parsed.entries),
        "kept": 0,
        "filtered_out": 0,
        "no_link": 0,
        "no_date": 0,
        "with_full_text": 0,
        "parse_warning": str(parsed.get("bozo_exception")) if parsed.bozo else None,
    }
    dates: list[datetime] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            stats["no_link"] += 1
            continue
        title = html_to_text(entry.get("title")) or ""
        summary = html_to_text(entry.get("summary"))
        published_at, published_raw, published_tz = parse_published(entry)
        if published_at is None:
            stats["no_date"] += 1
        else:
            dates.append(published_at)
        if feed.filter_re is not None and not feed.filter_re.search(
            f"{title}\n{summary or ''}"
        ):
            stats["filtered_out"] += 1
            continue
        full_text = None
        contents = entry.get("content") or []
        if contents and feed.store_full_text:
            full_text = html_to_text(contents[0].get("value"))
        if full_text:
            stats["with_full_text"] += 1
        canonical = canonical_url(link, feed.canonical_id_re)
        records.append(
            {
                "article_id": article_id(canonical),
                "url": link,
                "canonical_url": canonical,
                "source": feed.source,
                "feed_name": feed.name,
                "feed_url": feed.url,
                "feed_role": feed.role,
                "guid": entry.get("id"),
                "title": title,
                "published_at": published_at,
                "published_at_raw": published_raw,
                "published_tz": published_tz,
                "fetched_at": fetched_at,
                "summary": summary if feed.store_full_text else None,
                "full_text": full_text,
                "full_text_source": "content:encoded" if full_text else None,
                "language": detect_language(feed_language, title),
                "categories": [
                    t.get("term") for t in entry.get("tags", []) if t.get("term")
                ],
                "author": entry.get("author"),
                "tickers_mentioned": [],
                "run_id": run_id,
            }
        )
    stats["kept"] = len(records)
    stats["newest_published_at"] = _iso(max(dates)) if dates else None
    stats["oldest_published_at"] = _iso(min(dates)) if dates else None
    return records, stats


# --------------------------------------------------------------------------
# News sitemaps (catch items that fall out of short RSS feeds)
# --------------------------------------------------------------------------


_SM_URL = re.compile(r"<url>(.*?)</url>", re.S)
_SM_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_SM_TITLE = re.compile(r"<news:title>\s*(.*?)\s*</news:title>", re.S)


def parse_news_sitemap(xml: bytes) -> list[tuple[str, str | None]]:
    """(url, title) for each ``<url>`` that carries ``<news:news>``, in file order.
    Section pages in the same sitemap (no news block) are skipped."""
    text = xml.decode("utf-8", "replace")
    out: list[tuple[str, str | None]] = []
    for block in _SM_URL.findall(text):
        if "<news:news>" not in block:
            continue
        loc = _SM_LOC.search(block)
        if not loc:
            continue
        title = _SM_TITLE.search(block)
        out.append(
            (
                html.unescape(loc.group(1)),
                html.unescape(title.group(1)) if title else None,
            )
        )
    return out


def process_sitemap(
    sm: Sitemap,
    *,
    seen: set[str],
    fetched_at: datetime,
    run_id: str,
    fetch_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[Path], list[str]]:
    """Fetch one news sitemap and every listed article not yet in ``seen``.

    Returns (new records, stats, files written, warnings). Adds new ids to ``seen``.
    """
    stats: dict[str, Any] = {"url": sm.url, "role": "sitemap", "ok": False}
    files: list[Path] = []
    warnings: list[str] = []
    try:
        resp = http_get(sm.url, min_interval=sm.min_interval)
    except (HttpError, RobotsDisallowedError) as exc:
        stats["error"] = str(exc)
        return [], stats, files, [f"{sm.name}: fetch failed: {exc}"]
    raw_dir = LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}"
    if sm.store_full_text:
        files.append(
            _write_new(
                raw_dir / f"{run_id}_{sm.name}.xml.gz",
                gzip.compress(resp.content, mtime=0),
            )
        )
    entries = parse_news_sitemap(resp.content)
    todo: dict[str, tuple[str, str, str | None]] = {}
    for loc, title in entries:
        canon = canonical_url(loc, sm.canonical_id_re)
        aid = article_id(canon)
        if aid not in seen:
            todo.setdefault(aid, (loc, canon, title))
    stats.update(
        ok=True, items=len(entries), new_candidates=len(todo), fetched=0, failed=0
    )
    if len(todo) > sm.max_fetch:
        stats["capped"] = len(todo) - sm.max_fetch
        warnings.append(
            f"{sm.name}: {stats['capped']} new articles left for the next run "
            f"(max_fetch_per_run={sm.max_fetch})"
        )
    records: list[dict[str, Any]] = []
    for aid, (loc, canon, sm_title) in list(todo.items())[: sm.max_fetch]:
        try:
            page = http_get(canon, min_interval=sm.min_interval)
        except (HttpError, RobotsDisallowedError) as exc:
            stats["failed"] += 1
            warnings.append(f"{sm.name}: article {canon}: {exc}")
            continue
        if sm.store_full_text:
            files.append(
                _write_new(
                    raw_dir / f"{run_id}_{sm.name}_{aid}.html.gz",
                    gzip.compress(page.content, mtime=0),
                )
            )
        f = parse_article(page.content, page.url)
        full_text = f["full_text"] if sm.store_full_text else None
        records.append(
            {
                "article_id": aid,
                "url": loc,
                "canonical_url": canon,
                "source": sm.source,
                "feed_name": sm.name,
                "feed_url": sm.url,
                "feed_role": "sitemap",
                "guid": None,
                "title": f["title"] or sm_title,
                "published_at": f["published_at"],
                "published_at_raw": f["published_at_raw"],
                "published_tz": f["published_tz"],
                "fetched_at": fetched_at,
                "summary": f["summary"] if sm.store_full_text else None,
                "full_text": full_text,
                "full_text_source": "article_page" if full_text else None,
                "language": f["language"],
                "categories": f["tags"],
                "author": ", ".join(f["authors"]) or None,
                "tickers_mentioned": [],
                "run_id": run_id,
            }
        )
        seen.add(aid)
        stats["fetched"] += 1
    return records, stats, files, warnings


# --------------------------------------------------------------------------
# Index (dedupe state)
# --------------------------------------------------------------------------


def _empty_index() -> pa.Table:
    return INDEX_SCHEMA.empty_table()


def _hf_api() -> Any:
    from huggingface_hub import HfApi

    return HfApi(token=get_secret("HF_TOKEN"))


def _rebuild_index_from_hf() -> pa.Table:
    """Fallback if the index file is missing but partitions exist on HF."""
    api = _hf_api()
    try:
        entries = api.list_repo_tree(
            HF_REPO,
            path_in_repo=f"{HF_PREFIX}/articles",
            repo_type=HF_REPO_TYPE,
            recursive=True,
        )
        paths = sorted(e.path for e in entries if e.path.endswith(".parquet"))
    except (
        Exception
    ) as exc:  # noqa: BLE001 - HF raises several types for "missing folder"
        if "404" in str(exc) or "not found" in str(exc).lower():
            return _empty_index()
        raise
    tables = []
    for p in paths:
        local = hf_download(p)
        if local is None:
            continue
        t = pq.read_table(
            local,
            columns=[
                "article_id",
                "canonical_url",
                "feed_name",
                "published_at",
                "fetched_at",
            ],
        )
        rel = p[len(HF_PREFIX) + 1 :]
        tables.append(
            pa.table(
                {
                    "article_id": t["article_id"],
                    "canonical_url": t["canonical_url"],
                    "feed_name": t["feed_name"],
                    "published_at": t["published_at"],
                    "first_fetched_at": t["fetched_at"],
                    "partition": pa.array([rel] * t.num_rows, pa.string()),
                },
                schema=INDEX_SCHEMA,
            )
        )
    if paths:
        log.warning("index missing on HF: rebuilt from %d partitions", len(paths))
    return pa.concat_tables(tables) if tables else _empty_index()


def load_index(remote: bool) -> pa.Table:
    if remote:
        local = hf_download(f"{HF_PREFIX}/{INDEX_REL}")
        if local is None:
            return _rebuild_index_from_hf()
        return pq.read_table(local, schema=INDEX_SCHEMA)
    path = LOCAL_ROOT / INDEX_REL
    return (
        pq.read_table(path, schema=INDEX_SCHEMA) if path.is_file() else _empty_index()
    )


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def _write_new(path: Path, data: bytes) -> Path:
    """Write a file that must not already exist (raw data is immutable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        fh.write(data)
    return path


def _upload_run(files: Iterable[Path], run_id: str, n_new: int) -> str:
    """Upload this run's files to news/rss/ in a single HF commit."""
    with tempfile.TemporaryDirectory(prefix="news_rss_upload_") as tmp:
        for f in files:
            rel = f.relative_to(LOCAL_ROOT)
            dest = Path(tmp) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        return upload_to_hf(
            tmp,
            f"{HF_PREFIX}/",
            stream=STREAM,
            commit_message=f"news/rss: run {run_id} (+{n_new} articles)",
        )


def run(config: Config, *, upload: bool, only: set[str] | None = None) -> int:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = fetched_at.strftime("%Y%m%dT%H%M%SZ")
    fetch_date = fetched_at.date().isoformat()
    feeds = [f for f in config.feeds if only is None or f.name in only]
    sitemaps = [sm for sm in config.sitemaps if only is None or sm.name in only]
    if only and len(feeds) + len(sitemaps) != len(only):
        known = {f.name for f in feeds} | {sm.name for sm in sitemaps}
        raise SystemExit(f"unknown feed(s): {sorted(only - known)}")

    run_files: list[Path] = []
    feed_stats: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    warnings: list[str] = []

    for feed in feeds:
        stats: dict[str, Any] = {"url": feed.url, "role": feed.role, "ok": False}
        try:
            resp = http_get(feed.url, min_interval=config.min_interval)
        except (HttpError, RobotsDisallowedError) as exc:
            stats["error"] = str(exc)
            warnings.append(f"{feed.name}: fetch failed: {exc}")
            feed_stats[feed.name] = stats
            log.error("%s: %s", feed.name, exc)
            continue
        stats["raw_file"] = None
        if feed.store_full_text:  # raw feeds contain article text: only if terms allow
            raw_name = f"{run_id}_{feed.name}.xml.gz"
            raw_path = LOCAL_ROOT / "raw" / f"fetch_date={fetch_date}" / raw_name
            run_files.append(_write_new(raw_path, gzip.compress(resp.content, mtime=0)))
            stats["raw_file"] = raw_path.relative_to(LOCAL_ROOT).as_posix()
        recs, parsed_stats = parse_feed(resp.content, feed, fetched_at, run_id)
        stats.update(
            parsed_stats, ok=True, http_status=resp.status, bytes=len(resp.content)
        )
        newest = parsed_stats["newest_published_at"]
        if parsed_stats["items"] == 0:
            warnings.append(f"{feed.name}: feed returned no items")
        elif newest is None or datetime.fromisoformat(newest.replace("Z", "+00:00")) < (
            fetched_at - timedelta(days=config.stale_after_days)
        ):
            warnings.append(
                f"{feed.name}: newest item ({newest}) older than "
                f"{config.stale_after_days} days"
            )
        if parsed_stats["parse_warning"]:
            warnings.append(
                f"{feed.name}: feed parse warning: {parsed_stats['parse_warning']}"
            )
        feed_stats[feed.name] = stats
        records.extend(recs)
        log.info(
            "%s: %d items, kept %d, filtered %d, full text %d, newest %s",
            feed.name,
            stats["items"],
            stats["kept"],
            stats["filtered_out"],
            stats["with_full_text"],
            newest,
        )

    # Dedupe against the archive and within this run (first sighting wins).
    index_ok = True
    try:
        index = load_index(remote=upload)
    except Exception as exc:  # noqa: BLE001 - keep raw snapshots even if HF read fails
        index_ok = False
        index = _empty_index()
        warnings.append(
            f"could not load dedupe index, articles not written this run: {exc}"
        )
        log.error("index load failed: %s", exc)
    seen = set(index.column("article_id").to_pylist())
    new_records: list[dict[str, Any]] = []
    dupes = 0
    for rec in records:
        if rec["article_id"] in seen:
            dupes += 1
            continue
        seen.add(rec["article_id"])
        new_records.append(rec)
        feed_stats[rec["feed_name"]]["new"] = (
            feed_stats[rec["feed_name"]].get("new", 0) + 1
        )

    # Sitemaps last, so items already caught by RSS are not fetched again.
    sitemap_stats: dict[str, dict[str, Any]] = {}
    if index_ok:
        for sm in sitemaps:
            recs, st, files, warns = process_sitemap(
                sm,
                seen=seen,
                fetched_at=fetched_at,
                run_id=run_id,
                fetch_date=fetch_date,
            )
            new_records.extend(recs)
            run_files.extend(files)
            warnings.extend(warns)
            sitemap_stats[sm.name] = st
            log.info(
                "%s: %s items, %s new, %s fetched, %s failed",
                sm.name,
                st.get("items"),
                st.get("new_candidates"),
                st.get("fetched"),
                st.get("failed"),
            )

    if index_ok and new_records:
        part_rel = f"articles/fetch_date={fetch_date}/part-{run_id}.parquet"
        table = pa.Table.from_pylist(new_records, schema=ARTICLE_SCHEMA)
        part_path = LOCAL_ROOT / part_rel
        part_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, part_path, compression="zstd")
        run_files.append(part_path)
        added = pa.table(
            {
                "article_id": table["article_id"],
                "canonical_url": table["canonical_url"],
                "feed_name": table["feed_name"],
                "published_at": table["published_at"],
                "first_fetched_at": table["fetched_at"],
                "partition": pa.array([part_rel] * table.num_rows, pa.string()),
            },
            schema=INDEX_SCHEMA,
        )
        index = pa.concat_tables([index, added])
        index_path = LOCAL_ROOT / INDEX_REL
        index_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(index, index_path, compression="zstd")
        run_files.append(index_path)

    ok_feeds = [n for n, s in feed_stats.items() if s["ok"]]
    n_new = len(new_records) if index_ok else 0
    summary = {
        "run_id": run_id,
        "fetched_at": _iso(fetched_at),
        "git": git_info(),
        "config_version": config.version,
        "config_sha256": config.sha256,
        "feeds_ok": len(ok_feeds),
        "feeds_failed": len(feeds) - len(ok_feeds),
        "items_kept": len(records),
        "new_articles": n_new,
        "duplicates": dupes,
        "articles_written": index_ok,
        "warnings": warnings,
        "feeds": feed_stats,
        "sitemaps": sitemap_stats,
    }
    runlog = LOCAL_ROOT / "runs" / f"fetch_date={fetch_date}" / f"run-{run_id}.json"
    run_files.append(
        _write_new(runlog, (json.dumps(summary, indent=2) + "\n").encode())
    )
    log.info(
        "run %s: %d/%d feeds ok, %d items, %d new, %d duplicates",
        run_id,
        len(ok_feeds),
        len(feeds),
        len(records),
        summary["new_articles"],
        dupes,
    )

    revision = None
    exit_code = 0 if ok_feeds and index_ok else 1
    if upload:
        try:
            revision = _upload_run(run_files, run_id, n_new)
        except Exception as exc:  # noqa: BLE001 - report and fail the run
            log.error("upload failed: %s", exc)
            return 1

    published = [d for d in index.column("published_at").to_pylist() if d is not None]
    manifest = write_manifest(
        stream=STREAM,
        dataset=DATASET,
        source="RSS feeds: "
        + "; ".join(f"{f.source} <{f.url}>" for f in config.feeds)
        + "; news sitemaps: "
        + "; ".join(f"{sm.source} <{sm.url}>" for sm in config.sitemaps),
        fetch_mode="append_only_snapshot",
        row_count=index.num_rows,
        file_count=len(set(index.column("partition").to_pylist())),
        date_range=(min(published), max(published)) if published else None,
        hf_path=f"{HF_PREFIX}/" if upload else None,
        hf_revision=revision,
        notes=NOTES,
        extra={"last_run": {k: v for k, v in summary.items() if k != "warnings"}},
        files=run_files,
        warnings=warnings,
    )
    if upload:
        try:
            upload_to_hf(
                manifest,
                f"{HF_PREFIX}/{MANIFEST_REL}",
                stream=STREAM,
                commit_message=f"news/rss: manifest for run {run_id}",
            )
        except Exception as exc:  # noqa: BLE001 - data is already uploaded
            log.error(
                "manifest upload failed (data commit %s is fine): %s", revision, exc
            )
            exit_code = 1
    return exit_code


def pull_manifest() -> int:
    """Copy news/rss/manifest.json from HF into backend/data_manifests/ (to commit)."""
    local = hf_download(f"{HF_PREFIX}/{MANIFEST_REL}")
    if local is None:
        log.error("no manifest on HF yet")
        return 1
    dest = MANIFEST_DIR / f"{STREAM}_{DATASET}.json"
    shutil.copyfile(local, dest)
    log.info("wrote %s", dest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-upload", action="store_true", help="stage locally only")
    parser.add_argument("--feeds", help="comma-separated feed names (default: all)")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--pull-manifest", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.pull_manifest:
        return pull_manifest()
    only = (
        {s.strip() for s in args.feeds.split(",") if s.strip()} if args.feeds else None
    )
    return run(load_config(args.config), upload=not args.no_upload, only=only)


if __name__ == "__main__":
    sys.exit(main())
