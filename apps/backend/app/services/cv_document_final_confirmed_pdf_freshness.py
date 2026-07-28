"""Explicit Provenance Stage P6-B3b-B: informational Final Confirmed PDF
freshness evaluation.

``evaluate_final_confirmed_pdf_freshness`` reads exclusively through the
``FinalConfirmedPdfRepository`` Protocol's existing
``read_current_authority``/``read_artifact`` methods -- it never imports
SQLAlchemy, never opens a transaction, and never calls the converter or a
semantic/Truth Library validator. It never mutates current authority and
never deletes a PDF.
"""

from __future__ import annotations

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfLineageKind
from app.schemas.cv_document_final_confirmed_pdf_freshness import (
    FinalConfirmedPdfFreshnessResult,
    FinalConfirmedPdfFreshnessStatus,
)
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfRepository,
)


_Status = FinalConfirmedPdfFreshnessStatus


def evaluate_final_confirmed_pdf_freshness(
    *,
    owner_key: JobArtifactOwnerKey,
    current_final_snapshot_fingerprint: str,
    current_final_docx_sha256: str,
    current_runtime_identity_fingerprint: str,
    current_conversion_policy_version: str,
    repository: FinalConfirmedPdfRepository,
) -> FinalConfirmedPdfFreshnessResult:
    """Compare the ``FinalConfirmedPdfArtifact`` currently pointed at by
    ``owner_key``'s current-PDF authority against the caller-supplied,
    independently-derived current request context."""

    authority_read = repository.read_current_authority(owner_key)
    if authority_read.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT:
        return FinalConfirmedPdfFreshnessResult(status=_Status.STORAGE_METADATA_CONFLICT)
    if authority_read.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE:
        return FinalConfirmedPdfFreshnessResult(status=_Status.STORAGE_UNAVAILABLE)

    observation = authority_read.observation
    assert observation is not None
    if not observation.exists:
        return FinalConfirmedPdfFreshnessResult(status=_Status.NOT_FOUND)
    if observation.lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
        # LEGACY_VALIDATED_DOCX authority is not a FinalConfirmedPdfArtifact.
        return FinalConfirmedPdfFreshnessResult(status=_Status.NOT_FOUND)

    assert observation.current_artifact_fingerprint is not None
    artifact_read = repository.read_artifact(observation.current_artifact_fingerprint)
    if artifact_read.status == FinalConfirmedPdfArtifactReadStatus.STORAGE_METADATA_CONFLICT:
        return FinalConfirmedPdfFreshnessResult(status=_Status.STORAGE_METADATA_CONFLICT)
    if artifact_read.status == FinalConfirmedPdfArtifactReadStatus.STORAGE_UNAVAILABLE:
        return FinalConfirmedPdfFreshnessResult(status=_Status.STORAGE_UNAVAILABLE)
    if artifact_read.status == FinalConfirmedPdfArtifactReadStatus.NOT_FOUND:
        # Current authority points at a missing artifact row -- a storage-layer
        # inconsistency, never silently treated as NOT_FOUND/FRESH.
        return FinalConfirmedPdfFreshnessResult(status=_Status.STORAGE_METADATA_CONFLICT)

    artifact = artifact_read.artifact
    assert artifact is not None

    if (
        artifact.source_final_snapshot_fingerprint != current_final_snapshot_fingerprint
        or artifact.source_final_docx_sha256 != current_final_docx_sha256
    ):
        return FinalConfirmedPdfFreshnessResult(
            status=_Status.STALE_SOURCE_SNAPSHOT, artifact=artifact, current_authority_observation=observation
        )
    if artifact.runtime_identity_fingerprint != current_runtime_identity_fingerprint:
        return FinalConfirmedPdfFreshnessResult(
            status=_Status.STALE_RUNTIME_IDENTITY, artifact=artifact, current_authority_observation=observation
        )
    if artifact.conversion_policy_version != current_conversion_policy_version:
        return FinalConfirmedPdfFreshnessResult(
            status=_Status.STALE_CONVERSION_POLICY, artifact=artifact, current_authority_observation=observation
        )
    return FinalConfirmedPdfFreshnessResult(
        status=_Status.FRESH, artifact=artifact, current_authority_observation=observation
    )
