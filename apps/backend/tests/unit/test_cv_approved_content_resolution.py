"""Unit tests for Explicit Provenance Stage P6-C1b-B: the Approved Content
Resolution composition root (``resolve_current_approved_content_document_input``).

Covers the schema's closed status/result consistency, owner-key
self-consistency (rejected before any repository read), the exact stable
A1 -> package -> A2 observation sequence and every one of its status
mappings, the deeper owner/package binding checks, flat-payload
reconstruction with independent fingerprint recomputation, mandatory
document-input re-validation, concurrency/ABA fail-closed behavior, a real
``InMemoryApprovedContentPackageRepository``, and a real SQLite-backed
``ApprovedContentPackageSqlRepository``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.cv_document_models as models
from app.models import Base
from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentAuthorityReadResult,
    ApprovedContentAuthorityReadStatus,
    ApprovedContentCurrentAuthority,
    ApprovedContentPackageBuildStatus,
    ApprovedContentPackageReadResult,
    ApprovedContentPackageReadStatus,
    ApprovedContentPackageSaveStatus,
)
from app.schemas.cv_approved_content_resolution import (
    ApprovedContentResolutionResult,
    ApprovedContentResolutionStatus,
)
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInputValidationResult,
    ArtifactOwnerKind,
    DocumentInputValidationStatus,
    JobArtifactOwnerKey,
)
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_repository_protocol import InMemoryApprovedContentPackageRepository
from app.services.cv_approved_content_package_sql_repository import ApprovedContentPackageSqlRepository
from app.services.cv_approved_content_resolution import resolve_current_approved_content_document_input
from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
from app.services.cv_document_input_validation import compute_document_input_fingerprint, recompute_content_plan_fingerprint
from app.services.cv_document_owner_identity import build_owner_key
from app.services.truth_fingerprint import canonical_json_bytes
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


# -- shared test doubles ------------------------------------------------------


class _NeverCalledRepository:
    """A repository stub that fails the test loudly if the resolver ever
    reaches a repository call -- used to prove owner-key self-consistency is
    rejected strictly before the first read."""

    def read_current_authority(self, owner_key):
        raise AssertionError("read_current_authority must not be called")

    def get_package_by_fingerprint(self, package_fingerprint):
        raise AssertionError("get_package_by_fingerprint must not be called")

    def save_package(self, *, owner_key, package):
        raise AssertionError("save_package must not be called")

    def promote_current_authority(self, *, owner_key, package_fingerprint, expected_previous_slot_version):
        raise AssertionError("promote_current_authority must not be called")


class _ScriptedRepository:
    """A repository stub returning pre-built canned results, used to drive
    the resolver through exact A1/package/A2 status combinations that a real
    repository's own integrity checks would otherwise never allow to occur
    together (a dangling authority, a package whose payload disagrees with
    its own stored owner binding, etc.) -- exercising the resolver's own
    independent defenses in isolation from any one repository's guarantees."""

    def __init__(self, *, authority_results, package_result=None):
        self._authority_results = list(authority_results)
        self._package_result = package_result
        self.read_current_authority_calls = 0
        self.get_package_by_fingerprint_calls = 0

    def read_current_authority(self, owner_key):
        index = min(self.read_current_authority_calls, len(self._authority_results) - 1)
        self.read_current_authority_calls += 1
        return self._authority_results[index]

    def get_package_by_fingerprint(self, package_fingerprint):
        self.get_package_by_fingerprint_calls += 1
        assert self._package_result is not None
        return self._package_result

    def save_package(self, *, owner_key, package):
        raise AssertionError("not exercised by this test")

    def promote_current_authority(self, *, owner_key, package_fingerprint, expected_previous_slot_version):
        raise AssertionError("not exercised by this test")


class _HookedRepository:
    """Wraps a real repository and counts calls, firing an optional hook
    immediately before the package read or immediately before the *second*
    ``read_current_authority`` call -- used to inject a real authority
    promotion in between the resolver's own reads."""

    def __init__(self, inner, *, before_package_read=None, before_second_authority_read=None):
        self._inner = inner
        self._before_package_read = before_package_read
        self._before_second_authority_read = before_second_authority_read
        self.read_current_authority_calls = 0
        self.get_package_by_fingerprint_calls = 0

    def read_current_authority(self, owner_key):
        self.read_current_authority_calls += 1
        if self.read_current_authority_calls == 2 and self._before_second_authority_read is not None:
            self._before_second_authority_read()
        return self._inner.read_current_authority(owner_key)

    def get_package_by_fingerprint(self, package_fingerprint):
        if self._before_package_read is not None:
            self._before_package_read()
        self.get_package_by_fingerprint_calls += 1
        return self._inner.get_package_by_fingerprint(package_fingerprint)

    def save_package(self, *, owner_key, package):
        return self._inner.save_package(owner_key=owner_key, package=package)

    def promote_current_authority(self, *, owner_key, package_fingerprint, expected_previous_slot_version):
        return self._inner.promote_current_authority(
            owner_key=owner_key,
            package_fingerprint=package_fingerprint,
            expected_previous_slot_version=expected_previous_slot_version,
        )


