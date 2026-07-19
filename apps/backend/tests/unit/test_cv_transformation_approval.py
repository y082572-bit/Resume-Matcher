"""Unit contract matrix for Stage 10B approval and fingerprint semantics."""

from datetime import datetime, timezone
import inspect

import pytest
from pydantic import ValidationError

from app.schemas.cv_transformation_approval import (
    CVTransformationPlanApproval,
    CVTransformationPlanApprovalRequest,
    TransformationPlanDecision,
)
from app.schemas.cv_transformation_plan import (
    CVTransformationPlan,
    CVTransformationPlanItem,
    EvidencePermission,
    PROHIBITED_CLAIM_CODES,
    ProhibitedClaim,
    TransformationSourceSummary,
)
from app.services.cv_transformation_approval import (
    ApprovalValidationError,
    plan_source_references,
    validate_approval_request,
)
from app.services.cv_transformation_plan import compute_plan_fingerprint
from app.services.project_metrics import (
    MetricEntityType,
    MetricEventInput,
    MetricEventSource,
    MetricEventType,
)


NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)


def make_plan(*, human_review=False):
    item = CVTransformationPlanItem(
        action="HUMAN_REVIEW" if human_review else "KEEP",
        section="SUMMARY",
        source_reference="resume:summary:0001",
        display_label="Professional summary",
        reason_code="REVIEW" if human_review else "KEEP_FACTS",
        reason="Review this item." if human_review else "Keep verified facts.",
        evidence_strength="WEAK" if human_review else "STRONG",
        human_review_required=human_review,
    )
    plan = CVTransformationPlan(
        plan_fingerprint="0" * 64,
        resume_id="resume-example",
        job_id="job-example",
        generated_at=NOW,
        detected_job_level="MANAGER",
        candidate_profile_level="MANAGER",
        positioning_strategy="BALANCED",
        requires_human_review=human_review,
        summary_plan=[item],
        prohibited_claims=[
            ProhibitedClaim(code=code, reason=f"Guardrail {code}")
            for code in PROHIBITED_CLAIM_CODES
        ],
        source_summary=TransformationSourceSummary(
            approved_truth_items_count=1,
            resume_items_count=1,
            job_requirements_count=1,
        ),
    )
    return plan.model_copy(update={"plan_fingerprint": compute_plan_fingerprint(plan)})


def request(*, decisions=None, acknowledged=True, submit=True):
    return CVTransformationPlanApprovalRequest(
        plan_fingerprint=make_plan().plan_fingerprint,
        decisions=decisions
        if decisions is not None
        else [
            TransformationPlanDecision(
                source_reference="resume:summary:0001", decision="ACCEPT"
            )
        ],
        guardrails_acknowledged=acknowledged,
        submit=submit,
    )


def test_01_fingerprint_ignores_generated_at():
    first = make_plan()
    second = first.model_copy(update={"generated_at": NOW.replace(hour=11)})
    assert compute_plan_fingerprint(first) == compute_plan_fingerprint(second)


def test_02_fingerprint_is_stable_for_identical_plan():
    assert make_plan().plan_fingerprint == make_plan().plan_fingerprint


def test_03_action_change_changes_fingerprint():
    plan = make_plan()
    changed = plan.model_copy(
        update={
            "summary_plan": [
                plan.summary_plan[0].model_copy(update={"action": "EMPHASIZE"})
            ]
        }
    )
    assert compute_plan_fingerprint(plan) != compute_plan_fingerprint(changed)


def test_04_permission_change_changes_fingerprint():
    plan = make_plan()
    changed = plan.model_copy(
        update={
            "evidence_permissions": [
                EvidencePermission(
                    claim_code="COMPETENCY_EVIDENCE",
                    source_reference="resume:summary:0001",
                    evidence_strength="STRONG",
                    allowed_operation="KEEP_EXISTING",
                    human_review_required=False,
                )
            ]
        }
    )
    assert compute_plan_fingerprint(plan) != compute_plan_fingerprint(changed)


def test_05_guardrail_change_changes_fingerprint():
    plan = make_plan()
    claims = list(plan.prohibited_claims)
    claims[0] = claims[0].model_copy(update={"reason": "Updated public guardrail"})
    changed = plan.model_copy(update={"prohibited_claims": claims})
    assert compute_plan_fingerprint(plan) != compute_plan_fingerprint(changed)


def test_06_complete_decisions_are_approved():
    _, status = validate_approval_request(make_plan(), request())
    assert status == "APPROVED"


def test_07_missing_decision_is_rejected_on_submit():
    with pytest.raises(ApprovalValidationError, match="Every transformation-plan item"):
        validate_approval_request(make_plan(), request(decisions=[]))


def test_08_duplicate_source_reference_is_rejected():
    decision = {"source_reference": "resume:summary:0001", "decision": "ACCEPT"}
    with pytest.raises(ValidationError, match="must be unique"):
        CVTransformationPlanApprovalRequest(
            plan_fingerprint="a" * 64,
            decisions=[decision, decision],
        )


