"""Change detection: raw diff parsing, line ranges and symbol classification."""

from __future__ import annotations

import textwrap

import pytest

from repolens.domain.enums import SymbolKind
from repolens.impact.changes import (
    DiffParseError,
    FileChangeType,
    SymbolChangeKind,
    SymbolVersion,
    innermost_owners,
    line_changes,
    map_symbol_changes,
    parse_raw_diff,
)
from repolens.parsers.python import PythonAdapter

ADAPTER = PythonAdapter()


def test_parse_raw_diff_all_statuses() -> None:
    a, b, z = "a" * 40, "b" * 40, "0" * 40
    raw = (
        f":000000 100644 {z} {a} A\0new.py\0"
        f":100644 100644 {a} {b} M\0mod.py\0"
        f":100644 000000 {a} {z} D\0gone.py\0"
        f":100644 100644 {a} {b} R087\0old/name.py\0new/name.py\0"
        f":100644 120000 {a} {b} T\0link\0"
    ).encode()
    diffs = {(d.change_type, d.old_path, d.new_path) for d in parse_raw_diff(raw)}
    assert diffs == {
        (FileChangeType.ADDED, None, "new.py"),
        (FileChangeType.MODIFIED, "mod.py", "mod.py"),
        (FileChangeType.DELETED, "gone.py", None),
        (FileChangeType.RENAMED, "old/name.py", "new/name.py"),
        (FileChangeType.TYPE_CHANGED, "link", "link"),
    }
    renamed = next(d for d in parse_raw_diff(raw) if d.change_type is FileChangeType.RENAMED)
    assert renamed.similarity == 87
    added = next(d for d in parse_raw_diff(raw) if d.change_type is FileChangeType.ADDED)
    assert added.old_blob is None and added.new_blob == a


def test_parse_raw_diff_handles_unusual_paths() -> None:
    a, b = "a" * 40, "b" * 40
    raw = f":100644 100644 {a} {b} M\0dir with space/ünï\tcode.py\0".encode()
    assert parse_raw_diff(raw)[0].new_path == "dir with space/ünï\tcode.py"


@pytest.mark.parametrize("raw", [b"garbage\0", b":100644 100644 a b\0x\0", b":1 2 3 4 Z\0x\0"])
def test_parse_raw_diff_rejects_malformed_input(raw: bytes) -> None:
    with pytest.raises(DiffParseError):
        parse_raw_diff(raw)


def test_line_changes() -> None:
    old = "a\nb\nc\nd\n"
    new = "a\nB\nc\nd\ne\n"
    changes = line_changes(old, new)
    assert changes.old_ranges == ((2, 2),)
    assert changes.new_ranges == ((2, 2), (5, 5))
    assert (changes.added, changes.deleted) == (2, 1)
    assert changes.insertion_points == (4,)
    assert line_changes(None, "x\ny\n").new_ranges == ((1, 2),)
    assert line_changes("x\n", None).old_ranges == ((1, 1),)
    assert line_changes("same\n", "same\n").is_empty


def versions(source: str, module: str = "m", start_id: int = 1) -> list[SymbolVersion]:
    parsed = ADAPTER.parse(f"{module}.py", module, textwrap.dedent(source).lstrip("\n").encode())
    ids = {s.key: start_id + i for i, s in enumerate(parsed.symbols)}
    return [
        SymbolVersion(
            id=ids[s.key],
            key=s.key,
            qualified_name=s.qualified_name,
            name=s.name,
            kind=s.kind,
            span=s.span,
            signature=s.signature,
            body_fingerprint=s.body_fingerprint,
            decorators=s.decorators,
            parent_id=ids.get(s.parent_key) if s.parent_key else None,
        )
        for s in parsed.symbols
    ]


def classify(old: str, new: str, *, old_module: str = "m", new_module: str = "m") -> dict[str, str]:
    old_src = textwrap.dedent(old).lstrip("\n")
    new_src = textwrap.dedent(new).lstrip("\n")
    changes = map_symbol_changes(
        versions(old_src, old_module),
        versions(new_src, new_module, start_id=1000),
        line_changes(old_src, new_src),
        base_module=old_module,
        target_module=new_module,
    )
    result = {}
    for change in changes:
        flags = [
            name
            for name in ("signature_changed", "body_changed", "decorators_changed", "bases_changed")
            if getattr(change, name)
        ]
        result[change.qualified_name] = ":".join([change.kind.value, *flags])
    return result