def _found_authority(package_fingerprint: str, slot_version: int) -> ApprovedContentAuthorityReadResult:
    return ApprovedContentAuthorityReadResult(
        status=ApprovedContentAuthorityReadStatus.FOUND,
        authority=ApprovedContentCurrentAuthority(package_fingerprint=package_fingerprint, slot_version=slot_version),
    )


async def _build_owner_and_package():
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    return owner_key, build_result.package, document_input


async def _build_second_package_for_same_owner(owner_key, document_input):
    build_result = build_approved_content_package(
        owner_key=owner_key,
        document_input=document_input,
        package_policy_version="cv-approved-content-package-policy-v2",
    )
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    return build_result.package


def _in_memory_repo_with_current_package(owner_key, package):
    repo = InMemoryApprovedContentPackageRepository()
    repo.register_owner(owner_key)
    saved = repo.save_package(owner_key=owner_key, package=package)
    assert saved.status == ApprovedContentPackageSaveStatus.SAVED
    promoted = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert promoted.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
    return repo


# -- SCHEMA --------------------------------------------------------------------


def test_resolution_status_exact_members() -> None:
    assert {member.value for member in ApprovedContentResolutionStatus} == {
        "FOUND",
        "NOT_FOUND",
        "STALE_AUTHORITY_OBSERVATION",
        "OWNER_MISMATCH",
        "DOCUMENT_INPUT_FINGERPRINT_MISMATCH",
        "DOCUMENT_INPUT_INVALID",
        "STORAGE_METADATA_CONFLICT",
        "STORAGE_UNAVAILABLE",
    }


def test_found_requires_all_success_fields() -> None:
    with pytest.raises(Exception):
        ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.FOUND)


def test_failure_status_forbids_every_success_field() -> None:
    for status in ApprovedContentResolutionStatus:
        if status == ApprovedContentResolutionStatus.FOUND:
            continue
        with pytest.raises(Exception):
            ApprovedContentResolutionResult(status=status, observed_package_fingerprint="a" * 64)
        with pytest.raises(Exception):
            ApprovedContentResolutionResult(status=status, observed_authority_slot_version=1)


def test_invalid_fingerprint_rejected() -> None:
    with pytest.raises(Exception):
        ApprovedContentResolutionResult(
            status=ApprovedContentResolutionStatus.NOT_FOUND, observed_package_fingerprint="not-a-hash"
        )


def test_invalid_slot_version_rejected() -> None:
    with pytest.raises(Exception):
        ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.NOT_FOUND, observed_authority_slot_version=0)


# -- OWNER KEY ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        lambda k: k.model_copy(update={"owner_key_fingerprint": "0" * 64}),
        lambda k: k.model_copy(update={"owner_kind": ArtifactOwnerKind.APPLICATION}),
        lambda k: k.model_copy(update={"person_entity_id": uuid4()}),
        lambda k: k.model_copy(update={"owner_reference_id": "some-other-reference"}),
    ],
    ids=["forged_fingerprint", "owner_kind_inconsistency", "person_identity_inconsistency", "owner_reference_inconsistency"],
)
def test_owner_key_self_inconsistency_is_rejected_before_any_repository_call(tamper) -> None:
    base = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-resolution-owner-key"
    )
    tampered = tamper(base)

    result = resolve_current_approved_content_document_input(owner_key=tampered, repository=_NeverCalledRepository())

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


def test_valid_self_consistent_owner_reaches_the_repository() -> None:
    owner_key = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-resolution-owner-key"
    )
    repo = InMemoryApprovedContentPackageRepository()
    repo.register_owner(owner_key)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.status == ApprovedContentResolutionStatus.NOT_FOUND


# -- HAPPY PATH (also covers IN-MEMORY: saved+promoted resolves FOUND) ---------


