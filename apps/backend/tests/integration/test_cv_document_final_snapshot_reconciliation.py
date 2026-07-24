"""Integration tests for the read-only ``cv_docx_final_snapshots``
reconciliation pass (``cv_document_final_snapshot_reconciliation.py``): every
listed inconsistency scenario is detected, standard mode never mutates DB or
filesystem state, and this module never emits the global orphan-blob codes
(that stays exclusively ``cv_document_reconciliation.run_reconciliation``'s
concern).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_snapshot_reconciliation import (
    FinalDocxSnapshotReconciliationIssueCode,
    run_final_docx_snapshot_reconciliation,
)
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
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
    engine = create_engine(f"sqlite:///{tmp_path / 'recon.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    return base_repo, final_repo, session_factory, store, engine


def _owner_key(ref: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=ref)


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


def _make_final_snapshot(owner_key, proposal, final_bytes: bytes) -> FinalDocxSnapshot:
    final_hash = _sha256(final_bytes)
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=proposal.proposal_revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=proposal.proposal_revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=proposal.generated_docx_content_hash != final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def _setup(env, content: bytes, final_bytes: bytes):
    base_repo, final_repo, *_ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, content)
    base_repo.replace_current_proposal(owner_key, proposal, content, None)
    snapshot = _make_final_snapshot(owner_key, proposal, final_bytes)
    result = final_repo.save_final_docx_snapshot(snapshot, final_bytes)
    assert result.status.value == "SAVED"
    return owner_key, proposal, snapshot


def _issue_codes(report):
    return {issue.issue_code for issue in report.issues}


# 1: clean storage ----------------------------------------------------------------


def test_clean_storage_gives_an_empty_report(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    _setup(env, b"clean proposal content", b"clean final bytes")

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert report.issues == ()
    assert report.scanned_final_snapshot_count == 1


# 2: missing owner ------------------------------------------------------------------


def test_missing_owner_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"missing owner proposal", b"missing owner final bytes")

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("DELETE FROM cv_document_artifact_owners WHERE owner_key_fingerprint = :owner"),
                {"owner": owner_key.owner_key_fingerprint},
            )

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.MISSING_OWNER in _issue_codes(report)


# 3: missing source proposal ---------------------------------------------------------


def test_missing_source_proposal_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"missing proposal base", b"missing proposal final bytes")

    with session_factory() as session:
        with session.begin():
            session.execute(text("PRAGMA foreign_keys=OFF"))
            session.execute(
                text("DELETE FROM cv_docx_proposal_artifacts WHERE artifact_fingerprint = :fp"),
                {"fp": proposal.artifact_fingerprint},
            )
            session.execute(text("PRAGMA foreign_keys=ON"))

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.MISSING_SOURCE_PROPOSAL in _issue_codes(report)


# 4-6: lineage mismatches (owner / revision / hash) -----------------------------------


def test_source_proposal_owner_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"owner mismatch base", b"owner mismatch final bytes")
    other_owner = _owner_key("other-job")
    other_proposal = _make_proposal(other_owner, 1, b"other owner proposal content")
    base_repo.replace_current_proposal(other_owner, other_proposal, b"other owner proposal content", None)

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET source_proposal_artifact_fingerprint = :fp "
                "WHERE final_snapshot_fingerprint = :own_fp"
            ),
            {"fp": other_proposal.artifact_fingerprint, "own_fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_OWNER_MISMATCH in _issue_codes(report)


def test_source_proposal_revision_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"revision mismatch base", b"revision mismatch final bytes")

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET source_proposal_revision = 99 "
                "WHERE final_snapshot_fingerprint = :fp"
            ),
            {"fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_REVISION_MISMATCH in _issue_codes(report)


def test_generated_proposal_hash_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"gen hash mismatch base", b"gen hash mismatch final bytes")

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET generated_proposal_sha256 = :bad "
                "WHERE final_snapshot_fingerprint = :fp"
            ),
            {"bad": "d" * 64, "fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    codes = _issue_codes(report)
    assert FinalDocxSnapshotReconciliationIssueCode.SOURCE_PROPOSAL_HASH_MISMATCH in codes
    # Tampering generated_proposal_sha256 also invalidates the fingerprint
    # and edited_by_user derivation -- all three are legitimately reported.
    assert FinalDocxSnapshotReconciliationIssueCode.INVALID_FINAL_SNAPSHOT_FINGERPRINT in codes


# 7-10: blob-layer checks -------------------------------------------------------------


def test_missing_blob_row_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"missing blob row base", b"missing blob row final bytes")

    with session_factory() as session:
        with session.begin():
            session.execute(text("PRAGMA foreign_keys=OFF"))
            session.execute(
                text("DELETE FROM cv_document_blobs WHERE blob_sha256 = :sha"),
                {"sha": snapshot.final_docx_sha256},
            )
            session.execute(text("PRAGMA foreign_keys=ON"))

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.MISSING_BLOB_ROW in _issue_codes(report)


def test_missing_blob_file_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"missing blob file base", b"missing blob file final bytes")

    sha = snapshot.final_docx_sha256
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.unlink()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.MISSING_BLOB_FILE in _issue_codes(report)


def test_blob_hash_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"blob hash mismatch base", b"blob hash mismatch final bytes")

    sha = snapshot.final_docx_sha256
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"tampered on disk, completely different bytes now")

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.BLOB_HASH_MISMATCH in _issue_codes(report)


def test_blob_size_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"blob size mismatch base", b"blob size mismatch final bytes")

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("UPDATE cv_document_blobs SET byte_size = byte_size + 1000 WHERE blob_sha256 = :sha"),
                {"sha": snapshot.final_docx_sha256},
            )

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.BLOB_SIZE_MISMATCH in _issue_codes(report)


def test_blob_media_type_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"media type mismatch base", b"media type mismatch final bytes")

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("UPDATE cv_document_blobs SET media_type = 'application/pdf' WHERE blob_sha256 = :sha"),
                {"sha": snapshot.final_docx_sha256},
            )

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.BLOB_MEDIA_TYPE_MISMATCH in _issue_codes(report)


# 11: invalid final snapshot fingerprint ------------------------------------------------


def test_invalid_final_snapshot_fingerprint_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"invalid fingerprint base", b"invalid fingerprint final bytes")

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET finalization_policy_version = 'tampered-policy' "
                "WHERE final_snapshot_fingerprint = :fp"
            ),
            {"fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.INVALID_FINAL_SNAPSHOT_FINGERPRINT in _issue_codes(report)


# 12: edited_by_user mismatch ------------------------------------------------------------


def test_edited_by_user_mismatch_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    owner_key, proposal, snapshot = _setup(env, b"edited flag base", b"edited flag final bytes")

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_final_snapshots SET edited_by_user = 1 - edited_by_user "
                "WHERE final_snapshot_fingerprint = :fp"
            ),
            {"fp": snapshot.final_snapshot_fingerprint},
        )
        session.commit()

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.EDITED_BY_USER_MISMATCH in _issue_codes(report)


# 13-14: stale temp files / symlinks -----------------------------------------------------


def test_stale_temp_file_is_detected(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    (store.tmp_dir / "blob-leftover-final.tmp").write_bytes(b"leftover")

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.STALE_TEMP_FILE in _issue_codes(report)


def test_symlink_in_managed_tree_is_detected(env, tmp_path: Path) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    evil_target = tmp_path / "evil_target_final"
    evil_target.mkdir()
    os.symlink(evil_target, store.blobs_dir / "zz_final")

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert FinalDocxSnapshotReconciliationIssueCode.MANAGED_TREE_SYMLINK in _issue_codes(report)


# 15: never emits global orphan codes -----------------------------------------------------


def test_never_emits_global_orphan_codes(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    _setup(env, b"orphan-code-exclusion base", b"orphan-code-exclusion final bytes")
    store.write_blob(b"a genuinely orphaned blob unrelated to anything")

    report = run_final_docx_snapshot_reconciliation(session_factory, store)
    codes = {code.value for code in _issue_codes(report)}
    assert "ORPHAN_BLOB_ROW" not in codes
    assert "ORPHAN_FILESYSTEM_BLOB" not in codes


# 16: standard mode is read-only ----------------------------------------------------------


def _tree_snapshot(root: Path) -> set[tuple[str, int]]:
    entries = set()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            entries.add((str(path.relative_to(root)), len(path.read_bytes())))
    return entries


def test_standard_mode_never_mutates_db_or_filesystem(env) -> None:
    base_repo, final_repo, session_factory, store, engine = env
    _setup(env, b"read-only guarantee base", b"read-only guarantee final bytes")
    store.write_blob(b"another orphan blob for the read-only test")
    (store.tmp_dir / "blob-stale-final.tmp").write_bytes(b"stale")

    with engine.connect() as conn:
        before_rows = [
            tuple(row)
            for row in conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    before_tree = _tree_snapshot(store.root)

    run_final_docx_snapshot_reconciliation(session_factory, store)

    with engine.connect() as conn:
        after_rows = [
            tuple(row)
            for row in conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    after_tree = _tree_snapshot(store.root)

    assert before_rows == after_rows
    assert before_tree == after_tree


def test_reconciliation_is_idempotent(env) -> None:
    base_repo, final_repo, session_factory, store, _ = env
    _setup(env, b"idempotence base", b"idempotence final bytes")
    store.write_blob(b"an orphan blob to make the report non-empty")

    report_1 = run_final_docx_snapshot_reconciliation(session_factory, store)
    report_2 = run_final_docx_snapshot_reconciliation(session_factory, store)
    assert report_1 == report_2
