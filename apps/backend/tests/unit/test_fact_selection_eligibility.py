"""P3 eligibility gate: derived only from status, use_in_cv, requires_approval."""

from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.fact_selection import (
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_fact import (
    FactStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
)
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    evaluate_eligibility,
    select_fact,
)


def _fact(
    *,
    status: FactStatus,
    use_in_cv: bool,
    requires_approval: bool,
    transferability: Transferability = Transferability.GLOBAL_TRANSFERABLE,
    employment_scope_entity_id=None,
) -> TruthFactRead:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TruthFactRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=now,
        updated_at=now,
        archived_at=now if status == FactStatus.ARCHIVED else None,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type="SKILL",
        value_json={"text": "Python"},
        normalized_value_json={"text": "Python"},
        status=status,
        use_in_cv=use_in_cv,
        requires_approval=requires_approval,
        transferability=transferability,
        employment_scope_entity_id=employment_scope_entity_id,
        source_reference=None,
    )


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-1",
        target_role_title="Senior Engineer",
        target_role_family="ENGINEERING",
        target_seniority="SENIOR",
        target_organization_or_industry="SOFTWARE",
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def test_confirmed_use_in_cv_no_approval_is_a_selected_candidate() -> None:
    result = evaluate_eligibility(
        status=FactStatus.CONFIRMED, use_in_cv=True, requires_approval=False
    )
    assert result.outcome is None


def test_review_required_forces_approval() -> None:
    result = evaluate_eligibility(
        status=FactStatus.REVIEW_REQUIRED, use_in_cv=True, requires_approval=False
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED in result.reason_codes


def test_requires_approval_flag_forces_approval() -> None:
    result = evaluate_eligibility(
        status=FactStatus.CONFIRMED, use_in_cv=True, requires_approval=True
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.FACT_REQUIRES_APPROVAL in result.reason_codes


def test_draft_is_blocked() -> None:
    result = evaluate_eligibility(
        status=FactStatus.DRAFT, use_in_cv=False, requires_approval=True
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_STATUS_DRAFT_NOT_ELIGIBLE in result.reason_codes


def test_rejected_is_blocked() -> None:
    result = evaluate_eligibility(
        status=FactStatus.REJECTED, use_in_cv=True, requires_approval=False
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_STATUS_REJECTED in result.reason_codes


def test_archived_is_blocked() -> None:
    result = evaluate_eligibility(
        status=FactStatus.ARCHIVED, use_in_cv=True, requires_approval=False
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_STATUS_ARCHIVED in result.reason_codes


def test_use_in_cv_disabled_is_excluded() -> None:
    result = evaluate_eligibility(
        status=FactStatus.CONFIRMED, use_in_cv=False, requires_approval=False
    )
    assert result.outcome == SelectionDecisionOutcome.EXCLUDED
    assert FactSelectionReasonCode.FACT_USE_IN_CV_DISABLED in result.reason_codes


def test_unknown_status_fails_closed() -> None:
    result = evaluate_eligibility(
        status="SOME_FUTURE_STATUS",  # type: ignore[arg-type]
        use_in_cv=True,
        requires_approval=False,
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_STATUS_UNKNOWN in result.reason_codes


def test_eligibility_runs_before_transferability_and_permission() -> None:
    """A DRAFT fact must be BLOCKED for its status even with a permissive
    transferability and an active, matching permission -- eligibility is
    evaluated first and short-circuits the pipeline."""

    fact = _fact(
        status=FactStatus.DRAFT,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
    )
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_STATUS_DRAFT_NOT_ELIGIBLE in decision.reason_codes
