"""Cyclomatic complexity counted by the Python parser (definition in docs/insights.md)."""

from __future__ import annotations

import textwrap

import pytest

from repolens.parsers.python import PythonAdapter
from repolens.parsers.serialize import from_json, to_json

ADAPTER = PythonAdapter()


def complexities(source: str) -> dict[str, int | None]:
    parsed = ADAPTER.parse("pkg/mod.py", "pkg.mod", textwrap.dedent(source).lstrip("\n").encode())
    return {s.qualified_name.removeprefix("pkg.mod."): s.complexity for s in parsed.symbols}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("return 1", 1),
        ("if a:\n    pass", 2),
        ("if a:\n    pass\nelif b:\n    pass\nelse:\n    pass", 3),  # plain else adds nothing
        ("for x in y:\n    pass", 2),
        ("for x in y:\n    pass\nelse:\n    pass", 3),  # loop else is its own path
        ("while a:\n    pass\nelse:\n    pass", 3),
        ("try:\n    pass\nexcept A:\n    pass\nexcept (B, C):\n    pass", 3),
        ("try:\n    pass\nexcept A:\n    pass\nelse:\n    pass\nfinally:\n    pass", 3),
        ("try:\n    pass\nexcept* A:\n    pass", 2),
        ("return a and b", 2),
        ("return a and b or c", 3),  # each boolean operator
        ("return a if b else c", 2),
        ("return [x for x in y]", 2),
        ("return [x for x in y if x for z in x if z]", 5),
        ("return {k: v for k, v in d if v}", 3),
        ("with a:\n    pass", 1),
        ("assert a", 1),  # assert is not counted
        ("return lambda q: q if q else 0", 2),  # lambdas are not symbols: counted here
    ],
)
def test_decision_points(body: str, expected: int) -> None:
    source = "def f(a, b, c, d, y):\n" + textwrap.indent(body, "    ") + "\n"
    assert complexities(source)["f"] == expected


def test_match_counts_cases_but_not_the_wildcard() -> None:
    source = """
    def f(x):
        match x:
            case 1:
                pass
            case 2 | 3 if x > 0:
                pass
            case _:
                pass
    """
    # 1 + two refutable cases + one guard; ``case _`` always matches.
    assert complexities(source)["f"] == 4


def test_nested_definitions_are_measured_separately() -> None:
    source = """
    def outer(a):
        if a:
            pass

        def inner(b):
            if b and a:
                return 1
            return 2

        class Local:
            def method(self):
                for _ in a:
                    pass

        return inner

    class Shape:
        @property
        async def area(self):
            return 0 if self else 1
    """
    assert complexities(source) == {
        "pkg.mod": None,  # module
        "outer": 2,
        "outer.<locals>.inner": 3,
        "outer.<locals>.Local": None,  # classes have no complexity
        "outer.<locals>.Local.method": 2,
        "Shape": None,
        "Shape.area": 2,
    }


def test_complexity_survives_the_parse_cache_round_trip() -> None:
    parsed = ADAPTER.parse("m.py", "m", b"def f(a):\n    return a or 1\n")
    restored = from_json(to_json(parsed))
    assert [s.complexity for s in restored.symbols] == [None, 2]


def test_parser_version_changed_with_complexity() -> None:
    # Analyses and cached parses from before complexity existed must not be reused.
    assert ADAPTER.version.endswith("-r3")
