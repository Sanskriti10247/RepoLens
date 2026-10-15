"""Hardened execution of the ``git`` CLI.

Repository contents and remotes are untrusted. Every invocation:
- runs with a minimal, explicit environment (nothing inherited except ``PATH``);
- ignores system and global Git configuration and uses an empty ``HOME``;
- disables hooks, fsmonitor, credential helpers, prompts, askpass, LFS smudging, replace refs;
- allows only the configured transport protocols (``https`` by default) and no redirects;
- verifies received objects (``transfer.fsckObjects``);
- uses argument vectors (never a shell) and a deadline that kills the whole process group;
- caps captured output so a hostile repository cannot exhaust memory.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


class GitError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class GitTimeout(GitError):
    pass


class GitOutputLimitExceeded(GitError):
    pass


@dataclass(frozen=True, slots=True)
class GitPolicy:
    allowed_protocols: tuple[str, ...] = ("https",)
    timeout_seconds: float = 300.0
    max_output_bytes: int = 256 * 1024 * 1024
    low_speed_limit_bytes: int = 1000  # abort transfers slower than this ...
    low_speed_time_seconds: int = 60  # ... for this long


@dataclass
class GitRunner:
    home_dir: Path
    policy: GitPolicy = field(default_factory=GitPolicy)
    git_binary: str = "git"

    def __post_init__(self) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- configuration --------------------------------------------------------------------

    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", _DEFAULT_PATH),
            "HOME": str(self.home_dir),
            "XDG_CONFIG_HOME": str(self.home_dir),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "false",
            "SSH_ASKPASS": "false",
            "GIT_SSH_COMMAND": "false",
            "GIT_ALLOW_PROTOCOL": ":".join(self.policy.allowed_protocols),
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

    def config_args(self) -> list[str]:
        settings = {
            "core.hooksPath": os.devnull,
            "core.fsmonitor": "false",
            "core.askPass": "false",
            "credential.helper": "",
            "protocol.allow": "never",
            "http.followRedirects": "false",
            "http.lowSpeedLimit": str(self.policy.low_speed_limit_bytes),
            "http.lowSpeedTime": str(self.policy.low_speed_time_seconds),
            "transfer.fsckObjects": "true",
            "submodule.recurse": "false",
            "fetch.recurseSubmodules": "false",
            "init.defaultBranch": "main",
            "advice.detachedHead": "false",
            "gc.auto": "0",
            "maintenance.auto": "false",
        }
        for protocol in self.policy.allowed_protocols:
            settings[f"protocol.{protocol}.allow"] = "always"
        args: list[str] = []
        for key, value in settings.items():
            args += ["-c", f"{key}={value}"]
        return args

    def command(self, args: Sequence[str], *, git_dir: Path | None = None) -> list[str]:
        cmd = [self.git_binary, *self.config_args()]
        if git_dir is not None:
            cmd.append(f"--git-dir={git_dir}")
        return [*cmd, *args]

    # -- execution ------------------------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        *,
        git_dir: Path | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        max_output_bytes: int | None = None,
    ) -> bytes:
        """Run git and return stdout. Raises ``GitError`` on non-zero exit."""
        cmd = self.command(args, git_dir=git_dir)
        limit = max_output_bytes or self.policy.max_output_bytes
        deadline = time.monotonic() + (timeout or self.policy.timeout_seconds)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment(),
            cwd=cwd or self.home_dir,
            start_new_session=True,
        )
        try:
            stdout, stderr = _collect(proc, deadline, limit)
        except BaseException:
            _kill_group(proc)
            raise
        returncode = proc.wait()
        if returncode != 0:
            message = _stderr_summary(stderr)
            raise GitError(
                f"git {args[0] if args else ''} failed ({returncode}): {message}",
                returncode=returncode,
                stderr=message,
            )
        return stdout

    @contextmanager
    def blob_reader(self, git_dir: Path, *, timeout: float | None = None) -> Iterator[BlobReader]:
        """A long-running ``git cat-file --batch`` process for reading many objects."""
        proc = subprocess.Popen(
            self.command(["cat-file", "--batch"], git_dir=git_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.environment(),
            cwd=self.home_dir,
            start_new_session=True,
        )
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            _kill_group(proc)

        watchdog = threading.Timer(timeout or self.policy.timeout_seconds, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            yield BlobReader(proc, expired)
        finally:
            watchdog.cancel()
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            _kill_group(proc)
            proc.wait()


class BlobReader:
    def __init__(self, proc: subprocess.Popen[bytes], expired: threading.Event) -> None:
        assert proc.stdin is not None and proc.stdout is not None  # noqa: S101 - Popen contract
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        self._expired = expired

    def read(self, object_id: str, *, max_bytes: int) -> bytes:
        """Return the object's content. Raises if missing, not a blob, or larger than allowed."""
        try:
            self._stdin.write(object_id.encode("ascii") + b"\n")
            self._stdin.flush()
            header = self._stdout.readline()
        except (BrokenPipeError, ValueError) as exc:
            raise self._failure("git cat-file stopped") from exc
        parts = header.split()
        if len(parts) != 3:
            raise self._failure(f"object {object_id} unavailable: {header[:100]!r}")
        _, kind, size_text = parts
        size = int(size_text)
        if kind != b"blob" or size > max_bytes:
            self._read_exact(size + 1, keep=False)  # stay in sync without buffering it
            if kind != b"blob":
                raise GitError(f"object {object_id} is a {kind.decode()}, not a blob")
            raise GitOutputLimitExceeded(f"object {object_id} is {size} bytes (> {max_bytes})")
        return self._read_exact(size + 1)[:-1]  # content is followed by a newline

    def _read_exact(self, size: int, *, keep: bool = True) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._stdout.read(min(remaining, 1 << 20))
            if not chunk:
                raise self._failure("unexpected end of git cat-file output")
            if keep:
                chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _failure(self, message: str) -> GitError:
        if self._expired.is_set():
            return GitTimeout("git cat-file exceeded its time limit")
        return GitError(message)


def _collect(proc: subprocess.Popen[bytes], deadline: float, limit: int) -> tuple[bytes, bytes]:
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - Popen contract
    buffers = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    stderr_limit = 64 * 1024
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitTimeout("git command exceeded its time limit")
            for key, _ in selector.select(timeout=min(remaining, 1.0)):
                chunk = os.read(key.fd, 1 << 16)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.fd]
                if key.fileobj is proc.stderr:
                    if len(buffer) < stderr_limit:
                        buffer.extend(chunk[: stderr_limit - len(buffer)])
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise GitOutputLimitExceeded(f"git output exceeded {limit} bytes")
    return bytes(buffers[proc.stdout.fileno()]), bytes(buffers[proc.stderr.fileno()])


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    # Kill the whole session even if the direct child already exited: helpers such as
    # git-remote-https may still be alive in the same process group.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _stderr_summary(stderr: bytes, limit: int = 500) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    summary = " | ".join(lines[-3:])
    return summary[:limit]
