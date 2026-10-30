"""Lossless JSON round trip for ``ParsedFile`` (used by the incremental parse cache)."""

from __future__ import annotations

from typing import Any

from repolens.domain.enums import ReferenceKind, SymbolKind, Visibility
from repolens.domain.spans import Span
from repolens.parsers.base import (
    ExtractedSymbol,
    ImportDetail,
    ParsedFile,
    ParseProblem,
    RawReference,
)

FORMAT_VERSION = 2


def _span(span: Span) -> list[int]:
    return [span.start_line, span.start_col, span.end_line, span.end_col]


def to_json(parsed: ParsedFile) -> dict[str, Any]:
    return {
        "v": FORMAT_VERSION,
        "path": parsed.path,
        "module": parsed.module,
        "language": parsed.language,
        "symbols": [
            [
                s.key,
                s.qualified_name,
                s.name,
                s.kind.value,
                _span(s.span),
                s.parent_key,
                s.visibility.value,
                s.signature,
                s.body_fingerprint,
                list(s.decorators),
                s.doc_summary,
                s.is_test,
                s.has_parse_errors,
                s.complexity,
            ]
            for s in parsed.symbols
        ],
        "references": [
            [
                r.kind.value,
                r.source_key,
                r.text,
                _span(r.span),
                r.target,
                None
                if r.import_detail is None
                else [
                    r.import_detail.module,
                    r.import_detail.name,
                    r.import_detail.alias,
                    r.import_detail.level,
                    r.import_detail.is_star,
                ],
                r.dynamic,
                r.type_checking,
            ]
            for r in parsed.references
        ],
        "problems": [[_span(p.span), p.message] for p in parsed.problems],
        "module_assignments": list(parsed.module_assignments),
        "exported_names": parsed.exported_names,
    }


def from_json(data: dict[str, Any]) -> ParsedFile:
    if data.get("v") != FORMAT_VERSION:
        raise ValueError("unsupported parse cache format")
    return ParsedFile(
        path=data["path"],
        module=data["module"],
        language=data["language"],
        symbols=[
            ExtractedSymbol(
                key=s[0],
                qualified_name=s[1],
                name=s[2],
                kind=SymbolKind(s[3]),
                span=Span(*s[4]),
                parent_key=s[5],
                visibility=Visibility(s[6]),
                signature=s[7],
                body_fingerprint=s[8],
                decorators=tuple(s[9]),
                doc_summary=s[10],
                is_test=s[11],
                has_parse_errors=s[12],
                complexity=s[13],
            )
            for s in data["symbols"]
        ],
        references=[
            RawReference(
                kind=ReferenceKind(r[0]),
                source_key=r[1],
                text=r[2],
                span=Span(*r[3]),
                target=r[4],
                import_detail=None if r[5] is None else ImportDetail(*r[5]),
                dynamic=r[6],
                type_checking=r[7],
            )
            for r in data["references"]
        ],
        problems=[ParseProblem(Span(*p[0]), p[1]) for p in data["problems"]],
        module_assignments=list(data["module_assignments"]),
        exported_names=data["exported_names"],
    )
