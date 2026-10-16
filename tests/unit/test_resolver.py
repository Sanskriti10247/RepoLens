"""Static resolution over small multi-module repositories."""

from __future__ import annotations

import textwrap

import pytest

from repolens.codegraph.modules import dedupe_modules, module_names
from repolens.codegraph.resolver import ResolutionResult, resolve_files
from repolens.domain.enums import Confidence, EdgeType, ReferenceKind, ResolutionStatus
from repolens.parsers.python import PythonAdapter

ADAPTER = PythonAdapter()


def analyze(files: dict[str, str]) -> ResolutionResult:
    names, _ = dedupe_modules(module_names(list(files)))
    parsed = [
        ADAPTER.parse(path, names[path].name, textwrap.dedent(src).lstrip("\n").encode())
        for path, src in files.items()
        if path in names
    ]
    return resolve_files(parsed, names)


def outcome(result: ResolutionResult, source: str, target: str, kind: ReferenceKind | None = None):
    matches = [
        r
        for r in result.references
        if r.ref.source_key == source
        and r.ref.target == target
        and (kind is None or r.ref.kind is kind)
    ]
    assert matches, f"no reference {source} -> {target}"
    return matches[0].resolution


def edges(result: ResolutionResult, edge_type: EdgeType) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in result.edges if e.edge_type is edge_type}


# -- module naming -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["pkg/__init__.py", "pkg/mod.py"], {"pkg/__init__.py": "pkg", "pkg/mod.py": "pkg.mod"}),
        (
            ["src/pkg/__init__.py", "src/pkg/a/__init__.py", "src/pkg/a/b.py"],
            {
                "src/pkg/__init__.py": "pkg",
                "src/pkg/a/__init__.py": "pkg.a",
                "src/pkg/a/b.py": "pkg.a.b",
            },
        ),
        (["tests/test_x.py"], {"tests/test_x.py": "test_x"}),
        (
            ["tests/__init__.py", "tests/test_x.py"],
            {"tests/__init__.py": "tests", "tests/test_x.py": "tests.test_x"},
        ),
        (["setup.py", "my-script.py", "__init__.py"], {"setup.py": "setup"}),
        (["a/b/c.py"], {"a/b/c.py": "c"}),
    ],
)
def test_module_names(paths: list[str], expected: dict[str, str]) -> None:
    assert {p: m.name for p, m in module_names(paths).items()} == expected


def test_dedupe_prefers_source_over_stub() -> None:
    names = module_names(["pkg/__init__.py", "pkg/x.py", "pkg/x.pyi"])
    chosen, shadowed = dedupe_modules(names)
    assert "pkg/x.py" in chosen and shadowed == ["pkg/x.pyi"]


# -- imports -------------------------------------------------------------------------------

LIBRARY = {
    "shop/__init__.py": "from .pricing import total as compute_total\n",
    "shop/pricing.py": """
        from shop.tax import rate
        import shop.discounts as disc

        def total(items):
            return sum(items) * (1 + rate()) - disc.discount(items)
    """,
    "shop/tax.py": """
        def rate():
            return 0.2
    """,
    "shop/discounts.py": """
        def discount(items):
            return 0
    """,
    "app.py": """
        import os.path
        import shop
        from shop import compute_total
        from shop.missing_module import thing
        from shop.tax import not_there

        def main():
            os.path.join("a", "b")
            print(compute_total([1]))
            shop.compute_total([2])
            shop.pricing.total([3])
            undefined_name()
    """,
}


def test_import_resolution_statuses() -> None:
    result = analyze(LIBRARY)
    module = "app#MODULE"
    imports = {
        r.ref.text: r.resolution
        for r in result.references
        if r.ref.kind is ReferenceKind.IMPORT and r.ref.source_key == module
    }
    assert imports["import os.path"].status is ResolutionStatus.EXTERNAL
    assert imports["import os.path"].external == "os.path"
    assert imports["import shop"].target == "shop#MODULE"
    # Re-export: shop.compute_total is shop.pricing.total
    assert imports["from shop import compute_total"].target == "shop.pricing.total#FUNCTION"
    missing_module = imports["from shop.missing_module import thing"]
    assert (missing_module.status, missing_module.rule) == (
        ResolutionStatus.UNRESOLVED,
        "module_not_found",
    )
    missing_name = imports["from shop.tax import not_there"]
    assert (missing_name.status, missing_name.rule) == (
        ResolutionStatus.UNRESOLVED,
        "name_not_in_module",
    )


