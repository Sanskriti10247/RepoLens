"""Evidence-grounded repository questions.

Retrieval is deterministic: identifiers and keywords from the question are matched against
symbols and paths; the strongest matches are expanded with source snippets, graph
relationships, reverse-impact traversal, tests and recorded coverage. The answer is composed
from that evidence with citations. An optional model provider may rephrase the answer, but
only from the same evidence, and only citations of evidence it was given are accepted.

Repository text is untrusted data: it is quoted as evidence and never treated as an
instruction to this service or to a model.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from repolens.domain.enums import EdgeType, ResolutionStatus
from repolens.impact.propagation import Seed, propagate
from repolens.llm.provider import AnswerProvider, ProviderUnavailable
from repolens.persistence import graph_queries as q
from repolens.persistence.models import (
    CoverageRecord,
    SourceFile,
    Symbol,
    SymbolReference,
    TestRun,
)
from repolens.services.errors import InvalidInput, NotFound, Unavailable
from repolens.services.graph import RunContext, SourceReader

MAX_QUESTION = 1000
MAX_SNIPPET_LINES = 60
_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "of",
        "in",
        "on",
        "to",
        "for",
        "and",
        "or",
        "not",
        "what",
        "which",
        "who",
        "where",
        "when",
        "why",
        "how",
        "does",
        "do",
        "did",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "with",
        "from",
        "by",
        "as",
        "at",
        "into",
        "if",
        "i",
        "my",
        "me",
        "we",
        "our",
        "you",
        "can",
        "could",
        "would",
        "should",
        "will",
        "there",
        "their",
        "them",
        "function",
        "functions",
        "method",
        "methods",
        "class",
        "classes",
        "module",
        "modules",
        "file",
        "files",
        "code",
        "implemented",
        "implementation",
        "defined",
        "define",
        "call",
        "calls",
        "called",
        "caller",
        "callers",
        "test",
        "tests",
        "tested",
        "change",
        "changes",
        "changing",
        "break",
        "breaks",
        "depend",
        "depends",
        "dependent",
        "dependents",
        "use",
        "used",
        "uses",
        "using",
        "find",
        "show",
        "list",
        "all",
        "any",
    ]
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_PATH = re.compile(r"[\w./-]+\.py\b")


class Intent(StrEnum):
    DEFINITION = "DEFINITION"
    CALLERS = "CALLERS"
    CALLEES = "CALLEES"
    IMPACT = "IMPACT"
    TESTS = "TESTS"
    UNRESOLVED = "UNRESOLVED"
    GENERAL = "GENERAL"


_INTENT_PATTERNS: list[tuple[Intent, re.Pattern[str]]] = [
    (
        Intent.UNRESOLVED,
        re.compile(r"\b(unresolved|dynamic|could(?:n't| not) (?:be )?resolv)", re.I),
    ),
    (
        Intent.TESTS,
        re.compile(r"\b(which tests?|tests? (?:for|cover|that)|tested by|coverage)\b", re.I),
    ),
    (
        Intent.IMPACT,
        re.compile(r"\b(break|impact|depends? on|dependents?|affect|if i change)\b", re.I),
    ),
    (
        Intent.CALLERS,
        re.compile(
            r"\b(who calls|what calls|callers?|called (?:from|by)|uses? of|used by)\b", re.I
        ),
    ),
    (
        Intent.CALLEES,
        re.compile(r"\b(what does \S+ call|calls? made by|callees?|depend(?:s)? upon)\b", re.I),
    ),
    (
        Intent.DEFINITION,
        re.compile(r"\b(where is|where are|defined|implemented|implementation|location)\b", re.I),
    ),
]


class Category(StrEnum):
    SOURCE_FACT = "SOURCE_FACT"  # read directly from the analysed commit
    STATIC_RELATIONSHIP = "STATIC_RELATIONSHIP"  # derived by static analysis
    RUNTIME_OBSERVATION = "RUNTIME_OBSERVATION"  # recorded during sandboxed test runs
    UNCERTAINTY = "UNCERTAINTY"  # what analysis could not determine


@dataclass
class EvidenceItem:
    id: str
    category: Category
    statement: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    symbol_id: int | None = None
    qualified_name: str | None = None
    text: str | None = None  # source snippet (SOURCE_FACT only)


@dataclass
class Terms:
    identifiers: list[str]
    paths: list[str]
    keywords: list[str]
    intent: Intent


@dataclass
class Answer:
    question: str
    intent: Intent
    commit_sha: str
    analysis_id: uuid.UUID
    answer_markdown: str
    insufficient_evidence: bool
    evidence: list[EvidenceItem] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    mode: str = "deterministic"
    model_explanation: str | None = None
    model_note: str | None = None


def extract_terms(question: str) -> Terms:
    intent = Intent.GENERAL
    for candidate, pattern in _INTENT_PATTERNS:
        if pattern.search(question):
            intent = candidate
            break
    paths = sorted(set(_PATH.findall(question)))
    stripped = _PATH.sub(" ", question)
    identifiers: list[str] = []
    keywords: list[str] = []
    for token in _IDENTIFIER.findall(stripped):
        lower = token.lower()
        if lower in _STOPWORDS or len(token) < 3:
            continue
        looks_like_code = "_" in token or "." in token or (any(c.isupper() for c in token[1:]))
        (identifiers if looks_like_code else keywords).append(token)
    # Plain words can still be names ("authentication" → "authenticate"), searched more loosely.
    return Terms(
        identifiers=list(dict.fromkeys(identifiers)),
        paths=paths,
        keywords=list(dict.fromkeys(keywords)),
        intent=intent,
    )


class QuestionService:
    def __init__(self, source_reader: SourceReader) -> None:
        self.source_reader = source_reader

    def answer(self, session: Session, ctx: RunContext, question: str) -> Answer:
        question = question.strip()
        if not question or len(question) > MAX_QUESTION:
            raise InvalidInput(
                f"Question must be 1-{MAX_QUESTION} characters", code="invalid_question"
            )
        terms = extract_terms(question)
        ranked = self._rank_symbols(session, ctx, terms)
        evidence: list[EvidenceItem] = []
        builder = _EvidenceBuilder(evidence)
        primary = ranked[: 1 if terms.intent is not Intent.GENERAL else 3]
        for symbol in primary:
            self._definition(session, ctx, symbol, builder)
            if terms.intent in (Intent.CALLERS, Intent.GENERAL, Intent.DEFINITION):
                self._relationships(session, ctx, symbol, builder, outgoing=False)
            if terms.intent in (Intent.CALLEES, Intent.GENERAL):
                self._relationships(session, ctx, symbol, builder, outgoing=True)
            if terms.intent in (Intent.IMPACT, Intent.TESTS):
                self._impact(session, ctx, symbol, builder)
            if terms.intent is Intent.TESTS:
                self._coverage(session, ctx, symbol, builder)
            if terms.intent in (Intent.UNRESOLVED, Intent.CALLEES, Intent.IMPACT):
                self._unresolved(session, ctx, symbol, builder)
        for other in ranked[len(primary) : 6]:
            builder.add(
                Category.SOURCE_FACT,
                f"Also matching: `{other.qualified_name}` ({other.kind.lower()}) in `{other.path}`",
                path=other.path,
                start_line=other.start_line,
                end_line=_last_line(other),
                symbol_id=other.id,
                qualified_name=other.qualified_name,
            )
        if terms.intent is Intent.UNRESOLVED and not primary:
            self._repository_unresolved(session, ctx, builder)
        insufficient = not evidence
        return Answer(
            question=question,
            intent=terms.intent,
            commit_sha=ctx.snapshot.commit_sha,
            analysis_id=ctx.requested_run_id,
            answer_markdown=_compose(terms, evidence, primary),
            insufficient_evidence=insufficient,
            evidence=evidence,
            matched_symbols=[s.qualified_name for s in ranked[:10]],
        )

    # -- retrieval -------------------------------------------------------------------------

    def _rank_symbols(self, session: Session, ctx: RunContext, terms: Terms) -> list[q.SymbolRow]:
        scores: dict[int, float] = defaultdict(float)
        rows: dict[int, q.SymbolRow] = {}

        def consider(found: list[q.SymbolRow], weight: float) -> None:
            for row in found:
                rows[row.id] = row
                scores[row.id] += weight

        for ident in terms.identifiers:
            last = ident.rsplit(".", 1)[-1]
            exact, _ = q.find_symbols(session, ctx.run.id, query=last, limit=25, offset=0)
            consider([r for r in exact if r.name.lower() == last.lower()], 10)
            consider([r for r in exact if r.qualified_name.endswith(ident)], 8)
            consider([r for r in exact if r.name.lower() != last.lower()], 3)
        for path in terms.paths:
            found, _ = q.find_symbols(
                session, ctx.run.id, query=None, path=path, limit=50, offset=0
            )
            consider([r for r in found if r.kind == "MODULE"], 9)
            consider([r for r in found if r.kind != "MODULE"], 1)
        for word in terms.keywords:
            stem = word.lower()[: max(4, len(word) - 3)]
            consider(_keyword_matches(session, ctx.run.id, stem), 2)
        ordered = sorted(
            rows.values(),
            key=lambda r: (-scores[r.id], r.is_test, len(r.qualified_name), r.qualified_name),
        )
        return [r for r in ordered if scores[r.id] >= 2]

    def _definition(
        self, session: Session, ctx: RunContext, symbol: q.SymbolRow, builder: _EvidenceBuilder
    ) -> None:
        kind = symbol.kind.lower().replace("_", " ")
        builder.add(
            Category.SOURCE_FACT,
            f"`{symbol.qualified_name}` is a {kind} defined in `{symbol.path}` "
            # The docstring is repository text: it reaches a model only inside ``text``,
            # which the source-upload policy controls.
            f"lines {symbol.start_line}-{_last_line(symbol)}",
            path=symbol.path,
            start_line=symbol.start_line,
            end_line=_last_line(symbol),
            symbol_id=symbol.id,
            qualified_name=symbol.qualified_name,
            text=self._snippet(session, ctx, symbol),
        )

    def _snippet(self, session: Session, ctx: RunContext, symbol: q.SymbolRow) -> str | None:
        try:
            _, content = self.source_reader.read(session, ctx, symbol.file_id)
        except (NotFound, InvalidInput, Unavailable):
            return None
        lines = content.splitlines()
        first = symbol.start_line - 1
        last = min(_last_line(symbol), first + MAX_SNIPPET_LINES)
        return "\n".join(lines[first:last])

    def _relationships(
        self,
        session: Session,
        ctx: RunContext,
        symbol: q.SymbolRow,
        builder: _EvidenceBuilder,
        *,
        outgoing: bool,
    ) -> None:
        rows, total = q.neighbours(
            session,
            ctx.run.id,
            symbol.id,
            outgoing=outgoing,
            types=[EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.INHERITS],
            limit=15,
        )
        for row in rows:
            other = row.symbol
            verb = _VERBS.get(row.edge_type, "depends on")
            subject, obj = (symbol, other) if outgoing else (other, symbol)
            confidence = "" if row.confidence == "EXACT" else " (heuristic)"
            builder.add(
                Category.STATIC_RELATIONSHIP,
                f"`{subject.qualified_name}` {verb} `{obj.qualified_name}`{confidence}",
                path=other.path,
                start_line=other.start_line,
                end_line=_last_line(other),
                symbol_id=other.id,
                qualified_name=other.qualified_name,
            )
        if total > len(rows):
            builder.add(
                Category.STATIC_RELATIONSHIP,
                f"{total - len(rows)} further {'callees' if outgoing else 'callers'} of "
                f"`{symbol.qualified_name}` are not listed",
                symbol_id=symbol.id,
            )

    def _impact(
        self, session: Session, ctx: RunContext, symbol: q.SymbolRow, builder: _EvidenceBuilder
    ) -> None:
        result = propagate(
            q.SqlEdgeSource(session, ctx.run.id),
            [Seed(symbol.id, "QUESTION")],
            max_depth=6,
            max_nodes=300,
        )
        reached = q.symbols_by_ids(session, ctx.run.id, list(result.impacts))
        dependents = sorted(
            (i for n, i in result.impacts.items() if i.depth > 0 and n in reached),
            key=lambda i: (i.depth, reached[i.node].qualified_name),
        )
        for impact in dependents[:20]:
            other = reached[impact.node]
            path = " → ".join(
                [reached[e.src].qualified_name for e in reversed(impact.path) if e.src in reached]
            )
            label = "test " if other.is_test else ""
            note = " (via heuristic relationship)" if impact.heuristic else ""
            builder.add(
                Category.STATIC_RELATIONSHIP,
                f"{label}`{other.qualified_name}` depends on `{symbol.qualified_name}` at "
                f"distance {impact.depth}{note}; path: {path}",
                path=other.path,
                start_line=other.start_line,
                end_line=_last_line(other),
                symbol_id=other.id,
                qualified_name=other.qualified_name,
            )
        if result.truncated:
            builder.add(
                Category.UNCERTAINTY,
                f"Dependency traversal stopped at its limit ({result.truncation_reason}); "
                "more dependents may exist",
                symbol_id=symbol.id,
            )
        if not dependents:
            builder.add(
                Category.STATIC_RELATIONSHIP,
                f"No statically resolved dependents of `{symbol.qualified_name}` were found",
                symbol_id=symbol.id,
            )

    def _coverage(
        self, session: Session, ctx: RunContext, symbol: q.SymbolRow, builder: _EvidenceBuilder
    ) -> None:
        latest = session.execute(
            select(TestRun.id)
            .where(
                TestRun.snapshot_id == ctx.snapshot.id,
                TestRun.coverage.is_(True),
                TestRun.status == "COMPLETED",
            )
            .order_by(TestRun.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            builder.add(
                Category.UNCERTAINTY,
                "No coverage has been recorded for this commit; runtime evidence is unavailable",
            )
            return
        span = set(range(symbol.start_line, _last_line(symbol) + 1))
        rows = session.execute(
            select(CoverageRecord.test_node_id, CoverageRecord.lines).where(
                CoverageRecord.test_run_id == latest,
                CoverageRecord.path == symbol.path,
                CoverageRecord.test_node_id.is_not(None),
            )
        ).all()
        for node_id, lines in sorted(rows, key=lambda r: r[0] or ""):
            hit = sorted(span & set(lines or []))
            if hit:
                builder.add(
                    Category.RUNTIME_OBSERVATION,
                    f"Test `{node_id}` executed lines {hit[0]}-{hit[-1]} of "
                    f"`{symbol.qualified_name}` in test run {latest}",
                    path=symbol.path,
                    start_line=hit[0],
                    end_line=hit[-1],
                    symbol_id=symbol.id,
                    qualified_name=symbol.qualified_name,
                )

    def _unresolved(
        self, session: Session, ctx: RunContext, symbol: q.SymbolRow, builder: _EvidenceBuilder
    ) -> None:
        references, _ = q.outgoing_references(session, ctx.run.id, symbol.id, limit=50)
        for ref in references:
            if ref.resolution_status not in (
                ResolutionStatus.UNRESOLVED.value,
                ResolutionStatus.DYNAMIC.value,
            ):
                continue
            builder.add(
                Category.UNCERTAINTY,
                f"`{ref.raw_text}` in `{symbol.qualified_name}` is "
                f"{ref.resolution_status.lower()} ({ref.resolver_rule})",
                path=symbol.path,
                start_line=ref.start_line,
                end_line=ref.end_line,
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
            )

    def _repository_unresolved(
        self, session: Session, ctx: RunContext, builder: _EvidenceBuilder
    ) -> None:
        rows = session.execute(
            select(SymbolReference.resolution_status, SymbolReference.resolver_rule, func.count())
            .where(
                SymbolReference.analysis_run_id == ctx.run.id,
                SymbolReference.resolution_status.in_(["UNRESOLVED", "DYNAMIC"]),
            )
            .group_by(SymbolReference.resolution_status, SymbolReference.resolver_rule)
            .order_by(func.count().desc())
            .limit(10)
        ).all()
        for status, rule, count in rows:
            builder.add(
                Category.UNCERTAINTY,
                f"{count} references are {status.lower()} because of `{rule}`",
            )


class _EvidenceBuilder:
    def __init__(self, items: list[EvidenceItem]) -> None:
        self.items = items
        self.seen: set[str] = set()

    def add(self, category: Category, statement: str, **fields: Any) -> None:
        if statement in self.seen or len(self.items) >= 60:
            return
        self.seen.add(statement)
        self.items.append(
            EvidenceItem(
                id=f"E{len(self.items) + 1}", category=category, statement=statement, **fields
            )
        )


def _keyword_matches(session: Session, run_id: uuid.UUID, stem: str) -> list[q.SymbolRow]:
    escaped = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    ids = list(
        session.execute(
            select(Symbol.id)
            .join(SourceFile, SourceFile.id == Symbol.file_id)
            .where(
                Symbol.analysis_run_id == run_id,
                or_(
                    Symbol.name.ilike(pattern, escape="\\"),
                    Symbol.doc_summary.ilike(pattern, escape="\\"),
                    SourceFile.path.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(func.length(Symbol.qualified_name), Symbol.id)
            .limit(30)
        ).scalars()
    )
    return list(q.symbols_by_ids(session, run_id, ids).values())


def _last_line(symbol: q.SymbolRow) -> int:
    if symbol.end_col == 0 and symbol.end_line > symbol.start_line:
        return symbol.end_line - 1
    return symbol.end_line


_VERBS = {"CALLS": "calls", "REFERENCES": "references", "INHERITS": "inherits from"}


def _compose(terms: Terms, evidence: list[EvidenceItem], primary: list[q.SymbolRow]) -> str:
    if not evidence:
        looked_for = ", ".join(
            f"`{t}`" for t in [*terms.identifiers, *terms.paths, *terms.keywords]
        )
        return (
            "**Insufficient evidence.** No symbol or file in the analysed commit matched "
            + (looked_for or "the question")
            + ". Try naming a function, class, module or file path."
        )
    sections: list[str] = []
    titles = {
        Category.SOURCE_FACT: "Source facts",
        Category.STATIC_RELATIONSHIP: "Static relationships",
        Category.RUNTIME_OBSERVATION: "Runtime observations",
        Category.UNCERTAINTY: "Uncertainty",
    }
    for category, title in titles.items():
        items = [e for e in evidence if e.category is category]
        if items:
            lines = [f"- {e.statement} [{e.id}]" for e in items]
            sections.append(f"**{title}**\n" + "\n".join(lines))
    if primary and not any(e.category is Category.UNCERTAINTY for e in evidence):
        sections.append(
            "**Uncertainty**\n- Static analysis cannot see calls made through instance "
            "attributes, `getattr`, or other dynamic dispatch; such callers may be missing."
        )
    return "\n\n".join(sections)


def ask(
    service: QuestionService,
    session: Session,
    ctx: RunContext,
    question: str,
    provider: AnswerProvider | None,
) -> Answer:
    """Answer deterministically, then optionally let the model rephrase the same evidence."""
    answer = service.answer(session, ctx, question)
    if provider is None or answer.insufficient_evidence:
        return answer
    evidence = [asdict(e) for e in answer.evidence]
    try:
        explained = provider.explain(answer.question, evidence)
    except ProviderUnavailable as exc:
        answer.model_note = (
            f"Model explanation unavailable ({exc}); showing the evidence-based answer."
        )
        return answer
    answer.mode = "model"
    answer.model_explanation = explained.explanation
    answer.model_note = (
        f"Generated by {explained.model} from the evidence below; citations were checked "
        "against it. Verify important claims in the cited source."
    )
    return answer
