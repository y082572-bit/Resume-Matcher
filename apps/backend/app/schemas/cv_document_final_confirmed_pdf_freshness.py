"""Explicit Provenance Stage P6-B3b-B: the closed result contract for
informational Final Confirmed PDF freshness evaluation.

Freshness is purely informational: it never mutates current authority,
never deletes a PDF, never blocks access, never runs the converter, and
never performs semantic/Truth Library validation. It only compares the
current-authority-pointed ``FinalConfirmedPdfArtifact`` against a caller-
supplied, independently-derived current request context.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    FinalConfirmedPdfArtifact,
)


class FinalConfirmedPdfFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE_SOURCE_SNAPSHOT = "STALE_SOURCE_SNAPSHOT"
    STALE_RUNTIME_IDENTITY = "STALE_RUNTIME_IDENTITY"
    STALE_CONVERSION_POLICY = "STALE_CONVERSION_POLICY"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


#: FRESH and every STALE_* status always carry the canonical artifact and
#: the current-authority observation it was compared against.
_PAYLOAD_STATUSES: frozenset[FinalConfirmedPdfFreshnessStatus] = frozenset(
    {
        FinalConfirmedPdfFreshnessStatus.FRESH,
        FinalConfirmedPdfFreshnessStatus.STALE_SOURCE_SNAPSHOT,
        FinalConfirmedPdfFreshnessStatus.STALE_RUNTIME_IDENTITY,
        FinalConfirmedPdfFreshnessStatus.STALE_CONVERSION_POLICY,
    }
)


class FinalConfirmedPdfFreshnessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: FinalConfirmedPdfFreshnessStatus
    artifact: FinalConfirmedPdfArtifact | None = None
    current_authority_observation: ConfirmedPdfCurrentAuthorityObservation | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalConfirmedPdfFreshnessResult":
        if self.status in _PAYLOAD_STATUSES:
            if self.artifact is None or self.current_authority_observation is None:
                raise ValueError(f"{self.status} requires artifact and current_authority_observation")
        else:
            if self.artifact is not None or self.current_authority_observation is not None:
                raise ValueError(f"{self.status} must carry no artifact/current_authority_observation")
        return self
