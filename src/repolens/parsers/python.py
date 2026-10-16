"""Python adapter built on Tree-sitter.

Extracts definitions (modules, classes, functions, methods, nested definitions), imports,
calls, base classes, decorators and selected name references, each with exact byte spans.
Tree-sitter's error recovery means a syntax error only affects the region around it; the
problems are reported and symbols that overlap them are flagged.

Qualified names follow Python's ``__qualname__`` convention: ``pkg.mod.Class.method`` and
``pkg.mod.outer.<locals>.inner``.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import version as dist_version
from pathlib import PurePosixPath

import tree_sitter as ts
import tree_sitter_python as tsp

from repolens.domain.enums import ReferenceKind, SymbolKind, Visibility
from repolens.domain.spans import Span
from repolens.parsers.base import (
    ExtractedSymbol,
    ImportDetail,
    ParsedFile,
    ParseProblem,
    RawReference,
)

# Bump when extraction logic changes in a way that alters output for the same input.
EXTRACTOR_REVISION = "1"
MAX_PROBLEMS = 50
MAX_TEXT = 200
DYNAMIC_CALLS = frozenset(
    {"getattr", "setattr", "delattr", "__import__", "eval", "exec", "import_module", "globals"}
)
_CODING_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


@dataclass(frozen=True, slots=True)
class _Scope:
    key: str
    qualname: str  # relative to the module ("" for the module itself)
    kind: SymbolKind
    class_is_test: bool = False


class PythonAdapter:
    language = "python"

    def __init__(self) -> None:
        self._parser = ts.Parser(ts.Language(tsp.language()))
        self.version = (
            f"python-ts{dist_version('tree-sitter')}-grammar{dist_version('tree-sitter-python')}"
            f"-r{EXTRACTOR_REVISION}"
        )

    def handles(self, path: str) -> bool:
        return path.endswith((".py", ".pyi"))

    def parse(self, path: str, module: str, source: bytes) -> ParsedFile:
        tree = self._parser.parse(source)
        return _Extractor(path, module, source, tree).run()


def decode_python_source(data: bytes) -> bytes | None:
    """Return UTF-8 bytes for ``data`` (honouring a PEP 263 cookie), or None if undecodable."""
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        pass
    for line in data.splitlines()[:2]:
        match = _CODING_COOKIE.match(line)
        if match:
            try:
                return data.decode(match.group(1).decode("ascii")).encode("utf-8")
            except (LookupError, UnicodeDecodeError):
                return None
    return None


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.suffix == ".py" and (
        pure.name.startswith("test_")
        or pure.name.endswith("_test.py")
        or pure.name == "conftest.py"
    )


def visibility_of(name: str) -> Visibility:
    if name.startswith("__") and name.endswith("__") and len(name) > 4:
        return Visibility.DUNDER
    if name.startswith("_"):
        return Visibility.PRIVATE
    return Visibility.PUBLIC


def _span(node: ts.Node) -> Span:
    return Span(
        node.start_point.row + 1,
        node.start_point.column,
        node.end_point.row + 1,
        node.end_point.column,
    )


def _text(src: bytes, node: ts.Node | None) -> str:
    # Slicing the source ourselves avoids allocating via ``Node.text`` for every node.
    # Note: py-tree-sitter 0.26.0 segfaulted deterministically in this extractor on CPython
    # 3.14 (3000-function file); 0.25.2 does not, hence the version pin in pyproject.toml.
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def dotted_target(src: bytes, node: ts.Node | None) -> str | None:
    """``a.b.c`` for identifier/attribute chains, ``super().m`` for super calls, else None."""
    parts: list[str] = []
    current = node
    while current is not None:
        if current.type == "identifier":
            parts.append(_text(src, current))
            return ".".join(reversed(parts))
        if current.type == "attribute":
            parts.append(_text(src, current.child_by_field_name("attribute")))
            current = current.child_by_field_name("object")
            continue
        if current.type == "call":
            function = current.child_by_field_name("function")
            if function is not None and _text(src, function) == "super":
                parts.append("super()")
                return ".".join(reversed(parts))
        return None
    return None


def _leaves(node: ts.Node) -> Iterator[ts.Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.child_count == 0:
            yield current
        else:
            stack.extend(reversed(current.children))


def _fingerprint(src: bytes, node: ts.Node) -> str:
    """Hash of the token stream: insensitive to formatting and comments."""
    digest = hashlib.sha256()
    for leaf in _leaves(node):
        if leaf.type != "comment":
            digest.update(src[leaf.start_byte : leaf.end_byte])
            digest.update(b"\x1f")
    return digest.hexdigest()[:32]


class _Extractor:
    def __init__(self, path: str, module: str, source: bytes, tree: ts.Tree) -> None:
        self.path = path
        self.module = module
        self.source = source
        self.tree = tree
        self.test_file = is_test_path(path)
        self.result = ParsedFile(path=path, module=module, language="python")
        self._ordinals: Counter[str] = Counter()
        self._problem_lines: list[tuple[int, int]] = []

    # -- entry point -----------------------------------------------------------------------

    def run(self) -> ParsedFile:
        root = self.tree.root_node
        self._collect_problems(root)
        module_key = f"{self.module}#MODULE"
        end = root.end_point
        module_span = Span(1, 0, max(end.row + 1, 1), end.column)
        self._add_symbol(
            ExtractedSymbol(
                key=module_key,
                qualified_name=self.module,
                name=self.module.rsplit(".", 1)[-1],
                kind=SymbolKind.MODULE,
                span=module_span,
                parent_key=None,
                visibility=visibility_of(self.module.rsplit(".", 1)[-1]),
                body_fingerprint=_fingerprint(self.source, root),
                doc_summary=self._docstring(root),
                has_parse_errors=bool(self._problem_lines),
            )
        )
        self._collect_module_names(root)
        self._walk(root, _Scope(module_key, "", SymbolKind.MODULE))
        return self.result

    # -- traversal -------------------------------------------------------------------------

    def _walk(self, start: ts.Node, scope: _Scope) -> None:
        stack: list[tuple[ts.Node, _Scope]] = [(c, scope) for c in reversed(start.children)]
        while stack:
            node, current = stack.pop()
            kind = node.type
            if kind == "decorated_definition":
                self._definition(node, current, stack)
                continue
            if kind in ("function_definition", "class_definition"):
                self._definition(node, current, stack)
                continue
            if kind in ("import_statement", "import_from_statement", "future_import_statement"):
                self._imports(node, current)
                continue
            if kind == "call":
                self._call(node, current)
            elif kind in ("argument_list", "return_statement", "assignment", "keyword_argument"):
                self._name_references(node, current)
            stack.extend((c, current) for c in reversed(node.children))

    def _definition(
        self, node: ts.Node, scope: _Scope, stack: list[tuple[ts.Node, _Scope]]
    ) -> None:
        decorators: list[ts.Node] = []
        definition = node
        if node.type == "decorated_definition":
            decorators = [c for c in node.children if c.type == "decorator"]
            inner = node.child_by_field_name("definition")
            if inner is None:
                return
            definition = inner
        name_node = definition.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(self.source, name_node)
        if definition.type == "class_definition":
            child = self._class(definition, node, name, scope, decorators)
        else:
            child = self._function(definition, node, name, scope, decorators)
        # A decorated definition depends on its decorators, so they are its references.
        for decorator in decorators:
            expression = next((c for c in decorator.children if c.is_named), None)
            target_node = expression
            if expression is not None and expression.type == "call":
                target_node = expression.child_by_field_name("function")
                arguments = expression.child_by_field_name("arguments")
                if arguments is not None:
                    stack.append((arguments, scope))  # e.g. @app.route("/", handler=h)
            target = dotted_target(self.source, target_node)
            self._reference(ReferenceKind.DECORATOR, child, decorator, target)
        body = definition.child_by_field_name("body")
        if body is not None:
            stack.append((body, child))
        if definition.type == "function_definition":
            # Defaults and annotations are evaluated in the enclosing scope.
            for field_name in ("return_type", "parameters"):
                part = definition.child_by_field_name(field_name)
                if part is not None:
                    stack.append((part, scope))

    def _qualname(self, scope: _Scope, name: str) -> str:
        if scope.kind is SymbolKind.MODULE:
            return name
        if scope.kind is SymbolKind.CLASS:
            return f"{scope.qualname}.{name}"
        return f"{scope.qualname}.<locals>.{name}"

    def _class(
        self,
        definition: ts.Node,
        outer: ts.Node,
        name: str,
        scope: _Scope,
        decorators: list[ts.Node],
    ) -> _Scope:
        qualname = self._qualname(scope, name)
        bases = definition.child_by_field_name("superclasses")
        base_texts: list[str] = []
        key = self._key(qualname, SymbolKind.CLASS)
        if bases is not None:
            for base in bases.named_children:
                if base.type == "keyword_argument":
                    continue
                base_texts.append(_text(self.source, base))
        is_test_class = self.test_file and (
            (name.startswith("Test") and scope.kind is SymbolKind.MODULE)
            or any(b.endswith("TestCase") for b in base_texts)
        )
        header_end = bases.end_byte if bases is not None else name_node_end(definition)
        signature = _short(self.source[definition.start_byte : header_end].decode(errors="replace"))
        self._add_symbol(
            ExtractedSymbol(
                key=key,
                qualified_name=f"{self.module}.{qualname}",
                name=name,
                kind=SymbolKind.CLASS,
                span=_span(outer),
                parent_key=scope.key,
                visibility=visibility_of(name),
                signature=signature,
                body_fingerprint=_fingerprint(self.source, outer),
                decorators=tuple(_short(_text(self.source, d))[1:] for d in decorators),
                doc_summary=self._docstring(definition.child_by_field_name("body")),
                is_test=is_test_class,
                has_parse_errors=self._overlaps_problem(outer),
            )
        )
        child = _Scope(key, qualname, SymbolKind.CLASS, class_is_test=is_test_class)
        if bases is not None:
            for base in bases.named_children:
                if base.type == "keyword_argument":
                    continue
                self._reference(
                    ReferenceKind.INHERIT, child, base, dotted_target(self.source, base)
                )
        return child

    def _function(
        self,
        definition: ts.Node,
        outer: ts.Node,
        name: str,
        scope: _Scope,
        decorators: list[ts.Node],
    ) -> _Scope:
        is_async = any(c.type == "async" for c in definition.children)
        in_class = scope.kind is SymbolKind.CLASS
        if in_class:
            kind = SymbolKind.ASYNC_METHOD if is_async else SymbolKind.METHOD
        else:
            kind = SymbolKind.ASYNC_FUNCTION if is_async else SymbolKind.FUNCTION
        qualname = self._qualname(scope, name)
        key = self._key(qualname, kind)
        header_end_node = definition.child_by_field_name(
            "return_type"
        ) or definition.child_by_field_name("parameters")
        header_end = header_end_node.end_byte if header_end_node else definition.end_byte
        signature = _short(self.source[definition.start_byte : header_end].decode(errors="replace"))
        is_test = (
            self.test_file
            and name.startswith("test")
            and (scope.kind is SymbolKind.MODULE or (in_class and scope.class_is_test))
        )
        self._add_symbol(
            ExtractedSymbol(
                key=key,
                qualified_name=f"{self.module}.{qualname}",
                name=name,
                kind=kind,
                span=_span(outer),
                parent_key=scope.key,
                visibility=visibility_of(name),
                signature=signature,
                body_fingerprint=_fingerprint(self.source, outer),
                decorators=tuple(_short(_text(self.source, d))[1:] for d in decorators),
                doc_summary=self._docstring(definition.child_by_field_name("body")),
                is_test=is_test,
                has_parse_errors=self._overlaps_problem(outer),
            )
        )
        return _Scope(key, qualname, kind)

    # -- references ------------------------------------------------------------------------

    def _imports(self, node: ts.Node, scope: _Scope) -> None:
        if node.type == "import_statement":
            for name in node.children_by_field_name("name"):
                module, alias = self._import_name(name)
                detail = ImportDetail(module=module, name=None, alias=alias)
                self._reference(ReferenceKind.IMPORT, scope, node, module, detail)
            return
        if node.type == "future_import_statement":
            for name in node.children_by_field_name("name"):
                imported, alias = self._import_name(name)
                detail = ImportDetail(module="__future__", name=imported, alias=alias)
                self._reference(ReferenceKind.IMPORT, scope, node, "__future__", detail)
            return
        module_node = node.child_by_field_name("module_name")
        level = 0
        source_module: str | None = None
        if module_node is not None and module_node.type == "relative_import":
            prefix = next((c for c in module_node.children if c.type == "import_prefix"), None)
            level = len(_text(self.source, prefix).strip())
            dotted = next((c for c in module_node.children if c.type == "dotted_name"), None)
            source_module = _text(self.source, dotted) or None
        elif module_node is not None:
            source_module = _text(self.source, module_node)
        if any(c.type == "wildcard_import" for c in node.children):
            detail = ImportDetail(
                module=source_module, name=None, alias=None, level=level, is_star=True
            )
            self._reference(ReferenceKind.IMPORT, scope, node, source_module, detail)
            return
        for name in node.children_by_field_name("name"):
            imported, alias = self._import_name(name)
            detail = ImportDetail(module=source_module, name=imported, alias=alias, level=level)
            target = f"{source_module}.{imported}" if source_module else imported
            self._reference(ReferenceKind.IMPORT, scope, node, target, detail)

    def _import_name(self, node: ts.Node) -> tuple[str, str | None]:
        if node.type == "aliased_import":
            name = _text(self.source, node.child_by_field_name("name"))
            alias = _text(self.source, node.child_by_field_name("alias"))
            return name, alias or None
        return _text(self.source, node), None

    def _call(self, node: ts.Node, scope: _Scope) -> None:
        function = node.child_by_field_name("function")
        target = dotted_target(self.source, function)
        last = target.rsplit(".", 1)[-1] if target else None
        dynamic = target is None or last in DYNAMIC_CALLS
        self._reference(ReferenceKind.CALL, scope, node, target, dynamic=dynamic)

    def _name_references(self, node: ts.Node, scope: _Scope) -> None:
        """Function/class objects passed around (callbacks, registries, returned values)."""
        if node.type == "argument_list":
            candidates = [c for c in node.named_children if c.type in ("identifier", "attribute")]
        elif node.type == "keyword_argument":
            value = node.child_by_field_name("value")
            candidates = [value] if value is not None else []
        elif node.type == "assignment":
            right = node.child_by_field_name("right")
            candidates = [right] if right is not None else []
        else:
            candidates = list(node.named_children)
        for candidate in candidates:
            if candidate.type in ("identifier", "attribute"):
                target = dotted_target(self.source, candidate)
                if target:
                    self._reference(ReferenceKind.NAME, scope, candidate, target)

    def _reference(
        self,
        kind: ReferenceKind,
        scope: _Scope,
        node: ts.Node,
        target: str | None,
        detail: ImportDetail | None = None,
        *,
        dynamic: bool = False,
    ) -> None:
        self.result.references.append(
            RawReference(
                kind=kind,
                source_key=scope.key,
                text=_short(_text(self.source, node)),
                span=_span(node),
                target=target,
                import_detail=detail,
                dynamic=dynamic,
            )
        )

    # -- helpers ---------------------------------------------------------------------------

    def _key(self, qualname: str, kind: SymbolKind) -> str:
        base = f"{self.module}.{qualname}#{kind.value}"
        self._ordinals[base] += 1
        ordinal = self._ordinals[base]
        return base if ordinal == 1 else f"{base}~{ordinal}"

    def _add_symbol(self, symbol: ExtractedSymbol) -> None:
        self.result.symbols.append(symbol)

    def _docstring(self, body: ts.Node | None) -> str | None:
        if body is None:
            return None
        first = next((c for c in body.named_children if c.type != "comment"), None)
        if first is None or first.type != "expression_statement":
            return None
        string = first.named_children[0] if first.named_children else None
        if string is None or string.type != "string":
            return None
        content = "".join(
            _text(self.source, c) for c in string.named_children if c.type == "string_content"
        )
        for line in content.strip().splitlines():
            if line.strip():
                return _short(line.strip())
        return None

    def _collect_problems(self, root: ts.Node) -> None:
        if not root.has_error:
            return
        stack = [root]
        while stack and len(self.result.problems) < MAX_PROBLEMS:
            node = stack.pop()
            if node.is_error or node.is_missing:
                message = f"missing {node.type}" if node.is_missing else "syntax error"
                self.result.problems.append(ParseProblem(_span(node), message))
                self._problem_lines.append((node.start_point.row + 1, node.end_point.row + 1))
                continue
            if node.has_error:
                stack.extend(reversed(node.children))

    def _overlaps_problem(self, node: ts.Node) -> bool:
        first, last = node.start_point.row + 1, node.end_point.row + 1
        return any(a <= last and first <= b for a, b in self._problem_lines)

    def _collect_module_names(self, root: ts.Node) -> None:
        for statement in root.named_children:
            if statement.type != "expression_statement" or not statement.named_children:
                continue
            assignment = statement.named_children[0]
            if assignment.type != "assignment":
                continue
            left = assignment.child_by_field_name("left")
            if left is None:
                continue
            names = (
                [left]
                if left.type == "identifier"
                else [c for c in left.named_children if c.type == "identifier"]
            )
            for name in names:
                self.result.module_assignments.append(_text(self.source, name))
                if _text(self.source, name) == "__all__":
                    self.result.exported_names = _string_list(
                        self.source, assignment.child_by_field_name("right")
                    )


def name_node_end(definition: ts.Node) -> int:
    name = definition.child_by_field_name("name")
    return name.end_byte if name is not None else definition.end_byte


def _string_list(src: bytes, node: ts.Node | None) -> list[str] | None:
    if node is None or node.type not in ("list", "tuple"):
        return None
    values: list[str] = []
    for item in node.named_children:
        if item.type != "string":
            return None
        values.append(
            "".join(_text(src, c) for c in item.named_children if c.type == "string_content")
        )
    return values
