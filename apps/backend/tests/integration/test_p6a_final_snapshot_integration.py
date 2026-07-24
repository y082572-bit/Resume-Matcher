"""End-to-end proof of the Explicit Provenance Stage P6-A Final DOCX
Snapshot Domain Addendum: ``FinalDocxSourceReader`` -> ``FinalDocxSnapshot``
builder -> ``InMemoryFinalDocxSnapshotRepository`` -- plus proof that this
strictly additive flow leaves the existing P6-A
``CvDocumentArtifactRepository`` Protocol and its SQL implementation
completely unchanged.

All facts, owners, and documents are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotBuildStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_snapshot_builder import finalize_current_docx_for_pdf
from app.services.cv_document_final_snapshot_repository import InMemoryFinalDocxSnapshotRepository
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceCapture,
    WorkingCopyReadResult,
    WorkingCopyReadStatus,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import (
    CvDocumentArtifactRepository,
    InMemoryCvDocumentArtifactRepository,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


FINALIZATION_POLICY_VERSION = "cv-document-final-snapshot-finalization-policy-integration-v1"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key(reference_id: str):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


class _StaticSourceReader:
    """A deterministic in-memory fake -- P6-A ships no real filesystem
    reader. Returns a fixed ``WorkingCopyReadResult`` regardless of the
    arguments it is called with (the builder is the one responsible for
    independently verifying the returned capture's binding)."""

    def __init__(self, result: WorkingCopyReadResult) -> None:
        self._result = result

    def read_finalization_source(self, owner_key, expected_proposal_artifact_fingerprint, expected_proposal_revision):
        return self._result


def _finalize(
    *,
    owner_key,
    proposal_fingerprint: str,
    proposal_revision: int,
    generated_sha256: str,
    docx_bytes: bytes,
    snapshot_repository: InMemoryFinalDocxSnapshotRepository,
):
    capture = FinalDocxSourceCapture(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_fingerprint,
        source_proposal_revision=proposal_revision,
        generated_proposal_sha256=generated_sha256,
        docx_bytes=docx_bytes,
    )
    reader = _StaticSourceReader(WorkingCopyReadResult(status=WorkingCopyReadStatus.SUCCESS, capture=capture))
    return finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=snapshot_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )


# -- 50: end-to-end domain flow -------------------------------------------------------


def test_end_to_end_domain_flow() -> None:
    owner_key = _owner_key("job-final-snapshot-e2e")
    docx_bytes = b"the exact working copy bytes captured at PDF-generation time"
    generated_sha256 = hashlib.sha256(docx_bytes).hexdigest()
    snapshot_repository = InMemoryFinalDocxSnapshotRepository()

    result = _finalize(
        owner_key=owner_key,
        proposal_fingerprint=_sha("proposal-artifact-e2e"),
        proposal_revision=1,
        generated_sha256=generated_sha256,
        docx_bytes=docx_bytes,
        snapshot_repository=snapshot_repository,
    )

    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.edited_by_user is False
    assert result.snapshot.owner_key.owner_key_fingerprint == owner_key.owner_key_fingerprint

    stored_metadata = snapshot_repository.get_final_docx_snapshot(result.snapshot.final_snapshot_fingerprint)
    assert stored_metadata == result.snapshot
    stored_bytes = snapshot_repository.retrieve_final_docx_bytes(result.snapshot.final_docx_sha256)
    assert stored_bytes == docx_bytes


# -- 51: two proposal revisions, identical bytes, separate lineage -------------------


def test_two_proposal_revisions_finalize_identical_bytes_with_separate_lineage() -> None:
    owner_key = _owner_key("job-final-snapshot-shared-bytes")
    docx_bytes = b"the candidate never touched this revision either time"
    generated_sha256 = hashlib.sha256(docx_bytes).hexdigest()
    snapshot_repository = InMemoryFinalDocxSnapshotRepository()

    result_one = _finalize(
        owner_key=owner_key,
        proposal_fingerprint=_sha("proposal-artifact-shared-bytes-rev1"),
        proposal_revision=1,
        generated_sha256=generated_sha256,
        docx_bytes=docx_bytes,
        snapshot_repository=snapshot_repository,
    )
    result_two = _finalize(
        owner_key=owner_key,
        proposal_fingerprint=_sha("proposal-artifact-shared-bytes-rev2"),
        proposal_revision=2,
        generated_sha256=generated_sha256,
        docx_bytes=docx_bytes,
        snapshot_repository=snapshot_repository,
    )

    assert result_one.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result_two.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result_one.snapshot.final_snapshot_fingerprint != result_two.snapshot.final_snapshot_fingerprint
    assert result_one.snapshot.final_docx_sha256 == result_two.snapshot.final_docx_sha256

    assert snapshot_repository.get_final_docx_snapshot(result_one.snapshot.final_snapshot_fingerprint) == result_one.snapshot
    assert snapshot_repository.get_final_docx_snapshot(result_two.snapshot.final_snapshot_fingerprint) == result_two.snapshot


# -- 52: later source-reader change never rewrites an old snapshot -------------------


