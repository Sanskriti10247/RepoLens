"""Evidence-grounded repository questions."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Request

from repolens.api.dependencies import Authorized, SessionDep
from repolens.api.schemas import AnswerOut, EvidenceOut, QuestionCreate
from repolens.services import graph as graph_service
from repolens.services import questions

router = APIRouter(tags=["questions"])


@router.post(
    "/repositories/{repository_id}/questions",
    response_model=AnswerOut,
    dependencies=[Authorized],
)
def ask_question(
    repository_id: uuid.UUID, body: QuestionCreate, session: SessionDep, request: Request
) -> AnswerOut:
    """Answer from analysis evidence. Each claim cites evidence with path, lines and commit."""
    ctx = graph_service.results_run(session, repository_id, body.analysis_id)
    provider = request.app.state.answer_provider if body.use_model else None
    answer = questions.ask(
        request.app.state.question_service, session, ctx, body.question, provider
    )
    return AnswerOut(
        question=answer.question,
        intent=answer.intent.value,
        analysis_id=answer.analysis_id,
        commit_sha=answer.commit_sha,
        mode=answer.mode,
        answer_markdown=answer.answer_markdown,
        model_explanation=answer.model_explanation,
        model_note=answer.model_note,
        insufficient_evidence=answer.insufficient_evidence,
        matched_symbols=answer.matched_symbols,
        evidence=[
            EvidenceOut(
                **{**asdict(e), "category": e.category.value, "commit_sha": answer.commit_sha}
            )
            for e in answer.evidence
        ],
    )
