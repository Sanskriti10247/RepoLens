"""Execution policy: what may run, with which limits. Validated before any container starts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class PolicyViolation(ValueError):
    """The request violates execution policy (reported as SECURITY_POLICY_BLOCKED)."""


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    memory_mb: int = 1024
    cpus: float = 2.0
    pids: int = 256
    timeout_seconds: float = 600.0
    workspace_mb: int = 1024  # tmpfs holding the writable copy of the repository
    tmp_mb: int = 128
    max_output_bytes: int = 16 * 1024 * 1024
    open_files: int = 1024


# Hard ceilings: no request, configuration or API call may exceed these.
CEILING = SandboxLimits(
    memory_mb=8192,
    cpus=8.0,
    pids=4096,
    timeout_seconds=3600.0,
    workspace_mb=8192,
    tmp_mb=2048,
    max_output_bytes=64 * 1024 * 1024,
    open_files=65536,
)
MAX_NODE_IDS = 5000
# Repository-relative path (no leading '-' or '/', no '..'), then ::Class::name[param] parts.
_NODE_ID = re.compile(
    r"^(?![-/])(?!.*\.\.)[A-Za-z0-9_./-]+\.py(::[A-Za-z_][A-Za-z0-9_]*(\[[^\]\x00-\x1f]{0,200}\])?)*$"
)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    approved_images: frozenset[str]
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def check_image(self, image: str) -> None:
        if image not in self.approved_images:
            raise PolicyViolation(f"Image '{image}' is not an approved sandbox image")

    def check_limits(self, limits: SandboxLimits) -> None:
        for name in (
            "memory_mb",
            "cpus",
            "pids",
            "timeout_seconds",
            "workspace_mb",
            "tmp_mb",
            "max_output_bytes",
            "open_files",
        ):
            value, ceiling = getattr(limits, name), getattr(CEILING, name)
            if not 0 < value <= ceiling:
                raise PolicyViolation(f"Limit {name}={value} is outside (0, {ceiling}]")

    def check_node_ids(self, node_ids: list[str]) -> None:
        if len(node_ids) > MAX_NODE_IDS:
            raise PolicyViolation(f"{len(node_ids)} tests exceed the limit of {MAX_NODE_IDS}")
        for node_id in node_ids:
            if len(node_id) > 1000 or not _NODE_ID.match(node_id):
                raise PolicyViolation(f"Invalid test identifier: {node_id[:100]!r}")
