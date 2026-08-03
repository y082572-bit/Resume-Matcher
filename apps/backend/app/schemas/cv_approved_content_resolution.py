"""Pydantic contracts for Explicit Provenance Stage P6-C1b-B: the Approved
Content Resolution composition root.

``ApprovedContentResolutionResult`` is the single, closed return value of
``cv_approved_content_resolution.resolve_current_approved_content_document_input``.
It never carries a full ``ApprovedContentPackage`` -- only the already
reconstructed, re-validated ``ApprovedContentDocumentInput`` plus the two
scalar observations (``observed_package_fingerprint``,
``observed_authority_slot_version``) taken from the authority read that
linearized the resolution. It never carries free-text diagnostics, raw
exception text, proposal text, or approval decision detail: a caller gets a
closed status and, on success only, a defensively copied document input --
nothing else.

This stage never implements a router, an HTTP endpoint, a dependency
provider, POST Proposal, Proposal DOCX generation, PRE-P4 -> P5.5 production
orchestration, Approved Content Package save/promotion, the frontend, the
working-copy flow, ``FinalDocxSnapshot``, or PDF generation -- see
``docs/explicit-provenance-stage-p6c1bb-approved-content-resolution.md`` for
the full scope boundary.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_artifact import ApprovedContentDocumentInput
from app.schemas.truth_entity import SHA256_PATTERN


class ApprovedContentResolutionStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STALE_AUTHORITY_OBSERVATION = "STALE_AUTHORITY_OBSERVATION"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    DOCUMENT_INPUT_FINGERPRINT_MISMATCH = "DOCUMENT_INPUT_FINGERPRINT_MISMATCH"
    DOCUMENT_INPUT_INVALID = "DOCUMENT_INPUT_INVALID"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class ApprovedContentResolutionResult(BaseModel):
    """The single return value of
    ``resolve_current_approved_content_document_input``. Only ``FOUND`` ever
    carries ``document_input``/``observed_package_fingerprint``/
    ``observed_authority_slot_version`` -- every other status carries none
    of them, never a partially reconstructed input and never the raw
    authority observation that led to the failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ApprovedContentResolutionStatus
    document_input: ApprovedContentDocumentInput | None = None
    observed_package_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    observed_authority_slot_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentResolutionResult":
        if self.status == ApprovedContentResolutionStatus.FOUND:
            if (
                self.document_input is None
                or self.observed_package_fingerprint is None
                or self.observed_authority_slot_version is None
            ):
                raise ValueError(
                    "FOUND requires document_input, observed_package_fingerprint, "
                    "and observed_authority_slot_version"
                )
        else:
            if (
                self.document_input is not None
                or self.observed_package_fingerprint is not None
                or self.observed_authority_slot_version is not None
            ):
                raise ValueError(
                    f"{self.status} must carry no document_input/observed_package_fingerprint/"
                    "observed_authority_slot_version"
                )
        return self
