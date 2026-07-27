"""Explicit Provenance Stage P6-A Final DOCX Snapshot Domain Addendum: the
finalization builder -- ``finalize_current_docx_for_pdf``.

This builder never accepts a caller-supplied ``generated_proposal_sha256``
-- the *only* trusted baseline for ``edited_by_user``/the final snapshot
fingerprint is ``FinalDocxSourceCapture.generated_proposal_sha256``, read
exactly once from ``source_reader`` and independently re-bound to the
caller's expected owner/proposal identity before being trusted at all. A
binding mismatch (wrong owner, wrong proposal artifact fingerprint, wrong
proposal revision) is always fail-closed.

The builder performs no structural validation, no semantic validation, no
Truth Library lookup, and no manual-confirmation check -- see
``docs/explicit-provenance-stage-p6a-final-snapshot-addendum.md``. It
never touches ``CvDocumentArtifactRepository`` or any P6-A proposal/PDF
slot -- it only ever talks to the ``FinalDocxSourceReader`` and
``FinalDocxSnapshotRepository`` it is handed.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_snapshot import (
    FINAL_SNAPSHOT_SCHEMA_VERSION,
    FinalDocxSnapshot,
    FinalDocxSnapshotBuildResult,
    FinalDocxSnapshotBuildStatus,
)
from app.services.cv_document_final_snapshot_repository import (
    FinalDocxSnapshotRepository,
    FinalSnapshotSaveStatus,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceReader,
    WorkingCopyReadStatus,
)
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint


#: Explicit Provenance Stage P6-B2b-B addendum: this mapping is now a
#: *complete*, fallback-free 1:1 mapping of every non-``SUCCESS``
#: ``WorkingCopyReadStatus`` member onto its corresponding
#: ``FinalDocxSnapshotBuildStatus`` -- every new P6-B2b-B member shares its
#: exact name across both enums, so the mapping is a name-preserving
#: passthrough. ``test_all_read_failures_are_covered_by_the_mapping`` in the
#: unit tests fails if a future addition to ``WorkingCopyReadStatus`` is
#: ever left unmapped.
_READ_FAILURE_TO_BUILD_STATUS: dict[WorkingCopyReadStatus, FinalDocxSnapshotBuildStatus] = {
    WorkingCopyReadStatus.WORKING_COPY_MISSING: FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING,
    WorkingCopyReadStatus.WORKING_COPY_UNREADABLE: FinalDocxSnapshotBuildStatus.WORKING_COPY_UNREADABLE,
    WorkingCopyReadStatus.WORKING_COPY_LOCKED: FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCKED,
    WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_CHANGED_DURING_READ
    ),
    WorkingCopyReadStatus.WORKING_COPY_TOO_LARGE: FinalDocxSnapshotBuildStatus.WORKING_COPY_TOO_LARGE,
    WorkingCopyReadStatus.WORKING_COPY_BINDING_MISMATCH: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH
    ),
    WorkingCopyReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH: (
        FinalDocxSnapshotBuildStatus.OWNER_KEY_FINGERPRINT_MISMATCH
    ),
    WorkingCopyReadStatus.WORKING_COPY_BINDING_MISSING: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISSING
    ),
    WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_INVALID
    ),
    WorkingCopyReadStatus.WORKING_COPY_BINDING_OWNER_MISMATCH: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_OWNER_MISMATCH
    ),
    WorkingCopyReadStatus.WORKING_COPY_BINDING_TOO_LARGE: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_TOO_LARGE
    ),
    WorkingCopyReadStatus.LOCK_CAPABILITY_INVALID: FinalDocxSnapshotBuildStatus.LOCK_CAPABILITY_INVALID,
    WorkingCopyReadStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH
    ),
    WorkingCopyReadStatus.SOURCE_PROPOSAL_NOT_FOUND: FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_NOT_FOUND,
    WorkingCopyReadStatus.SOURCE_PROPOSAL_OWNER_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_OWNER_MISMATCH
    ),
    WorkingCopyReadStatus.SOURCE_PROPOSAL_REVISION_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_REVISION_MISMATCH
    ),
    WorkingCopyReadStatus.SOURCE_PROPOSAL_HASH_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_HASH_MISMATCH
    ),
    WorkingCopyReadStatus.STORAGE_UNAVAILABLE: FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE,
}

# Explicit Provenance Stage P6-B1 SQL storage addendum: additive 1:1
# mapping from a real SQL-backed FinalDocxSnapshotRepository's closed save
# statuses onto their corresponding build statuses. This dict is the only
# addition needed here -- the builder still maps exclusively on
# FinalSnapshotSaveStatus (the Protocol's own closed result type), never on
# a SQLAlchemy/CvDocumentStorageError exception, which it never imports.
_SAVE_STATUS_TO_BUILD_STATUS: dict[FinalSnapshotSaveStatus, FinalDocxSnapshotBuildStatus] = {
    FinalSnapshotSaveStatus.SOURCE_PROPOSAL_NOT_FOUND: FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_NOT_FOUND,
    FinalSnapshotSaveStatus.SOURCE_PROPOSAL_OWNER_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_OWNER_MISMATCH
    ),
    FinalSnapshotSaveStatus.SOURCE_PROPOSAL_REVISION_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_REVISION_MISMATCH
    ),
    FinalSnapshotSaveStatus.SOURCE_PROPOSAL_HASH_MISMATCH: (
        FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_HASH_MISMATCH
    ),
    FinalSnapshotSaveStatus.STORAGE_METADATA_CONFLICT: FinalDocxSnapshotBuildStatus.STORAGE_METADATA_CONFLICT,
    FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE: FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE,
}


def finalize_current_docx_for_pdf(
    *,
    owner_key: JobArtifactOwnerKey,
    expected_proposal_artifact_fingerprint: str,
    expected_proposal_revision: int,
    source_reader: FinalDocxSourceReader,
    snapshot_repository: FinalDocxSnapshotRepository,
    finalization_policy_version: str,
) -> FinalDocxSnapshotBuildResult:
    read_result = source_reader.read_finalization_source(
        owner_key,
        expected_proposal_artifact_fingerprint,
        expected_proposal_revision,
    )

    if read_result.status != WorkingCopyReadStatus.SUCCESS:
        mapped_status = _READ_FAILURE_TO_BUILD_STATUS[read_result.status]
        return FinalDocxSnapshotBuildResult(
            status=mapped_status,
            diagnostics=(read_result.error_message or mapped_status.value,),
        )

    capture = read_result.capture
    if capture is None:
        # Unreachable given WorkingCopyReadResult's own SUCCESS invariant;
        # guarded explicitly so this builder never dereferences None bytes.
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.WORKING_COPY_UNREADABLE,
            diagnostics=("source reader reported SUCCESS without a capture",),
        )

    recomputed_owner_key_fingerprint = compute_owner_key_fingerprint(
        person_entity_id=owner_key.person_entity_id,
        owner_kind=owner_key.owner_kind,
        owner_reference_id=owner_key.owner_reference_id,
        owner_key_schema_version=owner_key.owner_key_schema_version,
    )

    if (
        capture.owner_key_fingerprint != recomputed_owner_key_fingerprint
        or capture.source_proposal_artifact_fingerprint != expected_proposal_artifact_fingerprint
        or capture.source_proposal_revision != expected_proposal_revision
    ):
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH,
            diagnostics=("source capture does not match the expected owner/proposal binding",),
        )

    final_docx_sha256 = hashlib.sha256(capture.docx_bytes).hexdigest()
    generated_proposal_sha256 = capture.generated_proposal_sha256
    edited_by_user = generated_proposal_sha256 != final_docx_sha256

    final_snapshot_fingerprint = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=recomputed_owner_key_fingerprint,
        source_proposal_artifact_fingerprint=capture.source_proposal_artifact_fingerprint,
        source_proposal_revision=capture.source_proposal_revision,
        generated_proposal_sha256=generated_proposal_sha256,
        final_docx_sha256=final_docx_sha256,
        finalization_policy_version=finalization_policy_version,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )

    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=capture.source_proposal_artifact_fingerprint,
        source_proposal_revision=capture.source_proposal_revision,
        generated_proposal_sha256=generated_proposal_sha256,
        final_docx_sha256=final_docx_sha256,
        edited_by_user=edited_by_user,
        finalization_policy_version=finalization_policy_version,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=final_snapshot_fingerprint,
    )

    save_result = snapshot_repository.save_final_docx_snapshot(snapshot, capture.docx_bytes)
    if save_result.status == FinalSnapshotSaveStatus.DOCX_HASH_MISMATCH:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_DOCX_HASH_MISMATCH,
            diagnostics=("repository recomputed a different final_docx_sha256 than the builder",),
        )
    if save_result.status == FinalSnapshotSaveStatus.SNAPSHOT_FINGERPRINT_MISMATCH:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_FINGERPRINT_MISMATCH,
            diagnostics=(
                "repository recomputed a different final_snapshot_fingerprint than the builder",
            ),
        )
    if save_result.status in _SAVE_STATUS_TO_BUILD_STATUS:
        return FinalDocxSnapshotBuildResult(
            status=_SAVE_STATUS_TO_BUILD_STATUS[save_result.status],
            diagnostics=(f"repository save failed: {save_result.status.value}",),
        )
    if save_result.status not in (
        FinalSnapshotSaveStatus.SAVED,
        FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL,
    ):
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED,
            diagnostics=(f"repository save failed: {save_result.status.value}",),
        )

    stored_metadata = snapshot_repository.get_final_docx_snapshot(final_snapshot_fingerprint)
    if stored_metadata is None:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED,
            diagnostics=("snapshot metadata could not be re-read immediately after being saved",),
        )

    stored_bytes = snapshot_repository.retrieve_final_docx_bytes(final_docx_sha256)
    if stored_bytes is None:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED,
            diagnostics=("snapshot bytes could not be re-read immediately after being saved",),
        )
    if hashlib.sha256(stored_bytes).hexdigest() != final_docx_sha256:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED,
            diagnostics=("re-read bytes do not match final_docx_sha256",),
        )
    if stored_metadata != snapshot:
        return FinalDocxSnapshotBuildResult(
            status=FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED,
            diagnostics=("re-read metadata does not match the snapshot that was saved",),
        )

    return FinalDocxSnapshotBuildResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=stored_metadata)
