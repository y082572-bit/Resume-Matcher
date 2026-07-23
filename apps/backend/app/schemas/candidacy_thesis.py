"""Pydantic contracts for Explicit Provenance Stage PRE-P4, Step 2:
Candidacy Thesis.

This module answers exactly one question: *why should Marek specifically
be invited to interview for this role?* It consumes a fresh
``JobPostingAnalysisResult`` plus only Marek's *approved* fact payloads --
never pending facts, never rejected facts, never the full Truth Library,
never the Master Resume, never other job postings, and never the Career
Positioning Engine.

This module never scores, ranks, or evaluates a candidate. It carries no
``match_score``, ``fit_score``, ``candidate_risk``, ``weaknesses``, or
apply/reject recommendation field, and it never promises a future outcome
or guarantees success -- see
``docs/explicit-provenance-stage-pre-p4.md`` for the full scope boundary.

Every model here is a closed (``extra="forbid"``), replayable Pydantic
value object: identity is expressed only through content-derived SHA-256
fingerprints, never a random UUID, a URL, a list index, or an implicit
``datetime.now()``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN


CANDIDACY_THESIS_SCHEMA_VERSION = "candidacy-thesis-schema-v1"
CANDIDACY_THESIS_POLICY_VERSION = "candidacy-thesis-policy-v1"
CANDIDACY_THESIS_PROMPT_VERSION = "controlled-candidacy-thesis-prompt-v1"
CANDIDACY_THESIS_RESPONSE_SCHEMA_VERSION = "controlled-candidacy-thesis-response-v1"
CANDIDACY_THESIS_RETRY_POLICY_VERSION = "candidacy-thesis-retry-policy-v1"
ROLE_STRATEGY_CONTEXT_SCHEMA_VERSION = "role-strategy-context-schema-v1"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CandidacyThesisStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class CandidacyThesisFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class CandidacyThesisProviderErrorCode(StrEnum):
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


class PositioningLevel(StrEnum):
    """Independent of the Career Positioning Engine -- never imports or
    reuses its level literals."""

    EXECUTIVE = "EXECUTIVE"
    SENIOR_LEADERSHIP = "SENIOR_LEADERSHIP"
    MANAGEMENT = "MANAGEMENT"
    EXPERT = "EXPERT"
    SPECIALIST = "SPECIALIST"
    OPERATIONAL = "OPERATIONAL"


class RoleEvidenceCategory(StrEnum):
    SCALE_OF_RESPONSIBILITY = "SCALE_OF_RESPONSIBILITY"
    BUSINESS_RESULT = "BUSINESS_RESULT"
    TRANSFORMATION = "TRANSFORMATION"
    TEAM_LEADERSHIP = "TEAM_LEADERSHIP"
    DISTRIBUTION_DEVELOPMENT = "DISTRIBUTION_DEVELOPMENT"
    PARTNERSHIP_MANAGEMENT = "PARTNERSHIP_MANAGEMENT"
    PRODUCT_IMPLEMENTATION = "PRODUCT_IMPLEMENTATION"
    PROCESS_IMPROVEMENT = "PROCESS_IMPROVEMENT"
    STAKEHOLDER_MANAGEMENT = "STAKEHOLDER_MANAGEMENT"
    REGULATORY_OR_RISK_RESPONSIBILITY = "REGULATORY_OR_RISK_RESPONSIBILITY"
    CUSTOMER_EXPERIENCE = "CUSTOMER_EXPERIENCE"
    TRAINING_AND_CAPABILITY_BUILDING = "TRAINING_AND_CAPABILITY_BUILDING"


class CandidacyThesisItemKind(StrEnum):
    EVIDENCE_MAPPING = "EVIDENCE_MAPPING"
    STRATEGIC_CONTRIBUTION = "STRATEGIC_CONTRIBUTION"
    THESIS_STATEMENT = "THESIS_STATEMENT"
    PROVIDER_LEVEL = "PROVIDER_LEVEL"


class CandidacyThesisViolationCode(StrEnum):
    JOB_ANALYSIS_NOT_READY = "JOB_ANALYSIS_NOT_READY"
    JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH = "JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH"
    JOB_ANALYSIS_STALE = "JOB_ANALYSIS_STALE"
    JOB_ANALYSIS_FRESHNESS_NOT_VERIFIED = "JOB_ANALYSIS_FRESHNESS_NOT_VERIFIED"
    PAYLOAD_SET_FINGERPRINT_MISMATCH = "PAYLOAD_SET_FINGERPRINT_MISMATCH"
    UNKNOWN_FACT = "UNKNOWN_FACT"
    MISSING_FACT = "MISSING_FACT"
    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    DUPLICATE_FACT_IDENTITY = "DUPLICATE_FACT_IDENTITY"
    UNKNOWN_HYPOTHESIS = "UNKNOWN_HYPOTHESIS"
    UNKNOWN_ROLE_OBJECTIVE = "UNKNOWN_ROLE_OBJECTIVE"
    DUPLICATE_EVIDENCE_MAPPING = "DUPLICATE_EVIDENCE_MAPPING"
    DUPLICATE_RESPONSE_ITEM_ID = "DUPLICATE_RESPONSE_ITEM_ID"
    CLAIM_WITHOUT_PROVENANCE = "CLAIM_WITHOUT_PROVENANCE"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    FUTURE_OUTCOME_GUARANTEE = "FUTURE_OUTCOME_GUARANTEE"
    FORBIDDEN_CANDIDATE_SCORING_FIELD = "FORBIDDEN_CANDIDATE_SCORING_FIELD"
    POSITIONING_LEVEL_MISMATCH = "POSITIONING_LEVEL_MISMATCH"
    EMPTY_STATEMENT = "EMPTY_STATEMENT"
    NO_THESIS_STATEMENT = "NO_THESIS_STATEMENT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    INVALID_RESPONSE_JSON = "INVALID_RESPONSE_JSON"
    RESPONSE_SCHEMA_MISMATCH = "RESPONSE_SCHEMA_MISMATCH"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    TOKEN_LIMIT = "TOKEN_LIMIT"
    UNSUPPORTED_FINISH_REASON = "UNSUPPORTED_FINISH_REASON"


class CandidacyThesisProhibitionCode(StrEnum):
    NO_CANDIDATE_SCORING = "NO_CANDIDATE_SCORING"
    NO_MATCH_OR_FIT_SCORE = "NO_MATCH_OR_FIT_SCORE"
    NO_CANDIDATE_RISK_OR_WEAKNESS = "NO_CANDIDATE_RISK_OR_WEAKNESS"
    NO_APPLICATION_RECOMMENDATION = "NO_APPLICATION_RECOMMENDATION"
    NO_FUTURE_OUTCOME_GUARANTEE = "NO_FUTURE_OUTCOME_GUARANTEE"
    NO_NEW_FACT_CREATION = "NO_NEW_FACT_CREATION"
    NO_CANDIDATE_COMPARISON = "NO_CANDIDATE_COMPARISON"


ALL_CANDIDACY_THESIS_PROHIBITIONS: tuple[CandidacyThesisProhibitionCode, ...] = tuple(
    CandidacyThesisProhibitionCode
)


def _fact_identity_key(entity_id: UUID4, fact_id: UUID4) -> str:
    return f"{entity_id}:{fact_id}"


# ---------------------------------------------------------------------------
# Model configuration and context
# ---------------------------------------------------------------------------


class CandidacyThesisModelConfiguration(BaseModel):
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


class CandidacyThesisContext(BaseModel):
    """Only the data allowed to influence one candidacy-thesis call. Never
    carries pending facts, rejected facts, the full Truth Library, the
    Master Resume, other postings, a match score, or a risk score."""

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=1, max_length=32)
    thesis_prompt_version: str = Field(min_length=1, max_length=64)
    thesis_policy_version: str = Field(min_length=1, max_length=64)
    retry_policy_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    requested_positioning_level: PositioningLevel
    priority_evidence_categories: tuple[RoleEvidenceCategory, ...] = Field(min_length=1)
    model_configuration: CandidacyThesisModelConfiguration

    @field_validator("priority_evidence_categories")
    @classmethod
    def _dedupe_preserving_order(
        cls, value: tuple[RoleEvidenceCategory, ...]
    ) -> tuple[RoleEvidenceCategory, ...]:
        seen: list[RoleEvidenceCategory] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Approved-fact-anchored evidence and thesis content
# ---------------------------------------------------------------------------


class CandidacyEvidenceMapping(BaseModel):
    """Links exactly one approved fact to exactly one employer problem
    hypothesis or role objective. Never a free-text claim without this
    provenance."""

    model_config = ConfigDict(extra="forbid")

    evidence_mapping_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    linked_hypothesis_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    linked_role_objective_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    role_evidence_category: RoleEvidenceCategory
    narrative_link: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def _exactly_one_link(self) -> "CandidacyEvidenceMapping":
        links = (self.linked_hypothesis_fingerprint, self.linked_role_objective_fingerprint)
        if sum(1 for link in links if link is not None) != 1:
            raise ValueError(
                "an evidence mapping must link to exactly one of "
                "linked_hypothesis_fingerprint / linked_role_objective_fingerprint"
            )
        return self


class StrategicContributionStatement(BaseModel):
    """One bounded, evidence-anchored contribution claim. Never promises a
    future outcome or guarantees success."""

    model_config = ConfigDict(extra="forbid")

    contribution_fingerprint: str = Field(pattern=SHA256_PATTERN)
    statement: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_mapping_fingerprints: tuple[str, ...] = Field(min_length=1)

    @field_validator("supporting_evidence_mapping_fingerprints")
    @classmethod
    def _canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class CandidacyThesisStatement(BaseModel):
    """The single overall thesis: why Marek should be invited to
    interview. Never claims he is the ideal candidate, never guarantees an
    outcome, never introduces a new fact."""

    model_config = ConfigDict(extra="forbid")

    thesis_fingerprint: str = Field(pattern=SHA256_PATTERN)
    statement: str = Field(min_length=1, max_length=4_000)
    role_objective_fingerprint: str = Field(pattern=SHA256_PATTERN)
    referenced_hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    supporting_evidence_mapping_fingerprints: tuple[str, ...] = Field(min_length=1)

    @field_validator("referenced_hypothesis_fingerprints", "supporting_evidence_mapping_fingerprints")
    @classmethod
    def _canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


# ---------------------------------------------------------------------------
# Prompt request/response
# ---------------------------------------------------------------------------


class ThesisPromptHypothesis(BaseModel):
    """A reduced, deterministic projection of one accepted
    ``EmployerProblemHypothesis`` -- carries only what the thesis prompt is
    allowed to see."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_fingerprint: str = Field(pattern=SHA256_PATTERN)
    problem_statement: str = Field(min_length=1, max_length=2_000)
    affected_business_area: str = Field(min_length=1, max_length=255)