def test_call_resolution_through_imports_and_aliases() -> None:
    result = analyze(LIBRARY)
    main = "app.main#FUNCTION"
    assert outcome(result, main, "os.path.join").external == "os.path.join"
    assert outcome(result, main, "print").external == "builtins.print"
    assert outcome(result, main, "compute_total").target == "shop.pricing.total#FUNCTION"
    assert outcome(result, main, "shop.compute_total").target == "shop.pricing.total#FUNCTION"
    assert outcome(result, main, "shop.pricing.total").target == "shop.pricing.total#FUNCTION"
    assert outcome(result, main, "undefined_name").status is ResolutionStatus.UNRESOLVED
    total = "shop.pricing.total#FUNCTION"
    assert outcome(result, total, "rate").target == "shop.tax.rate#FUNCTION"
    assert outcome(result, total, "disc.discount").target == "shop.discounts.discount#FUNCTION"
    assert outcome(result, total, "sum").status is ResolutionStatus.EXTERNAL
    calls = edges(result, EdgeType.CALLS)
    assert (main, "shop.pricing.total#FUNCTION") in calls
    assert (total, "shop.tax.rate#FUNCTION") in calls


def test_relative_imports() -> None:
    result = analyze(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "from . import leaf\nfrom .leaf import f\n",
            "pkg/sub/leaf.py": "from .. import helpers\nfrom ...beyond import x\ndef f(): ...\n",
            "pkg/helpers.py": "def h(): ...\n",
        }
    )
    imports = {
        (r.ref.source_key, r.ref.text): r.resolution
        for r in result.references
        if r.ref.kind is ReferenceKind.IMPORT
    }
    assert imports[("pkg.sub#MODULE", "from . import leaf")].target == "pkg.sub.leaf#MODULE"
    assert imports[("pkg.sub#MODULE", "from .leaf import f")].target == "pkg.sub.leaf.f#FUNCTION"
    assert imports[("pkg.sub.leaf#MODULE", "from .. import helpers")].target == "pkg.helpers#MODULE"
    beyond = imports[("pkg.sub.leaf#MODULE", "from ...beyond import x")]
    assert beyond.status is ResolutionStatus.UNRESOLVED


def test_circular_imports_terminate() -> None:
    result = analyze(
        {
            "a.py": "from b import x\ndef use():\n    x()\n",
            "b.py": "from a import x\n",
        }
    )
    use = outcome(result, "a.use#FUNCTION", "x")
    assert use.status is ResolutionStatus.UNRESOLVED
    assert use.rule == "import_cycle"


def test_circular_imports_between_real_definitions_resolve() -> None:
    result = analyze(
        {
            "a.py": "import b\ndef ping():\n    b.pong()\n",
            "b.py": "import a\ndef pong():\n    a.ping()\n",
        }
    )
    assert (("a.ping#FUNCTION", "b.pong#FUNCTION")) in edges(result, EdgeType.CALLS)
    assert (("b.pong#FUNCTION", "a.ping#FUNCTION")) in edges(result, EdgeType.CALLS)


def test_later_definition_shadows_import() -> None:
    result = analyze(
        {
            "util.py": "def helper(): ...\n",
            "main.py": (
                "from util import helper\n\ndef helper():\n    pass\n\ndef run():\n    helper()\n"
            ),
        }
    )
    assert outcome(result, "main.run#FUNCTION", "helper").target == "main.helper#FUNCTION"


def test_local_imports_and_closures() -> None:
    result = analyze(
        {
            "tools.py": "def tool(): ...\n",
            "main.py": """
                def outer():
                    from tools import tool

                    def inner():
                        tool()
                        sibling()

                    def sibling(): ...
                    inner()
            """,
        }
    )
    inner = "main.outer.<locals>.inner#FUNCTION"
    assert outcome(result, inner, "tool").target == "tools.tool#FUNCTION"
    assert outcome(result, inner, "sibling").target == "main.outer.<locals>.sibling#FUNCTION"
    assert outcome(result, "main.outer#FUNCTION", "inner").target == inner


def test_star_imports_are_heuristic() -> None:
    result = analyze(
        {
            "lib.py": "def exported(): ...\n",
            "main.py": "from lib import *\n\ndef run():\n    exported()\n",
        }
    )
    resolution = outcome(result, "main.run#FUNCTION", "exported")
    assert resolution.target == "lib.exported#FUNCTION"
    assert resolution.confidence is Confidence.HEURISTIC


# -- classes -----------------------------------------------------------------------------

SHAPES = {
    "shapes/__init__.py": "",
    "shapes/base.py": """
        class Shape:
            def __init__(self, name):
                self.name = name

            def area(self):
                return 0

            def describe(self):
                return f"{self.name}: {self.area()}"
    """,
    "shapes/square.py": """
        import unittest
        from shapes.base import Shape
        from external_lib import Mixin

        class Square(Shape):
            def area(self):
                return self.side ** 2

            def describe(self):
                return super().describe() + "!"

        class Fancy(Square, Mixin):
            def show(self):
                self.describe()
                self.mixin_method()

        class Case(unittest.TestCase):
            def test_it(self):
                self.assertEqual(1, 1)

        def build():
            return Square("sq")
    """,
}


