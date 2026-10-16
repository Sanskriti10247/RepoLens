"""Exact-output tests for the Python parser adapter."""

from __future__ import annotations

import textwrap

import pytest

from repolens.domain.enums import ReferenceKind, SymbolKind, Visibility
from repolens.domain.spans import Span
from repolens.parsers.base import ImportDetail, ParsedFile
from repolens.parsers.python import PythonAdapter, decode_python_source, dotted_target

ADAPTER = PythonAdapter()


def parse(source: str, *, path: str = "pkg/mod.py", module: str = "pkg.mod") -> ParsedFile:
    return ADAPTER.parse(path, module, textwrap.dedent(source).lstrip("\n").encode())


def symbols(parsed: ParsedFile) -> dict[str, tuple[SymbolKind, Span]]:
    return {s.key: (s.kind, s.span) for s in parsed.symbols}


def refs(parsed: ParsedFile, kind: ReferenceKind) -> list[tuple[str, str | None]]:
    return [(r.source_key, r.target) for r in parsed.references if r.kind is kind]


def test_functions_classes_methods_and_nesting() -> None:
    parsed = parse(
        """
        def top(a, b=1):
            def inner():
                return a
            return inner

        class Shape:
            def area(self):
                return 0

            class Meta:
                pass

        async def fetch():
            pass
        """
    )
    assert symbols(parsed) == {
        "pkg.mod#MODULE": (SymbolKind.MODULE, Span(1, 0, 15, 0)),  # exclusive end
        "pkg.mod.top#FUNCTION": (SymbolKind.FUNCTION, Span(1, 0, 4, 16)),
        "pkg.mod.top.<locals>.inner#FUNCTION": (SymbolKind.FUNCTION, Span(2, 4, 3, 16)),
        "pkg.mod.Shape#CLASS": (SymbolKind.CLASS, Span(6, 0, 11, 12)),
        "pkg.mod.Shape.area#METHOD": (SymbolKind.METHOD, Span(7, 4, 8, 16)),
        "pkg.mod.Shape.Meta#CLASS": (SymbolKind.CLASS, Span(10, 4, 11, 12)),
        "pkg.mod.fetch#ASYNC_FUNCTION": (SymbolKind.ASYNC_FUNCTION, Span(13, 0, 14, 8)),
    }
    by_key = {s.key: s for s in parsed.symbols}
    assert by_key["pkg.mod.top.<locals>.inner#FUNCTION"].parent_key == "pkg.mod.top#FUNCTION"
    assert by_key["pkg.mod.Shape.Meta#CLASS"].parent_key == "pkg.mod.Shape#CLASS"
    assert by_key["pkg.mod.top#FUNCTION"].signature == "def top(a, b=1)"
    assert not parsed.has_errors


def test_async_methods_and_visibility() -> None:
    parsed = parse(
        """
        class Client:
            async def _send(self): ...
            def __init__(self): ...
            def __private(self): ...
        """
    )
    by_name = {s.name: s for s in parsed.symbols}
    assert by_name["_send"].kind is SymbolKind.ASYNC_METHOD
    assert by_name["_send"].visibility is Visibility.PRIVATE
    assert by_name["__init__"].visibility is Visibility.DUNDER
    assert by_name["__private"].visibility is Visibility.PRIVATE
    assert by_name["Client"].visibility is Visibility.PUBLIC


def test_multiline_signature_and_unicode_columns_are_bytes() -> None:
    parsed = parse(
        """
        def résumé(
            first: int,
            second: str = "é",
        ) -> dict[str, int]:
            return {}
        """
    )
    func = next(s for s in parsed.symbols if s.kind is SymbolKind.FUNCTION)
    assert func.name == "résumé"
    assert func.signature == 'def résumé( first: int, second: str = "é", ) -> dict[str, int]'
    assert func.span == Span(1, 0, 5, 13)


def test_decorators_are_recorded_and_included_in_span() -> None:
    parsed = parse(
        """
        import functools

        @functools.lru_cache(maxsize=None)
        @register
        def cached():
            pass
        """
    )
    func = next(s for s in parsed.symbols if s.name == "cached")
    assert func.decorators == ("functools.lru_cache(maxsize=None)", "register")
    assert func.span.start_line == 3  # starts at the first decorator
    assert refs(parsed, ReferenceKind.DECORATOR) == [
        ("pkg.mod.cached#FUNCTION", "functools.lru_cache"),
        ("pkg.mod.cached#FUNCTION", "register"),
    ]
    assert refs(parsed, ReferenceKind.CALL) == []  # the decorator call is not double counted