@pytest.mark.asyncio
async def test_happy_path_resolves_found_with_exact_reconstructed_input() -> None:
    owner_key, package, document_input = await _build_owner_and_package()
    repo = _in_memory_repo_with_current_package(owner_key, package)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.status == ApprovedContentResolutionStatus.FOUND
    assert result.document_input == document_input
    assert result.document_input.document_input_fingerprint == package.document_input_fingerprint
    assert result.observed_package_fingerprint == package.package_fingerprint
    assert result.observed_authority_slot_version == 1


@pytest.mark.asyncio
async def test_happy_path_returns_deep_defensive_copies() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _in_memory_repo_with_current_package(owner_key, package)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.status == ApprovedContentResolutionStatus.FOUND
    assert result.document_input is not package.payload
    assert result.document_input.source_content_plan is not package.payload.source_content_plan
    assert result.document_input.approved_content_result is not package.payload.approved_content_result


# -- AUTHORITY A1 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("authority_status", "expected_status"),
    [
        (ApprovedContentAuthorityReadStatus.NOT_FOUND, ApprovedContentResolutionStatus.NOT_FOUND),
        (ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT, ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT),
        (ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE, ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE),
    ],
)
def test_a1_status_mapping_short_circuits_before_any_package_read(authority_status, expected_status) -> None:
    owner_key = build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-a1")
    repo = _ScriptedRepository(authority_results=[ApprovedContentAuthorityReadResult(status=authority_status)])

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=expected_status)
    assert repo.read_current_authority_calls == 1
    assert repo.get_package_by_fingerprint_calls == 0


# -- PACKAGE --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("package_status", "expected_status"),
    [
        (ApprovedContentPackageReadStatus.NOT_FOUND, ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT),
        (ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT, ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT),
        (ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE, ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE),
    ],
)
def test_package_status_mapping_after_a1_found(package_status, expected_status) -> None:
    owner_key = build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-package")
    repo = _ScriptedRepository(
        authority_results=[_found_authority("a" * 64, 1)],
        package_result=ApprovedContentPackageReadResult(status=package_status),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=expected_status)
    assert repo.read_current_authority_calls == 1
    assert repo.get_package_by_fingerprint_calls == 1


# -- OWNER BINDING --------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_binding_package_owner_key_fingerprint_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered = package.model_copy(update={"owner_key_fingerprint": "0" * 64})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)
    assert repo.read_current_authority_calls == 1


@pytest.mark.asyncio
async def test_owner_binding_package_owner_kind_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered = package.model_copy(update={"owner_kind": ArtifactOwnerKind.APPLICATION})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


@pytest.mark.asyncio
async def test_owner_binding_payload_owner_kind_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered_payload = package.payload.model_copy(update={"owner_kind": ArtifactOwnerKind.APPLICATION})
    tampered = package.model_copy(update={"payload": tampered_payload})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


@pytest.mark.asyncio
async def test_owner_binding_plan_person_entity_id_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered_plan = package.payload.source_content_plan.model_copy(update={"person_entity_id": uuid4()})
    tampered_payload = package.payload.model_copy(update={"source_content_plan": tampered_plan})
    tampered = package.model_copy(update={"payload": tampered_payload})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


@pytest.mark.asyncio
async def test_owner_binding_plan_job_or_application_id_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered_plan = package.payload.source_content_plan.model_copy(update={"job_or_application_id": "some-other-job"})
    tampered_payload = package.payload.model_copy(update={"source_content_plan": tampered_plan})
    tampered = package.model_copy(update={"payload": tampered_payload})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


# -- AUTHORITY A2 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_unchanged_resolves_found() -> None:
    owner_key, package, document_input = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.status == ApprovedContentResolutionStatus.FOUND
    assert result.document_input == document_input
    assert repo.read_current_authority_calls == 2
    assert repo.get_package_by_fingerprint_calls == 1


@pytest.mark.asyncio
async def test_a2_changed_package_fingerprint_is_stale() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority("f" * 64, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)


@pytest.mark.asyncio
async def test_a2_changed_slot_version_is_stale() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 2)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)


@pytest.mark.asyncio
async def test_a2_not_found_is_stale() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[
            _found_authority(package.package_fingerprint, 1),
            ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.NOT_FOUND),
        ],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)


@pytest.mark.asyncio
async def test_a2_metadata_conflict() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[
            _found_authority(package.package_fingerprint, 1),
            ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT),
        ],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)


