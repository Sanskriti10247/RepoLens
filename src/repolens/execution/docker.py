"""Docker sandbox backend (``docker`` CLI with argument vectors, never a shell).

Container hardening (see docs/security/sandbox.md for the threat model):
non-root user, all capabilities dropped, ``no-new-privileges``, default seccomp profile,
read-only root filesystem, size-limited tmpfs for every writable path, no network, PID /
memory (no extra swap) / CPU / open-file limits, ``--init`` for signal handling and zombie
reaping, no Docker socket, no host credentials, no environment passthrough, the snapshot
mounted read-only, and logs not written by the Docker daemon (output is streamed and capped).

Results leave the container only through the bounded stdout envelope, never through a
writable host mount, so a test cannot fill the host disk.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from repolens.execution.policy import SandboxLimits

ENVELOPE_PREFIX = "REPOLENS-RESULT "
MANAGED_LABEL = "repolens.managed"


class SandboxUnavailable(RuntimeError):
    """The sandbox could not be started (daemon down, image missing): infrastructure error."""


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    name: str  # container name, unique per execution
    image: str
    source_dir: Path  # materialized snapshot, mounted read-only at /src
    arguments: list[str]  # runner arguments (mode, flags, node ids)
    limits: SandboxLimits
    labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class SandboxResult:
    envelope: dict[str, Any] | None
    container_exit_code: int | None
    timed_out: bool
    cancelled: bool
    oom_killed: bool
    output_truncated: bool
    stderr_tail: str
    duration_seconds: float
    cleanup_ok: bool


StopCheck = Callable[[], str | None]  # returns a reason to stop ("cancelled", ...) or None


class DockerSandbox:
    def __init__(self, docker_binary: str = "docker") -> None:
        self.docker = docker_binary

    # -- command construction (pure, unit-tested) --------------------------------------------

    def run_command(self, request: SandboxRequest) -> list[str]:
        limits = request.limits
        command = [
            self.docker,
            "run",
            "--name",
            request.name,
            "--label",
            f"{MANAGED_LABEL}=1",
        ]
        for key, value in sorted(request.labels.items()):
            command += ["--label", f"{key}={value}"]
        command += [
            "--log-driver",
            "none",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={limits.tmp_mb}m",  # noqa: S108 - container path
            "--tmpfs",
            f"/work:rw,nosuid,nodev,size={limits.workspace_mb}m,uid=10001,gid=10001,mode=0700",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpus),
            "--ulimit",
            f"nofile={limits.open_files}:{limits.open_files}",
            "--ulimit",
            "core=0:0",  # no core dumps, whatever the host's core_pattern does with them
            "--ipc",
            "private",
            "--init",
            "--stop-timeout",
            "1",
            "--mount",
            f"type=bind,source={request.source_dir},target=/src,readonly",
            request.image,
            *request.arguments,
        ]
        return command

    # -- execution -----------------------------------------------------------------------

    def run(self, request: SandboxRequest, should_stop: StopCheck) -> SandboxResult:
        started = time.monotonic()
        deadline = started + request.limits.timeout_seconds
        try:
            proc = subprocess.Popen(
                self.run_command(request),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"cannot run docker: {exc}") from exc
        stdout = bytearray()
        stderr = bytearray()
        truncated = False
        stop_reason: str | None = None
        try:
            stop_reason, truncated = self._pump(
                proc, stdout, stderr, deadline, request.limits.max_output_bytes, should_stop
            )
        finally:
            if stop_reason is not None or proc.poll() is None:
                self._kill(request.name)
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=30)
            if proc.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        state = self._inspect(request.name)
        cleanup_ok = self.remove(request.name)
        stderr_text = bytes(stderr[-8192:]).decode("utf-8", errors="replace")
        if state is None and proc.returncode == 125:
            raise SandboxUnavailable(f"container failed to start: {stderr_text.strip()[-500:]}")
        return SandboxResult(
            envelope=parse_envelope(bytes(stdout)),
            container_exit_code=state.get("ExitCode") if state else proc.returncode,
            timed_out=stop_reason == "timeout",
            cancelled=stop_reason is not None and stop_reason != "timeout",
            oom_killed=bool(state and state.get("OOMKilled")),
            output_truncated=truncated,
            stderr_tail=stderr_text,
            duration_seconds=round(time.monotonic() - started, 3),
            cleanup_ok=cleanup_ok,
        )

    def _pump(
        self,
        proc: subprocess.Popen[bytes],
        stdout: bytearray,
        stderr: bytearray,
        deadline: float,
        max_output: int,
        should_stop: StopCheck,
    ) -> tuple[str | None, bool]:
        assert proc.stdout is not None and proc.stderr is not None  # noqa: S101
        truncated = False
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, stdout)
            selector.register(proc.stderr, selectors.EVENT_READ, stderr)
            while selector.get_map():
                if time.monotonic() > deadline:
                    return "timeout", truncated
                reason = should_stop()
                if reason is not None:
                    return reason, truncated
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 1 << 16)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer: bytearray = key.data
                    room = max_output - len(buffer)
                    if room <= 0:
                        truncated = True
                        continue  # keep draining so the container is never blocked on a pipe
                    buffer.extend(chunk[:room])
                    truncated = truncated or len(chunk) > room
        return None, truncated

    def _docker(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self.docker, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    def _kill(self, name: str) -> None:
        with suppress(subprocess.TimeoutExpired, OSError):
            self._docker("kill", "--signal", "KILL", name)

    def _inspect(self, name: str) -> dict[str, Any] | None:
        try:
            result = self._docker("inspect", "--format", "{{json .State}}", name)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            state: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return state

    def remove(self, name: str) -> bool:
        try:
            result = self._docker("rm", "--force", "--volumes", name)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0 or b"No such container" in result.stderr

    def remove_stale(self, older_than_seconds: float) -> list[str]:
        """Remove managed containers left behind by crashed workers."""
        try:
            listing = self._docker(
                "ps",
                "--all",
                "--filter",
                f"label={MANAGED_LABEL}=1",
                "--format",
                "{{.Names}}\t{{.CreatedAt}}",
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        removed: list[str] = []
        now = time.time()
        for line in listing.stdout.decode(errors="replace").splitlines():
            name, _, created = line.partition("\t")
            created_at = _parse_docker_time(created)
            stale = created_at is not None and now - created_at > older_than_seconds
            if stale and self.remove(name):
                removed.append(name)
        return removed

    def available(self) -> bool:
        try:
            return self._docker("version", "--format", "{{.Server.Version}}").returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def image_present(self, image: str) -> bool:
        try:
            return self._docker("image", "inspect", image).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def parse_envelope(stdout: bytes) -> dict[str, Any] | None:
    """The runner's result: the last line starting with the envelope prefix, if well formed."""
    text = stdout.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if line.startswith(ENVELOPE_PREFIX):
            try:
                data = json.loads(line[len(ENVELOPE_PREFIX) :])
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _parse_docker_time(value: str) -> float | None:
    # e.g. "2026-10-16 05:31:12 +0530 IST"
    try:
        return datetime.strptime(" ".join(value.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None
