"""Explicit Provenance Stage P6-B3b-A: ``build_and_confirm_pdf``'s explicit,
total mapping of every ``PdfReplaceResult.status`` member (``CasReplaceStatus
| CvDocumentPdfSlotCutoverStatus``) onto a ``PdfConfirmationBuildStatus``.
"""

from __future__ import annotations

import hashlib

import pytest

from app.schemas.cv_document_artifact import PdfConfirmationBuildStatus, PdfConversionStatus
from app.services.cv_document_pdf_confirmation_builder import build_and_confirm_pdf
from app.services.cv_document_proposal_builder import build_current_docx_proposal
from app.services.cv_document_repository_protocol import (
    CasReplaceStatus,
    CvDocumentPdfSlotCutoverStatus,
    InMemoryCvDocumentArtifactRepository,
    PdfReplaceResult,
)
from app.services.cv_document_snapshot_builder import build_validated_docx_snapshot
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context
from tests.unit.test_cv_document_pdf_confirmation_builder import (
    CONVERSION_POLICY_VERSION,
    RENDERING_POLICY_VERSION,
    VALIDATION_POLICY_VERSION,
    _FakePdfConversionAdapter,
    _FakeRenderer,
    _FakeStructuralValidator,
    _FakeTemplateProvider,
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
    return owner_key, snapshot_result.snapshot


class _ForcedStatusRepository(InMemoryCvDocumentArtifactRepository):
    """Wraps the real in-memory repository, but ``replace_current_pdf``
    always returns a caller-forced status -- used to drive
    ``build_and_confirm_pdf`` through every ``PdfReplaceResult.status``
    member without needing a real frozen-slot SQL fixture."""

    def __init__(self, forced_status) -> None:
        super().__init__()
        self._forced_status = forced_status

    def replace_current_pdf(self, owner_key, artifact, pdf_bytes, expected_previous_revision):
        # model_construct bypasses field validation -- required to simulate
        # a hypothetical future/rogue status the Pydantic status field type
        # itself would otherwise reject before build_and_confirm_pdf's own
        # fail-loud branch ever gets a chance to run.
        return PdfReplaceResult.model_construct(status=self._forced_status, current_artifact=None)


@pytest.mark.asyncio
async def test_replaced_maps_to_confirmed() -> None:
    ctx = await build_ready_integrated_context()
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(repository, ctx)

    result = build_and_confirm_pdf(
        owner_key=owner_key, snapshot=snapshot, repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(), conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.CONFIRMED


@pytest.mark.asyncio
async def test_stale_revision_maps_to_stale_revision() -> None:
    ctx = await build_ready_integrated_context()
    real_repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(real_repository, ctx)

    forced = _ForcedStatusRepository(CasReplaceStatus.STALE_REVISION)
    forced._snapshots = real_repository._snapshots

    result = build_and_confirm_pdf(
        owner_key=owner_key, snapshot=snapshot, repository=forced,
        conversion_adapter=_FakePdfConversionAdapter(), conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.STALE_REVISION


@pytest.mark.asyncio
async def test_legacy_frozen_maps_to_legacy_frozen() -> None:
    ctx = await build_ready_integrated_context()
    real_repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(real_repository, ctx)

    forced = _ForcedStatusRepository(CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN)
    forced._snapshots = real_repository._snapshots

    result = build_and_confirm_pdf(
        owner_key=owner_key, snapshot=snapshot, repository=forced,
        conversion_adapter=_FakePdfConversionAdapter(), conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == PdfConfirmationBuildStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
    assert result.artifact is None


@pytest.mark.asyncio
async def test_unmapped_status_fails_loud() -> None:
    import enum

    class _RogueStatus(enum.StrEnum):
        UNKNOWN_FUTURE_STATUS = "UNKNOWN_FUTURE_STATUS"

    ctx = await build_ready_integrated_context()
    real_repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(real_repository, ctx)

    forced = _ForcedStatusRepository(_RogueStatus.UNKNOWN_FUTURE_STATUS)
    forced._snapshots = real_repository._snapshots

    with pytest.raises(AssertionError):
        build_and_confirm_pdf(
            owner_key=owner_key, snapshot=snapshot, repository=forced,
            conversion_adapter=_FakePdfConversionAdapter(), conversion_policy_version=CONVERSION_POLICY_VERSION,
            expected_previous_revision=None,
        )


@pytest.mark.asyncio
async def test_exhaustive_status_mapping_covers_every_member() -> None:
    """Every member of ``CasReplaceStatus`` and ``CvDocumentPdfSlotCutoverStatus``
    must be explicitly handled by ``build_and_confirm_pdf`` -- this test
    fails the moment a new member is added without updating both the
    mapping and this test."""

    all_members = list(CasReplaceStatus) + list(CvDocumentPdfSlotCutoverStatus)
    assert set(all_members) == {
        CasReplaceStatus.REPLACED,
        CasReplaceStatus.STALE_REVISION,
        CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN,
    }

    ctx = await build_ready_integrated_context()
    real_repository = InMemoryCvDocumentArtifactRepository()
    owner_key, snapshot = await _seed_snapshot(real_repository, ctx)

    expected_mapping = {
        CasReplaceStatus.REPLACED: PdfConfirmationBuildStatus.CONFIRMED,
        CasReplaceStatus.STALE_REVISION: PdfConfirmationBuildStatus.STALE_REVISION,
        CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN: (
            PdfConfirmationBuildStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
        ),
    }
    for member in all_members:
        forced = _ForcedStatusRepository(member)
        forced._snapshots = real_repository._snapshots
        result = build_and_confirm_pdf(
            owner_key=owner_key, snapshot=snapshot, repository=forced,
            conversion_adapter=_FakePdfConversionAdapter(), conversion_policy_version=CONVERSION_POLICY_VERSION,
            expected_previous_revision=None,
        )
        assert result.status == expected_mapping[member]
