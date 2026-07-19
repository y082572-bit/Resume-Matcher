"""Validation for explicit decisions on an immutable transformation plan."""

from dataclasses import dataclass

from app.schemas.cv_transformation_approval import (
    CVTransformationPlanApprovalRequest,
    TransformationPlanDecision,
    TransformationPlanApprovalStatus,
)
from app.schemas.cv_transformation_plan import CVTransformationPlan


@dataclass(frozen=True)
class ApprovalValidationError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def plan_source_references(plan: CVTransformationPlan) -> tuple[str, ...]:
    items = (
        plan.summary_plan
        + plan.experience_plan
        + plan.competencies_plan
        + plan.achievements_plan
        + plan.tools_plan
    )
    return tuple(sorted(item.source_reference for item in items))


def validate_approval_request(
    plan: CVTransformationPlan,
    request: CVTransformationPlanApprovalRequest,
) -> tuple[list[TransformationPlanDecision], TransformationPlanApprovalStatus]:
    """Validate references and derive status without changing plan semantics."""

    expected = set(plan_source_references(plan))
    supplied = {item.source_reference for item in request.decisions}
    unknown = sorted(supplied - expected)
    if unknown:
        raise ApprovalValidationError(
            "UNKNOWN_SOURCE_REFERENCE",
            f"Unknown source_reference: {unknown[0]}",
        )

    decisions = sorted(request.decisions, key=lambda item: item.source_reference)
    if not request.submit:
        return decisions, "DRAFT"

    missing = sorted(expected - supplied)
    if missing:
        raise ApprovalValidationError(
            "INCOMPLETE_DECISIONS",
            "Every transformation-plan item requires an explicit decision.",
        )
    if not request.guardrails_acknowledged:
        raise ApprovalValidationError(
            "GUARDRAILS_NOT_ACKNOWLEDGED",
            "Universal guardrails must be acknowledged before submission.",
        )
    if any(item.decision == "REQUEST_REVIEW" for item in decisions):
        return decisions, "REQUIRES_REVIEW"
    return decisions, "APPROVED"
