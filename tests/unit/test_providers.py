from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from repolens.repository.providers import (
    GitHubProvider,
    LocalDirectoryProvider,
    ProviderError,
    locate_with,
)
from repolens.repository.validation import ValidationError

RESPONSES: dict[str, tuple[int, dict[str, str], bytes]] = {
    "/repos/octo/ok": (
        200,
        {},
        json.dumps({"default_branch": "dev", "size": 12, "private": False}).encode(),
    ),
    "/repos/octo/moved": (301, {"Location": "https://api.github.com/repositories/1"}, b""),
    "/repos/octo/missing": (404, {}, b"{}"),
    "/repos/octo/limited": (403, {}, b"{}"),
    "/repos/octo/broken": (502, {}, b""),
    "/repos/octo/garbage": (200, {}, b"not json"),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, headers, body = RESPONSES.get(self.path, (500, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def api() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _github(api: str, name: str) -> tuple[GitHubProvider, object]:
    provider = GitHubProvider(api_base=api, timeout=5)
    return provider, provider.locate(f"https://github.com/octo/{name}")


def test_github_preflight_reports_metadata(api: str) -> None:
    provider, locator = _github(api, "ok")
    meta = provider.preflight(locator)  # type: ignore[arg-type]
    assert (meta.default_branch, meta.size_bytes, meta.is_private) == ("dev", 12 * 1024, False)


@pytest.mark.parametrize(
    ("name", "retryable", "message"),
    [
        ("moved", False, "moved"),
        ("missing", False, "not found"),
        ("limited", True, "rate limit"),
        ("broken", True, "502"),
        ("garbage", True, "invalid JSON"),
    ],
)
def test_github_preflight_errors(api: str, name: str, retryable: bool, message: str) -> None:
    provider, locator = _github(api, name)
    with pytest.raises(ProviderError, match=message) as info:
        provider.preflight(locator)  # type: ignore[arg-type]
    assert info.value.retryable is retryable


def test_github_unreachable_is_retryable() -> None:
    provider = GitHubProvider(api_base="http://127.0.0.1:9", timeout=1)
    with pytest.raises(ProviderError) as info:
        provider.preflight(provider.locate("https://github.com/a/b"))
    assert info.value.retryable


class TestLocalProvider:
    def test_accepts_paths_under_allowed_roots(self, tmp_path: Path) -> None:
        (tmp_path / "repo").mkdir()
        provider = LocalDirectoryProvider([tmp_path])
        locator = provider.locate((tmp_path / "repo").as_uri())
        assert provider.fetch_url(locator) == (tmp_path / "repo").resolve().as_uri()

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc",
            "file://remote-host/srv/repo",
            "file:relative/path",
            "https://github.com/a/b",
        ],
    )
    def test_rejects_paths_outside_roots(self, tmp_path: Path, url: str) -> None:
        with pytest.raises(ValidationError):
            LocalDirectoryProvider([tmp_path]).locate(url)

    def test_rejects_traversal_out_of_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValidationError):
            LocalDirectoryProvider([root]).locate(f"file://{root}/../")

    def test_rejects_symlink_escaping_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "link").symlink_to("/etc")
        with pytest.raises(ValidationError):
            LocalDirectoryProvider([root]).locate((root / "link").as_uri())


def test_locate_with_picks_first_matching_provider(tmp_path: Path) -> None:
    github = GitHubProvider()
    local = LocalDirectoryProvider([tmp_path])
    provider, _ = locate_with([github, local], "https://github.com/a/b")
    assert provider is github
    provider, _ = locate_with([github, local], tmp_path.as_uri())
    assert provider is local
    with pytest.raises(ValidationError):
        locate_with([github], tmp_path.as_uri())
