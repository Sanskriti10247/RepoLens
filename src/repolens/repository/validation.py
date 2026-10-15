"""Validation of untrusted repository identifiers: URLs and Git revisions.

Everything here is pure (no IO) so it can be tested exhaustively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """The input is not acceptable. The message is safe to show to API clients."""


# GitHub: owners are 1-39 alphanumerics/hyphens, not starting or ending with a hyphen.
_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_ALLOWED_HOSTS = {"github.com": "github"}


@dataclass(frozen=True, slots=True)
class RepositoryLocator:
    provider: str
    host: str
    owner: str
    name: str
    # Stable identity used for deduplication; each provider defines its normal form.
    canonical_url: str

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}.git"


def parse_repository_url(raw: str) -> RepositoryLocator:
    """Accept only ``https://github.com/<owner>/<repo>[.git][/]``.

    Rejects other schemes and hosts, credentials, ports, query strings, fragments, extra path
    segments and non-ASCII input (homoglyph / IDN tricks).
    """
    if not isinstance(raw, str) or not raw or len(raw) > 300:
        raise ValidationError("Repository URL must be a non-empty string of at most 300 characters")
    if not raw.isascii() or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValidationError(
            "Repository URL must be ASCII without whitespace or control characters"
        )
    if "\\" in raw or "%" in raw:
        raise ValidationError("Repository URL must not contain backslashes or percent-encoding")
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ValidationError("Only https:// repository URLs are supported")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValidationError("Repository URLs must not contain credentials")
    if parts.port is not None or ":" in parts.netloc:
        raise ValidationError("Repository URLs must not specify a port")
    host = parts.netloc.lower()
    provider = _ALLOWED_HOSTS.get(host)
    if provider is None:
        allowed = ", ".join(sorted(_ALLOWED_HOSTS))
        raise ValidationError(f"Unsupported repository host; allowed: {allowed}")
    if parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise ValidationError("Repository URLs must not contain a query string or fragment")

    path = parts.path.removesuffix("/")
    segments = path.split("/")[1:] if path.startswith("/") else []
    if len(segments) != 2:
        raise ValidationError("Expected https://github.com/<owner>/<repository>")
    owner, name = segments
    name = name.removesuffix(".git")
    if not _GITHUB_OWNER.match(owner):
        raise ValidationError("Invalid repository owner")
    if not _GITHUB_REPO.match(name) or name in {".", ".."} or name.startswith("."):
        raise ValidationError("Invalid repository name")
    return RepositoryLocator(
        provider=provider,
        host=host,
        owner=owner,
        name=name,
        # GitHub owner and repository names are case-insensitive.
        canonical_url=f"https://{host}/{owner.lower()}/{name.lower()}",
    )


_HEX_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FORBIDDEN_REF_CHARS = set(" ~^:?*[\\\x7f")
MAX_REF_LENGTH = 255


def is_full_sha(value: str) -> bool:
    return bool(_FULL_SHA.match(value))


def looks_like_commit_id(value: str) -> bool:
    return bool(_HEX_REVISION.match(value))


def validate_ref(ref: str) -> str:
    """Validate a branch/tag name or (possibly abbreviated) commit id.

    Implements the rules of ``git check-ref-format`` for a single ref name, plus a ban on a
    leading ``-`` so a ref can never be interpreted as a command-line option.
    """
    if not isinstance(ref, str) or not ref:
        raise ValidationError("Revision must be a non-empty string")
    if len(ref) > MAX_REF_LENGTH:
        raise ValidationError(f"Revision must be at most {MAX_REF_LENGTH} characters")
    if not ref.isascii():
        raise ValidationError("Revision must be ASCII")
    if looks_like_commit_id(ref):
        return ref
    problems = [
        (ref.startswith("-"), "must not start with '-'"),
        (ref.startswith("/") or ref.endswith("/"), "must not start or end with '/'"),
        ("//" in ref, "must not contain '//'"),
        (".." in ref, "must not contain '..'"),
        ("@{" in ref, "must not contain '@{'"),
        (ref == "@", "must not be '@'"),
        (ref.endswith("."), "must not end with '.'"),
        (any(ord(ch) < 32 for ch in ref), "must not contain control characters"),
        (any(ch in _FORBIDDEN_REF_CHARS for ch in ref), "contains a forbidden character"),
        (
            any(c.startswith(".") or c.endswith(".lock") for c in ref.split("/")),
            "path components must not start with '.' or end with '.lock'",
        ),
    ]
    for failed, message in problems:
        if failed:
            raise ValidationError(f"Invalid revision: {message}")
    return ref
