"""Map repository paths to Python module names.

Rule (matches how pytest's default "prepend" import mode and ordinary packages behave):
walk up from the file while the directory contains ``__init__.py``; the first directory
without one is the import root, and the module name is the path relative to it. A
top-level ``src/`` directory that is not itself a package is treated as an import root, so
``src/pkg/mod.py`` becomes ``pkg.mod``.

Namespace packages (directories without ``__init__.py``) are therefore named from their
nearest regular-package root. The resolver compensates with a unique-suffix fallback,
reported as heuristic rather than exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class ModuleName:
    path: str
    name: str
    is_package: bool  # an __init__ file
    root: str  # import root directory ("" for the repository root)


def module_names(paths: list[str]) -> dict[str, ModuleName]:
    """Return module names for every ``.py`` / ``.pyi`` path. Stubs lose to sources."""
    path_set = set(paths)
    package_dirs = {
        str(PurePosixPath(p).parent)
        for p in path_set
        if PurePosixPath(p).name in ("__init__.py", "__init__.pyi")
    }
    result: dict[str, ModuleName] = {}
    for path in sorted(path_set):
        pure = PurePosixPath(path)
        if pure.suffix not in (".py", ".pyi"):
            continue
        parts = list(pure.parent.parts)
        root_len = len(parts)
        while root_len > 0 and "/".join(parts[:root_len]) in package_dirs:
            root_len -= 1
        root = "/".join(parts[:root_len])
        package_parts = parts[root_len:]
        is_package = pure.stem == "__init__"
        name_parts = package_parts if is_package else [*package_parts, pure.stem]
        if not name_parts or not all(part.isidentifier() for part in name_parts):
            continue  # e.g. a top-level __init__.py, or "my-script.py": not importable
        result[path] = ModuleName(path, ".".join(name_parts), is_package, root)
    return result


def dedupe_modules(names: dict[str, ModuleName]) -> tuple[dict[str, ModuleName], list[str]]:
    """Keep one file per module name; ``.py`` beats ``.pyi``, then shortest root wins.

    Returns the chosen mapping (path → module) and the paths that were shadowed.
    """
    by_module: dict[str, list[ModuleName]] = {}
    for module in names.values():
        by_module.setdefault(module.name, []).append(module)
    chosen: dict[str, ModuleName] = {}
    shadowed: list[str] = []
    for candidates in by_module.values():
        candidates.sort(key=lambda m: (m.path.endswith(".pyi"), len(m.root), m.root, m.path))
        chosen[candidates[0].path] = candidates[0]
        shadowed.extend(c.path for c in candidates[1:])
    return chosen, sorted(shadowed)
