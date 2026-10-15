"""Build real Git repositories for tests, deterministically and without user config."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "LC_ALL": "C",
}


class FixtureRepo:
    def __init__(self, path: Path, *, branch: str = "main") -> None:
        self.path = path
        self._tick = 0
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", branch)

    @property
    def url(self) -> str:
        return self.path.resolve().as_uri()

    def git(self, *args: str) -> str:
        env = {**_ENV, "GIT_AUTHOR_DATE": self._date(), "GIT_COMMITTER_DATE": self._date()}
        result = subprocess.run(
            ["git", *args], cwd=self.path, env=env, check=True, capture_output=True
        )
        return result.stdout.decode().strip()

    def write(self, files: dict[str, str | bytes]) -> None:
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

    def remove(self, *paths: str) -> None:
        self.git("rm", "-q", "-r", "--", *paths)

    def symlink(self, rel: str, target: str) -> None:
        link = self.path / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    def commit(self, message: str = "change", files: dict[str, str | bytes] | None = None) -> str:
        if files:
            self.write(files)
        self._tick += 1
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, ref: str = "HEAD") -> None:
        self.git("tag", name, ref)

    def branch(self, name: str, ref: str = "HEAD") -> None:
        self.git("branch", name, ref)

    def _date(self) -> str:
        return f"2026-01-01T00:{self._tick // 60:02d}:{self._tick % 60:02d}Z"
