"""Explicit Provenance Stage P6-B2b-B: the ``finalize_working_copy_to_final_docx_snapshot``
composition root.

Wires the P6-B2b-A Working Copy Foundation (``WorkingCopyStore.inspect_working_copy``)
to the existing P6-A finalization builder (``finalize_current_docx_for_pdf``)
and its ``FinalDocxSourceReader``/``FinalDocxSnapshotRepository`` collaborators
-- this module never re-implements filesystem access, owner-lock handling,
or Proposal lineage verification itself.

A caller of ``finalize_working_copy_to_final_docx_snapshot`` supplies only an
``owner_key``, dependency-injection infrastructure (an already-constructed
``WorkingCopyStore``, ``FinalDocxSourceReader``, ``FinalDocxSnapshotRepository``,
``CvDocumentArtifactRepository``), and ``finalization_policy_version`` --
never DOCX bytes, a hash, a proposal revision, a Proposal artifact
fingerprint, ``edited_by_user``, or a filesystem path. The expected Proposal
artifact fingerprint/revision ``finalize_current_docx_for_pdf`` itself
requires are derived here, internally, from the *observed* (not
caller-supplied) ``binding.json`` sidecar -- ``WorkingCopyStore.inspect_working_copy``'s
own ``OBSERVED``/``VALID`` binding projection.

The post-finalization ``source_proposal_is_current`` lookup is best-effort
and purely informational (see module docstring on ``FinalizeWorkingCopyResult``):
a failure there can never turn an already-``FINALIZED`` result into a
failure, never removes the snapshot that was already durably saved, and
never lets an exception escape this module -- it is the *only* place in
this stage's flow that catches a broad ``Exception`` (never
``BaseException``/``KeyboardInterrupt``/``SystemExit``).
"""

from __future__ import annotations

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotBuildStatus
from app.schemas.cv_document_finalize_working_copy import FinalizeWorkingCopyResult
from app.schemas.cv_document_working_copy import WorkingCopyBindingState, WorkingCopyInspectionStatus
from app.services.cv_document_final_snapshot_builder import finalize_current_docx_for_pdf
from app.services.cv_document_final_snapshot_repository import FinalDocxSnapshotRepository
from app.services.cv_document_final_source_reader import FinalDocxSourceReader
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_document_working_copy_store import WorkingCopyStore


#: Explicit Provenance Stage P6-B2b-B: a complete, fallback-free 1:1 mapping
#: of every non-``OBSERVED`` ``WorkingCopyInspectionStatus`` member onto its
#: corresponding ``FinalDocxSnapshotBuildStatus``.
#: ``test_inspection_status_mapping_is_complete`` proves
#: ``set(WorkingCopyInspectionStatus) - {OBSERVED} == set(_INSPECTION_FAILURE_TO_BUILD_STATUS)``.
_INSPECTION_FAILURE_TO_BUILD_STATUS: dict[WorkingCopyInspectionStatus, FinalDocxSnapshotBuildStatus] = {
    WorkingCopyInspectionStatus.WORKING_COPY_MISSING: FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING,
    WorkingCopyInspectionStatus.WORKING_COPY_UNREADABLE: FinalDocxSnapshotBuildStatus.WORKING_COPY_UNREADABLE,
    WorkingCopyInspectionStatus.WORKING_COPY_CHANGED_DURING_READ: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_CHANGED_DURING_READ
    ),
    WorkingCopyInspectionStatus.OWNER_LOCK_TIMEOUT: FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCKED,
    WorkingCopyInspectionStatus.LOCK_PATH_IDENTITY_MISMATCH: (
        FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH
    ),
    WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID: FinalDocxSnapshotBuildStatus.LOCK_CAPABILITY_INVALID,
    WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE: FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE,
}


def finalize_working_copy_to_final_docx_snapshot(
    *,
    owner_key: JobArtifactOwnerKey,
    working_copy_store: WorkingCopyStore,
    source_reader: FinalDocxSourceReader,
    snapshot_repository: FinalDocxSnapshotRepository,
    artifact_repository: CvDocumentArtifactRepository,
    finalization_policy_version: str,
) -> FinalizeWorkingCopyResult:
    recomputed_owner_key_fingerprint = compute_owner_key_fingerprint(
        person_entity_id=owner_key.person_entity_id,
        owner_kind=owner_key.owner_kind,
        owner_reference_id=owner_key.owner_reference_id,
        owner_key_schema_version=owner_key.owner_key_schema_version,
    )
    if recomputed_owner_key_fingerprint != owner_key.owner_key_fingerprint:
        return FinalizeWorkingCopyResult(
            status=FinalDocxSnapshotBuildStatus.OWNER_KEY_FINGERPRINT_MISMATCH,
            diagnostics=("owner_key.owner_key_fingerprint does not match a recomputed fingerprint",),
        )

    inspection = working_copy_store.inspect_working_copy(owner_key=owner_key)
    if inspection.status != WorkingCopyInspectionStatus.OBSERVED:
        mapped_status = _INSPECTION_FAILURE_TO_BUILD_STATUS[inspection.status]
        return FinalizeWorkingCopyResult(
            status=mapped_status,
            diagnostics=(f"working copy inspection did not succeed: {inspection.status.value}",),
        )

    observed_state = inspection.observed_state
    assert observed_state is not None  # guaranteed by WorkingCopyInspectionResult's own OBSERVED invariant

    if observed_state.binding_state == WorkingCopyBindingState.MISSING:
        return FinalizeWorkingCopyResult(
            status=FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISSING,
            diagnostics=("binding.json sidecar is missing",),
        )
    if observed_state.binding_state == WorkingCopyBindingState.INVALID:
        return FinalizeWorkingCopyResult(
            status=FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_INVALID,
            diagnostics=("binding.json sidecar failed to parse",),
        )

    assert observed_state.bound_proposal_artifact_fingerprint is not None
    assert observed_state.bound_proposal_revision is not None

    build_result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=observed_state.bound_proposal_artifact_fingerprint,
        expected_proposal_revision=observed_state.bound_proposal_revision,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        finalization_policy_version=finalization_policy_version,
    )
    if build_result.status != FinalDocxSnapshotBuildStatus.FINALIZED:
        return FinalizeWorkingCopyResult(status=build_result.status, diagnostics=build_result.diagnostics)

    snapshot = build_result.snapshot
    assert snapshot is not None  # guaranteed by FinalDocxSnapshotBuildResult's own FINALIZED invariant

    source_proposal_is_current, lookup_diagnostics = _best_effort_source_proposal_is_current(
        artifact_repository, owner_key, snapshot.source_proposal_artifact_fingerprint
    )

    return FinalizeWorkingCopyResult(
        status=FinalDocxSnapshotBuildStatus.FINALIZED,
        snapshot=snapshot,
        source_proposal_is_current=source_proposal_is_current,
        diagnostics=lookup_diagnostics,
    )


def _best_effort_source_proposal_is_current(
    artifact_repository: CvDocumentArtifactRepository,
    owner_key: JobArtifactOwnerKey,
    source_proposal_artifact_fingerprint: str,
) -> tuple[bool | None, tuple[str, ...]]:
    """Never blocks finalization and never lets an exception escape -- the
    already-durably-saved ``FinalDocxSnapshot`` from the caller above is
    always kept regardless of this lookup's outcome."""

    try:
        current_proposal = artifact_repository.get_current_proposal(owner_key)
    except Exception:
        return None, ("current-proposal lookup is unavailable; source_proposal_is_current could not be determined",)
    if current_proposal is None:
        return False, ()
    return current_proposal.artifact_fingerprint == source_proposal_artifact_fingerprint, ()
