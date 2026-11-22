"""Tests for backend/jobs/common.py. Offline: local HTTP server, fake HF client."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from jobs import common  # noqa: E402  (needs the sys.path line above)

# --------------------------------------------------------------------------
# HF path guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["news/rss/2026-11-08/part.parquet", "news/rss/", "news/x"],
)
def test_check_hf_path_accepts_own_stream(path: str) -> None:
    assert common.check_hf_path(path, "news") == path


@pytest.mark.parametrize(
    "path",
    [
        "company/prices.parquet",  # another stream
        "macro/x",
        "news",  # stream root itself
        "news/",
        "",
        "/news/x",  # absolute
        "news/../company/x",  # traversal
        "news/./x",
        "news//x",
        "news\\x",
        "newsletter/x",  # prefix trick
        "x/news/y",
    ],
)
def test_check_hf_path_rejects_outside_stream(path: str) -> None:
    with pytest.raises(ValueError):
        common.check_hf_path(path, "news")


def test_check_hf_path_rejects_unknown_stream() -> None:
    with pytest.raises(ValueError):
        common.check_hf_path("other/x", "other")


class _FakeHfApi:
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, token: str) -> None:
        self.token = token

    def upload_file(self, **kwargs: Any) -> Any:
        _FakeHfApi.calls.append(("upload_file", kwargs))
        return types.SimpleNamespace(oid="abc123")

    def upload_folder(self, **kwargs: Any) -> Any:
        _FakeHfApi.calls.append(("upload_folder", kwargs))
        return types.SimpleNamespace(oid="def456")


@pytest.fixture
def fake_hf(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHfApi]:
    _FakeHfApi.calls = []
    module = types.ModuleType("huggingface_hub")
    module.HfApi = _FakeHfApi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    monkeypatch.setenv("HF_TOKEN", "hf_test_dummy")
    return _FakeHfApi


def test_upload_refuses_before_touching_token_or_network(
    tmp_path: Path, fake_hf: type[_FakeHfApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN")
    f = tmp_path / "a.parquet"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        common.upload_to_hf(f, "company/a.parquet", stream="news")
    assert fake_hf.calls == []


def test_upload_file_and_folder(tmp_path: Path, fake_hf: type[_FakeHfApi]) -> None:
    f = tmp_path / "a.parquet"
    f.write_bytes(b"x")
    assert common.upload_to_hf(f, "news/rss/a.parquet", stream="news") == "abc123"
    folder = tmp_path / "batch"
    folder.mkdir()
    (folder / "b.parquet").write_bytes(b"y")
    assert common.upload_to_hf(folder, "news/rss/", stream="news") == "def456"

    (kind1, kw1), (kind2, kw2) = fake_hf.calls
    assert kind1 == "upload_file" and kw1["path_in_repo"] == "news/rss/a.parquet"
    assert kind2 == "upload_folder" and kw2["path_in_repo"] == "news/rss"
    for kw in (kw1, kw2):
        assert kw["repo_id"] == common.HF_REPO
        assert kw["repo_type"] == "dataset"
        assert "delete_patterns" not in kw


def test_missing_token_error_names_variable(
    tmp_path: Path, fake_hf: type[_FakeHfApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.setattr(common, "ENV_FILE", tmp_path / "no.env")
    f = tmp_path / "a.parquet"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="HF_TOKEN"):
        common.upload_to_hf(f, "news/a.parquet", stream="news")


def test_load_env_does_not_override_real_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text('# c\nexport A_KEY="from_file"\nB_KEY=file_b\nEMPTY_KEY=\n')
    monkeypatch.setenv("A_KEY", "from_env")
    monkeypatch.delenv("B_KEY", raising=False)
    common.load_env(env)
    assert common.os.environ["A_KEY"] == "from_env"
    assert common.os.environ["B_KEY"] == "file_b"
    monkeypatch.delenv("B_KEY")
    monkeypatch.delenv("EMPTY_KEY", raising=False)


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def test_write_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path / "manifests")
    out_file = tmp_path / "part.parquet"
    out_file.write_bytes(b"hello")
    path = common.write_manifest(
        stream="news",
        dataset="rss_articles",
        source="test feeds",
        fetch_mode="append_only_snapshot",
        row_count=3,
        date_range=("2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z"),
        hf_path="news/rss/",
        hf_revision="abc123",
        notes="n",
        extra={"feeds": 2},
        files=[out_file],
        warnings=["feed X stale"],
    )
    assert path.name == "news_rss_articles.json"
    m = json.loads(path.read_text())
    assert m["stream"] == "news" and m["dataset"] == "rss_articles"
    assert m["fetch_mode"] == "append_only_snapshot"
    assert m["row_count"] == 3 and m["file_count"] == 1
    assert m["files"][0]["sha256"] == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert m["date_range"] == {
        "min": "2026-11-01T00:00:00Z",
        "max": "2026-11-08T00:00:00Z",
    }
    assert m["hf_repo"] == common.HF_REPO and m["hf_revision"] == "abc123"
    assert m["script"] == "backend/tests/jobs/test_common.py"
    assert m["fetched_at"].endswith("Z")
    assert "feed X stale" in m["warnings"]
    assert m["extra"] == {"feeds": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stream": "other"},
        {"dataset": "Bad-Name"},
        {"fetch_mode": "sometimes"},
        {"hf_path": "company/x"},
        {"row_count": -1},
    ],
)
def test_write_manifest_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    monkeypatch.setattr(common, "MANIFEST_DIR", tmp_path)
    base: dict[str, Any] = {
        "stream": "news",
        "dataset": "d",
        "source": "s",
        "fetch_mode": "reproducible",
    }
    with pytest.raises(ValueError):
        common.write_manifest(**{**base, **kwargs})
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# HTTP (local server)
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    hits: ClassVar[dict[str, list[float]]] = {}
    user_agents: ClassVar[list[str]] = []
    bodies: ClassVar[list[str]] = []
    content_types: ClassVar[list[str]] = []

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.bodies.append(self.rfile.read(length).decode())
        _Handler.content_types.append(self.headers.get("Content-Type", ""))
        self._send(200, b"<table></table>", "text/html")

    def do_GET(self) -> None:
        _Handler.hits.setdefault(self.path, []).append(time.monotonic())
        _Handler.user_agents.append(self.headers.get("User-Agent", ""))
        n = len(_Handler.hits[self.path])
        if self.path == "/robots.txt":
            self._send(200, b"User-agent: *\nDisallow: /private\n")
        elif self.path == "/flaky" and n < 3:
            self._send(503, b"busy")
        elif self.path == "/missing":
            self._send(404, b"no")
        else:
            self._send(200, b'{"ok": true}', "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.hits = {}
    _Handler.user_agents = []
    _Handler.bodies = []
    _Handler.content_types = []
    common._robots_cache.clear()
    common._next_slot.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_http_get_ok_sends_project_user_agent(server: str) -> None:
    resp = common.http_get(
        server + "/data",
        params={"q": "PSX"},
        headers={"User-Agent": "spoof"},
        min_interval=0,
    )
    assert resp.status == 200 and resp.json() == {"ok": True}
    assert resp.encoding == "utf-8"
    assert set(_Handler.user_agents) == {common.USER_AGENT}
    assert "/data?q=PSX" in _Handler.hits


def test_http_get_retries_then_succeeds(server: str) -> None:
    resp = common.http_get(server + "/flaky", backoff=0.01, min_interval=0)
    assert resp.status == 200
    assert len(_Handler.hits["/flaky"]) == 3


def test_http_get_gives_up_after_retries(server: str) -> None:
    with pytest.raises(common.HttpError) as info:
        common.http_get(server + "/flaky", retries=1, backoff=0.01, min_interval=0)
    assert info.value.status == 503
    assert len(_Handler.hits["/flaky"]) == 2


def test_http_get_404_not_retried(server: str) -> None:
    with pytest.raises(common.HttpError) as info:
        common.http_get(server + "/missing", backoff=0.01, min_interval=0)
    assert info.value.status == 404
    assert len(_Handler.hits["/missing"]) == 1


def test_http_get_respects_robots(server: str) -> None:
    with pytest.raises(common.RobotsDisallowedError):
        common.http_get(server + "/private/page", min_interval=0)
    assert "/private/page" not in _Handler.hits
    assert len(_Handler.hits["/robots.txt"]) == 1  # cached afterwards
    common.http_get(server + "/a", min_interval=0)
    assert len(_Handler.hits["/robots.txt"]) == 1


def test_http_get_rate_limits_per_host(server: str) -> None:
    for _ in range(3):
        common.http_get(server + "/r", min_interval=0.2, respect_robots=False)
    t = _Handler.hits["/r"]
    assert t[1] - t[0] >= 0.18 and t[2] - t[1] >= 0.18


def test_http_get_rejects_non_http() -> None:
    with pytest.raises(ValueError):
        common.http_get("file:///etc/passwd")


def test_same_host_https_to_http_redirect_stays_https() -> None:
    handler = common._KeepHttpsRedirectHandler()
    req = common.urllib.request.Request("https://www.example.com/terms")
    new = handler.redirect_request(
        req, None, 301, "Moved", {}, "http://www.example.com/terms/"
    )
    assert new is not None and new.full_url == "https://www.example.com/terms/"


def test_cross_host_https_to_http_redirect_refused() -> None:
    handler = common._KeepHttpsRedirectHandler()
    req = common.urllib.request.Request("https://www.example.com/a")
    with pytest.raises(common.urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/a")


def test_http_post_sends_form_body(server: str) -> None:
    resp = common.http_post(
        server + "/announcements", data={"type": "C", "symbol": "OGDC"}, min_interval=0
    )
    assert resp.status == 200 and resp.text() == "<table></table>"
    assert _Handler.bodies == ["type=C&symbol=OGDC"]
    assert _Handler.content_types[0].startswith("application/x-www-form-urlencoded")


def test_http_post_respects_robots(server: str) -> None:
    with pytest.raises(common.RobotsDisallowedError):
        common.http_post(server + "/private/x", data={"a": 1}, min_interval=0)
