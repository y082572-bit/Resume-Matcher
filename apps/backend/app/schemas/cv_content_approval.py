"""Pydantic contracts for Explicit Provenance Stage P5.5: semantic proposal
validation, user approval, and final content assembly.

Stage P5.5 is split into two fully separated steps, each with its own
result model, fingerprint, replay input, and freshness evaluation (Variant
B):

* **Step 1 -- semantic proposal validation** (``ProposalSemanticValidationResult``):
  re-validates P4/P5a/P5b, re-runs P5b's own deterministic guardrails, and
  makes exactly one semantic-validator call per P5b proposal. It never
  approves a proposal and never changes its text.
* **Step 2 -- user approval and content assembly** (``ApprovedCvContentResult``):
  consumes an already-computed ``ProposalSemanticValidationResult`` plus
  explicit ``ProposalApprovalDecision`` values and assembles the final,
  deterministic CV content. It never calls a provider or performs an LLM
  validation, and it never approves a proposal on the user's behalf.

Every model here is a closed, replayable Pydantic value object: identity is
expressed only through content-derived SHA-256 fingerprints, never a random
UUID or an implicit ``datetime.now()``. See
``docs/explicit-provenance-stage-p55.md`` for the full scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.cv_content_plan import CvSection, EntryOrderSource
from app.schemas.cv_content_proposal import ImmutableConstraintSet, TargetRoleContext
from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN, TransformationOperation


SEMANTIC_VALIDATION_SCHEMA_VERSION = "cv-content-semantic-validation-schema-v1"
SEMANTIC_VALIDATION_PROMPT_VERSION = "controlled-semantic-validation-prompt-v1"
SEMANTIC_VALIDATION_RESPONSE_SCHEMA_VERSION = "controlled-semantic-validation-response-v1"
APPROVAL_DECISION_SCHEMA_VERSION = "proposal-approval-decision-schema-v1"
APPROVED_CONTENT_SCHEMA_VERSION = "approved-cv-content-schema-v1"


class ProposalSemanticVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INVALID_INPUT = "INVALID_INPUT"


class SemanticValidationStatus(StrEnum):
    VALIDATED = "VALIDATED"
    BLOCKED = "BLOCKED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ProposalApprovalDecisionValue(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalAssemblyStatus(StrEnum):
    READY = "READY"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    BLOCKED = "BLOCKED"
    REQUIRES_REPLAN = "REQUIRES_REPLAN"
    INVALID_INPUT = "INVALID_INPUT"


class FinalContentOrigin(StrEnum):
    DETERMINISTIC_P5A = "DETERMINISTIC_P5A"
    APPROVED_PROPOSAL_P5B = "APPROVED_PROPOSAL_P5B"


class SemanticValidationFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class ApprovedContentFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class SemanticValidatorProviderErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    INVALID_RESPONSE_JSON = "INVALID_RESPONSE_JSON"
    RESPONSE_SCHEMA_MISMATCH = "RESPONSE_SCHEMA_MISMATCH"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    TOKEN_LIMIT = "TOKEN_LIMIT"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    UNSUPPORTED_FINISH_REASON = "UNSUPPORTED_FINISH_REASON"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class SemanticClaimViolationCode(StrEnum):
    """Closed taxonomy of claim-level semantic changes a validator can flag.

    Never extended by ``startswith``, substring, or text-similarity
    classification -- a validator response's ``detected_violation_codes``
    are always drawn from this exact set (Pydantic enforces this at parse
    time).
    """

    NEW_CLAIM_ADDED = "NEW_CLAIM_ADDED"
    MATERIAL_CLAIM_REMOVED = "MATERIAL_CLAIM_REMOVED"
    RESPONSIBILITY_SCOPE_INCREASED = "RESPONSIBILITY_SCOPE_INCREASED"
    RESPONSIBILITY_SCOPE_DECREASED_MATERIALLY = "RESPONSIBILITY_SCOPE_DECREASED_MATERIALLY"
    MANAGEMENT_SCOPE_CHANGED = "MANAGEMENT_SCOPE_CHANGED"
    DECISION_AUTHORITY_CHANGED = "DECISION_AUTHORITY_CHANGED"
    OWNERSHIP_CHANGED = "OWNERSHIP_CHANGED"
    ACHIEVEMENT_ATTRIBUTION_CHANGED = "ACHIEVEMENT_ATTRIBUTION_CHANGED"
    EMPLOYMENT_ATTRIBUTION_CHANGED = "EMPLOYMENT_ATTRIBUTION_CHANGED"
    COMPANY_CHANGED = "COMPANY_CHANGED"
    ROLE_CHANGED = "ROLE_CHANGED"
    DATE_CHANGED = "DATE_CHANGED"
    NUMBER_CHANGED = "NUMBER_CHANGED"
    PERCENTAGE_CHANGED = "PERCENTAGE_CHANGED"
    CURRENCY_CHANGED = "CURRENCY_CHANGED"
    NEGATION_CHANGED = "NEGATION_CHANGED"
    CERTAINTY_CHANGED = "CERTAINTY_CHANGED"
    CAUSALITY_CHANGED = "CAUSALITY_CHANGED"
    TEAM_SIZE_CHANGED = "TEAM_SIZE_CHANGED"
    PORTFOLIO_SCALE_CHANGED = "PORTFOLIO_SCALE_CHANGED"
    GEOGRAPHIC_SCOPE_CHANGED = "GEOGRAPHIC_SCOPE_CHANGED"
    PRODUCT_SCOPE_CHANGED = "PRODUCT_SCOPE_CHANGED"
    CUSTOMER_SCOPE_CHANGED = "CUSTOMER_SCOPE_CHANGED"
    LOWER_ROLE_FLATTENING_BECAME_FALSE = "LOWER_ROLE_FLATTENING_BECAME_FALSE"
    UNSUPPORTED_SEMANTIC_CHANGE = "UNSUPPORTED_SEMANTIC_CHANGE"


class SemanticGuardrailViolationCode(StrEnum):
    """Closed set of problems the P5.5 Step 1 deterministic re-check can
    report *before* a semantic validator is ever called.

    A guardrail failure always yields a ``BlockedSemanticValidationItem``
    and never a validator call -- see ``cv_content_semantic_validation_builder``.
    """

    PLANNED_FACT_USE_NOT_FOUND = "PLANNED_FACT_USE_NOT_FOUND"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_IDENTITY_MISMATCH = "PAYLOAD_IDENTITY_MISMATCH"
    PAYLOAD_FINGERPRINT_MISMATCH = "PAYLOAD_FINGERPRINT_MISMATCH"
    PAYLOAD_SHAPE_INVALID = "PAYLOAD_SHAPE_INVALID"
    PROPOSAL_FINGERPRINT_MISMATCH = "PROPOSAL_FINGERPRINT_MISMATCH"
    PROPOSAL_TEXT_FINGERPRINT_MISMATCH = "PROPOSAL_TEXT_FINGERPRINT_MISMATCH"
    SOURCE_TEXT_FINGERPRINT_MISMATCH = "SOURCE_TEXT_FINGERPRINT_MISMATCH"
    BLOCKED_GENERATION_ITEM_FINGERPRINT_MISMATCH = "BLOCKED_GENERATION_ITEM_FINGERPRINT_MISMATCH"
    PROMPT_FINGERPRINT_MISMATCH = "PROMPT_FINGERPRINT_MISMATCH"
    MODEL_CONFIGURATION_FINGERPRINT_MISMATCH = "MODEL_CONFIGURATION_FINGERPRINT_MISMATCH"
    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    EMPLOYMENT_SCOPE_MISMATCH = "EMPLOYMENT_SCOPE_MISMATCH"
    TARGET_SECTION_MISMATCH = "TARGET_SECTION_MISMATCH"
    TARGET_SCOPE_MISMATCH = "TARGET_SCOPE_MISMATCH"
    PLACEMENT_ORDER_MISMATCH = "PLACEMENT_ORDER_MISMATCH"
    IMMUTABLE_CONSTRAINT_VIOLATION = "IMMUTABLE_CONSTRAINT_VIOLATION"
    AWARD_NOT_SUPPORTED = "AWARD_NOT_SUPPORTED"


class CvContentApprovalViolationCode(StrEnum):
    """Closed set of result-level (not per-item) problems either P5.5 step
    can report -- always re-derived structurally, never guessed."""

    INPUT_PLAN_INVALID = "INPUT_PLAN_INVALID"
    INPUT_PLAN_STALE = "INPUT_PLAN_STALE"
    INPUT_PLAN_HAS_CONFLICTS = "INPUT_PLAN_HAS_CONFLICTS"
    INPUT_P5A_RESULT_INVALID = "INPUT_P5A_RESULT_INVALID"
    INPUT_P5A_RESULT_STALE = "INPUT_P5A_RESULT_STALE"
    INPUT_P5B_RESULT_INVALID = "INPUT_P5B_RESULT_INVALID"
    INPUT_P5B_RESULT_STALE = "INPUT_P5B_RESULT_STALE"
    CONTENT_PLAN_FINGERPRINT_MISMATCH = "CONTENT_PLAN_FINGERPRINT_MISMATCH"
    P5A_RESULT_FINGERPRINT_MISMATCH = "P5A_RESULT_FINGERPRINT_MISMATCH"
    P5B_RESULT_FINGERPRINT_MISMATCH = "P5B_RESULT_FINGERPRINT_MISMATCH"
    PAYLOAD_SET_FINGERPRINT_MISMATCH = "PAYLOAD_SET_FINGERPRINT_MISMATCH"
    SEMANTIC_VALIDATION_RESULT_INVALID = "SEMANTIC_VALIDATION_RESULT_INVALID"
    SEMANTIC_VALIDATION_RESULT_STALE = "SEMANTIC_VALIDATION_RESULT_STALE"
    SEMANTIC_VALIDATION_RESULT_FINGERPRINT_MISMATCH = (
        "SEMANTIC_VALIDATION_RESULT_FINGERPRINT_MISMATCH"
    )
    DUPLICATE_APPROVAL_DECISION = "DUPLICATE_APPROVAL_DECISION"
    EXTRA_APPROVAL_DECISION = "EXTRA_APPROVAL_DECISION"
    APPROVAL_DECISION_FOR_UNKNOWN_PROPOSAL = "APPROVAL_DECISION_FOR_UNKNOWN_PROPOSAL"
    APPROVAL_DECISION_FINGERPRINT_MISMATCH = "APPROVAL_DECISION_FINGERPRINT_MISMATCH"
    APPROVAL_FOR_STALE_PROPOSAL = "APPROVAL_FOR_STALE_PROPOSAL"
    APPROVAL_FOR_FAILED_VALIDATION = "APPROVAL_FOR_FAILED_VALIDATION"
    FINAL_PARTITION_GAP = "FINAL_PARTITION_GAP"
    FINAL_PARTITION_OVERLAP = "FINAL_PARTITION_OVERLAP"
    DRAFT_FINGERPRINT_MISMATCH = "DRAFT_FINGERPRINT_MISMATCH"


class FinalDispositionCode(StrEnum):
    INCLUDED_DETERMINISTIC = "INCLUDED_DETERMINISTIC"
    INCLUDED_APPROVED_PROPOSAL = "INCLUDED_APPROVED_PROPOSAL"
    REJECTED_BY_USER = "REJECTED_BY_USER"
    PENDING_USER_DECISION = "PENDING_USER_DECISION"
    SEMANTIC_VALIDATION_FAILED = "SEMANTIC_VALIDATION_FAILED"
    SEMANTIC_VALIDATION_INCONCLUSIVE = "SEMANTIC_VALIDATION_INCONCLUSIVE"
    SEMANTIC_VALIDATION_PROVIDER_ERROR = "SEMANTIC_VALIDATION_PROVIDER_ERROR"
    INPUT_INVALID = "INPUT_INVALID"
    NON_ELIGIBLE_BLOCKED = "NON_ELIGIBLE_BLOCKED"
    REQUIRES_REPLAN = "REQUIRES_REPLAN"


class SemanticValidatorModelConfiguration(BaseModel):
    """Explicit, closed semantic-validator call configuration.

    Never carries a secret or a fallback model list -- mirrors
    ``LlmModelConfiguration`` from P5b's controlled-proposal contract.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    max_output_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0.0)
    maximum_attempts: Literal[1, 2]
    response_schema_version: str = Field(min_length=1, max_length=64)
    retry_policy_version: str = Field(min_length=1, max_length=64)


