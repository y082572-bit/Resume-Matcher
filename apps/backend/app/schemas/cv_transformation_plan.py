"""Public response contract for the deterministic CV transformation preview."""

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.career_positioning import PositioningStrategy, RoleLevel


# Derived from the canonical engine enums so schema additions cannot silently drift.
RoleLevelName = Literal[*tuple(level.name for level in RoleLevel)]
PositioningStrategyName = Literal[*tuple(strategy.value for strategy in PositioningStrategy)]

CVTransformationAction = Literal[
    "KEEP", "EMPHASIZE", "DEEMPHASIZE", "REPHRASE", "OMIT", "HUMAN_REVIEW"
]
CVTransformationSection = Literal[
    "SUMMARY", "EXPERIENCE", "COMPETENCIES", "ACHIEVEMENTS", "TOOLS"
]
EvidenceStrength = Literal["NONE", "WEAK", "MODERATE", "STRONG"]
EvidenceAllowedOperation = Literal[
    "KEEP_EXISTING", "EMPHASIZE_EXISTING", "REPHRASE_EXISTING"
]
ProhibitedClaimCode = Literal[
    "PNL_WITHOUT_EVIDENCE",
    "BUDGET_WITHOUT_EVIDENCE",
    "BOARD_WITHOUT_EVIDENCE",
    "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE",
    "MANAGING_MANAGERS_WITHOUT_EVIDENCE",
    "TEAM_SIZE_WITHOUT_EVIDENCE",
    "TECHNICAL_SKILL_WITHOUT_EVIDENCE",
    "LANGUAGE_LEVEL_WITHOUT_EVIDENCE",
    "CERTIFICATION_WITHOUT_EVIDENCE",
    "QUANTIFIED_RESULT_WITHOUT_EVIDENCE",
]
EvidenceClaimCode = Literal[
    *get_args(ProhibitedClaimCode),
    "STRATEGY_RESPONSIBILITY_EVIDENCE",
    "ORGANIZATIONAL_SCALE_EVIDENCE",
    "COMPETENCY_EVIDENCE",
]

PROHIBITED_CLAIM_CODES: tuple[str, ...] = tuple(sorted(get_args(ProhibitedClaimCode)))

_SOURCE_REFERENCE_PATTERN = (
    r"^(resume|positioning):(summary|experience|competencies|achievements|tools):\d{4}$"
)


class CVTransformationPlanItem(BaseModel):
    """A UI-readable transformation instruction bound to a structural reference."""

    model_config = ConfigDict(extra="forbid")

    action: CVTransformationAction
    section: CVTransformationSection
    source_reference: str = Field(..., pattern=_SOURCE_REFERENCE_PATTERN)
    display_label: str = Field(..., min_length=1, max_length=120)
    reason_code: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    evidence_strength: EvidenceStrength
    human_review_required: bool

    @model_validator(mode="after")
    def validate_item(self) -> "CVTransformationPlanItem":
        if not self.display_label.strip():
            raise ValueError("display_label cannot be whitespace-only")
        reference_section = self.source_reference.split(":")[1]
        if reference_section != self.section.lower():
            raise ValueError("source_reference section must match item section")
        if self.action == "HUMAN_REVIEW" and not self.human_review_required:
            raise ValueError("HUMAN_REVIEW action requires human_review_required=true")
        return self


class EvidencePermission(BaseModel):
    """Permission to reuse one concrete existing plan element, never to generate new facts."""

    model_config = ConfigDict(extra="forbid")

    claim_code: EvidenceClaimCode
    source_reference: str = Field(..., pattern=_SOURCE_REFERENCE_PATTERN)
    evidence_strength: Literal["MODERATE", "STRONG"]
    allowed_operation: EvidenceAllowedOperation
    human_review_required: bool


class ProhibitedClaim(BaseModel):
    """A universal rule forbidding new claims without item-scoped evidence."""

    model_config = ConfigDict(extra="forbid")

    code: ProhibitedClaimCode
    reason_code: Literal["EVIDENCE_REQUIRED_FOR_NEW_CLAIM"] = (
        "EVIDENCE_REQUIRED_FOR_NEW_CLAIM"
    )
    reason: str = Field(..., min_length=1)
    human_review_required: Literal[True] = True


class TransformationSourceSummary(BaseModel):
    """Non-sensitive aggregate provenance for the plan."""

    model_config = ConfigDict(extra="forbid")

    approved_truth_items_count: int = Field(..., ge=0)
    resume_items_count: int = Field(..., ge=0)
    job_requirements_count: int = Field(..., ge=0)
    career_positioning_schema_version: Literal["1.0"] = "1.0"


class CVTransformationPlan(BaseModel):
    """Deterministic, read-only instructions for a future CV generator."""

    model_config = ConfigDict(extra="forbid")

    plan_version: Literal["1.1"] = "1.1"
    resume_id: str = Field(..., min_length=1)
    job_id: str = Field(..., min_length=1)
    generated_at: datetime
    detected_job_level: RoleLevelName
    candidate_profile_level: RoleLevelName
    positioning_strategy: PositioningStrategyName
    requires_human_review: bool
    summary_plan: list[CVTransformationPlanItem] = Field(default_factory=list)
    experience_plan: list[CVTransformationPlanItem] = Field(default_factory=list)
    competencies_plan: list[CVTransformationPlanItem] = Field(default_factory=list)
    achievements_plan: list[CVTransformationPlanItem] = Field(default_factory=list)
    tools_plan: list[CVTransformationPlanItem] = Field(default_factory=list)
    evidence_permissions: list[EvidencePermission] = Field(default_factory=list)
    prohibited_claims: list[ProhibitedClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_summary: TransformationSourceSummary

    @model_validator(mode="after")
    def validate_plan(self) -> "CVTransformationPlan":
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        item_lists = (
            self.summary_plan,
            self.experience_plan,
            self.competencies_plan,
            self.achievements_plan,
            self.tools_plan,
        )
        items = [item for section_items in item_lists for item in section_items]
        references = [item.source_reference for item in items]
        if len(references) != len(set(references)):
            raise ValueError("source_reference values must be unique")
        if any(item.human_review_required for item in items):
            if not self.requires_human_review:
                raise ValueError("item review requirement must propagate to the plan")

        permission_keys = [
            (permission.claim_code, permission.source_reference, permission.allowed_operation)
            for permission in self.evidence_permissions
        ]
        if permission_keys != sorted(set(permission_keys)):
            raise ValueError("evidence_permissions must be unique and sorted")
        unknown_references = {
            permission.source_reference for permission in self.evidence_permissions
        } - set(references)
        if unknown_references:
            raise ValueError("evidence permission must reference an existing plan item")
        if any(permission.human_review_required for permission in self.evidence_permissions):
            if not self.requires_human_review:
                raise ValueError("permission review requirement must propagate to the plan")

        codes = [item.code for item in self.prohibited_claims]
        if tuple(codes) != PROHIBITED_CLAIM_CODES:
            raise ValueError("prohibited_claims must contain all universal guardrails")
        if self.limitations != sorted(set(self.limitations)):
            raise ValueError("limitations must be unique and sorted")
        return self
