"""Write a snapshot's files from Git objects into a fresh directory (for sandboxed execution).

Nothing is checked out with Git, so no filters, hooks or attributes run. Safety properties:

- every path is validated (no absolute paths, ``..``, ``.git`` components or control chars);
- files are created relative to directory file descriptors with ``O_NOFOLLOW | O_EXCL``, so
  a path can never be redirected through a symlink;
- all regular files are written *before* any symlink exists, and a symlink is created only if
  its target stays inside the workspace (absolute targets and escaping ``..`` are refused);
- submodules are skipped; size and file-count limits are enforced while writing.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from repolens.domain.enums import FileMode
from repolens.repository.git import GitRunner
from repolens.repository.inventory import InventoryIssue, _parse_tree

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class MaterializeError(RuntimeError):
    pass


@dataclass
class MaterializeResult:
    files: int = 0
    bytes: int = 0
    symlinks: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


def symlink_target_is_contained(link_path: str, target: str) -> bool:
    """True if ``target`` (relative to the link's directory) stays inside the root."""
    if not target or target.startswith("/") or "\0" in target or "\\" in target:
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(link_path), target))
    return joined != ".." and not joined.startswith("../") and not joined.startswith("/")


def materialize(
    runner: GitRunner,
    mirror: Path,
    commit_sha: str,
    destination: Path,
    *,
    max_bytes: int,
    max_files: int,
) -> MaterializeResult:
    destination.mkdir(parents=False, exist_ok=False)
    # Readable (not writable) by the unprivileged container user; the workspace lives under a
    # private data directory and is mounted read-only.
    destination.chmod(0o755)
    raw = runner.run(
        ["ls-tree", "-r", "-z", "-l", "--full-tree", "--end-of-options", commit_sha],
        git_dir=mirror,
    )
    issues: list[InventoryIssue] = []
    items = _parse_tree([r for r in raw.split(b"\0") if r], issues)
    result = MaterializeResult(skipped=[(i.path or "", i.code) for i in issues])
    if len(items) > max_files:
        raise MaterializeError(f"{len(items)} files exceed the limit of {max_files}")

    root_fd = os.open(destination, _DIR_FLAGS)
    try:
        links: list[tuple[str, str]] = []
        with runner.blob_reader(mirror) as reader:
            for path, mode, object_id, size in items:
                if mode is FileMode.SUBMODULE:
                    result.skipped.append((path, "SUBMODULE"))
                    continue
                if result.bytes + size > max_bytes:
                    raise MaterializeError(f"workspace would exceed {max_bytes} bytes")
                data = reader.read(object_id, max_bytes=max_bytes)
                if mode is FileMode.SYMLINK:
                    links.append((path, data.decode("utf-8", errors="replace")))
                    continue
                try:
                    _write_file(root_fd, path, data, executable=mode is FileMode.EXECUTABLE)
                except OSError as exc:
                    result.skipped.append((path, f"WRITE_FAILED:{exc.errno}"))
                    continue
                result.files += 1
                result.bytes += len(data)
        for path, target in links:  # only after every regular file exists
            if not symlink_target_is_contained(path, target):
                result.skipped.append((path, "SYMLINK_ESCAPES"))
                continue
            try:
                _create_symlink(root_fd, path, target)
            except OSError as exc:
                result.skipped.append((path, f"SYMLINK_FAILED:{exc.errno}"))
                continue
            result.symlinks += 1
    finally:
        os.close(root_fd)
    return result


def _open_parent(root_fd: int, path: str, *, create: bool) -> tuple[int, str]:
    """Open (creating if needed) the parent directory of ``path`` without following links."""
    parts = path.split("/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o755, dir_fd=current)
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
    except BaseException:
        os.close(current)
        raise
    return current, parts[-1]


def _write_file(root_fd: int, path: str, data: bytes, *, executable: bool) -> None:
    parent, name = _open_parent(root_fd, path, create=True)
    try:
        fd = os.open(name, _FILE_FLAGS, 0o755 if executable else 0o644, dir_fd=parent)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, 0o755 if executable else 0o644)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _create_symlink(root_fd: int, path: str, target: str) -> None:
    parent, name = _open_parent(root_fd, path, create=True)
    try:
        os.symlink(target, name, dir_fd=parent)
    finally:
        os.close(parent)
