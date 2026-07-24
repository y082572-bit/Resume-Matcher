"""Explicit Provenance Stage P6-B1: read-only storage reconciliation.

``run_reconciliation`` is a report-first pass over the seven P6-B1 tables
and the blob store's managed filesystem tree. In its standard mode it never
mutates SQL rows or the filesystem -- it never moves the current-proposal
or current-PDF slot to an older artifact, never deletes a current PDF or
any domain history row, and never quarantines or overwrites a blob. The
only mutation this module ever performs is ``cleanup_stale_temp_files``,
and only when a caller explicitly invokes that separate function -- never
as a side effect of ``run_reconciliation`` itself.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_storage import (
    CvDocumentReconciliationIssue,
    CvDocumentReconciliationIssueCode,
    CvDocumentReconciliationReport,
    DocumentBlobStorageStatus,
)
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_models import (
    CvConfirmedPdfArtifactRow,
    CvDocumentBlob,
    CvDocumentPdfSlot,
    CvDocumentProposalSlot,
    CvDocxFinalSnapshotRow,
    CvDocxProposalArtifactRow,
    CvDocxValidatedSnapshotRow,
)


_TEMP_FILE_SUFFIX = ".tmp"
_BLOB_FILE_SUFFIX = ".blob"


def _is_valid_blob_filename_hash(stem: str) -> bool:
    return len(stem) == 64 and stem == stem.lower() and all(c in "0123456789abcdef" for c in stem)


def run_reconciliation(
    session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
) -> CvDocumentReconciliationReport:
    """Produce a read-only inconsistency report.

    Every check here is a pure observation: it reads SQL rows and stats/
    re-hashes filesystem blobs, but issues no ``INSERT``/``UPDATE``/
    ``DELETE`` and no filesystem write. Calling this function twice in a
    row on unchanged storage always yields an identical report
    (idempotent).
    """

    issues: list[CvDocumentReconciliationIssue] = []

    with session_factory() as session:
        proposal_rows = list(session.execute(select(CvDocxProposalArtifactRow)).scalars())
        snapshot_rows = list(session.execute(select(CvDocxValidatedSnapshotRow)).scalars())
        pdf_rows = list(session.execute(select(CvConfirmedPdfArtifactRow)).scalars())
        # Explicit Provenance Stage P6-B1 SQL storage addendum: read-only,
        # solely to extend the global referenced-blob set below so a blob
        # referenced only by a final snapshot is never misreported as an
        # orphan here -- every other final-snapshot-specific invariant is
        # exclusively cv_document_final_snapshot_reconciliation.py's concern.
        final_snapshot_rows = list(session.execute(select(CvDocxFinalSnapshotRow)).scalars())
        blob_rows = list(session.execute(select(CvDocumentBlob)).scalars())
        proposal_slots = list(session.execute(select(CvDocumentProposalSlot)).scalars())
        pdf_slots = list(session.execute(select(CvDocumentPdfSlot)).scalars())

        proposal_by_fp = {row.artifact_fingerprint: row for row in proposal_rows}
        pdf_by_fp = {row.artifact_fingerprint: row for row in pdf_rows}
        blob_by_sha = {row.blob_sha256: row for row in blob_rows}

        # -- dangling slots ---------------------------------------------------
        for slot in proposal_slots:
            if slot.current_artifact_fingerprint is not None and slot.current_artifact_fingerprint not in proposal_by_fp:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PROPOSAL_SLOT_DANGLING_ARTIFACT,
                        detail="proposal slot points at a missing artifact row",
                        owner_key_fingerprint=slot.owner_key_fingerprint,
                        artifact_fingerprint=slot.current_artifact_fingerprint,
                    )
                )
        for slot in pdf_slots:
            if slot.current_artifact_fingerprint is not None and slot.current_artifact_fingerprint not in pdf_by_fp:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PDF_SLOT_DANGLING_ARTIFACT,
                        detail="pdf slot points at a missing artifact row",
                        owner_key_fingerprint=slot.owner_key_fingerprint,
                        artifact_fingerprint=slot.current_artifact_fingerprint,
                    )
                )

        # -- artifact/snapshot rows referencing a missing blob row ------------
        for row in proposal_rows:
            if row.blob_sha256 not in blob_by_sha:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PROPOSAL_ARTIFACT_MISSING_BLOB_ROW,
                        detail="proposal artifact references a blob_sha256 with no cv_document_blobs row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
            elif row.generated_docx_content_hash != row.blob_sha256:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PROPOSAL_BLOB_HASH_MISMATCH,
                        detail="proposal artifact generated_docx_content_hash does not match blob_sha256",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )

        for row in pdf_rows:
            if row.blob_sha256 not in blob_by_sha:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PDF_ARTIFACT_MISSING_BLOB_ROW,
                        detail="pdf artifact references a blob_sha256 with no cv_document_blobs row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
            elif row.pdf_sha256 != row.blob_sha256:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.PDF_BLOB_HASH_MISMATCH,
                        detail="pdf artifact pdf_sha256 does not match blob_sha256",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.artifact_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )

        for row in snapshot_rows:
            if row.blob_sha256 not in blob_by_sha:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.SNAPSHOT_MISSING_BLOB_ROW,
                        detail="snapshot references a blob_sha256 with no cv_document_blobs row",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.snapshot_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )
            elif row.exact_docx_sha256 != row.blob_sha256:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.SNAPSHOT_BLOB_HASH_MISMATCH,
                        detail="snapshot exact_docx_sha256 does not match blob_sha256",
                        owner_key_fingerprint=row.owner_key_fingerprint,
                        artifact_fingerprint=row.snapshot_fingerprint,
                        blob_sha256=row.blob_sha256,
                    )
                )

        # -- duplicate owner/revision (defensive; DB UNIQUE already blocks it) -
        proposal_revision_counts = Counter((row.owner_key_fingerprint, row.proposal_revision) for row in proposal_rows)
        for (owner_fp, revision), count in proposal_revision_counts.items():
            if count > 1:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.DUPLICATE_OWNER_REVISION,
                        detail=f"{count} proposal artifact rows share revision {revision} for one owner",
                        owner_key_fingerprint=owner_fp,
                    )
                )
        pdf_revision_counts = Counter((row.owner_key_fingerprint, row.pdf_revision) for row in pdf_rows)
        for (owner_fp, revision), count in pdf_revision_counts.items():
            if count > 1:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.DUPLICATE_OWNER_REVISION,
                        detail=f"{count} pdf artifact rows share revision {revision} for one owner",
                        owner_key_fingerprint=owner_fp,
                    )
                )

        # -- orphan blob rows (no artifact/snapshot references them) ----------
        referenced_blob_shas = {row.blob_sha256 for row in proposal_rows}
        referenced_blob_shas.update(row.blob_sha256 for row in snapshot_rows)
        referenced_blob_shas.update(row.blob_sha256 for row in pdf_rows)
        referenced_blob_shas.update(row.final_docx_sha256 for row in final_snapshot_rows)
        for row in blob_rows:
            if row.blob_sha256 not in referenced_blob_shas:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.ORPHAN_BLOB_ROW,
                        detail="blob row is not referenced by any proposal/snapshot/pdf artifact",
                        blob_sha256=row.blob_sha256,
                    )
                )
            if row.storage_status == DocumentBlobStorageStatus.QUARANTINED.value:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.QUARANTINED_BLOB_WITH_METADATA,
                        detail="blob row is marked QUARANTINED and still carries metadata",
                        blob_sha256=row.blob_sha256,
                    )
                )

        # -- blob row vs actual filesystem bytes -------------------------------
        for row in blob_rows:
            status = blob_store.verify_blob(row.blob_sha256)
            if status == DocumentBlobStorageStatus.MISSING:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.BLOB_METADATA_FILE_MISSING,
                        detail="cv_document_blobs row exists but the blob file is missing",
                        blob_sha256=row.blob_sha256,
                    )
                )
                continue
            if status == DocumentBlobStorageStatus.HASH_MISMATCH:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.BLOB_FILE_HASH_MISMATCH,
                        detail="stored blob bytes do not re-hash to blob_sha256",
                        blob_sha256=row.blob_sha256,
                    )
                )
                continue
            read_result = blob_store.read_blob(row.blob_sha256)
            if read_result.success and read_result.byte_size != row.byte_size:
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.BLOB_FILE_SIZE_MISMATCH,
                        detail=(
                            f"stored blob byte_size={read_result.byte_size} does not match "
                            f"cv_document_blobs.byte_size={row.byte_size}"
                        ),
                        blob_sha256=row.blob_sha256,
                    )
                )

    # -- orphan filesystem blobs (no metadata row) -----------------------------
    for blob_path in blob_store.blobs_dir.rglob(f"*{_BLOB_FILE_SUFFIX}"):
        if blob_path.is_symlink() or not blob_path.is_file():
            continue
        stem = blob_path.name[: -len(_BLOB_FILE_SUFFIX)]
        if not _is_valid_blob_filename_hash(stem):
            continue
        if stem not in blob_by_sha:
            issues.append(
                CvDocumentReconciliationIssue(
                    issue_code=CvDocumentReconciliationIssueCode.ORPHAN_FILESYSTEM_BLOB,
                    detail="a blob file exists on disk with no cv_document_blobs row",
                    blob_sha256=stem,
                )
            )

    # -- stale temp files -------------------------------------------------------
    for temp_path in blob_store.tmp_dir.iterdir():
        if temp_path.is_symlink():
            continue
        if temp_path.is_file() and temp_path.suffix == _TEMP_FILE_SUFFIX:
            issues.append(
                CvDocumentReconciliationIssue(
                    issue_code=CvDocumentReconciliationIssueCode.STALE_TEMP_FILE,
                    detail=f"stale temp file left under the managed tmp directory: {temp_path.name}",
                )
            )

    # -- symlinks anywhere in the managed tree -----------------------------------
    for managed_dir in (blob_store.blobs_dir, blob_store.tmp_dir, blob_store.quarantine_dir):
        if not managed_dir.exists():
            continue
        for entry in managed_dir.rglob("*"):
            if entry.is_symlink():
                issues.append(
                    CvDocumentReconciliationIssue(
                        issue_code=CvDocumentReconciliationIssueCode.MANAGED_TREE_SYMLINK,
                        detail=f"a symlink was found inside the managed storage tree: {entry}",
                    )
                )

    return CvDocumentReconciliationReport(
        issues=tuple(issues),
        scanned_blob_count=len(blob_rows),
        scanned_proposal_count=len(proposal_rows),
        scanned_snapshot_count=len(snapshot_rows),
        scanned_pdf_count=len(pdf_rows),
    )


def cleanup_stale_temp_files(blob_store: CvDocumentBlobStore) -> tuple[str, ...]:
    """The only mutation this module ever performs, and only when a caller
    explicitly invokes it (never a side effect of ``run_reconciliation``).

    Removes leftover ``*.tmp`` files from the managed ``tmp`` directory
    only -- never touches ``blobs/`` or ``quarantine/``, and never removes
    anything that is not a plain file directly inside ``tmp_dir``.
    """

    removed: list[str] = []
    for temp_path in blob_store.tmp_dir.iterdir():
        if temp_path.is_symlink():
            continue
        if temp_path.is_file() and temp_path.suffix == _TEMP_FILE_SUFFIX:
            try:
                temp_path.unlink()
            except OSError:
                continue
            removed.append(temp_path.name)
    return tuple(removed)