def test_inheritance_and_member_resolution() -> None:
    result = analyze(SHAPES)
    inherits = edges(result, EdgeType.INHERITS)
    assert ("shapes.square.Square#CLASS", "shapes.base.Shape#CLASS") in inherits
    assert ("shapes.square.Fancy#CLASS", "shapes.square.Square#CLASS") in inherits
    mixin = outcome(result, "shapes.square.Fancy#CLASS", "Mixin", ReferenceKind.INHERIT)
    assert mixin.status is ResolutionStatus.EXTERNAL

    describe = "shapes.base.Shape.describe#METHOD"
    self_area = outcome(result, describe, "self.area")
    assert self_area.target == "shapes.base.Shape.area#METHOD"
    assert self_area.confidence is Confidence.HEURISTIC  # may be overridden
    super_call = outcome(result, "shapes.square.Square.describe#METHOD", "super().describe")
    assert super_call.target == describe

    show = "shapes.square.Fancy.show#METHOD"
    assert outcome(result, show, "self.describe").target == ("shapes.square.Square.describe#METHOD")
    assert outcome(result, show, "self.mixin_method").rule == (
        "self_member_maybe_inherited_external"
    )
    assert outcome(result, "shapes.square.Case.test_it#METHOD", "self.assertEqual").rule == (
        "self_member_maybe_inherited_external"
    )


def test_virtual_dispatch_and_constructor_edges() -> None:
    result = analyze(SHAPES)
    dispatch = {
        (e.src, e.dst)
        for e in result.edges
        if e.rule == "virtual_dispatch" and e.confidence is Confidence.HEURISTIC
    }
    # Shape.describe calls self.area(); Square overrides area.
    assert ("shapes.base.Shape.describe#METHOD", "shapes.square.Square.area#METHOD") in dispatch
    constructor = {(e.src, e.dst) for e in result.edges if e.rule == "constructor"}
    assert ("shapes.square.build#FUNCTION", "shapes.base.Shape.__init__#METHOD") in constructor
    assert ("shapes.square.build#FUNCTION", "shapes.square.Square#CLASS") in edges(
        result, EdgeType.CALLS
    )


def test_containment_edges() -> None:
    result = analyze(SHAPES)
    contains = edges(result, EdgeType.CONTAINS)
    assert ("shapes.base#MODULE", "shapes.base.Shape#CLASS") in contains
    assert ("shapes.base.Shape#CLASS", "shapes.base.Shape.area#METHOD") in contains


# -- dynamic, names, decorators -----------------------------------------------------------


def test_dynamic_dispatch_is_reported_not_guessed() -> None:
    result = analyze(
        {
            "plugins.py": """
                import importlib

                HANDLERS = {}

                def load(name):
                    module = importlib.import_module(name)
                    getattr(module, "run")()
                    HANDLERS[name]()
                    HANDLERS.get(name)()
            """,
        }
    )
    load = "plugins.load#FUNCTION"
    statuses = [r.resolution.status for r in result.references if r.ref.source_key == load]
    assert statuses.count(ResolutionStatus.DYNAMIC) >= 3
    handlers_get = outcome(result, load, "HANDLERS.get")
    assert (handlers_get.status, handlers_get.rule) == (
        ResolutionStatus.UNRESOLVED,
        "bound_to_variable",
    )
    assert not any(e.edge_type is EdgeType.CALLS and e.src == load for e in result.edges)


def test_name_references_keep_only_callables() -> None:
    result = analyze(
        {
            "events.py": """
                def on_save(): ...

                def setup(bus, value):
                    bus.subscribe(on_save)
                    bus.publish(value)
            """,
        }
    )
    refs = {(e.src, e.dst) for e in result.edges if e.edge_type is EdgeType.REFERENCES}
    assert ("events.setup#FUNCTION", "events.on_save#FUNCTION") in refs
    assert result.dropped_name_references >= 1  # 'value' is a parameter, not a symbol


def test_decorators_create_reference_edges() -> None:
    result = analyze(
        {
            "registry.py": "def register(fn):\n    return fn\n",
            "views.py": "from registry import register\n\n@register\ndef index(): ...\n",
        }
    )
    assert ("views.index#FUNCTION", "registry.register#FUNCTION") in edges(
        result, EdgeType.REFERENCES
    )


def test_namespace_package_path_fallback() -> None:
    result = analyze(
        {
            "acme/core/engine.py": "def start(): ...\n",
            "acme/cli.py": "from acme.core.engine import start\n",
        }
    )
    imported = next(r for r in result.references if r.ref.kind is ReferenceKind.IMPORT)
    assert imported.resolution.target == "engine.start#FUNCTION"
    assert imported.resolution.confidence is Confidence.HEURISTIC


def test_stdlib_names_never_match_repository_modules() -> None:
    result = analyze(
        {
            "mypkg/__init__.py": "",
            "mypkg/json.py": "def loads(): ...\n",
            "main.py": "import json\n\ndef run():\n    json.loads('1')\n",
        }
    )
    assert outcome(result, "main.run#FUNCTION", "json.loads").external == "json.loads"


def test_resolution_is_deterministic() -> None:
    first = analyze(SHAPES)
    second = analyze(dict(reversed(list(SHAPES.items()))))

    def summary(result: ResolutionResult) -> list[tuple[str, str, str]]:
        return sorted((e.src, e.dst, e.edge_type.value) for e in result.edges)

    assert summary(first) == summary(second)
    assert first.status_counts() == second.status_counts()