def test_import_forms() -> None:
    parsed = parse(
        """
        import os
        import a.b as c, d.e
        from . import sibling
        from ..pkg.mod import (y as z, w)
        from m import *
        from __future__ import annotations

        def local():
            import json
        """
    )
    details = [(r.source_key, r.import_detail) for r in parsed.references if r.import_detail]
    module = "pkg.mod#MODULE"
    assert details == [
        (module, ImportDetail("os", None, None)),
        (module, ImportDetail("a.b", None, "c")),
        (module, ImportDetail("d.e", None, None)),
        (module, ImportDetail(None, "sibling", None, level=1)),
        (module, ImportDetail("pkg.mod", "y", "z", level=2)),
        (module, ImportDetail("pkg.mod", "w", None, level=2)),
        (module, ImportDetail("m", None, None, is_star=True)),
        (module, ImportDetail("__future__", "annotations", None)),
        ("pkg.mod.local#FUNCTION", ImportDetail("json", None, None)),
    ]
    bound = [d.bound_name for _, d in details if d]
    assert bound == ["os", "c", "d", "sibling", "z", "w", None, "annotations", "json"]


def test_calls_inheritance_and_dynamic_dispatch() -> None:
    parsed = parse(
        """
        from base import Base
        import mod

        class Child(Base, mod.Mixin, metaclass=Meta):
            def run(self):
                self.helper()
                super().run()
                getattr(self, "x")()
                handlers["a"]()
                mod.util.go(1)
        """
    )
    assert refs(parsed, ReferenceKind.INHERIT) == [
        ("pkg.mod.Child#CLASS", "Base"),
        ("pkg.mod.Child#CLASS", "mod.Mixin"),
    ]
    calls = [(r.target, r.dynamic) for r in parsed.references if r.kind is ReferenceKind.CALL]
    assert calls == [
        ("self.helper", False),
        ("super().run", False),
        ("super", False),
        (None, True),  # getattr(self, "x")()
        ("getattr", True),
        (None, True),  # handlers["a"]()
        ("mod.util.go", False),
    ]
    assert all(
        r.source_key == "pkg.mod.Child.run#METHOD"
        for r in parsed.references
        if r.kind is ReferenceKind.CALL
    )


def test_name_references_capture_callbacks() -> None:
    parsed = parse(
        """
        from handlers import on_save

        def setup(app):
            app.connect("save", on_save)
            app.register(callback=on_load)
            return make_handler
        """
    )
    names = refs(parsed, ReferenceKind.NAME)
    assert ("pkg.mod.setup#FUNCTION", "on_save") in names
    assert ("pkg.mod.setup#FUNCTION", "on_load") in names
    assert ("pkg.mod.setup#FUNCTION", "make_handler") in names


def test_duplicate_definitions_get_ordinals() -> None:
    parsed = parse(
        """
        class Temp:
            @property
            def value(self):
                return 1

            @value.setter
            def value(self, v):
                pass

        if FAST:
            def impl(): ...
        else:
            def impl(): ...
        """
    )
    keys = [s.key for s in parsed.symbols]
    assert "pkg.mod.Temp.value#METHOD" in keys and "pkg.mod.Temp.value#METHOD~2" in keys
    assert "pkg.mod.impl#FUNCTION" in keys and "pkg.mod.impl#FUNCTION~2" in keys
    assert len(set(keys)) == len(keys)


def test_same_name_in_different_modules_is_distinct() -> None:
    a = parse("def helper(): ...\n", path="a.py", module="a")
    b = parse("def helper(): ...\n", path="b.py", module="b")
    assert {s.key for s in a.symbols} & {s.key for s in b.symbols} == set()


def test_syntax_errors_are_contained() -> None:
    parsed = parse(
        """
        def good():
            return 1

        def broken(:
            pass

        class AfterError:
            def method(self):
                return 2
        """
    )
    assert parsed.has_errors
    names = {s.name: s for s in parsed.symbols}
    assert {"good", "AfterError", "method"} <= set(names)
    assert not names["good"].has_parse_errors
    assert not names["AfterError"].has_parse_errors
    assert all(p.span.start_line in (4, 5) for p in parsed.problems)


def test_empty_file() -> None:
    parsed = parse("", path="pkg/__init__.py", module="pkg")
    assert [(s.key, s.span) for s in parsed.symbols] == [("pkg#MODULE", Span(1, 0, 1, 0))]
    assert parsed.references == [] and not parsed.has_errors


