"""P4 replay/staleness and the fail-closed structural validator.

Synthetic facts and entities only.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.cv_content_plan import (
    CV_CONTENT_PLAN_SCHEMA_VERSION,
    CvContentPlan,
    CvContentPlanStatus,
    CvContentPlanViolationCode,
    CvFreshnessStatus,
    CvSection,
    CvStructuralValidationStatus,
    EntryOrderSource,
)
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import (
    is_cv_content_plan_stale,
    replay_input_from_plan,
    stale_fields,
)
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, compute_target_context_fingerprint


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-1",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(*, target_context, fact_type, target_scope, salt="", **overrides) -> FactSelectionDecision:
    fact_id = overrides.pop("fact_id", uuid4())
    entity_id = overrides.pop("entity_id", uuid4())
    decision_outcome = overrides.pop("decision", SelectionDecisionOutcome.SELECTED)
    requested_operation = overrides.pop("requested_operation", TransformationOperation.EXACT_COPY)
    fact_revision = overrides.pop("fact_revision", 1)
    fact_content_fingerprint = overrides.pop("fact_content_fingerprint", "b" * 64)
    allowed_operation = requested_operation if decision_outcome == SelectionDecisionOutcome.SELECTED else None
    reason_codes = overrides.pop(
        "reason_codes",
        (FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,)
        if decision_outcome == SelectionDecisionOutcome.SELECTED
        else (FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED,),
    )
    seed = f"{fact_id}{entity_id}{fact_type}{target_scope}{decision_outcome}{fact_content_fingerprint}{fact_revision}{salt}{uuid4()}"
    return FactSelectionDecision(
        decision_fingerprint=hashlib.sha256(seed.encode()).hexdigest(),
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=TRANSFORMATION_POLICY_VERSION,
        target_context_id=target_context.target_context_id,
        target_context_fingerprint=compute_target_context_fingerprint(target_context),
        person_entity_id=target_context.person_entity_id,
        entity_id=entity_id,
        fact_id=fact_id,
        fact_type=fact_type,
        fact_revision=fact_revision,
        fact_content_fingerprint=fact_content_fingerprint,
        decision=decision_outcome,
        reason_codes=reason_codes,
        requested_operation=requested_operation,
        requested_target_scope=target_scope,
        allowed_operation=allowed_operation,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=overrides.pop("employment_scope_entity_id", None),
        permission_ids=(),
        permission_snapshot_fingerprint=overrides.pop("permission_snapshot_fingerprint", "c" * 64),
        requires_user_approval=(decision_outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED),
        evaluation_time=NOW,
        advisory_flags=(),
    )


def _build(target_context, decisions, entity_types=None):
    entity_types = dict(entity_types or {})
    for d in decisions:
        entity_types.setdefault(d.entity_id, EntityType.SKILL)
        if d.employment_scope_entity_id is not None:
            entity_types.setdefault(d.employment_scope_entity_id, EntityType.EMPLOYMENT)
    return build_cv_content_plan(target_context=target_context, decisions=decisions, entity_types=entity_types)


def _decisions_by_fp(decisions):
    return {d.decision_fingerprint: d for d in decisions}


def _header_decision(target_context, employment_id, salt=""):
    return _decision(
        target_context=target_context,
        fact_type="COMPANY",
        target_scope="EMPLOYMENT_HEADER",
        entity_id=employment_id,
        salt=salt,
    )


def _build_with_employment(target_context, employment_ids, employment_entry_order=None):
    decisions = [
        _header_decision(target_context, eid, salt=str(index))
        for index, eid in enumerate(employment_ids)
    ]
    entity_types = {eid: EntityType.EMPLOYMENT for eid in employment_ids}
    result = build_cv_content_plan(
        target_context=target_context,
        decisions=decisions,
        entity_types=entity_types,
        employment_entry_order=employment_entry_order,
    )
    return result.plan, decisions


# -- Replay / staleness --


def test_identical_replay_input_is_not_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    first = replay_input_from_plan(plan)
    second = replay_input_from_plan(plan)
    assert not is_cv_content_plan_stale(first, second)
    assert stale_fields(first, second) == ()


def test_decision_fingerprint_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    current = previous.model_copy(
        update={"fact_selection_decision_fingerprints": ("f" * 64,)}
    )
    assert is_cv_content_plan_stale(previous, current)
    assert "fact_selection_decision_fingerprints" in stale_fields(previous, current)


def test_fact_revision_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    bumped_revisions = dict(previous.fact_revisions)
    bumped_revisions[d.fact_id] = bumped_revisions[d.fact_id] + 1
    current = previous.model_copy(update={"fact_revisions": bumped_revisions})
    assert is_cv_content_plan_stale(previous, current)
    assert "fact_revisions" in stale_fields(previous, current)


def test_fact_content_fingerprint_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    changed = dict(previous.fact_content_fingerprints)
    changed[d.fact_id] = "9" * 64
    current = previous.model_copy(update={"fact_content_fingerprints": changed})
    assert is_cv_content_plan_stale(previous, current)
    assert "fact_content_fingerprints" in stale_fields(previous, current)


def test_fact_type_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    changed = dict(previous.fact_types)
    changed[d.fact_id] = "TOOL"
    current = previous.model_copy(update={"fact_types": changed})
    assert is_cv_content_plan_stale(previous, current)
    assert "fact_types" in stale_fields(previous, current)


def test_permission_snapshot_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    changed = dict(previous.permission_snapshot_fingerprints)
    changed[d.fact_id] = "7" * 64
    current = previous.model_copy(update={"permission_snapshot_fingerprints": changed})
    assert is_cv_content_plan_stale(previous, current)
    assert "permission_snapshot_fingerprints" in stale_fields(previous, current)


def test_target_context_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    current = previous.model_copy(update={"target_context_fingerprint": "6" * 64})
    assert is_cv_content_plan_stale(previous, current)
    assert "target_context_fingerprint" in stale_fields(previous, current)


def test_content_policy_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    current = previous.model_copy(update={"content_policy_version": "cv-content-policy-v99"})
    assert is_cv_content_plan_stale(previous, current)
    assert "content_policy_version" in stale_fields(previous, current)


def test_transformation_policy_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    current = previous.model_copy(update={"transformation_policy_version": "truth-transformation-policy-v99"})
    assert is_cv_content_plan_stale(previous, current)
    assert "transformation_policy_version" in stale_fields(previous, current)


def test_budget_profile_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    current = previous.model_copy(update={"budget_profile_fingerprint": "5" * 64})
    assert is_cv_content_plan_stale(previous, current)
    assert "budget_profile_fingerprint" in stale_fields(previous, current)


def test_pending_approval_state_change_is_stale() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.APPROVAL_REQUIRED
    )
    plan = _build(tc, [d]).plan
    previous = replay_input_from_plan(plan)
    pending_fp = plan.pending_approvals[0].pending_approval_fingerprint
    current = previous.model_copy(update={"pending_approval_states": {pending_fp: "SOME_OTHER_STATE"}})
    assert is_cv_content_plan_stale(previous, current)
    assert "pending_approval_states" in stale_fields(previous, current)


def test_no_current_replay_input_gives_freshness_not_verified() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    result = validate_cv_content_plan(plan, _decisions_by_fp([d]), entity_types={})
    assert result.freshness_status == CvFreshnessStatus.FRESHNESS_NOT_VERIFIED
    assert result.p5_ready is False


def test_stale_plan_is_never_p5_ready() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    current = replay_input_from_plan(plan).model_copy(update={"content_policy_version": "cv-content-policy-v99"})
    result = validate_cv_content_plan(plan, _decisions_by_fp([d]), entity_types={}, current_replay_input=current)
    assert result.freshness_status == CvFreshnessStatus.STALE
    assert result.p5_ready is False


def test_fresh_valid_plan_is_p5_ready() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    current = replay_input_from_plan(plan)
    result = validate_cv_content_plan(plan, _decisions_by_fp([d]), entity_types={}, current_replay_input=current)
    assert result.structural_status == CvStructuralValidationStatus.VALID
    assert result.freshness_status == CvFreshnessStatus.FRESH
    assert result.p5_ready is True
    assert result.violations == ()


# -- Employment entry order replay snapshot --


def test_replay_input_contains_employment_entry_orders_field() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    replay_input = replay_input_from_plan(plan)
    assert len(replay_input.employment_entry_orders) == 1


def test_snapshot_contains_employment_entity_id() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders[0][0] == eid


def test_snapshot_contains_entry_order() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders[0][1] == 0


def test_snapshot_contains_entry_order_source() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders[0][2] == EntryOrderSource.EXPLICIT_INPUT


def test_identical_employment_order_is_not_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, _ = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    first = replay_input_from_plan(plan)
    second = replay_input_from_plan(plan)
    assert not is_cv_content_plan_stale(first, second)
    assert stale_fields(first, second) == ()


def test_entry_order_change_is_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, _ = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    previous = replay_input_from_plan(plan)
    swapped = tuple(
        (item[0], 1 - item[1], item[2]) for item in previous.employment_entry_orders
    )
    current = previous.model_copy(update={"employment_entry_orders": swapped})
    assert is_cv_content_plan_stale(previous, current)
    assert "employment_entry_orders" in stale_fields(previous, current)


def test_entry_order_source_change_is_stale() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    previous = replay_input_from_plan(plan)
    flipped = tuple(
        (item[0], item[1], EntryOrderSource.UUID_FALLBACK) for item in previous.employment_entry_orders
    )
    current = previous.model_copy(update={"employment_entry_orders": flipped})
    assert is_cv_content_plan_stale(previous, current)
    assert "employment_entry_orders" in stale_fields(previous, current)


def test_added_employment_entry_is_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, _ = _build_with_employment(tc, [eid_a], employment_entry_order={eid_a: 0})
    previous = replay_input_from_plan(plan)
    added = previous.employment_entry_orders + ((eid_b, 1, EntryOrderSource.EXPLICIT_INPUT),)
    current = previous.model_copy(update={"employment_entry_orders": added})
    assert is_cv_content_plan_stale(previous, current)
    assert "employment_entry_orders" in stale_fields(previous, current)


def test_removed_employment_entry_is_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, _ = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    previous = replay_input_from_plan(plan)
    reduced = previous.employment_entry_orders[:1]
    current = previous.model_copy(update={"employment_entry_orders": reduced})
    assert is_cv_content_plan_stale(previous, current)
    assert "employment_entry_orders" in stale_fields(previous, current)


def test_stale_fields_isolated_to_employment_entry_orders_for_order_only_change() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, _ = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    previous = replay_input_from_plan(plan)
    swapped = tuple(
        (item[0], 1 - item[1], item[2]) for item in previous.employment_entry_orders
    )
    current = previous.model_copy(update={"employment_entry_orders": swapped})
    assert stale_fields(previous, current) == ("employment_entry_orders",)


def test_validator_marks_reordered_replay_as_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, decisions = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    previous = replay_input_from_plan(plan)
    swapped = tuple(
        (item[0], 1 - item[1], item[2]) for item in previous.employment_entry_orders
    )
    current = previous.model_copy(update={"employment_entry_orders": swapped})
    result = validate_cv_content_plan(plan, _decisions_by_fp(decisions), entity_types={}, current_replay_input=current)
    assert result.freshness_status == CvFreshnessStatus.STALE


def test_validator_reordered_replay_is_not_p5_ready() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, decisions = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    previous = replay_input_from_plan(plan)
    swapped = tuple(
        (item[0], 1 - item[1], item[2]) for item in previous.employment_entry_orders
    )
    current = previous.model_copy(update={"employment_entry_orders": swapped})
    result = validate_cv_content_plan(plan, _decisions_by_fp(decisions), entity_types={}, current_replay_input=current)
    assert result.p5_ready is False


def test_validator_identical_employment_order_stays_fresh() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    plan, decisions = _build_with_employment(
        tc, [eid_a, eid_b], employment_entry_order={eid_a: 0, eid_b: 1}
    )
    current = replay_input_from_plan(plan)
    result = validate_cv_content_plan(plan, _decisions_by_fp(decisions), entity_types={}, current_replay_input=current)
    assert result.freshness_status == CvFreshnessStatus.FRESH
    assert result.p5_ready is True


def test_input_mapping_iteration_order_does_not_change_replay_snapshot() -> None:
    tc = _target_context()
    eid_a, eid_b, eid_c = uuid4(), uuid4(), uuid4()
    order_forward = {eid_a: 0, eid_b: 1, eid_c: 2}
    order_reversed = {eid_c: 2, eid_a: 0, eid_b: 1}
    decisions = [
        _header_decision(tc, eid_a, salt="0"),
        _header_decision(tc, eid_b, salt="1"),
        _header_decision(tc, eid_c, salt="2"),
    ]
    entity_types = {eid_a: EntityType.EMPLOYMENT, eid_b: EntityType.EMPLOYMENT, eid_c: EntityType.EMPLOYMENT}
    plan_forward = build_cv_content_plan(
        target_context=tc, decisions=decisions, entity_types=entity_types, employment_entry_order=order_forward
    ).plan
    plan_reversed = build_cv_content_plan(
        target_context=tc, decisions=decisions, entity_types=entity_types, employment_entry_order=order_reversed
    ).plan
    assert replay_input_from_plan(plan_forward).employment_entry_orders == replay_input_from_plan(
        plan_reversed
    ).employment_entry_orders


def test_uuid_fallback_is_tracked_in_snapshot() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order=None)
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders[0][2] == EntryOrderSource.UUID_FALLBACK


def test_explicit_input_is_tracked_in_snapshot() -> None:
    tc = _target_context()
    eid = uuid4()
    plan, _ = _build_with_employment(tc, [eid], employment_entry_order={eid: 0})
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders[0][2] == EntryOrderSource.EXPLICIT_INPUT


def test_no_experience_entries_gives_empty_employment_entry_orders() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    replay_input = replay_input_from_plan(plan)
    assert replay_input.employment_entry_orders == ()


def test_employment_entry_orders_snapshot_is_deterministic() -> None:
    tc = _target_context()
    eid_a, eid_b, eid_c = uuid4(), uuid4(), uuid4()
    plan, _ = _build_with_employment(
        tc, [eid_a, eid_b, eid_c], employment_entry_order={eid_a: 0, eid_b: 1, eid_c: 2}
    )
    first = replay_input_from_plan(plan).employment_entry_orders
    second = replay_input_from_plan(plan).employment_entry_orders
    assert first == second
    assert [str(item[0]) for item in first] == sorted(str(item[0]) for item in first)


def test_reordered_employment_entry_order_input_makes_replay_stale() -> None:
    tc = _target_context()
    eid_a, eid_b = uuid4(), uuid4()
    decisions = [
        _header_decision(tc, eid_a, salt="0"),
        _header_decision(tc, eid_b, salt="1"),
    ]
    entity_types = {eid_a: EntityType.EMPLOYMENT, eid_b: EntityType.EMPLOYMENT}
    plan_original = build_cv_content_plan(
        target_context=tc,
        decisions=decisions,
        entity_types=entity_types,
        employment_entry_order={eid_a: 0, eid_b: 1},
    ).plan
    plan_reordered = build_cv_content_plan(
        target_context=tc,
        decisions=decisions,
        entity_types=entity_types,
        employment_entry_order={eid_a: 1, eid_b: 0},
    ).plan
    previous = replay_input_from_plan(plan_original)
    current = replay_input_from_plan(plan_reordered)
    assert is_cv_content_plan_stale(previous, current)


# -- Validator fail-closed checks --


def test_partition_violation_is_invalid() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    tampered = plan.model_copy(update={"source_decision_fingerprints": ()})
    result = validate_cv_content_plan(tampered, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.SOURCE_PARTITION_INCOMPLETE for v in result.violations
    )


def test_fingerprint_mismatch_is_invalid() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    tampered = plan.model_copy(update={"content_plan_fingerprint": "1" * 64})
    result = validate_cv_content_plan(tampered, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(v.violation_code == CvContentPlanViolationCode.FINGERPRINT_MISMATCH for v in result.violations)


def test_employment_mismatch_is_invalid() -> None:
    tc = _target_context()
    employment_id = uuid4()
    other_employment_id = uuid4()
    d = _decision(
        target_context=tc, entity_id=employment_id, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER"
    )
    plan = _build(tc, [d], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    tampered_entry = experience.experience_entries[0].model_copy(
        update={"employment_entity_id": other_employment_id}
    )
    tampered_experience = experience.model_copy(update={"experience_entries": (tampered_entry,)})
    new_sections = tuple(
        tampered_experience if s.section == CvSection.EXPERIENCE else s for s in plan.sections
    )
    tampered_plan = plan.model_copy(update={"sections": new_sections})
    result = validate_cv_content_plan(tampered_plan, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.EMPLOYMENT_GROUPING_MISMATCH for v in result.violations
    )


def test_duplicate_planned_fact_is_invalid() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    duplicated_use = competencies.planned_fact_uses[0].model_copy(update={"placement_order": 1})
    tampered_competencies = competencies.model_copy(
        update={"planned_fact_uses": competencies.planned_fact_uses + (duplicated_use,)}
    )
    new_sections = tuple(
        tampered_competencies if s.section == CvSection.COMPETENCIES else s for s in plan.sections
    )
    tampered_plan = plan.model_copy(update={"sections": new_sections})
    result = validate_cv_content_plan(tampered_plan, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.DUPLICATE_FACT_IN_PLANNED_CONTENT
        for v in result.violations
    )


def test_profile_multi_fact_is_invalid() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    plan = _build(tc, [d]).plan
    profile = next(s for s in plan.sections if s.section == CvSection.PROFILE)
    second_use = profile.planned_fact_uses[0].model_copy(
        update={"fact_id": uuid4(), "placement_order": 1}
    )
    tampered_profile = profile.model_copy(
        update={"planned_fact_uses": profile.planned_fact_uses + (second_use,), "skipped_empty": False}
    )
    new_sections = tuple(tampered_profile if s.section == CvSection.PROFILE else s for s in plan.sections)
    tampered_plan = plan.model_copy(update={"sections": new_sections})
    result = validate_cv_content_plan(tampered_plan, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(v.violation_code == CvContentPlanViolationCode.PROFILE_MULTIPLE_FACTS for v in result.violations)


def test_budget_exceeded_gives_requires_review_and_non_fatal_violation() -> None:
    from app.schemas.cv_content_plan import CvContentBudgetProfile, SectionBudgetLimit
    from app.services.cv_content_plan_builder import compute_budget_profile_fingerprint

    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", salt="1")
    d2 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", salt="2")
    limits = (SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),)
    budget_profile = CvContentBudgetProfile(
        budget_profile_id="bp",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="bp", section_limits=limits
        ),
        section_limits=limits,
    )
    result = build_cv_content_plan(
        target_context=tc,
        decisions=[d1, d2],
        entity_types={d1.entity_id: EntityType.SKILL, d2.entity_id: EntityType.SKILL},
        budget_profile=budget_profile,
    )
    plan = result.plan
    assert plan.plan_status == CvContentPlanStatus.REQUIRES_REVIEW
    validation = validate_cv_content_plan(plan, _decisions_by_fp([d1, d2]), entity_types={})
    assert validation.structural_status == CvStructuralValidationStatus.VALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.SECTION_BUDGET_EXCEEDED for v in validation.violations
    )
    assert validation.p5_ready is False


def test_validator_is_fail_closed_on_decision_not_found() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    result = validate_cv_content_plan(plan, {}, entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(v.violation_code == CvContentPlanViolationCode.DECISION_NOT_FOUND for v in result.violations)


# -- Stage P4.5a: validator owner EntityType fail-closed checks --


def test_validator_requires_entity_types_keyword_argument() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    with pytest.raises(TypeError):
        validate_cv_content_plan(plan, _decisions_by_fp([d]))


def test_validator_missing_owner_type_is_structural_invalid() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(target_context=tc, entity_id=entity_id, fact_type="COURSE_NAME", target_scope="COURSES")
    plan = _build(tc, [d], entity_types={entity_id: EntityType.COURSE}).plan
    result = validate_cv_content_plan(plan, _decisions_by_fp([d]), entity_types={})
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISSING for v in result.violations
    )
    assert result.p5_ready is False


def test_validator_mismatched_owner_type_is_structural_invalid() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(target_context=tc, entity_id=entity_id, fact_type="COURSE_NAME", target_scope="COURSES")
    plan = _build(tc, [d], entity_types={entity_id: EntityType.COURSE}).plan
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.SKILL}
    )
    assert result.structural_status == CvStructuralValidationStatus.INVALID
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH for v in result.violations
    )
    assert result.p5_ready is False


def test_validator_owner_check_covers_planned_decision() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(target_context=tc, entity_id=entity_id, fact_type="LANGUAGE_NAME", target_scope="LANGUAGES")
    plan = _build(tc, [d], entity_types={entity_id: EntityType.LANGUAGE}).plan
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.SKILL}
    )
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH
        and v.decision_fingerprint == d.decision_fingerprint
        for v in result.violations
    )


def test_validator_owner_check_covers_pending_decision() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(
        target_context=tc,
        entity_id=entity_id,
        fact_type="LANGUAGE_NAME",
        target_scope="LANGUAGES",
        decision=SelectionDecisionOutcome.APPROVAL_REQUIRED,
    )
    plan = _build(tc, [d], entity_types={entity_id: EntityType.LANGUAGE}).plan
    assert len(plan.pending_approvals) == 1
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.SKILL}
    )
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH
        and v.decision_fingerprint == d.decision_fingerprint
        for v in result.violations
    )


def test_validator_owner_check_covers_omitted_decision() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(
        target_context=tc,
        entity_id=entity_id,
        fact_type="LANGUAGE_NAME",
        target_scope="LANGUAGES",
        decision=SelectionDecisionOutcome.BLOCKED,
    )
    plan = _build(tc, [d], entity_types={entity_id: EntityType.LANGUAGE}).plan
    assert len(plan.omitted_facts) == 1
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.SKILL}
    )
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH
        and v.decision_fingerprint == d.decision_fingerprint
        for v in result.violations
    )


def test_validator_owner_check_covers_conflict_member() -> None:
    tc = _target_context()
    entity_id = uuid4()
    fact_id = uuid4()
    d1 = _decision(
        target_context=tc,
        fact_id=fact_id,
        entity_id=entity_id,
        fact_type="LANGUAGE_NAME",
        target_scope="LANGUAGES",
        salt="a",
    )
    d2 = _decision(
        target_context=tc,
        fact_id=fact_id,
        entity_id=entity_id,
        fact_type="LANGUAGE_NAME",
        target_scope="LANGUAGES",
        salt="b",
    )
    plan = _build(tc, [d1, d2], entity_types={entity_id: EntityType.LANGUAGE}).plan
    assert len(plan.conflicts) == 1
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d1, d2]), entity_types={entity_id: EntityType.SKILL}
    )
    mismatch_fps = {
        v.decision_fingerprint
        for v in result.violations
        if v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH
    }
    assert {d1.decision_fingerprint, d2.decision_fingerprint} <= mismatch_fps


def test_validator_owner_check_is_not_limited_to_sections() -> None:
    """A BLOCKED decision never appears in plan.sections at all -- the owner
    check must still catch its mismatch."""

    tc = _target_context()
    entity_id = uuid4()
    d = _decision(
        target_context=tc,
        entity_id=entity_id,
        fact_type="LANGUAGE_NAME",
        target_scope="LANGUAGES",
        decision=SelectionDecisionOutcome.BLOCKED,
    )
    plan = _build(tc, [d], entity_types={entity_id: EntityType.LANGUAGE}).plan
    for section in plan.sections:
        uses = section.planned_fact_uses if section.kind == "FLAT" else ()
        assert all(u.fact_id != d.fact_id for u in uses)
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.SKILL}
    )
    assert any(
        v.violation_code == CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH for v in result.violations
    )


def test_validator_correct_owner_stays_valid() -> None:
    tc = _target_context()
    entity_id = uuid4()
    d = _decision(target_context=tc, entity_id=entity_id, fact_type="COURSE_NAME", target_scope="COURSES")
    plan = _build(tc, [d], entity_types={entity_id: EntityType.COURSE}).plan
    result = validate_cv_content_plan(
        plan, _decisions_by_fp([d]), entity_types={entity_id: EntityType.COURSE}
    )
    assert result.structural_status == CvStructuralValidationStatus.VALID
    assert not any(
        v.violation_code
        in (CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISSING, CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH)
        for v in result.violations
    )


def test_validator_unrelated_fact_types_get_no_new_owner_restriction() -> None:
    """SKILL is outside the closed P4.5a fact_type set: any entity_types
    mapping (even an empty one) must never trigger the new owner checks."""

    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    result = validate_cv_content_plan(plan, _decisions_by_fp([d]), entity_types={})
    assert not any(
        v.violation_code
        in (CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISSING, CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH)
        for v in result.violations
    )
    assert result.structural_status == CvStructuralValidationStatus.VALID


# -- Stage P4.5a: schema version, fingerprints, replay --


def test_schema_version_is_cv_content_plan_schema_v2() -> None:
    assert CV_CONTENT_PLAN_SCHEMA_VERSION == "cv-content-plan-schema-v2"


def test_old_schema_v1_literal_is_rejected() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    payload = plan.model_dump(mode="json")
    payload["schema_version"] = "cv-content-plan-schema-v1"
    with pytest.raises(ValidationError):
        CvContentPlan.model_validate(payload)


def test_adding_courses_changes_section_fingerprint() -> None:
    tc = _target_context()
    entity_id = uuid4()
    empty_plan = _build(tc, [_decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")]).plan
    course_decision = _decision(
        target_context=tc, entity_id=entity_id, fact_type="COURSE_NAME", target_scope="COURSES"
    )
    with_course_plan = _build(tc, [course_decision], entity_types={entity_id: EntityType.COURSE}).plan
    empty_courses = next(s for s in empty_plan.sections if s.section == CvSection.COURSES)
    filled_courses = next(s for s in with_course_plan.sections if s.section == CvSection.COURSES)
    assert empty_courses.section_plan_fingerprint != filled_courses.section_plan_fingerprint


def test_adding_course_name_changes_plan_fingerprint() -> None:
    tc = _target_context()
    entity_id = uuid4()
    without_course = _build(tc, [_decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")]).plan
    course_decision = _decision(
        target_context=tc, entity_id=entity_id, fact_type="COURSE_NAME", target_scope="COURSES"
    )
    with_course = _build(
        tc,
        [_decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS"), course_decision],
        entity_types={entity_id: EntityType.COURSE},
    ).plan
    assert without_course.content_plan_fingerprint != with_course.content_plan_fingerprint


def test_content_policy_v2_is_in_replay_input() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    replay_input = replay_input_from_plan(plan)
    assert replay_input.content_policy_version == "cv-content-policy-v2"


def test_replay_input_gained_no_new_field_for_courses() -> None:
    expected_fields = {
        "target_context_fingerprint",
        "job_or_application_id",
        "fact_selection_decision_fingerprints",
        "fact_revisions",
        "fact_content_fingerprints",
        "fact_types",
        "permission_snapshot_fingerprints",
        "employment_entry_orders",
        "selection_policy_version",
        "transformation_policy_version",
        "content_policy_version",
        "budget_profile_fingerprint",
        "career_positioning_snapshot_fingerprint",
        "pending_approval_states",
    }
    from app.schemas.cv_content_plan import CvContentPlanReplayInput

    assert set(CvContentPlanReplayInput.model_fields) == expected_fields


def test_employment_entry_orders_still_works_alongside_course_name() -> None:
    tc = _target_context()
    employment_id, course_id = uuid4(), uuid4()
    header = _header_decision(tc, employment_id)
    course_decision = _decision(
        target_context=tc, entity_id=course_id, fact_type="COURSE_NAME", target_scope="COURSES"
    )
    entity_types = {employment_id: EntityType.EMPLOYMENT, course_id: EntityType.COURSE}
    plan = build_cv_content_plan(
        target_context=tc,
        decisions=[header, course_decision],
        entity_types=entity_types,
        employment_entry_order={employment_id: 0},
    ).plan
    replay_input = replay_input_from_plan(plan)
    assert len(replay_input.employment_entry_orders) == 1
    assert replay_input.employment_entry_orders[0][0] == employment_id
