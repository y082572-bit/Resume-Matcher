"""Explicit Provenance Stage P6-A: PDF confirmation lifecycle.

``build_and_confirm_pdf`` never accepts a live proposal path -- it only
ever converts the exact, immutable bytes already stored in the
content-addressed snapshot repository. A ``FAILED`` conversion attempt
never becomes a document artifact: it is returned as-is, never replaces an
existing current PDF, and never bumps the PDF revision. Replacing the PDF
slot never touches the proposal slot.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.schemas.cv_document_artifact import (
    ConfirmedCvPdfArtifact,
    CvPdfConfirmationBuildResult,
    JobArtifactOwnerKey,
    PdfConfirmationBuildStatus,
    PdfConversionStatus,
    ValidatedCvDocxSnapshot,
)
from app.services.cv_document_adapters import CvPdfConversionAdapter
from app.services.cv_document_repository_protocol import (
    CasReplaceStatus,
    CvDocumentArtifactRepository,
    CvDocumentPdfSlotCutoverStatus,
)
from app.services.truth_fingerprint import canonical_json_bytes


PDF_ARTIFACT_FINGERPRINT_VERSION = "cv-document-confirmed-pdf-artifact-v1"


def compute_pdf_artifact_fingerprint(
    *,
    owner_key_fingerprint: str,
    source_validated_docx_snapshot_fingerprint: str,
    source_docx_sha256: str,
    pdf_sha256: str,
    conversion_adapter_id: str,
    conversion_policy_version: str,
    pdf_revision: int,
) -> str:
    content = {
        "fingerprint_version": PDF_ARTIFACT_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "source_validated_docx_snapshot_fingerprint": source_validated_docx_snapshot_fingerprint,
            "source_docx_sha256": source_docx_sha256,
            "pdf_sha256": pdf_sha256,
            "conversion_adapter_id": conversion_adapter_id,
            "conversion_policy_version": conversion_policy_version,
            "pdf_revision": pdf_revision,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def build_and_confirm_pdf(
    *,
    owner_key: JobArtifactOwnerKey,
    snapshot: ValidatedCvDocxSnapshot,
    repository: CvDocumentArtifactRepository,
    conversion_adapter: CvPdfConversionAdapter,
    conversion_policy_version: str,
    expected_previous_revision: int | None,
) -> CvPdfConfirmationBuildResult:
    stored = repository.retrieve_snapshot(snapshot.exact_docx_sha256)
    if stored is None:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.SNAPSHOT_NOT_FOUND,
            diagnostics=("no snapshot stored under this exact_docx_sha256 locator",),
        )

    stored_snapshot, stored_bytes = stored
    if stored_snapshot.snapshot_fingerprint != snapshot.snapshot_fingerprint:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.SNAPSHOT_MISMATCH,
            diagnostics=("stored snapshot fingerprint does not match the supplied snapshot",),
        )

    if hashlib.sha256(stored_bytes).hexdigest() != snapshot.exact_docx_sha256:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.SNAPSHOT_MISMATCH,
            diagnostics=("re-read snapshot bytes do not match exact_docx_sha256",),
        )

    attempt = conversion_adapter.convert(
        snapshot_bytes=stored_bytes,
        snapshot_fingerprint=snapshot.snapshot_fingerprint,
        conversion_policy_version=conversion_policy_version,
    )

    if attempt.status != PdfConversionStatus.SUCCEEDED:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.CONVERSION_FAILED,
            attempt=attempt,
            diagnostics=attempt.diagnostics,
        )

    assert attempt.pdf_bytes is not None
    assert attempt.pdf_sha256 is not None
    recomputed_pdf_sha256 = hashlib.sha256(attempt.pdf_bytes).hexdigest()
    if recomputed_pdf_sha256 != attempt.pdf_sha256:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.TAMPERED_PDF_HASH,
            diagnostics=("conversion adapter pdf_sha256 does not match a recomputed hash of pdf_bytes",),
        )

    pdf_revision = (expected_previous_revision or 0) + 1
    artifact_fingerprint = compute_pdf_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256,
        pdf_sha256=recomputed_pdf_sha256,
        conversion_adapter_id=attempt.adapter_id,
        conversion_policy_version=attempt.conversion_policy_version,
        pdf_revision=pdf_revision,
    )

    artifact = ConfirmedCvPdfArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256,
        approved_cv_content_fingerprint=snapshot.approved_cv_content_fingerprint,
        content_plan_fingerprint=snapshot.content_plan_fingerprint,
        pdf_sha256=recomputed_pdf_sha256,
        conversion_adapter_id=attempt.adapter_id,
        conversion_policy_version=attempt.conversion_policy_version,
        provenance_mode=snapshot.provenance_mode,
        artifact_fingerprint=artifact_fingerprint,
        pdf_revision=pdf_revision,
        superseded=False,
    )

    replace_result = repository.replace_current_pdf(
        owner_key, artifact, attempt.pdf_bytes, expected_previous_revision
    )
    if replace_result.status == CasReplaceStatus.REPLACED:
        return CvPdfConfirmationBuildResult(status=PdfConfirmationBuildStatus.CONFIRMED, artifact=artifact)
    if replace_result.status == CasReplaceStatus.STALE_REVISION:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.STALE_REVISION,
            diagnostics=("expected_previous_revision did not match the repository's current PDF revision",),
        )
    if replace_result.status == CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN:
        return CvPdfConfirmationBuildResult(
            status=PdfConfirmationBuildStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN,
            diagnostics=(
                "the legacy cv_document_pdf_slots table has been frozen by the Final-Confirmed-PDF cutover",
            ),
        )
    raise AssertionError(f"unmapped PdfReplaceResult.status: {replace_result.status!r}")