def test_large_file_is_parsed_completely() -> None:
    source = "\n".join(f"def f{i}(x):\n    return g{i}(x)\n" for i in range(3000))
    parsed = ADAPTER.parse("big.py", "big", source.encode())
    assert len(parsed.symbols) == 3001
    assert len([r for r in parsed.references if r.kind is ReferenceKind.CALL]) == 3000


def test_deep_nesting_does_not_recurse() -> None:
    depth = 120
    lines = [("    " * i) + f"def f{i}():" for i in range(depth)]
    lines.append(("    " * depth) + "pass")
    parsed = ADAPTER.parse("deep.py", "deep", "\n".join(lines).encode())
    assert len(parsed.symbols) == depth + 1


def test_docstrings_signatures_and_fingerprints() -> None:
    parsed = parse(
        '''
        class Repo:
            """Stores things.

            Longer description.
            """

            def get(self, key):  # comment
                """Return the value."""
                return self._data[key]
        '''
    )
    by_name = {s.name: s for s in parsed.symbols}
    assert by_name["Repo"].doc_summary == "Stores things."
    assert by_name["get"].doc_summary == "Return the value."
    assert by_name["Repo"].signature == "class Repo"

    reformatted = parse(
        '''
        class Repo:
            """Stores things.

            Longer description.
            """

            def get(self,   key):
                """Return the value."""
                return self._data[ key ]
        '''
    )
    other = {s.name: s for s in reformatted.symbols}
    # Formatting and comments do not change the fingerprint; code changes do.
    assert by_name["get"].body_fingerprint == other["get"].body_fingerprint
    changed = parse(
        """
        class Repo:
            def get(self, key):
                return self._data.get(key)
        """
    )
    assert {s.name: s for s in changed.symbols}["get"].body_fingerprint != by_name[
        "get"
    ].body_fingerprint


def test_test_declarations() -> None:
    parsed = parse(
        """
        import unittest

        def test_top(): ...
        def helper(): ...

        class TestThing:
            def test_method(self): ...
            def setup_method(self): ...

        class LegacyCase(unittest.TestCase):
            def test_legacy(self): ...
        """,
        path="tests/test_thing.py",
        module="test_thing",
    )
    tests = {s.name for s in parsed.symbols if s.is_test}
    assert tests == {"test_top", "TestThing", "test_method", "LegacyCase", "test_legacy"}


def test_test_names_outside_test_files_are_not_tests() -> None:
    parsed = parse("def test_like(): ...\n", path="pkg/util.py", module="pkg.util")
    assert not any(s.is_test for s in parsed.symbols)


def test_module_assignments_and_all() -> None:
    parsed = parse(
        """
        __all__ = ["public", "Other"]
        VALUE = 1
        a, b = 1, 2
        """
    )
    assert parsed.exported_names == ["public", "Other"]
    assert parsed.module_assignments == ["__all__", "VALUE", "a", "b"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"x = 1\n", b"x = 1\n"),
        (b"\xef\xbb\xbfx = 1\n", b"x = 1\n"),
        (
            b"# -*- coding: latin-1 -*-\nx = '\xe9'\n",
            "# -*- coding: latin-1 -*-\nx = 'é'\n".encode(),
        ),
        (b"x = '\xff\xfe'\n", None),
    ],
)
def test_decode_python_source(raw: bytes, expected: bytes | None) -> None:
    assert decode_python_source(raw) == expected


def test_dotted_target_of_none() -> None:
    assert dotted_target(b"", None) is None


def test_adapter_handles_python_paths_only() -> None:
    assert ADAPTER.handles("a/b.py") and ADAPTER.handles("stubs/x.pyi")
    assert not ADAPTER.handles("a/b.pyc") and not ADAPTER.handles("README.md")
    assert ADAPTER.version.startswith("python-ts")


def test_fingerprint_ignores_the_definition_name() -> None:
    first = parse("def compute(x):\n    return x + 1\n")
    renamed = parse("def calculate(x):\n    return x + 1\n")
    changed = parse("def compute(x):\n    return x + 2\n")

    def fingerprint(parsed: ParsedFile) -> str | None:
        return next(s for s in parsed.symbols if s.kind is SymbolKind.FUNCTION).body_fingerprint

    assert fingerprint(first) == fingerprint(renamed)
    assert fingerprint(first) != fingerprint(changed)
