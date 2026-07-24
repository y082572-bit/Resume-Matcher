"""Explicit Provenance Stage P6-A Final DOCX Snapshot Domain Addendum: the
``FinalDocxSnapshot`` repository Protocol and its in-memory
implementation.

``FinalDocxSnapshotRepository`` is deliberately a separate, additive
contract -- it never reuses, extends, or is implemented by
``CvDocumentArtifactRepository``. Metadata (``FinalDocxSnapshot``) is
always looked up by ``final_snapshot_fingerprint``; raw bytes are always
looked up by ``final_docx_sha256``. The two are independently
deduplicated: many snapshot metadata records (distinct owner/proposal
lineage) may point at the exact same bytes, and the repository never
picks an arbitrary lineage record when a caller only supplies a bytes
hash -- there is no such lookup at all.

Both ``final_docx_sha256`` and ``final_snapshot_fingerprint`` are always
independently recomputed at save time from the caller-supplied
``docx_bytes``/``snapshot`` -- never trusted at face value from the
``FinalDocxSnapshot`` the caller handed in.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot
from app.services.truth_fingerprint import canonical_json_bytes


FINAL_SNAPSHOT_FINGERPRINT_VERSION = "cv-document-final-snapshot-v1"


def compute_final_snapshot_fingerprint(
    *,
    owner_key_fingerprint: str,
    source_proposal_artifact_fingerprint: str,
    source_proposal_revision: int,
    generated_proposal_sha256: str,
    final_docx_sha256: str,
    finalization_policy_version: str,
    snapshot_schema_version: str,
) -> str:
    """Covers exactly the identity fields of a ``FinalDocxSnapshot`` --
    never ``edited_by_user`` (fully derived from the two hashes already
    included), never bytes, never a path/filename/locator/timestamp."""

    content = {
        "fingerprint_version": FINAL_SNAPSHOT_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "source_proposal_artifact_fingerprint": source_proposal_artifact_fingerprint,
            "source_proposal_revision": source_proposal_revision,
            "generated_proposal_sha256": generated_proposal_sha256,
            "final_docx_sha256": final_docx_sha256,
            "finalization_policy_version": finalization_policy_version,
            "snapshot_schema_version": snapshot_schema_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


class FinalSnapshotSaveStatus(StrEnum):
    SAVED = "SAVED"
    ALREADY_EXISTS_IDENTICAL = "ALREADY_EXISTS_IDENTICAL"
    SNAPSHOT_FINGERPRINT_MISMATCH = "SNAPSHOT_FINGERPRINT_MISMATCH"
    DOCX_HASH_MISMATCH = "DOCX_HASH_MISMATCH"
    SNAPSHOT_METADATA_CONFLICT = "SNAPSHOT_METADATA_CONFLICT"
    DOCX_BYTES_CONFLICT = "DOCX_BYTES_CONFLICT"
    # Explicit Provenance Stage P6-B1 SQL storage addendum: additive --
    # only ever returned by a real SQL-backed FinalDocxSnapshotRepository
    # (never by InMemoryFinalDocxSnapshotRepository above), and always in
    # place of the generic SNAPSHOT_METADATA_CONFLICT whenever the failure
    # cause is specifically the source proposal lineage or the shared SQL/
    # blob storage layer, never a bare CvDocumentStorageError/SQLAlchemyError.
    SOURCE_PROPOSAL_NOT_FOUND = "SOURCE_PROPOSAL_NOT_FOUND"
    SOURCE_PROPOSAL_OWNER_MISMATCH = "SOURCE_PROPOSAL_OWNER_MISMATCH"
    SOURCE_PROPOSAL_REVISION_MISMATCH = "SOURCE_PROPOSAL_REVISION_MISMATCH"
    SOURCE_PROPOSAL_HASH_MISMATCH = "SOURCE_PROPOSAL_HASH_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalSnapshotSaveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalSnapshotSaveStatus


@runtime_checkable
class FinalDocxSnapshotRepository(Protocol):
    """A separate, additive contract -- never implemented by, and never
    reusing state from, ``CvDocumentArtifactRepository``."""

    def save_final_docx_snapshot(
        self, snapshot: FinalDocxSnapshot, docx_bytes: bytes
    ) -> FinalSnapshotSaveResult: ...

    def get_final_docx_snapshot(self, final_snapshot_fingerprint: str) -> FinalDocxSnapshot | None: ...

    def retrieve_final_docx_bytes(self, final_docx_sha256: str) -> bytes | None: ...


class InMemoryFinalDocxSnapshotRepository:
    """Test-only in-memory repository for ``FinalDocxSnapshot``. Metadata
    is keyed by ``final_snapshot_fingerprint``; bytes are keyed by
    ``final_docx_sha256`` and deduplicated across lineages. Every read
    returns a fresh ``model_copy()`` (metadata) or freshly-recomputed
    identical ``bytes`` object -- never the internally stored reference --
    and every write copies its own defensive ``bytes(...)``/
    ``model_copy()`` of the input, so a caller mutating a ``bytearray``
    after the call, or a later ``model_copy(update=...)`` on the object it
    passed in, can never change repository state.
    """

    def __init__(self) -> None:
        self._metadata: dict[str, FinalDocxSnapshot] = {}
        self._bytes: dict[str, bytes] = {}

    def save_final_docx_snapshot(
        self, snapshot: FinalDocxSnapshot, docx_bytes: bytes
    ) -> FinalSnapshotSaveResult:
        exact_bytes = bytes(docx_bytes)

        recomputed_docx_sha256 = hashlib.sha256(exact_bytes).hexdigest()
        if recomputed_docx_sha256 != snapshot.final_docx_sha256:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.DOCX_HASH_MISMATCH)

        recomputed_fingerprint = compute_final_snapshot_fingerprint(
            owner_key_fingerprint=snapshot.owner_key.owner_key_fingerprint,
            source_proposal_artifact_fingerprint=snapshot.source_proposal_artifact_fingerprint,
            source_proposal_revision=snapshot.source_proposal_revision,
            generated_proposal_sha256=snapshot.generated_proposal_sha256,
            final_docx_sha256=snapshot.final_docx_sha256,
            finalization_policy_version=snapshot.finalization_policy_version,
            snapshot_schema_version=snapshot.snapshot_schema_version,
        )
        if recomputed_fingerprint != snapshot.final_snapshot_fingerprint:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SNAPSHOT_FINGERPRINT_MISMATCH)

        existing_bytes = self._bytes.get(snapshot.final_docx_sha256)
        if existing_bytes is not None and existing_bytes != exact_bytes:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.DOCX_BYTES_CONFLICT)

        existing_metadata = self._metadata.get(snapshot.final_snapshot_fingerprint)
        if existing_metadata is not None:
            if existing_metadata != snapshot:
                return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT)
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL)

        self._metadata[snapshot.final_snapshot_fingerprint] = snapshot.model_copy()
        self._bytes.setdefault(snapshot.final_docx_sha256, exact_bytes)
        return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SAVED)

    def get_final_docx_snapshot(self, final_snapshot_fingerprint: str) -> FinalDocxSnapshot | None:
        stored = self._metadata.get(final_snapshot_fingerprint)
        return stored.model_copy() if stored is not None else None

    def retrieve_final_docx_bytes(self, final_docx_sha256: str) -> bytes | None:
        return self._bytes.get(final_docx_sha256)
