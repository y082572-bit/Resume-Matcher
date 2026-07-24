"""Unit tests for ``FinalDocxSnapshotSqlRepository``: Protocol conformance,
lineage/blob invariants, the closed error channel (no leaked
``CvDocumentStorageError``/``SQLAlchemyError``), and concurrency.
"""

from __future__ import annotations

import hashlib
import inspect
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_final_snapshot_repository import (
    FinalDocxSnapshotRepository,
    FinalSnapshotSaveStatus,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_models import (
    CvDocumentPdfSlot,
    CvDocumentProposalSlot,
    CvDocxFinalSnapshotRow,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


_POLICY_VERSION = "final-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'final_snapshot_repo.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    return final_repo, base_repo, session_factory, store, engine


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


def _make_final_snapshot(
    owner_key,
    proposal: CvDocxProposalArtifact,
    final_bytes: bytes,
    *,
    policy_version: str = _POLICY_VERSION,
) -> FinalDocxSnapshot:
    final_hash = _sha256(final_bytes)
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=proposal.proposal_revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=policy_version,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=proposal.proposal_revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=proposal.generated_docx_content_hash != final_hash,
        finalization_policy_version=policy_version,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def _setup_proposal(base_repo, owner_key, revision: int, content: bytes) -> CvDocxProposalArtifact:
    proposal = _make_proposal(owner_key, revision, content)
    expected_previous = revision - 1 or None
    result = base_repo.replace_current_proposal(owner_key, proposal, content, expected_previous)
    assert result.status.value == "REPLACED"
    return proposal


# 1: Protocol conformance ----------------------------------------------------------


def test_repository_is_a_runtime_checkable_protocol_instance(env) -> None:
    final_repo, *_ = env
    assert isinstance(final_repo, FinalDocxSnapshotRepository)


# 2-3: first save / idempotent resave ----------------------------------------------


def test_first_save_returns_saved(env) -> None:
    final_repo, base_repo, _, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"generated proposal bytes")
    snapshot = _make_final_snapshot(owner_key, proposal, b"final docx bytes, unmodified")

    result = final_repo.save_final_docx_snapshot(snapshot, b"final docx bytes, unmodified")
    assert result.status == FinalSnapshotSaveStatus.SAVED
    assert store.blob_exists(snapshot.final_docx_sha256)


def test_identical_resave_is_idempotent(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"generated proposal bytes v2")
    snapshot = _make_final_snapshot(owner_key, proposal, b"final docx bytes v2")

    first = final_repo.save_final_docx_snapshot(snapshot, b"final docx bytes v2")
    second = final_repo.save_final_docx_snapshot(snapshot, b"final docx bytes v2")
    assert first.status == FinalSnapshotSaveStatus.SAVED
    assert second.status == FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL


# 4-5: lineage/bytes dedup -----------------------------------------------------------


def test_two_lineage_records_can_share_the_same_bytes(env) -> None:
    final_repo, base_repo, session_factory, _, _ = env
    owner_a = _owner_key("job-A")
    owner_b = _owner_key("job-B")
    shared_final_bytes = b"identical final bytes shared across two lineages"

    proposal_a = _setup_proposal(base_repo, owner_a, 1, b"proposal content A")
    proposal_b = _setup_proposal(base_repo, owner_b, 1, b"proposal content B")

    snap_a = _make_final_snapshot(owner_a, proposal_a, shared_final_bytes)
    snap_b = _make_final_snapshot(owner_b, proposal_b, shared_final_bytes)
    assert snap_a.final_snapshot_fingerprint != snap_b.final_snapshot_fingerprint
    assert snap_a.final_docx_sha256 == snap_b.final_docx_sha256

    result_a = final_repo.save_final_docx_snapshot(snap_a, shared_final_bytes)
    result_b = final_repo.save_final_docx_snapshot(snap_b, shared_final_bytes)
    assert result_a.status == FinalSnapshotSaveStatus.SAVED
    assert result_b.status == FinalSnapshotSaveStatus.SAVED

    with session_factory() as session:
        row_a = session.get(CvDocxFinalSnapshotRow, snap_a.final_snapshot_fingerprint)
        row_b = session.get(CvDocxFinalSnapshotRow, snap_b.final_snapshot_fingerprint)
        assert row_a.final_docx_sha256 == row_b.final_docx_sha256


def test_one_proposal_revision_can_have_many_final_snapshots(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"one proposal, many finalizations")

    snap_1 = _make_final_snapshot(owner_key, proposal, b"first user edit")
    snap_2 = _make_final_snapshot(owner_key, proposal, b"second, different user edit")
    assert snap_1.final_snapshot_fingerprint != snap_2.final_snapshot_fingerprint

    result_1 = final_repo.save_final_docx_snapshot(snap_1, b"first user edit")
    result_2 = final_repo.save_final_docx_snapshot(snap_2, b"second, different user edit")
    assert result_1.status == FinalSnapshotSaveStatus.SAVED
    assert result_2.status == FinalSnapshotSaveStatus.SAVED


# 6-7: retrieval ---------------------------------------------------------------------


def test_metadata_retrieval_by_fingerprint(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"metadata retrieval proposal")
    snapshot = _make_final_snapshot(owner_key, proposal, b"metadata retrieval final bytes")
    final_repo.save_final_docx_snapshot(snapshot, b"metadata retrieval final bytes")

    stored = final_repo.get_final_docx_snapshot(snapshot.final_snapshot_fingerprint)
    assert stored == snapshot


def test_bytes_retrieval_by_hash(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"bytes retrieval proposal")
    final_bytes = b"exact bytes that must round-trip"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)
    final_repo.save_final_docx_snapshot(snapshot, final_bytes)

    stored_bytes = final_repo.retrieve_final_docx_bytes(snapshot.final_docx_sha256)
    assert stored_bytes == final_bytes