class ThesisPromptApprovedFact(BaseModel):
    """A reduced, deterministic projection of one ``ApprovedFactPayloadSnapshot``
    -- carries only what the thesis prompt is allowed to see. Never a
    pending or rejected fact, never the full Truth Library."""

    model_config = ConfigDict(extra="forbid")

    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    value_json: object


class ControlledCandidacyThesisRequest(BaseModel):
    """The complete, bounded request handed to the candidacy-thesis
    adapter. Never carries pending facts, rejected facts, the full Truth
    Library, the Master Resume, other postings, or a scoring instruction."""

    model_config = ConfigDict(extra="forbid")

    prompt_template_version: str = Field(min_length=1, max_length=64)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_objective_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_objective_statement: str = Field(min_length=1, max_length=2_000)
    hypotheses: tuple[ThesisPromptHypothesis, ...] = Field(default_factory=tuple)
    approved_facts: tuple[ThesisPromptApprovedFact, ...] = Field(min_length=1)
    requested_positioning_level: PositioningLevel
    priority_evidence_categories: tuple[RoleEvidenceCategory, ...] = Field(min_length=1)
    prohibitions: tuple[CandidacyThesisProhibitionCode, ...] = Field(
        default=ALL_CANDIDACY_THESIS_PROHIBITIONS
    )
    response_schema_version: str = Field(min_length=1, max_length=64)

    @field_validator("prohibitions")
    @classmethod
    def _prohibitions_are_complete(
        cls, value: tuple[CandidacyThesisProhibitionCode, ...]
    ) -> tuple[CandidacyThesisProhibitionCode, ...]:
        if set(value) != set(ALL_CANDIDACY_THESIS_PROHIBITIONS):
            raise ValueError("prohibitions must always carry the complete closed set")
        return tuple(sorted(value, key=lambda item: item.value))


class ResponseEvidenceMappingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    entity_id: UUID4
    fact_id: UUID4
    fact_revision: int = Field(ge=1)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    linked_hypothesis_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    linked_role_objective_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    role_evidence_category: RoleEvidenceCategory
    narrative_link: str = Field(min_length=1, max_length=1_000)


class ResponseStrategicContributionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    statement: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_mapping_response_item_ids: tuple[str, ...] = Field(min_length=1)


class ResponseThesisStatementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    statement: str = Field(min_length=1, max_length=4_000)
    referenced_hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    supporting_evidence_mapping_response_item_ids: tuple[str, ...] = Field(min_length=1)


class ControlledCandidacyThesisResponse(BaseModel):
    """The closed response shape a ``ControlledCandidacyThesisAdapter`` must
    return as structured JSON. Never a candidate score, an apply/reject
    recommendation, or a guaranteed future outcome."""

    model_config = ConfigDict(extra="forbid")

    response_schema_version: str = Field(min_length=1, max_length=64)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    acknowledged_positioning_level: PositioningLevel
    target_narrative_emphasis: tuple[RoleEvidenceCategory, ...] = Field(min_length=1)
    evidence_mappings: tuple[ResponseEvidenceMappingItem, ...] = Field(default_factory=tuple)
    strategic_contribution_statements: tuple[ResponseStrategicContributionItem, ...] = Field(
        default_factory=tuple
    )
    thesis_statement: ResponseThesisStatementItem | None = None


class CandidacyThesisAdapterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    structured_response: ControlledCandidacyThesisResponse | None = None
    raw_response_text: str = Field(default="")
    finish_reason: str | None = None
    usage_metadata: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    provider_error_code: CandidacyThesisProviderErrorCode | None = None


class CandidacyThesisAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_number: int = Field(ge=1, le=2)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    response_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_error_code: CandidacyThesisProviderErrorCode | None = None
    finish_reason: str | None = None
    attempt_fingerprint: str = Field(pattern=SHA256_PATTERN)


class CandidacyThesisProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_replay_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_fingerprint: str = Field(pattern=SHA256_PATTERN)
    attempt_count: int = Field(ge=1, le=2)
    attempt_fingerprints: tuple[str, ...]
    raw_response_fingerprint: str = Field(pattern=SHA256_PATTERN)
    structured_response_fingerprint: str = Field(pattern=SHA256_PATTERN)


class BlockedCandidacyThesisItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocked_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    item_kind: CandidacyThesisItemKind
    source_response_item_id: str | None = Field(default=None, max_length=255)
    violation_code: CandidacyThesisViolationCode
    provider_error_code: CandidacyThesisProviderErrorCode | None = None
    detail: str = Field(min_length=1, max_length=1_000)


class CandidacyThesisResultViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: CandidacyThesisViolationCode
    detail: str = Field(min_length=1, max_length=1_000)