@pytest.mark.asyncio
async def test_a2_storage_unavailable() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    repo = _ScriptedRepository(
        authority_results=[
            _found_authority(package.package_fingerprint, 1),
            ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE),
        ],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE)
    assert repo.read_current_authority_calls == 2
    assert repo.get_package_by_fingerprint_calls == 1


# -- DOCUMENT INPUT --------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_document_input_fingerprint_mismatch() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    tampered = package.model_copy(update={"document_input_fingerprint": "1" * 64})
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.DOCUMENT_INPUT_FINGERPRINT_MISMATCH)


@pytest.mark.asyncio
async def test_document_input_invalid_stale_freshness_family() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    payload = package.payload
    stale_current_p55 = payload.current_p55_replay_input.model_copy(
        update={"approval_decision_fingerprints": ("0" * 64,)}
    )
    new_current_p55_fp = compute_approved_content_replay_input_fingerprint(stale_current_p55)
    new_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=payload.approved_content_result.result_fingerprint,
        content_plan_fingerprint=payload.source_content_plan.content_plan_fingerprint,
        owner_kind=owner_key.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=new_current_p55_fp,
    )
    tampered_payload = payload.model_copy(update={"current_p55_replay_input": stale_current_p55})
    tampered = package.model_copy(
        update={"payload": tampered_payload, "document_input_fingerprint": new_document_input_fingerprint}
    )
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.DOCUMENT_INPUT_INVALID)


@pytest.mark.asyncio
async def test_document_input_invalid_strategy_fields_incomplete_family() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    payload = package.payload
    plan = payload.source_content_plan

    tampered_plan = plan.model_copy(update={"role_strategy_context_fingerprint": None})
    self_consistent_plan_fingerprint = recompute_content_plan_fingerprint(tampered_plan)
    tampered_plan = tampered_plan.model_copy(update={"content_plan_fingerprint": self_consistent_plan_fingerprint})

    current_p55_fp = compute_approved_content_replay_input_fingerprint(payload.current_p55_replay_input)
    new_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=payload.approved_content_result.result_fingerprint,
        content_plan_fingerprint=self_consistent_plan_fingerprint,
        owner_kind=owner_key.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_fp,
    )
    tampered_payload = payload.model_copy(update={"source_content_plan": tampered_plan})
    tampered = package.model_copy(
        update={"payload": tampered_payload, "document_input_fingerprint": new_document_input_fingerprint}
    )
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.DOCUMENT_INPUT_INVALID)


@pytest.mark.asyncio
async def test_document_input_invalid_result_carries_no_raw_violation_detail() -> None:
    owner_key, package, _ = await _build_owner_and_package()
    payload = package.payload
    plan = payload.source_content_plan

    tampered_plan = plan.model_copy(update={"role_strategy_context_fingerprint": None})
    self_consistent_plan_fingerprint = recompute_content_plan_fingerprint(tampered_plan)
    tampered_plan = tampered_plan.model_copy(update={"content_plan_fingerprint": self_consistent_plan_fingerprint})
    current_p55_fp = compute_approved_content_replay_input_fingerprint(payload.current_p55_replay_input)
    new_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=payload.approved_content_result.result_fingerprint,
        content_plan_fingerprint=self_consistent_plan_fingerprint,
        owner_kind=owner_key.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_fp,
    )
    tampered_payload = payload.model_copy(update={"source_content_plan": tampered_plan})
    tampered = package.model_copy(
        update={"payload": tampered_payload, "document_input_fingerprint": new_document_input_fingerprint}
    )
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=tampered),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.model_dump() == {
        "status": "DOCUMENT_INPUT_INVALID",
        "document_input": None,
        "observed_package_fingerprint": None,
        "observed_authority_slot_version": None,
    }


