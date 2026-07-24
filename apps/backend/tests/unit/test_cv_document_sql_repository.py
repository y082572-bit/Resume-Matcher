"""Unit tests for ``CvDocumentArtifactSqlRepository``: exact P6-A Protocol
conformance, CAS/slot independence, owner/fingerprint/bytes integrity
checks, and the DB/filesystem orphan-blob boundary.
"""

from __future__ import annotations

import hashlib
import inspect
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.services.cv_document_sql_repository as repo_module
from app.models import Base
from app.schemas.cv_document_artifact import (
    ArtifactOwnerKind,
    ConfirmedCvPdfArtifact,
    CvDocxProposalArtifact,
    DocumentProvenanceMode,
    DocxProposalProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    ValidatedCvDocxSnapshot,
)
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_models import (
    CvConfirmedPdfArtifactRow,
    CvDocumentBlob,
    CvDocumentPdfSlot,
    CvDocumentProposalSlot,
    CvDocxProposalArtifactRow,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_pdf_confirmation_builder import compute_pdf_artifact_fingerprint
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_repository_protocol import (
    CasReplaceStatus,
    CvDocumentArtifactRepository,
    SnapshotSaveStatus,
)
from app.schemas.cv_document_storage import CvDocumentStorageErrorCode
from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repo_env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'repo.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    repo = CvDocumentArtifactSqlRepository(session_factory, store)
    return repo, session_factory, store, engine


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _make_proposal(owner_key, revision: int, content: bytes) -> CvDocxProposalArtifact:
    gen_hash = _sha256(content)
    gen_input_fp = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        template_fingerprint="c" * 64,
        renderer_adapter_id="fake-renderer",
        rendering_policy_version="v1",
        document_schema_version="cv-document-proposal-schema-v1",
    )
    artifact_fp = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash,
        proposal_revision=revision,
    )
    return CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1",
        rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer",
        template_fingerprint="c" * 64,
        generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash,
        validated_file_hash=None,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fp,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )


def _make_snapshot(owner_key, proposal: CvDocxProposalArtifact, content: bytes) -> ValidatedCvDocxSnapshot:
    gen_hash = _sha256(content)
    struct_result = DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=gen_hash)
    snap_fp = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=gen_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="v1",
        structural_validated_sha256=gen_hash,
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
    )
    return ValidatedCvDocxSnapshot(
        owner_key=owner_key,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=gen_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="v1",
        structural_validation_result=struct_result,
        manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint=snap_fp,
    )


def _make_manual_snapshot(owner_key, proposal, content: bytes, confirmation_salt: str) -> ValidatedCvDocxSnapshot:
    gen_hash = _sha256(content)
    struct_result = DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=gen_hash)
    manual_fp = _sha256(("manual-confirmation-" + confirmation_salt).encode())
    snap_fp = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=gen_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="v1",
        structural_validated_sha256=gen_hash,
        manual_confirmation_fingerprint=manual_fp,
        provenance_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
    )
    return ValidatedCvDocxSnapshot(
        owner_key=owner_key,
        proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=proposal.proposal_revision,
        exact_docx_sha256=gen_hash,
        approved_cv_content_fingerprint=proposal.approved_cv_content_fingerprint,
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        template_fingerprint=proposal.template_fingerprint,
        validation_policy_version="v1",
        structural_validation_result=struct_result,
        manual_confirmation_fingerprint=manual_fp,
        provenance_mode=DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT,
        snapshot_fingerprint=snap_fp,
    )


def _make_pdf(owner_key, snapshot: ValidatedCvDocxSnapshot, revision: int, pdf_bytes: bytes) -> ConfirmedCvPdfArtifact:
    pdf_hash = _sha256(pdf_bytes)
    fp = compute_pdf_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256,
        pdf_sha256=pdf_hash,
        conversion_adapter_id="fake-conv",
        conversion_policy_version="v1",
        pdf_revision=revision,
    )
    return ConfirmedCvPdfArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256,
        approved_cv_content_fingerprint=snapshot.approved_cv_content_fingerprint,
        content_plan_fingerprint=snapshot.content_plan_fingerprint,
        pdf_sha256=pdf_hash,
        conversion_adapter_id="fake-conv",
        conversion_policy_version="v1",
        provenance_mode=snapshot.provenance_mode,
        artifact_fingerprint=fp,
        pdf_revision=revision,
        superseded=False,
    )


