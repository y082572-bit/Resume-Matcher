"""Explicit Provenance Stage P6-A: the document artifact repository
Protocol and its in-memory (test-only) implementation.

``CvDocumentArtifactRepository`` is the exact contract a future P6-B
filesystem repository must satisfy: two independent 0/1 CAS slots per
owner (current proposal, current PDF), content-addressed immutable
snapshot storage, and previous-current history preserved as
``superseded``. No filesystem path ever participates in identity -- only
``JobArtifactOwnerKey``/content-addressed locators.

``InMemoryCvDocumentArtifactRepository`` implements this contract with
plain dicts. It performs no fsync and no atomic rename (explicitly out of
scope for P6-A) but enforces the same CAS/atomicity guarantees a real
implementation must: a failed replace never mutates the current slot, and
the proposal slot and PDF slot are always independent.

The repository never stores or returns a caller's own object reference.
Every write (``replace_current_proposal``/``replace_current_pdf``/
``save_validated_snapshot``) takes its own defensive ``model_copy()`` of
the artifact before storing it, and normalizes byte-like input to plain
``bytes`` (never a ``bytearray``/``memoryview`` the caller could still
mutate in place). Every read (``get_current_proposal``/``get_current_pdf``/
``retrieve_snapshot``) returns a fresh ``model_copy()`` of the stored
artifact, never the internally stored instance -- so
``returned_object is not internally_stored_object`` always holds, and
neither a field assignment (blocked by the models' own ``frozen=True``) nor
a ``model_copy(update=...)`` performed by the caller on what it was handed
back can ever change repository state. CAS ordering is always: (1) compare
``expected_previous_revision`` against the current slot -- no mutation yet
if it does not match; (2) build the defensive copies of the incoming
artifact/bytes and, if a current artifact already exists, its
``superseded=True`` copy (via ``model_copy``, never an in-place mutation of
the previous artifact); (3) only then atomically swap the current slot and
append the superseded copy to history.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.cv_document_artifact import (
    ConfirmedCvPdfArtifact,
    CvDocxProposalArtifact,
    JobArtifactOwnerKey,
    ValidatedCvDocxSnapshot,
)
from typing import Protocol, runtime_checkable


class CasReplaceStatus(StrEnum):
    REPLACED = "REPLACED"
    STALE_REVISION = "STALE_REVISION"


class CvDocumentPdfSlotCutoverStatus(StrEnum):
    """Explicit Provenance Stage P6-B3b-A: the permanent terminal status
    ``replace_current_pdf`` reports once the legacy ``cv_document_pdf_slots``
    table has been frozen by the Final-Confirmed-PDF cutover -- never a
    transient condition, never mapped onto ``CasReplaceStatus.STALE_REVISION``,
    and never resolved by a retry."""

    LEGACY_CURRENT_PDF_CUTOVER_FROZEN = "LEGACY_CURRENT_PDF_CUTOVER_FROZEN"


class ProposalReplaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CasReplaceStatus
    current_artifact: CvDocxProposalArtifact | None = None


class PdfReplaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CasReplaceStatus | CvDocumentPdfSlotCutoverStatus
    current_artifact: ConfirmedCvPdfArtifact | None = None


class SnapshotSaveStatus(StrEnum):
    SAVED = "SAVED"
    ALREADY_EXISTS_IDENTICAL = "ALREADY_EXISTS_IDENTICAL"
    LOCATOR_CONTENT_MISMATCH = "LOCATOR_CONTENT_MISMATCH"


class SnapshotSaveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SnapshotSaveStatus


@runtime_checkable
class CvDocumentArtifactRepository(Protocol):
    """The exact contract a future P6-B filesystem repository must
    satisfy. P6-A ships only the in-memory implementation below."""

    def get_current_proposal(self, owner_key: JobArtifactOwnerKey) -> CvDocxProposalArtifact | None: ...

    def read_current_proposal_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None: ...

    def replace_current_proposal(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: CvDocxProposalArtifact,
        docx_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> ProposalReplaceResult: ...

    def get_current_pdf(self, owner_key: JobArtifactOwnerKey) -> ConfirmedCvPdfArtifact | None: ...

    def read_current_pdf_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None: ...

    def replace_current_pdf(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: ConfirmedCvPdfArtifact,
        pdf_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> PdfReplaceResult: ...

    def save_validated_snapshot(
        self, snapshot: ValidatedCvDocxSnapshot, docx_bytes: bytes
    ) -> SnapshotSaveResult: ...

    def retrieve_snapshot(
        self, exact_docx_sha256: str
    ) -> tuple[ValidatedCvDocxSnapshot, bytes] | None: ...

    def mark_proposal_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None: ...

    def mark_pdf_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None: ...


class InMemoryCvDocumentArtifactRepository:
    """Test-only in-memory repository. Not a filesystem implementation --
    see ``docs/explicit-provenance-stage-p6a.md``."""

    def __init__(self) -> None:
        self._current_proposal: dict[str, CvDocxProposalArtifact] = {}
        self._current_proposal_bytes: dict[str, bytes] = {}
        self._proposal_history: dict[str, list[CvDocxProposalArtifact]] = {}

        self._current_pdf: dict[str, ConfirmedCvPdfArtifact] = {}
        self._current_pdf_bytes: dict[str, bytes] = {}
        self._pdf_history: dict[str, list[ConfirmedCvPdfArtifact]] = {}

        self._snapshots: dict[str, tuple[ValidatedCvDocxSnapshot, bytes]] = {}

    # -- proposal slot ---------------------------------------------------

    def get_current_proposal(self, owner_key: JobArtifactOwnerKey) -> CvDocxProposalArtifact | None:
        current = self._current_proposal.get(owner_key.owner_key_fingerprint)
        return current.model_copy() if current is not None else None

    def read_current_proposal_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
        return self._current_proposal_bytes.get(owner_key.owner_key_fingerprint)

    def replace_current_proposal(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: CvDocxProposalArtifact,
        docx_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> ProposalReplaceResult:
        key = owner_key.owner_key_fingerprint
        current = self._current_proposal.get(key)
        current_revision = current.proposal_revision if current is not None else None
        if current_revision != expected_previous_revision:
            return ProposalReplaceResult(
                status=CasReplaceStatus.STALE_REVISION,
                current_artifact=current.model_copy() if current is not None else None,
            )

        stored_artifact = artifact.model_copy()
        stored_bytes = bytes(docx_bytes)
        superseded = current.model_copy(update={"superseded": True}) if current is not None else None

        if superseded is not None:
            self._proposal_history.setdefault(key, []).append(superseded)
        self._current_proposal[key] = stored_artifact
        self._current_proposal_bytes[key] = stored_bytes
        return ProposalReplaceResult(status=CasReplaceStatus.REPLACED, current_artifact=stored_artifact.model_copy())

    def mark_proposal_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None:
        key = owner_key.owner_key_fingerprint
        current = self._current_proposal.get(key)
        if current is not None and current.artifact_fingerprint == artifact_fingerprint:
            superseded = current.model_copy(update={"superseded": True})
            self._proposal_history.setdefault(key, []).append(superseded)
            del self._current_proposal[key]
            del self._current_proposal_bytes[key]

    # -- pdf slot ----------------------------------------------------------

    def get_current_pdf(self, owner_key: JobArtifactOwnerKey) -> ConfirmedCvPdfArtifact | None:
        current = self._current_pdf.get(owner_key.owner_key_fingerprint)
        return current.model_copy() if current is not None else None

    def read_current_pdf_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
        return self._current_pdf_bytes.get(owner_key.owner_key_fingerprint)

    def replace_current_pdf(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: ConfirmedCvPdfArtifact,
        pdf_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> PdfReplaceResult:
        key = owner_key.owner_key_fingerprint
        current = self._current_pdf.get(key)
        current_revision = current.pdf_revision if current is not None else None
        if current_revision != expected_previous_revision:
            return PdfReplaceResult(
                status=CasReplaceStatus.STALE_REVISION,
                current_artifact=current.model_copy() if current is not None else None,
            )

        stored_artifact = artifact.model_copy()
        stored_bytes = bytes(pdf_bytes)
        superseded = current.model_copy(update={"superseded": True}) if current is not None else None

        if superseded is not None:
            self._pdf_history.setdefault(key, []).append(superseded)
        self._current_pdf[key] = stored_artifact
        self._current_pdf_bytes[key] = stored_bytes
        return PdfReplaceResult(status=CasReplaceStatus.REPLACED, current_artifact=stored_artifact.model_copy())

    def mark_pdf_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None:
        key = owner_key.owner_key_fingerprint
        current = self._current_pdf.get(key)
        if current is not None and current.artifact_fingerprint == artifact_fingerprint:
            superseded = current.model_copy(update={"superseded": True})
            self._pdf_history.setdefault(key, []).append(superseded)
            del self._current_pdf[key]
            del self._current_pdf_bytes[key]

    # -- content-addressed snapshot storage ---------------------------------

    def save_validated_snapshot(
        self, snapshot: ValidatedCvDocxSnapshot, docx_bytes: bytes
    ) -> SnapshotSaveResult:
        if hashlib.sha256(docx_bytes).hexdigest() != snapshot.exact_docx_sha256:
            return SnapshotSaveResult(status=SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH)

        key = snapshot.exact_docx_sha256
        existing = self._snapshots.get(key)
        if existing is not None:
            _, existing_bytes = existing
            if existing_bytes != docx_bytes:
                return SnapshotSaveResult(status=SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH)
            return SnapshotSaveResult(status=SnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL)

        self._snapshots[key] = (snapshot.model_copy(), bytes(docx_bytes))
        return SnapshotSaveResult(status=SnapshotSaveStatus.SAVED)

    def retrieve_snapshot(self, exact_docx_sha256: str) -> tuple[ValidatedCvDocxSnapshot, bytes] | None:
        stored = self._snapshots.get(exact_docx_sha256)
        if stored is None:
            return None
        snapshot, docx_bytes = stored
        return snapshot.model_copy(), docx_bytes