def test_09_unknown_source_reference_is_rejected():
    unknown = TransformationPlanDecision(
        source_reference="resume:summary:9999", decision="ACCEPT"
    )
    with pytest.raises(ApprovalValidationError, match="Unknown source_reference"):
        validate_approval_request(make_plan(), request(decisions=[unknown]))


@pytest.mark.parametrize(
    ("decision", "status"),
    [("ACCEPT", "APPROVED"), ("REJECT", "APPROVED"), ("REQUEST_REVIEW", "REQUIRES_REVIEW")],
)
def test_10_to_12_decision_semantics(decision, status):
    selected = TransformationPlanDecision(
        source_reference="resume:summary:0001", decision=decision
    )
    result, actual = validate_approval_request(make_plan(), request(decisions=[selected]))
    assert actual == status
    assert result[0].decision == decision


def test_13_approved_status_requires_submit():
    _, status = validate_approval_request(make_plan(), request(submit=True))
    assert status == "APPROVED"


def test_14_request_review_status_is_derived():
    selected = TransformationPlanDecision(
        source_reference="resume:summary:0001", decision="REQUEST_REVIEW"
    )
    _, status = validate_approval_request(make_plan(), request(decisions=[selected]))
    assert status == "REQUIRES_REVIEW"


def test_15_draft_allows_partial_decisions():
    decisions, status = validate_approval_request(
        make_plan(), request(decisions=[], acknowledged=False, submit=False)
    )
    assert decisions == [] and status == "DRAFT"


def test_16_fingerprint_is_lowercase_sha256():
    fingerprint = make_plan().plan_fingerprint
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()


def test_17_decisions_are_sorted_deterministically():
    plan = make_plan().model_copy(
        update={
            "tools_plan": [
                CVTransformationPlanItem(
                    action="KEEP",
                    section="TOOLS",
                    source_reference="resume:tools:0001",
                    display_label="Python",
                    reason_code="KEEP_TOOL",
                    reason="Keep verified tool.",
                    evidence_strength="STRONG",
                    human_review_required=False,
                )
            ]
        }
    )
    supplied = [
        TransformationPlanDecision(source_reference="resume:tools:0001", decision="REJECT"),
        TransformationPlanDecision(source_reference="resume:summary:0001", decision="ACCEPT"),
    ]
    decisions, _ = validate_approval_request(plan, request(decisions=supplied))
    assert [item.source_reference for item in decisions] == sorted(plan_source_references(plan))


def test_18_guardrails_false_blocks_submission():
    with pytest.raises(ApprovalValidationError, match="guardrails"):
        validate_approval_request(make_plan(), request(acknowledged=False))


def test_19_human_review_without_decision_is_rejected():
    with pytest.raises(ApprovalValidationError, match="Every transformation-plan item"):
        validate_approval_request(make_plan(human_review=True), request(decisions=[]))


def test_20_approval_service_has_no_llm_dependency():
    source = inspect.getsource(
        __import__("app.services.cv_transformation_approval", fromlist=["x"])
    ).lower()
    assert all(name not in source for name in ("litellm", "openai", "ollama", "generate_resume"))


def test_21_resume_change_changes_fingerprint():
    plan = make_plan()
    first = compute_plan_fingerprint(plan, resume={"summary": "Version one"})
    second = compute_plan_fingerprint(plan, resume={"summary": "Version two"})
    assert first != second


def test_22_job_change_changes_fingerprint():
    plan = make_plan()
    assert compute_plan_fingerprint(plan, job={"content": "A"}) != compute_plan_fingerprint(
        plan, job={"content": "B"}
    )


def test_23_truth_change_changes_fingerprint_without_exposing_truth():
    plan = make_plan()
    private = {"id": "private-truth-id", "fact": "A"}
    fingerprint = compute_plan_fingerprint(plan, truth_library=private)
    assert fingerprint != compute_plan_fingerprint(plan, truth_library={**private, "fact": "B"})
    assert "private-truth-id" not in fingerprint


def test_24_source_order_does_not_change_fingerprint():
    plan = make_plan()
    first = {"items": [{"name": "A"}, {"name": "B"}]}
    second = {"items": list(reversed(first["items"]))}
    assert compute_plan_fingerprint(plan, resume=first) == compute_plan_fingerprint(
        plan, resume=second
    )


def test_25_plan_version_is_fingerprinted():
    plan = make_plan()
    changed = plan.model_copy(update={"plan_version": "future"})
    assert compute_plan_fingerprint(plan) != compute_plan_fingerprint(changed)


def test_26_source_references_include_every_plan_section_item():
    assert plan_source_references(make_plan()) == ("resume:summary:0001",)


def test_27_draft_preserves_explicit_reject():
    selected = TransformationPlanDecision(
        source_reference="resume:summary:0001", decision="REJECT"
    )
    decisions, status = validate_approval_request(
        make_plan(), request(decisions=[selected], submit=False)
    )
    assert status == "DRAFT" and decisions[0].decision == "REJECT"


