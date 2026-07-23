"""Unit tests for Explicit Provenance Stage P6-A:
``build_validated_docx_snapshot`` and manual document confirmation.

Uses a real PRE-P4 -> P3(strategy) -> P4(``ROLE_STRATEGY_INTEGRATED``) ->
P5a -> P5.5 pipeline (``EXACT_COPY``, no LLM call), via the shared
``build_ready_integrated_context``/``build_document_input`` factory in
``test_cv_document_input_validation.py`` -- P6-A accepts no other plan
mode -- plus deterministic fake renderer/template-provider/structural-
validator implementations -- P6-A ships no real python-docx/OOXML
validator.
"""

from __future__ import annotations

import hashlib

import pytest

from app.schemas.cv_document_artifact import (
    DocumentProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    DocxStructuralViolationCode,
    SnapshotBuildStatus,
)
from app.services.cv_document_adapters import DocxTemplateHandle
from app.services.cv_document_proposal_builder import build_current_docx_proposal
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository
from app.services.cv_document_snapshot_builder import build_manual_document_snapshot_confirmation, build_validated_docx_snapshot
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


VALIDATION_POLICY_VERSION = "cv-document-structural-validation-policy-test-v1"
RENDERING_POLICY_VERSION = "cv-document-rendering-policy-test-v1"

build_ready_context = build_ready_integrated_context


class _FakeTemplateProvider:
    def __init__(self, template_bytes: bytes = b"FAKE-DOCX-TEMPLATE-BYTES-V1") -> None:
        self._template_bytes = template_bytes

    def get_template(self) -> DocxTemplateHandle:
        return DocxTemplateHandle(
            template_bytes=self._template_bytes,
            template_fingerprint=hashlib.sha256(self._template_bytes).hexdigest(),
            template_adapter_id="fake-template-provider-v1",
        )


class _FakeRenderer:
    renderer_adapter_id = "fake-renderer-v1"

    def render(self, *, approved_content_result, content_plan, template_bytes, template_fingerprint, rendering_policy_version):
        from app.schemas.cv_document_artifact import CvDocxRenderingResult, DocxRenderingStatus

        output = template_bytes + b"::" + approved_content_result.result_fingerprint.encode()
        return CvDocxRenderingResult(
            status=DocxRenderingStatus.SUCCEEDED,
            renderer_adapter_id=self.renderer_adapter_id,
            rendering_policy_version=rendering_policy_version,
            docx_bytes=output,
            output_sha256=hashlib.sha256(output).hexdigest(),
        )


class _FakeStructuralValidator:
    def __init__(self, fail: bool = False, tamper_validated_hash: bool = False) -> None:
        self._fail = fail
        self._tamper = tamper_validated_hash

    def validate(self, *, exact_bytes: bytes, expected_sha256: str, validation_policy_version: str) -> DocxStructuralValidationResult:
        if self._fail:
            return DocxStructuralValidationResult(
                status=DocxStructuralValidationStatus.INVALID,
                validated_sha256=expected_sha256,
                violations=(DocxStructuralViolationCode.EMPTY_BODY,),
                diagnostics=("forced structural failure for test",),
            )
        validated_hash = ("0" * 64) if self._tamper else expected_sha256
        return DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=validated_hash)


async def _seed_proposal(repository: InMemoryCvDocumentArtifactRepository, ctx: dict):
    document_input = build_document_input(ctx)
    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status.value == "GENERATED"
    return result.artifact.owner_key


@pytest.mark.asyncio
async def test_unmodified_document_gets_pipeline_provenance() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )

    assert result.status == SnapshotBuildStatus.SNAPSHOT_CREATED
    assert result.snapshot.provenance_mode == DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT
    assert result.snapshot.manual_confirmation_fingerprint is None


@pytest.mark.asyncio
async def test_user_edited_document_without_confirmation_is_blocked() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    repository._current_proposal_bytes[owner_key.owner_key_fingerprint] += b"-edited-by-user"

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert result.status == SnapshotBuildStatus.MANUAL_CONFIRMATION_REQUIRED
    assert result.snapshot is None


@pytest.mark.asyncio
async def test_user_edited_document_with_confirmation_for_wrong_hash_is_blocked() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    repository._current_proposal_bytes[owner_key.owner_key_fingerprint] += b"-edited-by-user"

    proposal = repository.get_current_proposal(owner_key)
    wrong_confirmation = build_manual_document_snapshot_confirmation(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256="0" * 64,  # wrong hash
    )

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
        manual_confirmation=wrong_confirmation,
    )
    assert result.status == SnapshotBuildStatus.MANUAL_CONFIRMATION_MISMATCH


@pytest.mark.asyncio
async def test_confirmation_for_older_revision_is_blocked() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    proposal_v1 = repository.get_current_proposal(owner_key)

    # regenerate to bump revision to 2
    document_input = build_document_input(ctx)
    regen = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert regen.status.value == "GENERATED"

    edited_bytes = repository._current_proposal_bytes[owner_key.owner_key_fingerprint] + b"-edited"
    repository._current_proposal_bytes[owner_key.owner_key_fingerprint] = edited_bytes
    exact_sha256 = hashlib.sha256(edited_bytes).hexdigest()

    stale_confirmation = build_manual_document_snapshot_confirmation(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal_v1.artifact_fingerprint,  # stale: revision 1
        proposal_revision=proposal_v1.proposal_revision,
        exact_docx_sha256=exact_sha256,
    )

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
        manual_confirmation=stale_confirmation,
    )
    assert result.status == SnapshotBuildStatus.MANUAL_CONFIRMATION_MISMATCH


