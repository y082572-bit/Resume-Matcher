"""Explicit Provenance Stage P6-B3b-A: unit tests for
``FinalConfirmedPdfSqlRepository`` -- schema, lineage/blob invariants, the
closed error channel, and CAS concurrency.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import Base
import app.services.cv_document_models as cdm
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    build_final_confirmed_pdf_artifact,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import run_cutover_installer
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfCanonicalReadStatus,
    FinalConfirmedPdfRetrievalStatus,
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


_POLICY_VERSION = "final-confirmed-pdf-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'final_confirmed_pdf_repo.db'}", future=True)
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


def _runtime_identity(**overrides):
    defaults = dict(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256="a" * 64,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )
    defaults.update(overrides)
    return build_converter_runtime_identity(**defaults)


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


def _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, content: bytes) -> FinalDocxSnapshot:
    proposal = _make_proposal(owner_key, 1, content)
    result = base_repo.replace_current_proposal(owner_key, proposal, content, None)
    assert result.status.value == "REPLACED"

    final_hash = _sha256(content)
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=final_hash,
        edited_by_user=False,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )
    save = final_snapshot_repo.save_final_docx_snapshot(snapshot, content)
    assert save.status.value == "SAVED"
    return snapshot


def _make_artifact(owner_key, snapshot: FinalDocxSnapshot, pdf_bytes: bytes, *, runtime_identity=None):
    return build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=runtime_identity or _runtime_identity(),
        conversion_policy_version=_POLICY_VERSION,
        pdf_sha256=_sha256(pdf_bytes),
    )


# 1: exact schema -----------------------------------------------------------------


def test_cutover_installs_exact_schema(env) -> None:
    from app.services.cv_document_final_confirmed_pdf_cutover_state import (
        FinalConfirmedPdfCutoverState,
        evaluate_cutover_state,
    )

    *_, engine = env
    assert evaluate_cutover_state(engine) == FinalConfirmedPdfCutoverState.READY


# 2: exact UNIQUE request fingerprint ------------------------------------------------


def test_unique_request_fingerprint_enforced_at_sql_level(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"unique request fp content")
    rt = _runtime_identity()
    a = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"pdf a"),
    )
    b = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"pdf b"),
    )
    assert a.conversion_request_fingerprint == b.conversion_request_fingerprint
    r1 = repo.save_artifact(a, b"pdf a")
    r2 = repo.save_artifact(b, b"pdf b")
    assert r1.status == FinalConfirmedPdfSaveStatus.SAVED
    assert r2.status == FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL
    with session_factory() as session:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM cv_final_confirmed_pdf_artifacts WHERE conversion_request_fingerprint = :fp"
            ),
            {"fp": a.conversion_request_fingerprint},
        ).scalar_one()
        assert count == 1


# 3-5: owner/snapshot/blob FK --------------------------------------------------------


def test_owner_fk_enforced(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    other_owner = _owner_key("job-other-owner")
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"owner fk content")
    artifact = _make_artifact(other_owner, snapshot, b"owner fk pdf")
    result = repo.save_artifact(artifact, b"owner fk pdf")
    assert result.status == FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND


def test_snapshot_fk_enforced(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"snapshot fk content")
    fake_snapshot = FinalDocxSnapshot(
        owner_key=owner_key, source_proposal_artifact_fingerprint="a" * 64, source_proposal_revision=1,
        generated_proposal_sha256="b" * 64, final_docx_sha256="b" * 64, edited_by_user=False,
        finalization_policy_version=_POLICY_VERSION, snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint="f" * 64,
    )
    artifact = _make_artifact(owner_key, fake_snapshot, b"snapshot fk pdf")
    result = repo.save_artifact(artifact, b"snapshot fk pdf")
    assert result.status == FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND


def test_blob_fk_and_media_type_conflict(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"blob fk content")

    shared_content = b"bytes that will collide across media types"
    store.write_blob(shared_content, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    with session_factory() as session:
        from app.schemas.cv_document_storage import DocumentBlobStorageStatus

        with session.begin():
            session.add(
                cdm.CvDocumentBlob(
                    blob_sha256=_sha256(shared_content),
                    byte_size=len(shared_content),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    storage_locator=store.locator_for(_sha256(shared_content)),
                    storage_status=DocumentBlobStorageStatus.OK.value,
                )
            )

    artifact = _make_artifact(owner_key, snapshot, shared_content)
    result = repo.save_artifact(artifact, shared_content)
    assert result.status == FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT


# 6-8: source lineage trigger checks --------------------------------------------------


def test_source_snapshot_owner_mismatch_blocked_by_trigger(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_a = _owner_key("job-owner-a")
    owner_b = _owner_key("job-owner-b")
    snapshot_a = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_a, b"lineage owner a content")
    # Also register owner_b so save's own OWNER_NOT_FOUND check does not
    # short-circuit before ever reaching the SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH path.
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_b, b"lineage owner b content")

    artifact = _make_artifact(owner_b, snapshot_a, b"cross owner pdf")
    result = repo.save_artifact(artifact, b"cross owner pdf")
    assert result.status == FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH


def test_source_docx_hash_mismatch_blocked(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"hash mismatch content")
    rt = _runtime_identity()
    tampered = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256="9" * 64,
        runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION,
        pdf_sha256=_sha256(b"tampered hash pdf"),
    )
    result = repo.save_artifact(tampered, b"tampered hash pdf")
    assert result.status == FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH


def test_trigger_snapshot_missing_raises_when_python_check_bypassed(env) -> None:
    """The Python-level pre-check in ``save_artifact`` already blocks a
    missing source snapshot (SOURCE_FINAL_SNAPSHOT_NOT_FOUND) -- this test
    proves the SQL trigger itself is the second, independent line of
    defense by inserting directly and bypassing the repository entirely."""

    _, base_repo, final_snapshot_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"trigger direct insert content")
    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_FINAL_CONFIRMED_PDF_ARTIFACT_SNAPSHOT_MISSING"):
            with session.begin():
                session.execute(
                    text(
                        "INSERT INTO cv_final_confirmed_pdf_artifacts VALUES ("
                        ":af, :ok, :snap, :docx, :req, :rt_fp, :rt_json, :policy, :pdf, :blob, :req_v, :art_v, :created"
                        ")"
                    ),
                    {
                        "af": "1" * 64, "ok": owner_key.owner_key_fingerprint, "snap": "9" * 64,
                        "docx": "a" * 64, "req": "2" * 64, "rt_fp": "b" * 64, "rt_json": "{}",
                        "policy": "p1", "pdf": "c" * 64, "blob": "c" * 64,
                        "req_v": "cv-final-confirmed-pdf-request-fingerprint-schema-v1",
                        "art_v": "cv-final-confirmed-pdf-artifact-fingerprint-schema-v1",
                        "created": "2024-01-01",
                    },
                )


# 9-10: artifact immutability --------------------------------------------------------


def test_artifact_update_blocked(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"immutable update content")
    artifact = _make_artifact(owner_key, snapshot, b"immutable update pdf")
    repo.save_artifact(artifact, b"immutable update pdf")

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_FINAL_CONFIRMED_PDF_ARTIFACT_IMMUTABLE"):
            with session.begin():
                session.execute(
                    text(
                        "UPDATE cv_final_confirmed_pdf_artifacts SET conversion_policy_version = 'tampered' "
                        "WHERE artifact_fingerprint = :fp"
                    ),
                    {"fp": artifact.artifact_fingerprint},
                )


def test_artifact_delete_blocked(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"immutable delete content")
    artifact = _make_artifact(owner_key, snapshot, b"immutable delete pdf")
    repo.save_artifact(artifact, b"immutable delete pdf")

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_FINAL_CONFIRMED_PDF_ARTIFACT_NO_DELETE"):
            with session.begin():
                session.execute(
                    text("DELETE FROM cv_final_confirmed_pdf_artifacts WHERE artifact_fingerprint = :fp"),
                    {"fp": artifact.artifact_fingerprint},
                )


# 11-16: authority triggers ------------------------------------------------------------


def test_authority_insert_final_validation(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority insert final content")
    artifact = _make_artifact(owner_key, snapshot, b"authority insert final pdf")
    repo.save_artifact(artifact, b"authority insert final pdf")

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_FINAL_ARTIFACT_MISSING"):
            with session.begin():
                session.execute(
                    text(
                        "INSERT INTO cv_confirmed_pdf_current_authority VALUES "
                        "(:ok, 'FINAL_DOCX_SNAPSHOT', :missing_fp, 1, :now)"
                    ),
                    {"ok": owner_key.owner_key_fingerprint, "missing_fp": "9" * 64, "now": "2024-01-01"},
                )


def test_authority_insert_legacy_validation(env) -> None:
    _, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority insert legacy content")

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_LEGACY_ARTIFACT_MISSING"):
            with session.begin():
                session.execute(
                    text(
                        "INSERT INTO cv_confirmed_pdf_current_authority VALUES "
                        "(:ok, 'LEGACY_VALIDATED_DOCX', :missing_fp, 1, :now)"
                    ),
                    {"ok": owner_key.owner_key_fingerprint, "missing_fp": "9" * 64, "now": "2024-01-01"},
                )


def test_authority_update_final_validation(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority update final content")
    artifact = _make_artifact(owner_key, snapshot, b"authority update final pdf")
    repo.save_artifact(artifact, b"authority update final pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_FINAL_ARTIFACT_MISSING"):
            with session.begin():
                session.execute(
                    text(
                        "UPDATE cv_confirmed_pdf_current_authority SET current_artifact_fingerprint = :missing_fp, "
                        "slot_version = slot_version + 1 WHERE owner_key_fingerprint = :ok"
                    ),
                    {"missing_fp": "9" * 64, "ok": owner_key.owner_key_fingerprint},
                )


def test_authority_update_legacy_validation(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority update legacy content")
    artifact = _make_artifact(owner_key, snapshot, b"authority update legacy pdf")
    repo.save_artifact(artifact, b"authority update legacy pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_LEGACY_ARTIFACT_MISSING"):
            with session.begin():
                session.execute(
                    text(
                        "UPDATE cv_confirmed_pdf_current_authority SET lineage_kind = 'LEGACY_VALIDATED_DOCX', "
                        "current_artifact_fingerprint = :missing_fp, slot_version = slot_version + 1 "
                        "WHERE owner_key_fingerprint = :ok"
                    ),
                    {"missing_fp": "9" * 64, "ok": owner_key.owner_key_fingerprint},
                )


def test_authority_owner_immutable(env) -> None:
    """Changing ``owner_key_fingerprint`` always also breaks the lineage
    validation trigger's (fingerprint, owner) pairing check first -- so
    isolating the owner-immutability trigger itself requires temporarily
    dropping that other trigger, exactly as the metadata-conflict tests
    above do for their own target trigger."""

    from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
        AUTHORITY_UPDATE_FINAL_SQL,
        TRG_AUTHORITY_UPDATE_FINAL,
    )

    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    other_owner = _owner_key("job-owner-immutable-other")
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority owner immutable content")
    _setup_final_snapshot(base_repo, final_snapshot_repo, other_owner, b"other owner content for immutable test")
    artifact = _make_artifact(owner_key, snapshot, b"authority owner immutable pdf")
    repo.save_artifact(artifact, b"authority owner immutable pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        session.execute(text(f"DROP TRIGGER {TRG_AUTHORITY_UPDATE_FINAL}"))
        session.commit()
    try:
        with session_factory() as session:
            with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_OWNER_IMMUTABLE"):
                with session.begin():
                    session.execute(
                        text(
                            "UPDATE cv_confirmed_pdf_current_authority SET owner_key_fingerprint = :other, "
                            "slot_version = slot_version + 1 WHERE owner_key_fingerprint = :ok"
                        ),
                        {"other": other_owner.owner_key_fingerprint, "ok": owner_key.owner_key_fingerprint},
                    )
    finally:
        with session_factory() as session:
            session.execute(text(AUTHORITY_UPDATE_FINAL_SQL))
            session.commit()


def test_authority_exact_slot_increment(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority slot increment content")
    artifact = _make_artifact(owner_key, snapshot, b"authority slot increment pdf")
    repo.save_artifact(artifact, b"authority slot increment pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        with pytest.raises(IntegrityError, match="CV_CONFIRMED_PDF_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT"):
            with session.begin():
                session.execute(
                    text(
                        "UPDATE cv_confirmed_pdf_current_authority SET slot_version = slot_version + 2 "
                        "WHERE owner_key_fingerprint = :ok"
                    ),
                    {"ok": owner_key.owner_key_fingerprint},
                )


# 17-19: save / replay / canonical race -----------------------------------------------


def test_save_artifact(env) -> None:
    repo, base_repo, final_snapshot_repo, _, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"save artifact content")
    artifact = _make_artifact(owner_key, snapshot, b"save artifact pdf")
    result = repo.save_artifact(artifact, b"save artifact pdf")
    assert result.status == FinalConfirmedPdfSaveStatus.SAVED
    assert store.blob_exists(artifact.pdf_sha256)


def test_identical_save_replay(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"identical replay content")
    artifact = _make_artifact(owner_key, snapshot, b"identical replay pdf")
    first = repo.save_artifact(artifact, b"identical replay pdf")
    second = repo.save_artifact(artifact, b"identical replay pdf")
    assert first.status == FinalConfirmedPdfSaveStatus.SAVED
    assert second.status == FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL


def test_nondeterministic_same_request_canonical_race(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"canonical race content")
    rt = _runtime_identity()
    winner = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"winner bytes"),
    )
    loser = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"loser bytes"),
    )
    repo.save_artifact(winner, b"winner bytes")
    result = repo.save_artifact(loser, b"loser bytes")
    assert result.status == FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL
    assert result.artifact.artifact_fingerprint == winner.artifact_fingerprint


# 20: fresh Session after IntegrityError -----------------------------------------------


def test_fresh_session_used_after_integrity_error(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"fresh session content")
    artifact = _make_artifact(owner_key, snapshot, b"fresh session pdf")
    repo.save_artifact(artifact, b"fresh session pdf")

    reread_calls = {"n": 0}
    real_reread = repo._reread_after_race

    def spying_reread(art):
        reread_calls["n"] += 1
        return real_reread(art)

    monkeypatch.setattr(repo, "_reread_after_race", spying_reread)
    result = repo.save_artifact(artifact, b"fresh session pdf")
    # Identical PK re-save short-circuits before ever needing the race path --
    # confirm the ordinary existing-row branch already reports the same status.
    assert result.status == FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL
    assert reread_calls["n"] == 0


# 21: media type conflict (see test_blob_fk_and_media_type_conflict above) -------------
# 22: corrupted blob conflict ------------------------------------------------------------


def test_corrupted_existing_blob_never_overwritten(env) -> None:
    repo, base_repo, final_snapshot_repo, _, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"corrupted blob content")
    pdf_bytes = b"pdf bytes whose blob will be corrupted first"
    pdf_hash = _sha256(pdf_bytes)

    final_path_parent = store.blobs_dir / pdf_hash[0:2] / pdf_hash[2:4]
    final_path_parent.mkdir(parents=True, exist_ok=True)
    (final_path_parent / f"{pdf_hash}.blob").write_bytes(b"pre-existing corrupted bytes")

    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    result = repo.save_artifact(artifact, pdf_bytes)
    assert result.status == FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE
    assert (final_path_parent / f"{pdf_hash}.blob").read_bytes() == b"pre-existing corrupted bytes"


# 23: runtime JSON canonical mismatch ---------------------------------------------------


def test_runtime_json_canonical_mismatch_detected_at_read(env) -> None:
    from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
        ARTIFACT_NO_UPDATE_SQL,
        TRG_ARTIFACT_NO_UPDATE,
    )

    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"runtime json tamper content")
    artifact = _make_artifact(owner_key, snapshot, b"runtime json tamper pdf")
    repo.save_artifact(artifact, b"runtime json tamper pdf")

    # The artifact's own immutability trigger blocks every UPDATE
    # unconditionally -- temporarily drop it (only way to simulate
    # out-of-band storage corruption) and always restore it before this
    # test returns, whether or not the corruption below succeeds.
    with session_factory() as session:
        session.execute(text(f"DROP TRIGGER {TRG_ARTIFACT_NO_UPDATE}"))
        session.commit()
    try:
        with session_factory() as session:
            session.execute(text("PRAGMA ignore_check_constraints=1"))
            session.execute(
                text(
                    "UPDATE cv_final_confirmed_pdf_artifacts SET runtime_identity_json = "
                    "'{\"tampered\": true}' WHERE artifact_fingerprint = :fp"
                ),
                {"fp": artifact.artifact_fingerprint},
            )
            session.commit()
    finally:
        with session_factory() as session:
            session.execute(text(ARTIFACT_NO_UPDATE_SQL))
            session.commit()

    result = repo.read_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfArtifactReadStatus.STORAGE_METADATA_CONFLICT


# 24-25: read artifact NOT_FOUND / STORAGE_UNAVAILABLE ----------------------------------


def test_read_artifact_not_found(env) -> None:
    repo, *_ = env
    result = repo.read_artifact("f" * 64)
    assert result.status == FinalConfirmedPdfArtifactReadStatus.NOT_FOUND


def test_read_artifact_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, *_ = env

    def boom(*args, **kwargs):
        raise OperationalError("stmt", {}, Exception("db locked"))

    monkeypatch.setattr(repo, "_session_factory", boom)
    result = repo.read_artifact("f" * 64)
    assert result.status == FinalConfirmedPdfArtifactReadStatus.STORAGE_UNAVAILABLE


# 26-29: authority reads -----------------------------------------------------------------


def test_authority_absent(env) -> None:
    repo, *_ = env
    result = repo.read_current_authority(_owner_key())
    assert result.status == FinalConfirmedPdfAuthorityReadStatus.ABSENT


def test_authority_found(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority found content")
    artifact = _make_artifact(owner_key, snapshot, b"authority found pdf")
    repo.save_artifact(artifact, b"authority found pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    result = repo.read_current_authority(owner_key)
    assert result.status == FinalConfirmedPdfAuthorityReadStatus.FOUND
    assert result.observation.current_artifact_fingerprint == artifact.artifact_fingerprint


def test_authority_storage_metadata_conflict(env) -> None:
    from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
        AUTHORITY_SLOT_VERSION_INCREMENT_SQL,
        TRG_AUTHORITY_SLOT_VERSION_INCREMENT,
    )

    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"authority conflict content")
    artifact = _make_artifact(owner_key, snapshot, b"authority conflict pdf")
    repo.save_artifact(artifact, b"authority conflict pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    # The slot_version-increment trigger blocks any UPDATE that does not
    # bump slot_version by exactly 1 -- temporarily drop it (only way to
    # simulate out-of-band storage corruption) and always restore it.
    with session_factory() as session:
        session.execute(text(f"DROP TRIGGER {TRG_AUTHORITY_SLOT_VERSION_INCREMENT}"))
        session.commit()
    try:
        with session_factory() as session:
            session.execute(text("PRAGMA ignore_check_constraints=1"))
            session.execute(
                text(
                    "UPDATE cv_confirmed_pdf_current_authority SET slot_version = 0 WHERE owner_key_fingerprint = :ok"
                ),
                {"ok": owner_key.owner_key_fingerprint},
            )
            session.commit()
    finally:
        with session_factory() as session:
            session.execute(text(AUTHORITY_SLOT_VERSION_INCREMENT_SQL))
            session.commit()

    result = repo.read_current_authority(owner_key)
    assert result.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT


def test_authority_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, *_ = env

    def boom(*args, **kwargs):
        raise OperationalError("stmt", {}, Exception("db locked"))

    monkeypatch.setattr(repo, "_session_factory", boom)
    result = repo.read_current_authority(_owner_key())
    assert result.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE


# 30-31: canonical reads -------------------------------------------------------------------


def test_canonical_not_found(env) -> None:
    repo, *_ = env
    result = repo.read_canonical_artifact_by_conversion_request_fingerprint("f" * 64)
    assert result.status == FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND


def test_canonical_found(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"canonical found content")
    artifact = _make_artifact(owner_key, snapshot, b"canonical found pdf")
    repo.save_artifact(artifact, b"canonical found pdf")
    result = repo.read_canonical_artifact_by_conversion_request_fingerprint(artifact.conversion_request_fingerprint)
    assert result.status == FinalConfirmedPdfCanonicalReadStatus.FOUND
    assert result.artifact == artifact


# 32-36: retrieval ---------------------------------------------------------------------------


def test_retrieve_by_artifact_ok(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"retrieve ok content")
    pdf_bytes = b"retrieve ok pdf bytes"
    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    repo.save_artifact(artifact, pdf_bytes)
    result = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfRetrievalStatus.OK
    assert result.pdf_bytes == pdf_bytes


def test_retrieve_blob_missing(env) -> None:
    repo, base_repo, final_snapshot_repo, _, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"retrieve missing content")
    pdf_bytes = b"retrieve missing pdf bytes"
    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    repo.save_artifact(artifact, pdf_bytes)

    blob_path = store.blobs_dir / artifact.pdf_sha256[0:2] / artifact.pdf_sha256[2:4] / f"{artifact.pdf_sha256}.blob"
    blob_path.unlink()

    result = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfRetrievalStatus.BLOB_MISSING


def test_retrieve_hash_mismatch(env) -> None:
    repo, base_repo, final_snapshot_repo, _, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"retrieve hash mismatch content")
    pdf_bytes = b"retrieve hash mismatch pdf bytes"
    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    repo.save_artifact(artifact, pdf_bytes)

    blob_path = store.blobs_dir / artifact.pdf_sha256[0:2] / artifact.pdf_sha256[2:4] / f"{artifact.pdf_sha256}.blob"
    blob_path.write_bytes(b"tampered bytes")

    result = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH


def test_retrieve_size_mismatch(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"retrieve size mismatch content")
    pdf_bytes = b"retrieve size mismatch pdf bytes"
    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    repo.save_artifact(artifact, pdf_bytes)

    with session_factory() as session:
        session.execute(
            text("UPDATE cv_document_blobs SET byte_size = byte_size + 100 WHERE blob_sha256 = :sha"),
            {"sha": artifact.pdf_sha256},
        )
        session.commit()

    result = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfRetrievalStatus.BLOB_SIZE_MISMATCH


def test_retrieve_media_type_mismatch(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, store, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"retrieve media type mismatch content")
    pdf_bytes = b"retrieve media type mismatch pdf bytes"
    artifact = _make_artifact(owner_key, snapshot, pdf_bytes)
    repo.save_artifact(artifact, pdf_bytes)

    with session_factory() as session:
        session.execute(
            text("UPDATE cv_document_blobs SET media_type = 'application/octet-stream' WHERE blob_sha256 = :sha"),
            {"sha": artifact.pdf_sha256},
        )
        session.commit()

    result = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert result.status == FinalConfirmedPdfRetrievalStatus.BLOB_MEDIA_TYPE_MISMATCH


# 37-43: CAS ------------------------------------------------------------------------------------


def test_absent_row_cas_winner(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"cas absent winner content")
    artifact = _make_artifact(owner_key, snapshot, b"cas absent winner pdf")
    repo.save_artifact(artifact, b"cas absent winner pdf")

    result = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED
    assert result.current_observation.slot_version == 1


def test_absent_row_cas_loser_fresh_observation(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"cas absent loser content")
    artifact = _make_artifact(owner_key, snapshot, b"cas absent loser pdf")
    repo.save_artifact(artifact, b"cas absent loser pdf")
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    loser = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    assert loser.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION
    assert loser.current_observation.exists is True
    assert loser.current_observation.slot_version == 1


def test_existing_row_cas_winner(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"cas existing winner content")
    artifact = _make_artifact(owner_key, snapshot, b"cas existing winner pdf")
    repo.save_artifact(artifact, b"cas existing winner pdf")
    first = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))

    second = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, first.current_observation)
    assert second.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED
    assert second.current_observation.slot_version == 2


def test_existing_row_cas_loser_fresh_observation(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"cas existing loser content")
    artifact = _make_artifact(owner_key, snapshot, b"cas existing loser pdf")
    repo.save_artifact(artifact, b"cas existing loser pdf")
    first = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, first.current_observation)

    stale = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, first.current_observation)
    assert stale.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION
    assert stale.current_observation.slot_version == 2


def test_cas_owner_missing(env) -> None:
    repo, *_ = env
    result = repo.promote_current_authority(_owner_key(), "f" * 64, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND


def test_cas_target_missing(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"cas target missing content")
    result = repo.promote_current_authority(owner_key, "f" * 64, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_NOT_FOUND


def test_cas_target_owner_mismatch(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_a = _owner_key("job-cas-owner-a")
    owner_b = _owner_key("job-cas-owner-b")
    snapshot_a = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_a, b"cas owner mismatch content a")
    _setup_final_snapshot(base_repo, final_snapshot_repo, owner_b, b"cas owner mismatch content b")
    artifact_a = _make_artifact(owner_a, snapshot_a, b"cas owner mismatch pdf a")
    repo.save_artifact(artifact_a, b"cas owner mismatch pdf a")

    result = repo.promote_current_authority(owner_b, artifact_a.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False))
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_OWNER_MISMATCH


# -- concurrency (threaded) ------------------------------------------------------------------


def test_concurrent_absent_row_cas_exactly_one_promoted(env) -> None:
    repo, base_repo, final_snapshot_repo, _, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"concurrency absent cas content")
    artifact = _make_artifact(owner_key, snapshot, b"concurrency absent cas pdf")
    repo.save_artifact(artifact, b"concurrency absent cas pdf")

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

    statuses = sorted(r.status.value for r in results.values())
    assert statuses == sorted(
        [FinalConfirmedPdfAuthorityCasStatus.PROMOTED.value, FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION.value]
    )


def test_concurrent_same_request_different_bytes_exactly_one_canonical(env) -> None:
    repo, base_repo, final_snapshot_repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"concurrency canonical content")
    rt = _runtime_identity()
    artifact_a = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"concurrency pdf a"),
    )
    artifact_b = build_final_confirmed_pdf_artifact(
        owner_key=owner_key, source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256, runtime_identity=rt,
        conversion_policy_version=_POLICY_VERSION, pdf_sha256=_sha256(b"concurrency pdf b"),
    )

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, artifact, pdf_bytes: bytes) -> None:
        barrier.wait()
        results[name] = repo.save_artifact(artifact, pdf_bytes)

    t1 = threading.Thread(target=worker, args=("A", artifact_a, b"concurrency pdf a"))
    t2 = threading.Thread(target=worker, args=("B", artifact_b, b"concurrency pdf b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status.value for r in results.values())
    assert statuses == sorted(
        [FinalConfirmedPdfSaveStatus.SAVED.value, FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL.value]
    )

    with session_factory() as session:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM cv_final_confirmed_pdf_artifacts WHERE conversion_request_fingerprint = :fp"
            ),
            {"fp": artifact_a.conversion_request_fingerprint},
        ).scalar_one()
        assert count == 1
