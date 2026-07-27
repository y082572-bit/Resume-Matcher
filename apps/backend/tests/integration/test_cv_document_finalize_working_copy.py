"""End-to-end integration tests for the P6-B2b-B finalization flow: a real
``FilesystemFinalDocxSourceReader``, a real ``ProposalArtifactLineageSqlReader``,
a real ``FinalDocxSnapshotSqlRepository``, a real temporary SQLite database,
and the real ``WorkingCopyStore``/``PlatformOwnerLock`` -- driven entirely
through the public composition root,
``finalize_working_copy_to_final_docx_snapshot``.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotBuildStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_final_source_filesystem_reader import FilesystemFinalDocxSourceReader
from app.services.cv_document_finalize_working_copy import finalize_working_copy_to_final_docx_snapshot
from app.services.cv_document_models import CvDocxFinalSnapshotRow
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_artifact_lineage_sql_reader import ProposalArtifactLineageSqlReader
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_store import WorkingCopyStore


FINALIZATION_POLICY_VERSION = "cv-document-finalize-working-copy-e2e-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'e2e_finalize.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    blob_store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    artifact_repository = CvDocumentArtifactSqlRepository(session_factory, blob_store)
    lineage_reader = ProposalArtifactLineageSqlReader(session_factory, blob_store)
    snapshot_repository = FinalDocxSnapshotSqlRepository(session_factory, blob_store)

    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store")
    working_copy_store = WorkingCopyStore(paths, max_docx_size_bytes=10_000_000)
    source_reader = FilesystemFinalDocxSourceReader(
        paths, lineage_reader, max_docx_size_bytes=10_000_000, max_binding_size_bytes=1_000_000
    )

    return dict(
        session_factory=session_factory,
        blob_store=blob_store,
        artifact_repository=artifact_repository,
        working_copy_store=working_copy_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        paths=paths,
    )


def _owner_key(reference_id: str = "job-e2e-finalize-1"):
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


def _setup_proposal(artifact_repository, owner_key, revision: int, content: bytes) -> CvDocxProposalArtifact:
    proposal = _make_proposal(owner_key, revision, content)
    expected_previous = revision - 1 or None
    result = artifact_repository.replace_current_proposal(owner_key, proposal, content, expected_previous)
    assert result.status.value == "REPLACED"
    return proposal


def _finalize(env, owner_key):
    return finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=env["working_copy_store"],
        source_reader=env["source_reader"],
        snapshot_repository=env["snapshot_repository"],
        artifact_repository=env["artifact_repository"],
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )


# -- unedited working copy: edited_by_user is False -----------------------------------


def test_unedited_working_copy_finalizes_with_edited_by_user_false(env) -> None:
    owner_key = _owner_key()
    content = b"the proposal's original, unedited content"
    _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    result = _finalize(env, owner_key)
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.edited_by_user is False
    assert result.snapshot.final_docx_sha256 == _sha256(content)
    assert result.source_proposal_is_current is True


# -- user-edited working copy: edited_by_user is True ----------------------------------


def test_user_edited_working_copy_finalizes_with_edited_by_user_true(env) -> None:
    owner_key = _owner_key()
    content = b"the proposal's original content, before the user opens Word"
    _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    edited_bytes = b"the user opened this in Word and typed something new here"
    owner_fp = owner_key.owner_key_fingerprint
    env["paths"].current_docx_path(owner_fp).write_bytes(edited_bytes)

    result = _finalize(env, owner_key)
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.edited_by_user is True
    assert result.snapshot.final_docx_sha256 == _sha256(edited_bytes)


# -- stale but valid Proposal N while current is N+1 -----------------------------------


def test_stale_but_valid_proposal_n_finalizes_while_current_is_n_plus_1(env) -> None:
    owner_key = _owner_key()
    content_n = b"historical proposal N content, still bound in the working copy"
    proposal_n = _setup_proposal(env["artifact_repository"], owner_key, 1, content_n)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    # Advance the current slot to N+1 without refreshing the working copy --
    # the sidecar still names proposal N.
    _setup_proposal(env["artifact_repository"], owner_key, 2, b"current proposal N+1 content")

    result = _finalize(env, owner_key)
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.source_proposal_artifact_fingerprint == proposal_n.artifact_fingerprint
    assert result.snapshot.source_proposal_revision == 1
    assert result.snapshot.final_docx_sha256 == _sha256(content_n)
    assert result.source_proposal_is_current is False


# -- a tampered Proposal blob blocks capture (never trusts the DB row alone) -----------


def test_tampered_proposal_blob_bytes_block_finalization(env) -> None:
    """Tampering the Proposal's own repository blob bytes on disk must
    block ``FinalDocxSourceCapture`` construction entirely -- the real
    content-addressed ``CvDocumentBlobStore`` itself detects a blob whose
    bytes no longer hash to its own content address (a lower-level
    integrity failure than a same-hash-but-wrong-content row/blob
    disagreement, which the SQL lineage reader's own
    ``BYTES_HASH_MISMATCH`` unit tests cover directly), so this end-to-end
    failure surfaces as ``STORAGE_UNAVAILABLE``. Either way, finalization
    is blocked and no snapshot is ever produced."""

    owner_key = _owner_key()
    content = b"proposal content whose blob will be corrupted after publish"
    proposal = _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    blob_store = env["blob_store"]
    blob_dir = blob_store.blobs_dir / proposal.generated_docx_content_hash[0:2] / proposal.generated_docx_content_hash[2:4]
    (blob_dir / f"{proposal.generated_docx_content_hash}.blob").write_bytes(b"tampered proposal blob bytes")

    result = _finalize(env, owner_key)
    assert result.status != FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.status == FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE
    assert result.snapshot is None


def test_row_blob_sha256_disagreeing_with_generated_hash_blocks_via_bytes_hash_mismatch(env) -> None:
    """A row whose ``blob_sha256`` column points at a *different*,
    validly-stored blob than what its own ``generated_docx_content_hash``
    declares -- e.g. a corrupted SQL row, independent of the blob layer's
    own content-addressing integrity -- must surface end-to-end as
    ``SOURCE_PROPOSAL_HASH_MISMATCH`` (the lineage reader's
    ``BYTES_HASH_MISMATCH``), never a silently-accepted capture."""

    owner_key = _owner_key()
    content = b"proposal content whose row will be redirected to a different blob"
    proposal = _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    other_content = b"a completely different, validly-stored blob"
    write_result = env["blob_store"].write_blob(other_content)
    assert write_result.success

    from sqlalchemy import text

    with env["session_factory"]() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text("UPDATE cv_docx_proposal_artifacts SET blob_sha256 = :other WHERE artifact_fingerprint = :fp"),
            {"other": write_result.blob_sha256, "fp": proposal.artifact_fingerprint},
        )
        session.commit()

    result = _finalize(env, owner_key)
    assert result.status == FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_HASH_MISMATCH
    assert result.snapshot is None


# -- concurrent identical finalization converges on one logical snapshot, no duplicate --


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX owner-lock backend")
def test_concurrent_identical_finalization_yields_one_logical_snapshot(env) -> None:
    owner_key = _owner_key()
    content = b"unedited proposal content for the concurrency test"
    _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    results: list = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait(timeout=5)
        outcome = _finalize(env, owner_key)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 6
    fingerprints = {r.snapshot.final_snapshot_fingerprint for r in results if r.status == FinalDocxSnapshotBuildStatus.FINALIZED}
    assert all(r.status == FinalDocxSnapshotBuildStatus.FINALIZED for r in results)
    assert len(fingerprints) == 1

    with env["session_factory"]() as session:
        rows = list(
            session.execute(
                select(CvDocxFinalSnapshotRow).where(
                    CvDocxFinalSnapshotRow.final_snapshot_fingerprint == next(iter(fingerprints))
                )
            ).scalars()
        )
        assert len(rows) == 1


def test_replay_finalization_does_not_duplicate(env) -> None:
    owner_key = _owner_key()
    content = b"unedited proposal content for the replay test"
    _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    first = _finalize(env, owner_key)
    second = _finalize(env, owner_key)
    assert first.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert second.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert first.snapshot.final_snapshot_fingerprint == second.snapshot.final_snapshot_fingerprint

    with env["session_factory"]() as session:
        rows = list(
            session.execute(
                select(CvDocxFinalSnapshotRow).where(
                    CvDocxFinalSnapshotRow.final_snapshot_fingerprint == first.snapshot.final_snapshot_fingerprint
                )
            ).scalars()
        )
        assert len(rows) == 1


# -- end-to-end LOCK_PATH_IDENTITY_MISMATCH never becomes LOCK_CAPABILITY_INVALID ------


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX owner-lock backend")
def test_lock_path_identity_mismatch_is_never_reported_as_lock_capability_invalid(env) -> None:
    owner_key = _owner_key()
    content = b"proposal content for the lock-path-identity-mismatch test"
    _setup_proposal(env["artifact_repository"], owner_key, 1, content)
    env["working_copy_store"].create_or_refresh_working_copy(owner_key=owner_key, repository=env["artifact_repository"])

    owner_fp = owner_key.owner_key_fingerprint
    lock_path = env["paths"].lock_path(owner_fp)
    lock_path.unlink()
    lock_path.symlink_to(env["paths"].current_docx_path(owner_fp))

    result = _finalize(env, owner_key)
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH
    assert result.status != FinalDocxSnapshotBuildStatus.LOCK_CAPABILITY_INVALID
