"""Pydantic contracts for Explicit Provenance Stage P6-C1b-A: the Approved
Content Package foundation.

An ``ApprovedContentPackage`` is a flat, storable snapshot of one already
``VALID``-checked ``ApprovedContentDocumentInput`` -- built once by
``cv_approved_content_package_builder.build_approved_content_package`` and
persisted verbatim thereafter. It never embeds a nested
``ApprovedContentDocumentInput`` object (see ``ApprovedContentPackagePayload``
below): every field the input envelope carried is flattened directly onto
the payload instead, so a caller can never smuggle in a second, divergent
document-input identity alongside the package's own.

Every model here is a closed, replayable Pydantic value object. Identity is
always expressed through content-derived SHA-256 fingerprints -- never a
random UUID, a filesystem path, or an implicit ``datetime.now()``.

This stage never implements the ``ApprovedContentDocumentInput`` resolver,
the POST Proposal API, router changes, the frontend, PRE-P4 -> P5.5
production orchestration, Proposal DOCX generation, the working-copy flow,
``FinalDocxSnapshot``, the PDF converter, or final PDF authority -- see
``docs/explicit-provenance-stage-p6c1ba-approved-content-package-foundation.md``
for the full scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_content_approval import (
    ApprovedCvContentReplayInput,
    ApprovedCvContentResult,
    ProposalApprovalDecision,
    ProposalSemanticValidationReplayInput,
)
from app.schemas.cv_content_plan import CvContentPlan
from app.schemas.cv_document_artifact import ArtifactOwnerKind, DocumentInputViolation
from app.schemas.truth_entity import SHA256_PATTERN


APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION = "cv-approved-content-package-fingerprint-v1"
APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION = "cv-approved-content-package-schema-v1"
APPROVED_CONTENT_PACKAGE_POLICY_VERSION = "cv-approved-content-package-policy-v1"
APPROVAL_DECISIONS_FINGERPRINT_VERSION = "cv-approved-content-package-approval-decisions-v1"
FACT_SELECTION_IDENTITY_FINGERPRINT_VERSION = "cv-approved-content-package-fact-selection-v1"
PAYLOAD_LIMIT_BYTES = 1_048_576


# -- payload ------------------------------------------------------------------


class ApprovedContentPackagePayload(BaseModel):
    """The flat, canonical-JSON-serializable content of one package.

    Deliberately never carries a nested ``ApprovedContentDocumentInput`` --
    every field the envelope held is flattened directly here instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    approved_content_result: ApprovedCvContentResult
    source_content_plan: CvContentPlan
    current_p55_replay_input: ApprovedCvContentReplayInput
    semantic_validation_replay_input: ProposalSemanticValidationReplayInput
    approval_decisions: tuple[ProposalApprovalDecision, ...] = Field(default_factory=tuple)
    owner_kind: ArtifactOwnerKind
    document_input_schema_version: Literal["cv-document-input-schema-v1"] = (
        "cv-document-input-schema-v1"
    )
    document_input_policy_version: Literal["cv-document-input-policy-v1"] = (
        "cv-document-input-policy-v1"
    )


# -- fingerprint input ----------------------------------------------------------


class ApprovedContentPackageFingerprintInput(BaseModel):
    """The exact, deterministically reconstructable content that hashes into
    ``ApprovedContentPackage.package_fingerprint``.

    Never persisted separately from the package it identifies -- a reader
    always rebuilds an equivalent instance from ``package`` + ``payload``
    before recomputing/re-verifying ``package_fingerprint``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_kind: ArtifactOwnerKind
    document_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approved_content_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    semantic_validation_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    approval_decisions_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_identity_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_strategy_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    strategy_selection_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    strategy_ranking_input_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    strategy_integration_policy_version: str | None = Field(default=None, max_length=64)
    p5a_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    p5b_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    payload_sha256: str = Field(pattern=SHA256_PATTERN)
    document_input_schema_version: Literal["cv-document-input-schema-v1"] = (
        "cv-document-input-schema-v1"
    )
    document_input_policy_version: Literal["cv-document-input-policy-v1"] = (
        "cv-document-input-policy-v1"
    )
    package_fingerprint_schema_version: Literal["cv-approved-content-package-fingerprint-v1"] = (
        APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION
    )
    package_schema_version: str = Field(min_length=1, max_length=64)
    package_policy_version: str = Field(min_length=1, max_length=64)


# -- package --------------------------------------------------------------------


class ApprovedContentPackage(BaseModel):
    """One immutable, content-addressed Approved Content Package.

    Never stores a caller-supplied ``ApprovedContentPackageFingerprintInput``
    -- it is always deterministically reconstructable from this package's own
    stored fields plus ``payload``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    package_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_kind: ArtifactOwnerKind
    document_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approval_decisions_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_identity_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_sha256: str = Field(pattern=SHA256_PATTERN)
    payload_byte_size: int = Field(gt=0, le=PAYLOAD_LIMIT_BYTES)
    payload: ApprovedContentPackagePayload
    package_fingerprint_schema_version: Literal["cv-approved-content-package-fingerprint-v1"] = (
        APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION
    )
    package_schema_version: str = Field(min_length=1, max_length=64)
    package_policy_version: str = Field(min_length=1, max_length=64)
    created_at: str = Field(min_length=1)


