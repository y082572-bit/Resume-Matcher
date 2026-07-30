"""Integration/concurrency tests for Explicit Provenance Stage P6-C1b-A:
``ApprovedContentPackageSqlRepository.promote_current_authority`` -- exactly
one winner on every race, the loser always reporting a fresh state via a
brand-new ``Session``, zero retries, and cross-owner package promotion
always rejected.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.cv_document_models as models
from app.models import Base
from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentPackageBuildStatus,
)
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInputValidationResult,
    DocumentInputValidationStatus,
)
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_sql_repository import ApprovedContentPackageSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


def _force_valid(owner_key, document_input):
    """Patch ``validate_approved_content_document_input`` (as imported into
    the builder module) to unconditionally report ``VALID`` -- used only to
    build a second, genuinely different (different ``content_plan_fingerprint``),
    still schema/policy-v1-compliant package for the *same* owner without
    needing to reconstruct an entire second self-consistent P5.5 envelope.
    The SQL repository under test never calls
    ``validate_approved_content_document_input`` itself -- it independently
    re-verifies every fingerprint straight from the stored payload -- so
    this only isolates the builder's upstream P6-A freshness/consistency
    checks (already exhaustively covered by
    ``tests.unit.test_cv_document_input_validation``), never the repository
    behavior this test module is actually responsible for."""

    return patch(
        "app.services.cv_approved_content_package_builder.validate_approved_content_document_input",
        return_value=ApprovedContentDocumentInputValidationResult(
            status=DocumentInputValidationStatus.VALID,
            owner_key=owner_key,
            recomputed_document_input_fingerprint=document_input.document_input_fingerprint,
        ),
    )


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'authority_concurrency.db'}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    repo = ApprovedContentPackageSqlRepository(session_factory)
    return repo, session_factory, engine


def _register_owner(session_factory, owner_key) -> None:
    with session_factory() as session:
        session.add(
            models.CvDocumentArtifactOwner(
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                person_entity_id=str(owner_key.person_entity_id),
                owner_kind=owner_key.owner_kind.value,
                owner_reference_id=owner_key.owner_reference_id,
                owner_key_schema_version=owner_key.owner_key_schema_version,
            )
        )
        session.commit()


async def _build_and_save_package(repo, session_factory, *, package_policy_version: str | None = None):
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    _register_owner(session_factory, owner_key)
    kwargs = {}
    if package_policy_version is not None:
        kwargs["package_policy_version"] = package_policy_version
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input, **kwargs)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    save_result = repo.save_package(owner_key=owner_key, package=build_result.package)
    assert save_result.status.value in ("SAVED", "ALREADY_EXISTS_IDENTICAL")
    return owner_key, build_result.package, document_input


@pytest.mark.asyncio
async def test_concurrent_first_insert_exactly_one_winner(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_and_save_package(repo, session_factory)

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = repo.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
        )

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [r.status for r in results.values()]
    assert statuses.count(ApprovedContentAuthorityPromotionStatus.PROMOTED) == 1
    assert statuses.count(ApprovedContentAuthorityPromotionStatus.STALE_REVISION) == 1

    loser = next(r for r in results.values() if r.status == ApprovedContentAuthorityPromotionStatus.STALE_REVISION)
    assert loser.authority is not None
    assert loser.authority.slot_version == 1
    assert loser.authority.package_fingerprint == package.package_fingerprint


@pytest.mark.asyncio
async def test_concurrent_update_exactly_one_winner(env) -> None:
    repo, session_factory, _ = env
    owner_key, package_v1, document_input = await _build_and_save_package(repo, session_factory)
    first = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package_v1.package_fingerprint, expected_previous_slot_version=None
    )
    assert first.status == ApprovedContentAuthorityPromotionStatus.PROMOTED

    # ``cv_approved_content_packages`` hard-CHECKs package_schema_version/
    # package_policy_version to the exact frozen v1 literals -- a genuinely
    # different, still-persistable package for the *same* owner therefore
    # has to differ in plan/result content instead, never in schema/policy
    # version. ``content_plan_fingerprint`` is tampered directly (bypassing
    # ``validate_approved_content_document_input`` via ``_force_valid``,
    # since the SQL repository under test never calls it) to produce a
    # second package_fingerprint while keeping the owner identity intact.
    from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
    from app.services.cv_document_input_validation import compute_document_input_fingerprint

    tampered_plan = document_input.source_content_plan.model_copy(update={"content_plan_fingerprint": "9" * 64})
    recomputed_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=document_input.approved_content_result.result_fingerprint,
        content_plan_fingerprint=tampered_plan.content_plan_fingerprint,
        owner_kind=document_input.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=compute_approved_content_replay_input_fingerprint(
            document_input.current_p55_replay_input
        ),
    )
    tampered_input = document_input.model_copy(
        update={
            "source_content_plan": tampered_plan,
            "document_input_fingerprint": recomputed_document_input_fingerprint,
        }
    )
    with _force_valid(owner_key, tampered_input):
        v2_build = build_approved_content_package(owner_key=owner_key, document_input=tampered_input)
    assert v2_build.status == ApprovedContentPackageBuildStatus.BUILT
    package_v2 = v2_build.package
    assert package_v2.package_fingerprint != package_v1.package_fingerprint
    save_v2 = repo.save_package(owner_key=owner_key, package=package_v2)
    assert save_v2.status.value == "SAVED"

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = repo.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package_v2.package_fingerprint, expected_previous_slot_version=1
        )

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = [r.status for r in results.values()]
    assert statuses.count(ApprovedContentAuthorityPromotionStatus.PROMOTED) == 1
    assert statuses.count(ApprovedContentAuthorityPromotionStatus.STALE_REVISION) == 1

    loser = next(r for r in results.values() if r.status == ApprovedContentAuthorityPromotionStatus.STALE_REVISION)
    assert loser.authority.slot_version == 2
    assert loser.authority.package_fingerprint == package_v2.package_fingerprint


@pytest.mark.asyncio
async def test_loser_reread_uses_a_fresh_session_never_the_failed_writers(env) -> None:
    """A real thread race on ``expected_previous_slot_version=None`` almost
    always resolves through the *CAS-precondition* check alone (the loser's
    own ``current_row`` read already observes the winner's committed row,
    so it returns ``STALE_REVISION`` without ever attempting an INSERT) --
    SQLite's file-level write locking serializes the two transactions
    tightly enough that the narrower "both readers pass the None-check
    before either commits" window is not reliably reproducible from real
    thread timing alone. To deterministically exercise the genuine
    INTEGRITY-error -> fresh-Session-reread path this stage's contract
    requires, the very first ``Session.get`` lookup for the authority row
    is made to miss once (standing in for a commit that raced past it),
    forcing a real primary-key ``IntegrityError`` on the INSERT attempt."""

    from sqlalchemy.orm import Session

    repo, session_factory, _ = env
    owner_key, package, _ = await _build_and_save_package(repo, session_factory)

    # A concurrent writer already promoted this owner's authority row.
    first = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert first.status == ApprovedContentAuthorityPromotionStatus.PROMOTED

    observed_sessions: list[object] = []
    real_factory = repo._session_factory

    def _tracking_factory():
        session = real_factory()
        observed_sessions.append(session)
        return session

    repo._session_factory = _tracking_factory

    real_get = Session.get
    miss_budget = {"remaining": 1}

    def _get_that_misses_the_authority_row_once(self, entity, ident, *args, **kwargs):
        if entity is models.CvApprovedContentCurrentAuthorityRow and miss_budget["remaining"] > 0:
            miss_budget["remaining"] -= 1
            return None
        return real_get(self, entity, ident, *args, **kwargs)

    with patch.object(Session, "get", _get_that_misses_the_authority_row_once):
        result = repo.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
        )

    # The forced-miss attempt believed the row was absent (expected=None
    # matched what it saw) and tried an INSERT that collided on the real PK
    # -- a genuine IntegrityError, resolved via a fresh reread reporting the
    # true current state, never a retried write.
    assert result.status == ApprovedContentAuthorityPromotionStatus.STALE_REVISION
    assert result.authority is not None
    assert result.authority.slot_version == 1

    # At least two distinct Session objects were opened for this single
    # call: the failing writer and the fresh reread inside
    # ``_reread_authority_after_race`` -- never the same Session reused
    # after rollback.
    assert len(observed_sessions) >= 2
    assert len({id(session) for session in observed_sessions}) == len(observed_sessions)


@pytest.mark.asyncio
async def test_cross_owner_package_promotion_is_rejected(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_and_save_package(repo, session_factory)
    other_owner_key, other_package, _ = await _build_and_save_package(repo, session_factory)

    result = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=other_package.package_fingerprint, expected_previous_slot_version=None
    )
    assert result.status == ApprovedContentAuthorityPromotionStatus.OWNER_PACKAGE_MISMATCH
    assert result.authority is None

    # And the reverse direction too -- never a one-directional check.
    result_reverse = repo.promote_current_authority(
        owner_key=other_owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert result_reverse.status == ApprovedContentAuthorityPromotionStatus.OWNER_PACKAGE_MISMATCH
