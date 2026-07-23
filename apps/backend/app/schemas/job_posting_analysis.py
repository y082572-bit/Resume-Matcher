"""Pydantic contracts for Explicit Provenance Stage PRE-P4, Step 1: Job
Posting Analysis.

This module analyzes a single job posting in complete isolation. It never
sees Marek, his CV, the Truth Library, the Master Resume, prior
applications, other job postings, or the Career Positioning Engine. It
establishes only facts about the *role* -- its objective, responsibilities,
expected business outcomes, required competencies, employer problem
hypotheses, success indicators, operating context, evidence priorities, and
ambiguities.

This module never scores, ranks, or evaluates a candidate. It carries no
``match_score``, ``fit_score``, ``candidate_risk``, ``weaknesses``, or
apply/reject recommendation field -- see
``docs/explicit-provenance-stage-pre-p4.md`` for the full scope boundary
and ``tests/unit/test_job_analysis_policy.py`` /
``tests/integration/test_prep4_job_analysis_thesis_integration.py`` for the
source-level isolation tests that enforce it.

Every model here is a closed (``extra="forbid"``), replayable Pydantic
value object: identity is expressed only through content-derived SHA-256
fingerprints, never a random UUID, a URL, a list index, or an implicit
``datetime.now()``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.truth_entity import SHA256_PATTERN


JOB_POSTING_SNAPSHOT_SCHEMA_VERSION = "job-posting-snapshot-schema-v1"
JOB_EVIDENCE_SEGMENT_SCHEMA_VERSION = "job-evidence-segment-schema-v1"
JOB_ANALYSIS_SCHEMA_VERSION = "job-posting-analysis-schema-v1"
JOB_ANALYSIS_POLICY_VERSION = "job-posting-analysis-policy-v1"
JOB_ANALYSIS_PROMPT_VERSION = "controlled-job-analysis-prompt-v1"
JOB_ANALYSIS_RESPONSE_SCHEMA_VERSION = "controlled-job-analysis-response-v1"
JOB_ANALYSIS_RETRY_POLICY_VERSION = "job-analysis-retry-policy-v1"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AnalysisAssertionBasis(StrEnum):
    EXPLICIT_IN_POSTING = "EXPLICIT_IN_POSTING"
    INFERRED_FROM_POSTING = "INFERRED_FROM_POSTING"


class JobPostingAnalysisStatus(StrEnum):
    ANALYZED = "ANALYZED"
    BLOCKED = "BLOCKED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class JobAnalysisFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class JobAnalysisProviderErrorCode(StrEnum):
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


class CompetencyClass(StrEnum):
    DOMAIN_KNOWLEDGE = "DOMAIN_KNOWLEDGE"
    FUNCTIONAL_SKILL = "FUNCTIONAL_SKILL"
    LEADERSHIP = "LEADERSHIP"
    COMMERCIAL = "COMMERCIAL"
    ANALYTICAL = "ANALYTICAL"
    STAKEHOLDER = "STAKEHOLDER"
    REGULATORY = "REGULATORY"
    LANGUAGE = "LANGUAGE"
    TOOL = "TOOL"


class JobAmbiguityType(StrEnum):
    UNCLEAR_ROLE_SCOPE = "UNCLEAR_ROLE_SCOPE"
    STRATEGIC_OPERATIONAL_BOUNDARY_UNCLEAR = "STRATEGIC_OPERATIONAL_BOUNDARY_UNCLEAR"
    EXPECTED_RESULTS_NOT_EXPLICIT = "EXPECTED_RESULTS_NOT_EXPLICIT"
    TITLE_DUTIES_INCONSISTENT = "TITLE_DUTIES_INCONSISTENT"
    REPORTING_STRUCTURE_NOT_SPECIFIED = "REPORTING_STRUCTURE_NOT_SPECIFIED"
    OTHER_POSTING_AMBIGUITY = "OTHER_POSTING_AMBIGUITY"


class JobEvidenceSegmentType(StrEnum):
    HEADING = "HEADING"
    BULLET_ITEM = "BULLET_ITEM"
    PARAGRAPH = "PARAGRAPH"


class JobPostingSourceType(StrEnum):
    MANUAL_TEXT = "MANUAL_TEXT"
    URL_IMPORT = "URL_IMPORT"
    FILE_IMPORT = "FILE_IMPORT"
    API_IMPORT = "API_IMPORT"
    OTHER = "OTHER"


class OverclaimGuardStatus(StrEnum):
    PASSED = "PASSED"
    BLOCKED_UNSUPPORTED_ABSOLUTE_CLAIM = "BLOCKED_UNSUPPORTED_ABSOLUTE_CLAIM"


class JobAnalysisItemKind(StrEnum):
    ROLE_OBJECTIVE = "ROLE_OBJECTIVE"
    RESPONSIBILITY = "RESPONSIBILITY"
    OUTCOME = "OUTCOME"
    COMPETENCY = "COMPETENCY"
    HYPOTHESIS = "HYPOTHESIS"
    SUCCESS_INDICATOR = "SUCCESS_INDICATOR"
    OPERATING_CONTEXT = "OPERATING_CONTEXT"
    EVIDENCE_PRIORITY = "EVIDENCE_PRIORITY"
    AMBIGUITY = "AMBIGUITY"
    PROVIDER_LEVEL = "PROVIDER_LEVEL"


class JobAnalysisViolationCode(StrEnum):
    EMPTY_POSTING_TEXT = "EMPTY_POSTING_TEXT"
    SNAPSHOT_FINGERPRINT_MISMATCH = "SNAPSHOT_FINGERPRINT_MISMATCH"
    DUPLICATE_SEGMENT_ID = "DUPLICATE_SEGMENT_ID"
    UNKNOWN_SEGMENT = "UNKNOWN_SEGMENT"
    SEGMENT_NOT_OWNED_BY_SNAPSHOT = "SEGMENT_NOT_OWNED_BY_SNAPSHOT"
    SEGMENT_FINGERPRINT_MISMATCH = "SEGMENT_FINGERPRINT_MISMATCH"
    DUPLICATE_RESPONSE_ITEM_ID = "DUPLICATE_RESPONSE_ITEM_ID"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    EMPTY_STATEMENT = "EMPTY_STATEMENT"
    OBJECTIVE_EQUALS_ROLE_TITLE = "OBJECTIVE_EQUALS_ROLE_TITLE"
    UNSUPPORTED_NUMERIC_OUTCOME = "UNSUPPORTED_NUMERIC_OUTCOME"
    OVERCLAIM_UNSUPPORTED_HYPOTHESIS = "OVERCLAIM_UNSUPPORTED_HYPOTHESIS"
    FORBIDDEN_CANDIDATE_SCORING_FIELD = "FORBIDDEN_CANDIDATE_SCORING_FIELD"
    NO_ROLE_OBJECTIVE = "NO_ROLE_OBJECTIVE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    INVALID_RESPONSE_JSON = "INVALID_RESPONSE_JSON"
    RESPONSE_SCHEMA_MISMATCH = "RESPONSE_SCHEMA_MISMATCH"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    TOKEN_LIMIT = "TOKEN_LIMIT"
    UNSUPPORTED_FINISH_REASON = "UNSUPPORTED_FINISH_REASON"
    MODEL_CONFIGURATION_MISMATCH = "MODEL_CONFIGURATION_MISMATCH"


class JobAnalysisProhibitionCode(StrEnum):
    NO_CANDIDATE_SCORING = "NO_CANDIDATE_SCORING"
    NO_MATCH_OR_FIT_SCORE = "NO_MATCH_OR_FIT_SCORE"
    NO_APPLY_OR_REJECT_RECOMMENDATION = "NO_APPLY_OR_REJECT_RECOMMENDATION"
    NO_CANDIDATE_RISK_OR_WEAKNESS = "NO_CANDIDATE_RISK_OR_WEAKNESS"
    NO_CANDIDATE_COMPARISON = "NO_CANDIDATE_COMPARISON"


#: The complete, closed set of prohibitions declared in every job-analysis
#: prompt request -- never partial, never caller-configurable.
ALL_JOB_ANALYSIS_PROHIBITIONS: tuple[JobAnalysisProhibitionCode, ...] = tuple(
    JobAnalysisProhibitionCode
)


def _validate_segment_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    import re

    pattern = re.compile(SHA256_PATTERN)
    for item in value:
        if not pattern.match(item):
            raise ValueError(f"segment id {item!r} is not a well-formed sha256 identity")
    return tuple(sorted(set(value)))


# ---------------------------------------------------------------------------
# Snapshot and evidence segmentation
# ---------------------------------------------------------------------------


class JobPostingSnapshot(BaseModel):
    """A closed, content-addressed snapshot of one job posting.

    ``posting_id`` and ``source_url`` are correlation references only --
    neither participates in ``snapshot_fingerprint``, and neither may be
    used as a substitute for content-derived identity or freshness.
    """

    model_config = ConfigDict(extra="forbid")

    posting_id: str | None = Field(default=None, max_length=255)
    source_type: JobPostingSourceType
    source_url: str | None = Field(default=None, max_length=2048)
    company_name: str | None = Field(default=None, max_length=255)
    role_title: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=255)
    employment_type: str | None = Field(default=None, max_length=255)
    canonical_text: str = Field(min_length=1, max_length=50_000)
    source_language: str = Field(min_length=1, max_length=32)
    snapshot_schema_version: Literal["job-posting-snapshot-schema-v1"] = (
        JOB_POSTING_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)


class JobEvidenceSegment(BaseModel):
    """One deterministically-derived slice of a ``JobPostingSnapshot``'s
    canonical text. Never produced by an LLM, never dependent on
    ``datetime.now()`` or Python's randomized ``hash()``.
    """

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(pattern=SHA256_PATTERN)
    segment_type: JobEvidenceSegmentType
    source_text: str = Field(min_length=1, max_length=5_000)
    normalized_text: str = Field(min_length=1, max_length=5_000)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    section_hint: str | None = Field(default=None, max_length=255)
    segment_order: int = Field(ge=0)
    segment_fingerprint: str = Field(pattern=SHA256_PATTERN)


# ---------------------------------------------------------------------------
# Accepted analysis domain objects
# ---------------------------------------------------------------------------


class RoleObjective(BaseModel):
    """Answers: why does this role exist in the organization?"""

    model_config = ConfigDict(extra="forbid")

    objective_id: str = Field(pattern=SHA256_PATTERN)
    concise_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    objective_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class RoleResponsibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    responsibility_id: str = Field(pattern=SHA256_PATTERN)
    statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    priority_order: int = Field(ge=0)
    responsibility_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class ExpectedBusinessOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_id: str = Field(pattern=SHA256_PATTERN)
    expected_change: str = Field(min_length=1, max_length=2_000)
    affected_business_area: str = Field(min_length=1, max_length=255)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    outcome_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class RequiredRoleCompetency(BaseModel):
    """Never carries whether Marek has this competency -- see
    ``docs/explicit-provenance-stage-pre-p4.md`` for the scope boundary."""

    model_config = ConfigDict(extra="forbid")

    competency_id: str = Field(pattern=SHA256_PATTERN)
    competency_name: str = Field(min_length=1, max_length=255)
    competency_class: CompetencyClass
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    competency_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class EmployerProblemHypothesis(BaseModel):
    """A labeled hypothesis, never a stated internal fact about the
    employer -- always ``assertion_basis=INFERRED_FROM_POSTING``."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(pattern=SHA256_PATTERN)
    problem_statement: str = Field(min_length=1, max_length=2_000)
    affected_business_area: str = Field(min_length=1, max_length=255)
    observable_signals_in_posting: tuple[str, ...] = Field(min_length=1)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    assertion_basis: Literal[AnalysisAssertionBasis.INFERRED_FROM_POSTING] = (
        AnalysisAssertionBasis.INFERRED_FROM_POSTING
    )
    overclaim_guard_status: OverclaimGuardStatus
    hypothesis_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class RoleSuccessIndicator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success_indicator_id: str = Field(pattern=SHA256_PATTERN)
    indicator_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    success_indicator_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class RoleOperatingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operating_context_id: str = Field(pattern=SHA256_PATTERN)
    context_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    operating_context_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class RoleEvidencePriority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_priority_id: str = Field(pattern=SHA256_PATTERN)
    priority_theme: str = Field(min_length=1, max_length=255)
    rationale: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(min_length=1)
    priority_rank: int = Field(ge=1)
    evidence_priority_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)