def test_get_final_docx_snapshot_returns_none_for_unknown_fingerprint(env) -> None:
    final_repo, *_ = env
    assert final_repo.get_final_docx_snapshot("f" * 64) is None


def test_retrieve_final_docx_bytes_returns_none_for_unknown_hash(env) -> None:
    final_repo, *_ = env
    assert final_repo.retrieve_final_docx_bytes("f" * 64) is None


def test_no_metadata_lookup_by_bytes_hash_exists(env) -> None:
    """The Protocol (and this implementation) never offers a way to look up
    a ``FinalDocxSnapshot`` by ``final_docx_sha256`` -- only by
    ``final_snapshot_fingerprint``. A caller with only a bytes hash can
    never get "some" metadata record back."""

    final_repo, *_ = env
    public_methods = {
        name for name, _ in inspect.getmembers(final_repo, predicate=inspect.ismethod) if not name.startswith("_")
    }
    assert public_methods == {
        "save_final_docx_snapshot",
        "get_final_docx_snapshot",
        "retrieve_final_docx_bytes",
    }


# 8: no slot changes ------------------------------------------------------------------


def test_save_does_not_create_any_proposal_or_pdf_slot(env) -> None:
    final_repo, base_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"no slot side effect proposal")
    snapshot = _make_final_snapshot(owner_key, proposal, b"no slot side effect final bytes")
    final_repo.save_final_docx_snapshot(snapshot, b"no slot side effect final bytes")

    with session_factory() as session:
        # The proposal slot already exists from _setup_proposal's own
        # replace_current_proposal call -- prove the final-snapshot save
        # left its revision/fingerprint completely untouched, and that no
        # PDF slot was ever created.
        proposal_slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        assert proposal_slot.current_artifact_fingerprint == proposal.artifact_fingerprint
        assert proposal_slot.current_revision == 1
        assert session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint) is None


# 9-12: lineage mismatches ------------------------------------------------------------


def test_missing_source_proposal_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"never actually saved")
    final_bytes = b"final bytes for missing proposal test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SOURCE_PROPOSAL_NOT_FOUND


def test_source_proposal_owner_mismatch_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_a = _owner_key("job-owner-a")
    owner_b = _owner_key("job-owner-b")
    proposal_a = _setup_proposal(base_repo, owner_a, 1, b"owner A proposal content")

    final_bytes = b"final bytes claiming owner B but proposal belongs to A"
    # Build a snapshot whose owner_key is B but whose source proposal
    # fingerprint belongs to A's proposal row.
    final_hash = _sha256(final_bytes)
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_b.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_a.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal_a.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_b,
        source_proposal_artifact_fingerprint=proposal_a.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal_a.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=proposal_a.generated_docx_content_hash != final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SOURCE_PROPOSAL_OWNER_MISMATCH


