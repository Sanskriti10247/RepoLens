"""Turn a sandbox result into a test-run outcome and per-test results.

Rules (a run is only PASSED when the evidence for it is complete):
- cancelled → CANCELLED; wall-clock limit → TIMED_OUT; OOM kill → RESOURCE_LIMIT_EXCEEDED;
- no well-formed envelope (container died, output cut off) → INFRASTRUCTURE_ERROR, or
  RESOURCE_LIMIT_EXCEEDED when the container was killed by a limit;
- workspace copy failed (tmpfs full) → RESOURCE_LIMIT_EXCEEDED;
- pytest exit 0 → PASSED only if a JUnit report was produced and shows no failures or errors;
- pytest exit 1 → FAILED; exit 2/4/5 → COLLECTION_ERROR (interrupted, usage error, no tests);
  exit 3 → INFRASTRUCTURE_ERROR (pytest internal error); anything else → INFRASTRUCTURE_ERROR.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from repolens.execution.docker import SandboxResult


class RunOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    SECURITY_POLICY_BLOCKED = "SECURITY_POLICY_BLOCKED"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"


class TestOutcome(StrEnum):
    __test__ = False

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class TestResult:
    __test__ = False

    classname: str
    name: str
    outcome: TestOutcome
    duration_seconds: float
    message: str | None


@dataclass
class Classification:
    outcome: RunOutcome
    reason: str
    pytest_exit_code: int | None
    results: list[TestResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


_DECLARATION = re.compile(r"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
WORKSPACE_FULL_BYTES = 256 * 1024
_MAX_MESSAGE = 4000


class JUnitError(ValueError):
    pass


def parse_junit(text: str) -> list[TestResult]:
    if _DECLARATION.search(text):
        raise JUnitError("JUnit report contains a DTD or entity declaration")
    try:
        root = ET.fromstring(text)  # noqa: S314 - declarations rejected above; size capped
    except ET.ParseError as exc:
        raise JUnitError(f"malformed JUnit report: {exc}") from exc
    results: list[TestResult] = []
    for case in root.iter("testcase"):
        outcome, message = TestOutcome.PASSED, None
        for child in case:
            tag = child.tag
            if tag in ("failure", "error", "skipped"):
                outcome = {
                    "failure": TestOutcome.FAILED,
                    "error": TestOutcome.ERROR,
                    "skipped": TestOutcome.SKIPPED,
                }[tag]
                message = (child.get("message") or child.text or "")[:_MAX_MESSAGE] or None
                if outcome is not TestOutcome.SKIPPED:
                    break
        try:
            duration = float(case.get("time") or 0)
        except ValueError:
            duration = 0.0
        results.append(
            TestResult(
                case.get("classname") or "", case.get("name") or "", outcome, duration, message
            )
        )
    return results


def classify(result: SandboxResult) -> Classification:
    container = _container_outcome(result)
    if container is not None:
        return container
    assert result.envelope is not None  # noqa: S101 - checked by _container_outcome
    return _envelope_outcome(result.envelope)


def _container_outcome(result: SandboxResult) -> Classification | None:
    """Outcomes decided by what happened to the container, before reading its report."""
    if result.cancelled:
        return Classification(RunOutcome.CANCELLED, "execution was cancelled", None)
    if result.timed_out:
        return Classification(RunOutcome.TIMED_OUT, "wall-clock limit reached", None)
    if result.oom_killed:
        return Classification(RunOutcome.RESOURCE_LIMIT_EXCEEDED, "memory limit exceeded", None)
    if result.envelope is not None:
        return None
    if result.container_exit_code in (137, 139):
        reason = f"container killed (exit {result.container_exit_code}) before reporting"
        return Classification(RunOutcome.RESOURCE_LIMIT_EXCEEDED, reason, None)
    reason = "no result envelope from the sandbox runner"
    if result.output_truncated:
        reason = "no result envelope (output truncated)"
    return Classification(RunOutcome.INFRASTRUCTURE_ERROR, reason, None)


def _envelope_outcome(envelope: dict[str, Any]) -> Classification:
    if envelope.get("status") == "workspace_error":
        reason = f"workspace could not be prepared: {envelope.get('error', '')}"[:500]
        return Classification(RunOutcome.RESOURCE_LIMIT_EXCEEDED, reason, None)
    exit_code = envelope.get("pytest_exit_code")
    if not isinstance(exit_code, int):
        return Classification(RunOutcome.INFRASTRUCTURE_ERROR, "malformed envelope", None)
    free = envelope.get("workspace_free_bytes")
    workspace_full = isinstance(free, int) and free < WORKSPACE_FULL_BYTES
    results: list[TestResult] = []
    junit = envelope.get("junit")
    try:
        if isinstance(junit, str):
            results = parse_junit(junit)
        elif workspace_full and envelope.get("mode") != "collect":
            raise JUnitError("no test report")
    except JUnitError as exc:
        if workspace_full:
            reason = "workspace filled up; the test report could not be written"
            return Classification(RunOutcome.RESOURCE_LIMIT_EXCEEDED, reason, exit_code)
        return Classification(RunOutcome.INFRASTRUCTURE_ERROR, str(exc), exit_code)
    counts = _counts(results)
    outcome, reason = _from_exit_code(exit_code, envelope, counts)
    return Classification(outcome, reason, exit_code, results, counts)


def _counts(results: list[TestResult]) -> dict[str, int]:
    counts = {o.value.lower(): 0 for o in TestOutcome}
    for result in results:
        counts[result.outcome.value.lower()] += 1
    counts["total"] = len(results)
    return counts


def _from_exit_code(
    exit_code: int, envelope: dict[str, Any], counts: dict[str, int]
) -> tuple[RunOutcome, str]:
    if exit_code == 0:
        if envelope.get("mode") == "collect":
            return RunOutcome.PASSED, "collection completed"
        if envelope.get("junit") is None:
            return RunOutcome.INFRASTRUCTURE_ERROR, "pytest reported success without a report"
        if counts["failed"] or counts["error"]:
            return RunOutcome.FAILED, "report contains failures despite exit code 0"
        return RunOutcome.PASSED, f"{counts['passed']} passed, {counts['skipped']} skipped"
    if exit_code == 1:
        return RunOutcome.FAILED, f"{counts['failed']} failed, {counts['error']} errors"
    if exit_code in (2, 4, 5):
        meaning = {2: "interrupted", 4: "usage error", 5: "no tests collected"}[exit_code]
        return RunOutcome.COLLECTION_ERROR, f"pytest {meaning} (exit {exit_code})"
    if exit_code == 3:
        return RunOutcome.INFRASTRUCTURE_ERROR, "pytest internal error (exit 3)"
    if exit_code < 0:
        # pytest itself was killed by a signal (e.g. SIGKILL from the OOM killer).
        return RunOutcome.RESOURCE_LIMIT_EXCEEDED, f"pytest killed by signal {-exit_code}"
    return RunOutcome.INFRASTRUCTURE_ERROR, f"unexpected pytest exit code {exit_code}"


def node_id_key(node_id: str) -> tuple[str, str]:
    """The (classname, name) pair pytest's xunit2 report uses for a node id."""
    path, *parts = node_id.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    name = parts[-1] if parts else ""
    classname = ".".join([module, *parts[:-1]])
    return classname, re.sub(r"\[.*\]$", "", name)
