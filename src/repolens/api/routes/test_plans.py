"""Test discovery and selection endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from repolens.api.dependencies import Authorized, SessionDep
from repolens.api.pagination import Pagination
from repolens.api.schemas import (
    PageMeta,
    TestCaseOut,
    TestCasePage,
    TestPlanCreate,
    TestPlanItemOut,
    TestPlanItemPage,
    TestPlanOut,
)
from repolens.persistence.models import TestCaseRecord, TestPlan, TestPlanItem
from repolens.services import graph as graph_service
from repolens.services import test_planning
from repolens.services.errors import NotFound
from repolens.test_selection.selection import Policy

router = APIRouter(tags=["test selection"])


@router.post(
    "/impact/{impact_id}/test-plan",
    response_model=TestPlanOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Authorized],
)
def create_test_plan(
    impact_id: uuid.UUID, body: TestPlanCreate, session: SessionDep
) -> TestPlanOut:
    """Select tests for a completed impact analysis. Every decision carries its evidence."""
    with session.begin():
        plan = test_planning.create_test_plan(
            session,
            impact_id,
            policy=Policy(body.policy),
            max_tests=body.max_tests,
            max_seconds=body.max_seconds,
        )
        plan_id = plan.id
    return TestPlanOut.model_validate(session.get(TestPlan, plan_id))


@router.get("/test-plans/{plan_id}", response_model=TestPlanOut)
def get_test_plan(plan_id: uuid.UUID, session: SessionDep) -> TestPlanOut:
    plan = session.get(TestPlan, plan_id)
    if plan is None:
        raise NotFound("Test plan not found")
    return TestPlanOut.model_validate(plan)


@router.get("/test-plans/{plan_id}/items", response_model=TestPlanItemPage)
def test_plan_items(
    plan_id: uuid.UUID, session: SessionDep, page: Pagination, selected: bool | None = None
) -> TestPlanItemPage:
    if session.get(TestPlan, plan_id) is None:
        raise NotFound("Test plan not found")
    stmt = select(TestPlanItem).where(TestPlanItem.plan_id == plan_id)
    if selected is not None:
        stmt = stmt.where(TestPlanItem.selected.is_(selected))
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = session.execute(
        stmt.order_by(TestPlanItem.rank).limit(page.limit).offset(page.offset)
    ).scalars()
    return TestPlanItemPage(
        items=[TestPlanItemOut.model_validate(i) for i in items],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )


@router.get("/analyses/{analysis_id}/tests", response_model=TestCasePage)
def list_tests(analysis_id: uuid.UUID, session: SessionDep, page: Pagination) -> TestCasePage:
    """Tests discovered statically in an analysis (sandboxed collection refines the status)."""
    ctx = graph_service.run_context_for_analysis(session, analysis_id)
    stmt = select(TestCaseRecord).where(TestCaseRecord.analysis_run_id == ctx.run.id)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = session.execute(
        stmt.order_by(TestCaseRecord.node_id).limit(page.limit).offset(page.offset)
    ).scalars()
    return TestCasePage(
        items=[TestCaseOut.model_validate(i) for i in items],
        page=PageMeta(total=total, limit=page.limit, offset=page.offset),
    )