def test_source_proposal_revision_mismatch_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"revision mismatch proposal content")
    final_bytes = b"final bytes for revision mismatch test"
    final_hash = _sha256(final_bytes)

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=2,  # the real row is revision 1
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=2,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=proposal.generated_docx_content_hash != final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SOURCE_PROPOSAL_REVISION_MISMATCH


def test_source_proposal_hash_mismatch_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"hash mismatch proposal content")
    final_bytes = b"final bytes for generated-hash mismatch test"
    final_hash = _sha256(final_bytes)
    wrong_generated_hash = _sha256(b"this is not the real generated proposal content")

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=wrong_generated_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=wrong_generated_hash,
        final_docx_sha256=final_hash,
        edited_by_user=wrong_generated_hash != final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SOURCE_PROPOSAL_HASH_MISMATCH


# 13-14: recompute mismatches (hash / fingerprint) ------------------------------------


def test_docx_hash_mismatch_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"docx hash mismatch proposal")
    snapshot = _make_final_snapshot(owner_key, proposal, b"the bytes the fingerprint was built from")

    result = final_repo.save_final_docx_snapshot(snapshot, b"completely different bytes entirely")
    assert result.status == FinalSnapshotSaveStatus.DOCX_HASH_MISMATCH


def test_tampered_final_snapshot_fingerprint_blocks(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"tampered fingerprint proposal")
    final_bytes = b"final bytes for tampered fingerprint test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)
    tampered = snapshot.model_copy(update={"final_snapshot_fingerprint": "e" * 64})

    result = final_repo.save_final_docx_snapshot(tampered, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SNAPSHOT_FINGERPRINT_MISMATCH


# 15: media type conflict blocks -------------------------------------------------------


def test_media_type_conflict_blocks(env) -> None:
    final_repo, base_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"media type conflict proposal")

    # Publish a blob under a non-DOCX media type first (simulating some
    # other artifact kind having already claimed this exact content hash).
    shared_content = b"bytes that will collide across media types"
    store.write_blob(shared_content, media_type="application/pdf")
    with session_factory() as session:
        from app.services.cv_document_models import CvDocumentBlob
        from app.schemas.cv_document_storage import DocumentBlobStorageStatus

        with session.begin():
            session.add(
                CvDocumentBlob(
                    blob_sha256=_sha256(shared_content),
                    byte_size=len(shared_content),
                    media_type="application/pdf",
                    storage_locator=store.locator_for(_sha256(shared_content)),
                    storage_status=DocumentBlobStorageStatus.OK.value,
                )
            )

    snapshot = _make_final_snapshot(owner_key, proposal, shared_content)
    result = final_repo.save_final_docx_snapshot(snapshot, shared_content)
    assert result.status == FinalSnapshotSaveStatus.STORAGE_METADATA_CONFLICT

    with session_factory() as session:
        assert session.get(CvDocxFinalSnapshotRow, snapshot.final_snapshot_fingerprint) is None


# 16: corrupted blob is never overwritten ----------------------------------------------


def test_corrupted_existing_blob_is_never_overwritten(env) -> None:
    final_repo, base_repo, _, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"corrupted blob proposal")
    final_bytes = b"bytes whose on-disk blob will be corrupted first"
    final_hash = _sha256(final_bytes)

    # Corrupt the managed tree ahead of time: write a final blob path with
    # the wrong content already occupying that content-address.
    final_path_parent = store.blobs_dir / final_hash[0:2] / final_hash[2:4]
    final_path_parent.mkdir(parents=True, exist_ok=True)
    (final_path_parent / f"{final_hash}.blob").write_bytes(b"pre-existing corrupted bytes")

    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)
    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE

    # The corrupted file on disk must remain untouched.
    assert (final_path_parent / f"{final_hash}.blob").read_bytes() == b"pre-existing corrupted bytes"


# 17-18: DB failure orphan / no partial state -------------------------------------------


def test_db_failure_after_blob_write_leaves_orphan_blob_not_db_row(env, monkeypatch: pytest.MonkeyPatch) -> None:
    final_repo, base_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"orphan blob proposal")
    final_bytes = b"orphan blob test final bytes"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure after blob write")

    monkeypatch.setattr(final_repo, "_insert_snapshot_transaction", boom)

    with pytest.raises(RuntimeError):
        final_repo.save_final_docx_snapshot(snapshot, final_bytes)

    assert store.blob_exists(snapshot.final_docx_sha256)
    with session_factory() as session:
        assert session.get(CvDocxFinalSnapshotRow, snapshot.final_snapshot_fingerprint) is None