class JobPostingAmbiguity(BaseModel):
    """Describes only ambiguity in the posting itself -- never a candidate
    weakness, a candidacy risk, or an application recommendation."""

    model_config = ConfigDict(extra="forbid")

    ambiguity_id: str = Field(pattern=SHA256_PATTERN)
    ambiguity_type: JobAmbiguityType
    description: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)
    explicit_absence_of_information: bool = False
    ambiguity_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @field_validator("supporting_evidence_segment_ids")
    @classmethod
    def _canonical_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_segment_ids(value)

    @model_validator(mode="after")
    def _evidence_or_explicit_absence(self) -> "JobPostingAmbiguity":
        if not self.supporting_evidence_segment_ids and not self.explicit_absence_of_information:
            raise ValueError(
                "an ambiguity must carry evidence segments or "
                "explicit_absence_of_information=True"
            )
        return self


# ---------------------------------------------------------------------------
# Model configuration, context, prompt request/response
# ---------------------------------------------------------------------------


class JobAnalysisModelConfiguration(BaseModel):
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


class JobAnalysisContext(BaseModel):
    """Only the data allowed to influence one job-analysis call. Never
    carries Marek's CV, facts, Truth Library, Master Resume, prior
    applications, other postings, or Career Positioning output."""

    model_config = ConfigDict(extra="forbid")

    analysis_policy_version: str = Field(min_length=1, max_length=64)
    prompt_template_version: str = Field(min_length=1, max_length=64)
    retry_policy_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    model_configuration: JobAnalysisModelConfiguration


class ControlledJobAnalysisRequest(BaseModel):
    """The complete, bounded request handed to the job-analysis adapter.
    Never carries a candidate profile, CV text, or scoring instruction."""

    model_config = ConfigDict(extra="forbid")

    prompt_template_version: str = Field(min_length=1, max_length=64)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_language: str = Field(min_length=1, max_length=32)
    role_title: str | None = Field(default=None, max_length=500)
    company_name: str | None = Field(default=None, max_length=255)
    segments: tuple[JobEvidenceSegment, ...] = Field(min_length=1)
    prohibitions: tuple[JobAnalysisProhibitionCode, ...] = Field(
        default=ALL_JOB_ANALYSIS_PROHIBITIONS
    )
    response_schema_version: str = Field(min_length=1, max_length=64)

    @field_validator("prohibitions")
    @classmethod
    def _prohibitions_are_complete(
        cls, value: tuple[JobAnalysisProhibitionCode, ...]
    ) -> tuple[JobAnalysisProhibitionCode, ...]:
        if set(value) != set(ALL_JOB_ANALYSIS_PROHIBITIONS):
            raise ValueError("prohibitions must always carry the complete closed set")
        return tuple(sorted(value, key=lambda item: item.value))


class ResponseRoleObjectiveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    concise_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseResponsibilityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)
    priority_order: int = Field(ge=0)


class ResponseOutcomeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    expected_change: str = Field(min_length=1, max_length=2_000)
    affected_business_area: str = Field(min_length=1, max_length=255)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseCompetencyItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    competency_name: str = Field(min_length=1, max_length=255)
    competency_class: CompetencyClass
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseHypothesisItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    problem_statement: str = Field(min_length=1, max_length=2_000)
    affected_business_area: str = Field(min_length=1, max_length=255)
    observable_signals_in_posting: tuple[str, ...] = Field(default_factory=tuple)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseSuccessIndicatorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    indicator_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseOperatingContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    context_statement: str = Field(min_length=1, max_length=2_000)
    assertion_basis: AnalysisAssertionBasis
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)


class ResponseEvidencePriorityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    priority_theme: str = Field(min_length=1, max_length=255)
    rationale: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)
    priority_rank: int = Field(ge=1)


class ResponseAmbiguityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_item_id: str = Field(min_length=1, max_length=255)
    ambiguity_type: JobAmbiguityType
    description: str = Field(min_length=1, max_length=2_000)
    supporting_evidence_segment_ids: tuple[str, ...] = Field(default_factory=tuple)
    explicit_absence_of_information: bool = False


class ControlledJobAnalysisResponse(BaseModel):
    """The closed response shape a ``ControlledJobPostingAnalysisAdapter``
    must return as structured JSON. Never a candidate score, an apply/reject
    recommendation, or free text outside these closed fields."""

    model_config = ConfigDict(extra="forbid")

    response_schema_version: str = Field(min_length=1, max_length=64)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_objective: ResponseRoleObjectiveItem | None = None
    responsibilities: tuple[ResponseResponsibilityItem, ...] = Field(default_factory=tuple)
    outcomes: tuple[ResponseOutcomeItem, ...] = Field(default_factory=tuple)
    competencies: tuple[ResponseCompetencyItem, ...] = Field(default_factory=tuple)
    hypotheses: tuple[ResponseHypothesisItem, ...] = Field(default_factory=tuple)
    success_indicators: tuple[ResponseSuccessIndicatorItem, ...] = Field(default_factory=tuple)
    operating_context: tuple[ResponseOperatingContextItem, ...] = Field(default_factory=tuple)
    evidence_priorities: tuple[ResponseEvidencePriorityItem, ...] = Field(default_factory=tuple)
    ambiguities: tuple[ResponseAmbiguityItem, ...] = Field(default_factory=tuple)


