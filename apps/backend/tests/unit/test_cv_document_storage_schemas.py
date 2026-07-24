"""Unit tests for the P6-B1 internal storage-layer Pydantic contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.cv_document_storage import (
    CvDocumentBlobReadResult,
    CvDocumentBlobRecord,
    CvDocumentBlobWriteResult,
    CvDocumentReconciliationIssue,
    CvDocumentReconciliationIssueCode,
    CvDocumentReconciliationReport,
    CvDocumentStorageErrorCode,
    CvDocumentStorageOperationResult,
    DocumentArtifactStorageStatus,
    DocumentBlobStorageStatus,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def test_blob_storage_status_is_closed_set() -> None:
    assert {member.value for member in DocumentBlobStorageStatus} == {
        "OK", "MISSING", "HASH_MISMATCH", "QUARANTINED",
    }


def test_artifact_storage_status_is_closed_set() -> None:
    assert {member.value for member in DocumentArtifactStorageStatus} == {
        "ACTIVE", "SUPERSEDED", "STORAGE_UNAVAILABLE",
    }


def test_storage_error_code_contains_every_required_code() -> None:
    required = {
        "INVALID_SHA256", "BLOB_TOO_LARGE", "PATH_CONTAINMENT_FAILURE", "SYMLINK_ESCAPE",
        "TEMP_WRITE_FAILED", "TEMP_HASH_MISMATCH", "EXISTING_BLOB_CORRUPTED",
        "FINAL_BLOB_MISSING", "FINAL_BLOB_HASH_MISMATCH", "OWNER_IDENTITY_MISMATCH",
        "OWNER_NOT_FOUND", "STALE_REVISION", "ARTIFACT_FINGERPRINT_MISMATCH",
        "ARTIFACT_BYTES_HASH_MISMATCH", "SNAPSHOT_FINGERPRINT_MISMATCH",
        "SNAPSHOT_BYTES_HASH_MISMATCH", "STORAGE_METADATA_CONFLICT", "STORAGE_UNAVAILABLE",
    }
    actual = {member.value for member in CvDocumentStorageErrorCode}
    assert required <= actual


def test_blob_record_is_frozen_and_forbids_extra() -> None:
    record = CvDocumentBlobRecord(
        blob_sha256=SHA_A,
        byte_size=3,
        media_type="application/octet-stream",
        storage_locator="blobs/aa/aa/" + SHA_A + ".blob",
        storage_status=DocumentBlobStorageStatus.OK,
        created_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(ValidationError):
        record.byte_size = 4  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CvDocumentBlobRecord(
            blob_sha256=SHA_A,
            byte_size=3,
            media_type="application/octet-stream",
            storage_locator="x",
            storage_status=DocumentBlobStorageStatus.OK,
            created_at="2026-01-01T00:00:00+00:00",
            unexpected_field="nope",
        )


def test_blob_record_rejects_bad_sha256() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobRecord(
            blob_sha256="not-a-hash",
            byte_size=3,
            media_type="application/octet-stream",
            storage_locator="x",
            storage_status=DocumentBlobStorageStatus.OK,
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_blob_write_result_success_requires_fields() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobWriteResult(success=True)


def test_blob_write_result_success_forbids_error_code() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobWriteResult(
            success=True,
            blob_sha256=SHA_A,
            byte_size=1,
            media_type="application/octet-stream",
            error_code=CvDocumentStorageErrorCode.STORAGE_UNAVAILABLE,
        )


def test_blob_write_result_failure_requires_error_code() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobWriteResult(success=False)


def test_blob_write_result_failure_forbids_reused_existing() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobWriteResult(
            success=False,
            error_code=CvDocumentStorageErrorCode.EXISTING_BLOB_CORRUPTED,
            reused_existing=True,
        )


def test_blob_write_result_valid_success() -> None:
    result = CvDocumentBlobWriteResult(
        success=True, blob_sha256=SHA_A, byte_size=10, media_type="application/octet-stream"
    )
    assert result.reused_existing is False
    assert result.error_code is None


def test_blob_read_result_success_requires_fields() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobReadResult(success=True)


def test_blob_read_result_failure_forbids_data() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobReadResult(
            success=False,
            data=b"nope",
            error_code=CvDocumentStorageErrorCode.FINAL_BLOB_MISSING,
        )


def test_blob_read_result_failure_requires_error_code() -> None:
    with pytest.raises(ValidationError):
        CvDocumentBlobReadResult(success=False)


def test_blob_read_result_valid_round_trip() -> None:
    result = CvDocumentBlobReadResult(success=True, blob_sha256=SHA_A, data=b"abc", byte_size=3)
    assert result.data == b"abc"


def test_storage_operation_result_consistency() -> None:
    with pytest.raises(ValidationError):
        CvDocumentStorageOperationResult(success=True, error_code=CvDocumentStorageErrorCode.STALE_REVISION)
    with pytest.raises(ValidationError):
        CvDocumentStorageOperationResult(success=False)
    ok = CvDocumentStorageOperationResult(success=True)
    assert ok.error_code is None


def test_reconciliation_issue_code_is_closed_and_covers_required_scenarios() -> None:
    required = {
        "PROPOSAL_SLOT_DANGLING_ARTIFACT", "PDF_SLOT_DANGLING_ARTIFACT",
        "PROPOSAL_ARTIFACT_MISSING_BLOB_ROW", "PDF_ARTIFACT_MISSING_BLOB_ROW",
        "SNAPSHOT_MISSING_BLOB_ROW", "BLOB_METADATA_FILE_MISSING", "BLOB_FILE_HASH_MISMATCH",
        "BLOB_FILE_SIZE_MISMATCH", "SNAPSHOT_BLOB_HASH_MISMATCH", "PROPOSAL_BLOB_HASH_MISMATCH",
        "PDF_BLOB_HASH_MISMATCH", "DUPLICATE_OWNER_REVISION", "ORPHAN_BLOB_ROW",
        "ORPHAN_FILESYSTEM_BLOB", "STALE_TEMP_FILE", "MANAGED_TREE_SYMLINK",
        "QUARANTINED_BLOB_WITH_METADATA",
    }
    actual = {member.value for member in CvDocumentReconciliationIssueCode}
    assert required <= actual


def test_reconciliation_issue_forbids_extra_and_is_frozen() -> None:
    issue = CvDocumentReconciliationIssue(
        issue_code=CvDocumentReconciliationIssueCode.ORPHAN_BLOB_ROW,
        detail="an orphan blob row",
        blob_sha256=SHA_B,
    )
    with pytest.raises(ValidationError):
        issue.detail = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CvDocumentReconciliationIssue(
            issue_code=CvDocumentReconciliationIssueCode.ORPHAN_BLOB_ROW,
            detail="x",
            unexpected="nope",
        )


def test_reconciliation_report_defaults_to_empty_and_is_frozen() -> None:
    report = CvDocumentReconciliationReport(
        scanned_blob_count=0, scanned_proposal_count=0, scanned_snapshot_count=0, scanned_pdf_count=0
    )
    assert report.issues == ()
    with pytest.raises(ValidationError):
        report.scanned_blob_count = 1  # type: ignore[misc]


def test_reconciliation_report_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        CvDocumentReconciliationReport(
            scanned_blob_count=-1, scanned_proposal_count=0, scanned_snapshot_count=0, scanned_pdf_count=0
        )
