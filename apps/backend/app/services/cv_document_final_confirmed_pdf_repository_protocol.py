"""Explicit Provenance Stage P6-B3b-A: the ``FinalConfirmedPdfRepository``
Protocol (storage + cross-line current authority) and its in-memory
(test-only) implementation.

Every method returns a closed, typed result -- never an ORM row, a raw
SQLAlchemy exception, or a blob filesystem path. ``FinalConfirmedPdfArtifact``
rows are immutable and content-addressed exactly like the rest of the P6-A/
P6-B document-storage chain: the first successfully committed artifact for a
given ``conversion_request_fingerprint`` is always canonical, and a losing
concurrent writer with different output bytes is always reported
``REQUEST_ALREADY_CANONICAL`` against that winner -- never silently
overwritten and never guessed at by the caller.
"""

from __future__ import annotations

import threading
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    FinalConfirmedPdfArtifact,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot


# -- read: current authority -----------------------------------------------------


class FinalConfirmedPdfAuthorityReadStatus(StrEnum):
    FOUND = "FOUND"
    ABSENT = "ABSENT"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfAuthorityReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfAuthorityReadStatus
    observation: ConfirmedPdfCurrentAuthorityObservation | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfAuthorityReadResult":
        if self.status == FinalConfirmedPdfAuthorityReadStatus.FOUND:
            if self.observation is None or not self.observation.exists:
                raise ValueError("FOUND requires an observation with exists=True")
        elif self.status == FinalConfirmedPdfAuthorityReadStatus.ABSENT:
            if self.observation is None or self.observation.exists:
                raise ValueError("ABSENT requires an observation with exists=False")
        else:
            if self.observation is not None:
                raise ValueError(f"{self.status} must carry no observation")
        return self


# -- read: single artifact by fingerprint -----------------------------------------


class FinalConfirmedPdfArtifactReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfArtifactReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfArtifactReadStatus
    artifact: FinalConfirmedPdfArtifact | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfArtifactReadResult":
        if self.status == FinalConfirmedPdfArtifactReadStatus.FOUND:
            if self.artifact is None:
                raise ValueError("FOUND requires an artifact")
        else:
            if self.artifact is not None:
                raise ValueError(f"{self.status} must carry no artifact")
        return self


# -- read: canonical artifact by conversion_request_fingerprint --------------------


class FinalConfirmedPdfCanonicalReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfCanonicalReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfCanonicalReadStatus
    artifact: FinalConfirmedPdfArtifact | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfCanonicalReadResult":
        if self.status == FinalConfirmedPdfCanonicalReadStatus.FOUND:
            if self.artifact is None:
                raise ValueError("FOUND requires an artifact")
        else:
            if self.artifact is not None:
                raise ValueError(f"{self.status} must carry no artifact")
        return self


# -- save ---------------------------------------------------------------------------


class FinalConfirmedPdfSaveStatus(StrEnum):
    SAVED = "SAVED"
    ALREADY_EXISTS_IDENTICAL = "ALREADY_EXISTS_IDENTICAL"
    REQUEST_ALREADY_CANONICAL = "REQUEST_ALREADY_CANONICAL"
    OWNER_NOT_FOUND = "OWNER_NOT_FOUND"
    SOURCE_FINAL_SNAPSHOT_NOT_FOUND = "SOURCE_FINAL_SNAPSHOT_NOT_FOUND"
    SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH = "SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH"
    SOURCE_FINAL_DOCX_HASH_MISMATCH = "SOURCE_FINAL_DOCX_HASH_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfSaveResult(BaseModel):
    """``SAVED``/``ALREADY_EXISTS_IDENTICAL``/``REQUEST_ALREADY_CANONICAL``
    all carry an ``artifact`` -- for the last two it is always the exact
    winning/existing row, never a guess built by the caller."""

    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfSaveStatus
    artifact: FinalConfirmedPdfArtifact | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfSaveResult":
        if self.status in (
            FinalConfirmedPdfSaveStatus.SAVED,
            FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL,
            FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL,
        ):
            if self.artifact is None:
                raise ValueError(f"{self.status} requires an artifact")
        else:
            if self.artifact is not None:
                raise ValueError(f"{self.status} must carry no artifact")
        return self


# -- retrieve pdf bytes by artifact --------------------------------------------------


class FinalConfirmedPdfRetrievalStatus(StrEnum):
    OK = "OK"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    BLOB_MISSING = "BLOB_MISSING"
    BLOB_HASH_MISMATCH = "BLOB_HASH_MISMATCH"
    BLOB_SIZE_MISMATCH = "BLOB_SIZE_MISMATCH"
    BLOB_MEDIA_TYPE_MISMATCH = "BLOB_MEDIA_TYPE_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfRetrievalStatus
    artifact: FinalConfirmedPdfArtifact | None = None
    pdf_bytes: bytes | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfRetrievalResult":
        if self.status == FinalConfirmedPdfRetrievalStatus.OK:
            if self.artifact is None or self.pdf_bytes is None:
                raise ValueError("OK requires artifact and pdf_bytes")
            if not self.pdf_bytes:
                raise ValueError("OK requires non-empty pdf_bytes")
        else:
            if self.artifact is not None or self.pdf_bytes is not None:
                raise ValueError(f"{self.status} must carry no artifact/pdf_bytes")
        return self


