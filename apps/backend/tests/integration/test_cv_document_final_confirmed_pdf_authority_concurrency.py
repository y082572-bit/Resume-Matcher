"""Explicit Provenance Stage P6-B3b-A: integration/concurrency tests for
``FinalConfirmedPdfSqlRepository`` -- exactly one winner on every race, the
loser always reporting a fresh observation, zero retries, and orphan blobs
tolerated for a losing writer.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base
import app.services.cv_document_models as cdm
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    build_final_confirmed_pdf_artifact,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import run_cutover_installer
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfSaveStatus,
)
from app.services.cv_document_final_confirmed_pdf_sql_repository import FinalConfirmedPdfSqlRepository
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


_POLICY_VERSION = "concurrency-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrency.db'}", future=True)
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_snapshot_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    repo = FinalConfirmedPdfSqlRepository(session_factory, store)
    return repo, base_repo, final_snapshot_repo, session_factory, store, engine


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _runtime_identity():
    return build_converter_runtime_identity(
        implementation_id="libreoffice", implementation_version="7.6", executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2", executable_sha256="a" * 64, platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )


def _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, content: bytes) -> FinalDocxSnapshot:
    gen_hash = _sha256(content)
    gen_input_fp = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None, template_fingerprint="c" * 64,
        renderer_adapter_id="fake-renderer", rendering_policy_version="v1",
        document_schema_version="cv-document-proposal-schema-v1",
    )
    artifact_fp = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, proposal_revision=1,
    )
    proposal = CvDocxProposalArtifact(
        artifact_id=uuid4(), owner_key=owner_key, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1", rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer", template_fingerprint="c" * 64, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, current_file_hash=gen_hash, validated_file_hash=None,
        proposal_revision=1, superseded=False, artifact_fingerprint=artifact_fp,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    base_repo.replace_current_proposal(owner_key, proposal, content, None)

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1, generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash, finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key, source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1, generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash, edited_by_user=False, finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION, final_snapshot_fingerprint=fp,
    )
    final_snapshot_repo.save_final_docx_snapshot(snapshot, content)
    return snapshot


def _make_artifact(owner_key, snapshot: FinalDocxSnapshot, pdf_bytes: bytes, *, runtime_identity=None):
    return build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=runtime_identity or _runtime_identity(),
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(pdf_bytes),
    )


# 1-3: two absent-row authority inserts ----------------------------------------------


def test_two_absent_row_authority_inserts_exactly_one_promoted(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"absent row race content")
    artifact = _make_artifact(owner_key, snapshot, b"absent row race pdf")
    repo.save_artifact(artifact, b"absent row race pdf")

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = repo.promote_current_authority(
            owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
        )

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [r.status for r in results.values()]
    assert statuses.count(FinalConfirmedPdfAuthorityCasStatus.PROMOTED) == 1
    assert statuses.count(FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION) == 1

    loser = next(r for r in results.values() if r.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION)
    assert loser.current_observation.exists is True
    assert loser.current_observation.slot_version == 1


# 4-6: two existing-row updates with the same slot_version -----------------------------


def test_two_existing_row_updates_same_slot_version_exactly_one_promoted(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"existing row race content")
    artifact = _make_artifact(owner_key, snapshot, b"existing row race pdf")
    repo.save_artifact(artifact, b"existing row race pdf")
    first = repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert first.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, first.current_observation)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [r.status for r in results.values()]
    assert statuses.count(FinalConfirmedPdfAuthorityCasStatus.PROMOTED) == 1
    assert statuses.count(FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION) == 1

    loser = next(r for r in results.values() if r.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION)
    assert loser.current_observation.slot_version == 2


# 7-9: two same-request artifact saves with different PDF bytes -------------------------


def test_two_same_request_saves_different_bytes_one_saved_one_canonical(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"same request race content")
    rt = _runtime_identity()
    artifact_a = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"race pdf bytes a"),
    )
    artifact_b = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"race pdf bytes b"),
    )
    assert artifact_a.conversion_request_fingerprint == artifact_b.conversion_request_fingerprint

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, artifact, pdf_bytes: bytes) -> None:
        barrier.wait()
        results[name] = repo.save_artifact(artifact, pdf_bytes)

    t1 = threading.Thread(target=worker, args=("A", artifact_a, b"race pdf bytes a"))
    t2 = threading.Thread(target=worker, args=("B", artifact_b, b"race pdf bytes b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [r.status for r in results.values()]
    assert statuses.count(FinalConfirmedPdfSaveStatus.SAVED) == 1
    assert statuses.count(FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL) == 1

    # 10: canonical artifact is unambiguous -- both results point at the same winner.
    winner_fingerprints = {r.artifact.artifact_fingerprint for r in results.values()}
    assert len(winner_fingerprints) == 1

    with session_factory() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM cv_final_confirmed_pdf_artifacts WHERE conversion_request_fingerprint = :fp"),
            {"fp": artifact_a.conversion_request_fingerprint},
        ).scalar_one()
        assert count == 1

    # 11: the losing writer's blob may remain an orphan on disk -- the repo
    # never deletes it automatically.
    loser = artifact_a if results["A"].status == FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL else artifact_b
    assert store.blob_exists(loser.pdf_sha256)


# 12: no second write retry -------------------------------------------------------------


def test_no_second_write_retry_on_stale_cas(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"no retry content")
    artifact = _make_artifact(owner_key, snapshot, b"no retry pdf")
    repo.save_artifact(artifact, b"no retry pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    reread_calls = {"n": 0}
    real_reread = repo._reread_authority_after_race

    def spying_reread(owner_key_arg):
        reread_calls["n"] += 1
        return real_reread(owner_key_arg)

    monkeypatch.setattr(repo, "_reread_authority_after_race", spying_reread)

    # Stale expected_observation (absent) against an already-PROMOTED row --
    # this hits the ordinary rowcount==0 -> STALE_REVISION branch, never the
    # IntegrityError/_reread_authority_after_race path, confirming there is
    # no hidden second write attempt either way.
    result = repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION
    assert reread_calls["n"] == 0
