"""P3 permission gate: only EXACT_COPY/FORMAT_NORMALIZATION are free.

Also covers the P1 remediation gate (``evaluate_fact_type_policy``): the
existing ``TruthTransformationPolicyRegistry`` is the sole authority on which
target_scope/operation a fact_type may ever use, and neither the base-op
shortcut nor an active permission may grant anything it has already denied.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.schemas.fact_selection import FactSelectionReasonCode, SelectionDecisionOutcome, TargetContext
from app.schemas.truth_fact import (
    FactStatus,
    PermissionStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
    TruthPermissionRead,
)
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_permission_snapshot_fingerprint,
    evaluate_fact_type_policy,
    evaluate_permission_grant,
    select_applicable_permissions,
    select_fact,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _permission(
    *,
    fact_id,
    target_scope: str = "EXPERIENCE",
    allowed_operations=(TransformationOperation.CONTROLLED_REPHRASE,),
    status: PermissionStatus = PermissionStatus.ACTIVE,
    valid_from=None,
    valid_until=None,
    constraints_json=None,
    permission_id=None,
    revision: int = 1,
) -> TruthPermissionRead:
    return TruthPermissionRead(
        schema_version="1.0",
        revision=revision,
        content_fingerprint="b" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=NOW if status == PermissionStatus.ARCHIVED else None,
        permission_id=permission_id or uuid4(),
        fact_id=fact_id,
        target_scope=target_scope,
        allowed_operations=list(allowed_operations),
        constraints_json=constraints_json or {},
        status=status,
        approved_by=None,
        approved_at=None,
        valid_from=valid_from,
        valid_until=valid_until,
    )


def _fact(*, fact_type: str, **overrides) -> TruthFactRead:
    values = dict(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type=fact_type,
        value_json={"text": "x"},
        normalized_value_json={"text": "x"},
        status=FactStatus.CONFIRMED,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        source_reference=None,
    )
    values.update(overrides)
    return TruthFactRead(**values)


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


def test_no_permission_allows_exact_copy() -> None:
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome is None
    assert FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION in result.reason_codes


def test_no_permission_allows_format_normalization() -> None:
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.FORMAT_NORMALIZATION,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome is None


def test_no_permission_blocks_controlled_rephrase_to_approval_required() -> None:
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_MISSING_FOR_OPERATION in result.reason_codes


def test_active_permission_grants_the_operation() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id)
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome is None
    assert FactSelectionReasonCode.PERMISSION_ACTIVE_GRANT in result.reason_codes


def test_review_required_permission_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, status=PermissionStatus.REVIEW_REQUIRED)
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_REVIEW_REQUIRED in result.reason_codes


def test_revoked_permission_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, status=PermissionStatus.REVOKED)
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_REVOKED in result.reason_codes


def test_archived_permission_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, status=PermissionStatus.ARCHIVED)
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_ARCHIVED in result.reason_codes


def test_not_yet_valid_permission_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, valid_from=NOW + timedelta(days=1))
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_NOT_YET_VALID in result.reason_codes


def test_expired_permission_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, valid_until=NOW - timedelta(days=1))
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_EXPIRED in result.reason_codes


def test_several_applicable_permissions_pick_the_one_that_grants() -> None:
    fact_id = uuid4()
    revoked = _permission(fact_id=fact_id, status=PermissionStatus.REVOKED)
    active = _permission(fact_id=fact_id, status=PermissionStatus.ACTIVE)
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[revoked, active],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome is None


def test_permission_ids_are_sorted_deterministically() -> None:
    fact_id = uuid4()
    permissions = [_permission(fact_id=fact_id) for _ in range(3)]
    forward = select_applicable_permissions(
        fact_id=fact_id, requested_target_scope="EXPERIENCE", permissions=permissions
    )
    backward = select_applicable_permissions(
        fact_id=fact_id,
        requested_target_scope="EXPERIENCE",
        permissions=list(reversed(permissions)),
    )
    assert [p.permission_id for p in forward] == [p.permission_id for p in backward]
    assert [p.permission_id for p in forward] == sorted(
        (p.permission_id for p in permissions), key=str
    )


def test_permission_snapshot_fingerprint_is_order_independent_and_stable() -> None:
    fact_id = uuid4()
    permissions = [_permission(fact_id=fact_id) for _ in range(2)]
    forward = compute_permission_snapshot_fingerprint(permissions)
    backward = compute_permission_snapshot_fingerprint(list(reversed(permissions)))
    again = compute_permission_snapshot_fingerprint(permissions)
    assert forward == backward == again


def test_permission_snapshot_fingerprint_changes_with_the_permission_set() -> None:
    fact_id = uuid4()
    one_permission = [_permission(fact_id=fact_id)]
    two_permissions = one_permission + [_permission(fact_id=fact_id)]
    assert compute_permission_snapshot_fingerprint(
        one_permission
    ) != compute_permission_snapshot_fingerprint(two_permissions)


def test_constraints_mismatch_does_not_grant() -> None:
    fact_id = uuid4()
    permission = _permission(
        fact_id=fact_id, constraints_json={"target_role_family": "SALES"}
    )
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(target_role_family="ENGINEERING"),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_CONSTRAINTS_MISMATCH in result.reason_codes


def test_constraints_match_grants() -> None:
    fact_id = uuid4()
    permission = _permission(
        fact_id=fact_id, constraints_json={"target_role_family": "ENGINEERING"}
    )
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(target_role_family="ENGINEERING"),
        evaluation_time=NOW,
    )
    assert result.outcome is None


def test_scope_mismatch_excludes_the_permission_from_applicable_set() -> None:
    fact_id = uuid4()
    permission = _permission(fact_id=fact_id, target_scope="SUMMARY")
    applicable = select_applicable_permissions(
        fact_id=fact_id, requested_target_scope="EXPERIENCE", permissions=[permission]
    )
    assert applicable == []

    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_SCOPE_MISMATCH in result.reason_codes


def test_no_automatic_operation_downgrade() -> None:
    """A permission granting only FORMAT_NORMALIZATION must never silently
    authorize CONTROLLED_REPHRASE."""

    fact_id = uuid4()
    permission = _permission(
        fact_id=fact_id, allowed_operations=(TransformationOperation.FORMAT_NORMALIZATION,)
    )
    result = evaluate_permission_grant(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions_for_fact=[permission],
        target_context=_target_context(),
        evaluation_time=NOW,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED


# --- P1 TruthTransformationPolicyRegistry gate (remediation R1) -----------
#
# Real fact_type/target_scope combinations from the P1 registry
# (app/services/truth_policy.py):
#   COMPANY / ROLE / EMPLOYMENT_PERIOD -> {EXACT_COPY, FORMAT_NORMALIZATION}
#     only, scope must be EMPLOYMENT_HEADER.
#   SKILL -> {EXACT_COPY, CONTROLLED_REPHRASE, OMIT, REORDER},
#     scope in {SUMMARY, SKILLS, EXPERIENCE}.


def test_exact_copy_with_allowed_scope_passes_without_permission() -> None:
    fact = _fact(fact_type="SKILL")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.SELECTED


def test_format_normalization_with_allowed_scope_passes_without_permission() -> None:
    fact = _fact(fact_type="COMPANY")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.FORMAT_NORMALIZATION,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.SELECTED


def test_exact_copy_with_disallowed_scope_is_blocked() -> None:
    fact = _fact(fact_type="COMPANY")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert decision.allowed_operation is None
    assert (
        FactSelectionReasonCode.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY
        in decision.reason_codes
    )


def test_format_normalization_with_disallowed_scope_is_blocked() -> None:
    fact = _fact(fact_type="COMPANY")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.FORMAT_NORMALIZATION,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY
        in decision.reason_codes
    )


def test_active_permission_cannot_bypass_disallowed_target_scope() -> None:
    """SKILL is never allowed in PROJECT scope by P1, so an ACTIVE
    permission granting CONTROLLED_REPHRASE there must still be BLOCKED."""

    fact = _fact(fact_type="SKILL")
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="PROJECT",
        allowed_operations=(TransformationOperation.CONTROLLED_REPHRASE,),
    )
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="PROJECT",
        permissions=[permission],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY
        in decision.reason_codes
    )


def test_active_permission_cannot_grant_operation_outside_fact_type_policy() -> None:
    """COMPANY only ever allows EXACT_COPY/FORMAT_NORMALIZATION, so an
    ACTIVE permission granting CONTROLLED_REPHRASE must still be BLOCKED."""

    fact = _fact(fact_type="COMPANY")
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="EMPLOYMENT_HEADER",
        allowed_operations=(TransformationOperation.CONTROLLED_REPHRASE,),
    )
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[permission],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.OPERATION_NOT_ALLOWED_BY_POLICY in decision.reason_codes
    )


def test_missing_fact_type_policy_is_blocked() -> None:
    fact = _fact(fact_type="CUSTOM_UNKNOWN")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_TYPE_POLICY_NOT_FOUND in decision.reason_codes


def test_unknown_target_scope_is_blocked() -> None:
    fact = _fact(fact_type="SKILL")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SOME_SCOPE_NOT_IN_ANY_POLICY",
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY
        in decision.reason_codes
    )


def test_existing_experience_scope_scenario_still_selected() -> None:
    """SKILL + EXPERIENCE + CONTROLLED_REPHRASE with a matching ACTIVE
    permission is a real, policy-allowed combination and must still pass."""

    fact = _fact(fact_type="SKILL")
    permission = _permission(fact_id=fact.fact_id, target_scope="EXPERIENCE")
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.SELECTED


def test_company_employment_header_allowed_but_not_for_skill() -> None:
    """EMPLOYMENT_HEADER is only registered for COMPANY/ROLE/EMPLOYMENT_PERIOD
    -- the same scope must be rejected for a fact_type the registry never
    grants it to."""

    company_fact = _fact(fact_type="COMPANY")
    allowed = select_fact(
        fact=company_fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    assert allowed.decision == SelectionDecisionOutcome.SELECTED

    skill_fact = _fact(fact_type="SKILL")
    denied = select_fact(
        fact=skill_fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    assert denied.decision == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY
        in denied.reason_codes
    )


def test_permission_for_a_different_fact_id_does_not_affect_decision() -> None:
    fact = _fact(fact_type="SKILL")
    other_fact_id = uuid4()
    permission_for_other_fact = _permission(
        fact_id=other_fact_id,
        target_scope="EXPERIENCE",
        allowed_operations=(TransformationOperation.CONTROLLED_REPHRASE,),
    )
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission_for_other_fact],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert (
        FactSelectionReasonCode.PERMISSION_MISSING_FOR_OPERATION in decision.reason_codes
    )


def test_review_required_permission_does_not_bypass_fact_type_policy() -> None:
    fact = _fact(fact_type="SKILL")
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        status=PermissionStatus.REVIEW_REQUIRED,
    )
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_REVIEW_REQUIRED in decision.reason_codes


def test_revoked_and_archived_permission_do_not_bypass_fact_type_policy() -> None:
    fact = _fact(fact_type="SKILL")
    revoked = _permission(
        fact_id=fact.fact_id, target_scope="EXPERIENCE", status=PermissionStatus.REVOKED
    )
    revoked_decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[revoked],
        evaluation_time=NOW,
    )
    assert revoked_decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_REVOKED in revoked_decision.reason_codes

    archived_fact = _fact(fact_type="SKILL")
    archived = _permission(
        fact_id=archived_fact.fact_id,
        target_scope="EXPERIENCE",
        status=PermissionStatus.ARCHIVED,
    )
    archived_decision = select_fact(
        fact=archived_fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[archived],
        evaluation_time=NOW,
    )
    assert archived_decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert FactSelectionReasonCode.PERMISSION_ARCHIVED in archived_decision.reason_codes


def test_evaluate_fact_type_policy_directly_denies_unknown_fact_type() -> None:
    result = evaluate_fact_type_policy(
        fact_type="CUSTOM_UNKNOWN",
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert FactSelectionReasonCode.FACT_TYPE_POLICY_NOT_FOUND in result.reason_codes


def test_evaluate_fact_type_policy_directly_allows_known_combination() -> None:
    result = evaluate_fact_type_policy(
        fact_type="SKILL",
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
    )
    assert result.outcome is None
