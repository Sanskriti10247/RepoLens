"""Repository providers: where a repository lives and how to vet it before fetching."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from repolens.repository.validation import (
    RepositoryLocator,
    ValidationError,
    parse_repository_url,
)


class ProviderError(RuntimeError):
    """The provider rejected or could not answer a request."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RemoteMetadata:
    default_branch: str | None
    size_bytes: int | None  # provider-reported size, used to reject oversized repos early
    is_private: bool


class RepositoryProvider(Protocol):
    name: str
    protocols: tuple[str, ...]

    def locate(self, url: str) -> RepositoryLocator: ...

    def preflight(self, locator: RepositoryLocator) -> RemoteMetadata: ...

    def fetch_url(self, locator: RepositoryLocator) -> str: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # surfaces 3xx as HTTPError instead of following it


class GitHubProvider:
    """Public GitHub repositories, vetted with the unauthenticated REST API."""

    name = "github"
    protocols = ("https",)

    def __init__(self, api_base: str = "https://api.github.com", timeout: float = 10.0) -> None:
        self.api_base = api_base
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirects)

    def locate(self, url: str) -> RepositoryLocator:
        return parse_repository_url(url)

    def fetch_url(self, locator: RepositoryLocator) -> str:
        return locator.clone_url

    def preflight(self, locator: RepositoryLocator) -> RemoteMetadata:
        request = urllib.request.Request(  # noqa: S310 - fixed https base, validated path
            f"{self.api_base}/repos/{locator.owner}/{locator.name}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "repolens"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = json.loads(response.read(1_000_000))
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 307, 308):
                raise ProviderError("Repository has moved; register its new URL") from exc
            if exc.code == 404:
                raise ProviderError("Repository not found or not public") from exc
            if exc.code in (403, 429):
                raise ProviderError("GitHub API rate limit reached", retryable=True) from exc
            raise ProviderError(f"GitHub API error {exc.code}", retryable=exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError("GitHub API unreachable", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("GitHub API returned invalid JSON", retryable=True) from exc
        size_kb = body.get("size")
        return RemoteMetadata(
            default_branch=body.get("default_branch"),
            size_bytes=int(size_kb) * 1024 if isinstance(size_kb, int) else None,
            is_private=bool(body.get("private", True)),
        )


class LocalDirectoryProvider:
    """``file://`` repositories on this machine. Disabled unless explicitly configured.

    Intended for tests and offline demonstrations; the path must lie under an allowed root.
    """

    name = "local"
    protocols = ("file",)

    def __init__(self, allowed_roots: list[Path]) -> None:
        self.allowed_roots = [root.resolve() for root in allowed_roots]

    def locate(self, url: str) -> RepositoryLocator:
        parts = urlsplit(url)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            raise ValidationError("Expected a file:// URL")
        if parts.query or parts.fragment:
            raise ValidationError("file:// URLs must not contain a query or fragment")
        path = Path(unquote(parts.path))
        if not path.is_absolute():
            raise ValidationError("file:// URL must be absolute")
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
            raise ValidationError("Local repository is outside the allowed directories")
        return RepositoryLocator(
            provider=self.name, host="localhost", owner="local", name=str(resolved)
        )

    def fetch_url(self, locator: RepositoryLocator) -> str:
        return Path(locator.name).as_uri()

    def preflight(self, locator: RepositoryLocator) -> RemoteMetadata:
        path = Path(locator.name)
        if not path.is_dir():
            raise ProviderError("Local repository not found")
        return RemoteMetadata(default_branch=None, size_bytes=None, is_private=False)


def locate_with(
    providers: list[RepositoryProvider], url: str
) -> tuple[RepositoryProvider, RepositoryLocator]:
    """Return the first provider that accepts ``url``."""
    errors: list[str] = []
    for provider in providers:
        try:
            return provider, provider.locate(url)
        except ValidationError as exc:
            errors.append(str(exc))
    raise ValidationError(errors[0] if errors else "Unsupported repository URL")
