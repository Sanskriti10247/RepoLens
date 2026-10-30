"""JavaScript, TypeScript, Go, Java and Rust extraction, and their simpler name resolution."""

from __future__ import annotations

import pytest

from repolens.codegraph.modules import ModuleName
from repolens.codegraph.resolver import resolve_files
from repolens.domain.enums import EdgeType, ReferenceKind, ResolutionStatus, Visibility
from repolens.parsers.base import ParsedFile
from repolens.parsers.serialize import from_json, to_json
from repolens.parsers.treesitter import adapters

ADAPTERS = adapters()

# One small file per language: (path, source, symbols, complexities, docs, imports, calls).
CASES = {
    "javascript": (
        "lib/a.jsx",
        b'import def, { x as y } from "./b";\n'
        b"/** Greets. */\n"
        b"export function hi(n) { if (n || y) { return <p/>; } return def(n); }\n"
        b"class A extends B { m() { this.hi(); } }\n"
        b"const f = () => new A();\n",
        {"lib/a.jsx.hi": "FUNCTION", "lib/a.jsx.A": "CLASS", "lib/a.jsx.A.m": "METHOD"}
        | {"lib/a.jsx.f": "FUNCTION"},
        {"hi": 3, "m": 1, "f": 1},
        {"hi": "Greets."},
        {"./b.default", "./b.x"},
        {"this.hi", "def", "A"},
    ),
    "typescript": (
        "src/s.tsx",
        b"import * as ns from 'pkg';\n"
        b"export class S implements I {\n"
        b"  // Runs.\n"
        b"  private run(x: number): void { for (const i of [x]) { ns.go(i ?? 0); } }\n"
        b"}\n",
        {"src/s.tsx.S": "CLASS", "src/s.tsx.S.run": "METHOD"},
        {"run": 3},
        {"run": "Runs."},
        {"pkg"},
        {"ns.go"},
    ),
    "go": (
        "pkg/s.go",
        b'package pkg\n\nimport (\n\t"fmt"\n\tst "strings"\n)\n\n'
        b"// Server serves.\ntype Server struct{}\n\n"
        b"func (s *Server) Run(n int) {\n\tswitch n {\n\tcase 1:\n\tdefault:\n\t}\n"
        b'\tfmt.Println(st.ToUpper(helper()))\n}\n\nfunc helper() string { return "" }\n',
        {"pkg/s.go.Server": "CLASS", "pkg/s.go.Server.Run": "METHOD"}
        | {"pkg/s.go.helper": "FUNCTION"},
        {"Run": 2, "helper": 1},
        {"Server": "Server serves."},
        {"fmt", "strings"},
        {"fmt.Println", "st.ToUpper", "helper"},
    ),
    "java": (
        "src/main/java/a/A.java",
        b"package a;\nimport java.util.List;\n"
        b"/** The A. */\npublic class A extends B {\n"
        b"  private int m(int x) { return x > 0 && x < 9 ? List.of(x).size() : this.n(); }\n"
        b"  int n() { return new A().m(1); }\n}\n",
        {"src/main/java/a/A.java.A": "CLASS", "src/main/java/a/A.java.A.m": "METHOD"}
        | {"src/main/java/a/A.java.A.n": "METHOD"},
        {"m": 3, "n": 1},
        {"A": "The A."},
        {"java.util.List"},
        {"List.of", "this.n", "A", None},  # calls on call results have no static target
    ),
    "rust": (
        "src/lib.rs",
        b"use std::fmt;\n/// A point.\npub struct P;\n"
        b"impl P {\n    pub fn new(a: i32) -> P {\n"
        b"        match a { 1 => helper(), _ => fmt::f() };\n        P\n    }\n}\n"
        b"fn helper() {}\n",
        {"src/lib.rs.P": "CLASS", "src/lib.rs.P.new": "METHOD", "src/lib.rs.helper": "FUNCTION"},
        {"new": 2, "helper": 1},
        {"P": "A point."},
        {"std.fmt"},
        {"helper", "fmt.f"},
    ),
}


