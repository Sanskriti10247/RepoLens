"""The git runner must not let repository content or the host environment influence it."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from repolens.repository.git import (
    GitError,
    GitOutputLimitExceeded,
    GitPolicy,
    GitRunner,
    GitTimeout,
)
from tests.support.gitrepo import FixtureRepo

pytestmark = pytest.mark.security


@pytest.fixture
def runner(tmp_path: Path) -> GitRunner:
    return GitRunner(home_dir=tmp_path / "git-home")


def test_environment_is_not_inherited(runner: GitRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("GIT_DIR", "/etc")
    env = runner.environment()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GIT_DIR" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_repository_hooks_never_run(runner: GitRunner, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "hostile")
    marker = tmp_path / "pwned"
    hooks = repo.path / "evil-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "post-commit", "reference-transaction", "post-checkout"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    repo.git("config", "core.hooksPath", str(hooks))
    repo.write({"a.txt": "x"})
    repo.git("add", "-A")
    runner.run(
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "-m",
            "m",
        ],
        cwd=repo.path,
    )
    assert not marker.exists()


def test_repository_config_cannot_enable_fsmonitor(runner: GitRunner, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "hostile")
    marker = tmp_path / "pwned"
    repo.commit(files={"a.txt": "x"})
    repo.git("config", "core.fsmonitor", f"touch {marker}")
    repo.write({"a.txt": "changed"})
    runner.run(["status", "--porcelain"], cwd=repo.path)
    assert not marker.exists()
    # Control: an unhardened git does execute it, so the test is meaningful.
    repo.git("status", "--porcelain")
    assert marker.exists()


def test_file_protocol_blocked_by_default(runner: GitRunner, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "src")
    repo.commit(files={"a.txt": "x"})
    with pytest.raises(GitError):
        runner.run(["ls-remote", "--end-of-options", repo.url])


def test_file_protocol_allowed_when_configured(tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "src")
    sha = repo.commit(files={"a.txt": "x"})
    runner = GitRunner(tmp_path / "home", GitPolicy(allowed_protocols=("file",)))
    assert sha in runner.run(["ls-remote", "--end-of-options", repo.url]).decode()


@pytest.mark.parametrize(
    "url", ["ext::sh -c touch% /tmp/repolens-ext-pwned", "fd::17", "git://127.0.0.1/x"]
)
def test_dangerous_transports_are_blocked(runner: GitRunner, url: str) -> None:
    with pytest.raises(GitError):
        runner.run(["ls-remote", "--end-of-options", url], timeout=10)
    assert not Path("/tmp/repolens-ext-pwned").exists()


def _fake_git(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-git"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_timeout_kills_the_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    fake = _fake_git(tmp_path, f"sleep 30 & echo $! > {pidfile}; wait")
    runner = GitRunner(tmp_path / "home", git_binary=fake)
    started = time.monotonic()
    with pytest.raises(GitTimeout):
        runner.run(["anything"], timeout=0.5)
    assert time.monotonic() - started < 5
    child = int(pidfile.read_text())
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the grandchild was killed with the group


def test_output_limit(tmp_path: Path) -> None:
    fake = _fake_git(tmp_path, "head -c 5000000 /dev/zero")
    runner = GitRunner(tmp_path / "home", git_binary=fake)
    with pytest.raises(GitOutputLimitExceeded):
        runner.run(["anything"], max_output_bytes=1_000_000)


def test_failure_reports_stderr_summary(runner: GitRunner, tmp_path: Path) -> None:
    with pytest.raises(GitError) as info:
        runner.run(["rev-parse", "--verify", "nope"], cwd=FixtureRepo(tmp_path / "r").path)
    assert info.value.returncode != 0
    assert "fatal" in info.value.stderr or "Needed" in info.value.stderr


def test_blob_reader_reads_and_resyncs_after_oversize(runner: GitRunner, tmp_path: Path) -> None:
    repo = FixtureRepo(tmp_path / "r")
    repo.commit(files={"small.txt": "hello", "big.txt": "x" * 10_000})
    small = repo.git("rev-parse", "HEAD:small.txt")
    big = repo.git("rev-parse", "HEAD:big.txt")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    with runner.blob_reader(repo.path / ".git") as reader:
        assert reader.read(small, max_bytes=100) == b"hello"
        with pytest.raises(GitOutputLimitExceeded):
            reader.read(big, max_bytes=100)
        with pytest.raises(GitError, match="not a blob"):
            reader.read(tree, max_bytes=10**6)
        with pytest.raises(GitError):
            reader.read("0" * 40, max_bytes=100)
        assert reader.read(small, max_bytes=100) == b"hello"  # stream still in sync
