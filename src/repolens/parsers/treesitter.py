"""Adapters for JavaScript, TypeScript/TSX, Go, Java and Rust, built on Tree-sitter.

One table-driven extractor covers all of them: definitions (classes and type declarations,
functions, methods) with spans, a leading doc comment, imports, calls and base classes. It
reuses the Python extractor's bookkeeping (keys, problems, references) so the output has
the same shape and goes through the same resolver, which resolves less for these languages
(see ``codegraph/resolver.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version as dist_version
from pathlib import PurePosixPath

import tree_sitter as ts
import tree_sitter_language_pack as pack

from repolens.domain.enums import ReferenceKind, SymbolKind, Visibility
from repolens.domain.spans import Span
from repolens.parsers.base import ExtractedSymbol, ImportDetail, ParsedFile
from repolens.parsers.python import _Extractor, _fingerprint, _Scope, _short, _span, _text

# Bump when extraction logic changes in a way that alters output for the same input.
EXTRACTOR_REVISION = "1"

_JS_DECISIONS = frozenset(
    {
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "catch_clause",
        "ternary_expression",
        "switch_case",  # ``default`` is a separate ``switch_default`` node
    }
)
_LOGICAL = frozenset({"&&", "||", "??"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "generator_function"})
# Member access node → (object field, member field), for dotted call targets.
_MEMBER = {
    "member_expression": ("object", "property"),
    "selector_expression": ("operand", "field"),
    "field_access": ("object", "field"),
    "field_expression": ("value", "field"),
    "scoped_identifier": ("path", "name"),
    "scoped_type_identifier": ("path", "name"),
}
_NAMES = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "field_identifier",
        "package_identifier",
        "this",
        "self",
        "super",
    }
)


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    language: str  # inventory language (repository/inventory.py)
    grammars: dict[str, str]  # file suffix → grammar name in the language pack
    classes: frozenset[str]
    functions: frozenset[str]  # a method when directly inside a class body
    calls: dict[str, str]  # call node → field holding the callee
    decisions: frozenset[str]


_JS_CLASSES = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration", "class"}
)
_JS_FUNCTIONS = frozenset(
    {"function_declaration", "generator_function_declaration", "method_definition"}
)
_JS_CALLS = {"call_expression": "function", "new_expression": "constructor"}
SPECS = (
    LanguageSpec(
        "javascript",
        {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript"},
        _JS_CLASSES,
        _JS_FUNCTIONS,
        _JS_CALLS,
        _JS_DECISIONS,
    ),
    LanguageSpec(
        "typescript",
        {".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx"},
        _JS_CLASSES,
        _JS_FUNCTIONS,
        _JS_CALLS,
        _JS_DECISIONS,
    ),
    LanguageSpec(
        "go",
        {".go": "go"},
        frozenset({"type_spec"}),
        frozenset({"function_declaration", "method_declaration"}),
        {"call_expression": "function"},
        frozenset(
            {"if_statement", "for_statement", "expression_case", "type_case", "communication_case"}
        ),
    ),
    LanguageSpec(
        "java",
        {".java": "java"},
        frozenset(
            {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "record_declaration",
            }
        ),
        frozenset({"method_declaration", "constructor_declaration"}),
        {"method_invocation": "name", "object_creation_expression": "type"},
        frozenset(
            {
                "if_statement",
                "for_statement",
                "enhanced_for_statement",
                "while_statement",
                "do_statement",
                "catch_clause",
                "ternary_expression",
                "switch_label",
            }
        ),
    ),
    LanguageSpec(
        "rust",
        {".rs": "rust"},
        frozenset({"struct_item", "enum_item", "trait_item", "union_item"}),
        frozenset({"function_item", "function_signature_item"}),
        {"call_expression": "function"},
        frozenset({"if_expression", "while_expression", "for_expression", "match_arm"}),
    ),
)
SUPPORTED_LANGUAGES = frozenset(s.language for s in SPECS)
PARSED_LANGUAGES = SUPPORTED_LANGUAGES | {"python"}  # every language with symbol extraction


class TreeSitterAdapter:
    def __init__(self, spec: LanguageSpec) -> None:
        self.spec = spec
        self.language = spec.language
        self._parsers: dict[str, ts.Parser] = {}  # one per grammar, created on first use
        self.version = (
            f"{spec.language}-ts{dist_version('tree-sitter')}"
            f"-pack{dist_version('tree-sitter-language-pack')}-r{EXTRACTOR_REVISION}"
        )

    def handles(self, path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in self.spec.grammars

    def parse(self, path: str, module: str, source: bytes) -> ParsedFile:
        grammar = self.spec.grammars[PurePosixPath(path).suffix.lower()]
        parser = self._parsers.get(grammar)
        if parser is None:
            parser = self._parsers[grammar] = ts.Parser(pack.get_language(grammar))  # type: ignore[arg-type]
        return _GenericExtractor(self.spec, path, module, source, parser.parse(source)).run()


def adapters() -> list[TreeSitterAdapter]:
    return [TreeSitterAdapter(spec) for spec in SPECS]


class _GenericExtractor(_Extractor):
    def __init__(
        self, spec: LanguageSpec, path: str, module: str, source: bytes, tree: ts.Tree
    ) -> None:
        super().__init__(path, module, source, tree)
        self.spec = spec
        self.lang = spec.language
        self.test_file = False  # test discovery is Python-only
        self.result.language = spec.language

    def run(self) -> ParsedFile:
        root = self.tree.root_node
        self._collect_problems(root)
        module_key = f"{self.module}#MODULE"
        end = root.end_point
        self._add_symbol(
            ExtractedSymbol(
                key=module_key,
                qualified_name=self.module,
                name=PurePosixPath(self.path).name,
                kind=SymbolKind.MODULE,
                span=Span(1, 0, max(end.row + 1, 1), end.column),
                parent_key=None,
                visibility=Visibility.PUBLIC,
                body_fingerprint=_fingerprint(self.source, root),
                has_parse_errors=bool(self._problem_lines),
            )
        )
        self._walk(root, _Scope(module_key, "", SymbolKind.MODULE))
        return self.result

    def _walk(self, start: ts.Node, scope: _Scope) -> None:
        stack: list[tuple[ts.Node, _Scope]] = [(c, scope) for c in reversed(start.children)]
        while stack:
            node, current = stack.pop()
            kind = node.type
            if self._is_definition(node):
                child = self._define(node, current)
                if child is not None:
                    stack.extend((c, child) for c in reversed(node.children))
                    continue
            elif kind == "impl_item":  # Rust: methods of the implemented type
                type_name = _base_type(self.source, node.child_by_field_name("type"))
                body = node.child_by_field_name("body")
                if type_name and body is not None:
                    impl = _Scope(current.key, type_name, SymbolKind.CLASS)
                    stack.extend((c, impl) for c in reversed(body.children))
                    continue
            elif kind in ("import_statement", "import_declaration", "use_declaration"):
                self._imports(node, current)
                continue
            elif kind == "export_statement" and node.child_by_field_name("source") is not None:
                self._imports(node, current)  # ``export { a } from "./b"`` re-exports
                continue
            elif kind in self.spec.calls:
                self._generic_call(node, current)
            stack.extend((c, current) for c in reversed(node.children))

    def _is_definition(self, node: ts.Node) -> bool:
        kind = node.type
        if kind == "variable_declarator":  # ``const f = () => …``
            value = node.child_by_field_name("value")
            name = node.child_by_field_name("name")
            return (
                value is not None
                and value.type in _FUNCTION_VALUES
                and name is not None
                and name.type == "identifier"
            )
        if kind == "type_spec":  # Go: only struct and interface types
            type_node = node.child_by_field_name("type")
            return type_node is not None and type_node.type in ("struct_type", "interface_type")
        if kind == "class":  # JS class expression: only when named
            return node.child_by_field_name("name") is not None
        return kind in self.spec.classes or kind in self.spec.functions

    def _define(self, node: ts.Node, scope: _Scope) -> _Scope | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _text(self.source, name_node)
        is_class = node.type in self.spec.classes
        qualname = self._qualname(scope, name)
        if is_class:
            kind = SymbolKind.CLASS
        elif node.type == "method_declaration" and self.lang == "go":
            receiver = _base_type(self.source, node.child_by_field_name("receiver"))
            kind, qualname = SymbolKind.METHOD, f"{receiver}.{name}" if receiver else name
        elif scope.kind is SymbolKind.CLASS:
            kind = SymbolKind.METHOD
        else:
            kind = SymbolKind.FUNCTION
        key = self._key(qualname, kind)
        outer = node
        while outer.parent is not None and outer.parent.type in (
            "export_statement",
            "lexical_declaration",
            "variable_declaration",
        ):
            outer = outer.parent
        if outer.parent is not None and outer.parent.type == "type_declaration":
            outer = outer.parent  # Go: the doc comment precedes ``type``
        body = node.child_by_field_name("body") or (
            node.child_by_field_name("value") if node.type == "variable_declarator" else None
        )
        if body is not None and body.type in _FUNCTION_VALUES:
            body = body.child_by_field_name("body")
        header_end = body.start_byte if body is not None else node.end_byte
        self._add_symbol(
            ExtractedSymbol(
                key=key,
                qualified_name=f"{self.module}.{qualname}",
                name=name,
                kind=kind,
                span=_span(node),
                parent_key=scope.key,
                visibility=self._visibility(node, name),
                signature=_short(_text_between(self.source, node.start_byte, header_end)),
                body_fingerprint=_fingerprint(self.source, node, name_node),
                doc_summary=_doc_comment(self.source, outer),
                has_parse_errors=self._overlaps_problem(node),
                complexity=None if is_class else self._complexity(body),
            )
        )
        child = _Scope(key, qualname, SymbolKind.CLASS if is_class else kind)
        if is_class:
            for base in self._bases(node):
                self._reference(ReferenceKind.INHERIT, child, base, _dotted(self.source, base))
        return child

    def _bases(self, node: ts.Node) -> list[ts.Node]:
        found: list[ts.Node] = []
        for part in node.named_children:
            if part.type == "class_heritage":  # JS: the expression; TS: extends/implements
                for clause in part.named_children:
                    if clause.type in ("extends_clause", "implements_clause"):
                        found.extend(c for c in clause.named_children if c.type != "type_arguments")
                    else:
                        found.append(clause)
            elif part.type == "superclass":
                found.extend(part.named_children)
            elif part.type == "super_interfaces":
                for type_list in part.named_children:
                    found.extend(type_list.named_children)
        return found

    def _visibility(self, node: ts.Node, name: str) -> Visibility:
        if self.lang == "go":
            return Visibility.PUBLIC if name[:1].isupper() else Visibility.PRIVATE
        if self.lang == "rust":
            has_pub = any(c.type == "visibility_modifier" for c in node.children)
            return Visibility.PUBLIC if has_pub else Visibility.PRIVATE
        modifiers = " ".join(
            _text(self.source, c)
            for c in node.children
            if c.type in ("modifiers", "accessibility_modifier")
        )
        if name.startswith(("_", "#")) or "private" in modifiers or "protected" in modifiers:
            return Visibility.PRIVATE
        return Visibility.PUBLIC

    def _imports(self, node: ts.Node, scope: _Scope) -> None:
        if node.type in ("import_statement", "export_statement"):
            details = self._js_imports(node)
        elif self.lang == "go":
            details = []
            for spec_node in _descendants(node, "import_spec"):
                path = _text(self.source, spec_node.child_by_field_name("path")).strip('"`')
                local = _text(self.source, spec_node.child_by_field_name("name")) or None
                details.append(ImportDetail(path, None, local))
        elif self.lang == "java":
            target = next((c for c in node.named_children if c.type != "asterisk"), None)
            dotted = _text(self.source, target)
            module, _, name = dotted.rpartition(".")
            details = (
                [ImportDetail(dotted, None, None, is_star=True)]
                if any(c.type == "asterisk" for c in node.children)
                else [ImportDetail(module or None, name, None)]
            )
        else:  # Rust ``use``: recorded, not bound or resolved
            argument = _text(self.source, node.child_by_field_name("argument"))
            details = [ImportDetail(argument.replace("::", "."), None, None)]
        for detail in details:
            dotted_name = ".".join(p for p in (detail.module, detail.name) if p) or None
            self._reference(ReferenceKind.IMPORT, scope, node, dotted_name, detail)

    def _js_imports(self, node: ts.Node) -> list[ImportDetail]:
        spec = _text(self.source, node.child_by_field_name("source")).strip("'\"`")
        details: list[ImportDetail] = []
        for part in node.named_children:
            if part.type == "export_clause":
                details.extend(self._specifiers(spec, part))
            if part.type != "import_clause":
                continue
            for item in part.named_children:
                if item.type == "identifier":
                    details.append(ImportDetail(spec, "default", _text(self.source, item)))
                elif item.type == "namespace_import":
                    alias = next((c for c in item.named_children), None)
                    details.append(ImportDetail(spec, None, _text(self.source, alias)))
                elif item.type == "named_imports":
                    details.extend(self._specifiers(spec, item))
        if not details:  # side-effect import or ``export * from``
            details.append(ImportDetail(spec, None, None, is_star=node.type != "import_statement"))
        return details

    def _specifiers(self, spec: str, node: ts.Node) -> list[ImportDetail]:
        found = []
        for item in node.named_children:
            name = _text(self.source, item.child_by_field_name("name")) or None
            alias = _text(self.source, item.child_by_field_name("alias")) or None
            if name:
                found.append(ImportDetail(spec, name, alias))
        return found

    def _generic_call(self, node: ts.Node, scope: _Scope) -> None:
        callee = node.child_by_field_name(self.spec.calls[node.type])
        target = _dotted(self.source, callee)
        if node.type == "method_invocation" and target is not None:  # Java: ``obj.name(…)``
            receiver = node.child_by_field_name("object")
            if receiver is not None:
                prefix = _dotted(self.source, receiver)
                target = f"{prefix}.{target}" if prefix else None
        self._reference(ReferenceKind.CALL, scope, node, target, dynamic=target is None)

    def _complexity(self, body: ts.Node | None) -> int | None:
        """McCabe complexity: 1 + decision points, excluding nested definitions."""
        if body is None:
            return None
        count = 1
        stack = [body]
        while stack:
            node = stack.pop()
            if node is not body and self._is_definition(node):
                continue
            kind = node.type
            if kind in self.spec.decisions:
                if not _is_default_branch(self.source, node):
                    count += 1
            elif kind == "binary_expression":
                operator = node.child_by_field_name("operator")
                if operator is not None and _text(self.source, operator) in _LOGICAL:
                    count += 1
            stack.extend(node.children)
        return count


def _is_default_branch(src: bytes, node: ts.Node) -> bool:
    """Java ``default:`` labels and Rust ``_ =>`` arms always match: no extra path."""
    if node.type == "switch_label":
        return _text(src, node).startswith("default")
    if node.type == "match_arm":
        pattern = node.child_by_field_name("pattern")
        return pattern is not None and _text(src, pattern).strip() == "_"
    return False


def _dotted(src: bytes, node: ts.Node | None) -> str | None:
    """``a.b.c`` for names and member chains (``a::b`` in Rust), else None."""
    parts: list[str] = []
    current = node
    while current is not None:
        if current.type in _NAMES:
            parts.append(_text(src, current))
            return ".".join(reversed(parts))
        if current.type == "generic_type":  # ``new Foo<T>()``
            current = current.named_children[0] if current.named_children else None
            continue
        fields = _MEMBER.get(current.type)
        if fields is None:
            return None
        member = current.child_by_field_name(fields[1])
        if member is None:
            return None
        parts.append(_text(src, member))
        current = current.child_by_field_name(fields[0])
    return None


def _base_type(src: bytes, node: ts.Node | None) -> str | None:
    """The type name in a Go receiver ``(s *Server[T])`` or a Rust ``impl … for Type``."""
    if node is None:
        return None
    if node.type == "type_identifier":
        return _text(src, node)
    names = _descendants(node, "type_identifier")
    return _text(src, names[0]) if names else None


def _descendants(node: ts.Node, kind: str) -> list[ts.Node]:
    found: list[ts.Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
        else:
            stack.extend(reversed(current.named_children))
    return found


def _doc_comment(src: bytes, node: ts.Node) -> str | None:
    """First line of the comment directly above a definition (JSDoc, Javadoc, ``//``, ``///``)."""
    comment = node.prev_named_sibling
    if comment is None or "comment" not in comment.type:
        return None
    if comment.end_point.row < node.start_point.row - 1:
        return None  # separated by a blank line: not this definition's comment
    lines = [comment]
    while True:  # consecutive ``//`` lines form one comment
        previous = lines[0].prev_named_sibling
        if previous is None or "comment" not in previous.type:
            break
        if previous.end_point.row != lines[0].start_point.row - 1:
            break
        lines.insert(0, previous)
    text = "\n".join(_text(src, c) for c in lines)
    for line in text.splitlines():
        cleaned = line.strip().lstrip("/*!").rstrip("*/").strip()
        if cleaned and not cleaned.startswith("@"):
            return _short(cleaned)
    return None


def _text_between(src: bytes, start: int, end: int) -> str:
    return src[start:end].decode("utf-8", errors="replace").strip()
