"""Explicit Provenance Stage P6-B3b-B: read-only reconciliation for the
``cv_final_confirmed_pdf_artifacts`` / ``cv_confirmed_pdf_current_authority``
lines.

``run_final_confirmed_pdf_reconciliation`` never mutates a SQL row, never
mutates or deletes a blob, never regenerates a PDF, and never changes
current authority -- every check here is a pure observation, exactly like
``cv_document_reconciliation.run_reconciliation`` (P6-B1). A
``LEGACY_VALIDATED_DOCX`` authority row is never itself an issue for this
module (it belongs to the legacy line, not this one) and a historical
(non-current) ``FinalConfirmedPdfArtifact`` row is never itself an issue --
only its own internal consistency is ever checked.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfLineageKind,
    compute_final_confirmed_pdf_artifact_fingerprint,
    compute_final_confirmed_pdf_request_fingerprint,
)
from app.schemas.cv_document_final_confirmed_pdf_reconciliation import (
    FinalConfirmedPdfReconciliationIssue,
    FinalConfirmedPdfReconciliationIssueCode,
    FinalConfirmedPdfReconciliationReport,
)
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.schemas.cv_document_storage import DocumentBlobStorageStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_models import (
    CvConfirmedPdfCurrentAuthorityRow,
    CvDocumentArtifactOwner,
    CvDocumentBlob,
    CvDocxFinalSnapshotRow,
    CvFinalConfirmedPdfArtifactRow,
)


_PDF_MEDIA_TYPE = "application/pdf"


def run_final_confirmed_pdf_reconciliation(
    session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
) -> FinalConfirmedPdfReconciliationReport:
    """Produce a read-only inconsistency report for the Final Confirmed PDF
    line. Calling this function twice in a row on unchanged storage always
    yields an identical report (idempotent)."""

    issues: list[FinalConfirmedPdfReconciliationIssue] = []

    with session_factory() as session:
        artifact_rows = list(session.execute(select(CvFinalConfirmedPdfArtifactRow)).scalars())
        snapshot_rows = list(session.execute(select(CvDocxFinalSnapshotRow)).scalars())
        owner_rows = list(session.execute(select(CvDocumentArtifactOwner)).scalars())
        blob_rows = list(session.execute(select(CvDocumentBlob)).scalars())
        authority_rows = list(session.execute(select(CvConfirmedPdfCurrentAuthorityRow)).scalars())

        owner_fps = {row.owner_key_fingerprint for row in owner_rows}
        snapshot_by_fp = {row.final_snapshot_fingerprint: row for row in snapshot_rows}
        blob_by_sha = {row.blob_sha256: row for row in blob_rows}
        artifact_by_fp = {row.artifact_fingerprint: row for row in artifact_rows}

        # -- per-artifact checks --------------------------------------------------
        for row in artifact_rows:
            if row.owner_key_fingerprint not in owner_fps:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.OWNER_MISSING,
                        detail="final confirmed PDF artifact references a missing owner row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                    )
                )

            snapshot = snapshot_by_fp.get(row.source_final_snapshot_fingerprint)
            if snapshot is None:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_SNAPSHOT_MISSING,
                        detail="final confirmed PDF artifact references a missing source Final DOCX Snapshot",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                    )
                )
            else:
                if snapshot.owner_key_fingerprint != row.owner_key_fingerprint:
                    issues.append(
                        FinalConfirmedPdfReconciliationIssue(
                            issue_code=FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH,
                            detail="source Final DOCX Snapshot owner does not match the artifact's owner",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            artifact_fingerprint=row.artifact_fingerprint,
                        )
                    )
                if snapshot.final_docx_sha256 != row.source_final_docx_sha256:
                    issues.append(
                        FinalConfirmedPdfReconciliationIssue(
                            issue_code=FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_DOCX_SHA256_MISMATCH,
                            detail=(
                                "source Final DOCX Snapshot final_docx_sha256 does not match the "
                                "artifact's recorded value"
                            ),
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            artifact_fingerprint=row.artifact_fingerprint,
                        )
                    )

            runtime_identity: ConverterRuntimeIdentity | None = None
            try:
                runtime_identity = ConverterRuntimeIdentity.model_validate_json(row.runtime_identity_json)
            except ValidationError:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.RUNTIME_IDENTITY_JSON_INVALID,
                        detail="stored runtime_identity_json failed to parse as a ConverterRuntimeIdentity",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                    )
                )
            else:
                if runtime_identity.runtime_identity_fingerprint != row.runtime_identity_fingerprint:
                    issues.append(
                        FinalConfirmedPdfReconciliationIssue(
                            issue_code=FinalConfirmedPdfReconciliationIssueCode.RUNTIME_IDENTITY_FINGERPRINT_MISMATCH,
                            detail=(
                                "stored runtime_identity_fingerprint does not match a fingerprint "
                                "recomputed from runtime_identity_json"
                            ),
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            artifact_fingerprint=row.artifact_fingerprint,
                        )
                    )

            recomputed_request_fp = compute_final_confirmed_pdf_request_fingerprint(
                owner_key_fingerprint=row.owner_key_fingerprint,
                source_final_snapshot_fingerprint=row.source_final_snapshot_fingerprint,
                source_final_docx_sha256=row.source_final_docx_sha256,
                runtime_identity_fingerprint=row.runtime_identity_fingerprint,
                conversion_policy_version=row.conversion_policy_version,
                request_schema_version=row.conversion_request_fingerprint_schema_version,
            )
            if recomputed_request_fp != row.conversion_request_fingerprint:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.REQUEST_FINGERPRINT_MISMATCH,
                        detail=(
                            "conversion_request_fingerprint does not match a fingerprint recomputed "
                            "from the row's own fields"
                        ),
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                    )
                )

            recomputed_artifact_fp = compute_final_confirmed_pdf_artifact_fingerprint(
                conversion_request_fingerprint=row.conversion_request_fingerprint,
                pdf_sha256=row.pdf_sha256,
                artifact_schema_version=row.artifact_fingerprint_schema_version,
            )
            if recomputed_artifact_fp != row.artifact_fingerprint:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.ARTIFACT_FINGERPRINT_MISMATCH,
                        detail=(
                            "artifact_fingerprint does not match a fingerprint recomputed from the "
                            "row's own fields"
                        ),
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                    )
                )

            blob_meta = blob_by_sha.get(row.blob_sha256)
            if blob_meta is None:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_MISSING,
                        detail="final confirmed PDF artifact references a blob_sha256 with no cv_document_blobs row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
                continue

            if blob_meta.media_type != _PDF_MEDIA_TYPE:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_MEDIA_TYPE_MISMATCH,
                        detail="blob row media_type is not application/pdf",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )

            verify_status = blob_store.verify_blob(row.blob_sha256)
            if verify_status == DocumentBlobStorageStatus.MISSING:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_MISSING,
                        detail="cv_document_blobs row exists but the blob file is missing",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
                continue
            if verify_status == DocumentBlobStorageStatus.HASH_MISMATCH:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_HASH_MISMATCH,
                        detail="stored blob bytes do not re-hash to blob_sha256",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
                continue

            read_result = blob_store.read_blob(row.blob_sha256)
            if read_result.success:
                assert read_result.data is not None
                if len(read_result.data) != blob_meta.byte_size:
                    issues.append(
                        FinalConfirmedPdfReconciliationIssue(
                            issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_LENGTH_MISMATCH,
                            detail="stored blob byte length does not match cv_document_blobs.byte_size",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            artifact_fingerprint=row.artifact_fingerprint,
                            blob_sha256=row.blob_sha256,
                        )
                    )
                elif hashlib.sha256(read_result.data).hexdigest() != row.pdf_sha256:
                    issues.append(
                        FinalConfirmedPdfReconciliationIssue(
                            issue_code=FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_HASH_MISMATCH,
                            detail="stored blob bytes do not re-hash to the artifact's recorded pdf_sha256",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            artifact_fingerprint=row.artifact_fingerprint,
                            blob_sha256=row.blob_sha256,
                        )
                    )

        # -- current-authority checks (FINAL_DOCX_SNAPSHOT lineage only) --------------
        for row in authority_rows:
            try:
                lineage_kind = ConfirmedPdfLineageKind(row.lineage_kind)
            except ValueError:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_METADATA_INVALID,
                        detail="current-PDF authority row carries an unrecognized lineage_kind",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                    )
                )
                continue

            if lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
                # LEGACY_VALIDATED_DOCX authority is valid and out of scope here.
                continue

            target = artifact_by_fp.get(row.current_artifact_fingerprint)
            if target is None:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_TARGET_ARTIFACT_MISSING,
                        detail="current-PDF authority points at a missing FinalConfirmedPdfArtifact row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.current_artifact_fingerprint,
                    )
                )
            elif target.owner_key_fingerprint != row.owner_key_fingerprint:
                issues.append(
                    FinalConfirmedPdfReconciliationIssue(
                        issue_code=FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_TARGET_ARTIFACT_OWNER_MISMATCH,
                        detail="current-PDF authority target artifact belongs to a different owner",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.current_artifact_fingerprint,
                    )
                )

    return FinalConfirmedPdfReconciliationReport(
        issues=tuple(issues),
        scanned_artifact_count=len(artifact_rows),
        scanned_authority_count=len(authority_rows),
    )
