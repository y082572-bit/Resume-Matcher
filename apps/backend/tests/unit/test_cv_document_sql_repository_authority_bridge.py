"""Explicit Provenance Stage P6-B3b-A: the old ``CvDocumentArtifactSqlRepository``
current-PDF authority bridge -- ``get_current_pdf``/``read_current_pdf_bytes``
now read exclusively from ``cv_confirmed_pdf_current_authority``, and a
successful ``replace_current_pdf`` legacy write now atomically synchronizes
that same authority row in the same transaction (the P6-B3b-A LEGACY PDF
WRITE TO CROSS-LINE AUTHORITY remediation), so a legacy PDF is visible as
current the moment it is written -- never only after a separate startup
migration. ``replace_current_pdf`` also still recognizes the permanent
legacy-slot-frozen cutover token.
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
from app.schemas.cv_document_artifact import (
    ArtifactOwnerKind,
    ConfirmedCvPdfArtifact,
    DocumentProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    ValidatedCvDocxSnapshot,
)
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import run_cutover_installer
from app.services.cv_document_final_confirmed_pdf_schema_manifest import CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN
from app.services.cv_document_pdf_confirmation_builder import (
    build_and_confirm_pdf,
    compute_pdf_artifact_fingerprint,
)
from app.services.cv_document_repository_protocol import CasReplaceStatus, CvDocumentPdfSlotCutoverStatus
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository
from app.schemas.cv_document_artifact import CvDocxProposalArtifact, DocxProposalProvenanceMode


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bridge.db'}", future=True)
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
        artifact_id=uuid4(), owner_key=owner_key, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1", rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer", template_fingerprint="c" * 64,
        generation_input_fingerprint=gen_input_fp, generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash, validated_file_hash=None, proposal_revision=revision,
        superseded=False, artifact_fingerprint=artifact_fp, provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )


def _setup_snapshot(repo, owner_key, content: bytes) -> ValidatedCvDocxSnapshot:
    proposal = _make_proposal(owner_key, 1, content)
    repo.replace_current_proposal(owner_key, proposal, content, None)
    fp = compute_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint, proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        proposal_revision=1, exact_docx_sha256=_sha256(content), approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, template_fingerprint="c" * 64, validation_policy_version="val-v1",
        structural_validated_sha256=_sha256(content), manual_confirmation_fingerprint=None,
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
    )
    snapshot = ValidatedCvDocxSnapshot(
        owner_key=owner_key, proposal_artifact_fingerprint=proposal.artifact_fingerprint, proposal_revision=1,
        exact_docx_sha256=_sha256(content), approved_cv_content_fingerprint="a" * 64, content_plan_fingerprint="b" * 64,
        template_fingerprint="c" * 64, validation_policy_version="val-v1",
        structural_validation_result=DocxStructuralValidationResult(
            status=DocxStructuralValidationStatus.VALID, validated_sha256=_sha256(content)
        ),
        manual_confirmation_fingerprint=None, provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        snapshot_fingerprint=fp,
    )
    repo.save_validated_snapshot(snapshot, content)
    return snapshot


def _make_pdf(owner_key, snapshot: ValidatedCvDocxSnapshot, revision: int, pdf_bytes: bytes) -> ConfirmedCvPdfArtifact:
    pdf_sha256 = _sha256(pdf_bytes)
    fp = compute_pdf_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256, pdf_sha256=pdf_sha256,
        conversion_adapter_id="fake-adapter", conversion_policy_version="conv-v1", pdf_revision=revision,
    )
    return ConfirmedCvPdfArtifact(
        artifact_id=uuid4(), owner_key=owner_key, source_validated_docx_snapshot_fingerprint=snapshot.snapshot_fingerprint,
        source_docx_sha256=snapshot.exact_docx_sha256, approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64, pdf_sha256=pdf_sha256, conversion_adapter_id="fake-adapter",
        conversion_policy_version="conv-v1", provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        artifact_fingerprint=fp, pdf_revision=revision, superseded=False,
    )


# 1-2: successful legacy replace creates absent authority row at slot_version 1 --------


def test_first_legacy_replace_creates_authority_row_at_slot_version_1(env) -> None:
    repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"bridge insert content")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-bridge-insert")
    result = repo.replace_current_pdf(owner_key, pdf, b"%PDF-bridge-insert", None)
    assert result.status == CasReplaceStatus.REPLACED

    with session_factory() as session:
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority is not None
        assert authority.lineage_kind == "LEGACY_VALIDATED_DOCX"
        assert authority.current_artifact_fingerprint == pdf.artifact_fingerprint
        assert authority.slot_version == 1


# 3-4: second successful legacy replace updates existing LEGACY authority, +1 -----------


def test_second_legacy_replace_updates_existing_authority_slot_version(env) -> None:
    repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"bridge update content")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-bridge-update-1")
    first = repo.replace_current_pdf(owner_key, pdf1, b"%PDF-bridge-update-1", None)
    assert first.status == CasReplaceStatus.REPLACED

    pdf2 = _make_pdf(owner_key, snapshot, 2, b"%PDF-bridge-update-2")
    second = repo.replace_current_pdf(owner_key, pdf2, b"%PDF-bridge-update-2", 1)
    assert second.status == CasReplaceStatus.REPLACED

    with session_factory() as session:
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.lineage_kind == "LEGACY_VALIDATED_DOCX"
        assert authority.current_artifact_fingerprint == pdf2.artifact_fingerprint
        assert authority.slot_version == 2


# 5-6: get_current_pdf/read_current_pdf_bytes see the bridge without startup migration ---


def test_get_current_pdf_sees_legacy_replace_without_startup_migration(env) -> None:
    repo, *_ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"bridge get_current_pdf content")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-bridge-get-current")
    result = repo.replace_current_pdf(owner_key, pdf, b"%PDF-bridge-get-current", None)
    assert result.status == CasReplaceStatus.REPLACED

    current = repo.get_current_pdf(owner_key)
    assert current is not None
    assert current.artifact_fingerprint == pdf.artifact_fingerprint


def test_read_current_pdf_bytes_sees_legacy_replace_without_startup_migration(env) -> None:
    repo, *_ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"bridge read_bytes content")
    pdf_bytes = b"%PDF-bridge-read-bytes"
    pdf = _make_pdf(owner_key, snapshot, 1, pdf_bytes)
    result = repo.replace_current_pdf(owner_key, pdf, pdf_bytes, None)
    assert result.status == CasReplaceStatus.REPLACED

    assert repo.read_current_pdf_bytes(owner_key) == pdf_bytes


# 7-9: existing FINAL authority blocks legacy replace; FINAL/legacy slot unchanged -------


def test_existing_final_authority_blocks_legacy_replace(env) -> None:
    repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"final blocks legacy content")

    # Seed a pre-existing FINAL authority row directly, as if a prior
    # FinalConfirmedPdfSqlRepository.promote_current_authority call already
    # happened for this owner -- this fixture never installs the P6-B3b-A
    # triggers, so no FK/lineage trigger fires on this raw insert.
    with session_factory() as session:
        with session.begin():
            session.add(
                cdm.CvConfirmedPdfCurrentAuthorityRow(
                    owner_key_fingerprint=owner_key.owner_key_fingerprint,
                    lineage_kind="FINAL_DOCX_SNAPSHOT",
                    current_artifact_fingerprint="a" * 64,
                    slot_version=1,
                    updated_at="2024-01-01",
                )
            )

    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-blocked-by-final")
    result = repo.replace_current_pdf(owner_key, pdf, b"%PDF-blocked-by-final", None)
    assert result.status == CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
    assert result.current_artifact is None

    with session_factory() as session:
        # FINAL authority remains unchanged.
        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.lineage_kind == "FINAL_DOCX_SNAPSHOT"
        assert authority.current_artifact_fingerprint == "a" * 64
        assert authority.slot_version == 1

        # Legacy slot was never touched -- no write was ever attempted.
        assert session.get(cdm.CvDocumentPdfSlot, owner_key.owner_key_fingerprint) is None


# 10-11: mid-transaction authority races -------------------------------------------------


class _AuthorityBridgeRaceSession:
    """Wraps a real ``Session``: forces the bridge's own UPDATE against
    ``cv_confirmed_pdf_current_authority`` to report ``rowcount=0`` (as if
    a concurrent writer already changed that row) without ever touching the
    real table, and makes the *next* ``session.get()`` for that same model
    return an injected row representing what that concurrent writer left
    behind. SQLite's single-writer locking makes genuine cross-connection
    interleaving impractical to orchestrate deterministically in-process,
    so this reproduces the same code path
    (``_AuthorityBridgeConflict`` -> fresh-read re-classification) the real
    race would trigger."""

    def __init__(self, real_session, injected_row):
        self._real = real_session
        self._injected_row = injected_row
        self._authority_get_calls = 0
        self._forced_update = False

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def get(self, model, pk, *args, **kwargs):
        if model is cdm.CvConfirmedPdfCurrentAuthorityRow:
            self._authority_get_calls += 1
            if self._authority_get_calls > 1:
                return self._injected_row
        return self._real.get(model, pk, *args, **kwargs)

    def execute(self, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if (
            not self._forced_update
            and table is not None
            and getattr(table, "name", None) == cdm.CvConfirmedPdfCurrentAuthorityRow.__tablename__
        ):
            self._forced_update = True

            class _ZeroRowcountResult:
                rowcount = 0

            return _ZeroRowcountResult()
        return self._real.execute(statement, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._real, item)


def test_concurrent_legacy_to_final_race_rolls_back_legacy_slot(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"race legacy to final content")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-race-final-1")
    first = repo.replace_current_pdf(owner_key, pdf1, b"%PDF-race-final-1", None)
    assert first.status == CasReplaceStatus.REPLACED

    injected_final_row = cdm.CvConfirmedPdfCurrentAuthorityRow(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        lineage_kind="FINAL_DOCX_SNAPSHOT",
        current_artifact_fingerprint="f" * 64,
        slot_version=2,
        updated_at="now",
    )
    real_factory = repo._session_factory
    monkeypatch.setattr(
        repo, "_session_factory", lambda: _AuthorityBridgeRaceSession(real_factory(), injected_final_row)
    )

    pdf2 = _make_pdf(owner_key, snapshot, 2, b"%PDF-race-final-2")
    result = repo.replace_current_pdf(owner_key, pdf2, b"%PDF-race-final-2", 1)
    assert result.status == CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
    assert result.current_artifact is None

    monkeypatch.undo()
    with session_factory() as session:
        # The legacy slot CAS that ran inside the same (now rolled-back)
        # transaction must never have taken effect.
        slot = session.get(cdm.CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_revision == 1
        assert slot.current_artifact_fingerprint == pdf1.artifact_fingerprint

        authority = session.get(cdm.CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
        assert authority.lineage_kind == "LEGACY_VALIDATED_DOCX"
        assert authority.slot_version == 1


def test_concurrent_legacy_version_change_returns_stale_revision(env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, session_factory, _, _ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"race legacy version content")
    pdf1 = _make_pdf(owner_key, snapshot, 1, b"%PDF-race-legacy-1")
    first = repo.replace_current_pdf(owner_key, pdf1, b"%PDF-race-legacy-1", None)
    assert first.status == CasReplaceStatus.REPLACED

    injected_legacy_row = cdm.CvConfirmedPdfCurrentAuthorityRow(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        lineage_kind="LEGACY_VALIDATED_DOCX",
        current_artifact_fingerprint=pdf1.artifact_fingerprint,
        slot_version=5,
        updated_at="now",
    )
    real_factory = repo._session_factory
    monkeypatch.setattr(
        repo, "_session_factory", lambda: _AuthorityBridgeRaceSession(real_factory(), injected_legacy_row)
    )

    pdf2 = _make_pdf(owner_key, snapshot, 2, b"%PDF-race-legacy-2")
    result = repo.replace_current_pdf(owner_key, pdf2, b"%PDF-race-legacy-2", 1)
    assert result.status == CasReplaceStatus.STALE_REVISION
    assert result.current_artifact is not None
    assert result.current_artifact.artifact_fingerprint == pdf1.artifact_fingerprint

    monkeypatch.undo()
    with session_factory() as session:
        slot = session.get(cdm.CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        assert slot.current_revision == 1
        assert slot.current_artifact_fingerprint == pdf1.artifact_fingerprint


# 12-13: permanent cutover status / ordinary race ----------------------------------------


def test_permanent_cutover_status_from_replace_current_pdf(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'frozen.db'}", future=True)
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    repo = CvDocumentArtifactSqlRepository(session_factory, store)

    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"frozen slot content")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-frozen-slot")
    result = repo.replace_current_pdf(owner_key, pdf, b"%PDF-frozen-slot", None)
    assert result.status == CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
    assert result.current_artifact is None


def test_ordinary_race_still_stale_revision(env) -> None:
    repo, *_ = env
    owner_key = _owner_key()
    snapshot = _setup_snapshot(repo, owner_key, b"ordinary race content")
    pdf = _make_pdf(owner_key, snapshot, 1, b"%PDF-ordinary-race")
    repo.replace_current_pdf(owner_key, pdf, b"%PDF-ordinary-race", None)

    pdf2 = _make_pdf(owner_key, snapshot, 2, b"%PDF-ordinary-race-2")
    # Wrong expected_previous_revision (0 instead of 1) is an ordinary CAS
    # mismatch, unrelated to the freeze -- must stay STALE_REVISION.
    result = repo.replace_current_pdf(owner_key, pdf2, b"%PDF-ordinary-race-2", 0)
    assert result.status == CasReplaceStatus.STALE_REVISION


# 9: proposal replace regression --------------------------------------------------------


def test_proposal_replace_regression_unaffected(env) -> None:
    repo, *_ = env
    owner_key = _owner_key()
    content = b"proposal regression content"
    proposal = _make_proposal(owner_key, 1, content)
    result = repo.replace_current_proposal(owner_key, proposal, content, None)
    assert result.status == CasReplaceStatus.REPLACED
    assert repo.get_current_proposal(owner_key).artifact_fingerprint == proposal.artifact_fingerprint
