"""Unit tests for the P6-C1a CV Document Workflow query services:
``query_latest_final_docx_snapshot`` (deterministic latest-snapshot lookup)
and ``CvDocumentWorkflowQueryService.get_workflow_status`` (status
aggregate).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    build_final_confirmed_pdf_artifact,
)
from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.schemas.cv_document_workflow_api import CvDocumentWorkflowStatusResponse
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    InMemoryFinalConfirmedPdfRepository,
)
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_models import (
    CvDocumentArtifactOwner,
    CvDocumentBlob,
    CvDocxFinalSnapshotRow,
    CvDocxProposalArtifactRow,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_store import WorkingCopyStore
from app.services.cv_document_workflow_query import (
    CvDocumentWorkflowQueryService,
    LatestFinalSnapshotQueryStatus,
    WorkflowStatusQueryStatus,
    query_latest_final_docx_snapshot,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'workflow_query.db'}", future=True)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)


def _insert_owner(session_factory, owner_key) -> None:
    with session_factory() as session:
        session.add(
            CvDocumentArtifactOwner(
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                person_entity_id=str(owner_key.person_entity_id),
                owner_kind=owner_key.owner_kind.value,
                owner_reference_id=owner_key.owner_reference_id,
                owner_key_schema_version=owner_key.owner_key_schema_version,
            )
        )
        session.commit()


def _insert_proposal(session_factory, owner_key, *, artifact_fingerprint: str, blob_sha256: str) -> None:
    with session_factory() as session:
        session.add(
            CvDocumentBlob(
                blob_sha256=blob_sha256,
                byte_size=10,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                storage_locator=f"blobs/{blob_sha256[:2]}/{blob_sha256[2:4]}/{blob_sha256}.blob",
                storage_status="OK",
            )
        )
        session.add(
            CvDocxProposalArtifactRow(
                artifact_fingerprint=artifact_fingerprint,
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                artifact_id=str(uuid4()),
                approved_cv_content_fingerprint="a" * 64,
                content_plan_fingerprint="b" * 64,
                role_strategy_context_fingerprint=None,
                document_schema_version="cv-document-proposal-schema-v1",
                rendering_policy_version="v1",
                renderer_adapter_id="fake-renderer",
                template_fingerprint="c" * 64,
                generation_input_fingerprint="d" * 64,
                generated_docx_content_hash=blob_sha256,
                current_file_hash=blob_sha256,
                validated_file_hash=None,
                proposal_revision=1,
                provenance_mode="GENERATED_BY_RENDERER",
                artifact_storage_status="ACTIVE",
                blob_sha256=blob_sha256,
            )
        )
        session.commit()


def _insert_final_snapshot_blob(session_factory, *, blob_sha256: str) -> None:
    with session_factory() as session:
        existing = session.get(CvDocumentBlob, blob_sha256)
        if existing is not None:
            return
        session.add(
            CvDocumentBlob(
                blob_sha256=blob_sha256,
                byte_size=10,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                storage_locator=f"blobs/{blob_sha256[:2]}/{blob_sha256[2:4]}/{blob_sha256}.blob",
                storage_status="OK",
            )
        )
        session.commit()


def _make_final_snapshot_fingerprint(
    owner_key, *, proposal_fingerprint: str, final_docx_sha256: str, policy_version: str = "policy-v1"
) -> str:
    return compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_fingerprint,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha256,
        final_docx_sha256=final_docx_sha256,
        finalization_policy_version=policy_version,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )


def _insert_final_snapshot_row(
    session_factory,
    owner_key,
    *,
    proposal_fingerprint: str,
    final_docx_sha256: str,
    created_at: str,
    policy_version: str = "policy-v1",
    fingerprint_override: str | None = None,
    proposal_fingerprint_override: str | None = None,
) -> str:
    fingerprint = fingerprint_override or _make_final_snapshot_fingerprint(
        owner_key,
        proposal_fingerprint=proposal_fingerprint,
        final_docx_sha256=final_docx_sha256,
        policy_version=policy_version,
    )
    _insert_final_snapshot_blob(session_factory, blob_sha256=final_docx_sha256)
    with session_factory() as session:
        session.add(
            CvDocxFinalSnapshotRow(
                final_snapshot_fingerprint=fingerprint,
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                source_proposal_artifact_fingerprint=proposal_fingerprint_override or proposal_fingerprint,
                source_proposal_revision=1,
                generated_proposal_sha256=final_docx_sha256,
                final_docx_sha256=final_docx_sha256,
                edited_by_user=0,
                finalization_policy_version=policy_version,
                snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
                created_at=created_at,
            )
        )
        session.commit()
    return fingerprint


# ---------------------------------------------------------------------------
# query_latest_final_docx_snapshot
# ---------------------------------------------------------------------------


def test_latest_snapshot_found(session_factory) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)
    docx_sha = _sha256(b"final docx content")
    fp = _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=docx_sha,
        created_at="2024-01-01T00:00:00.000000Z",
    )

    result = query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.FOUND
    assert result.snapshot is not None
    assert result.snapshot.final_snapshot_fingerprint == fp
    assert result.created_at == "2024-01-01T00:00:00.000000Z"


def test_latest_snapshot_deterministic_created_at_ordering(session_factory) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)

    older_fp = _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"older"),
        created_at="2024-01-01T00:00:00.000000Z",
    )
    newer_fp = _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"newer"),
        created_at="2024-06-01T00:00:00.000000Z",
    )

    result = query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.FOUND
    assert result.snapshot.final_snapshot_fingerprint == newer_fp
    assert result.snapshot.final_snapshot_fingerprint != older_fp


def test_latest_snapshot_fingerprint_tiebreaker(session_factory) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)

    same_created_at = "2024-03-01T00:00:00.000000Z"
    low_fp = "1" * 64
    high_fp = "f" * 64
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"tie-a"),
        created_at=same_created_at,
        fingerprint_override=low_fp,
    )
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"tie-b"),
        created_at=same_created_at,
        fingerprint_override=high_fp,
    )

    result = query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.FOUND
    assert result.snapshot.final_snapshot_fingerprint == high_fp


def test_latest_snapshot_not_found(session_factory) -> None:
    owner_key = _owner_key()
    result = query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.NOT_FOUND
    assert result.snapshot is None


def test_latest_snapshot_invalid_orm_metadata_is_storage_metadata_conflict(session_factory) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)
    docx_sha = _sha256(b"tampered")
    # A malformed source_proposal_artifact_fingerprint bypasses the DB's own
    # CHECK constraints (there is none on this column) but fails the
    # domain model's SHA256_PATTERN field validation.
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=docx_sha,
        created_at="2024-01-01T00:00:00.000000Z",
        fingerprint_override="e" * 64,
        proposal_fingerprint_override="not-a-valid-sha256",
    )

    result = query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT
    assert result.snapshot is None


def test_latest_snapshot_storage_unavailable_on_operational_error() -> None:
    owner_key = _owner_key()

    def _boom():
        raise OperationalError("statement", {}, Exception("db locked"))

    result = query_latest_final_docx_snapshot(_boom, owner_key.owner_key_fingerprint)
    assert result.status == LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE


def test_latest_snapshot_query_is_read_only(session_factory, engine) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"read only check"),
        created_at="2024-01-01T00:00:00.000000Z",
    )

    with session_factory() as session:
        before_count = len(session.execute(select(CvDocxFinalSnapshotRow)).scalars().all())

    query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)
    query_latest_final_docx_snapshot(session_factory, owner_key.owner_key_fingerprint)

    with session_factory() as session:
        after_count = len(session.execute(select(CvDocxFinalSnapshotRow)).scalars().all())

    assert before_count == after_count == 1


# ---------------------------------------------------------------------------
# CvDocumentWorkflowQueryService.get_workflow_status
# ---------------------------------------------------------------------------


@pytest.fixture
def working_copy_store(tmp_path: Path) -> WorkingCopyStore:
    paths = WorkingCopyPaths(tmp_path / "working_copies", tmp_path / "wc_blobs")
    return WorkingCopyStore(paths, max_docx_size_bytes=10_000_000)


def _fake_converter(runtime_identity=None):
    class _Converter:
        @property
        def runtime_identity(self):
            return runtime_identity

        def convert_docx_to_pdf(self, **kwargs):  # pragma: no cover - unused in these tests
            raise NotImplementedError

    return _Converter()


def _build_runtime_identity(*, executable_sha256: str | None = None):
    return build_converter_runtime_identity(
        implementation_id="fake-converter",
        implementation_version="1.0",
        executable_canonical_path="/usr/bin/fake-soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256=executable_sha256 or "a" * 64,
        platform_identity="test-platform",
        font_environment_id="fonts-v1",
        runtime_identity_schema_version="cv-document-pdf-conversion-runtime-schema-v1",
    )


def _build_service(
    *,
    session_factory,
    working_copy_store: WorkingCopyStore,
    proposal_repository=None,
    final_confirmed_pdf_repository=None,
    converter=None,
    conversion_policy_version: str = "conversion-policy-v1",
) -> CvDocumentWorkflowQueryService:
    return CvDocumentWorkflowQueryService(
        proposal_repository=proposal_repository or InMemoryCvDocumentArtifactRepository(),
        working_copy_store=working_copy_store,
        final_snapshot_session_factory=session_factory,
        final_confirmed_pdf_repository=final_confirmed_pdf_repository or InMemoryFinalConfirmedPdfRepository(),
        converter=converter or _fake_converter(),
        conversion_policy_version=conversion_policy_version,
    )


def test_status_all_missing(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    service = _build_service(session_factory=session_factory, working_copy_store=working_copy_store)

    result = service.get_workflow_status(owner_key)
    assert result.status == WorkflowStatusQueryStatus.FOUND
    response = result.response
    assert response.proposal.exists is False
    assert response.working_copy.exists is False
    assert response.final_snapshot.exists is False
    assert response.final_pdf.exists is False
    assert response.final_pdf.freshness == "missing"
    assert response.available_actions == ()


def test_status_proposal_exists(session_factory, working_copy_store) -> None:
    from app.schemas.cv_document_artifact import CvDocxProposalArtifact, DocxProposalProvenanceMode

    owner_key = _owner_key()
    proposal_repository = InMemoryCvDocumentArtifactRepository()
    content = b"proposal bytes"
    gen_hash = _sha256(content)
    artifact = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1",
        rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer",
        template_fingerprint="c" * 64,
        generation_input_fingerprint="d" * 64,
        generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash,
        validated_file_hash=None,
        proposal_revision=1,
        superseded=False,
        artifact_fingerprint="e" * 64,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    proposal_repository.replace_current_proposal(owner_key, artifact, content, None)
    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        proposal_repository=proposal_repository,
    )

    result = service.get_workflow_status(owner_key)
    response = result.response
    assert response.proposal.exists is True
    assert response.proposal.revision == 1
    assert response.proposal.download_available is True
    assert "download_proposal" in response.available_actions
    assert "create_working_copy" in response.available_actions


def test_status_working_copy_exists(session_factory, working_copy_store) -> None:
    from app.schemas.cv_document_artifact import CvDocxProposalArtifact, DocxProposalProvenanceMode

    owner_key = _owner_key()
    proposal_repository = InMemoryCvDocumentArtifactRepository()
    content = b"proposal bytes for working copy"
    gen_hash = _sha256(content)
    artifact = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1",
        rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer",
        template_fingerprint="c" * 64,
        generation_input_fingerprint="d" * 64,
        generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash,
        validated_file_hash=None,
        proposal_revision=1,
        superseded=False,
        artifact_fingerprint="e" * 64,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    proposal_repository.replace_current_proposal(owner_key, artifact, content, None)
    create_result = working_copy_store.create_or_refresh_working_copy(
        owner_key=owner_key, repository=proposal_repository
    )
    assert create_result.status.value == "WORKING_COPY_CREATED"

    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        proposal_repository=proposal_repository,
    )
    result = service.get_workflow_status(owner_key)
    response = result.response
    assert response.working_copy.exists is True
    assert response.working_copy.edited_by_user is False
    assert response.working_copy.download_is_authoritative_edit_source is False
    assert "download_working_copy" in response.available_actions
    assert "finalize_working_copy" in response.available_actions


def test_status_final_snapshot_exists(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    _insert_owner(session_factory, owner_key)
    proposal_fp = "1" * 64
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=_sha256(b"final content"),
        created_at="2024-01-01T00:00:00.000000Z",
    )

    service = _build_service(session_factory=session_factory, working_copy_store=working_copy_store)
    result = service.get_workflow_status(owner_key)
    response = result.response
    assert response.final_snapshot.exists is True
    assert response.final_snapshot.download_available is True
    assert response.final_snapshot.created_at is not None
    assert "generate_final_pdf" in response.available_actions
    assert "download_final_docx" in response.available_actions


def _register_final_pdf_authority(
    *, owner_key, final_repo, session_factory, runtime_identity, conversion_policy_version: str
):
    proposal_fp = "1" * 64
    _insert_owner(session_factory, owner_key)
    _insert_proposal(session_factory, owner_key, artifact_fingerprint=proposal_fp, blob_sha256="a" * 64)
    final_docx_sha = _sha256(b"pdf source content")
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint=proposal_fp,
        final_docx_sha256=final_docx_sha,
        created_at="2024-01-01T00:00:00.000000Z",
    )
    snapshot_fp = _make_final_snapshot_fingerprint(
        owner_key, proposal_fingerprint=proposal_fp, final_docx_sha256=final_docx_sha
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal_fp,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha,
        final_docx_sha256=final_docx_sha,
        edited_by_user=False,
        finalization_policy_version="policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=snapshot_fp,
    )
    final_repo.register_owner(owner_key)
    final_repo.register_final_snapshot(snapshot)
    pdf_bytes = b"%PDF-1.4 fake pdf content"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot_fp,
        source_final_docx_sha256=final_docx_sha,
        runtime_identity=runtime_identity,
        conversion_policy_version=conversion_policy_version,
        pdf_sha256=_sha256(pdf_bytes),
    )
    save_result = final_repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"
    promote_result = final_repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert promote_result.status.value == "PROMOTED"
    return artifact


def test_status_final_pdf_current(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    final_repo = InMemoryFinalConfirmedPdfRepository()
    runtime_identity = _build_runtime_identity()
    _register_final_pdf_authority(
        owner_key=owner_key,
        final_repo=final_repo,
        session_factory=session_factory,
        runtime_identity=runtime_identity,
        conversion_policy_version="conversion-policy-v1",
    )

    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        final_confirmed_pdf_repository=final_repo,
        converter=_fake_converter(runtime_identity),
        conversion_policy_version="conversion-policy-v1",
    )
    result = service.get_workflow_status(owner_key)
    response = result.response
    assert response.final_pdf.exists is True
    assert response.final_pdf.is_current is True
    assert response.final_pdf.state == "already_current"
    assert response.final_pdf.download_available is True
    assert "download_final_pdf" in response.available_actions


def test_status_legacy_authority_not_treated_as_final_pdf(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    final_repo = InMemoryFinalConfirmedPdfRepository()
    final_repo.register_owner(owner_key)
    # Directly poke internal state: InMemoryFinalConfirmedPdfRepository's
    # public promote_current_authority always promotes FINAL_DOCX_SNAPSHOT
    # lineage -- a LEGACY authority can only be modeled by direct injection.
    final_repo._authority[owner_key.owner_key_fingerprint] = ConfirmedPdfCurrentAuthorityObservation(
        exists=True,
        lineage_kind=ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX,
        current_artifact_fingerprint="a" * 64,
        slot_version=1,
    )

    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        final_confirmed_pdf_repository=final_repo,
    )
    result = service.get_workflow_status(owner_key)
    response = result.response
    assert response.final_pdf.exists is False
    assert response.final_pdf.is_current is False
    assert response.final_pdf.state == "missing"
    assert "download_final_pdf" not in response.available_actions


def test_status_freshness_fresh(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    final_repo = InMemoryFinalConfirmedPdfRepository()
    runtime_identity = _build_runtime_identity()
    _register_final_pdf_authority(
        owner_key=owner_key,
        final_repo=final_repo,
        session_factory=session_factory,
        runtime_identity=runtime_identity,
        conversion_policy_version="conversion-policy-v1",
    )
    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        final_confirmed_pdf_repository=final_repo,
        converter=_fake_converter(runtime_identity),
        conversion_policy_version="conversion-policy-v1",
    )
    result = service.get_workflow_status(owner_key)
    assert result.response.final_pdf.freshness == "fresh"


def test_status_freshness_stale(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()
    final_repo = InMemoryFinalConfirmedPdfRepository()
    runtime_identity = _build_runtime_identity()
    _register_final_pdf_authority(
        owner_key=owner_key,
        final_repo=final_repo,
        session_factory=session_factory,
        runtime_identity=runtime_identity,
        conversion_policy_version="old-conversion-policy-v1",
    )
    # A different current conversion_policy_version than the one baked into
    # the artifact -> STALE_CONVERSION_POLICY -> "stale".
    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        final_confirmed_pdf_repository=final_repo,
        converter=_fake_converter(runtime_identity),
        conversion_policy_version="new-conversion-policy-v1",
    )
    result = service.get_workflow_status(owner_key)
    assert result.response.final_pdf.freshness == "stale"


def test_status_freshness_unavailable_when_converter_has_no_runtime_identity(
    session_factory, working_copy_store
) -> None:
    owner_key = _owner_key()
    final_repo = InMemoryFinalConfirmedPdfRepository()
    runtime_identity = _build_runtime_identity()
    _register_final_pdf_authority(
        owner_key=owner_key,
        final_repo=final_repo,
        session_factory=session_factory,
        runtime_identity=runtime_identity,
        conversion_policy_version="conversion-policy-v1",
    )
    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        final_confirmed_pdf_repository=final_repo,
        converter=_fake_converter(None),
        conversion_policy_version="conversion-policy-v1",
    )
    result = service.get_workflow_status(owner_key)
    assert result.response.final_pdf.freshness == "unavailable"
    # Freshness storage/converter unavailability is informational only --
    # it never turns the whole status aggregate into UNAVAILABLE.
    assert result.status == WorkflowStatusQueryStatus.FOUND


def test_status_hard_source_failure_returns_unavailable_result(session_factory, working_copy_store) -> None:
    owner_key = _owner_key()

    class _BoomProposalRepository:
        def get_current_proposal(self, owner_key):
            raise OperationalError("statement", {}, Exception("db locked"))

    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        proposal_repository=_BoomProposalRepository(),
    )
    result = service.get_workflow_status(owner_key)
    assert result.status == WorkflowStatusQueryStatus.UNAVAILABLE
    assert result.response is None


def test_status_available_actions_correctness_full_workflow(session_factory, working_copy_store) -> None:
    from app.schemas.cv_document_artifact import CvDocxProposalArtifact, DocxProposalProvenanceMode

    owner_key = _owner_key()
    proposal_repository = InMemoryCvDocumentArtifactRepository()
    content = b"full workflow proposal bytes"
    gen_hash = _sha256(content)
    artifact = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1",
        rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer",
        template_fingerprint="c" * 64,
        generation_input_fingerprint="d" * 64,
        generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash,
        validated_file_hash=None,
        proposal_revision=1,
        superseded=False,
        artifact_fingerprint="e" * 64,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    proposal_repository.replace_current_proposal(owner_key, artifact, content, None)
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=proposal_repository)

    _insert_owner(session_factory, owner_key)
    _insert_proposal(session_factory, owner_key, artifact_fingerprint="1" * 64, blob_sha256="a" * 64)
    _insert_final_snapshot_row(
        session_factory,
        owner_key,
        proposal_fingerprint="1" * 64,
        final_docx_sha256=_sha256(b"final workflow content"),
        created_at="2024-01-01T00:00:00.000000Z",
    )

    final_repo = InMemoryFinalConfirmedPdfRepository()
    runtime_identity = _build_runtime_identity()
    final_repo.register_owner(owner_key)
    snapshot_fp = _make_final_snapshot_fingerprint(
        owner_key, proposal_fingerprint="1" * 64, final_docx_sha256=_sha256(b"final workflow content")
    )
    final_docx_sha = _sha256(b"final workflow content")
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="1" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha,
        final_docx_sha256=final_docx_sha,
        edited_by_user=False,
        finalization_policy_version="policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=snapshot_fp,
    )
    final_repo.register_final_snapshot(snapshot)
    pdf_bytes = b"%PDF-1.4 full workflow"
    pdf_artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot_fp,
        source_final_docx_sha256=final_docx_sha,
        runtime_identity=runtime_identity,
        conversion_policy_version="conversion-policy-v1",
        pdf_sha256=_sha256(pdf_bytes),
    )
    final_repo.save_artifact(pdf_artifact, pdf_bytes)
    final_repo.promote_current_authority(
        owner_key, pdf_artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )

    service = _build_service(
        session_factory=session_factory,
        working_copy_store=working_copy_store,
        proposal_repository=proposal_repository,
        final_confirmed_pdf_repository=final_repo,
        converter=_fake_converter(runtime_identity),
        conversion_policy_version="conversion-policy-v1",
    )
    result = service.get_workflow_status(owner_key)
    assert result.response.available_actions == (
        "download_proposal",
        "create_working_copy",
        "download_working_copy",
        "finalize_working_copy",
        "generate_final_pdf",
        "download_final_docx",
        "download_final_pdf",
    )


def test_generate_proposal_never_appears_in_available_actions_schema() -> None:
    from app.schemas.cv_document_workflow_api import (
        CvDocumentFinalPdfResponse,
        CvDocumentFinalSnapshotResponse,
        CvDocumentProposalResponse,
        CvDocumentWorkingCopyResponse,
    )

    with pytest.raises(Exception):
        CvDocumentWorkflowStatusResponse(
            proposal=CvDocumentProposalResponse(exists=False, revision=None, download_available=False),
            working_copy=CvDocumentWorkingCopyResponse(
                exists=False, edited_by_user=None, download_available=False, finalization_available=False
            ),
            final_snapshot=CvDocumentFinalSnapshotResponse(
                exists=False, edited_by_user=None, download_available=False
            ),
            final_pdf=CvDocumentFinalPdfResponse(
                exists=False,
                is_current=False,
                state="missing",
                freshness="missing",
                download_available=False,
            ),
            available_actions=("generate_proposal",),
        )
