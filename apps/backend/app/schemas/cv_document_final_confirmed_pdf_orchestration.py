"""Explicit Provenance Stage P6-B3b-B: the closed result contract for the
``generate_final_confirmed_pdf`` composition root.

``FinalConfirmedPdfGenerationStatus`` is the single closed set of outcomes
the orchestrator may report -- it never distinguishes a repository-internal
race outcome (``ALREADY_EXISTS_IDENTICAL``/``REQUEST_ALREADY_CANONICAL``)
from a client's point of view; those always collapse into
``REPLAYED_AND_PROMOTED``/``ALREADY_CURRENT``/``CURRENT_PDF_SLOT_CONFLICT``
depending on the CAS outcome. ``FinalConfirmedPdfGenerationResult`` is
``frozen=True``/``extra="forbid"`` and its own ``model_validator`` enforces
every payload invariant -- never left to caller discipline.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    FinalConfirmedPdfArtifact,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot


class FinalConfirmedPdfGenerationStatus(StrEnum):
    """The closed set of outcomes ``generate_final_confirmed_pdf`` may
    return. Never extended ad hoc -- a new outcome is always an explicit,
    separately reviewed addition here."""

    # -- success / partial success ------------------------------------------
    CONFIRMED_PDF_CREATED = "CONFIRMED_PDF_CREATED"
    REPLAYED_AND_PROMOTED = "REPLAYED_AND_PROMOTED"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    CURRENT_PDF_SLOT_CONFLICT = "CURRENT_PDF_SLOT_CONFLICT"

    # -- source ---------------------------------------------------------------
    OWNER_KEY_FINGERPRINT_MISMATCH = "OWNER_KEY_FINGERPRINT_MISMATCH"
    FINAL_SNAPSHOT_NOT_FOUND = "FINAL_SNAPSHOT_NOT_FOUND"
    FINAL_SNAPSHOT_OWNER_MISMATCH = "FINAL_SNAPSHOT_OWNER_MISMATCH"
    FINAL_DOCX_BYTES_NOT_FOUND = "FINAL_DOCX_BYTES_NOT_FOUND"
    FINAL_DOCX_HASH_MISMATCH = "FINAL_DOCX_HASH_MISMATCH"

    # -- converter --------------------------------------------------------------
    CONVERTER_UNAVAILABLE = "CONVERTER_UNAVAILABLE"
    CONVERTER_CONTRACT_VIOLATION = "CONVERTER_CONTRACT_VIOLATION"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    PROCESS_OUTPUT_LIMIT_EXCEEDED = "PROCESS_OUTPUT_LIMIT_EXCEEDED"
    CONVERSION_TIMEOUT = "CONVERSION_TIMEOUT"
    PROCESS_TERMINATION_FAILED = "PROCESS_TERMINATION_FAILED"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    PDF_OUTPUT_MISSING = "PDF_OUTPUT_MISSING"
    PDF_OUTPUT_LOCKED = "PDF_OUTPUT_LOCKED"
    PDF_OUTPUT_UNREADABLE = "PDF_OUTPUT_UNREADABLE"
    PDF_OUTPUT_CHANGED_DURING_READ = "PDF_OUTPUT_CHANGED_DURING_READ"
    PDF_OUTPUT_TOO_LARGE = "PDF_OUTPUT_TOO_LARGE"
    PDF_OUTPUT_INVALID = "PDF_OUTPUT_INVALID"
    WORKSPACE_SETUP_FAILED = "WORKSPACE_SETUP_FAILED"
    WORKSPACE_CLEANUP_FAILED = "WORKSPACE_CLEANUP_FAILED"

    # -- pdf retrieval ------------------------------------------------------------
    PDF_ARTIFACT_NOT_FOUND = "PDF_ARTIFACT_NOT_FOUND"
    PDF_BLOB_MISSING = "PDF_BLOB_MISSING"
    PDF_BLOB_HASH_MISMATCH = "PDF_BLOB_HASH_MISMATCH"
    PDF_BLOB_SIZE_MISMATCH = "PDF_BLOB_SIZE_MISMATCH"
    PDF_BLOB_MEDIA_TYPE_MISMATCH = "PDF_BLOB_MEDIA_TYPE_MISMATCH"

    # -- save lineage ---------------------------------------------------------------
    SOURCE_FINAL_SNAPSHOT_NOT_FOUND = "SOURCE_FINAL_SNAPSHOT_NOT_FOUND"
    SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH = "SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH"
    SOURCE_FINAL_DOCX_HASH_MISMATCH = "SOURCE_FINAL_DOCX_HASH_MISMATCH"

    # -- owner ----------------------------------------------------------------------
    OWNER_NOT_FOUND = "OWNER_NOT_FOUND"

    # -- storage --------------------------------------------------------------------
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


#: Exactly the four statuses that always carry the full success payload.
_PAYLOAD_STATUSES: frozenset[FinalConfirmedPdfGenerationStatus] = frozenset(
    {
        FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
        FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED,
        FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT,
        FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT,
    }
)

_PROMOTING_STATUSES: frozenset[FinalConfirmedPdfGenerationStatus] = frozenset(
    {
        FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
        FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED,
    }
)


class FinalConfirmedPdfGenerationResult(BaseModel):
    """The single, closed return value of ``generate_final_confirmed_pdf``.

    Only the four statuses in ``_PAYLOAD_STATUSES`` ever carry
    ``artifact``/``pdf_bytes``/``source_final_snapshot``/
    ``final_authority_observation`` -- every other status forbids all
    four. The boolean fields (``replay_used``/``canonical_artifact_reused``/
    ``conversion_performed``/``current_authority_promoted``) are never
    restricted to the payload statuses -- they describe what the
    orchestrator actually did on *this* call, including on a failure path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FinalConfirmedPdfGenerationStatus
    artifact: FinalConfirmedPdfArtifact | None = None
    pdf_bytes: bytes | None = None
    source_final_snapshot: FinalDocxSnapshot | None = None
    replay_used: bool = False
    canonical_artifact_reused: bool = False
    conversion_performed: bool = False
    current_authority_promoted: bool = False
    final_authority_observation: ConfirmedPdfCurrentAuthorityObservation | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfGenerationResult":
        if self.status in _PAYLOAD_STATUSES:
            if self.artifact is None:
                raise ValueError(f"{self.status} requires artifact")
            if not self.pdf_bytes:
                raise ValueError(f"{self.status} requires non-empty pdf_bytes")
            if self.source_final_snapshot is None:
                raise ValueError(f"{self.status} requires source_final_snapshot")
            if self.final_authority_observation is None:
                raise ValueError(f"{self.status} requires final_authority_observation")
        else:
            if self.artifact is not None:
                raise ValueError(f"{self.status} must carry no artifact")
            if self.pdf_bytes is not None:
                raise ValueError(f"{self.status} must carry no pdf_bytes")
            if self.source_final_snapshot is not None:
                raise ValueError(f"{self.status} must carry no source_final_snapshot")
            if self.final_authority_observation is not None:
                raise ValueError(f"{self.status} must carry no final_authority_observation")
            return self

        observation = self.final_authority_observation
        assert observation is not None
        assert self.artifact is not None

        if self.status == FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT:
            if self.current_authority_promoted:
                raise ValueError("CURRENT_PDF_SLOT_CONFLICT requires current_authority_promoted=False")
            if (
                observation.exists
                and observation.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT
                and observation.current_artifact_fingerprint == self.artifact.artifact_fingerprint
            ):
                raise ValueError(
                    "CURRENT_PDF_SLOT_CONFLICT observation must not already point at the target artifact"
                )

        if self.status == FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT:
            if self.current_authority_promoted:
                raise ValueError("ALREADY_CURRENT requires current_authority_promoted=False")
            if not observation.exists:
                raise ValueError("ALREADY_CURRENT requires an existing final_authority_observation")
            if observation.lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
                raise ValueError("ALREADY_CURRENT requires lineage_kind=FINAL_DOCX_SNAPSHOT")
            if observation.current_artifact_fingerprint != self.artifact.artifact_fingerprint:
                raise ValueError("ALREADY_CURRENT observation must point at the target artifact")

        if self.status in _PROMOTING_STATUSES:
            if not self.current_authority_promoted:
                raise ValueError(f"{self.status} requires current_authority_promoted=True")
            if not observation.exists or observation.lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
                raise ValueError(f"{self.status} final_authority_observation must be a FINAL_DOCX_SNAPSHOT authority")
            if observation.current_artifact_fingerprint != self.artifact.artifact_fingerprint:
                raise ValueError(f"{self.status} final_authority_observation must point at the target artifact")

        return self
