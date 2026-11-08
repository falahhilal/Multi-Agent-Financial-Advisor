"""Shared helpers for the data-collection jobs in ``backend/jobs/``.

Used by all three streams (news, company, macro). Three public helpers:

* :func:`http_get` makes a polite HTTP GET. It sends the project User-Agent,
  obeys robots.txt (including Crawl-delay), rate-limits per host, and applies a
  timeout plus retries with exponential backoff on 429/5xx and network errors.
* :func:`upload_to_hf` uploads a file or folder to the shared private
  Hugging Face dataset repo ``HF_REPO``. It refuses any ``path_in_repo``
  outside the caller's own stream folder, and never deletes anything.
* :func:`write_manifest` writes ``backend/data_manifests/<stream>_<dataset>.json``,
  which is committed to git. The data itself never is.

Usage (run from ``backend/`` as ``python -m jobs.<script>``)::

    from jobs.common import DATA_DIR, http_get, upload_to_hf, write_manifest

    resp = http_get("https://www.dawn.com/feeds/business/")
    out = DATA_DIR / "news" / "example" / "2026-11-08" / "dawn_business.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)

    revision = upload_to_hf(
        out, "news/example/2026-11-08/dawn_business.xml", stream="news"
    )
    write_manifest(
        stream="news",
        dataset="example",
        source="Dawn business RSS feed",
        fetch_mode="append_only_snapshot",
        files=[out],
        hf_path="news/example/",
        hf_revision=revision,
        notes="Example only.",
    )

Only the standard library is needed, except ``huggingface_hub``, which
:func:`upload_to_hf` imports lazily. Secrets such as ``HF_TOKEN`` come from the
environment, falling back to the repo-root ``.env`` file (see ``.env.example``).
In GitHub Actions they come from repository secrets. Secret values are never
logged, printed or written to a manifest.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import posixpath
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
MANIFEST_DIR = BACKEND_DIR / "data_manifests"
#: Local staging area for downloaded data. Gitignored (``/data/``), never committed.
DATA_DIR = REPO_ROOT / "data"
ENV_FILE = REPO_ROOT / ".env"

#: Shared private HF dataset repo. Each stream writes only under ``<stream>/``.
HF_REPO = "fyp-psx-advisor/fyp-data"
HF_REPO_TYPE = "dataset"
STREAMS = frozenset({"news", "company", "macro"})

#: ``reproducible``: re-running the loader re-downloads the same data.
#: ``append_only_snapshot``: the source cannot be re-fetched later (e.g. RSS),
#: so the uploaded copy is the archive of record.
FETCH_MODES = frozenset({"reproducible", "append_only_snapshot"})
MANIFEST_SCHEMA_VERSION = 1

#: Project User-Agent. No personal contact details.
USER_AGENT = (
    "fyp-psx-advisor-databot/0.1 "
    "(academic research project, FAST-NU Karachi; low-rate, robots.txt-aware)"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_MIN_INTERVAL = 1.0  # seconds between requests to the same host
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_SLEEP = 60.0  # cap on any single backoff / Retry-After wait

_DATASET_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


# --------------------------------------------------------------------------
# Secrets and small utilities
# --------------------------------------------------------------------------


def load_env(path: Path | None = None) -> None:
    """Load ``KEY=VALUE`` lines from ``.env`` into ``os.environ``.

    Variables that are already set to a non-empty value are left alone, so a
    real environment (e.g. GitHub Actions secrets) always wins over the file.
    A missing file is not an error.
    """
    env_path = path or ENV_FILE
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not os.environ.get(key):
            os.environ[key] = value


def get_secret(name: str) -> str:
    """Return the secret ``name`` from the environment or ``.env``.

    Raises ``RuntimeError`` (naming the variable, never showing a value) if unset.
    """
    load_env()
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to the repo-root .env file (see .env.example) "
            f"or export it; in GitHub Actions add a repository secret named {name}."
        )
    return value


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with a ``Z`` suffix, second precision."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Hex SHA-256 of a file, streamed so large files are fine."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def git_info() -> dict[str, Any]:
    """Commit SHA of the code producing the data, and whether the tree was dirty.

    Changes under ``backend/data_manifests/`` are ignored for the dirty check,
    since jobs write manifests there themselves. ``dirty`` is ``None`` when git
    is unavailable.
    """
    commit: str | None
    try:
        commit = _git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        commit = os.environ.get("GITHUB_SHA")
    try:
        status = _git(
            "status", "--porcelain", "--", ".", ":(exclude)backend/data_manifests"
        )
        dirty: bool | None = bool(status)
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return {"commit": commit, "dirty": dirty}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class HttpError(RuntimeError):
    """A request failed. ``status`` is ``None`` for network errors and timeouts."""

    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(f"{message} for {url}")
        self.url = url
        self.status = status


class RobotsDisallowedError(RuntimeError):
    """robots.txt forbids our User-Agent from fetching this URL."""


@dataclass(frozen=True)
class HttpResponse:
    url: str  # final URL, after redirects
    status: int
    headers: Mapping[str, str]  # keys lower-cased
    content: bytes

    @property
    def encoding(self) -> str | None:
        msg = Message()
        msg["content-type"] = self.headers.get("content-type", "")
        return msg.get_content_charset()

    def text(self, encoding: str | None = None) -> str:
        return self.content.decode(encoding or self.encoding or "utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content)


_host_lock = threading.Lock()
_next_slot: dict[str, float] = {}
_robots_lock = threading.Lock()
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def _wait_for_host(host: str, interval: float) -> None:
    """Block until ``host`` may be hit again. Thread-safe; slots are reserved."""
    with _host_lock:
        now = time.monotonic()
        start = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = start + interval
    if start > now:
        time.sleep(start - now)


class _KeepHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect from https to plain http.

    Some sites (seen on brecorder.com and dawn.com) redirect
    ``https://host/path`` to ``http://host/path/``. For the same host the
    scheme is kept as https. A downgrade to a different host is refused
    (surfaces as ``HttpError`` with the 3xx status).
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if old.scheme == "https" and new.scheme == "http":
            if (new.hostname or "").lower() != (old.hostname or "").lower():
                raise urllib.error.HTTPError(
                    newurl,
                    code,
                    f"refusing https->http redirect to {newurl}",
                    headers,
                    fp,
                )
            port = f":{new.port}" if new.port and new.port != 80 else ""
            newurl = urllib.parse.urlunsplit(
                new._replace(scheme="https", netloc=f"{new.hostname}{port}")
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_KeepHttpsRedirectHandler)


def _fetch_once(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return HttpResponse(
                url=r.geturl(),
                status=r.status,
                headers={k.lower(): v for k, v in r.headers.items()},
                content=r.read(),
            )
    except urllib.error.HTTPError as exc:  # non-2xx: still a response
        try:
            body = exc.read()
        except (OSError, http.client.HTTPException):
            body = b""
        hdrs = exc.headers.items() if exc.headers is not None else []
        return HttpResponse(
            url=exc.geturl() or url,
            status=exc.code,
            headers={k.lower(): v for k, v in hdrs},
            content=body,
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _get_with_retries(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout: float,
    retries: int,
    backoff: float,
    interval: float,
) -> HttpResponse:
    host = urllib.parse.urlsplit(url).netloc.lower()
    attempt = 0
    while True:
        _wait_for_host(host, interval)
        retry_after: float | None = None
        try:
            resp = _fetch_once(url, headers, timeout)
        except (OSError, http.client.HTTPException) as exc:  # URLError is an OSError
            error = HttpError(url, None, f"{type(exc).__name__}: {exc}")
        else:
            if 200 <= resp.status < 300:
                return resp
            error = HttpError(url, resp.status, f"HTTP {resp.status}")
            if resp.status not in RETRY_STATUSES:
                raise error
            retry_after = _retry_after_seconds(resp.headers.get("retry-after"))
        if attempt >= retries:
            raise error
        if retry_after is not None:
            wait = retry_after
        else:
            wait = backoff * (2**attempt) * (1 + random.random() * 0.25)
        wait = min(wait, MAX_SLEEP)
        log.warning("%s; retry %d/%d in %.1fs", error, attempt + 1, retries, wait)
        time.sleep(wait)
        attempt += 1


def _robots_for(
    scheme: str, host: str, timeout: float
) -> urllib.robotparser.RobotFileParser:
    """Fetch and cache robots.txt for ``scheme://host``.

    Follows the usual conventions (RFC 9309): 401/403 means disallow
    everything, any other 4xx means no restrictions, and an unreachable
    robots.txt or a 5xx means we do not crawl (raises ``HttpError``).
    """
    base = f"{scheme}://{host}"
    with _robots_lock:
        cached = _robots_cache.get(base)
    if cached is not None:
        return cached
    rp = urllib.robotparser.RobotFileParser(base + "/robots.txt")
    try:
        resp = _get_with_retries(
            base + "/robots.txt",
            {"User-Agent": USER_AGENT},
            timeout=timeout,
            retries=2,
            backoff=2.0,
            interval=DEFAULT_MIN_INTERVAL,
        )
    except HttpError as exc:
        if exc.status in (401, 403):
            rp.disallow_all = True  # type: ignore[attr-defined]
        elif exc.status is not None and 400 <= exc.status < 500:
            rp.allow_all = True  # type: ignore[attr-defined]
        else:
            raise HttpError(
                exc.url, exc.status, f"robots.txt unavailable ({exc}); not crawling"
            ) from exc
    else:
        rp.parse(resp.text().splitlines())
    with _robots_lock:
        _robots_cache[base] = rp
    return rp


def _with_params(
    url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]]
) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(params, doseq=True)
    merged = f"{parts.query}&{query}" if parts.query else query
    return urllib.parse.urlunsplit(parts._replace(query=merged))


def http_get(
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff: float = 2.0,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    respect_robots: bool = True,
) -> HttpResponse:
    """Polite GET. Returns the 2xx response or raises.

    * Always sends the project ``USER_AGENT`` (a ``User-Agent`` in ``headers``
      is ignored).
    * ``respect_robots``: robots.txt is checked once per host per process;
      a disallowed URL raises ``RobotsDisallowedError``. A ``Crawl-delay``
      longer than ``min_interval`` is honoured.
    * ``min_interval``: minimum seconds between request starts to one host,
      shared across threads.
    * Retries up to ``retries`` times on 429/500/502/503/504, timeouts and
      connection errors, waiting ``backoff * 2**attempt`` seconds (plus jitter)
      or the server's ``Retry-After``, capped at ``MAX_SLEEP``. Other non-2xx
      statuses raise ``HttpError`` immediately.
    """
    if params:
        url = _with_params(url, params)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    interval = min_interval
    if respect_robots:
        rp = _robots_for(parts.scheme, parts.netloc.lower(), timeout)
        if not rp.can_fetch(USER_AGENT, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        delay = rp.crawl_delay(USER_AGENT)
        if delay:
            interval = max(interval, float(delay))
    req_headers = {"Accept": "*/*", **(headers or {})}
    req_headers = {k: v for k, v in req_headers.items() if k.lower() != "user-agent"}
    req_headers["User-Agent"] = USER_AGENT
    return _get_with_retries(
        url,
        req_headers,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        interval=interval,
    )


# --------------------------------------------------------------------------
# Hugging Face upload
# --------------------------------------------------------------------------


def check_hf_path(path_in_repo: str, stream: str) -> str:
    """Validate that ``path_in_repo`` lies strictly inside ``<stream>/``.

    Rejects other streams' folders, the repo root, absolute paths, ``..``,
    ``.`` and empty segments, and backslashes. Returns the path unchanged.
    """
    if stream not in STREAMS:
        raise ValueError(
            f"unknown stream {stream!r}; expected one of {sorted(STREAMS)}"
        )
    if not isinstance(path_in_repo, str) or not path_in_repo:
        raise ValueError("path_in_repo must be a non-empty string")
    if "\\" in path_in_repo or path_in_repo.startswith("/"):
        raise ValueError(
            f"path_in_repo must be a relative POSIX path: {path_in_repo!r}"
        )
    prefix = f"{stream}/"
    if not path_in_repo.startswith(prefix) or len(path_in_repo) == len(prefix):
        raise ValueError(
            f"refusing to write {path_in_repo!r}: stream {stream!r} may only write "
            f"inside {prefix!r} of {HF_REPO}"
        )
    segments = path_in_repo.rstrip("/").split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(
            f"path_in_repo has empty, '.' or '..' segments: {path_in_repo!r}"
        )
    if posixpath.normpath(path_in_repo) != path_in_repo.rstrip("/"):
        raise ValueError(f"path_in_repo is not normalised: {path_in_repo!r}")
    return path_in_repo


def upload_to_hf(
    local_path: str | Path,
    path_in_repo: str,
    stream: str,
    *,
    commit_message: str | None = None,
) -> str:
    """Upload a file or a folder to ``HF_REPO`` and return the HF commit SHA.

    * A file is uploaded to exactly ``path_in_repo``.
    * A folder's contents are uploaded under ``path_in_repo``. Existing remote
      files with other names are kept: nothing is ever deleted.

    ``path_in_repo`` must start with ``f"{stream}/"`` (see :func:`check_hf_path`).
    The check runs before the token is read or anything is sent.
    Needs ``HF_TOKEN`` and the ``huggingface_hub`` package.
    """
    check_hf_path(path_in_repo, stream)
    src = Path(local_path)
    if not src.exists():
        raise FileNotFoundError(src)
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "upload_to_hf needs huggingface_hub: pip install huggingface_hub"
        ) from exc

    api = HfApi(token=get_secret("HF_TOKEN"))
    message = commit_message or f"{stream}: upload {path_in_repo}"
    if src.is_dir():
        info = api.upload_folder(
            repo_id=HF_REPO,
            repo_type=HF_REPO_TYPE,
            folder_path=src,
            path_in_repo=path_in_repo.rstrip("/"),
            commit_message=message,
        )
    else:
        if path_in_repo.endswith("/"):
            raise ValueError("path_in_repo for a file must name the file, not a folder")
        info = api.upload_file(
            repo_id=HF_REPO,
            repo_type=HF_REPO_TYPE,
            path_or_fileobj=str(src),
            path_in_repo=path_in_repo,
            commit_message=message,
        )
    revision = str(info.oid)
    log.info("uploaded %s -> %s:%s @ %s", src.name, HF_REPO, path_in_repo, revision)
    return revision


def hf_download(path_in_repo: str, *, revision: str | None = None) -> Path | None:
    """Download one file from ``HF_REPO`` and return its local cached path.

    Returns ``None`` if the file does not exist at that revision (default: main).
    Read-only, so any stream's folder may be read. Needs ``HF_TOKEN``.
    """
    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import EntryNotFoundError
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "hf_download needs huggingface_hub: pip install huggingface_hub"
        ) from exc
    try:
        local = hf_hub_download(
            repo_id=HF_REPO,
            repo_type=HF_REPO_TYPE,
            filename=path_in_repo,
            revision=revision,
            token=get_secret("HF_TOKEN"),
        )
    except EntryNotFoundError:
        return None
    return Path(str(local))


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def _iso(value: date | datetime | str) -> str:
    if isinstance(value, (date, datetime)):  # datetime is a subclass of date
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError(
        f"date_range values must be date, datetime or str, not {type(value)}"
    )


def _normalise_date_range(
    date_range: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, str | None] | None:
    if date_range is None:
        return None
    if isinstance(date_range, Mapping):
        lo, hi = date_range.get("min"), date_range.get("max")
    else:
        if len(date_range) != 2:
            raise ValueError(
                "date_range must be (min, max) or {'min': ..., 'max': ...}"
            )
        lo, hi = date_range
    return {
        "min": None if lo is None else _iso(lo),
        "max": None if hi is None else _iso(hi),
    }


def _display_path(path: Path) -> str:
    """Path relative to data/ or the repo, so runner-specific prefixes never leak."""
    resolved = path.resolve()
    for base in (DATA_DIR, REPO_ROOT):
        try:
            return resolved.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return resolved.name


def _check_count(name: str, value: int | None) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")


def write_manifest(
    stream: str,
    dataset: str,
    source: str,
    fetch_mode: str,
    row_count: int | None = None,
    file_count: int | None = None,
    date_range: Mapping[str, Any] | Sequence[Any] | None = None,
    hf_path: str | None = None,
    hf_revision: str | None = None,
    notes: str | None = None,
    extra: Mapping[str, Any] | None = None,
    *,
    files: Iterable[str | Path] | None = None,
    warnings: Iterable[str] | None = None,
    script: str | None = None,
) -> Path:
    """Write ``backend/data_manifests/<stream>_<dataset>.json`` and return its path.

    The manifest describes the latest run and is overwritten each time.

    ``files`` are the output files this run produced. Each is recorded with
    its size and SHA-256, and ``file_count`` defaults to their number.
    ``date_range`` is ``(min, max)`` or ``{"min": .., "max": ..}`` of
    date/datetime/ISO strings. ``warnings`` are added to the manifest's
    warnings list, together with automatic ones (e.g. a dirty git tree).
    ``extra`` must be JSON-serialisable and is stored under ``"extra"``.
    ``script`` defaults to the calling module's path, relative to the repo.
    """
    if stream not in STREAMS:
        raise ValueError(
            f"unknown stream {stream!r}; expected one of {sorted(STREAMS)}"
        )
    if not _DATASET_RE.match(dataset):
        raise ValueError(f"dataset must be snake_case [a-z0-9_]: {dataset!r}")
    if fetch_mode not in FETCH_MODES:
        raise ValueError(
            f"fetch_mode must be one of {sorted(FETCH_MODES)}, got {fetch_mode!r}"
        )
    if not source or not source.strip():
        raise ValueError("source is required")
    _check_count("row_count", row_count)
    _check_count("file_count", file_count)
    if hf_path is not None:
        check_hf_path(hf_path, stream)
    elif hf_revision is not None:
        raise ValueError("hf_revision given without hf_path")

    file_entries = []
    for f in files or []:
        p = Path(f)
        if not p.is_file():
            raise FileNotFoundError(p)
        file_entries.append(
            {
                "path": _display_path(p),
                "bytes": p.stat().st_size,
                "sha256": sha256_file(p),
            }
        )
    if file_count is None and file_entries:
        file_count = len(file_entries)

    if script is None:
        caller = sys._getframe(1).f_code.co_filename
        script = _display_path(Path(caller))

    git = git_info()
    all_warnings = list(warnings or [])
    if git["commit"] is None:
        all_warnings.append("git commit unknown: results not traceable to code")
    if git["dirty"]:
        all_warnings.append(
            "uncommitted changes in working tree: git_commit may not reproduce this run"
        )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "id": f"{stream}_{dataset}",
        "stream": stream,
        "dataset": dataset,
        "source": source,
        "fetch_mode": fetch_mode,
        "fetched_at": utc_now_iso(),
        "script": script,
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "row_count": row_count,
        "file_count": file_count,
        "date_range": _normalise_date_range(date_range),
        "files": file_entries,
        "hf_repo": HF_REPO if hf_path else None,
        "hf_repo_type": HF_REPO_TYPE if hf_path else None,
        "hf_path": hf_path,
        "hf_revision": hf_revision,
        "notes": notes,
        "warnings": all_warnings,
        "extra": dict(extra) if extra else {},
    }
    text = json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    out = MANIFEST_DIR / f"{stream}_{dataset}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    for w in all_warnings:
        log.warning("%s: %s", manifest["id"], w)
    return out
