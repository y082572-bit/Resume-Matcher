"""Unit tests for Explicit Provenance Stage P6-A: ``build_and_confirm_pdf``.

Uses a real PRE-P4 -> P3(strategy) -> P4(``ROLE_STRATEGY_INTEGRATED``) ->
P5a -> P5.5 pipeline (``EXACT_COPY``, no LLM call), via the shared
``build_ready_integrated_context``/``build_document_input`` factory in
``test_cv_document_input_validation.py`` -- P6-A accepts no other plan
mode -- plus deterministic fake renderer/template-provider/structural-
validator/PDF-conversion-adapter implementations -- P6-A ships no real PDF
converter.
"""

from __future__ import annotations

import hashlib

import pytest

from app.schemas.cv_document_artifact import (
    CvPdfConversionAttemptResult,
    DocumentProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    PdfConfirmationBuildStatus,
    PdfConversionFailureCode,
    PdfConversionStatus,
)
from app.services.cv_document_adapters import DocxTemplateHandle
from app.services.cv_document_pdf_confirmation_builder import build_and_confirm_pdf
from app.services.cv_document_proposal_builder import build_current_docx_proposal
from app.services.cv_document_repository_protocol import CasReplaceStatus, InMemoryCvDocumentArtifactRepository
from app.services.cv_document_snapshot_builder import build_validated_docx_snapshot
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


VALIDATION_POLICY_VERSION = "cv-document-structural-validation-policy-test-v1"
RENDERING_POLICY_VERSION = "cv-document-rendering-policy-test-v1"
CONVERSION_POLICY_VERSION = "cv-document-pdf-conversion-policy-test-v1"

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
    def validate(self, *, exact_bytes: bytes, expected_sha256: str, validation_policy_version: str) -> DocxStructuralValidationResult:
        return DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=expected_sha256)


class _FakePdfConversionAdapter:
    adapter_id = "fake-pdf-adapter-v1"

    def __init__(self, fail: bool = False, tamper_hash: bool = False) -> None:
        self._fail = fail
        self._tamper = tamper_hash

    def convert(self, *, snapshot_bytes: bytes, snapshot_fingerprint: str, conversion_policy_version: str) -> CvPdfConversionAttemptResult:
        if self._fail:
            return CvPdfConversionAttemptResult(
                status=PdfConversionStatus.FAILED,
                adapter_id=self.adapter_id,
                conversion_policy_version=conversion_policy_version,
                failure_code=PdfConversionFailureCode.ADAPTER_ERROR,
                diagnostics=("forced conversion failure for test",),
                attempt_fingerprint=hashlib.sha256(b"forced-failure" + snapshot_fingerprint.encode()).hexdigest(),
            )
        output = b"%PDF-fake::" + snapshot_fingerprint.encode()
        real_hash = hashlib.sha256(output).hexdigest()
        claimed_hash = ("0" * 64) if self._tamper else real_hash
        return CvPdfConversionAttemptResult(
            status=PdfConversionStatus.SUCCEEDED,
            adapter_id=self.adapter_id,
            conversion_policy_version=conversion_policy_version,
            pdf_bytes=output,
            pdf_sha256=claimed_hash,
            attempt_fingerprint=hashlib.sha256(output).hexdigest(),
        )


async def _seed_snapshot(repository: InMemoryCvDocumentArtifactRepository, ctx: dict):
    document_input = build_document_input(ctx)
    proposal_result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = proposal_result.artifact.owner_key
    snapshot_result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert snapshot_result.status.value == "SNAPSHOT_CREATED"
    return owner_key, snapshot_result.snapshot


@pytest.mark.asyncio
async def test_pdf_requires_validated_snapshot() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    other_repository = InMemoryCvDocumentArtifactRepository()  # snapshot never saved here
    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=other_repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.SNAPSHOT_NOT_FOUND


@pytest.mark.asyncio
async def test_snapshot_mismatch_blocks() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)
    tampered_snapshot = snapshot.model_copy(update={"snapshot_fingerprint": "0" * 64})

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=tampered_snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.SNAPSHOT_MISMATCH