def test_28_empty_draft_is_valid():
    _, status = validate_approval_request(make_plan(), request(decisions=[], submit=False))
    assert status == "DRAFT"


def test_29_validation_does_not_mutate_plan():
    plan = make_plan()
    before = plan.model_dump()
    validate_approval_request(plan, request())
    assert plan.model_dump() == before


def test_30_request_cannot_override_action():
    with pytest.raises(ValidationError, match="Extra inputs"):
        TransformationPlanDecision.model_validate(
            {
                "source_reference": "resume:summary:0001",
                "decision": "ACCEPT",
                "action": "EMPHASIZE",
            }
        )


def test_31_approval_response_rejects_private_fields():
    payload = {
        "approval_id": "approval-example",
        "plan_fingerprint": "a" * 64,
        "resume_id": "resume-example",
        "job_id": "job-example",
        "status": "DRAFT",
        "decisions": [],
        "guardrails_acknowledged": False,
        "created_at": NOW,
        "updated_at": NOW,
        "metadata_json": {"private": True},
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        CVTransformationPlanApproval.model_validate(payload)


def test_32_approval_response_requires_sorted_decisions():
    payload = {
        "approval_id": "approval-example",
        "plan_fingerprint": "a" * 64,
        "resume_id": "resume-example",
        "job_id": "job-example",
        "status": "DRAFT",
        "decisions": [
            {"source_reference": "resume:tools:0001", "decision": "ACCEPT"},
            {"source_reference": "resume:summary:0001", "decision": "REJECT"},
        ],
        "guardrails_acknowledged": False,
        "created_at": NOW,
        "updated_at": NOW,
    }
    with pytest.raises(ValidationError, match="unique and sorted"):
        CVTransformationPlanApproval.model_validate(payload)


def test_33_request_review_never_removes_prohibited_claims():
    plan = make_plan(human_review=True)
    before = [item.code for item in plan.prohibited_claims]
    selected = TransformationPlanDecision(
        source_reference="resume:summary:0001", decision="REQUEST_REVIEW"
    )
    validate_approval_request(plan, request(decisions=[selected]))
    assert [item.code for item in plan.prohibited_claims] == before


def test_34_invalid_reference_structure_is_rejected():
    with pytest.raises(ValidationError):
        TransformationPlanDecision(source_reference="private-id", decision="ACCEPT")


def test_35_metric_event_accepts_neutral_approval_type():
    event = MetricEventInput(
        event_id="event-example",
        operation_key="transformation_plan_decision:approval-example",
        event_type=MetricEventType.TRANSFORMATION_PLAN_DECIDED,
        entity_type=MetricEntityType.JOB,
        entity_id="job-example",
        lifecycle_id="job-lifecycle-example",
        source=MetricEventSource.USER,
    )
    assert event.event_type == MetricEventType.TRANSFORMATION_PLAN_DECIDED


def test_36_metric_event_rejects_wrong_entity_type():
    with pytest.raises(ValueError, match="only allowed for JOB"):
        MetricEventInput(
            event_id="event-example",
            operation_key="transformation_plan_decision:approval-example",
            event_type=MetricEventType.TRANSFORMATION_PLAN_DECIDED,
            entity_type=MetricEntityType.RESUME,
            entity_id="resume-example",
            lifecycle_id="resume-lifecycle-example",
        )


def approval_response_payload(**updates):
    payload = {
        "approval_id": "approval-example",
        "plan_fingerprint": "a" * 64,
        "resume_id": "resume-example",
        "job_id": "job-example",
        "status": "APPROVED",
        "decisions": [
            {"source_reference": "resume:summary:0001", "decision": "ACCEPT"}
        ],
        "guardrails_acknowledged": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return payload


def test_r1_approved_requires_guardrails_acknowledgment():
    with pytest.raises(ValidationError, match="APPROVED requires guardrails"):
        CVTransformationPlanApproval.model_validate(
            approval_response_payload(guardrails_acknowledged=False)
        )


def test_r1_approved_rejects_request_review_decision():
    with pytest.raises(ValidationError, match="APPROVED cannot contain REQUEST_REVIEW"):
        CVTransformationPlanApproval.model_validate(
            approval_response_payload(
                decisions=[
                    {
                        "source_reference": "resume:summary:0001",
                        "decision": "REQUEST_REVIEW",
                    }
                ]
            )
        )


def test_r1_requires_review_requires_guardrails_acknowledgment():
    with pytest.raises(ValidationError, match="REQUIRES_REVIEW requires guardrails"):
        CVTransformationPlanApproval.model_validate(
            approval_response_payload(
                status="REQUIRES_REVIEW",
                decisions=[
                    {
                        "source_reference": "resume:summary:0001",
                        "decision": "REQUEST_REVIEW",
                    }
                ],
                guardrails_acknowledged=False,
            )
        )


def test_r1_requires_review_requires_request_review_decision():
    with pytest.raises(ValidationError, match="REQUIRES_REVIEW requires REQUEST_REVIEW"):
        CVTransformationPlanApproval.model_validate(
            approval_response_payload(status="REQUIRES_REVIEW")
        )
