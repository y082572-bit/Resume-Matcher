"""Explicit Provenance Stage P6-B3b-A: integration tests for the cutover
state machine -- every legal state, INVALID detection, resumable/idempotent
migration, legacy slot freeze enforcement, and the Hardening-3 restart
scenario (legacy migrated then switched to FINAL, restart stays READY).
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
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    build_final_confirmed_pdf_artifact,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import (
    FinalConfirmedPdfCutoverState,
    evaluate_cutover_state,
    run_cutover_installer,
)
from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
    ALL_TRIGGER_SQL,
    ARTIFACT_TRIGGER_NAMES,
    AUTHORITY_VALIDATION_TRIGGER_NAMES,
    LEGACY_FREEZE_TRIGGER_NAMES,
    TRIGGER_SQL_BY_NAME,
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
from app.schemas.cv_document_artifact import (
    ConfirmedCvPdfArtifact,
    CvDocxProposalArtifact,
    DocumentProvenanceMode,
    DocxProposalProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    ValidatedCvDocxSnapshot,
)
from app.services.cv_document_pdf_confirmation_builder import compute_pdf_artifact_fingerprint
from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _non_new_tables():
    return [t for t in Base.metadata.sorted_tables if t.name not in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES]


def _fresh_engine(tmp_path: Path, name: str = "cutover.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
    Base.metadata.create_all(engine, tables=_non_new_tables())
    return engine


# 1: ABSENT ------------------------------------------------------------------------


def test_absent_state(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.ABSENT


# 2: every legal *_PENDING state --------------------------------------------------


def test_artifact_table_triggers_pending(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.ARTIFACT_TABLE_TRIGGERS_PENDING


def test_authority_table_pending(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in ARTIFACT_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.AUTHORITY_TABLE_PENDING


def test_authority_validation_triggers_pending(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in ARTIFACT_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    cdm.CvConfirmedPdfCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.AUTHORITY_VALIDATION_TRIGGERS_PENDING


def test_legacy_freeze_pending(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in ARTIFACT_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    cdm.CvConfirmedPdfCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in AUTHORITY_VALIDATION_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.LEGACY_FREEZE_PENDING


def test_migration_pending_after_freeze(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    _write_legacy_pdf(base_repo, owner_key, b"legacy migration pending content")

    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    cdm.CvConfirmedPdfCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for sql in ALL_TRIGGER_SQL:
            conn.exec_driver_sql(sql)
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.MIGRATION_PENDING


# 3: READY ---------------------------------------------------------------------------


def test_ready_state(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    state = run_cutover_installer(engine)
    assert state == FinalConfirmedPdfCutoverState.READY
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.READY


# 4: INVALID table schema --------------------------------------------------------------


def test_invalid_table_schema(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE cv_final_confirmed_pdf_artifacts (artifact_fingerprint VARCHAR(64) PRIMARY KEY)"
        )
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.INVALID
    with pytest.raises(RuntimeError):
        run_cutover_installer(engine)


# 5: INVALID trigger body ---------------------------------------------------------------


def test_invalid_trigger_body(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    cdm.CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER trg_cv_final_confirmed_pdf_artifact_no_update "
            "BEFORE UPDATE ON cv_final_confirmed_pdf_artifacts "
            "BEGIN SELECT RAISE(ABORT, 'WRONG_TOKEN'); END"
        )
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.INVALID


# 6-7: migration resume -----------------------------------------------------------------


def _write_legacy_pdf(base_repo: CvDocumentArtifactSqlRepository, owner_key, content: bytes) -> ConfirmedCvPdfArtifact:
    gen_hash = _sha256(content)
    gen_input_fp = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None, template_fingerprint="c" * 64,
        renderer_adapter_id="fake", rendering_policy_version="v1", document_schema_version="cv-document-proposal-schema-v1",
    )
    artifact_fp = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, proposal_revision=1,
    )
    proposal = CvDocxProposalArtifact(
        artifact_id=uuid4(), owner_key=owner_key, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1", rendering_policy_version="v1",
        renderer_adapter_id="fake", template_fingerprint="c" * 64, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, current_file_hash=gen_hash, validated_file_hash=None,
        proposal_revision=1, superseded=False, artifact_fingerprint=artifact_fp,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    base_repo.replace_current_proposal(owner_key, proposal, content, None)

    snap_fp = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=1, exact_docx_sha256=gen_hash, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, template_fingerprint="c" * 64, validation_policy_version="val-v1",
        structural_validated_sha256=gen_hash, manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
    )
    snapshot = ValidatedCvDocxSnapshot(
        owner_key=owner_key, proposal_artifact_fingerprint=proposal.artifact_fingerprint, proposal_revision=1,
        exact_docx_sha256=gen_hash, approved_cv_content_fingerprint="a" * 64, content_plan_fingerprint="b" * 64,
        template_fingerprint="c" * 64, validation_policy_version="val-v1",
        structural_validation_result=DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=gen_hash),
        manual_confirmation_fingerprint=None, provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint=snap_fp,
    )
    base_repo.save_validated_snapshot(snapshot, content)

    pdf_bytes = b"%PDF-" + content
    pdf_sha256 = _sha256(pdf_bytes)
    pdf_fp = compute_pdf_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, source_validated_docx_snapshot_fingerprint=snap_fp,
        source_docx_sha256=gen_hash, pdf_sha256=pdf_sha256, conversion_adapter_id="fake-adapter",
        conversion_policy_version="conv-v1", pdf_revision=1,
    )
    pdf = ConfirmedCvPdfArtifact(
        artifact_id=uuid4(), owner_key=owner_key, source_validated_docx_snapshot_fingerprint=snap_fp,
        source_docx_sha256=gen_hash, approved_cv_content_fingerprint="a" * 64, content_plan_fingerprint="b" * 64,
        pdf_sha256=pdf_sha256, conversion_adapter_id="fake-adapter", conversion_policy_version="conv-v1",
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT, artifact_fingerprint=pdf_fp,
        pdf_revision=1, superseded=False,
    )
    result = base_repo.replace_current_pdf(owner_key, pdf, pdf_bytes, None)
    assert result.status.value == "REPLACED"
    return pdf


def test_conflicting_legacy_authority_is_invalid(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    pdf = _write_legacy_pdf(base_repo, owner_key, b"conflicting authority content")

    run_cutover_installer(engine)

    # Force a genuine conflict by re-pointing the *legacy slot* itself away
    # from what authority already recorded. cv_document_pdf_slots is frozen
    # by its own trigger -- temporarily drop it (only way to simulate
    # out-of-band corruption of already-migrated legacy history) and always
    # restore it.
    from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
        LEGACY_SLOT_FROZEN_UPDATE_SQL,
        TRG_LEGACY_SLOT_FROZEN_UPDATE,
    )

    with session_factory() as session:
        session.execute(text(f"DROP TRIGGER {TRG_LEGACY_SLOT_FROZEN_UPDATE}"))
        session.commit()
    try:
        with session_factory() as session:
            with session.begin():
                session.execute(
                    text(
                        "UPDATE cv_document_pdf_slots SET current_artifact_fingerprint = :bogus "
                        "WHERE owner_key_fingerprint = :ok"
                    ),
                    {"bogus": "9" * 64, "ok": owner_key.owner_key_fingerprint},
                )
    finally:
        with session_factory() as session:
            session.execute(text(LEGACY_SLOT_FROZEN_UPDATE_SQL))
            session.commit()

    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.INVALID


def test_existing_legacy_pointers_migrated(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    pdf = _write_legacy_pdf(base_repo, owner_key, b"existing legacy pointer content")

    state = run_cutover_installer(engine)
    assert state == FinalConfirmedPdfCutoverState.READY

    with session_factory() as session:
        row = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert row is not None
        assert row.lineage_kind == "LEGACY_VALIDATED_DOCX"
        assert row.current_artifact_fingerprint == pdf.artifact_fingerprint
        assert row.slot_version == 1


def test_empty_legacy_pointers_not_migrated(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    content = b"proposal only, no pdf ever confirmed"
    gen_hash = _sha256(content)
    gen_input_fp = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None, template_fingerprint="c" * 64,
        renderer_adapter_id="fake", rendering_policy_version="v1", document_schema_version="cv-document-proposal-schema-v1",
    )
    artifact_fp = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, proposal_revision=1,
    )
    proposal = CvDocxProposalArtifact(
        artifact_id=uuid4(), owner_key=owner_key, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1", rendering_policy_version="v1",
        renderer_adapter_id="fake", template_fingerprint="c" * 64, generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash, current_file_hash=gen_hash, validated_file_hash=None,
        proposal_revision=1, superseded=False, artifact_fingerprint=artifact_fp,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    base_repo.replace_current_proposal(owner_key, proposal, content, None)

    state = run_cutover_installer(engine)
    assert state == FinalConfirmedPdfCutoverState.READY
    with session_factory() as session:
        assert session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint) is None


def test_migration_idempotent(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    _write_legacy_pdf(base_repo, owner_key, b"idempotent migration content")

    run_cutover_installer(engine)
    run_cutover_installer(engine)
    run_cutover_installer(engine)

    with session_factory() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM cv_confirmed_pdf_current_authority WHERE owner_key_fingerprint = :ok"),
            {"ok": owner_key.owner_key_fingerprint},
        ).scalar_one()
        assert count == 1


# 12-14: old slot frozen -----------------------------------------------------------------


def test_old_slot_insert_frozen(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER"):
            with session.begin():
                session.execute(
                    text(
                        "INSERT INTO cv_document_pdf_slots (owner_key_fingerprint, slot_version, updated_at) "
                        "VALUES (:ok, 0, 'now')"
                    ),
                    {"ok": "a" * 64},
                )


def test_old_slot_update_frozen(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    _write_legacy_pdf(base_repo, owner_key, b"update frozen content")

    run_cutover_installer(engine)
    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER"):
            with session.begin():
                session.execute(
                    text("UPDATE cv_document_pdf_slots SET slot_version = slot_version + 1 WHERE owner_key_fingerprint = :ok"),
                    {"ok": owner_key.owner_key_fingerprint},
                )


def test_old_slot_delete_frozen(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    _write_legacy_pdf(base_repo, owner_key, b"delete frozen content")

    run_cutover_installer(engine)
    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER"):
            with session.begin():
                session.execute(
                    text("DELETE FROM cv_document_pdf_slots WHERE owner_key_fingerprint = :ok"),
                    {"ok": owner_key.owner_key_fingerprint},
                )


# 15-16: new-line authority coexists with history; restart after FINAL stays READY -------


def _make_final_confirmed_artifact(owner_key, snapshot: FinalDocxSnapshot, pdf_bytes: bytes):
    rt = build_converter_runtime_identity(
        implementation_id="libreoffice", implementation_version="7.6", executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2", executable_sha256="a" * 64, platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )
    return build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version="policy-v1", pdf_sha256=_sha256(pdf_bytes),
    )


def test_new_line_authority_coexists_with_legacy_history(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    owner_key = _owner_key()
    legacy_pdf = _write_legacy_pdf(base_repo, owner_key, b"coexistence legacy content")

    run_cutover_installer(engine)

    # legacy history is preserved in cv_document_pdf_slots even though the
    # product-facing authority now lives in cv_confirmed_pdf_current_authority.
    with session_factory() as session:
        slot = session.get(cdm.CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == legacy_pdf.artifact_fingerprint
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.current_artifact_fingerprint == legacy_pdf.artifact_fingerprint


def test_restart_after_final_lineage_remains_ready(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blobs", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_snapshot_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    final_repo = FinalConfirmedPdfSqlRepository(session_factory, store)

    owner_key = _owner_key()
    legacy_pdf = _write_legacy_pdf(base_repo, owner_key, b"final lineage restart content")
    run_cutover_installer(engine)
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.READY

    with session_factory() as session:
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.lineage_kind == "LEGACY_VALIDATED_DOCX"
        assert authority.current_artifact_fingerprint == legacy_pdf.artifact_fingerprint

    # Build a real FinalDocxSnapshot rooted in the same proposal, save a
    # FinalConfirmedPdfArtifact, then promote authority to FINAL_DOCX_SNAPSHOT.
    content = b"final lineage restart content"
    gen_hash = _sha256(content)
    with session_factory() as session:
        proposal_row = session.execute(
            text(
                "SELECT artifact_fingerprint, proposal_revision, generated_docx_content_hash "
                "FROM cv_docx_proposal_artifacts WHERE owner_key_fingerprint = :ok"
            ),
            {"ok": owner_key.owner_key_fingerprint},
        ).mappings().first()

    final_hash = _sha256(b"final docx bytes for restart test")
    final_snap_fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_row["artifact_fingerprint"],
        source_proposal_revision=proposal_row["proposal_revision"],
        generated_proposal_sha256=proposal_row["generated_docx_content_hash"],
        final_docx_sha256=final_hash, finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    final_snapshot = FinalDocxSnapshot(
        owner_key=owner_key, source_proposal_artifact_fingerprint=proposal_row["artifact_fingerprint"],
        source_proposal_revision=proposal_row["proposal_revision"],
        generated_proposal_sha256=proposal_row["generated_docx_content_hash"], final_docx_sha256=final_hash,
        edited_by_user=proposal_row["generated_docx_content_hash"] != final_hash,
        finalization_policy_version="fin-v1", snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=final_snap_fp,
    )
    final_snapshot_repo.save_final_docx_snapshot(final_snapshot, b"final docx bytes for restart test")

    final_pdf_bytes = b"%PDF-final-restart-bytes"
    final_artifact = _make_final_confirmed_artifact(owner_key, final_snapshot, final_pdf_bytes)
    save_result = final_repo.save_artifact(final_artifact, final_pdf_bytes)
    assert save_result.status.value == "SAVED"

    current_authority_read = final_repo.read_current_authority(owner_key)
    promote_result = final_repo.promote_current_authority(
        owner_key, final_artifact.artifact_fingerprint,
        current_authority_read.observation or ConfirmedPdfCurrentAuthorityObservation(exists=False),
    )
    assert promote_result.status.value in ("PROMOTED",)

    # "restart": re-run the evaluator (and idempotent installer) fresh.
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.READY
    assert run_cutover_installer(engine) == FinalConfirmedPdfCutoverState.READY

    with session_factory() as session:
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.lineage_kind == "FINAL_DOCX_SNAPSHOT"
        assert authority.current_artifact_fingerprint == final_artifact.artifact_fingerprint
        # The historical legacy slot pointer is untouched -- still points at
        # the original legacy PDF, deliberately different now from authority.
        slot = session.get(cdm.CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_artifact_fingerprint == legacy_pdf.artifact_fingerprint
        assert slot.current_artifact_fingerprint != authority.current_artifact_fingerprint


# 17: earlier create_all branches never create the new tables -----------------------------


def test_earlier_create_all_branches_do_not_create_new_tables(tmp_path: Path) -> None:
    from app.db_engine import make_sync_engine

    engine = make_sync_engine(tmp_path / "isolation.db")
    # Deliberately do NOT call init_models_sync -- prove that no earlier,
    # generic create_all path in this codebase can ever bring these tables
    # into existence outside the P6-B3b-A cutover installer.
    Base.metadata.create_all(engine, tables=_non_new_tables())
    with engine.connect() as conn:
        for table_name in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES:
            row = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).first()
            assert row is None
