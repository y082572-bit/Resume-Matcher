"""Integration tests for ``ProposalArtifactLineageSqlReader`` against a real
temporary SQLite database and a real ``CvDocumentBlobStore`` -- ordering
discipline (no blob read before every metadata check passes), the closed
error channel, and the "historical Proposal N while current is N+1"
(stale-but-valid) lineage case.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_artifact_lineage_reader import ProposalArtifactLineageStatus
from app.services.cv_document_proposal_artifact_lineage_sql_reader import ProposalArtifactLineageSqlReader
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lineage_reader.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    reader = ProposalArtifactLineageSqlReader(session_factory, store)
    return reader, base_repo, session_factory, store


def _owner_key(reference_id: str = "job-lineage-1"):
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


def _setup_proposal(base_repo, owner_key, revision: int, content: bytes) -> CvDocxProposalArtifact:
    proposal = _make_proposal(owner_key, revision, content)
    expected_previous = revision - 1 or None
    result = base_repo.replace_current_proposal(owner_key, proposal, content, expected_previous)
    assert result.status.value == "REPLACED"
    return proposal


# -- invalid arguments never touch SQL/blob -------------------------------------------


def test_invalid_artifact_fingerprint_returns_invalid_request_without_touching_sql(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    _setup_proposal(base_repo, owner_key, 1, b"proposal content for invalid-args test")

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint="not-a-valid-sha256",
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=_sha256(b"proposal content for invalid-args test"),
    )
    assert result.status == ProposalArtifactLineageStatus.INVALID_REQUEST
    assert result.lineage is None


def test_invalid_revision_returns_invalid_request(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"proposal content for invalid revision test")

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=0,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.INVALID_REQUEST


def test_invalid_arguments_never_invoke_blob_read(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env

    def boom(*args, **kwargs):
        raise AssertionError("blob store must never be read for an invalid request")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint="bad",
        expected_owner_key_fingerprint="bad",
        expected_proposal_revision=1,
        expected_generated_docx_sha256="bad",
    )
    assert result.status == ProposalArtifactLineageStatus.INVALID_REQUEST


# -- NOT_FOUND --------------------------------------------------------------------------


def test_missing_row_returns_not_found(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint="f" * 64,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256="a" * 64,
    )
    assert result.status == ProposalArtifactLineageStatus.NOT_FOUND
    assert result.lineage is None


# -- metadata mismatches never reach the blob store ------------------------------------


def test_owner_mismatch_never_reads_blob(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_a = _owner_key("job-owner-a")
    owner_b = _owner_key("job-owner-b")
    proposal_a = _setup_proposal(base_repo, owner_a, 1, b"owner A content")

    def boom(*args, **kwargs):
        raise AssertionError("blob store must never be read on an owner mismatch")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal_a.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_b.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal_a.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.OWNER_MISMATCH
    assert result.lineage is None


def test_revision_mismatch_never_reads_blob(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"revision mismatch content")

    def boom(*args, **kwargs):
        raise AssertionError("blob store must never be read on a revision mismatch")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=2,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.REVISION_MISMATCH
    assert result.lineage is None


def test_generated_hash_mismatch_never_reads_blob(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"generated hash mismatch content")

    def boom(*args, **kwargs):
        raise AssertionError("blob store must never be read on a generated-hash mismatch")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=_sha256(b"this is not the real generated content"),
    )
    assert result.status == ProposalArtifactLineageStatus.GENERATED_HASH_MISMATCH
    assert result.lineage is None


# -- bytes-hash mismatch: only reachable after every metadata check passes -----------


def test_bytes_hash_mismatch_when_blob_diverges_from_declared_hash(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    content = b"proposal content whose blob will be corrupted post-hoc"
    proposal = _setup_proposal(base_repo, owner_key, 1, content)

    # Corrupt the blob store's own bytes for this exact content hash --
    # this can only ever happen via a bug or tampering outside this reader,
    # but must still fail closed rather than silently trusting the DB row.
    blob_path = store.blobs_dir / proposal.generated_docx_content_hash[0:2] / proposal.generated_docx_content_hash[2:4]
    blob_file = blob_path / f"{proposal.generated_docx_content_hash}.blob"
    blob_file.write_bytes(b"tampered bytes that will not match the declared hash")

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE
    assert result.lineage is None


def test_bytes_hash_mismatch_reported_as_bytes_hash_mismatch_when_blob_read_succeeds_but_disagrees(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguishes ``BYTES_HASH_MISMATCH`` (blob read succeeds, but its
    freshly re-hashed bytes disagree with ``generated_docx_content_hash``)
    from ``STORAGE_UNAVAILABLE`` (the blob store itself reports failure).
    We force this by monkeypatching ``read_blob`` to return a successful
    read of different bytes than what the blob store actually stored."""

    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    content = b"proposal content for a clean bytes-hash-mismatch case"
    proposal = _setup_proposal(base_repo, owner_key, 1, content)

    from app.schemas.cv_document_storage import CvDocumentBlobReadResult

    real_read_blob = store.read_blob

    def fake_read_blob(blob_sha256: str):
        real_result = real_read_blob(blob_sha256)
        return CvDocumentBlobReadResult(
            success=True,
            blob_sha256=blob_sha256,
            data=b"bytes that do not match generated_docx_content_hash at all",
            byte_size=real_result.byte_size,
        )

    monkeypatch.setattr(store, "read_blob", fake_read_blob)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.BYTES_HASH_MISMATCH
    assert result.lineage is None