class SemanticValidationContext(BaseModel):
    """Only the data allowed to influence one semantic-validator call.

    Never carries the full Truth Library, the full CV, the Master Resume,
    other facts, other proposals, an API key, or an arbitrary metadata
    dict.
    """

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=1, max_length=32)
    validation_prompt_version: str = Field(min_length=1, max_length=64)
    validation_policy_version: str = Field(min_length=1, max_length=64)
    retry_policy_version: str = Field(min_length=1, max_length=64)
    validator_model_configuration: SemanticValidatorModelConfiguration


class ControlledSemanticValidationRequest(BaseModel):
    """The complete, bounded, one-proposal-one-fact request handed to the
    semantic validator. Never carries another fact, another proposal, the
    full plan, the full P5a draft, the full job ad, or the Master Resume."""

    model_config = ConfigDict(extra="forbid")

    prompt_template_version: str = Field(min_length=1, max_length=64)
    operation: TransformationOperation
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    employment_scope_entity_id: UUID4 | None
    source_fact_id: UUID4
    source_text: str = Field(min_length=1, max_length=20_000)
    proposal_text: str = Field(min_length=1, max_length=20_000)
    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraints: ImmutableConstraintSet
    target_role_context: TargetRoleContext
    forbidden_semantic_changes: tuple[str, ...] = Field(default_factory=tuple)
    response_schema_version: str = Field(min_length=1, max_length=64)