def _parse(language: str, path: str, source: bytes) -> ParsedFile:
    adapter = next(a for a in ADAPTERS if a.language == language)
    assert adapter.handles(path)
    return adapter.parse(path, path, source)


@pytest.mark.parametrize("language", sorted(CASES))
def test_extraction(language: str) -> None:
    path, source, symbols, complexity, docs, imports, calls = CASES[language]
    parsed = _parse(language, path, source)
    assert parsed.language == language and not parsed.problems
    found = {s.qualified_name: s.kind.value for s in parsed.symbols if s.kind.value != "MODULE"}
    assert found == symbols
    by_name = {s.name: s for s in parsed.symbols}
    assert {n: by_name[n].complexity for n in complexity} == complexity
    assert {n: by_name[n].doc_summary for n in docs} == docs
    refs = parsed.references
    assert {r.target for r in refs if r.kind is ReferenceKind.IMPORT} == imports
    assert {r.target for r in refs if r.kind is ReferenceKind.CALL} == calls
    assert from_json(to_json(parsed)).symbols == parsed.symbols  # parse cache round trip


def test_visibility() -> None:
    cases = [
        ("go", "a.go", b"package a\nfunc Pub() {}\nfunc priv() {}\n", "Pub", "priv"),
        ("rust", "a.rs", b"pub fn a() {}\nfn b() {}\n", "a", "b"),
        ("java", "A.java", b"class A { public void a() {} private void b() {} }\n", "a", "b"),
        ("typescript", "a.ts", b"class C { a() {} private b() {} }\n", "a", "b"),
    ]
    for language, path, source, public, private in cases:
        found = {s.name: s.visibility for s in _parse(language, path, source).symbols}
        assert (found[public], found[private]) == (Visibility.PUBLIC, Visibility.PRIVATE)


def test_syntax_errors_are_reported() -> None:
    parsed = _parse("typescript", "x.ts", b"function ok() {}\nfunction broken( {\n")
    assert parsed.problems
    assert any(s.has_parse_errors for s in parsed.symbols)


def _resolve(files: dict[str, tuple[str, bytes]]):  # type: ignore[no-untyped-def]
    parsed = [_parse(lang, path, src) for path, (lang, src) in files.items()]
    modules = {p: ModuleName(p, p, False, "") for p in files}
    return resolve_files(parsed, modules)


def test_resolution_follows_file_imports_and_never_guesses() -> None:
    result = _resolve(
        {
            "web/a.ts": ("typescript", b'import { b } from "./lib/b.js";\nb();\nexternal();\n'),
            "web/lib/b.ts": ("typescript", b"export function b() {}\n"),
            "j/x/Main.java": (
                "java",
                b"import x.y.Util;\nclass Main { void m() { Util.go(); } }\n",
            ),
            "j/x/y/Util.java": ("java", b"class Util { static void go() {} }\n"),
            "g/a.go": ("go", b"package g\nfunc (s *S) Run() {}\nfunc f() { Run() }\n"),
        }
    )
    calls = {(r.ref.target, r.path): r.resolution for r in result.references}
    assert calls[("b", "web/a.ts")].target == "web/lib/b.ts.b#FUNCTION"
    assert calls[("external", "web/a.ts")].status is ResolutionStatus.UNRESOLVED
    assert calls[("Util.go", "j/x/Main.java")].target == "j/x/y/Util.java.Util.go#METHOD"
    # A Go method is not a plain function name: ``Run()`` must not bind to ``(*S).Run``.
    assert calls[("Run", "g/a.go")].status is ResolutionStatus.UNRESOLVED
    assert ("web/a.ts#MODULE", "web/lib/b.ts.b#FUNCTION") in {
        (e.src, e.dst) for e in result.edges if e.edge_type is EdgeType.IMPORTS
    }