# -- authority CAS --------------------------------------------------------------------


class FinalConfirmedPdfAuthorityCasStatus(StrEnum):
    PROMOTED = "PROMOTED"
    STALE_REVISION = "STALE_REVISION"
    OWNER_NOT_FOUND = "OWNER_NOT_FOUND"
    TARGET_ARTIFACT_NOT_FOUND = "TARGET_ARTIFACT_NOT_FOUND"
    TARGET_ARTIFACT_OWNER_MISMATCH = "TARGET_ARTIFACT_OWNER_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalConfirmedPdfAuthorityCasResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FinalConfirmedPdfAuthorityCasStatus
    current_observation: ConfirmedPdfCurrentAuthorityObservation | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfAuthorityCasResult":
        if self.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED:
            if self.current_observation is None or not self.current_observation.exists:
                raise ValueError("PROMOTED requires an observation with exists=True")
            if self.current_observation.lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
                raise ValueError("PROMOTED requires lineage_kind=FINAL_DOCX_SNAPSHOT")
        elif self.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION:
            if self.current_observation is None:
                raise ValueError("STALE_REVISION requires a fresh current_observation")
        else:
            if self.current_observation is not None:
                raise ValueError(f"{self.status} must carry no current_observation")
        return self


# -- Protocol -------------------------------------------------------------------------


@runtime_checkable
class FinalConfirmedPdfRepository(Protocol):
    def read_current_authority(
        self, owner_key: JobArtifactOwnerKey
    ) -> FinalConfirmedPdfAuthorityReadResult: ...

    def read_artifact(self, artifact_fingerprint: str) -> FinalConfirmedPdfArtifactReadResult: ...

    def read_canonical_artifact_by_conversion_request_fingerprint(
        self, conversion_request_fingerprint: str
    ) -> FinalConfirmedPdfCanonicalReadResult: ...

    def retrieve_pdf_bytes_by_artifact(
        self, artifact_fingerprint: str
    ) -> FinalConfirmedPdfRetrievalResult: ...

    def save_artifact(
        self, artifact: FinalConfirmedPdfArtifact, pdf_bytes: bytes
    ) -> FinalConfirmedPdfSaveResult: ...

    def promote_current_authority(
        self,
        owner_key: JobArtifactOwnerKey,
        target_artifact_fingerprint: str,
        expected_observation: ConfirmedPdfCurrentAuthorityObservation,
    ) -> FinalConfirmedPdfAuthorityCasResult: ...


