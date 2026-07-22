"""Pydantic contracts for Explicit Provenance Stage P5b: controlled LLM
content proposals.

P5b consumes an already ``p5_ready`` P4 ``CvContentPlan`` plus its P5a
``CvContentGenerationResult`` and produces, through an injected
``ControlledProposalLlmAdapter``, exactly one ``GeneratedCvContentProposal``
or ``BlockedProposalItem`` per eligible ``BlockedGenerationItem`` -- eligible
meaning P5a blocked it solely because its operation
(``CONTROLLED_REPHRASE``/``FLATTEN_FOR_LOWER_ROLE``) is not one of the
deterministic operations P5a itself can render.

Every proposal always carries ``approval_state=REQUIRES_USER_APPROVAL`` and
``ready_for_p55=False`` on the result: P5b never approves, rejects, or
merges a proposal into a full CV. Claim-level semantic validation of a
proposal's text belongs to Stage P5.5, not here. See
``docs/explicit-provenance-stage-p5b.md`` for the full scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.cv_content_plan import CvSection
from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN, TransformationOperation


PROPOSAL_GENERATION_SCHEMA_VERSION = "cv-content-proposal-schema-v1"
PROPOSAL_GENERATION_POLICY_VERSION = "cv-content-proposal-policy-v1"
CONTROLLED_REPHRASE_PROMPT_VERSION = "controlled-rephrase-prompt-v1"
FLATTEN_FOR_LOWER_ROLE_PROMPT_VERSION = "flatten-for-lower-role-prompt-v1"
PROPOSAL_RESPONSE_SCHEMA_VERSION = "controlled-proposal-response-v1"
IMMUTABLE_CONSTRAINT_POLICY_VERSION = "immutable-constraint-policy-v1"
MODEL_RETRY_POLICY_VERSION = "proposal-model-retry-policy-v1"

#: The closed set of ``TransformationOperation`` values P5b can propose a
#: controlled LLM rewrite for. Every other value is out of scope by
#: definition -- see ``cv_content_proposal_policy.ELIGIBLE_OPERATIONS``.
EligibleProposalOperation = Literal[
    TransformationOperation.CONTROLLED_REPHRASE,
    TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
]


class ProposalGenerationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    BLOCKED = "BLOCKED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ProposalApprovalState(StrEnum):
    REQUIRES_USER_APPROVAL = "REQUIRES_USER_APPROVAL"


class ProposalFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class ProposalProviderErrorCode(StrEnum):
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


class ProposalViolationCode(StrEnum):
    """Closed set of problems the P5b proposal generator can report.

    Never extended by ``startswith``, substring, or text-similarity
    classification -- every code here is reached only through an explicit,
    versioned check in ``cv_content_proposal_builder``/``_policy``/
    ``_constraints``.
    """

    INPUT_PLAN_NOT_READY = "INPUT_PLAN_NOT_READY"
    INPUT_PLAN_STRUCTURALLY_INVALID = "INPUT_PLAN_STRUCTURALLY_INVALID"
    INPUT_PLAN_STALE = "INPUT_PLAN_STALE"
    INPUT_PLAN_HAS_CONFLICTS = "INPUT_PLAN_HAS_CONFLICTS"
    INPUT_P5A_RESULT_INVALID = "INPUT_P5A_RESULT_INVALID"
    INPUT_P5A_RESULT_STALE = "INPUT_P5A_RESULT_STALE"
    CONTENT_PLAN_FINGERPRINT_MISMATCH = "CONTENT_PLAN_FINGERPRINT_MISMATCH"
    P5A_RESULT_FINGERPRINT_MISMATCH = "P5A_RESULT_FINGERPRINT_MISMATCH"
    P5A_REPLAY_FINGERPRINT_MISMATCH = "P5A_REPLAY_FINGERPRINT_MISMATCH"
    PAYLOAD_SET_FINGERPRINT_MISMATCH = "PAYLOAD_SET_FINGERPRINT_MISMATCH"
    PAYLOAD_FINGERPRINT_MISMATCH = "PAYLOAD_FINGERPRINT_MISMATCH"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_IDENTITY_MISMATCH = "PAYLOAD_IDENTITY_MISMATCH"
    BLOCKED_ITEM_FINGERPRINT_MISMATCH = "BLOCKED_ITEM_FINGERPRINT_MISMATCH"
    BLOCKED_ITEM_NOT_FOUND_IN_PLAN = "BLOCKED_ITEM_NOT_FOUND_IN_PLAN"
    BLOCKED_ITEM_OPERATION_MISMATCH = "BLOCKED_ITEM_OPERATION_MISMATCH"
    OPERATION_NOT_ELIGIBLE_FOR_P5B = "OPERATION_NOT_ELIGIBLE_FOR_P5B"
    OMIT_NOT_GENERATABLE = "OMIT_NOT_GENERATABLE"
    PROMPT_FINGERPRINT_MISMATCH = "PROMPT_FINGERPRINT_MISMATCH"
    MODEL_CONFIGURATION_FINGERPRINT_MISMATCH = "MODEL_CONFIGURATION_FINGERPRINT_MISMATCH"
    RESPONSE_SCHEMA_MISMATCH = "RESPONSE_SCHEMA_MISMATCH"
    RESPONSE_OPERATION_MISMATCH = "RESPONSE_OPERATION_MISMATCH"
    RESPONSE_FACT_ID_MISMATCH = "RESPONSE_FACT_ID_MISMATCH"
    EMPTY_PROPOSAL_TEXT = "EMPTY_PROPOSAL_TEXT"
    PROPOSAL_CHARACTER_LIMIT_EXCEEDED = "PROPOSAL_CHARACTER_LIMIT_EXCEEDED"
    PROPOSAL_SENTENCE_LIMIT_EXCEEDED = "PROPOSAL_SENTENCE_LIMIT_EXCEEDED"
    IMMUTABLE_TOKEN_MISSING = "IMMUTABLE_TOKEN_MISSING"
    IMMUTABLE_TOKEN_CHANGED = "IMMUTABLE_TOKEN_CHANGED"
    IMMUTABLE_TOKEN_ADDED = "IMMUTABLE_TOKEN_ADDED"
    EMPLOYMENT_SCOPE_MISMATCH = "EMPLOYMENT_SCOPE_MISMATCH"
    TARGET_SECTION_MISMATCH = "TARGET_SECTION_MISMATCH"
    TARGET_SCOPE_MISMATCH = "TARGET_SCOPE_MISMATCH"
    PLACEMENT_ORDER_MISMATCH = "PLACEMENT_ORDER_MISMATCH"
    RESULT_PARTITION_VIOLATION = "RESULT_PARTITION_VIOLATION"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    AWARD_NOT_SUPPORTED = "AWARD_NOT_SUPPORTED"
    #: A payload's ``value_json`` fails the closed P5a payload-shape
    #: contract. P5a never reaches its own shape check for an eligible item
    #: (it blocks on operation support first), so P5b re-validates shape
    #: itself before ever building a prompt.
    PAYLOAD_SHAPE_INVALID = "PAYLOAD_SHAPE_INVALID"
    #: The P5a result carries zero eligible blocked items at all -- there is
    #: nothing for P5b to propose or block per-item, so this is reported as
    #: a result-level violation instead.
    NO_ELIGIBLE_ITEMS = "NO_ELIGIBLE_ITEMS"


class ImmutableConstraintType(StrEnum):
    """Closed set of deterministically extractable immutable token kinds.

    Never extended to proper names outside a structured payload field --
    those remain P5.5's semantic-validation responsibility.
    """

    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    PERCENTAGE = "PERCENTAGE"
    CURRENCY_AMOUNT = "CURRENCY_AMOUNT"
    NUMERIC_RANGE = "NUMERIC_RANGE"
    DATE = "DATE"
    PERIOD = "PERIOD"
    INEQUALITY = "INEQUALITY"
    UNIT = "UNIT"
    COMPANY_NAME = "COMPANY_NAME"
    ROLE_NAME = "ROLE_NAME"
    NEGATION = "NEGATION"


class TargetRoleContext(BaseModel):
    """Bounded targeting context: three closed scalar fields, nothing else.

    Never carries a full job-ad text, an arbitrary metadata dict, or any CV
    content -- see ``compute_target_role_context_fingerprint``.
    """

    model_config = ConfigDict(extra="forbid")

    target_job_title: str | None = Field(default=None, max_length=500)
    target_seniority_level: str | None = Field(default=None, max_length=255)
    target_role_family: str | None = Field(default=None, max_length=255)


class LlmModelConfiguration(BaseModel):
    """Explicit, closed model call configuration. Never carries a secret."""

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


class ProposalGenerationContext(BaseModel):
    """Only the data allowed to influence one controlled LLM proposal call.

    Never carries job-ad text, full CV content, the Truth Library, other
    facts, other employment entries, omitted/pending facts, the Master
    Resume, or a provider API key.
    """

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=1, max_length=32)
    tone_profile: str = Field(min_length=1, max_length=64)
    maximum_character_count: int = Field(gt=0, le=20_000)
    maximum_sentence_count: int = Field(gt=0, le=100)
    target_role_context: TargetRoleContext
    prompt_template_version: str = Field(min_length=1, max_length=64)
    generation_policy_version: str = Field(min_length=1, max_length=64)
    immutable_constraint_policy_version: str = Field(min_length=1, max_length=64)
    retry_policy_version: str = Field(min_length=1, max_length=64)
    model_configuration: LlmModelConfiguration


class ImmutableTokenConstraint(BaseModel):
    """One deterministically extracted immutable token from a source text."""

    model_config = ConfigDict(extra="forbid")

    constraint_type: ImmutableConstraintType
    raw_text: str = Field(min_length=1, max_length=512)
    normalized_value: str = Field(min_length=1, max_length=512)


class ImmutableConstraintSet(BaseModel):
    """The complete, caller-supplied snapshot of one fact's immutable tokens."""

    model_config = ConfigDict(extra="forbid")

    fact_id: UUID4
    constraints: tuple[ImmutableTokenConstraint, ...] = Field(default_factory=tuple)
    immutable_constraint_set_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ControlledProposalRequest(BaseModel):
    """The complete, bounded, one-fact request handed to the LLM adapter.

    Never carries another fact_id, another employment entry, the full plan,
    the full P5a draft, omitted/pending facts, the full job ad, or the
    Master Resume.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_template_version: str = Field(min_length=1, max_length=64)
    operation: TransformationOperation
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    employment_scope_entity_id: UUID4 | None
    source_fact_id: UUID4
    source_text: str = Field(min_length=1, max_length=20_000)
    immutable_constraints: ImmutableConstraintSet
    target_role_context: TargetRoleContext
    tone_profile: str = Field(min_length=1, max_length=64)
    maximum_character_count: int = Field(gt=0, le=20_000)
    maximum_sentence_count: int = Field(gt=0, le=100)
    forbidden_transformations: tuple[str, ...] = Field(default_factory=tuple)
    response_schema_version: str = Field(min_length=1, max_length=64)


class ControlledProposalResponse(BaseModel):
    """The closed response shape a ``ControlledProposalLlmAdapter`` must
    return as structured JSON. Never a fallback to free text."""

    model_config = ConfigDict(extra="forbid")

    proposal_text: str = Field(min_length=1, max_length=20_000)
    operation: TransformationOperation
    source_fact_id: UUID4
    preserved_tokens: tuple[str, ...] = Field(default_factory=tuple)
    model_self_reported_warnings: tuple[str, ...] = Field(default_factory=tuple)


class LlmAdapterResponse(BaseModel):
    """The adapter's raw return value for one attempt.

    ``raw_response_text`` is transient -- only its fingerprint is ever
    persisted into ``LlmGenerationProvenance``/``LlmGenerationAttempt``.
    ``provider_request_id`` is metadata only: it never participates in any
    fingerprint or identity check.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    structured_response: ControlledProposalResponse | None = None
    raw_response_text: str = Field(default="")
    finish_reason: str | None = None
    usage_metadata: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    provider_error_code: ProposalProviderErrorCode | None = None


