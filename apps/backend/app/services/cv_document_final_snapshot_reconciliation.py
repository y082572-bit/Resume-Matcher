"""Explicit Provenance Stage P6-B1 SQL storage addendum: read-only
reconciliation for ``cv_docx_final_snapshots`` (R1).

``run_final_docx_snapshot_reconciliation`` is a report-first pass, entirely
separate from ``cv_document_reconciliation.run_reconciliation``. It never
mutates SQL rows or the filesystem. It is scoped exclusively to the final
snapshot table's own lineage/blob/fingerprint invariants -- it never scans
for, and never emits, ``ORPHAN_BLOB_ROW`` or ``ORPHAN_FILESYSTEM_BLOB``:
the global ``run_reconciliation`` pass remains the single authority for
those two codes, and it already includes ``cv_docx_final_snapshots.
final_docx_sha256`` in its own referenced-blob set (see
``cv_document_reconciliation.py``) so a blob referenced only by a final
snapshot is never misreported as an orphan there either.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_storage import DocumentBlobStorageStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_models import (
    CvDocumentArtifactOwner,
    CvDocumentBlob,
    CvDocxFinalSnapshotRow,
    CvDocxProposalArtifactRow,
)


_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEMP_FILE_SUFFIX = ".tmp"


class FinalDocxSnapshotReconciliationIssueCode(StrEnum):
    """Closed set of inconsistencies this reconciliation pass can report.

    Deliberately excludes ``ORPHAN_BLOB_ROW``/``ORPHAN_FILESYSTEM_BLOB`` --
    those remain exclusively ``cv_document_reconciliation.run_reconciliation``'s
    to report.
    """

    MISSING_OWNER = "MISSING_OWNER"
    MISSING_SOURCE_PROPOSAL = "MISSING_SOURCE_PROPOSAL"
    SOURCE_PROPOSAL_OWNER_MISMATCH = "SOURCE_PROPOSAL_OWNER_MISMATCH"
    SOURCE_PROPOSAL_REVISION_MISMATCH = "SOURCE_PROPOSAL_REVISION_MISMATCH"
    SOURCE_PROPOSAL_HASH_MISMATCH = "SOURCE_PROPOSAL_HASH_MISMATCH"
    MISSING_BLOB_ROW = "MISSING_BLOB_ROW"
    MISSING_BLOB_FILE = "MISSING_BLOB_FILE"
    BLOB_HASH_MISMATCH = "BLOB_HASH_MISMATCH"
    BLOB_SIZE_MISMATCH = "BLOB_SIZE_MISMATCH"
    BLOB_MEDIA_TYPE_MISMATCH = "BLOB_MEDIA_TYPE_MISMATCH"
    INVALID_FINAL_SNAPSHOT_FINGERPRINT = "INVALID_FINAL_SNAPSHOT_FINGERPRINT"
    EDITED_BY_USER_MISMATCH = "EDITED_BY_USER_MISMATCH"
    MANAGED_TREE_SYMLINK = "MANAGED_TREE_SYMLINK"
    STALE_TEMP_FILE = "STALE_TEMP_FILE"


class FinalDocxSnapshotReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: FinalDocxSnapshotReconciliationIssueCode
    detail: str = Field(min_length=1, max_length=2000)
    owner_key_fingerprint: str | None = None
    final_snapshot_fingerprint: str | None = None
    blob_sha256: str | None = None


class FinalDocxSnapshotReconciliationReport(BaseModel):
    """The single return value of ``run_final_docx_snapshot_reconciliation``.

    Standard mode is always read-only: producing this report never changes
    any ``cv_docx_final_snapshots`` row, never touches any other P6-B1
    table, and never quarantines or overwrites a blob.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[FinalDocxSnapshotReconciliationIssue, ...] = Field(default_factory=tuple)
    scanned_final_snapshot_count: int = Field(ge=0)


