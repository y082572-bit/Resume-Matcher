"""Explicit Provenance Stage P6-A: validated DOCX snapshot lifecycle.

``build_validated_docx_snapshot`` never builds a snapshot from a live
proposal path -- it always re-reads exact bytes from the repository and
re-hashes them at every step (TOCTOU protection). An unmodified proposal
gets ``PIPELINE_UNMODIFIED_DOCUMENT`` provenance automatically; a
user-edited proposal is only ever snapshotted with an explicit, exact-hash-
and-revision-matched ``ManualDocumentSnapshotConfirmation`` -- the system
never infers semantic equivalence to ``ApprovedCvContent`` on the user's
behalf.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import (
    CvDocxSnapshotBuildResult,
    DocumentProvenanceMode,
    DocxStructuralValidationStatus,
    JobArtifactOwnerKey,
    ManualDocumentSnapshotConfirmation,
    ManualEditStatus,
    MANUAL_CONFIRMATION_POLICY_VERSION,
    SnapshotBuildStatus,
    ValidatedCvDocxSnapshot,
)
from app.services.cv_document_adapters import DocxStructuralValidator
from app.services.cv_document_manual_edit_detector import detect_manual_edit
from app.services.cv_document_repository_protocol import (
    CvDocumentArtifactRepository,
    SnapshotSaveStatus,
)
from app.services.truth_fingerprint import canonical_json_bytes


MANUAL_CONFIRMATION_FINGERPRINT_VERSION = "cv-document-manual-confirmation-v1"
SNAPSHOT_FINGERPRINT_VERSION = "cv-document-validated-snapshot-v1"


def compute_manual_confirmation_fingerprint(
    *,
    owner_key_fingerprint: str,
    proposal_artifact_fingerprint: str,
    proposal_revision: int,
    exact_docx_sha256: str,
    confirmation_mode: DocumentProvenanceMode,
    user_confirmation_decision: str | None,
    confirmation_policy_version: str,
) -> str:
    content = {
        "fingerprint_version": MANUAL_CONFIRMATION_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "proposal_artifact_fingerprint": proposal_artifact_fingerprint,
            "proposal_revision": proposal_revision,
            "exact_docx_sha256": exact_docx_sha256,
            "confirmation_mode": confirmation_mode.value,
            "user_confirmation_decision": user_confirmation_decision,
            "confirmation_policy_version": confirmation_policy_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def build_manual_document_snapshot_confirmation(
    *,
    owner_key_fingerprint: str,
    proposal_artifact_fingerprint: str,
    proposal_revision: int,
    exact_docx_sha256: str,
    confirmation_policy_version: str = MANUAL_CONFIRMATION_POLICY_VERSION,
) -> ManualDocumentSnapshotConfirmation:
    """Build an explicit ``USER_CONFIRMED_MANUAL_DOCUMENT`` confirmation.

    There is no factory for ``PIPELINE_UNMODIFIED_DOCUMENT`` here: that
    mode never requires a user decision and is applied automatically by
    ``build_validated_docx_snapshot`` when the manual-edit detector reports
    ``UNMODIFIED``.
    """

    fingerprint = compute_manual_confirmation_fingerprint(
        owner_key_fingerprint=owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal_artifact_fingerprint,
        proposal_revision=proposal_revision,
        exact_docx_sha256=exact_docx_sha256,
        confirmation_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
        user_confirmation_decision="CONFIRMED",
        confirmation_policy_version=confirmation_policy_version,
    )
    return ManualDocumentSnapshotConfirmation(
        owner_key_fingerprint=owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal_artifact_fingerprint,
        proposal_revision=proposal_revision,
        exact_docx_sha256=exact_docx_sha256,
        confirmation_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
        user_confirmation_decision="CONFIRMED",
        confirmation_policy_version=confirmation_policy_version,
        confirmation_fingerprint=fingerprint,
    )


def compute_snapshot_fingerprint(
    *,
    owner_key_fingerprint: str,
    proposal_artifact_fingerprint: str,
    proposal_revision: int,
    exact_docx_sha256: str,
    approved_cv_content_fingerprint: str,
    content_plan_fingerprint: str,
    template_fingerprint: str,
    validation_policy_version: str,
    structural_validated_sha256: str,
    manual_confirmation_fingerprint: str | None,
    provenance_mode: DocumentProvenanceMode,
) -> str:
    content = {
        "fingerprint_version": SNAPSHOT_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "proposal_artifact_fingerprint": proposal_artifact_fingerprint,
            "proposal_revision": proposal_revision,
            "exact_docx_sha256": exact_docx_sha256,
            "approved_cv_content_fingerprint": approved_cv_content_fingerprint,
            "content_plan_fingerprint": content_plan_fingerprint,
            "template_fingerprint": template_fingerprint,
            "validation_policy_version": validation_policy_version,
            "structural_validated_sha256": structural_validated_sha256,
            "manual_confirmation_fingerprint": manual_confirmation_fingerprint,
            "provenance_mode": provenance_mode.value,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def _confirmation_matches(
    confirmation: ManualDocumentSnapshotConfirmation,
    *,
    owner_key_fingerprint: str,
    proposal_artifact_fingerprint: str,
    proposal_revision: int,
    raw_sha256: str,
) -> bool:
    return (
        confirmation.confirmation_mode == DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT
        and confirmation.user_confirmation_decision == "CONFIRMED"
        and confirmation.owner_key_fingerprint == owner_key_fingerprint
        and confirmation.proposal_artifact_fingerprint == proposal_artifact_fingerprint
        and confirmation.proposal_revision == proposal_revision
        and confirmation.exact_docx_sha256 == raw_sha256
    )


def build_validated_docx_snapshot(
    *,
    owner_key: JobArtifactOwnerKey,
    repository: CvDocumentArtifactRepository,
    structural_validator: DocxStructuralValidator,
    validation_policy_version: str,
    manual_confirmation: ManualDocumentSnapshotConfirmation | None = None,
) -> CvDocxSnapshotBuildResult:
    proposal = repository.get_current_proposal(owner_key)
    if proposal is None:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.NO_CURRENT_PROPOSAL,
            diagnostics=("repository has no current proposal for this owner",),
        )

    proposal_bytes = repository.read_current_proposal_bytes(owner_key)
    if proposal_bytes is None:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.PROPOSAL_BYTES_MISSING,
            diagnostics=("repository has a current proposal artifact but no bytes",),
        )

    raw_sha256 = hashlib.sha256(proposal_bytes).hexdigest()

    edit_detection = detect_manual_edit(
        expected_sha256=proposal.generated_docx_content_hash, observed_bytes=proposal_bytes
    )

    if edit_detection.status == ManualEditStatus.UNMODIFIED:
        provenance_mode = DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT
        manual_confirmation_fingerprint = None
    else:
        if manual_confirmation is None:
            return CvDocxSnapshotBuildResult(
                status=SnapshotBuildStatus.MANUAL_CONFIRMATION_REQUIRED,
                diagnostics=(f"proposal bytes are {edit_detection.status.value}; no confirmation supplied",),
            )
        if not _confirmation_matches(
            manual_confirmation,
            owner_key_fingerprint=owner_key.owner_key_fingerprint,
            proposal_artifact_fingerprint=proposal.artifact_fingerprint,
            proposal_revision=proposal.proposal_revision,
            raw_sha256=raw_sha256,
        ):
            return CvDocxSnapshotBuildResult(
                status=SnapshotBuildStatus.MANUAL_CONFIRMATION_MISMATCH,
                diagnostics=("manual confirmation does not match the current proposal/exact bytes",),
            )
        provenance_mode = DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT
        manual_confirmation_fingerprint = manual_confirmation.confirmation_fingerprint

    structural_result = structural_validator.validate(
        exact_bytes=proposal_bytes,
        expected_sha256=raw_sha256,
        validation_policy_version=validation_policy_version,
    )
    if structural_result.status != DocxStructuralValidationStatus.VALID:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.STRUCTURAL_VALIDATION_FAILED,
            diagnostics=tuple(v.value for v in structural_result.violations),
        )

    post_validation_sha256 = hashlib.sha256(proposal_bytes).hexdigest()
    if (
        post_validation_sha256 != raw_sha256
        or structural_result.validated_sha256 != raw_sha256
    ):
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.HASH_CHANGED_DURING_VALIDATION,
            diagnostics=("exact bytes hash changed during structural validation",),
        )

    snapshot_fingerprint = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=raw_sha256,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version=validation_policy_version,
        structural_validated_sha256=structural_result.validated_sha256,
        manual_confirmation_fingerprint=manual_confirmation_fingerprint,
        provenance_mode=provenance_mode,
    )

    snapshot = ValidatedCvDocxSnapshot(
        owner_key=owner_key,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=raw_sha256,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version=validation_policy_version,
        structural_validation_result=structural_result,
        manual_confirmation_fingerprint=manual_confirmation_fingerprint,
        provenance_mode=provenance_mode,
        snapshot_fingerprint=snapshot_fingerprint,
    )

    save_result = repository.save_validated_snapshot(snapshot, proposal_bytes)
    if save_result.status == SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.REPOSITORY_SAVE_FAILED,
            diagnostics=("a different snapshot already exists under this exact_docx_sha256 locator",),
        )

    stored = repository.retrieve_snapshot(raw_sha256)
    if stored is None:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.REPOSITORY_INTEGRITY_FAILURE,
            diagnostics=("snapshot could not be re-read immediately after being saved",),
        )
    _, stored_bytes = stored
    if hashlib.sha256(stored_bytes).hexdigest() != raw_sha256:
        return CvDocxSnapshotBuildResult(
            status=SnapshotBuildStatus.REPOSITORY_INTEGRITY_FAILURE,
            diagnostics=("re-read snapshot bytes do not match exact_docx_sha256",),
        )

    return CvDocxSnapshotBuildResult(status=SnapshotBuildStatus.SNAPSHOT_CREATED, snapshot=snapshot)