def test_later_source_reader_change_does_not_change_old_snapshot() -> None:
    owner_key = _owner_key("job-final-snapshot-later-change")
    snapshot_repository = InMemoryFinalDocxSnapshotRepository()
    proposal_fingerprint = _sha("proposal-artifact-later-change")
    original_bytes = b"the original working copy bytes at first finalization"

    first_result = _finalize(
        owner_key=owner_key,
        proposal_fingerprint=proposal_fingerprint,
        proposal_revision=1,
        generated_sha256=hashlib.sha256(original_bytes).hexdigest(),
        docx_bytes=original_bytes,
        snapshot_repository=snapshot_repository,
    )
    assert first_result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    frozen_fingerprint = first_result.snapshot.final_snapshot_fingerprint
    frozen_bytes = snapshot_repository.retrieve_final_docx_bytes(first_result.snapshot.final_docx_sha256)

    # A later finalization for a *new* proposal revision, with a brand new
    # source reader / bytes, must never mutate the old snapshot.
    later_bytes = b"a completely different working copy, several edits later"
    later_result = _finalize(
        owner_key=owner_key,
        proposal_fingerprint=_sha("proposal-artifact-later-change-rev2"),
        proposal_revision=2,
        generated_sha256=hashlib.sha256(later_bytes).hexdigest(),
        docx_bytes=later_bytes,
        snapshot_repository=snapshot_repository,
    )
    assert later_result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert later_result.snapshot.final_snapshot_fingerprint != frozen_fingerprint

    still_stored = snapshot_repository.get_final_docx_snapshot(frozen_fingerprint)
    assert still_stored == first_result.snapshot
    assert snapshot_repository.retrieve_final_docx_bytes(first_result.snapshot.final_docx_sha256) == frozen_bytes


# -- 53: existing CvDocumentArtifactRepository Protocol unchanged --------------------


def test_existing_cv_document_artifact_repository_protocol_unchanged() -> None:
    expected_methods = {
        "get_current_proposal",
        "read_current_proposal_bytes",
        "replace_current_proposal",
        "get_current_pdf",
        "read_current_pdf_bytes",
        "replace_current_pdf",
        "save_validated_snapshot",
        "retrieve_snapshot",
        "mark_proposal_superseded",
        "mark_pdf_superseded",
    }
    actual_methods = {name for name in vars(CvDocumentArtifactRepository) if not name.startswith("_")}
    assert actual_methods == expected_methods

    # still functions exactly as before: a real proposal replace + a real
    # snapshot save through the pre-existing, untouched Protocol/impl.
    from app.schemas.cv_document_artifact import (
        CvDocxProposalArtifact,
        DocumentProvenanceMode,
        DocxProposalProvenanceMode,
        DocxStructuralValidationResult,
        DocxStructuralValidationStatus,
        ValidatedCvDocxSnapshot,
    )
    from app.services.cv_document_repository_protocol import CasReplaceStatus, SnapshotSaveStatus
    from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint

    repo = InMemoryCvDocumentArtifactRepository()
    assert isinstance(repo, CvDocumentArtifactRepository)

    owner_key = _owner_key("job-p6a-protocol-unchanged")
    docx_bytes = b"legacy P6-A proposal bytes, protocol regression check"
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    proposal = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint=_sha("approved-content"),
        content_plan_fingerprint=_sha("content-plan"),
        role_strategy_context_fingerprint=None,
        document_schema_version="document-schema-v1",
        rendering_policy_version="rendering-policy-v1",
        renderer_adapter_id="renderer-v1",
        template_fingerprint=_sha("template"),
        generation_input_fingerprint=_sha("generation-input"),
        generated_docx_content_hash=docx_hash,
        current_file_hash=docx_hash,
        validated_file_hash=None,
        proposal_revision=1,
        superseded=False,
        artifact_fingerprint=_sha("legacy-proposal-artifact"),
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    replace_result = repo.replace_current_proposal(owner_key, proposal, docx_bytes, None)
    assert replace_result.status == CasReplaceStatus.REPLACED

    structural_result = DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=docx_hash)
    snapshot_fingerprint = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=docx_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="validation-policy-v1",
        structural_validated_sha256=docx_hash,
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
    )
    snapshot = ValidatedCvDocxSnapshot(
        owner_key=owner_key,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=docx_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="validation-policy-v1",
        structural_validation_result=structural_result,
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint=snapshot_fingerprint,
    )
    save_result = repo.save_validated_snapshot(snapshot, docx_bytes)
    assert save_result.status == SnapshotSaveStatus.SAVED


# -- 54: existing CvDocumentArtifactSqlRepository still conforms ---------------------


def test_sql_repository_still_conforms_to_existing_protocol(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'p6a_protocol_regression.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    sql_repo = CvDocumentArtifactSqlRepository(session_factory, store)

    assert isinstance(sql_repo, CvDocumentArtifactRepository)

    owner_key = _owner_key("job-sql-protocol-unchanged")
    assert sql_repo.get_current_proposal(owner_key) is None
    assert sql_repo.get_current_pdf(owner_key) is None

    engine.dispose()
