"""Explicit Provenance Stage P6-B3b-B: the closed report contract for
read-only ``FinalConfirmedPdfArtifact``/current-authority reconciliation.

Deliberately a wholly separate, additive issue-code set from
``CvDocumentReconciliationIssueCode`` (P6-B1) -- this module never emits
``ORPHAN_BLOB_ROW``/``ORPHAN_FILESYSTEM_BLOB`` (those remain exclusively
``cv_document_reconciliation.py``'s concern, which already folds every new
PDF blob's ``blob_sha256`` into its own global referenced-blob set).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.truth_entity import SHA256_PATTERN


class FinalConfirmedPdfReconciliationIssueCode(StrEnum):
    """The closed set of read-only inconsistencies this reconciliation pass
    can report. A prior, fully historical (not current-authority)
    ``FinalConfirmedPdfArtifact`` row is never itself an issue -- only its
    own internal fingerprint/lineage/blob consistency is checked."""

    OWNER_MISSING = "OWNER_MISSING"
    SOURCE_FINAL_SNAPSHOT_MISSING = "SOURCE_FINAL_SNAPSHOT_MISSING"
    SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH = "SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH"
    SOURCE_FINAL_DOCX_SHA256_MISMATCH = "SOURCE_FINAL_DOCX_SHA256_MISMATCH"
    RUNTIME_IDENTITY_JSON_INVALID = "RUNTIME_IDENTITY_JSON_INVALID"
    RUNTIME_IDENTITY_FINGERPRINT_MISMATCH = "RUNTIME_IDENTITY_FINGERPRINT_MISMATCH"
    REQUEST_FINGERPRINT_MISMATCH = "REQUEST_FINGERPRINT_MISMATCH"
    ARTIFACT_FINGERPRINT_MISMATCH = "ARTIFACT_FINGERPRINT_MISMATCH"
    PDF_BLOB_MISSING = "PDF_BLOB_MISSING"
    PDF_BLOB_MEDIA_TYPE_MISMATCH = "PDF_BLOB_MEDIA_TYPE_MISMATCH"
    PDF_BLOB_LENGTH_MISMATCH = "PDF_BLOB_LENGTH_MISMATCH"
    PDF_BLOB_HASH_MISMATCH = "PDF_BLOB_HASH_MISMATCH"
    AUTHORITY_TARGET_ARTIFACT_MISSING = "AUTHORITY_TARGET_ARTIFACT_MISSING"
    AUTHORITY_TARGET_ARTIFACT_OWNER_MISMATCH = "AUTHORITY_TARGET_ARTIFACT_OWNER_MISMATCH"
    AUTHORITY_METADATA_INVALID = "AUTHORITY_METADATA_INVALID"


class FinalConfirmedPdfReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: FinalConfirmedPdfReconciliationIssueCode
    detail: str = Field(min_length=1, max_length=2000)
    owner_key_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    blob_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class FinalConfirmedPdfReconciliationReport(BaseModel):
    """The single return value of
    ``run_final_confirmed_pdf_reconciliation``. Always read-only to
    produce: never mutates SQL rows, the blob store, or current authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[FinalConfirmedPdfReconciliationIssue, ...] = Field(default_factory=tuple)
    scanned_artifact_count: int = Field(ge=0)
    scanned_authority_count: int = Field(ge=0)