# 1-2: Protocol conformance / no async ------------------------------------------


def test_repository_is_a_runtime_checkable_protocol_instance(repo_env) -> None:
    repo, *_ = repo_env
    assert isinstance(repo, CvDocumentArtifactRepository)


def test_repository_has_no_async_methods(repo_env) -> None:
    repo, *_ = repo_env
    for name, member in inspect.getmembers(repo, predicate=inspect.ismethod):
        if name.startswith("_"):
            continue
        assert not inspect.iscoroutinefunction(member), f"{name} must not be async"


def test_module_source_has_no_forbidden_async_constructs() -> None:
    """AST-level check (not a raw text scan) so mentions of these terms
    inside docstrings/comments never produce a false positive."""

    import ast

    tree = ast.parse(inspect.getsource(repo_module))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef), "module must define no async def"
        assert not isinstance(node, ast.Await), "module must contain no await expression"
        if isinstance(node, ast.Name):
            assert node.id != "AsyncSession"
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"run_until_complete"}
            if isinstance(node.value, ast.Name) and node.value.id == "asyncio":
                assert node.attr != "run"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                assert alias.name != "AsyncSession"


# 3-8: proposal slot behavior -----------------------------------------------------


def test_first_proposal_generation_sets_proposal_slot(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    artifact = _make_proposal(owner_key, 1, b"v1 content")
    result = repo.replace_current_proposal(owner_key, artifact, b"v1 content", None)
    assert result.status == CasReplaceStatus.REPLACED
    assert result.current_artifact.proposal_revision == 1

    with session_factory() as session:
        slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert slot is not None
        assert slot.current_artifact_fingerprint == artifact.artifact_fingerprint
        assert slot.current_revision == 1
        assert slot.slot_version == 1


def test_second_proposal_replaces_slot_atomically(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    a2 = _make_proposal(owner_key, 2, b"v2")
    result = repo.replace_current_proposal(owner_key, a2, b"v2", expected_previous_revision=1)
    assert result.status == CasReplaceStatus.REPLACED

    with session_factory() as session:
        slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == a2.artifact_fingerprint
        assert slot.current_revision == 2
        assert slot.slot_version == 2


def test_superseded_proposal_row_remains_immutable_history(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    a2 = _make_proposal(owner_key, 2, b"v2")
    repo.replace_current_proposal(owner_key, a2, b"v2", expected_previous_revision=1)

    with session_factory() as session:
        old_row = session.get(CvDocxProposalArtifactRow, a1.artifact_fingerprint)
        assert old_row is not None
        assert old_row.artifact_storage_status == "SUPERSEDED"
        assert old_row.generated_docx_content_hash == a1.generated_docx_content_hash
        assert old_row.proposal_revision == 1


def test_stale_revision_rolls_back_artifact_and_leaves_slot_untouched(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)

    a2_wrong = _make_proposal(owner_key, 2, b"v2")
    result = repo.replace_current_proposal(owner_key, a2_wrong, b"v2", expected_previous_revision=99)
    assert result.status == CasReplaceStatus.STALE_REVISION
    assert result.current_artifact.artifact_fingerprint == a1.artifact_fingerprint

    with session_factory() as session:
        assert session.get(CvDocxProposalArtifactRow, a2_wrong.artifact_fingerprint) is None
        slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == a1.artifact_fingerprint
        assert slot.current_revision == 1
        assert slot.slot_version == 1


def test_two_first_writers_never_produce_two_current_proposals(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1a = _make_proposal(owner_key, 1, b"writer A content")
    a1b = _make_proposal(owner_key, 1, b"writer B content")
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name, artifact, content):
        barrier.wait()
        results[name] = repo.replace_current_proposal(owner_key, artifact, content, None)

    t1 = threading.Thread(target=worker, args=("A", a1a, b"writer A content"))
    t2 = threading.Thread(target=worker, args=("B", a1b, b"writer B content"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status.value for r in results.values())
    assert statuses == sorted([CasReplaceStatus.REPLACED.value, CasReplaceStatus.STALE_REVISION.value])

    current = repo.get_current_proposal(owner_key)
    assert current.proposal_revision == 1
    with session_factory() as session:
        count = len(list(session.execute(
            select(CvDocxProposalArtifactRow).where(
                CvDocxProposalArtifactRow.owner_key_fingerprint == owner_key.owner_key_fingerprint,
                CvDocxProposalArtifactRow.proposal_revision == 1,
            )
        ).scalars()))
        assert count == 1


def test_proposal_replace_does_not_touch_pdf_slot(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")
    repo.save_validated_snapshot(snapshot, b"v1")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-1")
    repo.replace_current_pdf(owner_key, pdf1, b"%PDF-1", None)

    a2 = _make_proposal(owner_key, 2, b"v2")
    repo.replace_current_proposal(owner_key, a2, b"v2", expected_previous_revision=1)

    with session_factory() as session:
        pdf_slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert pdf_slot.current_artifact_fingerprint == pdf1.artifact_fingerprint
        assert pdf_slot.current_revision == 1


# 9-12: PDF slot behavior ------------------------------------------------------------


def test_first_pdf_sets_pdf_slot(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")
    repo.save_validated_snapshot(snapshot, b"v1")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-1")

    result = repo.replace_current_pdf(owner_key, pdf1, b"%PDF-1", None)
    assert result.status == CasReplaceStatus.REPLACED
    with session_factory() as session:
        slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == pdf1.artifact_fingerprint
        assert slot.current_revision == 1


def test_second_pdf_replaces_slot_atomically(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")
    repo.save_validated_snapshot(snapshot, b"v1")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-1")
    repo.replace_current_pdf(owner_key, pdf1, b"%PDF-1", None)

    pdf2 = _make_pdf(owner_key, snapshot, 2, b"%PDF-2")
    result = repo.replace_current_pdf(owner_key, pdf2, b"%PDF-2", expected_previous_revision=1)
    assert result.status == CasReplaceStatus.REPLACED
    with session_factory() as session:
        slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == pdf2.artifact_fingerprint
        assert slot.current_revision == 2
        assert slot.slot_version == 2


def test_pdf_replace_does_not_touch_proposal_slot(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")
    repo.save_validated_snapshot(snapshot, b"v1")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-1")
    repo.replace_current_pdf(owner_key, pdf1, b"%PDF-1", None)

    with session_factory() as session:
        proposal_slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert proposal_slot.current_artifact_fingerprint == a1.artifact_fingerprint
        assert proposal_slot.slot_version == 1


def test_pdf_replace_requires_an_existing_source_snapshot(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    phantom_snapshot = _make_snapshot(owner_key, a1, b"v1")  # never saved via save_validated_snapshot
    pdf1 = _make_pdf(owner_key, phantom_snapshot, 1, b"%PDF-1")

    with pytest.raises(CvDocumentStorageError):
        repo.replace_current_pdf(owner_key, pdf1, b"%PDF-1", None)


# 13-18: snapshot storage -------------------------------------------------------------


def test_first_snapshot_save_persists_row_and_blob(repo_env) -> None:
    repo, session_factory, store, _ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")

    result = repo.save_validated_snapshot(snapshot, b"v1")
    assert result.status == SnapshotSaveStatus.SAVED
    assert store.blob_exists(snapshot.exact_docx_sha256)


def test_identical_snapshot_resave_is_idempotent(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    snapshot = _make_snapshot(owner_key, a1, b"v1")
    first = repo.save_validated_snapshot(snapshot, b"v1")
    second = repo.save_validated_snapshot(snapshot, b"v1")
    assert first.status == SnapshotSaveStatus.SAVED
    assert second.status == SnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL


def test_two_snapshot_fingerprints_can_share_the_same_blob(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"shared bytes")
    repo.replace_current_proposal(owner_key, a1, b"shared bytes", None)

    snap_a = _make_manual_snapshot(owner_key, a1, b"shared bytes", "confirmation-A")
    snap_b = _make_manual_snapshot(owner_key, a1, b"shared bytes", "confirmation-B")
    assert snap_a.snapshot_fingerprint != snap_b.snapshot_fingerprint

    result_a = repo.save_validated_snapshot(snap_a, b"shared bytes")
    result_b = repo.save_validated_snapshot(snap_b, b"shared bytes")
    assert result_a.status == SnapshotSaveStatus.SAVED
    assert result_b.status == SnapshotSaveStatus.SAVED


def test_two_owners_can_share_the_same_blob(repo_env) -> None:
    repo, *_ = repo_env
    owner_a = _owner_key("job-A")
    owner_b = _owner_key("job-B")
    content = b"identical bytes shared across two owners"

    a1 = _make_proposal(owner_a, 1, content)
    repo.replace_current_proposal(owner_a, a1, content, None)
    b1 = _make_proposal(owner_b, 1, content)
    result = repo.replace_current_proposal(owner_b, b1, content, None)
    assert result.status == CasReplaceStatus.REPLACED
    assert a1.generated_docx_content_hash == b1.generated_docx_content_hash


def test_two_manual_confirmations_keep_separate_snapshot_lineage(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"manual edit bytes")
    repo.replace_current_proposal(owner_key, a1, b"manual edit bytes", None)

    snap_a = _make_manual_snapshot(owner_key, a1, b"manual edit bytes", "first-confirmation")
    snap_b = _make_manual_snapshot(owner_key, a1, b"manual edit bytes", "second-confirmation")
    repo.save_validated_snapshot(snap_a, b"manual edit bytes")
    repo.save_validated_snapshot(snap_b, b"manual edit bytes")

    from app.services.cv_document_models import CvDocxValidatedSnapshotRow

    with session_factory() as session:
        row_a = session.get(CvDocxValidatedSnapshotRow, snap_a.snapshot_fingerprint)
        row_b = session.get(CvDocxValidatedSnapshotRow, snap_b.snapshot_fingerprint)
        assert row_a is not None and row_b is not None
        assert row_a.snapshot_fingerprint != row_b.snapshot_fingerprint
        assert row_a.blob_sha256 == row_b.blob_sha256


def test_retrieve_snapshot_returns_exact_bytes(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"exact retrieval bytes")
    repo.replace_current_proposal(owner_key, a1, b"exact retrieval bytes", None)
    snapshot = _make_snapshot(owner_key, a1, b"exact retrieval bytes")
    repo.save_validated_snapshot(snapshot, b"exact retrieval bytes")

    stored = repo.retrieve_snapshot(snapshot.exact_docx_sha256)
    assert stored is not None
    stored_snapshot, stored_bytes = stored
    assert stored_bytes == b"exact retrieval bytes"
    assert stored_snapshot.snapshot_fingerprint == snapshot.snapshot_fingerprint


# 19-21: integrity checks block on ingress ----------------------------------------


def test_owner_identity_mismatch_blocks(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    other_owner_key = _owner_key("job-2")
    artifact = _make_proposal(owner_key, 1, b"content")
    with pytest.raises(CvDocumentStorageError):
        repo.replace_current_proposal(other_owner_key, artifact, b"content", None)


def test_artifact_fingerprint_mismatch_blocks(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    artifact = _make_proposal(owner_key, 1, b"content")
    tampered = artifact.model_copy(update={"artifact_fingerprint": "f" * 64})
    with pytest.raises(CvDocumentStorageError):
        repo.replace_current_proposal(owner_key, tampered, b"content", None)


def test_bytes_hash_mismatch_blocks(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    artifact = _make_proposal(owner_key, 1, b"real content")
    with pytest.raises(CvDocumentStorageError):
        repo.replace_current_proposal(owner_key, artifact, b"different bytes entirely", None)


# 22: SQL failure leaves orphan blob but no DB artifact ---------------------------


def test_sql_failure_after_blob_write_leaves_orphan_blob_not_db_artifact(repo_env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, session_factory, store, _ = repo_env
    owner_key = _owner_key()
    artifact = _make_proposal(owner_key, 1, b"orphan test content")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure after blob write")

    monkeypatch.setattr(repo, "_ensure_owner", boom)

    with pytest.raises(RuntimeError):
        repo.replace_current_proposal(owner_key, artifact, b"orphan test content", None)

    assert store.blob_exists(artifact.generated_docx_content_hash)
    with session_factory() as session:
        assert session.get(CvDocxProposalArtifactRow, artifact.artifact_fingerprint) is None


# 23-24: reads use slot pointer + rehash bytes -----------------------------------


def test_get_current_proposal_reads_slot_pointer_not_latest_row(repo_env) -> None:
    repo, session_factory, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"v1")
    repo.replace_current_proposal(owner_key, a1, b"v1", None)
    a2 = _make_proposal(owner_key, 2, b"v2")
    repo.replace_current_proposal(owner_key, a2, b"v2", expected_previous_revision=1)

    # Manually mutate the slot to point at the older artifact -- proves the
    # getter follows the slot pointer, never "the row with the max revision".
    with session_factory() as session:
        with session.begin():
            slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
            slot.current_artifact_fingerprint = a1.artifact_fingerprint
            slot.current_revision = 1

    current = repo.get_current_proposal(owner_key)
    assert current.artifact_fingerprint == a1.artifact_fingerprint


def test_read_current_proposal_bytes_rehashes_and_fails_closed_on_tamper(repo_env) -> None:
    repo, _, store, _ = repo_env
    owner_key = _owner_key()
    content = b"bytes that get tampered on disk"
    a1 = _make_proposal(owner_key, 1, content)
    repo.replace_current_proposal(owner_key, a1, content, None)

    sha = a1.generated_docx_content_hash
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"tampered bytes on disk")

    assert repo.read_current_proposal_bytes(owner_key) is None


# 26: blob media_type is part of the reuse identity, not just size/locator --------


def test_same_blob_sha256_cannot_be_rebound_to_different_media_type(repo_env) -> None:
    repo, session_factory, store, _ = repo_env
    owner_key = _owner_key()
    content = b"exact bytes shared between a docx proposal and a rebind attempt"

    a1 = _make_proposal(owner_key, 1, content)
    repo.replace_current_proposal(owner_key, a1, content, None)
    snapshot = _make_snapshot(owner_key, a1, content)
    repo.save_validated_snapshot(snapshot, content)

    blob_sha256 = hashlib.sha256(content).hexdigest()
    with session_factory() as session:
        blob_row = session.get(CvDocumentBlob, blob_sha256)
        assert blob_row is not None
        assert blob_row.media_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        original_byte_size = blob_row.byte_size
        original_locator = blob_row.storage_locator

    # Craft a PDF artifact whose "pdf" bytes are deliberately identical to
    # the already-stored DOCX bytes, so it hashes to the same blob_sha256
    # and tries to rebind that row's media_type from DOCX to PDF.
    pdf1 = _make_pdf(owner_key, snapshot, 1, content)
    assert pdf1.pdf_sha256 == blob_sha256

    with pytest.raises(CvDocumentStorageError) as exc_info:
        repo.replace_current_pdf(owner_key, pdf1, content, None)
    assert exc_info.value.result.error_code == CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT

    with session_factory() as session:
        blob_row = session.get(CvDocumentBlob, blob_sha256)
        assert blob_row is not None
        assert blob_row.media_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert blob_row.byte_size == original_byte_size
        assert blob_row.storage_locator == original_locator

        assert session.get(CvConfirmedPdfArtifactRow, pdf1.artifact_fingerprint) is None

        pdf_slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert pdf_slot is None

        proposal_slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert proposal_slot is not None
        assert proposal_slot.current_artifact_fingerprint == a1.artifact_fingerprint
        assert proposal_slot.current_revision == 1

        from app.services.cv_document_models import CvDocxValidatedSnapshotRow

        snapshot_row = session.get(CvDocxValidatedSnapshotRow, snapshot.snapshot_fingerprint)
        assert snapshot_row is not None
        assert snapshot_row.blob_sha256 == blob_sha256

    assert repo.get_current_proposal(owner_key).artifact_fingerprint == a1.artifact_fingerprint
    stored = repo.retrieve_snapshot(snapshot.exact_docx_sha256)
    assert stored is not None
    stored_snapshot, stored_bytes = stored
    assert stored_snapshot.snapshot_fingerprint == snapshot.snapshot_fingerprint
    assert stored_bytes == content
    assert store.blob_exists(blob_sha256)


# 25: defensive copies ------------------------------------------------------------


def test_returned_domain_objects_are_defensive_copies(repo_env) -> None:
    repo, *_ = repo_env
    owner_key = _owner_key()
    a1 = _make_proposal(owner_key, 1, b"defensive copy test")
    repo.replace_current_proposal(owner_key, a1, b"defensive copy test", None)

    first = repo.get_current_proposal(owner_key)
    second = repo.get_current_proposal(owner_key)
    assert first is not second
    assert first == second

    with pytest.raises(Exception):
        first.proposal_revision = 999  # type: ignore[misc]

    unrelated = first.model_copy(update={"proposal_revision": 42})
    assert unrelated.proposal_revision == 42
    assert repo.get_current_proposal(owner_key).proposal_revision == 1