BASE = """
    import os

    LIMIT = 10

    class Service:
        def fetch(self, key):
            return os.environ.get(key)

        def store(self, key, value):
            pass

    def helper(x):
        return x * 2
"""


def test_body_change_is_attributed_to_innermost_symbol() -> None:
    new = BASE.replace("return os.environ.get(key)", "return os.getenv(key)")
    assert classify(BASE, new) == {
        "m.Service.fetch": "MODIFIED:body_changed",
        # The class contains the changed method, so its own fingerprint changes too.
        "m.Service": "MODIFIED:body_changed",
        "m": "MODIFIED:body_changed",
    }


def test_signature_change() -> None:
    new = BASE.replace("def helper(x):", "def helper(x, factor=2):").replace(
        "return x * 2", "return x * factor"
    )
    assert classify(BASE, new)["m.helper"] == "MODIFIED:signature_changed:body_changed"


def test_whitespace_and_comment_changes_are_ignored() -> None:
    new = BASE.replace("return x * 2", "return x*2  # doubled").replace("    pass", "    pass\n")
    assert classify(BASE, new) == {}


def test_added_removed_and_renamed_symbols() -> None:
    new = BASE.replace("def helper(x):", "def double(x):") + "\ndef extra():\n    return 1\n"
    new = new.replace("        def store(self, key, value):\n            pass\n", "")
    result = classify(BASE, new)
    assert result["m.double"] == "RENAMED:signature_changed"
    assert result["m.extra"] == "ADDED"
    assert result["m.Service.store"] == "REMOVED"
    assert "m.helper" not in result


def test_trivial_identical_bodies_are_not_paired_as_renames() -> None:
    old = "def a():\n    pass\n\ndef b():\n    pass\n"
    new = "def c():\n    pass\n\ndef d():\n    pass\n"
    assert classify(old, new) == {
        "m": "MODIFIED:body_changed",  # the module's own token stream changed
        "m.a": "REMOVED",
        "m.b": "REMOVED",
        "m.c": "ADDED",
        "m.d": "ADDED",
    }


def test_base_class_and_decorator_changes() -> None:
    old = "class A(Base):\n    @cache\n    def m(self):\n        return 1\n"
    new = "class A(Other):\n    @cache(size=2)\n    def m(self):\n        return 1\n"
    result = classify(old, new)
    assert result["m.A"] == "MODIFIED:body_changed:bases_changed"
    assert "decorators_changed" in result["m.A.m"]


def test_file_rename_matches_symbols_across_module_names() -> None:
    old = "def keep():\n    return 1\n\ndef edit():\n    return 1\n"
    new = "def keep():\n    return 1\n\ndef edit():\n    return 2\n"
    result = classify(old, new, old_module="pkg.old", new_module="pkg.new")
    assert result == {"pkg.new": "MODIFIED:body_changed", "pkg.new.edit": "MODIFIED:body_changed"}


def test_new_and_deleted_files() -> None:
    added = classify("", "def f():\n    pass\n")
    assert added == {"m": "MODIFIED:body_changed", "m.f": "ADDED"}
    removed = map_symbol_changes(
        versions("def f():\n    pass\n"),
        [],
        line_changes("def f():\n    pass\n", None),
        base_module="m",
        target_module=None,
    )
    assert {(c.qualified_name, c.kind) for c in removed} == {
        ("m", SymbolChangeKind.REMOVED),
        ("m.f", SymbolChangeKind.REMOVED),
    }


def test_innermost_owner_counts() -> None:
    symbols = versions(BASE)
    by_name = {s.qualified_name: s.id for s in symbols}
    owners = innermost_owners(symbols, [(7, 7), (3, 3)])
    assert owners == {by_name["m.Service.fetch"]: 1, by_name["m"]: 1}
    assert all(s.kind is not SymbolKind.CLASS for s in symbols if s.id in owners)
