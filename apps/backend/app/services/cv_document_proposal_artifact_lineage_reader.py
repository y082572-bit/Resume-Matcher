"""Explicit Provenance Stage P6-B2b-B: the owner-bound Proposal artifact
lineage reader contract.

``ProposalArtifactLineageReader`` is the exact contract a Working Copy
finalization reader uses to re-verify a historical Proposal DOCX artifact's
lineage before it is ever trusted as the source of a
``FinalDocxSourceCapture``. ``binding.json`` is cache and a lineage *hint*
only (see ``cv_document_working_copy_store.py``); this Protocol's job is to
independently re-derive the *only* authority for that lineage -- the
Proposal artifact row and its immutable blob -- and to fail closed on any
owner, revision, generated-hash, or exact-bytes mismatch.

``INVALID_REQUEST`` is a deliberate, implementation-mandated correction: a
malformed argument (a non-hex ``artifact_fingerprint``, a ``proposal_revision``
below 1) must never reach a raw ``ValueError``/Pydantic ``ValidationError``/
``TypeError`` -- ``validate_lineage_request`` below is the single place
every ``ProposalArtifactLineageReader`` implementation performs this check,
so ``read_verified_proposal_artifact_lineage`` can return a closed
``INVALID_REQUEST`` result instead of ever raising on bad input.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.truth_entity import SHA256_PATTERN


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_valid_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX_RE.match(value))


def validate_lineage_request(
    *,
    artifact_fingerprint: object,
    expected_owner_key_fingerprint: object,
    expected_proposal_revision: object,
    expected_generated_docx_sha256: object,
) -> bool:
    """``True`` only if every argument is well-formed enough to attempt a
    lookup -- never raises. The single shared precondition check every
    ``ProposalArtifactLineageReader`` implementation runs before touching
    any SQL row or blob."""

    if not _is_valid_sha256_hex(artifact_fingerprint):
        return False
    if not _is_valid_sha256_hex(expected_owner_key_fingerprint):
        return False
    if not _is_valid_sha256_hex(expected_generated_docx_sha256):
        return False
    if isinstance(expected_proposal_revision, bool):
        return False
    if not isinstance(expected_proposal_revision, int):
        return False
    if expected_proposal_revision < 1:
        return False
    return True


class ProposalArtifactLineageStatus(StrEnum):
    VERIFIED = "VERIFIED"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    GENERATED_HASH_MISMATCH = "GENERATED_HASH_MISMATCH"
    BYTES_HASH_MISMATCH = "BYTES_HASH_MISMATCH"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class ProposalArtifactLineage(BaseModel):
    """The exact, owner-bound Proposal lineage a caller may trust --
    metadata always sourced from the authoritative SQL row, bytes always
    sourced from the blob store and independently re-hashed against
    ``generated_docx_content_hash`` before this model is ever constructed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_revision: int = Field(ge=1)
    generated_docx_content_hash: str = Field(pattern=SHA256_PATTERN)
    docx_bytes: bytes = Field(min_length=1)


class ProposalArtifactLineageReadResult(BaseModel):
    """The single return value of
    ``read_verified_proposal_artifact_lineage``. Only ``VERIFIED`` ever
    carries a ``lineage``; every failure status -- including
    ``INVALID_REQUEST`` -- carries none."""

    model_config = ConfigDict(extra="forbid")

    status: ProposalArtifactLineageStatus
    lineage: ProposalArtifactLineage | None = None
    error_message: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ProposalArtifactLineageReadResult":
        if self.status == ProposalArtifactLineageStatus.VERIFIED:
            if self.lineage is None:
                raise ValueError("VERIFIED requires lineage")
        else:
            if self.lineage is not None:
                raise ValueError(f"{self.status} must carry no lineage")
        return self


@runtime_checkable
class ProposalArtifactLineageReader(Protocol):
    """The exact contract a Working Copy finalization reader uses to
    re-verify a historical Proposal artifact's lineage. A conforming
    implementation's public method never raises ``ValueError``, Pydantic
    ``ValidationError``, or a ``TypeError`` stemming from bad argument
    values -- a malformed argument always yields ``INVALID_REQUEST``
    instead."""

    def read_verified_proposal_artifact_lineage(
        self,
        *,
        artifact_fingerprint: str,
        expected_owner_key_fingerprint: str,
        expected_proposal_revision: int,
        expected_generated_docx_sha256: str,
    ) -> ProposalArtifactLineageReadResult: ...