@pytest.mark.asyncio
async def test_exact_confirmation_creates_manual_snapshot() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    edited_bytes = repository._current_proposal_bytes[owner_key.owner_key_fingerprint] + b"-edited-by-user"
    repository._current_proposal_bytes[owner_key.owner_key_fingerprint] = edited_bytes
    exact_sha256 = hashlib.sha256(edited_bytes).hexdigest()

    proposal = repository.get_current_proposal(owner_key)
    confirmation = build_manual_document_snapshot_confirmation(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=exact_sha256,
    )

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
        manual_confirmation=confirmation,
    )
    assert result.status == SnapshotBuildStatus.SNAPSHOT_CREATED
    assert result.snapshot.provenance_mode == DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT
    assert result.snapshot.manual_confirmation_fingerprint == confirmation.confirmation_fingerprint
    # never claims semantic equivalence with ApprovedCvContent -- only carries
    # the fingerprint linkage, never an "equivalent" flag
    assert "semantically_equivalent" not in result.snapshot.model_dump(mode="json")


@pytest.mark.asyncio
async def test_snapshot_binds_exact_sha256() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    proposal_bytes = repository.read_current_proposal_bytes(owner_key)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert result.snapshot.exact_docx_sha256 == hashlib.sha256(proposal_bytes).hexdigest()


@pytest.mark.asyncio
async def test_snapshot_bytes_are_immutable_after_later_proposal_change() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    snapshot_sha256 = result.snapshot.exact_docx_sha256
    stored = repository.retrieve_snapshot(snapshot_sha256)
    assert stored is not None
    original_bytes = stored[1]

    # regenerate the proposal -- a completely new revision/bytes
    document_input = build_document_input(ctx)
    build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )

    still_stored = repository.retrieve_snapshot(snapshot_sha256)
    assert still_stored is not None
    assert still_stored[1] == original_bytes


@pytest.mark.asyncio
async def test_structural_validation_failure_blocks() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(fail=True),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert result.status == SnapshotBuildStatus.STRUCTURAL_VALIDATION_FAILED
    assert result.snapshot is None


@pytest.mark.asyncio
async def test_hash_changed_during_validation_blocks() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(tamper_validated_hash=True),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert result.status == SnapshotBuildStatus.HASH_CHANGED_DURING_VALIDATION


@pytest.mark.asyncio
async def test_repository_reread_is_verified() -> None:
    """Simulates a repository whose re-read snapshot bytes have been
    corrupted -- ``build_validated_docx_snapshot`` must catch this via its
    own post-save re-read integrity check."""

    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    class _CorruptingRepository(InMemoryCvDocumentArtifactRepository):
        def retrieve_snapshot(self, exact_docx_sha256):
            stored = super().retrieve_snapshot(exact_docx_sha256)
            if stored is None:
                return None
            snapshot, _bytes = stored
            return snapshot, b"corrupted-on-reread"

    corrupting_repository = _CorruptingRepository()
    corrupting_repository._current_proposal = repository._current_proposal
    corrupting_repository._current_proposal_bytes = repository._current_proposal_bytes

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=corrupting_repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert result.status == SnapshotBuildStatus.REPOSITORY_INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_different_content_under_same_locator_is_blocked() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)
    proposal_bytes = repository.read_current_proposal_bytes(owner_key)
    exact_sha256 = hashlib.sha256(proposal_bytes).hexdigest()

    from app.schemas.cv_document_artifact import ValidatedCvDocxSnapshot
    from app.services.cv_document_repository_protocol import SnapshotSaveStatus

    proposal = repository.get_current_proposal(owner_key)
    fake_snapshot = ValidatedCvDocxSnapshot(
        owner_key=owner_key,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=exact_sha256,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version=VALIDATION_POLICY_VERSION,
        structural_validation_result=DocxStructuralValidationResult(
            status=DocxStructuralValidationStatus.VALID, validated_sha256=exact_sha256
        ),
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint="f" * 64,
    )
    first_save = repository.save_validated_snapshot(fake_snapshot, proposal_bytes)
    assert first_save.status == SnapshotSaveStatus.SAVED

    different_bytes_claiming_same_locator = proposal_bytes + b"-different"
    fake_snapshot_2 = fake_snapshot.model_copy(update={"snapshot_fingerprint": "e" * 64})
    # deliberately mismatched: exact_docx_sha256 still points at the old locator
    second_save = repository.save_validated_snapshot(fake_snapshot_2, different_bytes_claiming_same_locator)
    assert second_save.status == SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH


# -- Repository immutability / defensive copying (REPOSITORY_CAS_OR_ATOMICITY_FALSE) --


@pytest.mark.asyncio
async def test_retrieve_snapshot_never_returns_same_object_identity() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    exact_sha256 = result.snapshot.exact_docx_sha256

    first = repository.retrieve_snapshot(exact_sha256)
    second = repository.retrieve_snapshot(exact_sha256)
    assert first is not None and second is not None
    first_snapshot, _ = first
    second_snapshot, _ = second
    assert first_snapshot == second_snapshot
    assert first_snapshot is not second_snapshot


@pytest.mark.asyncio
async def test_retrieved_snapshot_is_frozen_and_model_copy_does_not_leak() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key = await _seed_proposal(repository, ctx)

    result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    exact_sha256 = result.snapshot.exact_docx_sha256

    stored_snapshot, _ = repository.retrieve_snapshot(exact_sha256)
    with pytest.raises(Exception):
        stored_snapshot.proposal_revision = 999

    locally_modified = stored_snapshot.model_copy(update={"proposal_revision": 999})
    assert locally_modified.proposal_revision == 999

    still_stored, _ = repository.retrieve_snapshot(exact_sha256)
    assert still_stored.proposal_revision == result.snapshot.proposal_revision