def test_failure_leaves_no_partial_db_state(env) -> None:
    final_repo, base_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"partial state proposal")
    final_bytes = b"final bytes for partial-state test"
    final_hash = _sha256(final_bytes)

    # Force a lineage mismatch (owner never created for this fingerprint) --
    # confirm the final snapshot row was never inserted at all.
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint="d" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="d" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=proposal.generated_docx_content_hash != final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SOURCE_PROPOSAL_NOT_FOUND
    with session_factory() as session:
        assert session.get(CvDocxFinalSnapshotRow, fp) is None


# 19: STORAGE_UNAVAILABLE on OperationalError -------------------------------------------


def test_operational_error_maps_to_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"operational error proposal")
    final_bytes = b"final bytes for operational error test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    def boom(*args, **kwargs):
        raise OperationalError("statement", {}, Exception("db locked"))

    monkeypatch.setattr(final_repo, "_insert_snapshot_transaction", boom)

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE


# 20-22: concurrency -------------------------------------------------------------------


def test_identical_concurrent_writers_converge(env) -> None:
    final_repo, base_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"concurrency proposal content")
    final_bytes = b"identical final bytes for concurrency test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = final_repo.save_final_docx_snapshot(snapshot, final_bytes)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status.value for r in results.values())
    assert statuses == sorted(
        [FinalSnapshotSaveStatus.SAVED.value, FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL.value]
    )

    with session_factory() as session:
        count = len(
            list(
                session.execute(
                    select(CvDocxFinalSnapshotRow).where(
                        CvDocxFinalSnapshotRow.final_snapshot_fingerprint == snapshot.final_snapshot_fingerprint
                    )
                ).scalars()
            )
        )
        assert count == 1


def test_existing_row_with_different_metadata_reports_conflict(env) -> None:
    """A pre-existing row under the same PK with tampered fields is
    detected by the ordinary existing-row check inside
    ``_insert_snapshot_transaction`` (not the IntegrityError path -- see
    ``test_reread_after_integrity_error_never_leaks_raw_integrity_error``
    below for that one)."""

    final_repo, base_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"integrity conflict proposal")
    final_bytes = b"final bytes whose fingerprint will collide"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    # First, actually save it for real.
    first = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert first.status == FinalSnapshotSaveStatus.SAVED

    # Now tamper the stored row's finalization_policy_version directly (bypassing
    # the repository) so a resave of the *same* snapshot object would --
    # after passing this repository's own pre-write recompute checks --
    # find a stored row whose fields differ from what it is about to insert.
    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET finalization_policy_version = :bad "
                "WHERE final_snapshot_fingerprint = :fp"
            ),
            {"bad": "tampered-policy-version", "fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status == FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT


def test_reread_after_integrity_error_never_leaks_raw_integrity_error(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    final_repo, base_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"raw integrity error proposal")
    final_bytes = b"final bytes for raw integrity error test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    real_factory = final_repo._session_factory
    call_count = {"n": 0}

    class _ExplodingSession:
        def __init__(self, real_session):
            self._real = real_session

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

        def begin(self):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
            return self._real.begin()

        def __getattr__(self, item):
            return getattr(self._real, item)

    def fake_factory():
        return _ExplodingSession(real_factory())

    monkeypatch.setattr(final_repo, "_session_factory", fake_factory)

    # No exception should propagate -- the repository must map the raw
    # IntegrityError into a closed FinalSnapshotSaveResult.
    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status in (
        FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT,
        FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL,
    )


# 23: no leaked infrastructure exceptions from the module's public surface -------------


def test_public_methods_never_raise_cv_document_storage_error(env) -> None:
    final_repo, base_repo, _, _, _ = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"no leaked exception proposal")
    final_bytes = b"final bytes for no-leak test"
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)

    try:
        final_repo.save_final_docx_snapshot(snapshot, final_bytes)
        final_repo.get_final_docx_snapshot(snapshot.final_snapshot_fingerprint)
        final_repo.retrieve_final_docx_bytes(snapshot.final_docx_sha256)
        final_repo.retrieve_final_docx_bytes("not-a-valid-sha256-at-all")
    except CvDocumentStorageError:
        pytest.fail("CvDocumentStorageError must never leak from the public repository surface")