class InMemoryFinalConfirmedPdfRepository:
    """Test-only in-memory repository. Owners and source Final DOCX
    Snapshots must be registered explicitly (:meth:`register_owner`/
    :meth:`register_final_snapshot`) before ``save_artifact`` can succeed --
    this mirrors the SQL repository's own FK-backed validation without
    requiring a real database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: set[str] = set()
        self._snapshots: dict[str, FinalDocxSnapshot] = {}
        self._artifacts: dict[str, FinalConfirmedPdfArtifact] = {}
        self._pdf_bytes: dict[str, bytes] = {}
        self._by_request_fingerprint: dict[str, FinalConfirmedPdfArtifact] = {}
        self._authority: dict[str, ConfirmedPdfCurrentAuthorityObservation] = {}

    # -- test setup helpers (not part of the Protocol) -----------------------

    def register_owner(self, owner_key: JobArtifactOwnerKey) -> None:
        self._owners.add(owner_key.owner_key_fingerprint)

    def register_final_snapshot(self, snapshot: FinalDocxSnapshot) -> None:
        self._owners.add(snapshot.owner_key.owner_key_fingerprint)
        self._snapshots[snapshot.final_snapshot_fingerprint] = snapshot.model_copy()

    # -- reads -----------------------------------------------------------------

    def read_current_authority(
        self, owner_key: JobArtifactOwnerKey
    ) -> FinalConfirmedPdfAuthorityReadResult:
        observation = self._authority.get(owner_key.owner_key_fingerprint)
        if observation is None:
            return FinalConfirmedPdfAuthorityReadResult(
                status=FinalConfirmedPdfAuthorityReadStatus.ABSENT,
                observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
            )
        return FinalConfirmedPdfAuthorityReadResult(
            status=FinalConfirmedPdfAuthorityReadStatus.FOUND, observation=observation.model_copy()
        )

    def read_artifact(self, artifact_fingerprint: str) -> FinalConfirmedPdfArtifactReadResult:
        artifact = self._artifacts.get(artifact_fingerprint)
        if artifact is None:
            return FinalConfirmedPdfArtifactReadResult(status=FinalConfirmedPdfArtifactReadStatus.NOT_FOUND)
        return FinalConfirmedPdfArtifactReadResult(
            status=FinalConfirmedPdfArtifactReadStatus.FOUND, artifact=artifact.model_copy()
        )

    def read_canonical_artifact_by_conversion_request_fingerprint(
        self, conversion_request_fingerprint: str
    ) -> FinalConfirmedPdfCanonicalReadResult:
        artifact = self._by_request_fingerprint.get(conversion_request_fingerprint)
        if artifact is None:
            return FinalConfirmedPdfCanonicalReadResult(status=FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND)
        return FinalConfirmedPdfCanonicalReadResult(
            status=FinalConfirmedPdfCanonicalReadStatus.FOUND, artifact=artifact.model_copy()
        )

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint: str) -> FinalConfirmedPdfRetrievalResult:
        artifact = self._artifacts.get(artifact_fingerprint)
        if artifact is None:
            return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND)
        stored_bytes = self._pdf_bytes.get(artifact_fingerprint)
        if stored_bytes is None:
            return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.BLOB_MISSING)
        return FinalConfirmedPdfRetrievalResult(
            status=FinalConfirmedPdfRetrievalStatus.OK,
            artifact=artifact.model_copy(),
            pdf_bytes=bytes(stored_bytes),
        )

    # -- save --------------------------------------------------------------------

    def save_artifact(
        self, artifact: FinalConfirmedPdfArtifact, pdf_bytes: bytes
    ) -> FinalConfirmedPdfSaveResult:
        raw = bytes(pdf_bytes)
        with self._lock:
            if artifact.owner_key.owner_key_fingerprint not in self._owners:
                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND)

            snapshot = self._snapshots.get(artifact.source_final_snapshot_fingerprint)
            if snapshot is None:
                return FinalConfirmedPdfSaveResult(
                    status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND
                )
            if snapshot.owner_key.owner_key_fingerprint != artifact.owner_key.owner_key_fingerprint:
                return FinalConfirmedPdfSaveResult(
                    status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH
                )
            if snapshot.final_docx_sha256 != artifact.source_final_docx_sha256:
                return FinalConfirmedPdfSaveResult(
                    status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH
                )

            existing_by_fingerprint = self._artifacts.get(artifact.artifact_fingerprint)
            if existing_by_fingerprint is not None:
                return FinalConfirmedPdfSaveResult(
                    status=FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL,
                    artifact=existing_by_fingerprint.model_copy(),
                )

            existing_by_request = self._by_request_fingerprint.get(
                artifact.conversion_request_fingerprint
            )
            if existing_by_request is not None:
                return FinalConfirmedPdfSaveResult(
                    status=FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL,
                    artifact=existing_by_request.model_copy(),
                )

            stored = artifact.model_copy()
            self._artifacts[artifact.artifact_fingerprint] = stored
            self._by_request_fingerprint[artifact.conversion_request_fingerprint] = stored
            self._pdf_bytes[artifact.artifact_fingerprint] = raw
            return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.SAVED, artifact=stored.model_copy())

    # -- authority CAS -------------------------------------------------------------

    def promote_current_authority(
        self,
        owner_key: JobArtifactOwnerKey,
        target_artifact_fingerprint: str,
        expected_observation: ConfirmedPdfCurrentAuthorityObservation,
    ) -> FinalConfirmedPdfAuthorityCasResult:
        with self._lock:
            if owner_key.owner_key_fingerprint not in self._owners:
                return FinalConfirmedPdfAuthorityCasResult(
                    status=FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND
                )

            target = self._artifacts.get(target_artifact_fingerprint)
            if target is None:
                return FinalConfirmedPdfAuthorityCasResult(
                    status=FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_NOT_FOUND
                )
            if target.owner_key.owner_key_fingerprint != owner_key.owner_key_fingerprint:
                return FinalConfirmedPdfAuthorityCasResult(
                    status=FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_OWNER_MISMATCH
                )

            current = self._authority.get(owner_key.owner_key_fingerprint)
            current_observation = (
                current.model_copy() if current is not None else ConfirmedPdfCurrentAuthorityObservation(exists=False)
            )
            if current_observation != expected_observation:
                return FinalConfirmedPdfAuthorityCasResult(
                    status=FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION,
                    current_observation=current_observation,
                )

            next_slot_version = 1 if not current_observation.exists else current_observation.slot_version + 1
            promoted = ConfirmedPdfCurrentAuthorityObservation(
                exists=True,
                lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                current_artifact_fingerprint=target_artifact_fingerprint,
                slot_version=next_slot_version,
            )
            self._authority[owner_key.owner_key_fingerprint] = promoted
            return FinalConfirmedPdfAuthorityCasResult(
                status=FinalConfirmedPdfAuthorityCasStatus.PROMOTED, current_observation=promoted.model_copy()
            )