@pytest.mark.asyncio
async def test_validation_owner_mismatch_is_caught_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if ``validate_approved_content_document_input`` itself reports
    ``VALID``, the resolver must independently re-compare the returned
    ``owner_key`` against its own caller-supplied one. In legitimate use
    these two owner keys are always identical (both are deterministic
    functions of the very same three identity fields already proven
    consistent above), so this defense-in-depth branch is exercised here by
    substituting a stubbed validator that returns a genuinely different
    owner key."""

    owner_key, package, document_input = await _build_owner_and_package()
    other_owner_key = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="a-different-owner"
    )

    def _fake_validate(_document_input):
        return ApprovedContentDocumentInputValidationResult(
            status=DocumentInputValidationStatus.VALID,
            owner_key=other_owner_key,
            recomputed_document_input_fingerprint=document_input.document_input_fingerprint,
        )

    monkeypatch.setattr(
        "app.services.cv_approved_content_resolution.validate_approved_content_document_input", _fake_validate
    )
    repo = _ScriptedRepository(
        authority_results=[_found_authority(package.package_fingerprint, 1), _found_authority(package.package_fingerprint, 1)],
        package_result=ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package),
    )

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.OWNER_MISMATCH)


# -- CONCURRENCY ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_promotion_between_a1_and_package_read_is_stale() -> None:
    owner_key, package_v1, document_input = await _build_owner_and_package()
    inner = _in_memory_repo_with_current_package(owner_key, package_v1)
    package_v2 = await _build_second_package_for_same_owner(owner_key, document_input)
    saved_v2 = inner.save_package(owner_key=owner_key, package=package_v2)
    assert saved_v2.status == ApprovedContentPackageSaveStatus.SAVED

    def _promote_v2():
        promoted = inner.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package_v2.package_fingerprint, expected_previous_slot_version=1
        )
        assert promoted.status == ApprovedContentAuthorityPromotionStatus.PROMOTED

    hooked = _HookedRepository(inner, before_package_read=_promote_v2)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=hooked)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)
    assert hooked.read_current_authority_calls == 2
    assert hooked.get_package_by_fingerprint_calls == 1


@pytest.mark.asyncio
async def test_concurrency_promotion_between_package_read_and_a2_is_stale() -> None:
    owner_key, package_v1, document_input = await _build_owner_and_package()
    inner = _in_memory_repo_with_current_package(owner_key, package_v1)
    package_v2 = await _build_second_package_for_same_owner(owner_key, document_input)
    inner.save_package(owner_key=owner_key, package=package_v2)

    def _promote_v2():
        promoted = inner.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package_v2.package_fingerprint, expected_previous_slot_version=1
        )
        assert promoted.status == ApprovedContentAuthorityPromotionStatus.PROMOTED

    hooked = _HookedRepository(inner, before_second_authority_read=_promote_v2)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=hooked)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)
    assert hooked.read_current_authority_calls == 2
    assert hooked.get_package_by_fingerprint_calls == 1


@pytest.mark.asyncio
async def test_concurrency_aba_package_pointer_with_higher_slot_version_is_stale() -> None:
    """The authority pointer moves v1(slot1) -> v2(slot2) -> v1(slot3)
    between the package read and A2 -- ``package_fingerprint`` alone would
    wrongly read as "unchanged" (v1 == v1), so only the paired
    ``(package_fingerprint, slot_version)`` comparison catches this."""

    owner_key, package_v1, document_input = await _build_owner_and_package()
    inner = _in_memory_repo_with_current_package(owner_key, package_v1)
    package_v2 = await _build_second_package_for_same_owner(owner_key, document_input)
    inner.save_package(owner_key=owner_key, package=package_v2)

    def _aba_mutation():
        first = inner.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package_v2.package_fingerprint, expected_previous_slot_version=1
        )
        assert first.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
        second = inner.promote_current_authority(
            owner_key=owner_key, package_fingerprint=package_v1.package_fingerprint, expected_previous_slot_version=2
        )
        assert second.status == ApprovedContentAuthorityPromotionStatus.PROMOTED
        assert second.authority.slot_version == 3

    hooked = _HookedRepository(inner, before_second_authority_read=_aba_mutation)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=hooked)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)
    assert hooked.read_current_authority_calls == 2
    assert hooked.get_package_by_fingerprint_calls == 1


# -- IN-MEMORY --------------------------------------------------------------------


def test_in_memory_absent_authority_resolves_not_found() -> None:
    owner_key = build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-in-memory")
    repo = InMemoryApprovedContentPackageRepository()
    repo.register_owner(owner_key)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.NOT_FOUND)


# -- SQLITE -----------------------------------------------------------------------


@pytest.fixture
def sql_env(tmp_path: Path):
    db_path = tmp_path / "approved_content_resolution.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    repo = ApprovedContentPackageSqlRepository(session_factory)
    return repo, session_factory, engine, db_path


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


@pytest.mark.asyncio
async def test_sqlite_saved_and_promoted_package_resolves_found(sql_env) -> None:
    repo, session_factory, _, _ = sql_env
    owner_key, package, document_input = await _build_owner_and_package()
    _register_owner(session_factory, owner_key)
    saved = repo.save_package(owner_key=owner_key, package=package)
    assert saved.status == ApprovedContentPackageSaveStatus.SAVED
    promoted = repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    assert promoted.status == ApprovedContentAuthorityPromotionStatus.PROMOTED

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result.status == ApprovedContentResolutionStatus.FOUND
    assert result.document_input == document_input
    assert result.observed_package_fingerprint == package.package_fingerprint
    assert result.observed_authority_slot_version == 1


@pytest.mark.asyncio
async def test_sqlite_restart_persistence(sql_env) -> None:
    repo, session_factory, engine, db_path = sql_env
    owner_key, package, document_input = await _build_owner_and_package()
    _register_owner(session_factory, owner_key)
    repo.save_package(owner_key=owner_key, package=package)
    repo.promote_current_authority(
        owner_key=owner_key, package_fingerprint=package.package_fingerprint, expected_previous_slot_version=None
    )
    engine.dispose()

    restarted_engine = create_engine(f"sqlite:///{db_path}", future=True)
    restarted_session_factory = sessionmaker(restarted_engine, expire_on_commit=False)
    restarted_repo = ApprovedContentPackageSqlRepository(restarted_session_factory)

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=restarted_repo)

    assert result.status == ApprovedContentResolutionStatus.FOUND
    assert result.document_input == document_input
    restarted_engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_dangling_authority_fails_closed(sql_env) -> None:
    """The schema makes a dangling authority pointer impossible to write
    through any conforming code path -- both a ``BEFORE INSERT`` trigger
    (``trg_cv_approved_content_authority_insert_package_match``) and a real
    composite ``FOREIGN KEY`` on ``(owner_key_fingerprint,
    current_package_fingerprint)`` block it. Both are bypassed here, for one
    raw ``sqlite3`` connection only, purely to construct the corrupt-store
    state the resolver's *read* path must still fail closed against
    (bit-rot, a manual edit, or a future write path lacking these guards are
    all indistinguishable from the resolver's point of view)."""

    import sqlite3

    from app.services.cv_approved_content_package_schema_manifest import TRG_AUTHORITY_INSERT_PACKAGE_MATCH

    repo, session_factory, _, db_path = sql_env
    owner_key, package, _ = await _build_owner_and_package()
    _register_owner(session_factory, owner_key)

    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(f"DROP TRIGGER {TRG_AUTHORITY_INSERT_PACKAGE_MATCH}")
        raw.execute(
            "INSERT INTO cv_approved_content_current_authority "
            "(owner_key_fingerprint, current_package_fingerprint, slot_version, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (owner_key.owner_key_fingerprint, "f" * 64, 1, "2026-01-01T00:00:00+00:00"),
        )
        raw.commit()
    finally:
        raw.close()

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)


