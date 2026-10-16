"""Change detection between two revisions: files → changed line ranges → changed symbols.

Pure functions; Git access and persistence live in the service layer.

Attribution rule: each changed line belongs to the *innermost* symbol whose span contains it
(a method rather than its class); lines outside any definition belong to the module. A
symbol present in both revisions is matched by its key (qualified name + kind + ordinal),
with module names translated across file renames. Unmatched symbols are added or removed,
except that an unmatched pair in the same file with an identical body fingerprint is a rename.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from repolens.domain.enums import SymbolKind
from repolens.domain.spans import Span

LineRange = tuple[int, int]  # 1-based, inclusive


class FileChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    COPIED = "COPIED"
    TYPE_CHANGED = "TYPE_CHANGED"


class SymbolChangeKind(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    RENAMED = "RENAMED"


_STATUS = {
    "A": FileChangeType.ADDED,
    "M": FileChangeType.MODIFIED,
    "D": FileChangeType.DELETED,
    "R": FileChangeType.RENAMED,
    "C": FileChangeType.COPIED,
    "T": FileChangeType.TYPE_CHANGED,
}
ZERO_SHA = "0" * 40


@dataclass(frozen=True, slots=True)
class FileDiff:
    change_type: FileChangeType
    old_path: str | None
    new_path: str | None
    old_blob: str | None
    new_blob: str | None
    old_mode: str | None
    new_mode: str | None
    similarity: int | None = None  # percent, for renames and copies


class DiffParseError(ValueError):
    pass


def parse_raw_diff(raw: bytes) -> list[FileDiff]:
    """Parse ``git diff --raw -z --no-abbrev`` output (with ``-M`` for renames)."""
    fields = raw.split(b"\0")
    diffs: list[FileDiff] = []
    i = 0
    while i < len(fields):
        header = fields[i]
        if not header:
            i += 1
            continue
        if not header.startswith(b":"):
            raise DiffParseError(f"unexpected diff record: {header[:60]!r}")
        parts = header[1:].decode("ascii").split()
        if len(parts) != 5:
            raise DiffParseError(f"malformed diff header: {header[:60]!r}")
        old_mode, new_mode, old_sha, new_sha, status = parts
        kind = _STATUS.get(status[0])
        if kind is None:
            raise DiffParseError(f"unsupported diff status {status!r}")
        similarity = int(status[1:]) if len(status) > 1 and status[1:].isdigit() else None
        old_path: str | None
        new_path: str | None
        if kind in (FileChangeType.RENAMED, FileChangeType.COPIED):
            old_path, new_path = _decode(fields[i + 1]), _decode(fields[i + 2])
            i += 3
        else:
            path = _decode(fields[i + 1])
            old_path = None if kind is FileChangeType.ADDED else path
            new_path = None if kind is FileChangeType.DELETED else path
            i += 2
        diffs.append(
            FileDiff(
                change_type=kind,
                old_path=old_path,
                new_path=new_path,
                old_blob=None if old_sha.strip("0") == "" else old_sha,
                new_blob=None if new_sha.strip("0") == "" else new_sha,
                old_mode=None if old_mode == "000000" else old_mode,
                new_mode=None if new_mode == "000000" else new_mode,
                similarity=similarity,
            )
        )
    return sorted(diffs, key=lambda d: d.new_path or d.old_path or "")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


@dataclass(frozen=True, slots=True)
class LineChanges:
    old_ranges: tuple[LineRange, ...]  # lines removed or replaced in the old file
    new_ranges: tuple[LineRange, ...]  # lines added or replaced in the new file
    # Pure insertions have no old lines; the old-file line after which they were inserted.
    insertion_points: tuple[int, ...]
    added: int
    deleted: int

    @property
    def is_empty(self) -> bool:
        return not self.old_ranges and not self.new_ranges


def line_changes(old_text: str | None, new_text: str | None) -> LineChanges:
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    old_ranges: list[LineRange] = []
    new_ranges: list[LineRange] = []
    insertions: list[int] = []
    added = deleted = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            old_ranges.append((i1 + 1, i2))
            deleted += i2 - i1
        else:
            insertions.append(i1)  # inserted after old line i1 (0 = at the top)
        if j2 > j1:
            new_ranges.append((j1 + 1, j2))
            added += j2 - j1
    return LineChanges(tuple(old_ranges), tuple(new_ranges), tuple(insertions), added, deleted)


@dataclass(frozen=True, slots=True)
class SymbolVersion:
    """A symbol as stored for one revision (ids refer to that revision's analysis)."""

    id: int
    key: str
    qualified_name: str
    name: str
    kind: SymbolKind
    span: Span
    signature: str | None
    body_fingerprint: str | None
    decorators: tuple[str, ...]
    parent_id: int | None


@dataclass(frozen=True, slots=True)
class SymbolChange:
    kind: SymbolChangeKind
    base: SymbolVersion | None
    target: SymbolVersion | None
    signature_changed: bool = False
    body_changed: bool = False
    decorators_changed: bool = False
    bases_changed: bool = False
    changed_lines: int = 0  # lines attributed to this symbol directly (innermost)

    @property
    def qualified_name(self) -> str:
        version = self.target or self.base
        assert version is not None  # noqa: S101 - at least one side always exists
        return version.qualified_name

    @property
    def symbol_kind(self) -> SymbolKind:
        version = self.target or self.base
        assert version is not None  # noqa: S101
        return version.kind


def innermost_owners(
    symbols: Sequence[SymbolVersion], ranges: Iterable[LineRange]
) -> dict[int, int]:
    """Map symbol id → number of changed lines whose innermost containing symbol it is."""
    counts: dict[int, int] = {}
    # Smallest span first: the first container found for a line is the innermost one.
    ordered = sorted(symbols, key=lambda s: (s.span.line_count, s.span.start_line, s.id))
    for first, last in ranges:
        for line in range(first, last + 1):
            for symbol in ordered:
                if symbol.span.contains_line(line):
                    counts[symbol.id] = counts.get(symbol.id, 0) + 1
                    break
    return counts


def map_symbol_changes(
    base_symbols: Sequence[SymbolVersion],
    target_symbols: Sequence[SymbolVersion],
    lines: LineChanges,
    *,
    base_module: str | None,
    target_module: str | None,
) -> list[SymbolChange]:
    """Classify symbol changes for one file pair (either side may be empty)."""
    base_by_key = {_relative_key(s.key, base_module): s for s in base_symbols}
    target_by_key = {_relative_key(s.key, target_module): s for s in target_symbols}
    base_touched = innermost_owners(base_symbols, lines.old_ranges)
    target_touched = innermost_owners(target_symbols, lines.new_ranges)

    changes: list[SymbolChange] = []
    for key in sorted(base_by_key.keys() & target_by_key.keys()):
        old, new = base_by_key[key], target_by_key[key]
        flags = _compare(old, new)
        if not any(flags.values()):
            continue  # untouched, or only whitespace/comments changed inside it
        lines_here = base_touched.get(old.id, 0) + target_touched.get(new.id, 0)
        changes.append(
            SymbolChange(SymbolChangeKind.MODIFIED, old, new, changed_lines=lines_here, **flags)
        )

    removed = [base_by_key[k] for k in sorted(base_by_key.keys() - target_by_key.keys())]
    added = [target_by_key[k] for k in sorted(target_by_key.keys() - base_by_key.keys())]
    renames = _rename_pairs(
        removed,
        added,
        base_symbols=base_symbols,
        target_symbols=target_symbols,
        base_module=base_module,
        target_module=target_module,
    )
    for old, new in renames:
        removed.remove(old)
        added.remove(new)
        changes.append(
            SymbolChange(
                SymbolChangeKind.RENAMED,
                old,
                new,
                signature_changed=True,
                decorators_changed=old.decorators != new.decorators,
                changed_lines=target_touched.get(new.id, 0),
            )
        )
    changes.extend(
        SymbolChange(SymbolChangeKind.REMOVED, old, None, changed_lines=old.span.line_count)
        for old in removed
    )
    changes.extend(
        SymbolChange(SymbolChangeKind.ADDED, None, new, changed_lines=new.span.line_count)
        for new in added
    )
    return sorted(changes, key=lambda c: (c.qualified_name, c.kind.value))


def _rename_pairs(
    removed: list[SymbolVersion],
    added: list[SymbolVersion],
    *,
    base_symbols: Sequence[SymbolVersion],
    target_symbols: Sequence[SymbolVersion],
    base_module: str | None,
    target_module: str | None,
) -> list[tuple[SymbolVersion, SymbolVersion]]:
    """Removed/added pairs with the same kind, parent and name-independent fingerprint.

    Only unique matches are paired; identical trivial bodies (``pass``) stay add/remove.
    """
    base_parent = _parent_keys(base_symbols, base_module)
    target_parent = _parent_keys(target_symbols, target_module)

    def signature(symbol: SymbolVersion, parents: dict[int, str]) -> tuple[str, str, str]:
        return (symbol.kind.value, parents.get(symbol.id, ""), symbol.body_fingerprint or "")

    old_groups: dict[tuple[str, str, str], list[SymbolVersion]] = {}
    for old in removed:
        if old.body_fingerprint and old.kind is not SymbolKind.MODULE:
            old_groups.setdefault(signature(old, base_parent), []).append(old)
    new_groups: dict[tuple[str, str, str], list[SymbolVersion]] = {}
    for new in added:
        if new.body_fingerprint and new.kind is not SymbolKind.MODULE:
            new_groups.setdefault(signature(new, target_parent), []).append(new)
    return [
        (olds[0], new_groups[group][0])
        for group, olds in sorted(old_groups.items())
        if len(olds) == 1 and len(new_groups.get(group, [])) == 1
    ]


def _parent_keys(symbols: Sequence[SymbolVersion], module: str | None) -> dict[int, str]:
    by_id = {s.id: s for s in symbols}
    return {
        s.id: _relative_key(by_id[s.parent_id].key, module)
        for s in symbols
        if s.parent_id is not None and s.parent_id in by_id
    }


def _compare(old: SymbolVersion, new: SymbolVersion) -> dict[str, bool]:
    signature_differs = _normalise(old.signature) != _normalise(new.signature)
    is_class = new.kind is SymbolKind.CLASS
    return {
        # A class "signature" is ``class Name(Bases)``; with the name fixed, only bases vary.
        "signature_changed": signature_differs and not is_class,
        "body_changed": old.body_fingerprint != new.body_fingerprint,
        "decorators_changed": old.decorators != new.decorators,
        "bases_changed": signature_differs and is_class,
    }


def _normalise(text: str | None) -> str:
    return " ".join((text or "").split())


def _relative_key(key: str, module: str | None) -> str:
    """Strip the module prefix so keys can be matched across file renames."""
    if module and (key.startswith(f"{module}.") or key.startswith(f"{module}#")):
        return key[len(module) :]
    return key