# -- status enums ---------------------------------------------------------------


class ApprovedContentPackageBuildStatus(StrEnum):
    BUILT = "BUILT"
    DOCUMENT_INPUT_INVALID = "DOCUMENT_INPUT_INVALID"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    TERMINAL_DISPOSITION_SET_VIOLATION = "TERMINAL_DISPOSITION_SET_VIOLATION"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"


class ApprovedContentPackageSaveStatus(StrEnum):
    SAVED = "SAVED"
    ALREADY_EXISTS_IDENTICAL = "ALREADY_EXISTS_IDENTICAL"
    OWNER_NOT_FOUND = "OWNER_NOT_FOUND"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    PAYLOAD_NOT_CANONICAL = "PAYLOAD_NOT_CANONICAL"
    PAYLOAD_FINGERPRINT_MISMATCH = "PAYLOAD_FINGERPRINT_MISMATCH"
    PACKAGE_FINGERPRINT_MISMATCH = "PACKAGE_FINGERPRINT_MISMATCH"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class ApprovedContentPackageReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class ApprovedContentAuthorityReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class ApprovedContentAuthorityPromotionStatus(StrEnum):
    PROMOTED = "PROMOTED"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
    OWNER_PACKAGE_MISMATCH = "OWNER_PACKAGE_MISMATCH"
    STALE_REVISION = "STALE_REVISION"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


# -- result models ----------------------------------------------------------------


class ApprovedContentPackageBuildResult(BaseModel):
    """The single return value of ``build_approved_content_package``. Only
    ``BUILT`` ever carries a ``package``. ``input_violations`` is populated
    only when ``validate_approved_content_document_input`` itself rejected
    the envelope; a builder-internal integrity failure (a decision/fact
    identity that fails re-verification) is instead reported through
    ``diagnostics``, since it is never one of the closed
    ``DocumentInputViolationCode`` values."""

    model_config = ConfigDict(extra="forbid")

    status: ApprovedContentPackageBuildStatus
    package: ApprovedContentPackage | None = None
    input_violations: tuple[DocumentInputViolation, ...] = Field(default_factory=tuple)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentPackageBuildResult":
        if self.status == ApprovedContentPackageBuildStatus.BUILT:
            if self.package is None:
                raise ValueError("BUILT requires a package")
            if self.input_violations or self.diagnostics:
                raise ValueError("BUILT must carry no input_violations/diagnostics")
        else:
            if self.package is not None:
                raise ValueError(f"{self.status} must carry no package")
            if self.status == ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID:
                if not self.input_violations and not self.diagnostics:
                    raise ValueError(
                        "DOCUMENT_INPUT_INVALID requires input_violations or diagnostics"
                    )
            elif self.input_violations:
                raise ValueError(f"{self.status} must carry no input_violations")
        return self


class ApprovedContentCurrentAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_fingerprint: str = Field(pattern=SHA256_PATTERN)
    slot_version: int = Field(ge=1)


class ApprovedContentPackageSaveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApprovedContentPackageSaveStatus
    package: ApprovedContentPackage | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentPackageSaveResult":
        if self.status in (
            ApprovedContentPackageSaveStatus.SAVED,
            ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL,
        ):
            if self.package is None:
                raise ValueError(f"{self.status} requires a package")
        else:
            if self.package is not None:
                raise ValueError(f"{self.status} must carry no package")
        return self


class ApprovedContentPackageReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApprovedContentPackageReadStatus
    package: ApprovedContentPackage | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentPackageReadResult":
        if self.status == ApprovedContentPackageReadStatus.FOUND:
            if self.package is None:
                raise ValueError("FOUND requires a package")
        else:
            if self.package is not None:
                raise ValueError(f"{self.status} must carry no package")
        return self


class ApprovedContentAuthorityReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApprovedContentAuthorityReadStatus
    authority: ApprovedContentCurrentAuthority | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentAuthorityReadResult":
        if self.status == ApprovedContentAuthorityReadStatus.FOUND:
            if self.authority is None:
                raise ValueError("FOUND requires an authority")
        else:
            if self.authority is not None:
                raise ValueError(f"{self.status} must carry no authority")
        return self


class ApprovedContentAuthorityPromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApprovedContentAuthorityPromotionStatus
    authority: ApprovedContentCurrentAuthority | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentAuthorityPromotionResult":
        if self.status in (
            ApprovedContentAuthorityPromotionStatus.PROMOTED,
            ApprovedContentAuthorityPromotionStatus.ALREADY_CURRENT,
            ApprovedContentAuthorityPromotionStatus.STALE_REVISION,
        ):
            if self.authority is None:
                raise ValueError(f"{self.status} requires an authority")
        else:
            if self.authority is not None:
                raise ValueError(f"{self.status} must carry no authority")
        return self
