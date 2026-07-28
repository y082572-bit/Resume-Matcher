"""Explicit Provenance Stage P6-B3b-A: the closed reader that turns a
``final_snapshot_fingerprint`` locator into the exact, verified Final DOCX
Snapshot bytes a PDF conversion attempt is built from.

``FinalDocxSnapshotPdfSourceReader`` depends only on the storage-agnostic
``FinalDocxSnapshotRepository`` Protocol -- it never imports SQLAlchemy, a
concrete SQL repository class, or any filesystem path directly, so a Stage
P6-B3b-B composition root can swap in any conforming repository
(in-memory or SQL) without this module changing at all.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf_source import (
    FinalDocxSnapshotPdfSourceReadResult,
    FinalDocxSnapshotPdfSourceReadStatus,
)
from app.services.cv_document_blob_store import CvDocumentStorageError
from app.services.cv_document_final_snapshot_repository import FinalDocxSnapshotRepository
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint


class FinalDocxSnapshotPdfSourceReader:
    """Reads and re-verifies one Final DOCX Snapshot's exact bytes by
    ``final_snapshot_fingerprint`` locator. Every technical/lineage failure
    is a closed status -- this reader never lets a raw repository/storage
    exception escape to its own caller."""

    def __init__(self, repository: FinalDocxSnapshotRepository) -> None:
        self._repository = repository

    def read_source(
        self, *, owner_key: JobArtifactOwnerKey, final_snapshot_fingerprint: str
    ) -> FinalDocxSnapshotPdfSourceReadResult:
        recomputed_owner_fp = compute_owner_key_fingerprint(
            person_entity_id=owner_key.person_entity_id,
            owner_kind=owner_key.owner_kind,
            owner_reference_id=owner_key.owner_reference_id,
            owner_key_schema_version=owner_key.owner_key_schema_version,
        )
        if recomputed_owner_fp != owner_key.owner_key_fingerprint:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH
            )

        try:
            snapshot = self._repository.get_final_docx_snapshot(final_snapshot_fingerprint)
        except CvDocumentStorageError:
            return FinalDocxSnapshotPdfSourceReadResult(status=FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE)

        if snapshot is None:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_NOT_FOUND
            )
        if snapshot.final_snapshot_fingerprint != final_snapshot_fingerprint:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_NOT_FOUND
            )
        if snapshot.owner_key.owner_key_fingerprint != recomputed_owner_fp:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_OWNER_MISMATCH
            )

        try:
            docx_bytes = self._repository.retrieve_final_docx_bytes(snapshot.final_docx_sha256)
        except CvDocumentStorageError:
            return FinalDocxSnapshotPdfSourceReadResult(status=FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE)

        if not docx_bytes:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_BYTES_NOT_FOUND
            )

        recomputed_sha256 = hashlib.sha256(docx_bytes).hexdigest()
        if recomputed_sha256 != snapshot.final_docx_sha256:
            return FinalDocxSnapshotPdfSourceReadResult(
                status=FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_HASH_MISMATCH
            )

        return FinalDocxSnapshotPdfSourceReadResult(
            status=FinalDocxSnapshotPdfSourceReadStatus.VERIFIED,
            snapshot=snapshot,
            final_docx_bytes=bytes(docx_bytes),
        )
