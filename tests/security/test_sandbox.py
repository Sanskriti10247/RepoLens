"""Adversarial checks of the Docker sandbox (requires Docker and the sandbox image).

Each scenario runs real test code inside the hardened container and checks both the
classified outcome and the effect on the host.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from repolens.execution.docker import DockerSandbox, SandboxRequest, SandboxResult
from repolens.execution.outcomes import RunOutcome, TestOutcome, classify, parse_junit
from repolens.execution.policy import SandboxLimits

IMAGE = os.environ.get("REPOLENS_SANDBOX_IMAGE", "repolens-sandbox:py3.12-1")
SANDBOX = DockerSandbox()
pytestmark = [pytest.mark.security, pytest.mark.docker]


@pytest.fixture(scope="module", autouse=True)
def require_sandbox() -> None:
    if not SANDBOX.available() or not SANDBOX.image_present(IMAGE):
        if os.environ.get("REPOLENS_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker sandbox required but unavailable")
        pytest.skip("Docker or the sandbox image is unavailable")


def run_tests(
    tmp_path: Path,
    test_source: str,
    *,
    limits: SandboxLimits | None = None,
    stop_after: float | None = None,
    extra_files: dict[str, str] | None = None,
) -> tuple[SandboxResult, str]:
    project = tmp_path / f"project-{uuid.uuid4().hex[:6]}"
    files = {"tests/test_attack.py": textwrap.dedent(test_source), **(extra_files or {})}
    for rel, content in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["chmod", "-R", "a+rX", str(project)], check=True)
    name = f"repolens-test-{uuid.uuid4().hex[:10]}"
    started = time.monotonic()

    def should_stop() -> str | None:
        if stop_after is not None and time.monotonic() - started > stop_after:
            return "cancelled"
        return None

    result = SANDBOX.run(
        SandboxRequest(
            name=name,
            image=IMAGE,
            source_dir=project,
            arguments=["run"],
            limits=limits or SandboxLimits(timeout_seconds=60, memory_mb=256, pids=128),
            labels={"repolens.test": "1"},
        ),
        should_stop,
    )
    return result, name


def container_exists(name: str) -> bool:
    listing = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name=^{name}$"],
        capture_output=True,
        check=True,
    )
    return bool(listing.stdout.strip())


def outcomes(result: SandboxResult) -> dict[str, TestOutcome]:
    junit = (result.envelope or {}).get("junit") or ""
    return {r.name: r.outcome for r in parse_junit(junit)} if junit else {}


def test_container_is_unprivileged_and_isolated(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("top secret")
    os.environ["REPOLENS_FAKE_SECRET"] = "leak-me"
    try:
        result, name = run_tests(
            tmp_path,
            f"""
            import os, socket

            def test_not_root():
                assert os.getuid() != 0 and os.getgid() != 0

            def test_no_capabilities():
                status = open("/proc/self/status").read()
                assert "CapEff:\\t0000000000000000" in status

            def test_no_docker_socket():
                assert not os.path.exists("/var/run/docker.sock")
                assert not os.path.exists("/run/docker.sock")

            def test_host_files_not_visible():
                assert not os.path.exists({str(secret)!r})

            def test_no_host_environment():
                assert "REPOLENS_FAKE_SECRET" not in os.environ
                assert not any("TOKEN" in k or "SECRET" in k for k in os.environ)

            def test_root_filesystem_read_only():
                try:
                    open("/usr/pwned", "w")
                except OSError:
                    return
                raise AssertionError("root filesystem is writable")

            def test_source_mount_read_only():
                try:
                    open("/src/pwned", "w")
                except OSError:
                    return
                raise AssertionError("source mount is writable")

            def test_no_network():
                try:
                    socket.create_connection(("1.1.1.1", 53), timeout=3)
                except OSError:
                    return
                raise AssertionError("network reachable")
            """,
        )
    finally:
        del os.environ["REPOLENS_FAKE_SECRET"]
    classification = classify(result)
    assert classification.outcome is RunOutcome.PASSED, (classification, result.envelope)
    assert classification.counts["passed"] == 8
    assert not container_exists(name) and result.cleanup_ok


def test_infinite_loop_times_out_and_is_removed(tmp_path: Path) -> None:
    result, name = run_tests(
        tmp_path,
        "def test_forever():\n    while True:\n        pass\n",
        limits=SandboxLimits(timeout_seconds=4, memory_mb=256, pids=64),
    )
    assert classify(result).outcome is RunOutcome.TIMED_OUT
    assert result.duration_seconds < 20
    assert not container_exists(name)


def test_cancellation_stops_the_container(tmp_path: Path) -> None:
    result, name = run_tests(
        tmp_path, "import time\n\ndef test_slow():\n    time.sleep(120)\n", stop_after=2
    )
    assert classify(result).outcome is RunOutcome.CANCELLED
    assert result.duration_seconds < 20
    assert not container_exists(name)


def test_process_exhaustion_is_contained(tmp_path: Path) -> None:
    result, _ = run_tests(
        tmp_path,
        """
        import subprocess

        def test_fork_bomb():
            children = []
            try:
                for _ in range(10_000):
                    children.append(subprocess.Popen(["sleep", "30"]))
            except (BlockingIOError, OSError):
                pass
            assert len(children) < 200, len(children)
        """,
        limits=SandboxLimits(timeout_seconds=60, memory_mb=256, pids=64),
    )
    assert classify(result).outcome is RunOutcome.PASSED, result.envelope


def test_memory_exhaustion_is_reported_as_resource_limit(tmp_path: Path) -> None:
    result, _ = run_tests(
        tmp_path,
        "def test_eat_memory():\n    hog = bytearray(1024 * 1024 * 1024)\n    assert hog\n",
        limits=SandboxLimits(timeout_seconds=60, memory_mb=128, pids=64),
    )
    classification = classify(result)
    assert classification.outcome in (RunOutcome.RESOURCE_LIMIT_EXCEEDED, RunOutcome.FAILED)
    assert classification.outcome is not RunOutcome.PASSED


def test_disk_exhaustion_stays_inside_tmpfs(tmp_path: Path) -> None:
    before = os.statvfs(tmp_path).f_bavail
    result, _ = run_tests(
        tmp_path,
        """
        def test_fill_disk():
            with open("/work/fill", "wb") as handle:
                try:
                    while True:
                        handle.write(b"x" * (1 << 20))
                except OSError:
                    return
        """,
        limits=SandboxLimits(timeout_seconds=60, memory_mb=256, pids=64, workspace_mb=64),
    )
    # The attack is contained; with the workspace full the run cannot report normally.
    assert classify(result).outcome is RunOutcome.RESOURCE_LIMIT_EXCEEDED, result.envelope
    after = os.statvfs(tmp_path).f_bavail
    block = os.statvfs(tmp_path).f_frsize
    assert (before - after) * block < 50 * 1024 * 1024  # host disk barely touched


def test_background_processes_do_not_survive(tmp_path: Path) -> None:
    result, name = run_tests(
        tmp_path,
        """
        import subprocess

        def test_daemon():
            subprocess.Popen(["sleep", "600"], start_new_session=True)
        """,
    )
    assert classify(result).outcome is RunOutcome.PASSED
    assert not container_exists(name)
    ps = subprocess.run(["pgrep", "-f", "sleep 600"], capture_output=True, check=False)
    assert ps.stdout.strip() == b""


def test_output_flood_cannot_overwhelm_host(tmp_path: Path) -> None:
    result, _ = run_tests(
        tmp_path,
        """
        def test_flood_runner_stdout():
            # Write straight into the runner's stdout, bypassing pytest's capture.
            with open("/proc/1/fd/1", "w") as out:
                for _ in range(400):
                    out.write("x" * 100_000 + "\\n")
        """,
        limits=SandboxLimits(
            timeout_seconds=60, memory_mb=256, pids=64, max_output_bytes=2 * 1024 * 1024
        ),
    )
    classification = classify(result)
    print("flood:", classification.outcome, result.output_truncated, result.duration_seconds)
    assert result.output_truncated
    assert classification.outcome is RunOutcome.INFRASTRUCTURE_ERROR


def test_failing_test_is_failed_not_passed(tmp_path: Path) -> None:
    result, _ = run_tests(tmp_path, "def test_bad():\n    assert 1 == 2\n")
    classification = classify(result)
    assert classification.outcome is RunOutcome.FAILED
    assert outcomes(result) == {"test_bad": TestOutcome.FAILED}


def test_collection_error_is_reported(tmp_path: Path) -> None:
    result, _ = run_tests(tmp_path, "import module_that_does_not_exist\n\ndef test_x(): pass\n")
    assert classify(result).outcome in (RunOutcome.COLLECTION_ERROR, RunOutcome.FAILED)
    assert classify(result).outcome is not RunOutcome.PASSED
