"""The parse cache must return exactly what the parser would."""

from __future__ import annotations

import textwrap

import pytest

from repolens.parsers.python import PythonAdapter
from repolens.parsers.serialize import from_json, to_json

SOURCE = textwrap.dedent(
    '''
    """Module doc."""
    from __future__ import annotations
    import os.path as osp
    from . import sibling
    from ..pkg import *

    __all__ = ["Shape"]
    LIMIT = 3

    class Shape(Base, mixins.Mixin, metaclass=Meta):
        """A shape."""

        @property
        def area(self):
            return super().area() + getattr(self, "x")()

        async def fetch(self, url: str = osp.join("a", "b")) -> None:
            def inner():
                return helper(callback=on_done)
            return inner

    def broken(:
        pass
    '''
).lstrip()


def test_round_trip_is_lossless() -> None:
    parsed = PythonAdapter().parse("pkg/shapes.py", "pkg.shapes", SOURCE.encode())
    assert parsed.problems  # includes a syntax error, so problems are covered too
    restored = from_json(to_json(parsed))
    assert restored == parsed


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        from_json({"v": 99})