# -- VERIFIED, including the historical-Proposal-N-while-current-is-N+1 case ---------


def test_verified_for_the_current_proposal(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    content = b"the current proposal's own content"
    proposal = _setup_proposal(base_repo, owner_key, 1, content)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.VERIFIED
    assert result.lineage is not None
    assert result.lineage.docx_bytes == content
    assert result.lineage.artifact_fingerprint == proposal.artifact_fingerprint
    assert result.lineage.proposal_revision == 1
    assert result.lineage.owner_key_fingerprint == owner_key.owner_key_fingerprint


def test_historical_proposal_n_verified_while_current_is_n_plus_1(env) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    content_n = b"historical proposal N content"
    proposal_n = _setup_proposal(base_repo, owner_key, 1, content_n)

    content_n1 = b"current proposal N+1 content, supersedes N"
    _setup_proposal(base_repo, owner_key, 2, content_n1)

    # Proposal N is no longer current, but its row/blob still exist and
    # must still verify -- finalization never requires the bound Proposal
    # to still be the current slot (see WORKING_COPY_FOUNDATION docs).
    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal_n.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal_n.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.VERIFIED
    assert result.lineage is not None
    assert result.lineage.docx_bytes == content_n
    assert result.lineage.proposal_revision == 1


# -- closed error channel ---------------------------------------------------------------


def test_sqlalchemy_error_maps_to_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"sqlalchemy error test content")

    def boom():
        raise OperationalError("statement", {}, Exception("db locked"))

    monkeypatch.setattr(reader, "_session_factory", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE
    assert result.lineage is None


def test_cv_document_storage_error_maps_to_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"cv document storage error test content")

    from app.schemas.cv_document_storage import CvDocumentStorageErrorCode

    def boom(*args, **kwargs):
        raise CvDocumentStorageError.of(CvDocumentStorageErrorCode.SYMLINK_ESCAPE, "simulated symlink escape")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE
    assert result.lineage is None


def test_oserror_from_blob_layer_maps_to_storage_unavailable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"oserror blob layer test content")

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure while reading the blob")

    monkeypatch.setattr(store, "read_blob", boom)

    result = reader.read_verified_proposal_artifact_lineage(
        artifact_fingerprint=proposal.artifact_fingerprint,
        expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
        expected_proposal_revision=1,
        expected_generated_docx_sha256=proposal.generated_docx_content_hash,
    )
    assert result.status == ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE
    assert result.lineage is None


def test_no_infrastructural_exception_ever_escapes(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, base_repo, session_factory, store = env
    owner_key = _owner_key()
    proposal = _setup_proposal(base_repo, owner_key, 1, b"no leaked exception test content")

    def boom():
        raise SQLAlchemyError("generic SQLAlchemy failure")

    monkeypatch.setattr(reader, "_session_factory", boom)

    try:
        result = reader.read_verified_proposal_artifact_lineage(
            artifact_fingerprint=proposal.artifact_fingerprint,
            expected_owner_key_fingerprint=owner_key.owner_key_fingerprint,
            expected_proposal_revision=1,
            expected_generated_docx_sha256=proposal.generated_docx_content_hash,
        )
    except (SQLAlchemyError, CvDocumentStorageError, OSError):
        pytest.fail("no infrastructural exception must ever escape read_verified_proposal_artifact_lineage")
    assert result.status == ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE
