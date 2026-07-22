"""Pydantic contracts for Explicit Provenance Stage P4: the CV content plan.

P4 is a pure, deterministic, in-memory transformation of an existing set of
P3 ``FactSelectionDecision`` records into an explicit, replayable
``CvContentPlan``. It assigns already-approved facts to CV sections, groups
employment facts under a stable employment entity UUID, orders entries and
facts deterministically, and records pending approvals, structural
omissions and decision conflicts -- all through content-derived
fingerprints, never a random UUID or an implicit ``datetime.now()``.

P4 creates no new fact, no new entity, and never changes a P3 decision
outcome. It performs no database I/O, generates no CV text, and never
invokes an LLM. See ``docs/explicit-provenance-stage-p4.md`` for the full
scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.schemas.fact_selection import FactSelectionReasonCode, SelectionDecisionOutcome
from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN, Transferability, TransformationOperation


CV_CONTENT_PLAN_SCHEMA_VERSION = "cv-content-plan-schema-v2"


class CvSection(StrEnum):
    """The closed, versioned set of CV sections P4 can plan content for.

    Deliberately no global ``HEADER`` member: an employment header belongs
    to its own ``CvExperienceEntryPlan``, not to a plan-wide section.
    """

    PROFILE = "PROFILE"
    COMPETENCIES = "COMPETENCIES"
    EXPERIENCE = "EXPERIENCE"
    ACHIEVEMENTS = "ACHIEVEMENTS"
    EDUCATION = "EDUCATION"
    CERTIFICATIONS = "CERTIFICATIONS"
    COURSES = "COURSES"
    LANGUAGES = "LANGUAGES"
    TOOLS_TECHNOLOGIES = "TOOLS_TECHNOLOGIES"


class CvContentPlanStatus(StrEnum):
    VALID = "VALID"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    INVALID = "INVALID"


class CvContentPlanBuildOutcome(StrEnum):
    BUILT = "BUILT"
    INVALID_INPUT_SNAPSHOT = "INVALID_INPUT_SNAPSHOT"


class CvStructuralValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class CvFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"


class ApprovalType(StrEnum):
    """Closed classification of *why* a decision needs human approval.

    Derived only from the closed ``FactSelectionReasonCode`` set already on
    the P3 decision -- never from text or similarity matching. ``OTHER``
    covers any reason-code combination outside the three known gates, so
    classification always terminates deterministically.
    """

    ELIGIBILITY_APPROVAL = "ELIGIBILITY_APPROVAL"
    TRANSFERABILITY_APPROVAL = "TRANSFERABILITY_APPROVAL"
    PERMISSION_APPROVAL = "PERMISSION_APPROVAL"
    OTHER = "OTHER"


class OmissionReasonCode(StrEnum):
    DECISION_BLOCKED = "DECISION_BLOCKED"
    DECISION_EXCLUDED = "DECISION_EXCLUDED"
    DECISION_NOT_RELEVANT = "DECISION_NOT_RELEVANT"
    SECTION_NOT_ALLOWED_FOR_FACT_TYPE = "SECTION_NOT_ALLOWED_FOR_FACT_TYPE"
    BUDGET_NOT_AVAILABLE = "BUDGET_NOT_AVAILABLE"
    #: A SELECTED decision whose fact_type requires employment-entity
    #: grouping (either by ``entity_id`` for a header fact_type, or by
    #: ``employment_scope_entity_id`` for a responsibility/achievement
    #: fact_type) but the required identity is missing or does not resolve
    #: to ``EntityType.EMPLOYMENT`` in the caller-supplied snapshot.
    EMPLOYMENT_SCOPE_REQUIRED = "EMPLOYMENT_SCOPE_REQUIRED"


class ConflictReasonCode(StrEnum):
    CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT = (
        "CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT"
    )
    PROFILE_MULTIPLE_SUMMARY_CANDIDATES = "PROFILE_MULTIPLE_SUMMARY_CANDIDATES"


class EntryOrderSource(StrEnum):
    EXPLICIT_INPUT = "EXPLICIT_INPUT"
    UUID_FALLBACK = "UUID_FALLBACK"


class SectionBudgetStatus(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    WITHIN_BUDGET = "WITHIN_BUDGET"
    EXCEEDS_BUDGET = "EXCEEDS_BUDGET"


class CvContentPlanViolationCode(StrEnum):
    """Closed set of structural problems the P4 validator can report."""

    SOURCE_PARTITION_INCOMPLETE = "SOURCE_PARTITION_INCOMPLETE"
    SOURCE_PARTITION_OVERLAP = "SOURCE_PARTITION_OVERLAP"
    DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
    PLANNED_FACT_USE_DECISION_MISMATCH = "PLANNED_FACT_USE_DECISION_MISMATCH"
    PENDING_APPROVAL_DECISION_MISMATCH = "PENDING_APPROVAL_DECISION_MISMATCH"
    OMITTED_FACT_DECISION_MISMATCH = "OMITTED_FACT_DECISION_MISMATCH"
    DUPLICATE_FACT_IN_PLANNED_CONTENT = "DUPLICATE_FACT_IN_PLANNED_CONTENT"
    EMPLOYMENT_GROUPING_MISMATCH = "EMPLOYMENT_GROUPING_MISMATCH"
    PROFILE_MULTIPLE_FACTS = "PROFILE_MULTIPLE_FACTS"
    SECTION_NOT_ALLOWED_FOR_FACT_TYPE = "SECTION_NOT_ALLOWED_FOR_FACT_TYPE"
    PLACEMENT_ORDER_INVALID = "PLACEMENT_ORDER_INVALID"
    SECTION_ORDER_MISMATCH = "SECTION_ORDER_MISMATCH"
    #: Informational only -- never by itself flips ``structural_status`` to
    #: ``INVALID``. It exists so a caller inspecting ``violations`` can see
    #: *why* ``plan.plan_status`` is ``REQUIRES_REVIEW``.
    SECTION_BUDGET_EXCEEDED = "SECTION_BUDGET_EXCEEDED"
    CONFLICT_MEMBER_NOT_FOUND = "CONFLICT_MEMBER_NOT_FOUND"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    PLAN_STATUS_INCONSISTENT = "PLAN_STATUS_INCONSISTENT"
    #: Stage P4.5a: a decision whose fact_type owns a closed non-employment
    #: entity type (EDUCATION_DEGREE/CERTIFICATION_NAME/COURSE_NAME/
    #: LANGUAGE_NAME) has no entry at all for its ``entity_id`` in the
    #: caller-supplied ``entity_types`` mapping. Always fatal.
    OWNER_ENTITY_TYPE_MISSING = "OWNER_ENTITY_TYPE_MISSING"
    #: Stage P4.5a: same as above, but ``entity_types`` resolves the decision's
    #: ``entity_id`` to an ``EntityType`` other than the one closed policy
    #: requires for this fact_type. Always fatal.
    OWNER_ENTITY_TYPE_MISMATCH = "OWNER_ENTITY_TYPE_MISMATCH"


class PlannedFactUse(BaseModel):
    """One approved fact placed into exactly one CV section slot.

    Never stores generated text, a bullet, a narrative, a company/role
    name, a source reference, a legacy record key, or a list index --
    only the decision-derived placement of an existing fact.
    """

    model_config = ConfigDict(extra="forbid")

    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    requested_operation: TransformationOperation
    allowed_operation: TransformationOperation
    placement_order: int = Field(ge=0)
    employment_scope_entity_id: UUID4 | None
    transferability: Transferability
    permission_ids: tuple[UUID4, ...]
    permission_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    transformation_policy_version: str = Field(min_length=1, max_length=64)
    selection_policy_version: str = Field(min_length=1, max_length=64)
    requires_user_approval: Literal[False] = False
    approval_state: Literal[ApprovalState.NOT_REQUIRED] = ApprovalState.NOT_REQUIRED
    reason_codes: tuple[FactSelectionReasonCode, ...]
    advisory_flags: tuple[str, ...] = Field(default_factory=tuple)


class CvExperienceEntryPlan(BaseModel):
    """One employment entry, grouped only by its stable employment UUID."""

    model_config = ConfigDict(extra="forbid")

    experience_entry_fingerprint: str = Field(pattern=SHA256_PATTERN)
    employment_entity_id: UUID4
    entry_order: int = Field(ge=0)
    entry_order_source: EntryOrderSource
    header_fact_uses: tuple[PlannedFactUse, ...] = Field(default_factory=tuple)
    responsibility_fact_uses: tuple[PlannedFactUse, ...] = Field(default_factory=tuple)
    achievement_fact_uses: tuple[PlannedFactUse, ...] = Field(default_factory=tuple)


class CvExperienceSectionPlan(BaseModel):
    """Discriminated ``EXPERIENCE`` branch: entries only, never flat uses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["EXPERIENCE"] = "EXPERIENCE"
    section: Literal[CvSection.EXPERIENCE] = CvSection.EXPERIENCE
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    skipped_empty: bool
    budget_status: SectionBudgetStatus
    experience_entries: tuple[CvExperienceEntryPlan, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _skipped_empty_matches_entries(self) -> "CvExperienceSectionPlan":
        if self.skipped_empty != (len(self.experience_entries) == 0):
            raise ValueError("skipped_empty must match an empty experience_entries tuple")
        return self


class CvFlatSectionPlan(BaseModel):
    """Discriminated non-``EXPERIENCE`` branch: flat uses only."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["FLAT"] = "FLAT"
    section: CvSection
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    skipped_empty: bool
    budget_status: SectionBudgetStatus
    planned_fact_uses: tuple[PlannedFactUse, ...] = Field(default_factory=tuple)

    @field_validator("section")
    @classmethod
    def _section_is_not_experience(cls, value: CvSection) -> CvSection:
        if value == CvSection.EXPERIENCE:
            raise ValueError("CvFlatSectionPlan.section must not be EXPERIENCE")
        return value

    @model_validator(mode="after")
    def _skipped_empty_matches_uses(self) -> "CvFlatSectionPlan":
        if self.skipped_empty != (len(self.planned_fact_uses) == 0):
            raise ValueError("skipped_empty must match an empty planned_fact_uses tuple")
        return self


CvSectionPlan = Annotated[
    Union[CvExperienceSectionPlan, CvFlatSectionPlan], Field(discriminator="kind")
]


class PendingFactApproval(BaseModel):
    """A P3 ``APPROVAL_REQUIRED`` decision, carried forward without placement."""

    model_config = ConfigDict(extra="forbid")

    pending_approval_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    requested_operation: TransformationOperation
    target_section: CvSection | None
    target_scope: str = Field(min_length=1, max_length=512)
    reason_codes: tuple[FactSelectionReasonCode, ...]
    approval_type: ApprovalType
    approval_state: Literal[ApprovalState.PENDING] = ApprovalState.PENDING


class OmittedFactDecision(BaseModel):
    """A decision structurally excluded from every section by P4 policy."""

    model_config = ConfigDict(extra="forbid")

    omission_fingerprint: str = Field(pattern=SHA256_PATTERN)
    decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    original_outcome: SelectionDecisionOutcome
    omission_reason_code: OmissionReasonCode
    target_scope: str = Field(min_length=1, max_length=512)
    requested_operation: TransformationOperation


class CvContentPlanConflict(BaseModel):
    """Two or more decisions that cannot both be resolved by P4 alone."""

    model_config = ConfigDict(extra="forbid")

    conflict_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    fact_id: UUID4 | None
    entity_id: UUID4 | None
    conflicting_decision_fingerprints: tuple[str, ...] = Field(min_length=2)
    conflict_reason_code: ConflictReasonCode


class SectionBudgetLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: CvSection
    max_facts: int = Field(ge=0)


class CvContentBudgetProfile(BaseModel):
    """Optional, caller-supplied per-section item ceilings.

    P4 defines no default limits: a section absent from ``section_limits``
    is always ``SectionBudgetStatus.NOT_EVALUATED``.
    """

    model_config = ConfigDict(extra="forbid")

    budget_profile_id: str = Field(min_length=1, max_length=255)
    budget_profile_fingerprint: str = Field(pattern=SHA256_PATTERN)
    section_limits: tuple[SectionBudgetLimit, ...] = Field(default_factory=tuple)

    @field_validator("section_limits")
    @classmethod
    def _unique_sections(
        cls, value: tuple[SectionBudgetLimit, ...]
    ) -> tuple[SectionBudgetLimit, ...]:
        sections = [limit.section for limit in value]
        if len(sections) != len(set(sections)):
            raise ValueError("section_limits must not repeat the same CvSection")
        return value


class CvContentPlan(BaseModel):
    """The complete, deterministic, replayable P4 output."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["cv-content-plan-schema-v2"] = CV_CONTENT_PLAN_SCHEMA_VERSION
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_policy_version: str = Field(min_length=1, max_length=64)
    selection_policy_version: str = Field(min_length=1, max_length=64)
    transformation_policy_version: str = Field(min_length=1, max_length=64)
    target_context_id: UUID4
    target_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    job_or_application_id: str = Field(min_length=1, max_length=255)
    plan_status: CvContentPlanStatus
    sections: tuple[CvSectionPlan, ...]
    pending_approvals: tuple[PendingFactApproval, ...] = Field(default_factory=tuple)
    omitted_facts: tuple[OmittedFactDecision, ...] = Field(default_factory=tuple)
    conflicts: tuple[CvContentPlanConflict, ...] = Field(default_factory=tuple)
    source_decision_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    budget_profile_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    career_positioning_snapshot_fingerprint: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )

    @field_validator("sections")
    @classmethod
    def _exactly_one_section_per_cv_section(
        cls, value: tuple[CvSectionPlan, ...]
    ) -> tuple[CvSectionPlan, ...]:
        sections_seen = [entry.section for entry in value]
        if len(sections_seen) != len(set(sections_seen)):
            raise ValueError("sections must contain exactly one entry per CvSection")
        if set(sections_seen) != set(CvSection):
            raise ValueError("sections must cover every CvSection value")
        return value


class CvContentPlanBuildResult(BaseModel):
    """The single return value of ``build_cv_content_plan``."""

    model_config = ConfigDict(extra="forbid")

    outcome: CvContentPlanBuildOutcome
    plan: CvContentPlan | None
    snapshot_violations: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _outcome_matches_plan_presence(self) -> "CvContentPlanBuildResult":
        if self.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT and self.plan is not None:
            raise ValueError("INVALID_INPUT_SNAPSHOT must carry plan=None")
        if self.outcome == CvContentPlanBuildOutcome.BUILT and self.plan is None:
            raise ValueError("BUILT must carry a non-null plan")
        return self


class CvContentPlanReplayInput(BaseModel):
    """The exact, plan-derived fields whose change makes a plan stale.

    Built only from ``PlannedFactUse`` data already stored on the plan (the
    only bucket that carries ``fact_revision``/``fact_content_fingerprint``/
    ``permission_snapshot_fingerprint`` directly); pending and omitted
    decisions are still covered, at decision-fingerprint granularity, via
    ``fact_selection_decision_fingerprints``. ``employment_entry_orders`` is
    a separate explicit snapshot of every ``CvExperienceEntryPlan``'s
    ``(employment_entity_id, entry_order, entry_order_source)``, sorted by
    ``str(employment_entity_id)``, since a reordering of employment entries
    is a staleness-relevant change that is not otherwise represented by the
    per-fact maps above.
    """

    model_config = ConfigDict(extra="forbid")

    target_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    job_or_application_id: str = Field(min_length=1, max_length=255)
    fact_selection_decision_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    fact_revisions: dict[UUID4, int] = Field(default_factory=dict)
    fact_content_fingerprints: dict[UUID4, str] = Field(default_factory=dict)
    fact_types: dict[UUID4, str] = Field(default_factory=dict)
    permission_snapshot_fingerprints: dict[UUID4, str] = Field(default_factory=dict)
    employment_entry_orders: tuple[tuple[UUID4, int, EntryOrderSource], ...] = Field(
        default_factory=tuple
    )
    selection_policy_version: str = Field(min_length=1, max_length=64)
    transformation_policy_version: str = Field(min_length=1, max_length=64)
    content_policy_version: str = Field(min_length=1, max_length=64)
    budget_profile_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    career_positioning_snapshot_fingerprint: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    pending_approval_states: dict[str, ApprovalState] = Field(default_factory=dict)


class CvContentPlanViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: CvContentPlanViolationCode
    detail: str = Field(min_length=1, max_length=1000)
    decision_fingerprint: str | None = None
    fact_id: UUID4 | None = None
    section: CvSection | None = None


class CvContentPlanValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structural_status: CvStructuralValidationStatus
    freshness_status: CvFreshnessStatus
    p5_ready: bool
    violations: tuple[CvContentPlanViolation, ...] = Field(default_factory=tuple)
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
