"""Deterministic static name resolution for Python.

Input: parsed files (symbols plus raw references). Output: every reference annotated with a
resolution status and, where possible, its target symbol, plus typed dependency edges.

What is modelled:
- module namespaces built from definitions, imports (absolute, relative, aliased, star) and
  module-level assignments; function-local imports and nested definitions;
- re-exports (``from .impl import f`` in ``__init__``) followed through chains, cycle-safe;
- ``self.m`` / ``cls.m`` / ``super().m`` through an approximate MRO of in-repository bases;
- calls to classes (constructors) linked to ``__init__``;
- virtual dispatch: ``self.m()`` also links, heuristically, to overrides in known subclasses.

What is deliberately *not* modelled (reported as UNRESOLVED or DYNAMIC instead of guessed):
instance attributes, values returned by calls, ``getattr``/``importlib``/``eval``, monkey
patching, dependency injection and anything that depends on runtime state.
"""

from __future__ import annotations

import builtins
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repolens.codegraph.modules import ModuleName
from repolens.domain.enums import (
    Confidence,
    EdgeType,
    Provenance,
    ReferenceKind,
    ResolutionStatus,
    SymbolKind,
)
from repolens.parsers.base import ExtractedSymbol, ImportDetail, ParsedFile, RawReference

BUILTINS = frozenset(dir(builtins))
STDLIB = frozenset(sys.stdlib_module_names)
MAX_CHAIN = 25
MAX_AMBIGUOUS_EDGES = 5
_CALLABLE_KINDS = frozenset(
    {
        SymbolKind.CLASS,
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.METHOD,
        SymbolKind.ASYNC_METHOD,
    }
)
_FUNCTION_KINDS = _CALLABLE_KINDS - {SymbolKind.CLASS}
_EDGE_FOR_REF = {
    ReferenceKind.IMPORT: EdgeType.IMPORTS,
    ReferenceKind.CALL: EdgeType.CALLS,
    ReferenceKind.INHERIT: EdgeType.INHERITS,
    ReferenceKind.DECORATOR: EdgeType.REFERENCES,
    ReferenceKind.NAME: EdgeType.REFERENCES,
    ReferenceKind.ATTRIBUTE: EdgeType.REFERENCES,
}


# -- results -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolutionStatus
    rule: str
    target: str | None = None  # symbol key when RESOLVED
    candidates: tuple[str, ...] = ()  # symbol keys when AMBIGUOUS
    external: str | None = None  # dotted name when EXTERNAL
    confidence: Confidence = Confidence.EXACT


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    path: str
    ref: RawReference
    resolution: Resolution


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    edge_type: EdgeType
    provenance: Provenance
    confidence: Confidence
    reference_index: int | None = None  # index into ResolutionResult.references
    rule: str | None = None


@dataclass
class ResolutionResult:
    references: list[ResolvedReference] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    dropped_name_references: int = 0
    shadowed_paths: list[str] = field(default_factory=list)

    def status_counts(self) -> dict[str, int]:
        counts = Counter(r.resolution.status.value for r in self.references)
        return dict(sorted(counts.items()))


# -- internal values -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Sym:
    key: str


@dataclass(frozen=True, slots=True)
class _Mod:
    name: str  # in-repository module name


@dataclass(frozen=True, slots=True)
class _Ext:
    dotted: str


@dataclass(frozen=True, slots=True)
class _Var:
    name: str


@dataclass(frozen=True, slots=True)
class _Lazy:
    """An import binding, resolved on first use."""

    module_file: str  # path of the module that contains the import
    detail: ImportDetail


@dataclass(frozen=True, slots=True)
class _Fail:
    status: ResolutionStatus
    rule: str
    candidates: tuple[str, ...] = ()


_Value = _Sym | _Mod | _Ext | _Var | _Lazy
_Outcome = _Sym | _Mod | _Ext | _Var | _Fail


@dataclass
class _Module:
    name: str
    path: str
    is_package: bool
    parsed: ParsedFile
    bindings: dict[str, _Value] = field(default_factory=dict)
    star_imports: list[ImportDetail] = field(default_factory=list)


# -- resolver --------------------------------------------------------------------------------


