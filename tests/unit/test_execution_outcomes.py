"""Outcome classification, JUnit parsing, execution policy and docker command construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repolens.execution.docker import DockerSandbox, SandboxRequest, SandboxResult, parse_envelope
from repolens.execution.outcomes import (
    JUnitError,
    RunOutcome,
    TestOutcome,
    classify,
    node_id_key,
    parse_junit,
)
from repolens.execution.policy import ExecutionPolicy, PolicyViolation, SandboxLimits

JUNIT_OK = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
    '<testcase classname="tests.test_a" name="test_one" time="0.01"/>'
    '<testcase classname="tests.test_a.TestK" name="test_two[1]" time="0.02">'
    '<skipped message="nope"/></testcase></testsuite></testsuites>'
)
JUNIT_FAIL = (
    "<testsuites><testsuite>"
    '<testcase classname="tests.test_a" name="test_one" time="0.5">'
    '<failure message="assert 1 == 2">trace</failure></testcase>'
    '<testcase classname="tests.test_a" name="test_err"><error message="boom"/></testcase>'
    "</testsuite></testsuites>"
)


def result(envelope: dict[str, Any] | None, **overrides: Any) -> SandboxResult:
    values: dict[str, Any] = {
        "envelope": envelope,
        "container_exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "oom_killed": False,
        "output_truncated": False,
        "stderr_tail": "",
        "duration_seconds": 1.0,
        "cleanup_ok": True,
    }
    values.update(overrides)
    return SandboxResult(**values)


def env(**fields: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "run",
        "status": "completed",
        "workspace_free_bytes": 10**8,
        **fields,
    }


@pytest.mark.parametrize(
    ("sandbox", "expected"),
    [
        (result(env(pytest_exit_code=0, junit=JUNIT_OK)), RunOutcome.PASSED),
        (result(env(pytest_exit_code=1, junit=JUNIT_FAIL)), RunOutcome.FAILED),
        (result(env(pytest_exit_code=0, junit=JUNIT_FAIL)), RunOutcome.FAILED),
        (result(env(pytest_exit_code=0, junit=None)), RunOutcome.INFRASTRUCTURE_ERROR),
        (result(env(pytest_exit_code=5, junit=JUNIT_OK)), RunOutcome.COLLECTION_ERROR),
        (result(env(pytest_exit_code=4)), RunOutcome.COLLECTION_ERROR),
        (result(env(pytest_exit_code=3, junit=JUNIT_OK)), RunOutcome.INFRASTRUCTURE_ERROR),
        (result(env(pytest_exit_code=-9, junit=None)), RunOutcome.RESOURCE_LIMIT_EXCEEDED),
        (
            result(env(pytest_exit_code=1, junit="", workspace_free_bytes=0)),
            RunOutcome.RESOURCE_LIMIT_EXCEEDED,
        ),
        (
            result(env(status="workspace_error", error="No space")),
            RunOutcome.RESOURCE_LIMIT_EXCEEDED,
        ),
        (result(env(pytest_exit_code="0")), RunOutcome.INFRASTRUCTURE_ERROR),
        (result(None), RunOutcome.INFRASTRUCTURE_ERROR),
        (result(None, container_exit_code=137), RunOutcome.RESOURCE_LIMIT_EXCEEDED),
        (result(None, output_truncated=True), RunOutcome.INFRASTRUCTURE_ERROR),
        (result(env(pytest_exit_code=0, junit=JUNIT_OK), timed_out=True), RunOutcome.TIMED_OUT),
        (result(env(pytest_exit_code=0, junit=JUNIT_OK), cancelled=True), RunOutcome.CANCELLED),
        (result(None, oom_killed=True), RunOutcome.RESOURCE_LIMIT_EXCEEDED),
        (result(env(mode="collect", pytest_exit_code=0, collected=[])), RunOutcome.PASSED),
    ],
)
def test_classification(sandbox: SandboxResult, expected: RunOutcome) -> None:
    assert classify(sandbox).outcome is expected


def test_worker_crash_or_missing_report_is_never_passed() -> None:
    for broken in (result(None), result(None, container_exit_code=0), result({"junk": True})):
        assert classify(broken).outcome is not RunOutcome.PASSED


def test_junit_parsing() -> None:
    passed = parse_junit(JUNIT_OK)
    assert [(r.name, r.outcome) for r in passed] == [
        ("test_one", TestOutcome.PASSED),
        ("test_two[1]", TestOutcome.SKIPPED),
    ]
    failed = parse_junit(JUNIT_FAIL)
    assert [(r.outcome, r.message) for r in failed] == [
        (TestOutcome.FAILED, "assert 1 == 2"),
        (TestOutcome.ERROR, "boom"),
    ]
    counts = classify(result(env(pytest_exit_code=1, junit=JUNIT_FAIL))).counts
    assert counts == {"passed": 0, "failed": 1, "error": 1, "skipped": 0, "total": 2}


@pytest.mark.parametrize(
    "payload",
    [
        '<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><testsuites>&b;</testsuites>',
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><t>&e;</t>',
        "<testsuites><testcase",
    ],
)
def test_hostile_or_malformed_junit_is_rejected(payload: str) -> None:
    with pytest.raises(JUnitError):
        parse_junit(payload)


def test_envelope_parsing_takes_the_last_marker() -> None:
    stdout = b'noise\nREPOLENS-RESULT {"a": 1}\nmore\nREPOLENS-RESULT {"a": 2}\n'
    assert parse_envelope(stdout) == {"a": 2}
    assert parse_envelope(b"REPOLENS-RESULT {not json}\n") is None
    assert parse_envelope(b"REPOLENS-RESULT [1]\n") is None
    assert parse_envelope(b"nothing here") is None


@pytest.mark.parametrize(
    ("node_id", "key"),
    [
        ("tests/test_a.py::test_one", ("tests.test_a", "test_one")),
        ("tests/test_a.py::TestK::test_two[1-x]", ("tests.test_a.TestK", "test_two")),
        ("test_root.py::test_x", ("test_root", "test_x")),
    ],
)
def test_node_id_key(node_id: str, key: tuple[str, str]) -> None:
    assert node_id_key(node_id) == key


POLICY = ExecutionPolicy(approved_images=frozenset({"repolens-sandbox:test"}))


@pytest.mark.parametrize(
    "node_id",
    [
        "tests/test_a.py",
        "tests/test_a.py::test_one",
        "tests/test_a.py::TestK::test_two",
        "tests/test_a.py::test_p[1-2]",
        "src/pkg/test_x.py::test_ünï"[:-4] + "x",
    ],
)
def test_valid_node_ids(node_id: str) -> None:
    POLICY.check_node_ids([node_id])


@pytest.mark.parametrize(
    "node_id",
    [
        "-p evil",
        "--rootdir=/",
        "tests/test_a.py::test one",
        "../outside.py::test_x",
        "/abs/test.py::test",
        "tests/test_a.txt",
        "tests/test_a.py::test_p[\n]",
        "tests/test_a.py;rm -rf /",
    ],
)
def test_invalid_node_ids(node_id: str) -> None:
    with pytest.raises(PolicyViolation):
        POLICY.check_node_ids([node_id])


def test_image_and_limit_policies() -> None:
    POLICY.check_image("repolens-sandbox:test")
    with pytest.raises(PolicyViolation):
        POLICY.check_image("ubuntu:latest")
    POLICY.check_limits(SandboxLimits())
    with pytest.raises(PolicyViolation):
        POLICY.check_limits(SandboxLimits(memory_mb=10**6))
    with pytest.raises(PolicyViolation):
        POLICY.check_limits(SandboxLimits(timeout_seconds=0))


def test_docker_command_contains_every_hardening_flag() -> None:
    command = DockerSandbox().run_command(
        SandboxRequest(
            name="repolens-x",
            image="img:1",
            source_dir=Path("/data/ws/src"),
            arguments=["run", "--", "tests/test_a.py::test_one"],
            limits=SandboxLimits(memory_mb=512, pids=99, cpus=1.5),
            labels={"repolens.run": "abc"},
        )
    )
    joined = " ".join(command)
    for flag in (
        "--network none",
        "--user 10001:10001",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--read-only",
        "--pids-limit 99",
        "--memory 512m",
        "--memory-swap 512m",
        "--cpus 1.5",
        "--log-driver none",
        "--init",
        "type=bind,source=/data/ws/src,target=/src,readonly",
    ):
        assert flag in joined, flag
    assert "--privileged" not in command and "/var/run/docker.sock" not in joined
    assert "-e" not in command and "--env" not in command  # no environment passthrough
    assert command[-3:] == ["run", "--", "tests/test_a.py::test_one"]
