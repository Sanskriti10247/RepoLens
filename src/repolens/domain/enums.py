"""Enumerations shared across analysis, persistence and API layers.

Values are stored as text in PostgreSQL (with CHECK constraints) so adding a member is a
small, reviewable migration rather than an ``ALTER TYPE``.
"""

from __future__ import annotations

from enum import StrEnum


class SymbolKind(StrEnum):
    MODULE = "MODULE"
    CLASS = "CLASS"
    FUNCTION = "FUNCTION"
    ASYNC_FUNCTION = "ASYNC_FUNCTION"
    METHOD = "METHOD"
    ASYNC_METHOD = "ASYNC_METHOD"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"  # leading underscore
    DUNDER = "DUNDER"  # __name__ style special members


class ReferenceKind(StrEnum):
    IMPORT = "IMPORT"
    CALL = "CALL"
    INHERIT = "INHERIT"
    DECORATOR = "DECORATOR"
    ATTRIBUTE = "ATTRIBUTE"
    NAME = "NAME"


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"  # exactly one in-repository target
    AMBIGUOUS = "AMBIGUOUS"  # several plausible in-repository targets
    EXTERNAL = "EXTERNAL"  # resolves outside the repository (stdlib / third party)
    UNRESOLVED = "UNRESOLVED"  # no target found
    DYNAMIC = "DYNAMIC"  # target depends on runtime behaviour (getattr, dynamic import, ...)


class EdgeType(StrEnum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INHERITS = "INHERITS"
    REFERENCES = "REFERENCES"
    DEFINES_TEST = "DEFINES_TEST"
    TESTS_SYMBOL = "TESTS_SYMBOL"
    COVERED_BY_TEST = "COVERED_BY_TEST"


class Provenance(StrEnum):
    SYNTAX = "SYNTAX"  # directly observed in the syntax tree (e.g. containment)
    STATIC_RESOLVED = "STATIC_RESOLVED"  # produced by static name resolution
    COVERAGE = "COVERAGE"  # observed at runtime by coverage measurement
    EXPLICIT = "EXPLICIT"  # declared by configuration or markers
    HISTORICAL = "HISTORICAL"  # inferred from past execution history


class Confidence(StrEnum):
    EXACT = "EXACT"
    HEURISTIC = "HEURISTIC"


class FileRole(StrEnum):
    SOURCE = "SOURCE"
    TEST = "TEST"
    CONFIG = "CONFIG"
    DOC = "DOC"
    OTHER = "OTHER"


class FileMode(StrEnum):
    REGULAR = "REGULAR"
    EXECUTABLE = "EXECUTABLE"
    SYMLINK = "SYMLINK"
    SUBMODULE = "SUBMODULE"


class FileAnalysisStatus(StrEnum):
    PARSED = "PARSED"
    PARSED_WITH_ERRORS = "PARSED_WITH_ERRORS"
    SKIPPED_TOO_LARGE = "SKIPPED_TOO_LARGE"
    SKIPPED_BINARY = "SKIPPED_BINARY"
    SKIPPED_SYMLINK = "SKIPPED_SYMLINK"
    SKIPPED_UNSUPPORTED = "SKIPPED_UNSUPPORTED"
    SKIPPED_ENCODING = "SKIPPED_ENCODING"
    FAILED = "FAILED"


class SnapshotStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class AnalysisStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"  # completed, but some files could not be analysed
    REUSED = "REUSED"  # an equivalent completed run already existed
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class JobKind(StrEnum):
    ANALYZE_REPOSITORY = "ANALYZE_REPOSITORY"
    ANALYZE_CHANGE = "ANALYZE_CHANGE"
    RUN_TESTS = "RUN_TESTS"


class ChangeSetStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ChangeMappingStatus(StrEnum):
    """How completely a changed file could be mapped to symbols."""

    MAPPED = "MAPPED"
    MAPPED_WITH_PARSE_ERRORS = "MAPPED_WITH_PARSE_ERRORS"
    NOT_ANALYZED = "NOT_ANALYZED"  # parsed language, but skipped (too large, encoding, failure)
    NON_PYTHON = "NON_PYTHON"  # configuration, documentation, languages without a parser
    BINARY = "BINARY"
    SPECIAL = "SPECIAL"  # symlinks and submodules


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"