class Resolver:
    def __init__(self, parsed: list[ParsedFile], modules: dict[str, ModuleName]) -> None:
        self.files = sorted(parsed, key=lambda p: p.path)
        self.modules: dict[str, _Module] = {}
        self.symbols: dict[str, ExtractedSymbol] = {}
        self.symbol_file: dict[str, str] = {}
        self.module_symbol_key: dict[str, str] = {}
        self.children: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.local_bindings: dict[str, dict[str, _Value]] = defaultdict(dict)
        self.bases: dict[str, list[str]] = {}
        self.subclasses: dict[str, list[str]] = defaultdict(list)
        self.path_index: dict[str, str] = {}  # file path → module name
        self.declared_bases: Counter[str] = Counter()
        self.external_bases: dict[str, list[str]] = defaultdict(list)
        self._weak = False  # set when a heuristic step was used for the current lookup
        self._depth = 0  # recursion guard for import/re-export chains
        for parsed_file in self.files:
            info = modules.get(parsed_file.path)
            if info is None:
                continue
            self._index_file(parsed_file, info)

    # -- indexing --------------------------------------------------------------------------

    def _index_file(self, parsed: ParsedFile, info: ModuleName) -> None:
        module = _Module(info.name, info.path, info.is_package, parsed)
        self.modules[info.name] = module
        self.path_index[info.path] = info.name
        for symbol in parsed.symbols:
            self.symbols[symbol.key] = symbol
            self.symbol_file[symbol.key] = parsed.path
            if symbol.kind is SymbolKind.MODULE:
                self.module_symbol_key[info.name] = symbol.key
            elif symbol.parent_key is not None:
                self.children[symbol.parent_key][symbol.name].append(symbol.key)
        self._index_bindings(module)

    def _index_bindings(self, module: _Module) -> None:
        """Bind names in source order: later definitions and imports win, as at runtime.

        Module-level assignments carry no position, so they only fill otherwise unbound names.
        """
        parsed = module.parsed
        module_key = self.module_symbol_key.get(module.name)
        tables: dict[str, dict[str, tuple[int, _Value]]] = defaultdict(dict)

        def bind(scope: str, name: str, line: int, value: _Value) -> None:
            table = tables[scope]
            if name not in table or table[name][0] <= line:
                table[name] = (line, value)

        for symbol in parsed.symbols:
            parent = self.symbols.get(symbol.parent_key) if symbol.parent_key else None
            if parent is not None and (
                parent.kind is SymbolKind.MODULE or parent.kind in _FUNCTION_KINDS
            ):
                bind(parent.key, symbol.name, symbol.span.start_line, _Sym(symbol.key))
        for ref in parsed.references:
            if ref.kind is ReferenceKind.INHERIT:
                self.declared_bases[ref.source_key] += 1
            elif ref.kind is ReferenceKind.IMPORT and ref.import_detail is not None:
                detail = ref.import_detail
                if detail.is_star:
                    if ref.source_key == module_key:
                        module.star_imports.append(detail)
                elif detail.bound_name is not None:
                    value = _Lazy(parsed.path, _binding_detail(detail))
                    bind(ref.source_key, detail.bound_name, ref.span.start_line, value)
        for name in parsed.module_assignments:
            module.bindings[name] = _Var(name)
        for scope, table in tables.items():
            target = module.bindings if scope == module_key else self.local_bindings[scope]
            target.update({name: value for name, (_, value) in table.items()})

    # -- public entry point ----------------------------------------------------------------

    def resolve(self) -> ResolutionResult:
        result = ResolutionResult()
        pending: list[tuple[str, RawReference]] = []
        # Pass 1: inheritance, so member lookups can follow base classes afterwards.
        for parsed in self.files:
            if parsed.path not in self.path_index:
                continue
            for ref in parsed.references:
                if ref.kind is ReferenceKind.INHERIT:
                    resolution = self._resolve_reference(parsed.path, ref)
                    self._record(result, parsed.path, ref, resolution)
                    if resolution.status is ResolutionStatus.RESOLVED and resolution.target:
                        self.bases.setdefault(ref.source_key, []).append(resolution.target)
                        self.subclasses[resolution.target].append(ref.source_key)
                    elif resolution.status is ResolutionStatus.EXTERNAL and resolution.external:
                        self.external_bases[ref.source_key].append(resolution.external)
                else:
                    pending.append((parsed.path, ref))
        for path, ref in pending:
            resolution = self._resolve_reference(path, ref)
            if ref.kind is ReferenceKind.NAME and not self._keep_name_reference(resolution):
                result.dropped_name_references += 1
                continue
            self._record(result, path, ref, resolution)
        self._containment_edges(result)
        return result

    # -- per reference ---------------------------------------------------------------------

    def _resolve_reference(self, path: str, ref: RawReference) -> Resolution:
        self._weak = False
        self._depth = 0
        if ref.kind is ReferenceKind.IMPORT and ref.import_detail is not None:
            outcome = self._resolve_import(path, ref.import_detail)
            return self._to_resolution(outcome, "import")
        if ref.dynamic:
            return Resolution(ResolutionStatus.DYNAMIC, "dynamic_target", external=ref.target)
        if not ref.target:
            return Resolution(ResolutionStatus.UNRESOLVED, "no_static_target")
        parts = ref.target.split(".")
        head, rest = parts[0], parts[1:]
        if head in ("self", "cls") and rest:
            return self._resolve_self(ref, rest)
        if head == "super()" and rest:
            return self._resolve_super(ref, rest)
        start_scope = self._lookup_scope(ref)
        value = self._lookup_name(path, start_scope, head)
        if value is None:
            if head in BUILTINS:
                return Resolution(
                    ResolutionStatus.EXTERNAL, "builtin", external=f"builtins.{ref.target}"
                )
            return Resolution(ResolutionStatus.UNRESOLVED, "name_not_found")
        outcome = self._walk(self._force(value), rest)
        return self._to_resolution(outcome, "scope_lookup")

    def _resolve_self(self, ref: RawReference, rest: list[str]) -> Resolution:
        cls = self._enclosing_class(ref.source_key)
        if cls is None:
            return Resolution(ResolutionStatus.UNRESOLVED, "self_outside_class")
        found = self._member(cls, rest[0], include_self=True)
        if not found:
            return Resolution(ResolutionStatus.UNRESOLVED, self._missing_member_rule(cls, "self"))
        outcome = self._walk(_Sym(found[0]), rest[1:])
        resolution = self._to_resolution(outcome, "self_member")
        # Overridable: the runtime target may be a subclass override.
        return _with_confidence(resolution, Confidence.HEURISTIC)

    def _resolve_super(self, ref: RawReference, rest: list[str]) -> Resolution:
        cls = self._enclosing_class(ref.source_key)
        if cls is None:
            return Resolution(ResolutionStatus.UNRESOLVED, "super_outside_class")
        found = self._member(cls, rest[0], include_self=False)
        if not found:
            return Resolution(ResolutionStatus.UNRESOLVED, self._missing_member_rule(cls, "super"))
        outcome = self._walk(_Sym(found[0]), rest[1:])
        return _with_confidence(self._to_resolution(outcome, "super_member"), Confidence.HEURISTIC)

    def _resolve_import(self, path: str, detail: ImportDetail) -> _Outcome:
        if detail.module == "__future__":
            return _Ext(f"__future__.{detail.name}")
        module_name = self._absolute_module(path, detail)
        if isinstance(module_name, _Fail):
            return module_name
        if detail.level > 0 and module_name not in self.modules:
            # Relative imports always target this repository; never report them external.
            parent = module_name.rpartition(".")[0]
            if detail.name is None or parent not in self.modules:
                return _Fail(ResolutionStatus.UNRESOLVED, "relative_module_not_found")
        if detail.name is None:  # ``import a.b`` or star import
            return self._module_value(module_name)
        base = self._module_value(module_name)
        if isinstance(base, _Mod):
            # ``from pkg import name`` loads submodule ``pkg.name`` unless the package itself
            # defines ``name``; a binding that is merely an import (often this very statement,
            # e.g. ``from . import leaf`` in ``__init__``) does not count as a definition.
            submodule = f"{base.name}.{detail.name}"
            binding = self.modules[base.name].bindings.get(detail.name)
            if submodule in self.modules and (binding is None or isinstance(binding, _Lazy)):
                return _Mod(submodule)
            return self._lookup_in_module(base.name, [detail.name], set())
        if isinstance(base, _Ext):
            return _Ext(f"{base.dotted}.{detail.name}")
        return base

    # -- lookups -----------------------------------------------------------------------------

    def _absolute_module(self, path: str, detail: ImportDetail) -> str | _Fail:
        if detail.level == 0:
            return detail.module or ""
        current = self.modules[self.path_index[path]]
        package = current.name if current.is_package else current.name.rpartition(".")[0]
        parts = package.split(".") if package else []
        drop = detail.level - 1
        if drop >= len(parts):  # e.g. ``from ... import x`` two levels below the top package
            return _Fail(ResolutionStatus.UNRESOLVED, "relative_import_beyond_root")
        base_parts = parts[: len(parts) - drop] if drop else parts
        if detail.module:
            base_parts = [*base_parts, *detail.module.split(".")]
        if not base_parts:
            return _Fail(ResolutionStatus.UNRESOLVED, "relative_import_beyond_root")
        return ".".join(base_parts)

    def _module_value(self, name: str) -> _Mod | _Ext | _Fail:
        if name in self.modules:
            return _Mod(name)
        top = name.split(".", maxsplit=1)[0]
        if any(m == top or m.startswith(f"{top}.") for m in self.modules):
            # The package exists in this repository but this module does not.
            matched = self._path_suffix_match(name)
            if matched is not None:
                return matched
            return _Fail(ResolutionStatus.UNRESOLVED, "module_not_found")
        if top not in STDLIB:
            matched = self._path_suffix_match(name)
            if matched is not None:
                return matched
        return _Ext(name)

    def _path_suffix_match(self, name: str) -> _Mod | _Fail | None:
        """Namespace packages / unusual roots: match ``a/b.py`` or ``a/b/__init__.py``."""
        tail = name.replace(".", "/")
        wanted = (f"{tail}.py", f"{tail}/__init__.py", f"{tail}.pyi")
        hits = sorted(
            module
            for path, module in self.path_index.items()
            if any(path == w or path.endswith(f"/{w}") for w in wanted)
        )
        if not hits:
            return None
        if len(hits) > 1:
            keys = tuple(self.module_symbol_key[m] for m in hits if m in self.module_symbol_key)
            return _Fail(ResolutionStatus.AMBIGUOUS, "module_path_ambiguous", keys)
        self._weak = True
        return _Mod(hits[0])

    def _lookup_scope(self, ref: RawReference) -> str:
        """Scope in which the reference's expression is evaluated."""
        source = self.symbols.get(ref.source_key)
        if source is None:
            return ref.source_key
        if ref.kind in (ReferenceKind.DECORATOR, ReferenceKind.INHERIT):
            return source.parent_key or source.key
        return source.key

    def _lookup_name(self, path: str, scope_key: str, name: str) -> _Value | None:
        module = self.modules[self.path_index[path]]
        current: str | None = scope_key
        first = True
        while current is not None:
            symbol = self.symbols.get(current)
            if symbol is None or symbol.kind is SymbolKind.MODULE:
                break
            if symbol.kind is SymbolKind.CLASS:
                # Class bodies are visible only to code directly in the class body.
                if first and name in self.children[symbol.key]:
                    return _Sym(self.children[symbol.key][name][-1])
            elif name in self.local_bindings.get(symbol.key, {}):
                return self.local_bindings[symbol.key][name]
            first = False
            current = symbol.parent_key
        if name in module.bindings:
            return module.bindings[name]
        for star in module.star_imports:
            star_module = self._absolute_module(path, star)
            if isinstance(star_module, str) and star_module in self.modules:
                found = self._lookup_in_module(star_module, [name], set())
                if not isinstance(found, _Fail):
                    self._weak = True
                    return found
        return None

    def _force(self, value: _Value) -> _Outcome:
        if not isinstance(value, _Lazy):
            return value
        if self._depth > MAX_CHAIN:
            return _Fail(ResolutionStatus.UNRESOLVED, "import_cycle")
        self._depth += 1
        try:
            return self._resolve_import(value.module_file, value.detail)
        finally:
            self._depth -= 1

    def _lookup_in_module(self, module_name: str, attrs: list[str], visited: set[str]) -> _Outcome:
        if not attrs:
            return _Mod(module_name)
        if self._depth > MAX_CHAIN:
            return _Fail(ResolutionStatus.UNRESOLVED, "import_cycle")
        self._depth += 1
        try:
            return self._lookup_in_module_inner(module_name, attrs, visited | {module_name})
        finally:
            self._depth -= 1

    def _lookup_in_module_inner(
        self, module_name: str, attrs: list[str], visited: set[str]
    ) -> _Outcome:
        module = self.modules.get(module_name)
        if module is None:
            return _Ext(".".join([module_name, *attrs]))
        head, rest = attrs[0], attrs[1:]
        value = module.bindings.get(head)
        if value is not None:
            forced = self._force(value)
            if isinstance(forced, _Mod) and forced.name in visited:
                return _Fail(ResolutionStatus.UNRESOLVED, "import_cycle")
            return self._walk(forced, rest, visited)
        submodule = f"{module_name}.{head}"
        if submodule in self.modules:
            return self._lookup_in_module(submodule, rest, visited)
        for star in module.star_imports:
            star_module = self._absolute_module(module.path, star)
            if isinstance(star_module, str) and star_module in self.modules:
                found = self._lookup_in_module(star_module, attrs, visited)
                if not isinstance(found, _Fail):
                    self._weak = True
                    return found
        return _Fail(ResolutionStatus.UNRESOLVED, "name_not_in_module")

    def _walk(self, value: _Outcome, attrs: list[str], visited: set[str] | None = None) -> _Outcome:
        visited = visited or set()
        for index, attr in enumerate(attrs):
            if isinstance(value, _Fail):
                return value
            if isinstance(value, _Ext):
                return _Ext(".".join([value.dotted, *attrs[index:]]))
            if isinstance(value, _Var):
                return _Fail(ResolutionStatus.UNRESOLVED, "bound_to_variable")
            if isinstance(value, _Mod):
                return self._lookup_in_module(value.name, attrs[index:], visited)
            symbol = self.symbols[value.key]
            if symbol.kind is not SymbolKind.CLASS:
                return _Fail(ResolutionStatus.UNRESOLVED, "attribute_of_function")
            members = self._member(symbol.key, attr, include_self=True)
            if not members:
                return _Fail(ResolutionStatus.UNRESOLVED, "class_attribute_not_found")
            value = _Sym(members[0])
        if isinstance(value, _Var):
            return _Fail(ResolutionStatus.UNRESOLVED, "bound_to_variable")
        return value

    def _member(self, class_key: str, name: str, *, include_self: bool) -> list[str]:
        """Find ``name`` along an approximate MRO (depth-first, left to right)."""
        order = self._mro(class_key)
        if not include_self:
            order = order[1:]
        for cls in order:
            if cls != class_key:
                self._weak = True
            found = self.children[cls].get(name)
            if found:
                return found
        return []

    def _mro(self, class_key: str) -> list[str]:
        order: list[str] = []
        stack = [class_key]
        while stack and len(order) < 100:
            current = stack.pop()
            if current in order:
                continue
            order.append(current)
            stack.extend(reversed(self.bases.get(current, [])))
        return order

    def _missing_member_rule(self, class_key: str, prefix: str) -> str:
        """Explain why a member was not found: possibly inherited from code we cannot see."""
        for cls in self._mro(class_key):
            if self.external_bases.get(cls):
                return f"{prefix}_member_maybe_inherited_external"
            if (
                len(self.bases.get(cls, [])) + len(self.external_bases.get(cls, []))
                < (self.declared_bases[cls])
            ):
                return f"{prefix}_member_maybe_in_unresolved_base"
        return f"{prefix}_attribute_not_found"

    def _enclosing_class(self, key: str) -> str | None:
        """For code in a method (or nested in one), the class that owns the method."""
        current = self.symbols.get(key)
        while current is not None and current.parent_key is not None:
            parent = self.symbols.get(current.parent_key)
            if parent is None:
                return None
            if parent.kind is SymbolKind.CLASS and current.kind in (
                SymbolKind.METHOD,
                SymbolKind.ASYNC_METHOD,
            ):
                return parent.key
            current = parent
        return None

    # -- conversion and edges ----------------------------------------------------------------

    def _to_resolution(self, outcome: _Outcome, rule: str) -> Resolution:
        confidence = Confidence.HEURISTIC if self._weak else Confidence.EXACT
        if isinstance(outcome, _Fail):
            return Resolution(outcome.status, outcome.rule, candidates=outcome.candidates)
        if isinstance(outcome, _Ext):
            return Resolution(ResolutionStatus.EXTERNAL, rule, external=outcome.dotted)
        if isinstance(outcome, _Var):
            return Resolution(ResolutionStatus.UNRESOLVED, "bound_to_variable")
        if isinstance(outcome, _Mod):
            key = self.module_symbol_key.get(outcome.name)
            if key is None:
                return Resolution(ResolutionStatus.UNRESOLVED, "module_without_symbol")
            return Resolution(ResolutionStatus.RESOLVED, rule, target=key, confidence=confidence)
        return Resolution(
            ResolutionStatus.RESOLVED, rule, target=outcome.key, confidence=confidence
        )

    def _keep_name_reference(self, resolution: Resolution) -> bool:
        """Name references are kept only when they point at an in-repo callable or class."""
        if resolution.status is ResolutionStatus.RESOLVED and resolution.target:
            return self.symbols[resolution.target].kind in _CALLABLE_KINDS
        return resolution.status is ResolutionStatus.AMBIGUOUS

    def _record(
        self, result: ResolutionResult, path: str, ref: RawReference, resolution: Resolution
    ) -> None:
        index = len(result.references)
        result.references.append(ResolvedReference(path, ref, resolution))
        edge_type = _EDGE_FOR_REF[ref.kind]
        provenance = Provenance.STATIC_RESOLVED
        if resolution.status is ResolutionStatus.RESOLVED and resolution.target:
            result.edges.append(
                Edge(
                    ref.source_key,
                    resolution.target,
                    edge_type,
                    provenance,
                    resolution.confidence,
                    index,
                    resolution.rule,
                )
            )
            target = self.symbols[resolution.target]
            if ref.kind is ReferenceKind.CALL and target.kind is SymbolKind.CLASS:
                for init in self._member(target.key, "__init__", include_self=True)[:1]:
                    result.edges.append(
                        Edge(
                            ref.source_key,
                            init,
                            EdgeType.CALLS,
                            provenance,
                            Confidence.HEURISTIC,
                            index,
                            "constructor",
                        )
                    )
            if resolution.rule == "self_member" and target.kind in _FUNCTION_KINDS:
                for override in self._overrides(target):
                    result.edges.append(
                        Edge(
                            ref.source_key,
                            override,
                            edge_type,
                            provenance,
                            Confidence.HEURISTIC,
                            index,
                            "virtual_dispatch",
                        )
                    )
        elif resolution.status is ResolutionStatus.AMBIGUOUS:
            for candidate in resolution.candidates[:MAX_AMBIGUOUS_EDGES]:
                result.edges.append(
                    Edge(
                        ref.source_key,
                        candidate,
                        edge_type,
                        provenance,
                        Confidence.HEURISTIC,
                        index,
                        "ambiguous_candidate",
                    )
                )

    def _overrides(self, method: ExtractedSymbol) -> list[str]:
        owner = method.parent_key
        if owner is None:
            return []
        found: list[str] = []
        stack = list(self.subclasses.get(owner, []))
        seen: set[str] = set()
        while stack:
            cls = stack.pop()
            if cls in seen:
                continue
            seen.add(cls)
            found.extend(self.children[cls].get(method.name, [])[:1])
            stack.extend(self.subclasses.get(cls, []))
        return sorted(found)

    def _containment_edges(self, result: ResolutionResult) -> None:
        for key in sorted(self.symbols):
            symbol = self.symbols[key]
            if symbol.parent_key is not None and symbol.parent_key in self.symbols:
                result.edges.append(
                    Edge(
                        symbol.parent_key,
                        key,
                        EdgeType.CONTAINS,
                        Provenance.SYNTAX,
                        Confidence.EXACT,
                    )
                )


def _binding_detail(detail: ImportDetail) -> ImportDetail:
    """What a name bound by an import refers to: ``import a.b`` binds ``a`` to package ``a``."""
    if detail.name is None and detail.alias is None and detail.module:
        return ImportDetail(detail.module.split(".", maxsplit=1)[0], None, None)
    return detail


def _with_confidence(resolution: Resolution, confidence: Confidence) -> Resolution:
    if resolution.status is not ResolutionStatus.RESOLVED:
        return resolution
    return Resolution(
        resolution.status,
        resolution.rule,
        resolution.target,
        resolution.candidates,
        resolution.external,
        confidence,
    )


def resolve_files(parsed: list[ParsedFile], modules: dict[str, ModuleName]) -> ResolutionResult:
    return Resolver(parsed, modules).resolve()


def is_init_module(path: str) -> bool:
    return PurePosixPath(path).stem == "__init__"
