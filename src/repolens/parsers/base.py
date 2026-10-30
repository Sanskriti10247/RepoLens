"""Language-neutral parser output.

A parser adapter turns one file's bytes into symbols and *unresolved* references. Name
resolution across files is a separate step (``repolens.codegraph``), so parsers stay pure,
per-file and cacheable by content hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from repolens.domain.enums import ReferenceKind, SymbolKind, Visibility
from repolens.domain.spans import Span


@dataclass(frozen=True, slots=True)
class ExtractedSymbol:
    key: str  # unique within the module: "<qualified name>#<kind>[~<ordinal>]"
    qualified_name: str
    name: str
    kind: SymbolKind
    span: Span
    parent_key: str | None
    visibility: Visibility
    signature: str | None = None
    body_fingerprint: str | None = None
    decorators: tuple[str, ...] = ()
    doc_summary: str | None = None
    is_test: bool = False
    has_parse_errors: bool = False
    complexity: int | None = None  # cyclomatic complexity; functions and methods only


@dataclass(frozen=True, slots=True)
class ImportDetail:
    """``import a.b as c`` → module="a.b", name=None, alias="c", level=0.

    ``from ..pkg import x as y`` → module="pkg", name="x", alias="y", level=2.
    """

    module: str | None
    name: str | None
    alias: str | None
    level: int = 0
    is_star: bool = False

    @property
    def bound_name(self) -> str | None:
        """The local name this import binds (None for star imports)."""
        if self.is_star:
            return None
        if self.alias:
            return self.alias
        if self.name:
            return self.name
        return (self.module or "").split(".")[0] or None


@dataclass(frozen=True, slots=True)
class RawReference:
    kind: ReferenceKind
    source_key: str  # enclosing symbol (module, class or function)
    text: str  # source text, truncated
    span: Span
    target: str | None = None  # dotted name if statically expressible ("a.b.c", "self.m")
    import_detail: ImportDetail | None = None
    dynamic: bool = False  # getattr/importlib/eval-style indirection
    type_checking: bool = False  # an import inside ``if TYPE_CHECKING:`` (never runs)


@dataclass(frozen=True, slots=True)
class ParseProblem:
    span: Span
    message: str


@dataclass
class ParsedFile:
    path: str
    module: str
    language: str
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    references: list[RawReference] = field(default_factory=list)
    problems: list[ParseProblem] = field(default_factory=list)
    # Names bound at module level by assignment (e.g. ``handler = make_handler()``).
    module_assignments: list[str] = field(default_factory=list)
    # ``__all__`` if it is a literal list/tuple of strings.
    exported_names: list[str] | None = None

    @property
    def has_errors(self) -> bool:
        return bool(self.problems)


class LanguageAdapter(Protocol):
    language: str
    version: str  # changes whenever extraction output could change

    def handles(self, path: str) -> bool: ...

    def parse(self, path: str, module: str, source: bytes) -> ParsedFile: ...
