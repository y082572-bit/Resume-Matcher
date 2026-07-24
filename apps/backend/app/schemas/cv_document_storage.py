"""Explicit Provenance Stage P6-B1: internal storage-layer contracts.

These models are private to the P6-B1 storage foundation (blob store, SQL
repository, reconciliation) -- they are never the P6-A domain models
(``CvDocxProposalArtifact``/``ValidatedCvDocxSnapshot``/``ConfirmedCvPdfArtifact``)
and never leak a raw filesystem or SQL exception to a caller. Every model is
``frozen=True``/``extra="forbid"`` so a caller can never silently attach an
unrecognized field or mutate a returned result in place.

P6-B1 defines no API, no router, and no frontend -- see
``docs/explicit-provenance-stage-p6b1.md``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.truth_entity import SHA256_PATTERN


# -- Blob storage status ------------------------------------------------------


class DocumentBlobStorageStatus(StrEnum):
    """The closed set of states a ``cv_document_blobs`` row can carry.

    Never inferred from filesystem ``mtime`` -- always derived from an
    actual bytes re-hash (``CvDocumentBlobStore.verify_blob``) or from the
    write/publish sequence itself.
    """

    OK = "OK"
    MISSING = "MISSING"
    HASH_MISMATCH = "HASH_MISMATCH"
    QUARANTINED = "QUARANTINED"


class DocumentArtifactStorageStatus(StrEnum):
    """The closed set of storage states a domain artifact row can carry.

    Never the P6-A ``lifecycle_status`` -- that remains computed from the
    domain artifact, the current slot, and actual bytes, never stored as a
    caller-controlled SQL authority.
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


# -- Storage error codes -------------------------------------------------------


class CvDocumentStorageErrorCode(StrEnum):
    """Closed set of reasons a P6-B1 storage operation can fail closed.

    A repository or blob-store method never surfaces a raw
    ``OSError``/``sqlalchemy.exc.*`` to a caller -- every failure is mapped
    to one of these codes.
    """

    INVALID_SHA256 = "INVALID_SHA256"
    BLOB_TOO_LARGE = "BLOB_TOO_LARGE"
    PATH_CONTAINMENT_FAILURE = "PATH_CONTAINMENT_FAILURE"
    SYMLINK_ESCAPE = "SYMLINK_ESCAPE"
    TEMP_WRITE_FAILED = "TEMP_WRITE_FAILED"
    TEMP_HASH_MISMATCH = "TEMP_HASH_MISMATCH"
    EXISTING_BLOB_CORRUPTED = "EXISTING_BLOB_CORRUPTED"
    FINAL_BLOB_MISSING = "FINAL_BLOB_MISSING"
    FINAL_BLOB_HASH_MISMATCH = "FINAL_BLOB_HASH_MISMATCH"
    OWNER_IDENTITY_MISMATCH = "OWNER_IDENTITY_MISMATCH"
    OWNER_NOT_FOUND = "OWNER_NOT_FOUND"
    STALE_REVISION = "STALE_REVISION"
    ARTIFACT_FINGERPRINT_MISMATCH = "ARTIFACT_FINGERPRINT_MISMATCH"
    ARTIFACT_BYTES_HASH_MISMATCH = "ARTIFACT_BYTES_HASH_MISMATCH"
    SNAPSHOT_FINGERPRINT_MISMATCH = "SNAPSHOT_FINGERPRINT_MISMATCH"
    SNAPSHOT_BYTES_HASH_MISMATCH = "SNAPSHOT_BYTES_HASH_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


# -- Blob record / operation results -------------------------------------------


class CvDocumentBlobRecord(BaseModel):
    """A closed, internal projection of one ``cv_document_blobs`` row.

    ``storage_locator`` is diagnostic metadata only -- it never
    participates in blob identity, which is always ``blob_sha256`` alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    blob_sha256: str = Field(pattern=SHA256_PATTERN)
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    storage_locator: str = Field(min_length=1, max_length=2048)
    storage_status: DocumentBlobStorageStatus
    created_at: str = Field(min_length=1)
    verified_at: str | None = None


class CvDocumentBlobWriteResult(BaseModel):
    """The single return value of ``CvDocumentBlobStore.write_blob``.

    ``reused_existing=True`` means an identical final blob already existed
    and was reused as-is -- the write path never overwrites an existing
    final blob, identical or not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    blob_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    byte_size: int | None = Field(default=None, ge=0)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    reused_existing: bool = False
    error_code: CvDocumentStorageErrorCode | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocumentBlobWriteResult":
        if self.success:
            if self.blob_sha256 is None or self.byte_size is None or self.media_type is None:
                raise ValueError("a successful write requires blob_sha256/byte_size/media_type")
            if self.error_code is not None:
                raise ValueError("a successful write must carry no error_code")
        else:
            if self.error_code is None:
                raise ValueError("a failed write requires an error_code")
            if self.reused_existing:
                raise ValueError("a failed write cannot claim reused_existing")
        return self


