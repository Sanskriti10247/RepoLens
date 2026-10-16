"""Safe materialization of snapshots into sandbox workspaces."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from repolens.repository.git import GitPolicy, GitRunner
from repolens.repository.materialize import (
    MaterializeError,
    _write_file,
    materialize,
    symlink_target_is_contained,
)
from repolens.repository.mirror import MirrorError, MirrorStore
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.security


@pytest.fixture
def runner(tmp_path: Path) -> GitRunner:
    return GitRunner(tmp_path / "home", GitPolicy(allowed_protocols=("file",)))


def _mirror(tmp_path: Path, runner: GitRunner, repo: FixtureRepo) -> Path:
    return MirrorStore(tmp_path / "mirrors", runner).fetch(uuid.uuid4(), repo.url, max_bytes=10**8)


def test_materializes_files_modes_and_contained_symlinks(tmp_path: Path, runner: GitRunner) -> None:
    repo = FixtureRepo(tmp_path / "src")
    repo.write({"pkg/a.py": "x = 1\n", "run.sh": "#!/bin/sh\n", "ünï/ç.txt": "u\n"})
    (repo.path / "run.sh").chmod(0o755)
    repo.symlink("pkg/alias.py", "a.py")
    repo.symlink("up", "pkg/../pkg")
    sha = repo.commit("files")
    dest = tmp_path / "ws"
    result = materialize(
        runner, _mirror(tmp_path, runner, repo), sha, dest, max_bytes=10**6, max_files=100
    )
    assert (dest / "pkg/a.py").read_text() == "x = 1\n"
    assert os.access(dest / "run.sh", os.X_OK)
    assert (dest / "ünï/ç.txt").exists()
    assert str((dest / "pkg/alias.py").readlink()) == "a.py"
    assert (result.files, result.symlinks) == (3, 2)
    assert oct((dest / "pkg/a.py").stat().st_mode & 0o777) == "0o644"


def test_chained_symlinks_cannot_escape(tmp_path: Path, runner: GitRunner) -> None:
    # Each link is contained on its own; together ``chain`` resolves to the workspace parent.
    repo = FixtureRepo(tmp_path / "src")
    repo.write({"d/keep.txt": "k"})
    repo.symlink("d/up", "..")
    repo.symlink("chain", "d/up/..")
    sha = repo.commit("links")
    dest = tmp_path / "ws"
    result = materialize(
        runner, _mirror(tmp_path, runner, repo), sha, dest, max_bytes=10**6, max_files=100
    )
    assert not (dest / "chain").is_symlink()
    assert ("chain", "SYMLINK_ESCAPES") in result.skipped
    assert (dest / "d/up").is_symlink()  # resolves to the root itself: allowed
    assert result.symlinks == 1


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("evil", "/etc/passwd"),
        ("evil", "../outside"),
        ("deep/evil", "../../outside"),
        ("evil", ".."),
    ],
)
def test_escaping_symlinks_are_not_created(
    tmp_path: Path, runner: GitRunner, link: str, target: str
) -> None:
    repo = FixtureRepo(tmp_path / "src")
    repo.write({"keep.txt": "k"})
    repo.symlink(link, target)
    sha = repo.commit("links")
    dest = tmp_path / "ws"
    result = materialize(
        runner, _mirror(tmp_path, runner, repo), sha, dest, max_bytes=10**6, max_files=100
    )
    assert not (dest / link).is_symlink()
    assert (link, "SYMLINK_ESCAPES") in result.skipped


def test_file_writes_never_follow_symlinks(tmp_path: Path) -> None:
    """Even if a symlinked directory existed in the workspace, writes cannot pass through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "dir").symlink_to(outside)
    (workspace / "file-link").symlink_to(outside / "target.txt")
    root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            _write_file(root_fd, "dir/victim", b"pwned", executable=False)
        with pytest.raises(OSError):
            _write_file(root_fd, "file-link", b"pwned", executable=False)
    finally:
        os.close(root_fd)
    assert list(outside.iterdir()) == []


def test_git_rejects_trees_that_shadow_a_symlink_with_a_directory(
    tmp_path: Path, runner: GitRunner
) -> None:
    """The tree shape needed to attempt such a redirect never reaches the mirror."""
    repo = FixtureRepo(tmp_path / "src")

    def git_input(*args: str, data: bytes) -> str:
        return (
            subprocess.run(
                ["git", *args], cwd=repo.path, input=data, capture_output=True, check=True
            )
            .stdout.decode()
            .strip()
        )

    link = git_input("hash-object", "-w", "--stdin", data=b"/tmp")
    blob = git_input("hash-object", "-w", "--stdin", data=b"pwned")
    sub = git_input("mktree", data=f"100644 blob {blob}\tvictim\n".encode())
    root = git_input("mktree", data=f"120000 blob {link}\tdir\n040000 tree {sub}\tdir\n".encode())
    repo.git("update-ref", "refs/heads/main", repo.git("commit-tree", root, "-m", "evil"))
    with pytest.raises(MirrorError):
        MirrorStore(tmp_path / "mirrors", runner).fetch(uuid.uuid4(), repo.url, max_bytes=10**8)


def test_limits(tmp_path: Path, runner: GitRunner) -> None:
    repo = FixtureRepo(tmp_path / "src")
    sha = repo.commit("big", {f"f{i}.txt": "x" * 100 for i in range(10)})
    mirror = _mirror(tmp_path, runner, repo)
    with pytest.raises(MaterializeError):
        materialize(runner, mirror, sha, tmp_path / "a", max_bytes=500, max_files=100)
    with pytest.raises(MaterializeError):
        materialize(runner, mirror, sha, tmp_path / "b", max_bytes=10**6, max_files=5)


def test_destination_must_not_exist(tmp_path: Path, runner: GitRunner) -> None:
    repo = FixtureRepo(tmp_path / "src")
    sha = repo.commit("x", {"a": "1"})
    existing = tmp_path / "ws"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        materialize(
            runner, _mirror(tmp_path, runner, repo), sha, existing, max_bytes=10**6, max_files=10
        )


@pytest.mark.parametrize(
    ("link", "target", "ok"),
    [
        ("a", "b", True),
        ("d/a", "../b", True),
        ("d/a", "../../b", False),
        ("a", "/abs", False),
        ("a", "", False),
        ("a", "x\\y", False),
        ("d/e/a", "../../d", True),
    ],
)
def test_symlink_containment(link: str, target: str, ok: bool) -> None:
    assert symlink_target_is_contained(link, target) is ok
