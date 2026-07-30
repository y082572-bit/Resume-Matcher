"""Unit tests for Explicit Provenance Stage P6-C1b-A:
``ApprovedContentPackageRepository`` Protocol conformance and the in-memory
implementation.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentAuthorityReadStatus,
    ApprovedContentPackageBuildStatus,
    ApprovedContentPackageSaveStatus,
)
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_repository_protocol import (
    ApprovedContentPackageRepository,
    InMemoryApprovedContentPackageRepository,
)
from app.services.cv_approved_content_package_sql_repository import ApprovedContentPackageSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


async def _build_package(*, package_policy_version: str | None = None):
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
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input, **kwargs)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    return owner_key, build_result.package, document_input


async def _build_second_package_for_same_owner(owner_key, document_input):
    """A genuinely different package (different ``package_fingerprint``) for
    the *same* owner -- a policy-version bump changes the fingerprint
    without changing the owner identity, exercising a real CAS update."""

    build_result = build_approved_content_package(
        owner_key=owner_key,
        document_input=document_input,
        package_policy_version="cv-approved-content-package-policy-v2",
    )
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    return build_result.package


def test_in_memory_repository_satisfies_runtime_protocol() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    assert isinstance(repo, ApprovedContentPackageRepository)


def test_sql_repository_and_protocol_share_identical_method_signatures() -> None:
    for name in ("save_package", "get_package_by_fingerprint", "read_current_authority", "promote_current_authority"):
        protocol_sig = inspect.signature(getattr(ApprovedContentPackageRepository, name))
        sql_sig = inspect.signature(getattr(ApprovedContentPackageSqlRepository, name))
        in_memory_sig = inspect.signature(getattr(InMemoryApprovedContentPackageRepository, name))
        assert protocol_sig.parameters.keys() == sql_sig.parameters.keys(), name
        assert protocol_sig.parameters.keys() == in_memory_sig.parameters.keys(), name


@pytest.mark.asyncio
async def test_save_owner_not_found() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()

    result = repo.save_package(owner_key=owner_key, package=package)
    assert result.status == ApprovedContentPackageSaveStatus.OWNER_NOT_FOUND


@pytest.mark.asyncio
async def test_save_owner_mismatch() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    other_owner_key, _, _ = await _build_package()
    repo.register_owner(other_owner_key)

    result = repo.save_package(owner_key=other_owner_key, package=package)
    assert result.status == ApprovedContentPackageSaveStatus.OWNER_MISMATCH


@pytest.mark.asyncio
async def test_save_replay_is_identical() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)

    first = repo.save_package(owner_key=owner_key, package=package)
    second = repo.save_package(owner_key=owner_key, package=package)

    assert first.status == ApprovedContentPackageSaveStatus.SAVED
    assert second.status == ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL
    assert second.package == package


@pytest.mark.asyncio
async def test_save_fingerprint_conflict() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)

    conflicting = package.model_copy(update={"payload_byte_size": package.payload_byte_size + 1})
    assert conflicting.package_fingerprint == package.package_fingerprint
    assert conflicting != package

    first = repo.save_package(owner_key=owner_key, package=package)
    second = repo.save_package(owner_key=owner_key, package=conflicting)

    assert first.status == ApprovedContentPackageSaveStatus.SAVED
    assert second.status == ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
    assert second.package is None


@pytest.mark.asyncio
async def test_save_and_read_return_defensive_copies() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)

    saved = repo.save_package(owner_key=owner_key, package=package)
    assert saved.package is not package

    read_back = repo.get_package_by_fingerprint(package.package_fingerprint)
    assert read_back.package is not saved.package
    assert read_back.package == package


@pytest.mark.asyncio
async def test_authority_absent_when_never_promoted() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)
    repo.save_package(owner_key=owner_key, package=package)

    result = repo.read_current_authority(owner_key)
    assert result.status == ApprovedContentAuthorityReadStatus.NOT_FOUND
    assert result.authority is None


@pytest.mark.asyncio
async def test_authority_cas_insert_then_update() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package_v1, document_input = await _build_package()
    repo.register_owner(owner_key)
    repo.save_package(owner_key=owner_key, package=package_v1)

    insert_result = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package_v1.package_fingerprint, expected_previous_slot_version=None
    )
    assert insert_result.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
    assert insert_result.authority.package_fingerprint == package_v1.package_fingerprint
    assert insert_result.authority.slot_version == 1

    package_v2 = await _build_second_package_for_same_owner(owner_key, document_input)
    assert package_v2.package_fingerprint != package_v1.package_fingerprint
    repo.save_package(owner_key=owner_key, package=package_v2)

    update_result = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package_v2.package_fingerprint, expected_previous_slot_version=1
    )
    assert update_result.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
    assert update_result.authority.package_fingerprint == package_v2.package_fingerprint
    assert update_result.authority.slot_version == 2


@pytest.mark.asyncio
async def test_authority_promotion_rejects_a_package_belonging_to_another_owner() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    other_owner_key, other_package, _ = await _build_package()
    repo.register_owner(owner_key)
    repo.register_owner(other_owner_key)
    repo.save_package(owner_key=owner_key, package=package)
    repo.save_package(owner_key=other_owner_key, package=other_package)

    result = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=other_package.package_fingerprint, expected_previous_slot_version=None
    )
    assert result.status == ApprovedContentAuthorityPromotionStatus.OWNER_PACKAGE_MISMATCH


@pytest.mark.asyncio
async def test_authority_already_current_is_a_no_op() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)
    repo.save_package(owner_key=owner_key, package=package)
    repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )

    again = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=1
    )
    assert again.status == ApprovedContentAuthorityPromotionStatus.ALREADY_CURRENT
    assert again.authority.slot_version == 1


@pytest.mark.asyncio
async def test_authority_stale_revision_on_wrong_expected_slot_version() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)
    repo.save_package(owner_key=owner_key, package=package)
    repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )

    stale = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert stale.status == ApprovedContentAuthorityPromotionStatus.STALE_REVISION
    assert stale.authority is not None
    assert stale.authority.slot_version == 1


@pytest.mark.asyncio
async def test_authority_cas_is_thread_safe() -> None:
    repo = InMemoryApprovedContentPackageRepository()
    owner_key, package, _ = await _build_package()
    repo.register_owner(owner_key)
    repo.save_package(owner_key=owner_key, package=package)

    results: list[ApprovedContentAuthorityPromotionStatus] = []
    lock = threading.Lock()

    def _attempt() -> None:
        outcome = repo.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
        )
        with lock:
            results.append(outcome.status)

    threads = [threading.Thread(target=_attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(ApprovedContentAuthorityPromotionStatus.PROMOTED) == 1
    assert results.count(ApprovedContentAuthorityPromotionStatus.STALE_REVISION) == 15

    final_authority = repo.read_current_authority(owner_key)
    assert final_authority.authority.slot_version == 1
