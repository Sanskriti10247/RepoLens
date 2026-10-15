"""Database-level guarantees for identity and integrity (real PostgreSQL)."""

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from repolens.domain.enums import (
    AnalysisStatus,
    FileMode,
    FileRole,
    JobKind,
    JobStatus,
    ReferenceKind,
    ResolutionStatus,
    SymbolKind,
    Visibility,
)
from repolens.persistence.db import transaction
from repolens.persistence.models import (
    AnalysisRun,
    Job,
    Repository,
    RepositorySnapshot,
    SourceFile,
    Symbol,
    SymbolReference,
)

pytestmark = pytest.mark.integration
SHA = "a" * 40


def _repo(session: Session) -> Repository:
    repo = Repository(
        provider="github",
        host="github.com",
        owner="octo",
        name=f"r{uuid.uuid4().hex[:6]}",
        canonical_url=f"https://github.com/octo/{uuid.uuid4().hex}",
    )
    session.add(repo)
    session.flush()
    return repo


def _run(session: Session, repo: Repository, snapshot: RepositorySnapshot | None) -> AnalysisRun:
    run = AnalysisRun(
        repository_id=repo.id,
        requested_ref="main",
        snapshot_id=snapshot.id if snapshot else None,
        parser_version="p1",
        engine_version="e1",
        config_fingerprint="c1",
    )
    session.add(run)
    session.flush()
    return run


def _expect_integrity_error(factory: sessionmaker[Session], fn: object) -> None:
    with pytest.raises(IntegrityError), transaction(factory) as session:
        fn(session)  # type: ignore[operator]


def test_snapshot_identity_is_repository_and_commit(
    session_factory: sessionmaker[Session],
) -> None:
    with transaction(session_factory) as session:
        repo = _repo(session)
        session.add(RepositorySnapshot(repository_id=repo.id, commit_sha=SHA))
        repo_id = repo.id

    def duplicate(session: Session) -> None:
        session.add(RepositorySnapshot(repository_id=repo_id, commit_sha=SHA))
        session.flush()

    _expect_integrity_error(session_factory, duplicate)


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, "a" * 41, "--upload-pack=x"])
def test_commit_sha_format_is_enforced(session_factory: sessionmaker[Session], sha: str) -> None:
    def insert(session: Session) -> None:
        repo = _repo(session)
        session.add(RepositorySnapshot(repository_id=repo.id, commit_sha=sha))
        session.flush()

    _expect_integrity_error(session_factory, insert)


def test_sha256_object_format_is_accepted(session_factory: sessionmaker[Session]) -> None:
    with transaction(session_factory) as session:
        repo = _repo(session)
        session.add(RepositorySnapshot(repository_id=repo.id, commit_sha="b" * 64))


def test_only_one_usable_result_per_analysis_key(session_factory: sessionmaker[Session]) -> None:
    with transaction(session_factory) as session:
        repo = _repo(session)
        snapshot = RepositorySnapshot(repository_id=repo.id, commit_sha=SHA)
        session.add(snapshot)
        session.flush()
        first = _run(session, repo, snapshot)
        first.status = AnalysisStatus.SUCCEEDED
        # Failed and queued runs for the same key are allowed (retries, in-flight requests).
        _run(session, repo, snapshot).status = AnalysisStatus.FAILED
        _run(session, repo, snapshot)
        ids = (repo.id, snapshot.id)

    def second_success(session: Session) -> None:
        repo = session.get(Repository, ids[0])
        snapshot = session.get(RepositorySnapshot, ids[1])
        assert repo and snapshot
        _run(session, repo, snapshot).status = AnalysisStatus.PARTIAL
        session.flush()

    _expect_integrity_error(session_factory, second_success)


def test_reused_runs_must_reference_their_source(session_factory: sessionmaker[Session]) -> None:
    def reused_without_source(session: Session) -> None:
        run = _run(session, _repo(session), None)
        run.status = AnalysisStatus.REUSED
        session.flush()

    _expect_integrity_error(session_factory, reused_without_source)


def test_resolved_references_need_a_target(session_factory: sessionmaker[Session]) -> None:
    def resolved_without_target(session: Session) -> None:
        repo = _repo(session)
        snapshot = RepositorySnapshot(repository_id=repo.id, commit_sha=SHA)
        session.add(snapshot)
        session.flush()
        run = _run(session, repo, snapshot)
        file = SourceFile(
            snapshot_id=snapshot.id,
            path="m.py",
            blob_sha="c" * 40,
            size_bytes=1,
            mode=FileMode.REGULAR,
            role=FileRole.SOURCE,
            language="python",
        )
        session.add(file)
        session.flush()
        module = Symbol(
            analysis_run_id=run.id,
            file_id=file.id,
            symbol_key="m:MODULE",
            qualified_name="m",
            name="m",
            kind=SymbolKind.MODULE,
            visibility=Visibility.PUBLIC,
            start_line=1,
            start_col=0,
            end_line=1,
            end_col=0,
        )
        session.add(module)
        session.flush()
        session.add(
            SymbolReference(
                analysis_run_id=run.id,
                file_id=file.id,
                source_symbol_id=module.id,
                ref_kind=ReferenceKind.CALL,
                raw_text="f()",
                start_line=1,
                start_col=0,
                end_line=1,
                end_col=3,
                resolution_status=ResolutionStatus.RESOLVED,
            )
        )
        session.flush()

    _expect_integrity_error(session_factory, resolved_without_target)


def test_active_jobs_require_a_lease(session_factory: sessionmaker[Session]) -> None:
    with transaction(session_factory) as session:
        job = Job(kind=JobKind.ANALYZE_REPOSITORY)
        session.add(job)
        session.flush()
        job_id = job.id

    def claim_without_lease(session: Session) -> None:
        session.execute(update(Job).where(Job.id == job_id).values(status=JobStatus.RUNNING))

    _expect_integrity_error(session_factory, claim_without_lease)


def test_idempotency_key_unique_only_while_active(
    session_factory: sessionmaker[Session],
) -> None:
    with transaction(session_factory) as session:
        session.add(Job(kind=JobKind.ANALYZE_REPOSITORY, idempotency_key="k1"))

    def duplicate_active(session: Session) -> None:
        session.add(Job(kind=JobKind.ANALYZE_REPOSITORY, idempotency_key="k1"))
        session.flush()

    _expect_integrity_error(session_factory, duplicate_active)

    with transaction(session_factory) as session:
        session.execute(
            update(Job).where(Job.idempotency_key == "k1").values(status=JobStatus.CANCELLED)
        )
        session.add(Job(kind=JobKind.ANALYZE_REPOSITORY, idempotency_key="k1"))


def test_invalid_enum_values_are_rejected(session_factory: sessionmaker[Session]) -> None:
    def bad_status(session: Session) -> None:
        session.add(Job(kind=JobKind.ANALYZE_REPOSITORY, status="DONE"))
        session.flush()

    _expect_integrity_error(session_factory, bad_status)
