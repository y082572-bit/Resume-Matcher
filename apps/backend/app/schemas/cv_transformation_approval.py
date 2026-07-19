"""Public contracts for explicit CV transformation-plan decisions."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TransformationPlanDecisionValue = Literal["ACCEPT", "REJECT", "REQUEST_REVIEW"]
TransformationPlanApprovalStatus = Literal[
    "DRAFT", "APPROVED", "REQUIRES_REVIEW", "SUPERSEDED"
]


class TransformationPlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_reference: str = Field(
        ...,
        pattern=(
            r"^(resume|positioning):"
            r"(summary|experience|competencies|achievements|tools):\d{4}$"
        ),
    )
    decision: TransformationPlanDecisionValue


class CVTransformationPlanApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    decisions: list[TransformationPlanDecision] = Field(default_factory=list)
    guardrails_acknowledged: bool = False
    submit: bool = False

    @model_validator(mode="after")
    def validate_decision_references(self) -> "CVTransformationPlanApprovalRequest":
        references = [item.source_reference for item in self.decisions]
        if len(references) != len(set(references)):
            raise ValueError("decision source_reference values must be unique")
        return self


class CVTransformationPlanApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(..., min_length=1)
    plan_version: Literal["1.1"] = "1.1"
    plan_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    resume_id: str = Field(..., min_length=1)
    job_id: str = Field(..., min_length=1)
    status: TransformationPlanApprovalStatus
    decisions: list[TransformationPlanDecision]
    guardrails_acknowledged: bool
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_response(self) -> "CVTransformationPlanApproval":
        references = [item.source_reference for item in self.decisions]
        if references != sorted(set(references)):
            raise ValueError("approval decisions must be unique and sorted")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        has_review_request = any(
            item.decision == "REQUEST_REVIEW" for item in self.decisions
        )
        if self.status == "APPROVED":
            if not self.guardrails_acknowledged:
                raise ValueError("APPROVED requires guardrails acknowledgment")
            if has_review_request:
                raise ValueError("APPROVED cannot contain REQUEST_REVIEW")
        if self.status == "REQUIRES_REVIEW":
            if not self.guardrails_acknowledged:
                raise ValueError("REQUIRES_REVIEW requires guardrails acknowledgment")
            if not has_review_request:
                raise ValueError("REQUIRES_REVIEW requires REQUEST_REVIEW")
        return self
