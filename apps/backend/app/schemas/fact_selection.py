"""Pydantic contracts for Explicit Provenance Stage P3 fact selection.

P3 is a pure, deterministic decision layer over existing P1/P2 Truth
contracts (``TruthEntity``, ``TruthFact``, ``TruthPermission``). It creates
no new facts, no new Truth Library, no new SQL table, and persists no
selection decision. Every model in this module is a plain, replayable
Pydantic value object: the same full input must always produce the same
output, with identity expressed only through content-derived fingerprints
(never a random UUID or an implicit ``datetime.now()``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.schemas.truth_entity import SHA256_PATTERN, _require_timezone
from app.schemas.truth_fact import Transferability, TransformationOperation


FACT_SELECTION_SCHEMA_VERSION = "1.0"


class SelectionDecisionOutcome(StrEnum):
    """The five outcomes a P3 fact-selection decision can reach."""

    SELECTED = "SELECTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    BLOCKED = "BLOCKED"
    EXCLUDED = "EXCLUDED"
    NOT_RELEVANT = "NOT_RELEVANT"


class FactSelectionReasonCode(StrEnum):
    """Deterministic, closed set of reasons a decision can carry."""

    FACT_STATUS_DRAFT_NOT_ELIGIBLE = "FACT_STATUS_DRAFT_NOT_ELIGIBLE"
    FACT_STATUS_REJECTED = "FACT_STATUS_REJECTED"
    FACT_STATUS_ARCHIVED = "FACT_STATUS_ARCHIVED"
    FACT_STATUS_REVIEW_REQUIRED = "FACT_STATUS_REVIEW_REQUIRED"
    FACT_REQUIRES_APPROVAL = "FACT_REQUIRES_APPROVAL"
    FACT_USE_IN_CV_DISABLED = "FACT_USE_IN_CV_DISABLED"
    FACT_STATUS_UNKNOWN = "FACT_STATUS_UNKNOWN"

    TRANSFERABILITY_NON_TRANSFERABLE_SCOPE_MISMATCH = (
        "TRANSFERABILITY_NON_TRANSFERABLE_SCOPE_MISMATCH"
    )
    TRANSFERABILITY_EMPLOYMENT_SCOPE_MISSING = "TRANSFERABILITY_EMPLOYMENT_SCOPE_MISSING"
    TRANSFERABILITY_EMPLOYMENT_SCOPE_MISMATCH = "TRANSFERABILITY_EMPLOYMENT_SCOPE_MISMATCH"
    TRANSFERABILITY_ENTITY_SCOPE_UNVERIFIED = "TRANSFERABILITY_ENTITY_SCOPE_UNVERIFIED"
    TRANSFERABILITY_ROLE_CONTEXT_MISSING = "TRANSFERABILITY_ROLE_CONTEXT_MISSING"
    TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING = "TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING"
    TRANSFERABILITY_EXACT_ONLY_REPHRASE_BLOCKED = (
        "TRANSFERABILITY_EXACT_ONLY_REPHRASE_BLOCKED"
    )

    # Checked against the existing P1 ``TruthTransformationPolicyRegistry``,
    # strictly before any permission is evaluated. A ``TruthPermission`` can
    # never grant a target_scope or operation these codes have already denied.
    FACT_TYPE_POLICY_NOT_FOUND = "FACT_TYPE_POLICY_NOT_FOUND"
    TARGET_SCOPE_NOT_ALLOWED_BY_POLICY = "TARGET_SCOPE_NOT_ALLOWED_BY_POLICY"
    OPERATION_NOT_ALLOWED_BY_POLICY = "OPERATION_NOT_ALLOWED_BY_POLICY"

    PERMISSION_NOT_REQUIRED_BASE_OPERATION = "PERMISSION_NOT_REQUIRED_BASE_OPERATION"
    PERMISSION_ACTIVE_GRANT = "PERMISSION_ACTIVE_GRANT"
    PERMISSION_MISSING_FOR_OPERATION = "PERMISSION_MISSING_FOR_OPERATION"
    PERMISSION_REVIEW_REQUIRED = "PERMISSION_REVIEW_REQUIRED"
    PERMISSION_REVOKED = "PERMISSION_REVOKED"
    PERMISSION_ARCHIVED = "PERMISSION_ARCHIVED"
    PERMISSION_NOT_YET_VALID = "PERMISSION_NOT_YET_VALID"
    PERMISSION_EXPIRED = "PERMISSION_EXPIRED"
    PERMISSION_SCOPE_MISMATCH = "PERMISSION_SCOPE_MISMATCH"
    PERMISSION_CONSTRAINTS_MISMATCH = "PERMISSION_CONSTRAINTS_MISMATCH"

    CPE_NOT_RELEVANT = "CPE_NOT_RELEVANT"


class TargetContext(BaseModel):
    """Explicit, caller-supplied targeting context for one selection request.

    P3 never infers ``target_role_family`` or
    ``target_organization_or_industry``: both must be sourced by the caller
    from an existing, explicit input (``Job.metadata_json``, an existing
    ``CareerPositioningResponse``, or a parameter passed by the caller).
    """

    model_config = ConfigDict(extra="forbid")

    target_context_id: UUID4
    person_entity_id: UUID4
    job_or_application_id: str | None = Field(default=None, max_length=255)
    target_role_title: str | None = Field(default=None, max_length=500)
    target_role_family: str | None = Field(default=None, max_length=255)
    target_seniority: str | None = Field(default=None, max_length=255)
    target_organization_or_industry: str | None = Field(default=None, max_length=255)
    employment_entity_id: UUID4 | None = None
    selection_policy_version: str = Field(min_length=1, max_length=64)

    @field_validator(
        "job_or_application_id",
        "target_role_title",
        "target_role_family",
        "target_seniority",
        "target_organization_or_industry",
    )
    @classmethod
    def _blank_becomes_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = " ".join(value.split())
        return stripped or None


class CareerPositioningSignal(BaseModel):
    """Advisory-only signal sourced from an existing ``CareerPositioningResponse``.

    The Career Positioning Engine (CPE) cannot create, mutate, approve, or
    change the status/``use_in_cv``/transferability/permission of any fact.
    It can only mark specific facts as not relevant to the current target
    context, and only through their stable ``fact_id`` -- never through a
    text-similarity fallback.
    """

    model_config = ConfigDict(extra="forbid")

    source_job_id: str = Field(min_length=1, max_length=255)
    source_generated_at: datetime
    target_role_level: str | None = Field(default=None, max_length=255)
    target_role_family: str | None = Field(default=None, max_length=255)
    seniority_direction: Literal["FLATTEN", "ELEVATE", "MAINTAIN"] | None = None
    flattening_required: bool = False
    transferability_risk: Literal["NONE", "LOW", "MEDIUM", "HIGH"] | None = None
    requires_human_review: bool = False
    not_relevant_fact_ids: tuple[UUID4, ...] = Field(default_factory=tuple)

    @field_validator("source_generated_at")
    @classmethod
    def _generated_at_is_aware(cls, value: datetime) -> datetime:
        aware = _require_timezone(value)
        assert aware is not None
        return aware

    @field_validator("not_relevant_fact_ids")
    @classmethod
    def _canonical_not_relevant_ids(cls, value: tuple[UUID4, ...]) -> tuple[UUID4, ...]:
        return tuple(sorted(set(value), key=str))


class FactSelectionDecision(BaseModel):
    """A single, replayable fact-selection decision.

    Never stores generated CV text, redacted content, alternative bullet
    phrasing, or a new fact description -- only the decision over an
    existing fact.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = FACT_SELECTION_SCHEMA_VERSION
    decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    selection_policy_version: str = Field(min_length=1, max_length=64)
    transformation_policy_version: str = Field(min_length=1, max_length=64)

    target_context_id: UUID4
    target_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4

    entity_id: UUID4
    fact_id: UUID4
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)

    decision: SelectionDecisionOutcome
    reason_codes: tuple[FactSelectionReasonCode, ...]

    requested_operation: TransformationOperation
    requested_target_scope: str = Field(min_length=1, max_length=512)
    allowed_operation: TransformationOperation | None
    transferability: Transferability
    employment_scope_entity_id: UUID4 | None

    permission_ids: tuple[UUID4, ...]
    permission_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)

    requires_user_approval: bool
    evaluation_time: datetime
    advisory_flags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("reason_codes")
    @classmethod
    def _canonical_reason_codes(
        cls, value: tuple[FactSelectionReasonCode, ...]
    ) -> tuple[FactSelectionReasonCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("advisory_flags")
    @classmethod
    def _canonical_advisory_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("permission_ids")
    @classmethod
    def _canonical_permission_ids(cls, value: tuple[UUID4, ...]) -> tuple[UUID4, ...]:
        return tuple(sorted(set(value), key=str))

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time_is_aware(cls, value: datetime) -> datetime:
        aware = _require_timezone(value)
        assert aware is not None
        return aware

    @model_validator(mode="after")
    def _validate_decision_consistency(self) -> "FactSelectionDecision":
        if self.requires_user_approval != (
            self.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
        ):
            raise ValueError(
                "requires_user_approval must match decision == APPROVAL_REQUIRED"
            )
        if self.decision == SelectionDecisionOutcome.SELECTED:
            if self.allowed_operation != self.requested_operation:
                raise ValueError("SELECTED decisions must allow the requested operation")
        else:
            if self.allowed_operation is not None:
                raise ValueError(
                    "allowed_operation must be null unless the decision is SELECTED"
                )
        return self
