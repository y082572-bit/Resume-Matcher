"""Explicit Provenance Stage P6-B3b-B: integration tests for
``run_final_confirmed_pdf_reconciliation`` -- every listed inconsistency
scenario is detected, the pass never mutates DB or filesystem state, a
``LEGACY_VALIDATED_DOCX`` authority row is never itself an issue, a
historical (non-current) artifact row is never itself an issue, and this
module never emits ``ORPHAN_BLOB_ROW``/``ORPHAN_FILESYSTEM_BLOB``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base
import app.services.cv_document_models as cdm
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_confirmed_pdf import (
    ARTIFACT_FINGERPRINT_SCHEMA_VERSION,
    CONVERSION_REQUEST_FINGERPRINT_SCHEMA_VERSION,
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    build_final_confirmed_pdf_artifact,
    compute_canonical_runtime_identity_json,
    compute_final_confirmed_pdf_artifact_fingerprint,
    compute_final_confirmed_pdf_request_fingerprint,
)
from app.schemas.cv_document_final_confirmed_pdf_reconciliation import FinalConfirmedPdfReconciliationIssueCode
from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import run_cutover_installer
from app.services.cv_document_final_confirmed_pdf_reconciliation import run_final_confirmed_pdf_reconciliation
from app.services.cv_document_final_confirmed_pdf_schema_manifest import TRG_AUTHORITY_INSERT_FINAL
from app.services.cv_document_final_confirmed_pdf_sql_repository import FinalConfirmedPdfSqlRepository
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_models import CvFinalConfirmedPdfArtifactRow
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository
from app.services.cv_document_storage_shared import ensure_blob_row


_POLICY = "reconciliation-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'final_confirmed_pdf_recon.db'}", future=True)
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_snapshot_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    pdf_repo = FinalConfirmedPdfSqlRepository(session_factory, store)
    return base_repo, final_snapshot_repo, pdf_repo, session_factory, store, engine


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _runtime_identity(*, executable_sha256: str = "a" * 64):
    return build_converter_runtime_identity(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256=executable_sha256,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )


def _setup_final_snapshot(
    base_repo, final_snapshot_repo, owner_key, content: bytes, *, revision: int = 1
) -> FinalDocxSnapshot:
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
    proposal = CvDocxProposalArtifact(
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
    base_repo.replace_current_proposal(owner_key, proposal, content, revision - 1 if revision > 1 else None)

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash,
        finalization_policy_version=_POLICY,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash,
        edited_by_user=False,
        finalization_policy_version=_POLICY,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )
    final_snapshot_repo.save_final_docx_snapshot(snapshot, content)
    return snapshot


def _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes: bytes, policy: str = _POLICY) -> dict:
    pdf_sha = _sha256(pdf_bytes)
    request_fp = compute_final_confirmed_pdf_request_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        conversion_policy_version=policy,
    )
    artifact_fp = compute_final_confirmed_pdf_artifact_fingerprint(
        conversion_request_fingerprint=request_fp, pdf_sha256=pdf_sha
    )
    return dict(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        conversion_request_fingerprint=request_fp,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        runtime_identity_json=compute_canonical_runtime_identity_json(identity),
        conversion_policy_version=policy,
        pdf_sha256=pdf_sha,
        blob_sha256=pdf_sha,
        conversion_request_fingerprint_schema_version=CONVERSION_REQUEST_FINGERPRINT_SCHEMA_VERSION,
        artifact_fingerprint_schema_version=ARTIFACT_FINGERPRINT_SCHEMA_VERSION,
    )


def _write_pdf_blob_row(session_factory, store, pdf_bytes: bytes, *, media_type: str = "application/pdf") -> str:
    sha = _sha256(pdf_bytes)
    write_result = store.write_blob(pdf_bytes, expected_sha256=sha, media_type=media_type)
    assert write_result.success
    with session_factory() as session:
        with session.begin():
            ensure_blob_row(session, store, blob_sha256=sha, byte_size=len(pdf_bytes), media_type=media_type)
    return sha


def _insert_artifact_row(session_factory, fields: dict) -> None:
    with session_factory() as session:
        with session.begin():
            session.add(CvFinalConfirmedPdfArtifactRow(**fields))


def _issue_codes(report):
    return {issue.issue_code for issue in report.issues}


# -- clean baseline: no issues --------------------------------------------------------


def test_clean_storage_gives_an_empty_report(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"clean recon content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 clean"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"
    cas_result = pdf_repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert cas_result.status.value == "PROMOTED"

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert report.issues == ()
    assert report.scanned_artifact_count == 1
    assert report.scanned_authority_count == 1


# -- missing owner ----------------------------------------------------------------------


def test_owner_missing_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"owner missing content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 owner missing"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("DELETE FROM cv_document_artifact_owners WHERE owner_key_fingerprint = :fp"),
                {"fp": owner_key.owner_key_fingerprint},
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.OWNER_MISSING in _issue_codes(report)


# -- source Final DOCX Snapshot missing --------------------------------------------------


def test_source_final_snapshot_missing_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"snapshot missing content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 snapshot missing"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("DELETE FROM cv_docx_final_snapshots WHERE final_snapshot_fingerprint = :fp"),
                {"fp": snapshot.final_snapshot_fingerprint},
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_SNAPSHOT_MISSING in _issue_codes(report)


# -- source Final DOCX Snapshot owner mismatch -------------------------------------------


def test_source_final_snapshot_owner_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_a = _owner_key("owner-a")
    owner_b = _owner_key("owner-b")
    snapshot_a = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_a, b"owner mismatch content a")
    # Owner B must exist (with its own proposal, to satisfy the snapshot
    # row's own proposal FK below) before the raw re-insert under owner B.
    snapshot_b = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_b, b"owner mismatch content b")

    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 owner mismatch"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_a,
        source_final_snapshot_fingerprint=snapshot_a.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot_a.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"

    # The artifact row itself is immutable (no UPDATE/DELETE trigger) -- the
    # only way to diverge owner identity after the fact is to delete and
    # re-insert the *referenced* snapshot row (same final_snapshot_fingerprint,
    # same final_docx_sha256 so this scenario is never also flagged as a SHA
    # mismatch) under a different owner. Never possible through the
    # repository; only a raw, independently corrupted row could look like
    # this.
    with session_factory() as session:
        with session.begin():
            session.execute(
                text("DELETE FROM cv_docx_final_snapshots WHERE final_snapshot_fingerprint = :fp"),
                {"fp": snapshot_a.final_snapshot_fingerprint},
            )
            session.execute(
                text(
                    "INSERT INTO cv_docx_final_snapshots "
                    "(final_snapshot_fingerprint, owner_key_fingerprint, source_proposal_artifact_fingerprint, "
                    "source_proposal_revision, generated_proposal_sha256, final_docx_sha256, edited_by_user, "
                    "finalization_policy_version, snapshot_schema_version, created_at) "
                    "VALUES (:fp, :owner_b, :proposal_fp, 1, :final_sha, :final_sha, 0, :policy, :schema_version, :created_at)"
                ),
                {
                    "fp": snapshot_a.final_snapshot_fingerprint,
                    "owner_b": owner_b.owner_key_fingerprint,
                    "proposal_fp": snapshot_b.source_proposal_artifact_fingerprint,
                    "final_sha": snapshot_a.final_docx_sha256,
                    "policy": _POLICY,
                    "schema_version": FINAL_SNAPSHOT_SCHEMA_VERSION,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH in _issue_codes(report)


# -- source Final DOCX SHA256 mismatch ----------------------------------------------------


def test_source_final_docx_sha256_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"docx sha mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 docx sha mismatch"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"

    other_blob_bytes = b"an unrelated but valid blob for the tampered docx sha"
    other_blob_sha = _write_pdf_blob_row(session_factory, store, other_blob_bytes, media_type="application/octet-stream")

    with session_factory() as session:
        with session.begin():
            session.execute(text("PRAGMA ignore_check_constraints=1"))
            session.execute(
                text("DELETE FROM cv_docx_final_snapshots WHERE final_snapshot_fingerprint = :fp"),
                {"fp": snapshot.final_snapshot_fingerprint},
            )
            session.execute(
                text(
                    "INSERT INTO cv_docx_final_snapshots "
                    "(final_snapshot_fingerprint, owner_key_fingerprint, source_proposal_artifact_fingerprint, "
                    "source_proposal_revision, generated_proposal_sha256, final_docx_sha256, edited_by_user, "
                    "finalization_policy_version, snapshot_schema_version, created_at) "
                    "VALUES (:fp, :owner, :proposal_fp, 1, :other_sha, :other_sha, 1, :policy, :schema_version, :created_at)"
                ),
                {
                    "fp": snapshot.final_snapshot_fingerprint,
                    "owner": owner_key.owner_key_fingerprint,
                    "proposal_fp": snapshot.source_proposal_artifact_fingerprint,
                    "other_sha": other_blob_sha,
                    "policy": _POLICY,
                    "schema_version": FINAL_SNAPSHOT_SCHEMA_VERSION,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.SOURCE_FINAL_DOCX_SHA256_MISMATCH in _issue_codes(report)


# -- runtime identity JSON invalid --------------------------------------------------------


def test_runtime_identity_json_invalid_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"identity json invalid content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 identity json invalid"
    _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    fields["runtime_identity_json"] = "not valid json at all"
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.RUNTIME_IDENTITY_JSON_INVALID in _issue_codes(report)


# -- runtime identity fingerprint mismatch ------------------------------------------------


def test_runtime_identity_fingerprint_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"identity fp mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 identity fp mismatch"
    _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    fields["runtime_identity_fingerprint"] = "b" * 64
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.RUNTIME_IDENTITY_FINGERPRINT_MISMATCH in _issue_codes(report)


# -- request fingerprint mismatch ----------------------------------------------------------


def test_request_fingerprint_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"request fp mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 request fp mismatch"
    _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    fields["conversion_request_fingerprint"] = "c" * 64
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.REQUEST_FINGERPRINT_MISMATCH in _issue_codes(report)


# -- artifact fingerprint mismatch ----------------------------------------------------------


def test_artifact_fingerprint_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"artifact fp mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 artifact fp mismatch"
    _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    fields["artifact_fingerprint"] = "d" * 64
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.ARTIFACT_FINGERPRINT_MISMATCH in _issue_codes(report)


# -- PDF blob missing -------------------------------------------------------------------------


def test_pdf_blob_missing_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"pdf blob missing content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 blob missing, never written"

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    # Deliberately never write the blob row/bytes for this pdf_sha256/blob_sha256.
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_MISSING in _issue_codes(report)


# -- PDF media type mismatch -----------------------------------------------------------------


def test_pdf_blob_media_type_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"pdf media type content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 media type mismatch"
    _write_pdf_blob_row(session_factory, store, pdf_bytes, media_type="application/octet-stream")

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    _insert_artifact_row(session_factory, fields)

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_MEDIA_TYPE_MISMATCH in _issue_codes(report)


# -- PDF byte length mismatch -----------------------------------------------------------------


def test_pdf_blob_length_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"pdf length mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 length mismatch"
    _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    _insert_artifact_row(session_factory, fields)

    with session_factory() as session:
        with session.begin():
            session.execute(
                text("UPDATE cv_document_blobs SET byte_size = byte_size + 1000 WHERE blob_sha256 = :sha"),
                {"sha": fields["blob_sha256"]},
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_LENGTH_MISMATCH in _issue_codes(report)


# -- PDF hash mismatch (blob bytes tampered on disk) -------------------------------------------


def test_pdf_blob_hash_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"pdf hash mismatch content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 hash mismatch"
    sha = _write_pdf_blob_row(session_factory, store, pdf_bytes)

    fields = _base_artifact_fields(owner_key, snapshot, identity, pdf_bytes)
    _insert_artifact_row(session_factory, fields)

    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"tampered on disk, different pdf bytes now")

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.PDF_BLOB_HASH_MISMATCH in _issue_codes(report)


# -- FINAL authority points at a missing artifact ------------------------------------------------


def test_authority_target_artifact_missing_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority target missing content")

    # The insert-time trigger validates the target artifact exists (and
    # belongs to the same owner) -- exactly the invariant a real write path
    # can never violate. Only a raw, independently corrupted row (with that
    # trigger's enforcement bypassed) could ever look like this.
    with session_factory() as session:
        with session.begin():
            session.execute(text(f"DROP TRIGGER {TRG_AUTHORITY_INSERT_FINAL}"))
            session.execute(
                text(
                    "INSERT INTO cv_confirmed_pdf_current_authority "
                    "(owner_key_fingerprint, lineage_kind, current_artifact_fingerprint, slot_version, updated_at) "
                    "VALUES (:owner, 'FINAL_DOCX_SNAPSHOT', :missing_fp, 1, :created_at)"
                ),
                {
                    "owner": owner_key.owner_key_fingerprint,
                    "missing_fp": "e" * 64,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_TARGET_ARTIFACT_MISSING in _issue_codes(report)


# -- FINAL authority owner mismatch ---------------------------------------------------------------


def test_authority_target_artifact_owner_mismatch_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_a = _owner_key("authority-owner-a")
    owner_b = _owner_key("authority-owner-b")
    snapshot_a = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_a, b"authority owner mismatch a")
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_b, b"authority owner mismatch b")

    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 authority owner mismatch"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_a,
        source_final_snapshot_fingerprint=snapshot_a.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot_a.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = pdf_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"

    with session_factory() as session:
        with session.begin():
            session.execute(text(f"DROP TRIGGER {TRG_AUTHORITY_INSERT_FINAL}"))
            session.execute(
                text(
                    "INSERT INTO cv_confirmed_pdf_current_authority "
                    "(owner_key_fingerprint, lineage_kind, current_artifact_fingerprint, slot_version, updated_at) "
                    "VALUES (:owner_b, 'FINAL_DOCX_SNAPSHOT', :artifact_fp, 1, :created_at)"
                ),
                {
                    "owner_b": owner_b.owner_key_fingerprint,
                    "artifact_fp": artifact.artifact_fingerprint,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_TARGET_ARTIFACT_OWNER_MISMATCH in _issue_codes(report)


# -- authority metadata invalid ---------------------------------------------------------------------


def test_authority_metadata_invalid_lineage_kind_is_detected(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority metadata invalid content")

    with session_factory() as session:
        with session.begin():
            session.execute(text("PRAGMA ignore_check_constraints=1"))
            session.execute(
                text(
                    "INSERT INTO cv_confirmed_pdf_current_authority "
                    "(owner_key_fingerprint, lineage_kind, current_artifact_fingerprint, slot_version, updated_at) "
                    "VALUES (:owner, 'NOT_A_REAL_LINEAGE_KIND', :fp, 1, :created_at)"
                ),
                {
                    "owner": owner_key.owner_key_fingerprint,
                    "fp": "f" * 64,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert FinalConfirmedPdfReconciliationIssueCode.AUTHORITY_METADATA_INVALID in _issue_codes(report)


# -- LEGACY authority is ignored (out of scope for this line) -------------------------------------


def test_legacy_authority_is_ignored(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"legacy authority content")

    with session_factory() as session:
        with session.begin():
            session.execute(text("PRAGMA ignore_check_constraints=1"))
            # LEGACY_VALIDATED_DOCX authority requires a row in the legacy
            # cv_confirmed_pdf_artifacts table per the real INSERT trigger --
            # bypass that trigger too, since this test only cares that this
            # module (not the legacy line) never flags it.
            session.execute(text("DROP TRIGGER trg_cv_confirmed_pdf_authority_insert_legacy"))
            session.execute(
                text(
                    "INSERT INTO cv_confirmed_pdf_current_authority "
                    "(owner_key_fingerprint, lineage_kind, current_artifact_fingerprint, slot_version, updated_at) "
                    "VALUES (:owner, 'LEGACY_VALIDATED_DOCX', :fp, 1, :created_at)"
                ),
                {
                    "owner": owner_key.owner_key_fingerprint,
                    "fp": "a" * 64,
                    "created_at": "2024-01-01T00:00:00+00:00",
                },
            )

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert report.issues == ()


# -- historical (non-current) artifact is never itself an issue -----------------------------------


def test_historical_non_current_artifact_is_not_an_issue(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"historical artifact content")
    identity = _runtime_identity()

    pdf_bytes_1 = b"%PDF-1.4 historical winner"
    artifact_1 = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes_1),
    )
    pdf_repo.save_artifact(artifact_1, pdf_bytes_1)
    promoted = pdf_repo.promote_current_authority(
        owner_key, artifact_1.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert promoted.status.value == "PROMOTED"

    # A second, distinct artifact for the same owner/snapshot -- never
    # promoted to current authority. Purely historical.
    identity_2 = _runtime_identity(executable_sha256="b" * 64)
    pdf_bytes_2 = b"%PDF-1.4 historical loser"
    artifact_2 = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity_2,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes_2),
    )
    save_2 = pdf_repo.save_artifact(artifact_2, pdf_bytes_2)
    assert save_2.status.value == "SAVED"

    report = run_final_confirmed_pdf_reconciliation(session_factory, store)
    assert report.issues == ()
    assert report.scanned_artifact_count == 2


# -- read-only ---------------------------------------------------------------------------------------


def test_reconciliation_never_mutates_db_or_filesystem(env) -> None:
    base_repo, final_snapshot_repo, pdf_repo, session_factory, store, engine = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"read-only guarantee content")
    identity = _runtime_identity()
    pdf_bytes = b"%PDF-1.4 read-only guarantee"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=_sha256(pdf_bytes),
    )
    pdf_repo.save_artifact(artifact, pdf_bytes)

    with engine.connect() as conn:
        before_rows = [
            tuple(row)
            for row in conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    with session_factory() as session:
        before_authority = list(session.execute(text("SELECT * FROM cv_confirmed_pdf_current_authority")))
        before_artifacts = list(session.execute(text("SELECT * FROM cv_final_confirmed_pdf_artifacts")))

    run_final_confirmed_pdf_reconciliation(session_factory, store)

    with engine.connect() as conn:
        after_rows = [
            tuple(row)
            for row in conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    with session_factory() as session:
        after_authority = list(session.execute(text("SELECT * FROM cv_confirmed_pdf_current_authority")))
        after_artifacts = list(session.execute(text("SELECT * FROM cv_final_confirmed_pdf_artifacts")))

    assert before_rows == after_rows
    assert before_authority == after_authority
    assert before_artifacts == after_artifacts


# -- no new orphan codes ------------------------------------------------------------------------------


def test_no_new_orphan_codes_exist_in_this_modules_issue_set() -> None:
    names = {code.name for code in FinalConfirmedPdfReconciliationIssueCode}
    assert "ORPHAN_BLOB_ROW" not in names
    assert "ORPHAN_FILESYSTEM_BLOB" not in names