class LlmGenerationAttempt(BaseModel):
    """One retry attempt's provenance. Never carries a secret or a header."""

    model_config = ConfigDict(extra="forbid")

    attempt_number: int = Field(ge=1, le=2)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    response_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_error_code: ProposalProviderErrorCode | None = None
    finish_reason: str | None = None
    attempt_fingerprint: str = Field(pattern=SHA256_PATTERN)


class LlmGenerationProvenance(BaseModel):
    """Full, fingerprint-only provenance of one proposal's LLM generation."""

    model_config = ConfigDict(extra="forbid")

    proposal_schema_version: str = Field(min_length=1, max_length=64)
    proposal_policy_version: str = Field(min_length=1, max_length=64)
    system_prompt_template_version: str = Field(min_length=1, max_length=64)
    user_prompt_template_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    proposal_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_role_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraint_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_fingerprint: str = Field(pattern=SHA256_PATTERN)
    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    attempt_count: int = Field(ge=1, le=2)
    attempt_fingerprints: tuple[str, ...]
    raw_response_fingerprint: str = Field(pattern=SHA256_PATTERN)
    structured_response_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str = Field(pattern=SHA256_PATTERN)


class GeneratedCvContentProposal(BaseModel):
    """One controlled LLM proposal for exactly one ``PlannedFactUse``.

    Always ``approval_state=REQUIRES_USER_APPROVAL`` -- never carries
    ``APPROVED``/``REJECTED``/``NOT_REQUIRED`` or any ``ready_for_p6``/
    ``p6_ready`` flag. Not ready for P5.5 or P6 by construction.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    blocked_generation_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
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
    allowed_operation: TransformationOperation
    placement_order: int = Field(ge=0)
    source_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_text: str = Field(min_length=1, max_length=20_000)
    proposal_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraint_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_template_version: str = Field(min_length=1, max_length=64)
    prompt_fingerprint: str = Field(pattern=SHA256_PATTERN)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approval_state: Literal[ProposalApprovalState.REQUIRES_USER_APPROVAL] = (
        ProposalApprovalState.REQUIRES_USER_APPROVAL
    )
    llm_generation_provenance: LlmGenerationProvenance


class BlockedProposalItem(BaseModel):
    """Exactly one eligible ``BlockedGenerationItem`` that P5b could not
    turn into a proposal. Never carries a ``proposal_text``."""

    model_config = ConfigDict(extra="forbid")

    blocked_proposal_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    blocked_generation_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
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
    violation_code: ProposalViolationCode
    provider_error_code: ProposalProviderErrorCode | None = None
    detail: str = Field(min_length=1, max_length=1000)


class CvContentProposalViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: ProposalViolationCode
    detail: str = Field(min_length=1, max_length=1000)
    fact_id: UUID4 | None = None
    planned_fact_use_fingerprint: str | None = None


class CvContentProposalGenerationResult(BaseModel):
    """The single return value of ``generate_controlled_cv_content_proposals``.

    ``ready_for_p55`` is always ``False``: Stage P5.5 is the only stage that
    validates and approves a proposal.
    """

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: ProposalGenerationStatus
    ready_for_p55: Literal[False] = False
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_result_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposal_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposals: tuple[GeneratedCvContentProposal, ...] = Field(default_factory=tuple)
    blocked_proposal_items: tuple[BlockedProposalItem, ...] = Field(default_factory=tuple)
    violations: tuple[CvContentProposalViolation, ...] = Field(default_factory=tuple)
    non_eligible_blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    deterministic_element_fingerprints: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def eligible_blocked_item_count(self) -> int:
        return len(self.proposals) + len(self.blocked_proposal_items)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvContentProposalGenerationResult":
        if self.status == ProposalGenerationStatus.INVALID_INPUT:
            if self.proposals or self.blocked_proposal_items:
                raise ValueError(
                    "INVALID_INPUT must carry no proposals or blocked_proposal_items"
                )
            if not self.violations:
                raise ValueError("INVALID_INPUT must carry at least one violation")
        if self.status == ProposalGenerationStatus.PROPOSED:
            if not self.proposals:
                raise ValueError("PROPOSED must carry at least one proposal")
            if self.blocked_proposal_items:
                raise ValueError("PROPOSED must carry no blocked_proposal_items")
        if self.status == ProposalGenerationStatus.BLOCKED:
            if not self.blocked_proposal_items and not self.violations:
                raise ValueError(
                    "BLOCKED must carry at least one blocked_proposal_item or violation"
                )
        if self.status == ProposalGenerationStatus.PROVIDER_ERROR:
            if not self.blocked_proposal_items:
                raise ValueError("PROVIDER_ERROR must carry at least one blocked_proposal_item")
        return self


class CvContentProposalReplayInput(BaseModel):
    """The exact, proposal-derived fields whose change makes a result stale."""

    model_config = ConfigDict(extra="forbid")

    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_schema_version: str = Field(min_length=1, max_length=64)
    content_policy_version: str = Field(min_length=1, max_length=64)
    p5a_generation_schema_version: str = Field(min_length=1, max_length=64)
    p5a_generation_policy_version: str = Field(min_length=1, max_length=64)
    p5a_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    p5a_replay_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_schema_version: str = Field(min_length=1, max_length=64)
    proposal_policy_version: str = Field(min_length=1, max_length=64)
    fact_payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    eligible_blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    proposal_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_role_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    immutable_constraint_set_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    prompt_template_versions: tuple[str, ...] = Field(default_factory=tuple)
    model_configuration_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    proposal_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    blocked_proposal_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)


class CvContentProposalReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: ProposalFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