def run_final_docx_snapshot_reconciliation(
    session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
) -> FinalDocxSnapshotReconciliationReport:
    """Produce a read-only inconsistency report scoped to
    ``cv_docx_final_snapshots``. Every check here is a pure observation --
    it issues no ``INSERT``/``UPDATE``/``DELETE`` and no filesystem write.
    Calling this function twice in a row on unchanged storage always yields
    an identical report (idempotent).
    """

    issues: list[FinalDocxSnapshotReconciliationIssue] = []

    with session_factory() as session:
        rows = list(session.execute(select(CvDocxFinalSnapshotRow)).scalars())

        for row in rows:
            owner_row = session.get(CvDocumentArtifactOwner, row.owner_key_fingerprint)
            if owner_row is None:
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=FinalDocxSnapshotReconciliationIssueCode.MISSING_OWNER,
                        detail="final snapshot references an owner_key_fingerprint with no owner row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                    )
                )

            proposal_row = session.get(
                CvDocxProposalArtifactRow, row.source_proposal_artifact_fingerprint
            )
            if proposal_row is None:
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=FinalDocxSnapshotReconciliationIssueCode.MISSING_SOURCE_PROPOSAL,
                        detail=(
                            "final snapshot references a source_proposal_artifact_fingerprint "
                            "with no cv_docx_proposal_artifacts row"
                        ),
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                    )
                )
            else:
                if proposal_row.owner_key_fingerprint != row.owner_key_fingerprint:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=(
                                FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_OWNER_MISMATCH
                            ),
                            detail="source proposal's owner_key_fingerprint differs from the final snapshot's",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                        )
                    )
                if proposal_row.proposal_revision != row.source_proposal_revision:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=(
                                FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_REVISION_MISMATCH
                            ),
                            detail="source proposal's proposal_revision differs from the final snapshot's",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                        )
                    )
                if proposal_row.generated_docx_content_hash != row.generated_proposal_sha256:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=(
                                FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_HASH_MISMATCH
                            ),
                            detail=(
                                "source proposal's generated_docx_content_hash differs from the "
                                "final snapshot's generated_proposal_sha256"
                            ),
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                        )
                    )

            blob_row = session.get(CvDocumentBlob, row.final_docx_sha256)
            if blob_row is None:
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=FinalDocxSnapshotReconciliationIssueCode.MISSING_BLOB_ROW,
                        detail="final snapshot references a final_docx_sha256 with no cv_document_blobs row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                        blob_sha256=row.final_docx_sha256,
                    )
                )
            else:
                if blob_row.media_type != _DOCX_MEDIA_TYPE:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=FinalDocxSnapshotReconciliationIssueCode.BLOB_MEDIA_TYPE_MISMATCH,
                            detail="blob row media_type is not the DOCX media type",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                            blob_sha256=row.final_docx_sha256,
                        )
                    )

                status = blob_store.verify_blob(row.final_docx_sha256)
                if status == DocumentBlobStorageStatus.MISSING:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=FinalDocxSnapshotReconciliationIssueCode.MISSING_BLOB_FILE,
                            detail="cv_document_blobs row exists but the blob file is missing",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                            blob_sha256=row.final_docx_sha256,
                        )
                    )
                elif status == DocumentBlobStorageStatus.HASH_MISMATCH:
                    issues.append(
                        FinalDocxSnapshotReconciliationIssue(
                            issue_code=FinalDocxSnapshotReconciliationIssueCode.BLOB_HASH_MISMATCH,
                            detail="stored blob bytes do not re-hash to final_docx_sha256",
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                            blob_sha256=row.final_docx_sha256,
                        )
                    )
                else:
                    read_result = blob_store.read_blob(row.final_docx_sha256)
                    if read_result.success and read_result.byte_size != blob_row.byte_size:
                        issues.append(
                            FinalDocxSnapshotReconciliationIssue(
                                issue_code=FinalDocxSnapshotReconciliationIssueCode.BLOB_SIZE_MISMATCH,
                                detail=(
                                    f"stored blob byte_size={read_result.byte_size} does not match "
                                    f"cv_document_blobs.byte_size={blob_row.byte_size}"
                                ),
                                owner_key_fingerprint=row.owner_key_fingerprint,
                                final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                                blob_sha256=row.final_docx_sha256,
                            )
                        )

            recomputed_fingerprint = compute_final_snapshot_fingerprint(
                owner_key_fingerprint=row.owner_key_fingerprint,
                source_proposal_artifact_fingerprint=row.source_proposal_artifact_fingerprint,
                source_proposal_revision=row.source_proposal_revision,
                generated_proposal_sha256=row.generated_proposal_sha256,
                final_docx_sha256=row.final_docx_sha256,
                finalization_policy_version=row.finalization_policy_version,
                snapshot_schema_version=row.snapshot_schema_version,
            )
            if recomputed_fingerprint != row.final_snapshot_fingerprint:
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=(
                            FinalDocxSnapshotReconciliationIssueCode.INVALID_FINAL_SNAPSHOT_FINGERPRINT
                        ),
                        detail="a recomputed final_snapshot_fingerprint does not match the stored row's own PK",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                    )
                )

            expected_edited_by_user = row.generated_proposal_sha256 != row.final_docx_sha256
            if bool(row.edited_by_user) != expected_edited_by_user:
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=FinalDocxSnapshotReconciliationIssueCode.EDITED_BY_USER_MISMATCH,
                        detail="edited_by_user does not equal (generated_proposal_sha256 != final_docx_sha256)",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                    )
                )

    # -- symlinks anywhere in the managed tree -----------------------------------
    for managed_dir in (blob_store.blobs_dir, blob_store.tmp_dir, blob_store.quarantine_dir):
        if not managed_dir.exists():
            continue
        for entry in managed_dir.rglob("*"):
            if entry.is_symlink():
                issues.append(
                    FinalDocxSnapshotReconciliationIssue(
                        issue_code=FinalDocxSnapshotReconciliationIssueCode.MANAGED_TREE_SYMLINK,
                        detail=f"a symlink was found inside the managed storage tree: {entry}",
                    )
                )

    # -- stale temp files -------------------------------------------------------
    for temp_path in blob_store.tmp_dir.iterdir():
        if temp_path.is_symlink():
            continue
        if temp_path.is_file() and temp_path.suffix == _TEMP_FILE_SUFFIX:
            issues.append(
                FinalDocxSnapshotReconciliationIssue(
                    issue_code=FinalDocxSnapshotReconciliationIssueCode.STALE_TEMP_FILE,
                    detail=f"stale temp file left under the managed tmp directory: {temp_path.name}",
                )
            )

    return FinalDocxSnapshotReconciliationReport(
        issues=tuple(issues), scanned_final_snapshot_count=len(rows)
    )