class ControlledSemanticValidationResponse(BaseModel):
    """The closed response shape a ``ControlledProposalSemanticValidator``
    must return as structured JSON. Never a fallback to free text, and
    never a corrected/replacement proposal text."""

    model_config = ConfigDict(extra="forbid")

    verdict: ProposalSemanticVerdict
    source_fact_id: UUID4
    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    operation: TransformationOperation
    detected_violation_codes: tuple[SemanticClaimViolationCode, ...] = Field(default_factory=tuple)
    source_claim_summary: str = Field(min_length=1, max_length=2_000)
    proposal_claim_summary: str = Field(min_length=1, max_length=2_000)
    model_self_reported_warnings: tuple[str, ...] = Field(default_factory=tuple)


class SemanticValidatorAdapterResponse(BaseModel):
    """The adapter's raw return value for one attempt.

    ``raw_response_text`` is transient -- only its fingerprint is ever
    persisted into ``SemanticValidationProvenance``/``SemanticValidationAttempt``.
    ``provider_request_id`` is metadata only: it never participates in any
    fingerprint or identity check.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    structured_response: ControlledSemanticValidationResponse | None = None
    raw_response_text: str = Field(default="")
    finish_reason: str | None = None
    usage_metadata: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    provider_error_code: SemanticValidatorProviderErrorCode | None = None


class SemanticValidationAttempt(BaseModel):
    """One retry attempt's provenance. Never carries a secret or a header."""

    model_config = ConfigDict(extra="forbid")

    attempt_number: int = Field(ge=1, le=2)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    response_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_error_code: SemanticValidatorProviderErrorCode | None = None
    finish_reason: str | None = None
    attempt_fingerprint: str = Field(pattern=SHA256_PATTERN)


