"""Unit tests for Explicit Provenance Stage P6-A: pure replay/freshness
evaluation and the document lifecycle view.

No function under test performs I/O or reads a clock -- every scenario is
driven purely by explicit previous/current values.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.schemas.cv_document_artifact import (
    ApprovedDocumentInputReplayInput,
    ArtifactOwnerKind,
    ConfirmedCvPdfArtifact,
    ConfirmedPdfReplayInput,
    CvDocxProposalArtifact,
    DocumentProvenanceMode,
    DocumentReplayFreshnessStatus,
    DocxProposalProvenanceMode,
    DocxProposalStatus,
    PdfConfirmationStatus,
    PdfConversionReplayInput,
    ProposalGenerationReplayInput,
    ValidatedSnapshotReplayInput,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotReplayInput
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_replay import (
    build_final_docx_snapshot_replay_input,
    compute_document_lifecycle_view,
    evaluate_approved_document_input_freshness,
    evaluate_confirmed_pdf_freshness,
    evaluate_current_proposal_observation,
    evaluate_final_docx_snapshot_freshness,
    evaluate_pdf_conversion_freshness,
    evaluate_proposal_generation_freshness,
    evaluate_validated_snapshot_freshness,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key():
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-replay-test")


def _proposal_generation_input(**overrides) -> ProposalGenerationReplayInput:
    values = dict(
        owner_key_fingerprint=_sha("owner"),
        approved_cv_content_fingerprint=_sha("approved-content-v1"),
        content_plan_fingerprint=_sha("content-plan-v1"),
        role_strategy_context_fingerprint=None,
        template_fingerprint=_sha("template-v1"),
        renderer_adapter_id="renderer-v1",
        rendering_policy_version="rendering-policy-v1",
        document_schema_version="document-schema-v1",
        generation_input_fingerprint=_sha("generation-input-v1"),
    )
    values.update(overrides)
    return ProposalGenerationReplayInput(**values)


def test_approved_content_change_makes_proposal_generation_stale() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input(approved_cv_content_fingerprint=_sha("approved-content-v2"))
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "approved_cv_content_fingerprint" in result.stale_fields


def test_content_plan_change_makes_proposal_generation_stale() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input(content_plan_fingerprint=_sha("content-plan-v2"))
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "content_plan_fingerprint" in result.stale_fields


def test_template_change_makes_proposal_generation_stale() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input(template_fingerprint=_sha("template-v2"))
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "template_fingerprint" in result.stale_fields


def test_renderer_adapter_change_makes_proposal_generation_stale() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input(renderer_adapter_id="renderer-v2")
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "renderer_adapter_id" in result.stale_fields


def test_rendering_policy_change_makes_proposal_generation_stale() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input(rendering_policy_version="rendering-policy-v2")
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "rendering_policy_version" in result.stale_fields


def test_identical_proposal_generation_input_is_fresh() -> None:
    previous = _proposal_generation_input()
    current = _proposal_generation_input()
    result = evaluate_proposal_generation_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.FRESH


def test_missing_current_is_freshness_not_verified() -> None:
    previous = _proposal_generation_input()
    result = evaluate_proposal_generation_freshness(previous, None)
    assert result.freshness_status == DocumentReplayFreshnessStatus.FRESHNESS_NOT_VERIFIED


def test_docx_bytes_change_gives_file_changed() -> None:
    expected_bytes = b"pipeline-generated-docx-bytes"
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    result = evaluate_current_proposal_observation(
        expected_sha256=expected_sha256, current_bytes=b"user-edited-docx-bytes"
    )
    assert result.freshness_status == DocumentReplayFreshnessStatus.FILE_CHANGED


def test_current_proposal_observation_never_copies_stored_hash() -> None:
    """Two calls sharing the same ``expected_sha256`` but different
    ``current_bytes`` must never collapse to the same result -- the
    observation always recomputes from the actual bytes handed in."""

    expected_bytes = b"pipeline-generated-docx-bytes"
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()

    unmodified = evaluate_current_proposal_observation(expected_sha256=expected_sha256, current_bytes=expected_bytes)
    modified = evaluate_current_proposal_observation(
        expected_sha256=expected_sha256, current_bytes=b"a-completely-different-payload"
    )
    assert unmodified.freshness_status == DocumentReplayFreshnessStatus.FRESH
    assert modified.freshness_status == DocumentReplayFreshnessStatus.FILE_CHANGED


def test_missing_bytes_observation_is_file_missing() -> None:
    result = evaluate_current_proposal_observation(expected_sha256=_sha("expected"), current_bytes=None)
    assert result.freshness_status == DocumentReplayFreshnessStatus.FILE_MISSING


def _snapshot_replay_input(**overrides) -> ValidatedSnapshotReplayInput:
    values = dict(
        proposal_artifact_fingerprint=_sha("proposal-v1"),
        proposal_revision=1,
        validation_policy_version="validation-policy-v1",
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint=_sha("snapshot-v1"),
    )
    values.update(overrides)
    return ValidatedSnapshotReplayInput(**values)


def test_validation_policy_change_makes_snapshot_stale() -> None:
    previous = _snapshot_replay_input()
    current = _snapshot_replay_input(validation_policy_version="validation-policy-v2")
    result = evaluate_validated_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "validation_policy_version" in result.stale_fields


def test_manual_confirmation_change_makes_snapshot_stale() -> None:
    previous = _snapshot_replay_input(
        manual_confirmation_fingerprint=_sha("confirmation-v1"),
        provenance_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
    )
    current = _snapshot_replay_input(
        manual_confirmation_fingerprint=_sha("confirmation-v2"),
        provenance_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
    )
    result = evaluate_validated_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "manual_confirmation_fingerprint" in result.stale_fields


def _pdf_conversion_replay_input(**overrides) -> PdfConversionReplayInput:
    values = dict(
        validated_docx_snapshot_fingerprint=_sha("snapshot-v1"),
        exact_docx_sha256=_sha("snapshot-bytes-v1"),
        conversion_adapter_id="pdf-adapter-v1",
        conversion_policy_version="conversion-policy-v1",
    )
    values.update(overrides)
    return PdfConversionReplayInput(**values)


def test_snapshot_bytes_change_gives_snapshot_mismatch() -> None:
    previous = _pdf_conversion_replay_input()
    current = _pdf_conversion_replay_input()
    different_bytes = b"a snapshot with different exact bytes"
    result = evaluate_pdf_conversion_freshness(previous, current, current_snapshot_bytes=different_bytes)
    assert result.freshness_status == DocumentReplayFreshnessStatus.SNAPSHOT_MISMATCH


def test_conversion_adapter_change_makes_pdf_conversion_stale() -> None:
    previous = _pdf_conversion_replay_input()
    current = _pdf_conversion_replay_input(conversion_adapter_id="pdf-adapter-v2")
    result = evaluate_pdf_conversion_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "conversion_adapter_id" in result.stale_fields


def test_conversion_policy_change_makes_pdf_conversion_stale() -> None:
    previous = _pdf_conversion_replay_input()
    current = _pdf_conversion_replay_input(conversion_policy_version="conversion-policy-v2")
    result = evaluate_pdf_conversion_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "conversion_policy_version" in result.stale_fields


def _confirmed_pdf_replay_input(**overrides) -> ConfirmedPdfReplayInput:
    values = dict(
        source_validated_docx_snapshot_fingerprint=_sha("snapshot-v1"),
        source_docx_sha256=_sha("snapshot-bytes-v1"),
        pdf_sha256=_sha("pdf-bytes-v1"),
        conversion_adapter_id="pdf-adapter-v1",
        conversion_policy_version="conversion-policy-v1",
        artifact_fingerprint=_sha("confirmed-pdf-v1"),
    )
    values.update(overrides)
    return ConfirmedPdfReplayInput(**values)


def test_pdf_bytes_change_gives_conversion_mismatch() -> None:
    previous = _confirmed_pdf_replay_input()
    current = _confirmed_pdf_replay_input()
    different_pdf_bytes = b"a completely different pdf payload"
    result = evaluate_confirmed_pdf_freshness(previous, current, current_pdf_bytes=different_pdf_bytes)
    assert result.freshness_status == DocumentReplayFreshnessStatus.CONVERSION_MISMATCH


def test_approved_document_input_replay_generic_staleness() -> None:
    previous = ApprovedDocumentInputReplayInput(
        approved_content_result_fingerprint=_sha("result-v1"),
        content_plan_fingerprint=_sha("plan-v1"),
        owner_key_fingerprint=_sha("owner-v1"),
        document_input_fingerprint=_sha("input-v1"),
    )
    current = previous.model_copy(update={"approved_content_result_fingerprint": _sha("result-v2")})
    result = evaluate_approved_document_input_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE


def test_current_replay_never_reused_from_stored_input() -> None:
    """``evaluate_current_proposal_observation`` never accepts a stored
    "current" hash as a shortcut -- there is no such parameter at all, only
    ``current_bytes``, which is always freshly hashed."""

    import inspect

    signature = inspect.signature(evaluate_current_proposal_observation)
    assert "current_file_hash" not in signature.parameters
    assert set(signature.parameters) == {"expected_sha256", "current_bytes"}


# -- final DOCX snapshot replay (Final DOCX Snapshot Domain Addendum) ---------


def _final_snapshot_replay_input(**overrides) -> FinalDocxSnapshotReplayInput:
    values = dict(
        owner_key_fingerprint=_sha("owner"),
        source_proposal_artifact_fingerprint=_sha("proposal-artifact-v1"),
        source_proposal_revision=1,
        generated_proposal_sha256=_sha("generated-docx-v1"),
        final_docx_sha256=_sha("generated-docx-v1"),
        finalization_policy_version="final-docx-finalization-policy-v1",
        snapshot_schema_version="cv-document-final-snapshot-schema-v1",
    )
    values.update(overrides)
    return FinalDocxSnapshotReplayInput(**values)


def test_final_snapshot_replay_detects_proposal_revision_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(source_proposal_revision=2)
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "source_proposal_revision" in result.stale_fields


def test_final_snapshot_replay_detects_proposal_fingerprint_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(source_proposal_artifact_fingerprint=_sha("proposal-artifact-v2"))
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "source_proposal_artifact_fingerprint" in result.stale_fields


def test_final_snapshot_replay_detects_generated_hash_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(generated_proposal_sha256=_sha("generated-docx-v2"))
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "generated_proposal_sha256" in result.stale_fields


def test_final_snapshot_replay_detects_final_bytes_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(final_docx_sha256=_sha("edited-in-word-v1"))
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "final_docx_sha256" in result.stale_fields


def test_final_snapshot_replay_detects_finalization_policy_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(finalization_policy_version="final-docx-finalization-policy-v2")
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "finalization_policy_version" in result.stale_fields


def test_final_snapshot_replay_detects_owner_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(owner_key_fingerprint=_sha("a-different-owner"))
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "owner_key_fingerprint" in result.stale_fields


def test_final_snapshot_replay_detects_schema_version_change() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input(snapshot_schema_version="cv-document-final-snapshot-schema-v2")
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.STALE
    assert "snapshot_schema_version" in result.stale_fields


def test_final_snapshot_replay_never_uses_path_or_timestamp() -> None:
    forbidden = {"path", "filename", "locator", "timestamp", "mtime", "filesize"}
    assert forbidden.isdisjoint(set(FinalDocxSnapshotReplayInput.model_fields))


def test_identical_final_snapshot_replay_input_is_fresh() -> None:
    previous = _final_snapshot_replay_input()
    current = _final_snapshot_replay_input()
    result = evaluate_final_docx_snapshot_freshness(previous, current)
    assert result.freshness_status == DocumentReplayFreshnessStatus.FRESH


def test_build_final_docx_snapshot_replay_input_from_snapshot() -> None:
    from uuid import uuid4

    from app.schemas.cv_document_artifact import ArtifactOwnerKind
    from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot
    from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint

    owner_key = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-final-snapshot-replay-build"
    )
    generated_sha = _sha("generated-docx-build-replay")
    fingerprint = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=_sha("proposal-artifact-build-replay"),
        source_proposal_revision=1,
        generated_proposal_sha256=generated_sha,
        final_docx_sha256=generated_sha,
        finalization_policy_version="final-docx-finalization-policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=_sha("proposal-artifact-build-replay"),
        source_proposal_revision=1,
        generated_proposal_sha256=generated_sha,
        final_docx_sha256=generated_sha,
        edited_by_user=False,
        finalization_policy_version="final-docx-finalization-policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fingerprint,
    )

    replay_input = build_final_docx_snapshot_replay_input(snapshot)
    assert replay_input.owner_key_fingerprint == owner_key.owner_key_fingerprint
    assert replay_input.final_docx_sha256 == generated_sha
    result = evaluate_final_docx_snapshot_freshness(replay_input, replay_input)
    assert result.freshness_status == DocumentReplayFreshnessStatus.FRESH


# -- lifecycle view -----------------------------------------------------------


def _proposal_artifact(owner_key, *, revision: int = 1, generated_docx_content_hash: str | None = None) -> CvDocxProposalArtifact:
    docx_hash = generated_docx_content_hash or _sha(f"docx-revision-{revision}")
    return CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint=_sha("approved-content"),
        content_plan_fingerprint=_sha("content-plan"),
        role_strategy_context_fingerprint=None,
        document_schema_version="document-schema-v1",
        rendering_policy_version="rendering-policy-v1",
        renderer_adapter_id="renderer-v1",
        template_fingerprint=_sha("template"),
        generation_input_fingerprint=_sha(f"generation-input-{revision}"),
        generated_docx_content_hash=docx_hash,
        current_file_hash=docx_hash,
        validated_file_hash=None,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=_sha(f"proposal-artifact-{revision}"),
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )


def _confirmed_pdf(owner_key, *, revision: int = 1) -> ConfirmedCvPdfArtifact:
    return ConfirmedCvPdfArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        source_validated_docx_snapshot_fingerprint=_sha("snapshot"),
        source_docx_sha256=_sha("snapshot-bytes"),
        approved_cv_content_fingerprint=_sha("approved-content"),
        content_plan_fingerprint=_sha("content-plan"),
        pdf_sha256=_sha(f"pdf-{revision}"),
        conversion_adapter_id="pdf-adapter-v1",
        conversion_policy_version="conversion-policy-v1",
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        artifact_fingerprint=_sha(f"confirmed-pdf-{revision}"),
        pdf_revision=revision,
        superseded=False,
    )


def test_lifecycle_no_proposal_no_pdf() -> None:
    view = compute_document_lifecycle_view(
        current_proposal=None,
        current_proposal_bytes=None,
        has_validated_snapshot=False,
        current_pdf=None,
        pdf_source_proposal_revision=None,
    )
    assert view.proposal_status == DocxProposalStatus.NO_PROPOSAL
    assert view.pdf_status == PdfConfirmationStatus.NO_PDF


def test_lifecycle_generated_proposal_is_current_exact_bytes() -> None:
    owner_key = _owner_key()
    docx_bytes = b"exact docx bytes for revision 1"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = _proposal_artifact(owner_key, generated_docx_content_hash=docx_hash)
    view = compute_document_lifecycle_view(
        current_proposal=proposal,
        current_proposal_bytes=docx_bytes,
        has_validated_snapshot=False,
        current_pdf=None,
        pdf_source_proposal_revision=None,
    )
    assert view.proposal_status == DocxProposalStatus.PROPOSAL_CURRENT
    assert view.current_proposal_revision == 1


def test_lifecycle_raw_byte_change_is_externally_modified() -> None:
    owner_key = _owner_key()
    docx_bytes = b"exact docx bytes for revision 1"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = _proposal_artifact(owner_key, generated_docx_content_hash=docx_hash)
    view = compute_document_lifecycle_view(
        current_proposal=proposal,
        current_proposal_bytes=b"someone edited this docx file",
        has_validated_snapshot=False,
        current_pdf=None,
        pdf_source_proposal_revision=None,
    )
    assert view.proposal_status == DocxProposalStatus.PROPOSAL_EXTERNALLY_MODIFIED


def test_lifecycle_snapshot_gives_proposal_validated() -> None:
    owner_key = _owner_key()
    docx_bytes = b"exact docx bytes for revision 1"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = _proposal_artifact(owner_key, generated_docx_content_hash=docx_hash)
    view = compute_document_lifecycle_view(
        current_proposal=proposal,
        current_proposal_bytes=docx_bytes,
        has_validated_snapshot=True,
        current_pdf=None,
        pdf_source_proposal_revision=None,
    )
    assert view.proposal_status == DocxProposalStatus.PROPOSAL_VALIDATED


def test_lifecycle_regeneration_with_existing_pdf_keeps_pdf_current() -> None:
    owner_key = _owner_key()
    docx_bytes = b"exact docx bytes for revision 2"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = _proposal_artifact(owner_key, revision=2, generated_docx_content_hash=docx_hash)
    pdf = _confirmed_pdf(owner_key)
    view = compute_document_lifecycle_view(
        current_proposal=proposal,
        current_proposal_bytes=docx_bytes,
        has_validated_snapshot=False,
        current_pdf=pdf,
        pdf_source_proposal_revision=2,
    )
    assert view.pdf_status == PdfConfirmationStatus.PDF_CURRENT
    assert view.proposal_status == DocxProposalStatus.PROPOSAL_CURRENT


def test_lifecycle_pdf_older_than_proposal() -> None:
    owner_key = _owner_key()
    docx_bytes = b"exact docx bytes for revision 2"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = _proposal_artifact(owner_key, revision=2, generated_docx_content_hash=docx_hash)
    pdf = _confirmed_pdf(owner_key)
    view = compute_document_lifecycle_view(
        current_proposal=proposal,
        current_proposal_bytes=docx_bytes,
        has_validated_snapshot=False,
        current_pdf=pdf,
        pdf_source_proposal_revision=1,  # older than the current proposal's revision 2
    )
    assert view.pdf_status == PdfConfirmationStatus.PDF_CURRENT_BASED_ON_OLDER_PROPOSAL
