"""Explicit Provenance Stage P6-B3b-A: unit tests for
``FinalDocxSnapshotPdfSourceReader``.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf_source import FinalDocxSnapshotPdfSourceReadStatus
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.services.cv_document_blob_store import CvDocumentStorageError
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import FinalDocxSnapshotPdfSourceReader
from app.services.cv_document_final_snapshot_repository import (
    InMemoryFinalDocxSnapshotRepository,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_owner_identity import build_owner_key


def _owner(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _snapshot(owner_key, docx_bytes: bytes) -> FinalDocxSnapshot:
    final_hash = hashlib.sha256(docx_bytes).hexdigest()
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_hash,
        final_docx_sha256=final_hash,
        edited_by_user=False,
        finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def test_verified() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    owner_key = _owner()
    docx_bytes = b"verified docx bytes"
    snapshot = _snapshot(owner_key, docx_bytes)
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=owner_key, final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.VERIFIED
    assert result.snapshot == snapshot
    assert result.final_docx_bytes == docx_bytes


def test_owner_key_fingerprint_mismatch() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    owner_key = _owner()
    tampered_owner = owner_key.model_copy(update={"owner_key_fingerprint": "f" * 64})

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=tampered_owner, final_snapshot_fingerprint="a" * 64)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH


def test_final_snapshot_not_found() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    owner_key = _owner()
    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=owner_key, final_snapshot_fingerprint="a" * 64)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_NOT_FOUND


def test_final_snapshot_owner_mismatch() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    owner_key = _owner("job-owner-a")
    other_owner = _owner("job-owner-b")
    docx_bytes = b"owner mismatch docx bytes"
    snapshot = _snapshot(owner_key, docx_bytes)
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=other_owner, final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_OWNER_MISMATCH


def test_final_docx_bytes_not_found() -> None:
    class _MetadataOnlyRepo(InMemoryFinalDocxSnapshotRepository):
        def retrieve_final_docx_bytes(self, final_docx_sha256: str) -> bytes | None:
            return None

    repo = _MetadataOnlyRepo()
    owner_key = _owner()
    docx_bytes = b"bytes missing docx content"
    snapshot = _snapshot(owner_key, docx_bytes)
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=owner_key, final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_BYTES_NOT_FOUND


def test_final_docx_hash_mismatch() -> None:
    class _TamperedBytesRepo(InMemoryFinalDocxSnapshotRepository):
        def retrieve_final_docx_bytes(self, final_docx_sha256: str) -> bytes | None:
            return b"these bytes do not hash to the requested locator"

    repo = _TamperedBytesRepo()
    owner_key = _owner()
    docx_bytes = b"original docx bytes"
    snapshot = _snapshot(owner_key, docx_bytes)
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=owner_key, final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_HASH_MISMATCH


def test_storage_unavailable_on_metadata_read() -> None:
    class _ExplodingRepo(InMemoryFinalDocxSnapshotRepository):
        def get_final_docx_snapshot(self, final_snapshot_fingerprint: str):
            raise CvDocumentStorageError.of(
                __import__("app.schemas.cv_document_storage", fromlist=["CvDocumentStorageErrorCode"]).CvDocumentStorageErrorCode.STORAGE_UNAVAILABLE,
                "simulated storage failure",
            )

    reader = FinalDocxSnapshotPdfSourceReader(_ExplodingRepo())
    result = reader.read_source(owner_key=_owner(), final_snapshot_fingerprint="a" * 64)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE


def test_storage_unavailable_on_bytes_read() -> None:
    class _ExplodingBytesRepo(InMemoryFinalDocxSnapshotRepository):
        def retrieve_final_docx_bytes(self, final_docx_sha256: str):
            from app.schemas.cv_document_storage import CvDocumentStorageErrorCode

            raise CvDocumentStorageError.of(CvDocumentStorageErrorCode.STORAGE_UNAVAILABLE, "simulated storage failure")

    repo = _ExplodingBytesRepo()
    owner_key = _owner()
    docx_bytes = b"docx bytes for exploding read"
    snapshot = _snapshot(owner_key, docx_bytes)
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    reader = FinalDocxSnapshotPdfSourceReader(repo)
    result = reader.read_source(owner_key=owner_key, final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    assert result.status == FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE


def test_reader_never_imports_sqlalchemy() -> None:
    import ast
    import inspect

    import app.services.cv_document_final_docx_snapshot_pdf_source_reader as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sqlalchemy")
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("sqlalchemy")