class SemanticValidationProvenance(BaseModel):
    """Full, fingerprint-only provenance of one proposal's semantic
    validation call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5b_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraint_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_fingerprint: str = Field(pattern=SHA256_PATTERN)
    attempt_count: int = Field(ge=1, le=2)
    attempt_fingerprints: tuple[str, ...]
    raw_response_fingerprint: str = Field(pattern=SHA256_PATTERN)
    structured_response_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ProposalSemanticValidationItem(BaseModel):
    """One P5b proposal for which the semantic validator was actually
    called exactly once (deterministic guardrails already passed).

    ``verdict`` alone never implies approval -- only Step 2's
    ``ProposalApprovalDecision`` can approve content.
    """

    model_config = ConfigDict(extra="forbid")

    semantic_validation_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    employment_scope_entity_id: UUID4 | None
    allowed_operation: TransformationOperation
    placement_order: int = Field(ge=0)
    verdict: ProposalSemanticVerdict
    detected_violation_codes: tuple[SemanticClaimViolationCode, ...] = Field(default_factory=tuple)
    provenance: SemanticValidationProvenance


class BlockedSemanticValidationItem(BaseModel):
    """Exactly one P5b proposal that failed a deterministic re-check
    *before* the semantic validator was ever called. Never carries a
    verdict or validator provenance."""

    model_config = ConfigDict(extra="forbid")

    blocked_semantic_validation_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    requested_operation: TransformationOperation
    allowed_operation: TransformationOperation
    violation_code: SemanticGuardrailViolationCode
    detail: str = Field(min_length=1, max_length=1000)


class CvContentApprovalViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: CvContentApprovalViolationCode
    detail: str = Field(min_length=1, max_length=1000)
    fact_id: UUID4 | None = None
    planned_fact_use_fingerprint: str | None = None
    proposal_fingerprint: str | None = None


class ProposalSemanticValidationResult(BaseModel):
    """The single return value of ``validate_cv_content_proposals``
    (Step 1). Never approves a proposal and never carries edited text."""

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: SemanticValidationStatus
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    p5b_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    validation_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    validation_items: tuple[ProposalSemanticValidationItem, ...] = Field(default_factory=tuple)
    blocked_items: tuple[BlockedSemanticValidationItem, ...] = Field(default_factory=tuple)
    violations: tuple[CvContentApprovalViolation, ...] = Field(default_factory=tuple)
    non_eligible_blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    deterministic_element_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    pending_approval_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    omitted_fact_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    conflict_fingerprints: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ProposalSemanticValidationResult":
        if self.status in (SemanticValidationStatus.INVALID_INPUT,) and (
            self.validation_items or self.blocked_items
        ):
            raise ValueError("INVALID_INPUT must carry no validation_items or blocked_items")
        if self.status == SemanticValidationStatus.INVALID_INPUT and not self.violations:
            raise ValueError("INVALID_INPUT must carry at least one violation")
        return self


class ProposalApprovalDecision(BaseModel):
    """One explicit, user-sourced APPROVED/REJECTED decision for exactly
    one P5b proposal. Absence of a decision is never synthesized here --
    the approval builder interprets a missing decision as PENDING."""

    model_config = ConfigDict(extra="forbid")

    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    semantic_validation_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    semantic_validation_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    decision: ProposalApprovalDecisionValue
    decision_policy_version: str = Field(min_length=1, max_length=64)
    decision_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    decision_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ApprovedCvContentElement(BaseModel):
    """One final CV content element, sourced either from a deterministic
    P5a element or from a semantic-PASS, user-APPROVED P5b proposal.

    Never regenerates text, never reorders, never moves between
    sections/employment entries.
    """

    model_config = ConfigDict(extra="forbid")

    element_fingerprint: str = Field(pattern=SHA256_PATTERN)
    origin: FinalContentOrigin
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    employment_scope_entity_id: UUID4 | None
    placement_order: int = Field(ge=0)
    allowed_operation: TransformationOperation
    content_text: str = Field(min_length=1, max_length=20_000)
    # -- origin=DETERMINISTIC_P5A provenance --
    generated_element_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    generation_provenance_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    # -- origin=APPROVED_PROPOSAL_P5B provenance --
    proposal_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    semantic_validation_item_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    approval_decision_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposal_provenance_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    semantic_validation_provenance_fingerprint: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )

    @model_validator(mode="after")
    def _origin_provenance_consistency(self) -> "ApprovedCvContentElement":
        if self.origin == FinalContentOrigin.DETERMINISTIC_P5A:
            if self.generated_element_fingerprint is None or self.generation_provenance_fingerprint is None:
                raise ValueError("DETERMINISTIC_P5A origin requires P5a provenance fingerprints")
            if any(
                value is not None
                for value in (
                    self.proposal_fingerprint,
                    self.semantic_validation_item_fingerprint,
                    self.approval_decision_fingerprint,
                    self.proposal_provenance_fingerprint,
                    self.semantic_validation_provenance_fingerprint,
                )
            ):
                raise ValueError("DETERMINISTIC_P5A origin must carry no P5b provenance")
        else:
            if any(
                value is None
                for value in (
                    self.proposal_fingerprint,
                    self.semantic_validation_item_fingerprint,
                    self.approval_decision_fingerprint,
                    self.proposal_provenance_fingerprint,
                    self.semantic_validation_provenance_fingerprint,
                )
            ):
                raise ValueError("APPROVED_PROPOSAL_P5B origin requires full P5b provenance")
            if self.generated_element_fingerprint is not None or self.generation_provenance_fingerprint is not None:
                raise ValueError("APPROVED_PROPOSAL_P5B origin must carry no P5a provenance")
        return self


class ApprovedCvFlatSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["FLAT"] = "FLAT"
    section: CvSection
    section_fingerprint: str = Field(pattern=SHA256_PATTERN)
    elements: tuple[ApprovedCvContentElement, ...] = Field(default_factory=tuple)


class ApprovedCvExperienceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_fingerprint: str = Field(pattern=SHA256_PATTERN)
    employment_entity_id: UUID4
    entry_order: int = Field(ge=0)
    entry_order_source: EntryOrderSource
    header_elements: tuple[ApprovedCvContentElement, ...] = Field(default_factory=tuple)
    responsibility_elements: tuple[ApprovedCvContentElement, ...] = Field(default_factory=tuple)
    achievement_elements: tuple[ApprovedCvContentElement, ...] = Field(default_factory=tuple)


class ApprovedCvExperienceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["EXPERIENCE"] = "EXPERIENCE"
    section: Literal[CvSection.EXPERIENCE] = CvSection.EXPERIENCE
    section_fingerprint: str = Field(pattern=SHA256_PATTERN)
    experience_entries: tuple[ApprovedCvExperienceEntry, ...] = Field(default_factory=tuple)


ApprovedCvSection = Annotated[
    Union[ApprovedCvExperienceSection, ApprovedCvFlatSection], Field(discriminator="kind")
]


class CvContentFinalDisposition(BaseModel):
    """The exactly-one disposition of one P4 ``PlannedFactUse``."""

    model_config = ConfigDict(extra="forbid")

    disposition_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_id: UUID4
    disposition_code: FinalDispositionCode
    origin: FinalContentOrigin | None = None
    element_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    detail: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _included_requires_origin_and_element(self) -> "CvContentFinalDisposition":
        included_codes = (
            FinalDispositionCode.INCLUDED_DETERMINISTIC,
            FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL,
        )
        if self.disposition_code in included_codes:
            if self.origin is None or self.element_fingerprint is None:
                raise ValueError("an INCLUDED_* disposition requires origin and element_fingerprint")
        else:
            if self.origin is not None or self.element_fingerprint is not None:
                raise ValueError("a non-INCLUDED disposition must carry no origin/element_fingerprint")
        return self


class ApprovedCvContentDraft(BaseModel):
    """Structural mirror of the source P4 plan -- one approved section per
    plan section, in the same order, never moved or merged."""

    model_config = ConfigDict(extra="forbid")

    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    draft_fingerprint: str = Field(pattern=SHA256_PATTERN)
    sections: tuple[ApprovedCvSection, ...]

    @model_validator(mode="after")
    def _exactly_one_section_per_cv_section(self) -> "ApprovedCvContentDraft":
        sections_seen = [entry.section for entry in self.sections]
        if len(sections_seen) != len(set(sections_seen)):
            raise ValueError("sections must contain exactly one entry per CvSection")
        if set(sections_seen) != set(CvSection):
            raise ValueError("sections must cover every CvSection value")
        return self


class ApprovedCvContentResult(BaseModel):
    """The single return value of ``build_approved_cv_content`` (Step 2).

    ``ready_for_p6`` is always computed internally, never caller-supplied.
    """

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: ApprovalAssemblyStatus
    ready_for_p6: bool
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    p5b_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    semantic_validation_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    draft: ApprovedCvContentDraft | None = None
    dispositions: tuple[CvContentFinalDisposition, ...] = Field(default_factory=tuple)
    violations: tuple[CvContentApprovalViolation, ...] = Field(default_factory=tuple)
    pending_approval_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    omitted_fact_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    conflict_fingerprints: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _ready_for_p6_requires_status_ready(self) -> "ApprovedCvContentResult":
        if self.ready_for_p6 and self.status != ApprovalAssemblyStatus.READY:
            raise ValueError("ready_for_p6 requires ApprovalAssemblyStatus.READY")
        if self.status == ApprovalAssemblyStatus.READY and self.draft is None:
            raise ValueError("READY must carry a non-null draft")
        if self.status == ApprovalAssemblyStatus.INVALID_INPUT and self.draft is not None:
            raise ValueError("INVALID_INPUT must carry draft=None")
        return self


class ProposalSemanticValidationReplayInput(BaseModel):
    """The exact, Step-1-derived fields whose change makes a semantic
    validation result stale."""

    model_config = ConfigDict(extra="forbid")

    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_schema_version: str = Field(min_length=1, max_length=64)
    content_policy_version: str = Field(min_length=1, max_length=64)
    p5a_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_replay_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_generation_schema_version: str = Field(min_length=1, max_length=64)
    p5a_generation_policy_version: str = Field(min_length=1, max_length=64)
    p5b_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5b_replay_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5b_proposal_schema_version: str = Field(min_length=1, max_length=64)
    p5b_proposal_policy_version: str = Field(min_length=1, max_length=64)
    fact_payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    validation_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraint_set_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    validator_model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    validation_prompt_version: str = Field(min_length=1, max_length=64)
    validation_policy_version: str = Field(min_length=1, max_length=64)
    validation_schema_version: str = Field(min_length=1, max_length=64)
    validation_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    blocked_validation_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)


class ProposalSemanticValidationReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: SemanticValidationFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)


class ApprovedCvContentReplayInput(BaseModel):
    """The exact, Step-2-derived fields whose change makes an approved
    content result stale."""

    model_config = ConfigDict(extra="forbid")

    semantic_validation_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    semantic_validation_replay_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approval_decision_schema_version: str = Field(min_length=1, max_length=64)
    approval_decision_policy_version: str = Field(min_length=1, max_length=64)
    approved_content_schema_version: str = Field(min_length=1, max_length=64)
    approved_content_policy_version: str = Field(min_length=1, max_length=64)
    approval_decision_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    disposition_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    approved_element_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    section_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    draft_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    approved_content_result_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ApprovedCvContentReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: ApprovedContentFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