class JobAnalysisAdapterResponse(BaseModel):
    """The adapter's raw return value for one attempt. ``raw_response_text``
    is transient -- only its fingerprint is ever persisted into
    ``JobAnalysisProvenance``/``JobAnalysisAttempt``. ``provider_request_id``
    is metadata only and never participates in any fingerprint or identity
    check."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    structured_response: ControlledJobAnalysisResponse | None = None
    raw_response_text: str = Field(default="")
    finish_reason: str | None = None
    usage_metadata: dict[str, int] = Field(default_factory=dict)
    provider_request_id: str | None = None
    provider_error_code: JobAnalysisProviderErrorCode | None = None


class JobAnalysisAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_number: int = Field(ge=1, le=2)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    response_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provider_error_code: JobAnalysisProviderErrorCode | None = None
    finish_reason: str | None = None
    attempt_fingerprint: str = Field(pattern=SHA256_PATTERN)


class JobAnalysisProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    provider: str = Field(min_length=1, max_length=255)
    model_identifier: str = Field(min_length=1, max_length=255)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_fingerprint: str = Field(pattern=SHA256_PATTERN)
    attempt_count: int = Field(ge=1, le=2)
    attempt_fingerprints: tuple[str, ...]
    raw_response_fingerprint: str = Field(pattern=SHA256_PATTERN)
    structured_response_fingerprint: str = Field(pattern=SHA256_PATTERN)


class BlockedJobAnalysisItem(BaseModel):
    """Exactly one response item (or provider-level failure) that did not
    become an accepted analysis item. Never carries an accepted domain
    fingerprint."""

    model_config = ConfigDict(extra="forbid")

    blocked_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    item_kind: JobAnalysisItemKind
    source_response_item_id: str | None = Field(default=None, max_length=255)
    violation_code: JobAnalysisViolationCode
    provider_error_code: JobAnalysisProviderErrorCode | None = None
    detail: str = Field(min_length=1, max_length=1_000)


class JobAnalysisResultViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: JobAnalysisViolationCode
    detail: str = Field(min_length=1, max_length=1_000)


class JobPostingAnalysisResult(BaseModel):
    """The single return value of ``build_job_posting_analysis``.
    ``ready_for_candidacy_thesis`` is always computed internally, never
    caller-supplied."""

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: JobPostingAnalysisStatus
    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    snapshot_schema_version: str = Field(min_length=1, max_length=64)
    analysis_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    role_objective: RoleObjective | None = None
    responsibilities: tuple[RoleResponsibility, ...] = Field(default_factory=tuple)
    outcomes: tuple[ExpectedBusinessOutcome, ...] = Field(default_factory=tuple)
    competencies: tuple[RequiredRoleCompetency, ...] = Field(default_factory=tuple)
    hypotheses: tuple[EmployerProblemHypothesis, ...] = Field(default_factory=tuple)
    success_indicators: tuple[RoleSuccessIndicator, ...] = Field(default_factory=tuple)
    operating_context: tuple[RoleOperatingContext, ...] = Field(default_factory=tuple)
    evidence_priorities: tuple[RoleEvidencePriority, ...] = Field(default_factory=tuple)
    ambiguities: tuple[JobPostingAmbiguity, ...] = Field(default_factory=tuple)
    blocked_items: tuple[BlockedJobAnalysisItem, ...] = Field(default_factory=tuple)
    violations: tuple[JobAnalysisResultViolation, ...] = Field(default_factory=tuple)
    provenance: JobAnalysisProvenance | None = None
    ready_for_candidacy_thesis: bool

    @model_validator(mode="after")
    def _ready_requires_analyzed_and_objective(self) -> "JobPostingAnalysisResult":
        if self.ready_for_candidacy_thesis:
            if self.status != JobPostingAnalysisStatus.ANALYZED:
                raise ValueError("ready_for_candidacy_thesis requires status=ANALYZED")
            if self.role_objective is None:
                raise ValueError("ready_for_candidacy_thesis requires a role_objective")
        return self


class JobAnalysisReplayInput(BaseModel):
    """The exact, result-derived fields whose change makes a job-analysis
    result stale."""

    model_config = ConfigDict(extra="forbid")

    posting_fingerprint: str = Field(pattern=SHA256_PATTERN)
    canonical_text_fingerprint: str = Field(pattern=SHA256_PATTERN)
    snapshot_schema_version: str = Field(min_length=1, max_length=64)
    evidence_segment_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    analysis_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    model_configuration_fingerprint: str = Field(pattern=SHA256_PATTERN)
    prompt_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str = Field(min_length=1, max_length=64)
    objective_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    responsibility_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    outcome_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    competency_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    hypothesis_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    success_indicator_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    operating_context_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    evidence_priority_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    ambiguity_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    result_fingerprint: str = Field(pattern=SHA256_PATTERN)


class JobAnalysisReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: JobAnalysisFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
