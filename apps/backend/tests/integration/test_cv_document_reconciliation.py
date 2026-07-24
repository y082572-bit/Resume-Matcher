"""Integration tests for the P6-B1 read-only reconciliation pass
(``cv_document_reconciliation.py``): every listed inconsistency scenario is
detected, standard mode never mutates DB or filesystem state, a current
confirmed PDF is never removed, and reconciliation is idempotent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import (
    ArtifactOwnerKind,
    DocumentProvenanceMode,
    DocxProposalProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
)
from app.schemas.cv_document_storage import CvDocumentReconciliationIssueCode
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_models import CvDocxProposalArtifactRow
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_pdf_confirmation_builder import compute_pdf_artifact_fingerprint
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_reconciliation import cleanup_stale_temp_files, run_reconciliation
from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recon.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    repo = CvDocumentArtifactSqlRepository(session_factory, store)
    return repo, session_factory, store, engine


def _owner_key(ref: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=ref)


def _make_proposal(owner_key, revision: int, content: bytes):
    from app.schemas.cv_document_artifact import CvDocxProposalArtifact

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


def _make_snapshot(owner_key, proposal, content: bytes):
    from app.schemas.cv_document_artifact import ValidatedCvDocxSnapshot

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


def _make_pdf(owner_key, snapshot, revision: int, pdf_bytes: bytes):
    from app.schemas.cv_document_artifact import ConfirmedCvPdfArtifact

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


def _issue_codes(report):
    return {issue.issue_code for issue in report.issues}


def _tree_snapshot(root: Path) -> set[tuple[str, int]]:
    entries = set()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            entries.add((str(path.relative_to(root)), len(path.read_bytes())))
    return entries


# 1: clean storage ----------------------------------------------------------------


def test_clean_storage_gives_an_empty_report(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"clean content")
    repo.replace_current_proposal(owner_key, proposal, b"clean content", None)
    snapshot = _make_snapshot(owner_key, proposal, b"clean content")
    repo.save_validated_snapshot(snapshot, b"clean content")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-clean")
    repo.replace_current_pdf(owner_key, pdf, b"%PDF-clean", None)

    report = run_reconciliation(session_factory, store)
    assert report.issues == ()
    assert report.scanned_proposal_count == 1
    assert report.scanned_snapshot_count == 1
    assert report.scanned_pdf_count == 1


# 2-3: dangling slots ---------------------------------------------------------------


def test_dangling_proposal_slot_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"dangling proposal")
    repo.replace_current_proposal(owner_key, proposal, b"dangling proposal", None)

    with session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    "UPDATE cv_document_proposal_slots SET current_artifact_fingerprint = :fp "
                    "WHERE owner_key_fingerprint = :owner"
                ),
                {"fp": "f" * 64, "owner": owner_key.owner_key_fingerprint},
            )

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.PROPOSAL_SLOT_DANGLING_ARTIFACT in _issue_codes(report)


def test_dangling_pdf_slot_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"pdf dangling base")
    repo.replace_current_proposal(owner_key, proposal, b"pdf dangling base", None)
    snapshot = _make_snapshot(owner_key, proposal, b"pdf dangling base")
    repo.save_validated_snapshot(snapshot, b"pdf dangling base")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-dangling")
    repo.replace_current_pdf(owner_key, pdf, b"%PDF-dangling", None)

    with session_factory() as session:
        with session.begin():
            session.execute(
                text(
                    "UPDATE cv_document_pdf_slots SET current_artifact_fingerprint = :fp "
                    "WHERE owner_key_fingerprint = :owner"
                ),
                {"fp": "e" * 64, "owner": owner_key.owner_key_fingerprint},
            )

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.PDF_SLOT_DANGLING_ARTIFACT in _issue_codes(report)


# 4: artifact referencing a missing blob row ----------------------------------------


def test_proposal_artifact_missing_blob_row_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"missing blob row content")
    repo.replace_current_proposal(owner_key, proposal, b"missing blob row content", None)

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("DELETE FROM cv_document_blobs WHERE blob_sha256 = :sha"),
                {"sha": proposal.generated_docx_content_hash},
            )

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.PROPOSAL_ARTIFACT_MISSING_BLOB_ROW in _issue_codes(report)


# 5-6: blob metadata vs filesystem ---------------------------------------------------


def test_blob_row_without_file_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"blob row without file")
    repo.replace_current_proposal(owner_key, proposal, b"blob row without file", None)

    sha = proposal.generated_docx_content_hash
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.unlink()

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.BLOB_METADATA_FILE_MISSING in _issue_codes(report)


def test_filesystem_blob_without_metadata_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    data = b"orphan filesystem blob content"
    store.write_blob(data)

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.ORPHAN_FILESYSTEM_BLOB in _issue_codes(report)


# 7-8: wrong hash / wrong size --------------------------------------------------------


def test_blob_file_wrong_hash_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"wrong hash content")
    repo.replace_current_proposal(owner_key, proposal, b"wrong hash content", None)

    sha = proposal.generated_docx_content_hash
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"tampered on disk, different bytes now")

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.BLOB_FILE_HASH_MISMATCH in _issue_codes(report)


def test_blob_file_wrong_size_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"wrong size content")
    repo.replace_current_proposal(owner_key, proposal, b"wrong size content", None)

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("UPDATE cv_document_blobs SET byte_size = byte_size + 1000 WHERE blob_sha256 = :sha"),
                {"sha": proposal.generated_docx_content_hash},
            )

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.BLOB_FILE_SIZE_MISMATCH in _issue_codes(report)


# 9-11: declared hash column vs blob_sha256 column mismatches -----------------------
# These columns are protected by a same-row CHECK constraint (see
# cv_document_models.py) -- the only way to construct the row at all is to
# temporarily disable CHECK enforcement, exactly the way a real, independently
# corrupted row would already have bypassed application-level writes.


def test_snapshot_blob_hash_mismatch_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"snapshot mismatch base")
    repo.replace_current_proposal(owner_key, proposal, b"snapshot mismatch base", None)
    snapshot = _make_snapshot(owner_key, proposal, b"snapshot mismatch base")
    repo.save_validated_snapshot(snapshot, b"snapshot mismatch base")

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text("UPDATE cv_docx_validated_snapshots SET exact_docx_sha256 = :bad WHERE snapshot_fingerprint = :fp"),
            {"bad": "d" * 64, "fp": snapshot.snapshot_fingerprint},
        )
        session.commit()

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.SNAPSHOT_BLOB_HASH_MISMATCH in _issue_codes(report)


def test_proposal_blob_hash_mismatch_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"proposal mismatch base")
    repo.replace_current_proposal(owner_key, proposal, b"proposal mismatch base", None)

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE cv_docx_proposal_artifacts SET generated_docx_content_hash = :bad "
                "WHERE artifact_fingerprint = :fp"
            ),
            {"bad": "d" * 64, "fp": proposal.artifact_fingerprint},
        )
        session.commit()

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.PROPOSAL_BLOB_HASH_MISMATCH in _issue_codes(report)


def test_pdf_blob_hash_mismatch_is_detected(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"pdf mismatch base")
    repo.replace_current_proposal(owner_key, proposal, b"pdf mismatch base", None)
    snapshot = _make_snapshot(owner_key, proposal, b"pdf mismatch base")
    repo.save_validated_snapshot(snapshot, b"pdf mismatch base")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-mismatch-base")
    repo.replace_current_pdf(owner_key, pdf, b"%PDF-mismatch-base", None)

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text("UPDATE cv_confirmed_pdf_artifacts SET pdf_sha256 = :bad WHERE artifact_fingerprint = :fp"),
            {"bad": "d" * 64, "fp": pdf.artifact_fingerprint},
        )
        session.commit()

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.PDF_BLOB_HASH_MISMATCH in _issue_codes(report)


# 12-13: stale temp files / symlinks -------------------------------------------------


def test_stale_temp_file_is_detected(env) -> None:
    _, session_factory, store, _ = env
    (store.tmp_dir / "blob-leftover123.tmp").write_bytes(b"leftover")

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.STALE_TEMP_FILE in _issue_codes(report)


def test_symlink_in_managed_tree_is_detected(env, tmp_path: Path) -> None:
    _, session_factory, store, _ = env
    evil_target = tmp_path / "evil_target"
    evil_target.mkdir()
    os.symlink(evil_target, store.blobs_dir / "zz")

    report = run_reconciliation(session_factory, store)
    assert CvDocumentReconciliationIssueCode.MANAGED_TREE_SYMLINK in _issue_codes(report)


# 14: duplicate revision blocked by DB constraint ------------------------------------


def test_duplicate_owner_revision_is_blocked_by_db_constraint(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"duplicate revision base")
    repo.replace_current_proposal(owner_key, proposal, b"duplicate revision base", None)

    duplicate = _make_proposal(owner_key, 1, b"duplicate revision base second attempt")
    with session_factory() as session:
        store.write_blob(b"duplicate revision base second attempt")
        with pytest.raises(IntegrityError):
            with session.begin():
                session.add(
                    CvDocxProposalArtifactRow(
                        artifact_fingerprint=duplicate.artifact_fingerprint,
                        owner_key_fingerprint=owner_key.owner_key_fingerprint,
                        artifact_id=str(duplicate.artifact_id),
                        approved_cv_content_fingerprint=duplicate.approved_cv_content_fingerprint,
                        content_plan_fingerprint=duplicate.content_plan_fingerprint,
                        role_strategy_context_fingerprint=None,
                        document_schema_version=duplicate.document_schema_version,
                        rendering_policy_version=duplicate.rendering_policy_version,
                        renderer_adapter_id=duplicate.renderer_adapter_id,
                        template_fingerprint=duplicate.template_fingerprint,
                        generation_input_fingerprint=duplicate.generation_input_fingerprint,
                        generated_docx_content_hash=duplicate.generated_docx_content_hash,
                        current_file_hash=duplicate.current_file_hash,
                        validated_file_hash=None,
                        proposal_revision=1,  # duplicate revision for the same owner
                        provenance_mode=duplicate.provenance_mode.value,
                        artifact_storage_status="ACTIVE",
                        blob_sha256=duplicate.generated_docx_content_hash,
                    )
                )


# 15: current confirmed PDF is never removed -----------------------------------------


def test_reconciliation_never_removes_the_current_confirmed_pdf(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"keep pdf base")
    repo.replace_current_proposal(owner_key, proposal, b"keep pdf base", None)
    snapshot = _make_snapshot(owner_key, proposal, b"keep pdf base")
    repo.save_validated_snapshot(snapshot, b"keep pdf base")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-keep-me")
    repo.replace_current_pdf(owner_key, pdf, b"%PDF-keep-me", None)

    # Introduce an unrelated issue elsewhere (orphan filesystem blob).
    store.write_blob(b"totally unrelated orphan blob")

    report = run_reconciliation(session_factory, store)
    assert len(report.issues) > 0  # sanity: reconciliation did find something

    current_pdf = repo.get_current_pdf(owner_key)
    assert current_pdf is not None
    assert current_pdf.artifact_fingerprint == pdf.artifact_fingerprint
    assert repo.read_current_pdf_bytes(owner_key) == b"%PDF-keep-me"


# 16-17: idempotence and standard-mode read-only guarantee ---------------------------


def test_reconciliation_is_idempotent(env) -> None:
    repo, session_factory, store, _ = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"idempotence base")
    repo.replace_current_proposal(owner_key, proposal, b"idempotence base", None)
    store.write_blob(b"an orphan blob to make the report non-empty")

    report_1 = run_reconciliation(session_factory, store)
    report_2 = run_reconciliation(session_factory, store)
    assert report_1 == report_2


def test_standard_mode_never_mutates_db_or_filesystem(env) -> None:
    repo, session_factory, store, engine = env
    owner_key = _owner_key()
    proposal = _make_proposal(owner_key, 1, b"read-only guarantee base")
    repo.replace_current_proposal(owner_key, proposal, b"read-only guarantee base", None)
    store.write_blob(b"another orphan blob")
    (store.tmp_dir / "blob-stale999.tmp").write_bytes(b"stale")

    with engine.connect() as conn:
        before_rows = [
            tuple(row)
            for row in conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    before_tree = _tree_snapshot(store.root)

    run_reconciliation(session_factory, store)

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


def test_cleanup_stale_temp_files_is_a_separate_explicit_operation(env) -> None:
    _, session_factory, store, _ = env
    (store.tmp_dir / "blob-explicit-cleanup.tmp").write_bytes(b"stale")

    report_before = run_reconciliation(session_factory, store)
    assert any(
        issue.issue_code == CvDocumentReconciliationIssueCode.STALE_TEMP_FILE for issue in report_before.issues
    )
    assert (store.tmp_dir / "blob-explicit-cleanup.tmp").exists()  # standard mode did not remove it

    removed = cleanup_stale_temp_files(store)
    assert "blob-explicit-cleanup.tmp" in removed
    assert not (store.tmp_dir / "blob-explicit-cleanup.tmp").exists()

    report_after = run_reconciliation(session_factory, store)
    assert not any(
        issue.issue_code == CvDocumentReconciliationIssueCode.STALE_TEMP_FILE for issue in report_after.issues
    )
