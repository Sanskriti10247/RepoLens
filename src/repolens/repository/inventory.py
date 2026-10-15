"""Snapshot inventory: every file in a commit, read from Git objects (nothing checked out)."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from repolens.domain.enums import FileMode, FileRole, Severity
from repolens.repository.git import BlobReader, GitOutputLimitExceeded, GitRunner

_MODES = {
    "100644": FileMode.REGULAR,
    "100755": FileMode.EXECUTABLE,
    "120000": FileMode.SYMLINK,
    "160000": FileMode.SUBMODULE,
}

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".rb": "ruby",
    ".sh": "shell",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".ini": "ini",
    ".cfg": "ini",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}
_CONFIG_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "noxfile.py",
    "requirements.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "Makefile",
    "Dockerfile",
    ".coveragerc",
    "MANIFEST.in",
}
_CONFIG_SUFFIXES = {".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".lock"}
_DOC_SUFFIXES = {".md", ".rst", ".txt"}


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    path: str
    mode: FileMode
    blob_sha: str
    size_bytes: int
    language: str | None
    role: FileRole
    is_binary: bool
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class InventoryIssue:
    code: str
    severity: Severity
    message: str
    path: str | None = None


@dataclass
class Inventory:
    entries: list[InventoryEntry] = field(default_factory=list)
    issues: list[InventoryIssue] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    def language_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(e.language for e in self.entries if e.language).items()))

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for entry in sorted(self.entries, key=lambda e: e.path):
            digest.update(f"{entry.path}\0{entry.mode.value}\0{entry.blob_sha}\n".encode())
        return digest.hexdigest()


class InventoryTooLarge(RuntimeError):
    pass


def detect_language(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def classify_role(path: str) -> FileRole:
    pure = PurePosixPath(path)
    name = pure.name
    parts = {p.lower() for p in pure.parts[:-1]}
    if pure.suffix == ".py" and (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or parts & {"tests", "test", "testing"}
    ):
        return FileRole.TEST
    if name in _CONFIG_NAMES or pure.suffix.lower() in _CONFIG_SUFFIXES:
        return FileRole.CONFIG
    if pure.suffix.lower() in _DOC_SUFFIXES or "docs" in parts or "doc" in parts:
        return FileRole.DOC
    if detect_language(path) is not None:
        return FileRole.SOURCE
    return FileRole.OTHER


def path_problem(path: str) -> str | None:
    """Return why ``path`` is unsafe to use as a repository-relative path, or None."""
    if not path or path.startswith("/") or "\\" in path:
        return "absolute or non-POSIX path"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "path contains empty, '.' or '..' components"
    if any(p.lower() == ".git" for p in parts):
        return "path contains a .git component"
    if any(ord(ch) < 32 for ch in path):
        return "path contains control characters"
    return None


def build_inventory(
    runner: GitRunner,
    mirror: Path,
    commit_sha: str,
    *,
    max_files: int,
    max_hash_bytes: int,
    checkpoint: Callable[[], None] = lambda: None,
) -> Inventory:
    raw = runner.run(
        ["ls-tree", "-r", "-z", "-l", "--full-tree", "--end-of-options", commit_sha],
        git_dir=mirror,
    )
    records = [r for r in raw.split(b"\0") if r]
    if len(records) > max_files:
        raise InventoryTooLarge(f"Snapshot has {len(records)} files, above the {max_files} limit")
    inventory = Inventory()
    pending = _parse_tree(records, inventory.issues)
    with runner.blob_reader(mirror) as reader:
        for index, item in enumerate(pending):
            if index % 200 == 0:
                checkpoint()
            inventory.entries.append(_describe(reader, item, max_hash_bytes, inventory.issues))
    return inventory


_TreeItem = tuple[str, FileMode, str, int]  # path, mode, object id, size


def _parse_tree(records: list[bytes], issues: list[InventoryIssue]) -> list[_TreeItem]:
    items: list[_TreeItem] = []
    seen_casefold: dict[str, str] = {}
    for record in records:
        meta, _, raw_path = record.partition(b"\t")
        mode_text, _kind, object_id, size_text = meta.decode("ascii").split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            shown = raw_path.decode("utf-8", errors="replace")
            issues.append(
                InventoryIssue("PATH_NOT_UTF8", Severity.WARNING, "Skipped non-UTF-8 path", shown)
            )
            continue
        problem = path_problem(path)
        mode = _MODES.get(mode_text)
        if problem is not None or mode is None:
            reason = problem or f"unknown mode {mode_text}"
            issues.append(
                InventoryIssue("PATH_UNSAFE", Severity.ERROR, f"Skipped file: {reason}", path)
            )
            continue
        folded = path.casefold()
        if folded in seen_casefold:
            message = f"Path differs only by case from {seen_casefold[folded]}"
            issues.append(InventoryIssue("PATH_CASE_COLLISION", Severity.WARNING, message, path))
        seen_casefold.setdefault(folded, path)
        items.append((path, mode, object_id, int(size_text) if size_text.isdigit() else 0))
    return items


def _describe(
    reader: BlobReader, item: _TreeItem, max_hash_bytes: int, issues: list[InventoryIssue]
) -> InventoryEntry:
    path, mode, object_id, size = item
    regular = mode in (FileMode.REGULAR, FileMode.EXECUTABLE)
    content_sha256: str | None = None
    is_binary = False
    if regular and size <= max_hash_bytes:
        try:
            data = reader.read(object_id, max_bytes=max_hash_bytes)
        except GitOutputLimitExceeded:
            data = None
        if data is not None:
            content_sha256 = hashlib.sha256(data).hexdigest()
            is_binary = b"\0" in data[:8000]
    elif regular:
        message = f"File larger than {max_hash_bytes} bytes was not hashed"
        issues.append(InventoryIssue("FILE_NOT_HASHED", Severity.INFO, message, path))
    elif mode is FileMode.SUBMODULE:
        message = "Submodule contents are not fetched or analysed"
        issues.append(InventoryIssue("SUBMODULE_NOT_FETCHED", Severity.WARNING, message, path))
    return InventoryEntry(
        path=path,
        mode=mode,
        blob_sha=object_id,
        size_bytes=size,
        language=detect_language(path) if regular and not is_binary else None,
        role=classify_role(path) if regular else FileRole.OTHER,
        is_binary=is_binary,
        content_sha256=content_sha256,
    )
