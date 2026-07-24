"""Explicit Provenance Stage P6-A Final DOCX Snapshot Domain Addendum:
closed, frozen Pydantic contracts for ``FinalDocxSnapshot``.

``FinalDocxSnapshot`` is an exact, immutable, content-addressed capture of
the DOCX *working copy* at the moment PDF generation was requested. It is
strictly additive to Stage P6-A -- it never replaces, modifies, or is
built from ``ValidatedCvDocxSnapshot``. See
``docs/explicit-provenance-stage-p6a-final-snapshot-addendum.md`` for the
full rationale.

``FinalDocxSnapshot`` carries no structural validation result, no
semantic validation, no Truth Library compliance check, and no manual
confirmation decision. The user is solely responsible for any edits made
in Word between proposal generation and PDF finalization;
``edited_by_user`` is report metadata only -- a plain, independently
re-derivable boolean (``generated_proposal_sha256 != final_docx_sha256``)
-- never a gate that blocks finalization and never itself a form of
approval.

Identity is always expressed through content-derived SHA-256
fingerprints, exactly as in the rest of Stage P6-A -- never a filesystem
path, filename, locator, or timestamp.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.truth_entity import SHA256_PATTERN


FINAL_SNAPSHOT_SCHEMA_VERSION = "cv-document-final-snapshot-schema-v1"


# -- Final DOCX snapshot ----------------------------------------------------------


class FinalDocxSnapshot(BaseModel):
    """An exact, immutable capture of the DOCX working copy at the moment
    PDF generation was requested.

    Does not imply structural validation, semantic validation, Truth
    Library compliance, or any manual confirmation of the user's edits --
    the user takes sole responsibility for changes made in Word.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key: JobArtifactOwnerKey
    source_proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_proposal_revision: int = Field(ge=1)
    generated_proposal_sha256: str = Field(pattern=SHA256_PATTERN)
    final_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    edited_by_user: bool
    finalization_policy_version: str = Field(min_length=1, max_length=64)
    snapshot_schema_version: Literal["cv-document-final-snapshot-schema-v1"] = (
        FINAL_SNAPSHOT_SCHEMA_VERSION
    )
    final_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _edited_by_user_matches_hashes(self) -> "FinalDocxSnapshot":
        expected = self.generated_proposal_sha256 != self.final_docx_sha256
        if self.edited_by_user != expected:
            raise ValueError(
                "edited_by_user must equal (generated_proposal_sha256 != final_docx_sha256)"
            )
        return self


# -- Finalization build result -----------------------------------------------------


class FinalDocxSnapshotBuildStatus(StrEnum):
    FINALIZED = "FINALIZED"
    WORKING_COPY_MISSING = "WORKING_COPY_MISSING"
    WORKING_COPY_UNREADABLE = "WORKING_COPY_UNREADABLE"
    WORKING_COPY_LOCKED = "WORKING_COPY_LOCKED"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"
    WORKING_COPY_TOO_LARGE = "WORKING_COPY_TOO_LARGE"
    WORKING_COPY_BINDING_MISMATCH = "WORKING_COPY_BINDING_MISMATCH"
    FINAL_DOCX_HASH_MISMATCH = "FINAL_DOCX_HASH_MISMATCH"
    FINAL_SNAPSHOT_FINGERPRINT_MISMATCH = "FINAL_SNAPSHOT_FINGERPRINT_MISMATCH"
    FINAL_SNAPSHOT_PERSISTENCE_FAILED = "FINAL_SNAPSHOT_PERSISTENCE_FAILED"


class FinalDocxSnapshotBuildResult(BaseModel):
    """The single return value of ``finalize_current_docx_for_pdf``. Only
    ``FINALIZED`` ever carries a snapshot; every other status is a closed,
    fail-closed technical outcome -- never a raw ``OSError`` or raw
    repository exception."""

    model_config = ConfigDict(extra="forbid")

    status: FinalDocxSnapshotBuildStatus
    snapshot: FinalDocxSnapshot | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalDocxSnapshotBuildResult":
        if self.status == FinalDocxSnapshotBuildStatus.FINALIZED:
            if self.snapshot is None:
                raise ValueError("FINALIZED requires a snapshot")
        else:
            if self.snapshot is not None:
                raise ValueError(f"{self.status} must carry no snapshot")
        return self


# -- Replay / freshness -------------------------------------------------------------


class FinalDocxSnapshotReplayInput(BaseModel):
    """Everything a fresh ``FinalDocxSnapshot`` replay must be able to
    re-derive and compare -- never a path, filename, or timestamp, none of
    which this model even accepts a field for."""

    model_config = ConfigDict(extra="forbid")

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_proposal_revision: int = Field(ge=1)
    generated_proposal_sha256: str = Field(pattern=SHA256_PATTERN)
    final_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    finalization_policy_version: str = Field(min_length=1, max_length=64)
    snapshot_schema_version: str = Field(min_length=1, max_length=64)