class CvDocumentBlobReadResult(BaseModel):
    """The single return value of ``CvDocumentBlobStore.read_blob``.

    ``data`` is always the exact bytes re-hashed against ``blob_sha256``
    before this result is built -- a hash mismatch always yields
    ``success=False``, never a best-effort payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    blob_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    data: bytes | None = None
    byte_size: int | None = Field(default=None, ge=0)
    error_code: CvDocumentStorageErrorCode | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocumentBlobReadResult":
        if self.success:
            if self.data is None or self.byte_size is None or self.blob_sha256 is None:
                raise ValueError("a successful read requires blob_sha256/data/byte_size")
            if self.error_code is not None:
                raise ValueError("a successful read must carry no error_code")
        else:
            if self.data is not None:
                raise ValueError("a failed read must carry no data")
            if self.error_code is None:
                raise ValueError("a failed read requires an error_code")
        return self


class CvDocumentStorageOperationResult(BaseModel):
    """A generic closed success/failure envelope for internal P6-B1
    plumbing (e.g. the payload of ``CvDocumentStorageError``) -- never a
    substitute for the P6-A Protocol's own typed CAS results
    (``ProposalReplaceResult``/``PdfReplaceResult``/``SnapshotSaveResult``),
    which remain exactly as P6-A defined them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    error_code: CvDocumentStorageErrorCode | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocumentStorageOperationResult":
        if self.success and self.error_code is not None:
            raise ValueError("a successful result must carry no error_code")
        if not self.success and self.error_code is None:
            raise ValueError("a failed result requires an error_code")
        return self


# -- Reconciliation -------------------------------------------------------------


class CvDocumentReconciliationIssueCode(StrEnum):
    """Closed set of P6-B1 storage inconsistencies a reconciliation pass can
    report. Reporting is always read-only -- see ``cv_document_reconciliation.py``
    for exactly which of these an automatic cleanup mode may act on."""

    PROPOSAL_SLOT_DANGLING_ARTIFACT = "PROPOSAL_SLOT_DANGLING_ARTIFACT"
    PDF_SLOT_DANGLING_ARTIFACT = "PDF_SLOT_DANGLING_ARTIFACT"
    PROPOSAL_ARTIFACT_MISSING_BLOB_ROW = "PROPOSAL_ARTIFACT_MISSING_BLOB_ROW"
    PDF_ARTIFACT_MISSING_BLOB_ROW = "PDF_ARTIFACT_MISSING_BLOB_ROW"
    SNAPSHOT_MISSING_BLOB_ROW = "SNAPSHOT_MISSING_BLOB_ROW"
    BLOB_METADATA_FILE_MISSING = "BLOB_METADATA_FILE_MISSING"
    BLOB_FILE_HASH_MISMATCH = "BLOB_FILE_HASH_MISMATCH"
    BLOB_FILE_SIZE_MISMATCH = "BLOB_FILE_SIZE_MISMATCH"
    SNAPSHOT_BLOB_HASH_MISMATCH = "SNAPSHOT_BLOB_HASH_MISMATCH"
    PROPOSAL_BLOB_HASH_MISMATCH = "PROPOSAL_BLOB_HASH_MISMATCH"
    PDF_BLOB_HASH_MISMATCH = "PDF_BLOB_HASH_MISMATCH"
    DUPLICATE_OWNER_REVISION = "DUPLICATE_OWNER_REVISION"
    ORPHAN_BLOB_ROW = "ORPHAN_BLOB_ROW"
    ORPHAN_FILESYSTEM_BLOB = "ORPHAN_FILESYSTEM_BLOB"
    STALE_TEMP_FILE = "STALE_TEMP_FILE"
    MANAGED_TREE_SYMLINK = "MANAGED_TREE_SYMLINK"
    QUARANTINED_BLOB_WITH_METADATA = "QUARANTINED_BLOB_WITH_METADATA"


class CvDocumentReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: CvDocumentReconciliationIssueCode
    detail: str = Field(min_length=1, max_length=2000)
    owner_key_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    blob_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class CvDocumentReconciliationReport(BaseModel):
    """The single return value of ``run_reconciliation``.

    Standard mode is always read-only: producing this report never changes
    the current proposal/PDF slot, never deletes domain history, and never
    quarantines or overwrites a blob -- see
    ``docs/explicit-provenance-stage-p6b1.md``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[CvDocumentReconciliationIssue, ...] = Field(default_factory=tuple)
    scanned_blob_count: int = Field(ge=0)
    scanned_proposal_count: int = Field(ge=0)
    scanned_snapshot_count: int = Field(ge=0)
    scanned_pdf_count: int = Field(ge=0)