@pytest.mark.asyncio
async def test_conversion_failure_creates_no_artifact() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(fail=True),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.CONVERSION_FAILED
    assert result.artifact is None
    assert result.attempt is not None
    assert result.attempt.status == PdfConversionStatus.FAILED
    assert repository.get_current_pdf(owner_key) is None


@pytest.mark.asyncio
async def test_conversion_failure_does_not_remove_existing_pdf() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    first = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == PdfConfirmationBuildStatus.CONFIRMED

    failed = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(fail=True),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert failed.status == PdfConfirmationBuildStatus.CONVERSION_FAILED
    current = repository.get_current_pdf(owner_key)
    assert current.artifact_fingerprint == first.artifact.artifact_fingerprint
    assert current.pdf_revision == 1


@pytest.mark.asyncio
async def test_tampered_pdf_hash_blocks() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(tamper_hash=True),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.TAMPERED_PDF_HASH
    assert result.artifact is None


@pytest.mark.asyncio
async def test_first_pdf_creates_current() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.CONFIRMED
    assert result.artifact.pdf_revision == 1
    assert repository.get_current_pdf(owner_key).artifact_fingerprint == result.artifact.artifact_fingerprint


@pytest.mark.asyncio
async def test_second_pdf_replaces_current_and_supersedes_first() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    first = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == PdfConfirmationBuildStatus.CONFIRMED

    second = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version="different-conversion-policy-v2",
        expected_previous_revision=1,
    )
    assert second.status == PdfConfirmationBuildStatus.CONFIRMED
    assert second.artifact.pdf_revision == 2

    current = repository.get_current_pdf(owner_key)
    assert current.artifact_fingerprint == second.artifact.artifact_fingerprint
    assert current.superseded is False

    history = repository._pdf_history[owner_key.owner_key_fingerprint]
    assert len(history) == 1
    assert history[0].artifact_fingerprint == first.artifact.artifact_fingerprint
    assert history[0].superseded is True


@pytest.mark.asyncio
async def test_pdf_replace_does_not_change_proposal() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)
    proposal_before = repository.get_current_proposal(owner_key)

    build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )

    proposal_after = repository.get_current_proposal(owner_key)
    assert proposal_after == proposal_before


@pytest.mark.asyncio
async def test_pipeline_pdf_carries_pipeline_provenance() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)
    assert snapshot.provenance_mode == DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.artifact.provenance_mode == DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT


@pytest.mark.asyncio
async def test_pdf_artifact_binds_snapshot_fingerprint_and_source_hash() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.artifact.source_validated_docx_snapshot_fingerprint == snapshot.snapshot_fingerprint
    assert result.artifact.source_docx_sha256 == snapshot.exact_docx_sha256


# -- Repository immutability / defensive copying (REPOSITORY_CAS_OR_ATOMICITY_FALSE) --


@pytest.mark.asyncio
async def test_get_current_pdf_never_returns_same_object_identity() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)
    build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )

    first = repository.get_current_pdf(owner_key)
    second = repository.get_current_pdf(owner_key)
    assert first == second
    assert first is not second
    assert first is not repository._current_pdf[owner_key.owner_key_fingerprint]


@pytest.mark.asyncio
async def test_returned_pdf_is_frozen_and_model_copy_does_not_leak() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)
    confirmed = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    original_artifact = confirmed.artifact

    with pytest.raises(Exception):
        original_artifact.superseded = True

    returned = repository.get_current_pdf(owner_key)
    with pytest.raises(Exception):
        returned.pdf_revision = 999

    locally_modified = returned.model_copy(update={"pdf_revision": 999})
    assert locally_modified.pdf_revision == 999

    still_current = repository.get_current_pdf(owner_key)
    assert still_current.pdf_revision == 1
    assert still_current.superseded is False
    assert still_current is not original_artifact


@pytest.mark.asyncio
async def test_repository_never_holds_two_current_pdfs_for_one_owner() -> None:
    ctx = await build_ready_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    first = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == PdfConfirmationBuildStatus.CONFIRMED

    second = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert second.status == PdfConfirmationBuildStatus.CONFIRMED
    # exactly one entry in the current-pdf dict for this owner
    assert len([k for k in repository._current_pdf if k == owner_key.owner_key_fingerprint]) == 1
    assert repository.get_current_pdf(owner_key).artifact_fingerprint == second.artifact.artifact_fingerprint
