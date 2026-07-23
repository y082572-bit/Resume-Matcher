"""Explicit Provenance Stage P6-A: pure replay, freshness, and lifecycle-view
evaluation for the document artifact chain.

No function here reads a clock, performs a lookup, or touches a database.
Every "current" value is always supplied explicitly by the caller,
recomputed from the caller's actual current state -- never copied from a
previously stored field. Byte-level replay families (current proposal
observation, PDF conversion, confirmed PDF) always recompute a fresh
SHA-256 from the exact bytes handed in for *this* call.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInput,
    ApprovedDocumentInputReplayInput,
    ConfirmedCvPdfArtifact,
    ConfirmedPdfReplayInput,
    CvDocxProposalArtifact,
    DocumentLifecycleView,
    DocumentReplayFreshnessStatus,
    DocumentReplayResult,
    DocxProposalStatus,
    ManualEditStatus,
    PdfConfirmationStatus,
    PdfConversionReplayInput,
    ProposalGenerationReplayInput,
    ValidatedCvDocxSnapshot,
    ValidatedSnapshotReplayInput,
)
from app.services.cv_document_manual_edit_detector import detect_manual_edit


def _stale_fields(previous: object, current: object) -> tuple[str, ...]:
    differing = {
        name
        for name in type(previous).model_fields  # type: ignore[attr-defined]
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def _evaluate_generic_freshness(previous, current) -> DocumentReplayResult:
    if current is None:
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.FRESHNESS_NOT_VERIFIED)
    if previous == current:
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.FRESH)
    return DocumentReplayResult(
        freshness_status=DocumentReplayFreshnessStatus.STALE,
        stale_fields=_stale_fields(previous, current),
    )


# -- 1. Approved document input replay ------------------------------------------


def build_approved_document_input_replay_input(
    document_input: ApprovedContentDocumentInput, *, owner_key_fingerprint: str
) -> ApprovedDocumentInputReplayInput:
    return ApprovedDocumentInputReplayInput(
        approved_content_result_fingerprint=document_input.approved_content_result.result_fingerprint,
        content_plan_fingerprint=document_input.source_content_plan.content_plan_fingerprint,
        owner_key_fingerprint=owner_key_fingerprint,
        document_input_fingerprint=document_input.document_input_fingerprint,
    )


def evaluate_approved_document_input_freshness(
    previous: ApprovedDocumentInputReplayInput,
    current: ApprovedDocumentInputReplayInput | None,
) -> DocumentReplayResult:
    return _evaluate_generic_freshness(previous, current)


# -- 2. Proposal generation replay -----------------------------------------------


def build_proposal_generation_replay_input(
    artifact: CvDocxProposalArtifact, *, owner_key_fingerprint: str
) -> ProposalGenerationReplayInput:
    return ProposalGenerationReplayInput(
        owner_key_fingerprint=owner_key_fingerprint,
        approved_cv_content_fingerprint=artifact.approved_cv_content_fingerprint,
        content_plan_fingerprint=artifact.content_plan_fingerprint,
        role_strategy_context_fingerprint=artifact.role_strategy_context_fingerprint,
        template_fingerprint=artifact.template_fingerprint,
        renderer_adapter_id=artifact.renderer_adapter_id,
        rendering_policy_version=artifact.rendering_policy_version,
        document_schema_version=artifact.document_schema_version,
        generation_input_fingerprint=artifact.generation_input_fingerprint,
    )


def evaluate_proposal_generation_freshness(
    previous: ProposalGenerationReplayInput,
    current: ProposalGenerationReplayInput | None,
) -> DocumentReplayResult:
    return _evaluate_generic_freshness(previous, current)


# -- 3. Current proposal observation (raw bytes) ---------------------------------


_MANUAL_EDIT_TO_REPLAY_STATUS = {
    ManualEditStatus.UNMODIFIED: DocumentReplayFreshnessStatus.FRESH,
    ManualEditStatus.USER_EDITED: DocumentReplayFreshnessStatus.FILE_CHANGED,
    ManualEditStatus.FILE_CHANGED_DURING_READ: DocumentReplayFreshnessStatus.FILE_CHANGED,
    ManualEditStatus.FILE_MISSING: DocumentReplayFreshnessStatus.FILE_MISSING,
    ManualEditStatus.FRESHNESS_NOT_VERIFIED: DocumentReplayFreshnessStatus.FRESHNESS_NOT_VERIFIED,
}


def evaluate_current_proposal_observation(
    *, expected_sha256: str, current_bytes: bytes | None
) -> DocumentReplayResult:
    """Always recomputes a fresh SHA-256 from ``current_bytes`` -- never
    reads a previously stored ``current_file_hash``."""

    detection = detect_manual_edit(expected_sha256=expected_sha256, observed_bytes=current_bytes)
    return DocumentReplayResult(freshness_status=_MANUAL_EDIT_TO_REPLAY_STATUS[detection.status])


# -- 4. Validated snapshot replay -------------------------------------------------


def build_validated_snapshot_replay_input(
    snapshot: ValidatedCvDocxSnapshot,
) -> ValidatedSnapshotReplayInput:
    return ValidatedSnapshotReplayInput(
        proposal_artifact_fingerprint=snapshot.proposal_artifact_fingerprint,
        proposal_revision=snapshot.proposal_revision,
        validation_policy_version=snapshot.validation_policy_version,
        manual_confirmation_fingerprint=snapshot.manual_confirmation_fingerprint,
        provenance_mode=snapshot.provenance_mode,
        snapshot_fingerprint=snapshot.snapshot_fingerprint,
    )


def evaluate_validated_snapshot_freshness(
    previous: ValidatedSnapshotReplayInput,
    current: ValidatedSnapshotReplayInput | None,
) -> DocumentReplayResult:
    return _evaluate_generic_freshness(previous, current)


# -- 5. PDF conversion replay ------------------------------------------------------


def build_pdf_conversion_replay_input(
    snapshot: ValidatedCvDocxSnapshot, *, conversion_adapter_id: str, conversion_policy_version: str
) -> PdfConversionReplayInput:
    return PdfConversionReplayInput(
        validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        exact_docx_sha256=snapshot.exact_docx_sha256,
        conversion_adapter_id=conversion_adapter_id,
        conversion_policy_version=conversion_policy_version,
    )


def evaluate_pdf_conversion_freshness(
    previous: PdfConversionReplayInput,
    current: PdfConversionReplayInput | None,
    *,
    current_snapshot_bytes: bytes | None = None,
) -> DocumentReplayResult:
    if current is None:
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.FRESHNESS_NOT_VERIFIED)
    if (
        current_snapshot_bytes is not None
        and hashlib.sha256(current_snapshot_bytes).hexdigest() != previous.exact_docx_sha256
    ):
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.SNAPSHOT_MISMATCH)
    return _evaluate_generic_freshness(previous, current)


# -- 6. Confirmed PDF replay -------------------------------------------------------


def build_confirmed_pdf_replay_input(artifact: ConfirmedCvPdfArtifact) -> ConfirmedPdfReplayInput:
    return ConfirmedPdfReplayInput(
        source_validated_docx_snapshot_fingerprint=artifact.source_validated_docx_snapshot_fingerprint,
        source_docx_sha256=artifact.source_docx_sha256,
        pdf_sha256=artifact.pdf_sha256,
        conversion_adapter_id=artifact.conversion_adapter_id,
        conversion_policy_version=artifact.conversion_policy_version,
        artifact_fingerprint=artifact.artifact_fingerprint,
    )


def evaluate_confirmed_pdf_freshness(
    previous: ConfirmedPdfReplayInput,
    current: ConfirmedPdfReplayInput | None,
    *,
    current_pdf_bytes: bytes | None = None,
) -> DocumentReplayResult:
    if current is None:
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.FRESHNESS_NOT_VERIFIED)
    if current_pdf_bytes is not None and hashlib.sha256(current_pdf_bytes).hexdigest() != previous.pdf_sha256:
        return DocumentReplayResult(freshness_status=DocumentReplayFreshnessStatus.CONVERSION_MISMATCH)
    return _evaluate_generic_freshness(previous, current)


# -- Lifecycle view ----------------------------------------------------------------


def compute_document_lifecycle_view(
    *,
    current_proposal: CvDocxProposalArtifact | None,
    current_proposal_bytes: bytes | None,
    has_validated_snapshot: bool,
    current_pdf: ConfirmedCvPdfArtifact | None,
    pdf_source_proposal_revision: int | None,
    approved_content_is_stale: bool = False,
) -> DocumentLifecycleView:
    """A read-only, computed view -- never a single collapsed
    ``is_confirmed`` boolean. The PDF slot never disappears or resets just
    because the proposal was regenerated."""

    if current_proposal is None:
        proposal_status = DocxProposalStatus.NO_PROPOSAL
    else:
        detection = detect_manual_edit(
            expected_sha256=current_proposal.generated_docx_content_hash,
            observed_bytes=current_proposal_bytes,
        )
        if detection.status == ManualEditStatus.FILE_MISSING:
            proposal_status = DocxProposalStatus.PROPOSAL_FILE_MISSING
        elif detection.status in (
            ManualEditStatus.USER_EDITED,
            ManualEditStatus.FILE_CHANGED_DURING_READ,
        ):
            proposal_status = DocxProposalStatus.PROPOSAL_EXTERNALLY_MODIFIED
        elif approved_content_is_stale:
            proposal_status = DocxProposalStatus.PROPOSAL_STALE
        elif has_validated_snapshot:
            proposal_status = DocxProposalStatus.PROPOSAL_VALIDATED
        else:
            proposal_status = DocxProposalStatus.PROPOSAL_CURRENT

    if current_pdf is None:
        pdf_status = PdfConfirmationStatus.NO_PDF
    elif (
        current_proposal is not None
        and pdf_source_proposal_revision is not None
        and pdf_source_proposal_revision < current_proposal.proposal_revision
    ):
        pdf_status = PdfConfirmationStatus.PDF_CURRENT_BASED_ON_OLDER_PROPOSAL
    else:
        pdf_status = PdfConfirmationStatus.PDF_CURRENT

    return DocumentLifecycleView(
        proposal_status=proposal_status,
        pdf_status=pdf_status,
        current_proposal_revision=current_proposal.proposal_revision if current_proposal else None,
        current_pdf_revision=current_pdf.pdf_revision if current_pdf else None,
        pdf_source_proposal_revision=pdf_source_proposal_revision,
    )
