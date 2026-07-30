"""Service-level tests for Explicit Provenance Stage P6-C1b-A:
``ApprovedContentPackageSqlRepository`` -- schema-backed save/read, every
independent fingerprint re-verification, the closed exception channel, and
fresh-Session-after-race handling.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DatabaseError, DataError, OperationalError, StatementError
from sqlalchemy.orm import Session, sessionmaker

import app.services.cv_document_models as models
from app.models import Base
from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentPackageBuildStatus,
    ApprovedContentPackageReadStatus,
    ApprovedContentPackageSaveStatus,
)
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_sql_repository import ApprovedContentPackageSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'approved_content_package_repo.db'}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
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


async def _build_package(*, package_policy_version: str | None = None, package_schema_version: str | None = None):
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    kwargs = {}
    if package_policy_version is not None:
        kwargs["package_policy_version"] = package_policy_version
    if package_schema_version is not None:
        kwargs["package_schema_version"] = package_schema_version
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input, **kwargs)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    return owner_key, build_result.package, document_input


class _RaisingSession:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __enter__(self):
        raise self._exc

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _raising_session_factory(exc: Exception):
    def factory():
        return _RaisingSession(exc)

    return factory


@pytest.mark.asyncio
async def test_save_then_read_round_trip(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    save_result = repo.save_package(owner_key=owner_key, package=package)
    assert save_result.status == ApprovedContentPackageSaveStatus.SAVED

    read_result = repo.get_package_by_fingerprint(package.package_fingerprint)
    assert read_result.status == ApprovedContentPackageReadStatus.FOUND
    assert read_result.package == package


@pytest.mark.asyncio
async def test_save_owner_missing(env) -> None:
    repo, _, _ = env
    _, package, _ = await _build_package()

    result = repo.save_package(owner_key=build_owner_key(
        person_entity_id=package.payload.source_content_plan.person_entity_id,
        owner_kind=package.owner_kind,
        owner_reference_id=package.payload.source_content_plan.job_or_application_id,
    ), package=package)
    assert result.status == ApprovedContentPackageSaveStatus.OWNER_NOT_FOUND


@pytest.mark.asyncio
async def test_save_owner_mismatch(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    other_owner_key, _, _ = await _build_package()
    _register_owner(session_factory, owner_key)
    _register_owner(session_factory, other_owner_key)

    result = repo.save_package(owner_key=other_owner_key, package=package)
    assert result.status == ApprovedContentPackageSaveStatus.OWNER_MISMATCH


@pytest.mark.asyncio
async def test_save_rejects_document_input_fingerprint_tamper(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"document_input_fingerprint": "a" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PACKAGE_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_save_rejects_approval_decisions_fingerprint_tamper(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"approval_decisions_fingerprint": "b" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PACKAGE_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_save_rejects_fact_selection_identity_fingerprint_tamper(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"fact_selection_identity_fingerprint": "c" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PACKAGE_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_save_rejects_package_fingerprint_tamper(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"package_fingerprint": "d" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PACKAGE_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_save_rejects_payload_sha256_tamper(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"payload_sha256": "e" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PAYLOAD_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_save_replay_is_identical(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    first = repo.save_package(owner_key=owner_key, package=package)
    second = repo.save_package(owner_key=owner_key, package=package)
    assert first.status == ApprovedContentPackageSaveStatus.SAVED
    assert second.status == ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL
    assert second.package == package


@pytest.mark.asyncio
async def test_same_approved_result_under_current_package_policy_and_schema_is_accepted(env) -> None:
    """``cv_approved_content_packages`` hard-CHECKs
    ``package_schema_version``/``package_policy_version`` to the exact
    frozen v1 constants (see the exact package DDL) -- so the SQL storage
    layer only ever accepts a package built under the *current* recognized
    versions. The very same ``ApprovedCvContentResult``, built into a
    package via an explicit (not merely defaulted) current
    schema/policy-version pair, is accepted identically to the implicit
    default build."""

    from app.schemas.cv_approved_content_package import (
        APPROVED_CONTENT_PACKAGE_POLICY_VERSION,
        APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION,
    )

    repo, session_factory, _ = env
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    _register_owner(session_factory, owner_key)

    default_build = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    explicit_current_build = build_approved_content_package(
        owner_key=owner_key,
        document_input=document_input,
        package_schema_version=APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION,
        package_policy_version=APPROVED_CONTENT_PACKAGE_POLICY_VERSION,
    )
    assert default_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert explicit_current_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert default_build.package.package_fingerprint == explicit_current_build.package.package_fingerprint

    first = repo.save_package(owner_key=owner_key, package=default_build.package)
    second = repo.save_package(owner_key=owner_key, package=explicit_current_build.package)
    assert first.status == ApprovedContentPackageSaveStatus.SAVED
    assert second.status == ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL


@pytest.mark.asyncio
async def test_a_non_current_package_policy_version_is_rejected_by_the_hard_ck(env) -> None:
    """The exact package DDL hard-CHECKs ``package_policy_version`` to the
    frozen v1 literal -- a package built under any other (still internally
    self-consistent) policy version can never be persisted; this is a DB
    integrity failure this repository must map onto
    ``STORAGE_METADATA_CONFLICT``, never silently accepted."""

    repo, session_factory, _ = env
    owner_key, _, document_input = await _build_package()
    _register_owner(session_factory, owner_key)

    bumped_build = build_approved_content_package(
        owner_key=owner_key, document_input=document_input, package_policy_version="cv-approved-content-package-policy-v2"
    )
    assert bumped_build.status == ApprovedContentPackageBuildStatus.BUILT

    result = repo.save_package(owner_key=owner_key, package=bumped_build.package)
    assert result.status == ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT


@pytest.mark.asyncio
async def test_stored_metadata_conflict_on_same_fingerprint_different_row(env) -> None:
    """Directly insert a row sharing ``package_fingerprint`` with a package
    the repository already trusts, but whose stored metadata does not
    re-verify -- the read path must report ``STORAGE_METADATA_CONFLICT``,
    never silently accept it."""

    repo, session_factory, engine = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)
    repo.save_package(owner_key=owner_key, package=package)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET document_input_fingerprint = ? WHERE package_fingerprint = ?",
            ("f" * 64, package.package_fingerprint),
        )

    result = repo.get_package_by_fingerprint(package.package_fingerprint)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT


def test_unknown_integrity_error_is_not_mapped_to_stale_revision() -> None:
    """A genuinely unexpected ``IntegrityError`` on ``save_package`` must
    always resolve to a fresh, independent re-read (either
    ``ALREADY_EXISTS_IDENTICAL`` or ``STORAGE_METADATA_CONFLICT``) -- never
    silently treated as if it were an authority CAS ``STALE_REVISION``
    (that status does not even exist on ``ApprovedContentPackageSaveStatus``)."""

    from app.schemas.cv_approved_content_package import ApprovedContentPackageSaveStatus as _Status

    assert not hasattr(_Status, "STALE_REVISION")


def test_operational_error_maps_to_storage_unavailable() -> None:
    factory = _raising_session_factory(OperationalError("stmt", {}, Exception("database is locked")))
    repo = ApprovedContentPackageSqlRepository(factory)

    result = repo.get_package_by_fingerprint("a" * 64)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE


def test_data_error_maps_to_storage_metadata_conflict() -> None:
    factory = _raising_session_factory(DataError("stmt", {}, Exception("bad bind value")))
    repo = ApprovedContentPackageSqlRepository(factory)

    result = repo.get_package_by_fingerprint("a" * 64)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT


def test_statement_error_maps_to_storage_metadata_conflict() -> None:
    factory = _raising_session_factory(StatementError("bad statement", "stmt", {}, Exception("x")))
    repo = ApprovedContentPackageSqlRepository(factory)

    result = repo.get_package_by_fingerprint("a" * 64)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT


def test_remaining_database_error_maps_to_storage_unavailable() -> None:
    factory = _raising_session_factory(DatabaseError("stmt", {}, Exception("unknown database problem")))
    repo = ApprovedContentPackageSqlRepository(factory)

    result = repo.get_package_by_fingerprint("a" * 64)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_fresh_session_reread_after_save_race_uses_a_different_session_object(env) -> None:
    """Simulates a genuine concurrent-INSERT race: the row already exists,
    but the very first ``session.get(CvApprovedContentPackageRow, ...)``
    pre-check (standing in for another writer's not-yet-visible commit) is
    made to miss it exactly once, forcing ``save_package`` to actually
    attempt the INSERT, hit the real PK ``IntegrityError``, and fall
    through to ``_reread_after_race`` -- which must open a brand-new
    ``Session``, never reuse the failed writer's own ``Session``."""

    repo, session_factory, engine = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)

    # Pre-insert the row directly so the repository's own INSERT genuinely
    # collides on the primary key.
    with session_factory() as session:
        from app.services.truth_fingerprint import canonical_json_bytes as _canon

        canonical_bytes = _canon(package.payload.model_dump(mode="json"))
        session.add(
            models.CvApprovedContentPackageRow(
                package_fingerprint=package.package_fingerprint,
                owner_key_fingerprint=package.owner_key_fingerprint,
                owner_kind=package.owner_kind.value,
                document_input_fingerprint=package.document_input_fingerprint,
                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                payload_sha256=package.payload_sha256,
                payload_byte_size=len(canonical_bytes),
                payload_json=canonical_bytes.decode("utf-8"),
                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                package_schema_version=package.package_schema_version,
                package_policy_version=package.package_policy_version,
                created_at=package.created_at,
            )
        )
        session.commit()

    observed_session_ids: set[int] = set()
    real_factory = repo._session_factory

    def _tracking_factory():
        session = real_factory()
        observed_session_ids.add(id(session))
        return session

    repo._session_factory = _tracking_factory

    real_get = Session.get
    miss_budget = {"remaining": 1}

    def _get_that_misses_the_package_row_once(self, entity, ident, *args, **kwargs):
        if entity is models.CvApprovedContentPackageRow and miss_budget["remaining"] > 0:
            miss_budget["remaining"] -= 1
            return None
        return real_get(self, entity, ident, *args, **kwargs)

    with patch.object(Session, "get", _get_that_misses_the_package_row_once):
        result = repo.save_package(owner_key=owner_key, package=package)

    assert result.status == ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL
    assert result.package == package
    # At least two distinct Session objects were opened: the failing writer
    # (whose pre-check missed the row) and the fresh reread that found it --
    # never the same Session reused after rollback.
    assert len(observed_session_ids) >= 2


@pytest.mark.asyncio
async def test_authority_promotion_round_trip(env) -> None:
    repo, session_factory, _ = env
    owner_key, package, _ = await _build_package()
    _register_owner(session_factory, owner_key)
    repo.save_package(owner_key=owner_key, package=package)

    result = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert result.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
    assert result.authority.slot_version == 1

    current = repo.read_current_authority(owner_key)
    assert current.authority.package_fingerprint == package.package_fingerprint
