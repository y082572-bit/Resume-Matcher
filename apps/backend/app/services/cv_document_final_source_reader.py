"""Explicit Provenance Stage P6-A Final DOCX Snapshot Domain Addendum: the
working-copy source-reader contract.

``FinalDocxSourceReader`` is the exact contract a future real
working-copy reader (P6-B) must satisfy. This stage ships no concrete
implementation -- no filesystem access, no path handling -- only the
closed Protocol and its result/capture models. See
``docs/explicit-provenance-stage-p6a-final-snapshot-addendum.md``.

The reader never returns bytes on their own: every ``SUCCESS`` result
carries exactly one ``FinalDocxSourceCapture``, which binds the raw bytes
to the exact owner, proposal artifact, proposal revision, and generated
baseline hash they were read against. A finalization builder is expected
to treat this capture as the *only* trusted source of that baseline --
never a caller-supplied argument -- and to independently re-verify the
binding before using it. A failed read never carries a capture and never
carries bytes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.truth_entity import SHA256_PATTERN


class WorkingCopyReadStatus(StrEnum):
    SUCCESS = "SUCCESS"
    WORKING_COPY_MISSING = "WORKING_COPY_MISSING"
    WORKING_COPY_UNREADABLE = "WORKING_COPY_UNREADABLE"
    WORKING_COPY_LOCKED = "WORKING_COPY_LOCKED"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"
    WORKING_COPY_TOO_LARGE = "WORKING_COPY_TOO_LARGE"
    WORKING_COPY_BINDING_MISMATCH = "WORKING_COPY_BINDING_MISMATCH"


class FinalDocxSourceCapture(BaseModel):
    """The exact source binding a reader must return alongside the raw
    working-copy bytes. Never carries a filesystem path, filename, or
    timestamp -- only content-derived fingerprints/hashes plus the
    immutable bytes themselves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_proposal_revision: int = Field(ge=1)
    generated_proposal_sha256: str = Field(pattern=SHA256_PATTERN)
    docx_bytes: bytes = Field(min_length=1)


class WorkingCopyReadResult(BaseModel):
    """The single return value of
    ``FinalDocxSourceReader.read_finalization_source``. Only ``SUCCESS``
    ever carries a capture; every failure status carries a matching
    ``error_code`` and a non-empty ``error_message``, and never carries a
    capture (and therefore never carries bytes)."""

    model_config = ConfigDict(extra="forbid")

    status: WorkingCopyReadStatus
    capture: FinalDocxSourceCapture | None = None
    error_code: WorkingCopyReadStatus | None = None
    error_message: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _status_consistency(self) -> "WorkingCopyReadResult":
        if self.status == WorkingCopyReadStatus.SUCCESS:
            if self.capture is None:
                raise ValueError("SUCCESS requires a capture")
            if self.error_code is not None:
                raise ValueError("SUCCESS must carry no error_code")
            if self.error_message is not None:
                raise ValueError("SUCCESS must carry no error_message")
        else:
            if self.capture is not None:
                raise ValueError(f"{self.status} must carry no capture")
            if self.error_code != self.status:
                raise ValueError("a failure result's error_code must match its status")
            if not self.error_message:
                raise ValueError(f"{self.status} requires a non-empty error_message")
        return self


@runtime_checkable
class FinalDocxSourceReader(Protocol):
    """The exact contract a future real working-copy reader must satisfy.
    This stage ships no concrete implementation."""

    def read_finalization_source(
        self,
        owner_key: JobArtifactOwnerKey,
        expected_proposal_artifact_fingerprint: str,
        expected_proposal_revision: int,
    ) -> WorkingCopyReadResult: ...