class RoleStrategyContext(BaseModel):
    """The single, closed output of PRE-P4 core. Never an arbitrary dict,
    never the raw analysis as free text, never the thesis as an
    unverified string, never a match score or candidate risk.

    Not yet consumed by P4 in this stage -- see
    ``docs/explicit-provenance-stage-pre-p4.md`` for the deferred,
    mandatory PRE-P4-to-P4 integration gate.
    """

    model_config = ConfigDict(extra="forbid")

    context_schema_version: Literal["role-strategy-context-schema-v1"] = (
        ROLE_STRATEGY_CONTEXT_SCHEMA_VERSION
    )
    job_analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    job_analysis_replay_fingerprint: str = Field(pattern=SHA256_PATTERN)
    candidacy_thesis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    candidacy_thesis_replay_fingerprint: str = Field(pattern=SHA256_PATTERN)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_objective_fingerprint: str = Field(pattern=SHA256_PATTERN)
    employer_problem_hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    priority_evidence_categories: tuple[RoleEvidenceCategory, ...] = Field(min_length=1)
    positioning_level: PositioningLevel
    target_narrative_emphasis: tuple[RoleEvidenceCategory, ...] = Field(min_length=1)
    approved_supporting_fact_fingerprints: tuple[str, ...] = Field(min_length=1)
    context_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator(
        "employer_problem_hypothesis_fingerprints", "approved_supporting_fact_fingerprints"
    )
    @classmethod
    def _canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class CandidacyThesisResult(BaseModel):
    """The single return value of ``build_candidacy_thesis``.
    ``ready_for_p4`` is always computed internally, never caller-supplied."""

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: CandidacyThesisStatus
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    analysis_replay_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    payload_set_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    thesis_statement: CandidacyThesisStatement | None = None
    evidence_mappings: tuple[CandidacyEvidenceMapping, ...] = Field(default_factory=tuple)
    strategic_contribution_statements: tuple[StrategicContributionStatement, ...] = Field(
        default_factory=tuple
    )
    employer_problem_hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    role_objective_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    priority_evidence_categories: tuple[RoleEvidenceCategory, ...] = Field(default_factory=tuple)
    positioning_level: PositioningLevel | None = None
    target_narrative_emphasis: tuple[RoleEvidenceCategory, ...] = Field(default_factory=tuple)
    provenance: CandidacyThesisProvenance | None = None
    blocked_items: tuple[BlockedCandidacyThesisItem, ...] = Field(default_factory=tuple)
    violations: tuple[CandidacyThesisResultViolation, ...] = Field(default_factory=tuple)
    role_strategy_context: RoleStrategyContext | None = None
    ready_for_p4: bool

    @model_validator(mode="after")
    def _ready_requires_thesis_and_context(self) -> "CandidacyThesisResult":
        if self.ready_for_p4:
            if self.status != CandidacyThesisStatus.READY:
                raise ValueError("ready_for_p4 requires status=READY")
            if self.thesis_statement is None or self.role_strategy_context is None:
                raise ValueError(
                    "ready_for_p4 requires a thesis_statement and a role_strategy_context"
                )
        return self


class CandidacyThesisReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_analysis_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    job_analysis_replay_fingerprint: str = Field(pattern=SHA256_PATTERN)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approved_payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    supporting_fact_payload_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    role_objective_fingerprint: str = Field(pattern=SHA256_PATTERN)
    positioning_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    evidence_priority_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    thesis_statement_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    evidence_mapping_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    strategic_contribution_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_version: str = Field(min_length=1, max_length=64)
    blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    result_fingerprint: str = Field(pattern=SHA256_PATTERN)


class CandidacyThesisReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: CandidacyThesisFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
