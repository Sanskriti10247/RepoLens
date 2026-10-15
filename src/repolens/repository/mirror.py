"""Bare mirrors of registered repositories and revision resolution.

Each repository is fetched into its own bare repository. Nothing is ever checked out, so
checkout-time features (smudge filters, LFS, hooks, symlink handling) never run. Source
files are read straight from Git objects.
"""

from __future__ import annotations

import fcntl
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from repolens.repository.git import GitError, GitRunner
from repolens.repository.validation import is_full_sha, looks_like_commit_id, validate_ref


class MirrorError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class RevisionNotFound(MirrorError):
    pass


class RevisionAmbiguous(MirrorError):
    pass


class RevisionKind(StrEnum):
    BRANCH = "BRANCH"
    TAG = "TAG"
    COMMIT = "COMMIT"


@dataclass(frozen=True, slots=True)
class ResolvedRevision:
    requested: str
    commit_sha: str
    kind: RevisionKind
    full_ref: str | None


@dataclass(frozen=True, slots=True)
class CommitInfo:
    commit_sha: str
    tree_sha: str
    committed_at: datetime


def directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            total += entry.stat().st_size
    return total


class MirrorStore:
    def __init__(self, root: Path, runner: GitRunner) -> None:
        self.root = root
        self.runner = runner
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, repository_id: uuid.UUID) -> Path:
        return self.root / f"{repository_id}.git"

    @contextmanager
    def locked(self, repository_id: uuid.UUID) -> Iterator[Path]:
        """Serialise fetches of one mirror across threads and processes."""
        lock_path = self.root / f"{repository_id}.lock"
        with lock_path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield self.path_for(repository_id)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def fetch(
        self,
        repository_id: uuid.UUID,
        url: str,
        *,
        max_bytes: int,
        timeout: float | None = None,
    ) -> Path:
        """Create or update the mirror. Removes it again if it exceeds ``max_bytes``."""
        with self.locked(repository_id) as mirror:
            if not (mirror / "HEAD").exists():
                shutil.rmtree(mirror, ignore_errors=True)
                self.runner.run(["init", "--bare", "--quiet", str(mirror)])
            try:
                self.runner.run(
                    [
                        "fetch",
                        "--prune",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--no-write-fetch-head",
                        "--quiet",
                        "--end-of-options",
                        url,
                        "+refs/heads/*:refs/heads/*",
                        "+refs/tags/*:refs/tags/*",
                    ],
                    git_dir=mirror,
                    timeout=timeout,
                )
            except GitError as exc:
                raise MirrorError(
                    f"Fetching the repository failed: {exc.stderr or exc}", retryable=True
                ) from exc
            self._sync_head(mirror, url, timeout=timeout)
            size = directory_size(mirror)
            if size > max_bytes:
                shutil.rmtree(mirror, ignore_errors=True)
                raise MirrorError(
                    f"Repository is {size // (1024 * 1024)} MiB, above the "
                    f"{max_bytes // (1024 * 1024)} MiB limit"
                )
            return mirror

    def _sync_head(self, mirror: Path, url: str, *, timeout: float | None) -> None:
        """Point the mirror's HEAD at the remote's default branch, if it advertises one."""
        try:
            out = self.runner.run(
                ["ls-remote", "--symref", "--end-of-options", url, "HEAD"],
                git_dir=mirror,
                timeout=timeout,
                max_output_bytes=64 * 1024,
            ).decode("utf-8", errors="replace")
        except GitError:
            return
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                branch = line[len("ref: refs/heads/") : -len("\tHEAD")]
                try:
                    validate_ref(branch)
                except ValueError:
                    return
                self.runner.run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], git_dir=mirror)
                return

    def default_branch(self, mirror: Path) -> str | None:
        return self._head_branch(mirror)

    def delete(self, repository_id: uuid.UUID) -> None:
        with self.locked(repository_id) as mirror:
            shutil.rmtree(mirror, ignore_errors=True)
        (self.root / f"{repository_id}.lock").unlink(missing_ok=True)

    # -- revisions ------------------------------------------------------------------------

    def resolve(self, mirror: Path, ref: str, *, default_branch: str | None) -> ResolvedRevision:
        """Resolve a branch, tag or commit id to a full commit SHA, refusing ambiguity."""
        ref = validate_ref(ref)
        if ref == "HEAD":
            branch = default_branch or self._head_branch(mirror)
            if branch is None:
                raise RevisionNotFound("Repository has no default branch")
            resolved = self.resolve(mirror, branch, default_branch=None)
            return ResolvedRevision("HEAD", resolved.commit_sha, resolved.kind, resolved.full_ref)

        candidates: list[ResolvedRevision] = []
        for prefix, kind in (
            ("refs/heads/", RevisionKind.BRANCH),
            ("refs/tags/", RevisionKind.TAG),
        ):
            sha = self._peel(mirror, prefix + ref)
            if sha is not None:
                candidates.append(ResolvedRevision(ref, sha, kind, prefix + ref))
        if looks_like_commit_id(ref):
            sha = self._peel(mirror, ref)
            if sha is not None and (not is_full_sha(ref) or sha == ref):
                candidates.append(ResolvedRevision(ref, sha, RevisionKind.COMMIT, None))
        distinct = {c.commit_sha for c in candidates}
        if not candidates:
            raise RevisionNotFound(f"Revision '{ref}' not found")
        if len(distinct) > 1:
            kinds = ", ".join(sorted(c.kind.value.lower() for c in candidates))
            raise RevisionAmbiguous(f"Revision '{ref}' is ambiguous ({kinds})")
        return candidates[0]

    def commit_info(self, mirror: Path, commit_sha: str) -> CommitInfo:
        out = self.runner.run(
            ["log", "-1", "--format=%H%x00%T%x00%cI", "--end-of-options", commit_sha],
            git_dir=mirror,
        ).decode()
        sha, tree, committed = out.strip().split("\x00")
        return CommitInfo(sha, tree, datetime.fromisoformat(committed))

    def _peel(self, mirror: Path, name: str) -> str | None:
        try:
            out = self.runner.run(
                ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{name}^{{commit}}"],
                git_dir=mirror,
            )
        except GitError:
            return None
        sha = out.decode().strip()
        return sha if is_full_sha(sha) else None

    def _head_branch(self, mirror: Path) -> str | None:
        try:
            out = self.runner.run(["symbolic-ref", "--quiet", "HEAD"], git_dir=mirror)
        except GitError:
            return None
        name = out.decode().strip()
        return name.removeprefix("refs/heads/") if name.startswith("refs/heads/") else None