@pytest.mark.asyncio
async def test_sqlite_corrupted_package_fails_closed(sql_env) -> None:
    """The schema's immutability trigger blocks any ``UPDATE`` of an
    already-saved package row, so corruption is instead written directly at
    ``INSERT`` time (never through ``save_package``) -- a legitimate
    ``INSERT`` of a package row whose stored ``payload_sha256`` never
    matched its own ``payload_json`` in the first place, which the
    authority-insert trigger's existence/owner check does not (and is not
    meant to) detect."""

    repo, session_factory, _, _ = sql_env
    owner_key, package, _ = await _build_owner_and_package()
    _register_owner(session_factory, owner_key)
    with session_factory() as session:
        session.add(
            models.CvApprovedContentPackageRow(
                package_fingerprint=package.package_fingerprint,
                owner_key_fingerprint=package.owner_key_fingerprint,
                owner_kind=package.owner_kind.value,
                document_input_fingerprint=package.document_input_fingerprint,
                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                payload_sha256="0" * 64,
                payload_byte_size=package.payload_byte_size,
                payload_json=canonical_json_bytes(package.payload.model_dump(mode="json")).decode("utf-8"),
                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                package_schema_version=package.package_schema_version,
                package_policy_version=package.package_policy_version,
                created_at=package.created_at,
            )
        )
        session.commit()
    with session_factory() as session:
        session.add(
            models.CvApprovedContentCurrentAuthorityRow(
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                current_package_fingerprint=package.package_fingerprint,
                slot_version=1,
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()

    result = resolve_current_approved_content_document_input(owner_key=owner_key, repository=repo)

    assert result == ApprovedContentResolutionResult(status=ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)
