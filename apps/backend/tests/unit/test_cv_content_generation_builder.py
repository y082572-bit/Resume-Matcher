"""``generate_cv_content_draft``: P4 re-validation, payload matching,
rendering, result partition, provenance, fingerprints.

All facts/entities are synthetic UUIDs; no real person or CV data is used.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.cv_content_generation import (
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
    GenerationStatus,
    GenerationViolationCode,
)
from app.schemas.cv_content_plan import (
    CvContentBudgetProfile,
    CvSection,
    SectionBudgetLimit,
)
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    generate_cv_content_draft,
)
from app.services.cv_content_generation_policy import (
    CV_CONTENT_GENERATION_POLICY_VERSION,
    DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
)
from app.services.cv_content_plan_builder import (
    build_cv_content_plan,
    compute_budget_profile_fingerprint,
)
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_target_context_fingerprint,
)


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


def _decision(
    *,
    target_context: TargetContext,
    fact_id=None,
    entity_id=None,
    fact_type: str,
    target_scope: str,
    decision: SelectionDecisionOutcome = SelectionDecisionOutcome.SELECTED,
    employment_scope_entity_id=None,
    requested_operation: TransformationOperation = TransformationOperation.EXACT_COPY,
    fact_revision: int = 1,
    fact_content_fingerprint: str = "b" * 64,
    salt: str = "",
) -> FactSelectionDecision:
    fact_id = fact_id or uuid4()
    entity_id = entity_id or uuid4()
    allowed_operation = requested_operation if decision == SelectionDecisionOutcome.SELECTED else None
    reason_codes = (
        (FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,)
        if decision == SelectionDecisionOutcome.SELECTED
        else (FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED,)
    )
    seed = (
        f"{fact_id}{entity_id}{fact_type}{target_scope}{decision}{fact_content_fingerprint}"
        f"{fact_revision}{requested_operation}{salt}{uuid4()}"
    )
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
        decision=decision,
        reason_codes=reason_codes,
        requested_operation=requested_operation,
        requested_target_scope=target_scope,
        allowed_operation=allowed_operation,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=employment_scope_entity_id,
        permission_ids=(),
        permission_snapshot_fingerprint="c" * 64,
        requires_user_approval=(decision == SelectionDecisionOutcome.APPROVAL_REQUIRED),
        evaluation_time=NOW,
        advisory_flags=(),
    )


def _build_plan(target_context, decisions, entity_types=None, **kwargs):
    entity_types = dict(entity_types or {})
    for d in decisions:
        entity_types.setdefault(d.entity_id, EntityType.SKILL)
        if d.employment_scope_entity_id is not None:
            entity_types.setdefault(d.employment_scope_entity_id, EntityType.EMPLOYMENT)
    result = build_cv_content_plan(
        target_context=target_context, decisions=decisions, entity_types=entity_types, **kwargs
    )
    assert result.plan is not None, result.snapshot_violations
    return result.plan, entity_types


def _payload_for(decision: FactSelectionDecision, value_json, normalized_value_json=None):
    normalized_value_json = (
        value_json if normalized_value_json is None else normalized_value_json
    )
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=value_json,
        normalized_value_json=normalized_value_json,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    return ApprovedFactPayloadSnapshot(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=value_json,
        normalized_value_json=normalized_value_json,
        payload_fingerprint=fingerprint,
    )


def _payload_set(payloads) -> ApprovedFactPayloadSet:
    return ApprovedFactPayloadSet(
        payloads=tuple(payloads),
        payload_set_fingerprint=compute_fact_payload_set_fingerprint(payloads),
    )


def _generation_context(**overrides) -> GenerationContext:
    values = dict(
        locale="pl-PL",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    values.update(overrides)
    return GenerationContext(**values)


def _generate(
    plan,
    decisions,
    entity_types,
    fact_payloads,
    *,
    current_replay_input=None,
    generation_context=None,
):
    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}
    if current_replay_input is None:
        current_replay_input = replay_input_from_plan(plan)
    return generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_replay_input=current_replay_input,
        fact_payloads=fact_payloads,
        generation_context=generation_context or _generation_context(),
    )


def _simple_summary_plan(**decision_overrides):
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", **decision_overrides)
    plan, entity_types = _build_plan(tc, [d])
    return tc, d, plan, entity_types


def _all_generated_elements(draft):
    elements = []
    for section in draft.generated_sections:
        if section.kind == "EXPERIENCE":
            for entry in section.generated_experience_entries:
                elements.extend(entry.header_elements)
                elements.extend(entry.responsibility_elements)
                elements.extend(entry.achievement_elements)
        else:
            elements.extend(section.generated_elements)
    return elements


def _all_planned_use_fingerprints(plan):
    fingerprints = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                for use in (
                    entry.header_fact_uses
                    + entry.responsibility_fact_uses
                    + entry.achievement_fact_uses
                ):
                    fingerprints.append(use.planned_fact_use_fingerprint)
        else:
            for use in section.planned_fact_uses:
                fingerprints.append(use.planned_fact_use_fingerprint)
    return fingerprints


# -- 1: invalid P4 plan blocks generation --


def test_structurally_invalid_plan_blocks_generation() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    tampered_plan = plan.model_copy(update={"content_plan_fingerprint": "0" * 64})
    payload_set = _payload_set([_payload_for(d, "Some summary")])
    result = _generate(tampered_plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert result.draft is None
    assert any(
        v.violation_code == GenerationViolationCode.INPUT_PLAN_STRUCTURALLY_INVALID
        for v in result.violations
    )


# -- 2: stale P4 plan blocks generation --


def test_stale_plan_blocks_generation() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([_payload_for(d, "Some summary")])
    stale_replay_input = replay_input_from_plan(plan).model_copy(
        update={"job_or_application_id": "a-different-job"}
    )
    result = _generate(
        plan, [d], entity_types, payload_set, current_replay_input=stale_replay_input
    )
    assert result.status == GenerationStatus.BLOCKED
    assert result.draft is None
    assert any(
        v.violation_code == GenerationViolationCode.INPUT_PLAN_STALE for v in result.violations
    )


# -- 3: plan without p5_ready (REQUIRES_REVIEW) blocks generation --


def test_plan_not_p5_ready_blocks_generation() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    d2 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    budget = CvContentBudgetProfile(
        budget_profile_id="tight",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="tight",
            section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
        ),
        section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
    )
    plan, entity_types = _build_plan(tc, [d1, d2], budget_profile=budget)
    assert plan.plan_status.value == "REQUIRES_REVIEW"
    payload_set = _payload_set([_payload_for(d1, "Python"), _payload_for(d2, "Go")])
    result = _generate(plan, [d1, d2], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert result.draft is None
    assert any(
        v.violation_code == GenerationViolationCode.INPUT_PLAN_NOT_READY
        for v in result.violations
    )


# -- 4: conflicts block generation --


def test_conflicting_decisions_block_generation() -> None:
    tc = _target_context()
    shared_fact_id = uuid4()
    d1 = _decision(
        target_context=tc, fact_id=shared_fact_id, fact_type="SUMMARY", target_scope="SUMMARY"
    )
    d2 = _decision(
        target_context=tc,
        fact_id=shared_fact_id,
        entity_id=d1.entity_id,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        salt="conflict",
    )
    plan, entity_types = _build_plan(tc, [d1, d2])
    assert len(plan.conflicts) == 1
    payload_set = _payload_set([_payload_for(d1, "text-1"), _payload_for(d2, "text-2")])
    result = _generate(plan, [d1, d2], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert result.draft is None
    assert any(
        v.violation_code == GenerationViolationCode.INPUT_PLAN_HAS_CONFLICTS
        for v in result.violations
    )


# -- 5: pending/omitted decisions never generate content --


def test_pending_and_omitted_decisions_generate_no_content() -> None:
    tc = _target_context()
    d_selected = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    d_pending = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        decision=SelectionDecisionOutcome.APPROVAL_REQUIRED,
    )
    d_blocked = _decision(
        target_context=tc,
        fact_type="TOOL",
        target_scope="SKILLS",
        decision=SelectionDecisionOutcome.BLOCKED,
    )
    plan, entity_types = _build_plan(tc, [d_selected, d_pending, d_blocked])
    assert len(plan.pending_approvals) == 1
    assert len(plan.omitted_facts) == 1
    payload_set = _payload_set([_payload_for(d_selected, "Summary text")])
    result = _generate(plan, [d_selected, d_pending, d_blocked], entity_types, payload_set)
    assert result.status == GenerationStatus.GENERATED
    elements = _all_generated_elements(result.draft)
    assert len(elements) == 1
    assert elements[0].fact_id == d_selected.fact_id
    assert result.pending_approval_fingerprints == (
        plan.pending_approvals[0].pending_approval_fingerprint,
    )
    assert result.omitted_fact_fingerprints == (plan.omitted_facts[0].omission_fingerprint,)


# -- 6: missing payload -> blocked item --


def test_missing_payload_produces_blocked_item() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert result.draft is not None
    assert len(result.blocked_items) == 1
    assert result.blocked_items[0].violation_code == GenerationViolationCode.PAYLOAD_MISSING
    assert result.blocked_items[0].fact_id == d.fact_id


# -- 7: extra payload -> INVALID_INPUT --


def test_extra_payload_is_invalid_input() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    extra = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    payload_set = _payload_set([_payload_for(d, "text"), _payload_for(extra, "extra text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.INVALID_INPUT
    assert result.draft is None
    assert any(
        v.violation_code == GenerationViolationCode.PAYLOAD_EXTRA_FACT for v in result.violations
    )


# -- 8: every payload identity mismatch is blocked --


def test_person_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload = _payload_for(d, "text").model_copy(update={"person_entity_id": uuid4()})
    payload = payload.model_copy(
        update={
            "payload_fingerprint": compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            )
        }
    )
    # fact_id is unchanged, so this payload still matches the planned use's
    # fact_id (the extra-payload check only looks at fact_id membership) --
    # the mismatch is caught at the per-use identity check instead.
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert result.blocked_items[0].violation_code == GenerationViolationCode.PAYLOAD_PERSON_MISMATCH


def test_entity_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    wrong_entity_id = uuid4()
    payload = _payload_for(d, "text").model_copy(update={"entity_id": wrong_entity_id})
    payload = payload.model_copy(
        update={
            "payload_fingerprint": compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            )
        }
    )
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert result.blocked_items[0].violation_code == GenerationViolationCode.PAYLOAD_ENTITY_MISMATCH


def test_fact_type_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload = _payload_for(d, "text").model_copy(update={"fact_type": "SKILL"})
    payload = payload.model_copy(
        update={
            "payload_fingerprint": compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            )
        }
    )
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.PAYLOAD_FACT_TYPE_MISMATCH
    )


def test_revision_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload = _payload_for(d, "text").model_copy(update={"fact_revision": 2})
    payload = payload.model_copy(
        update={
            "payload_fingerprint": compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            )
        }
    )
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code == GenerationViolationCode.PAYLOAD_REVISION_MISMATCH
    )


def test_content_fingerprint_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload = _payload_for(d, "text").model_copy(
        update={"fact_content_fingerprint": "9" * 64}
    )
    payload = payload.model_copy(
        update={
            "payload_fingerprint": compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            )
        }
    )
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.PAYLOAD_CONTENT_FINGERPRINT_MISMATCH
    )


def test_payload_fingerprint_mismatch_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload = _payload_for(d, "text").model_copy(update={"payload_fingerprint": "f" * 64})
    result = _generate(plan, [d], entity_types, _payload_set([payload]))
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.PAYLOAD_FINGERPRINT_MISMATCH
    )


# -- 9/10/11: EXACT_COPY / FORMAT_NORMALIZATION / REORDER generate --


def test_exact_copy_generates() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.EXACT_COPY
    )
    payload_set = _payload_set([_payload_for(d, "Exact text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.GENERATED
    assert _all_generated_elements(result.draft)[0].generated_text == "Exact text"


def test_format_normalization_generates() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.FORMAT_NORMALIZATION
    )
    payload_set = _payload_set([_payload_for(d, "Text   with  extra   spaces")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.GENERATED
    assert _all_generated_elements(result.draft)[0].generated_text == "Text with extra spaces"


def test_reorder_generates() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.REORDER
    )
    payload_set = _payload_set([_payload_for(d, "Reordered text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.GENERATED
    assert _all_generated_elements(result.draft)[0].generated_text == "Reordered text"


# -- 12/13/14/15: unsupported operations are blocked --


def test_controlled_rephrase_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE
    )
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A
    )


def test_flatten_for_lower_role_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE
    )
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A
    )


def test_omit_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.OMIT
    )
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert result.blocked_items[0].violation_code == GenerationViolationCode.OMIT_NOT_GENERATABLE
    assert all(el.fact_id != d.fact_id for el in _all_generated_elements(result.draft))


def test_summarize_approved_facts_is_blocked() -> None:
    tc, d, plan, entity_types = _simple_summary_plan(
        requested_operation=TransformationOperation.SUMMARIZE_APPROVED_FACTS
    )
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.BLOCKED
    assert (
        result.blocked_items[0].violation_code
        == GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A
    )


# -- 16: result partition is complete and disjoint --


def test_result_partition_is_complete_and_disjoint() -> None:
    tc = _target_context()
    d_ok = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    d_blocked = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    plan, entity_types = _build_plan(tc, [d_ok, d_blocked])
    payload_set = _payload_set([_payload_for(d_ok, "text"), _payload_for(d_blocked, "skill")])
    result = _generate(plan, [d_ok, d_blocked], entity_types, payload_set)

    all_fps = set(_all_planned_use_fingerprints(plan))
    generated_fps = {el.planned_fact_use_fingerprint for el in _all_generated_elements(result.draft)}
    blocked_fps = {item.planned_fact_use_fingerprint for item in result.blocked_items}

    assert generated_fps.isdisjoint(blocked_fps)
    assert generated_fps | blocked_fps == all_fps


# -- 17: section structure is 1:1 with the plan --


def test_generated_sections_mirror_plan_sections_1_to_1() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    plan_sections = [s.section for s in plan.sections]
    draft_sections = [s.section for s in result.draft.generated_sections]
    assert plan_sections == draft_sections


# -- 18/19/20/21: experience grouping, placement_order, employment scoping --


def _employment_plan():
    tc = _target_context()
    employment_a = uuid4()
    employment_b = uuid4()
    role_a = _decision(
        target_context=tc, fact_type="EMPLOYMENT_ROLE", target_scope="EMPLOYMENT_HEADER",
        entity_id=employment_a,
    )
    activity_a = _decision(
        target_context=tc, fact_type="EMPLOYMENT_ACTIVITY", target_scope="EXPERIENCE",
        entity_id=uuid4(), employment_scope_entity_id=employment_a,
    )
    role_b = _decision(
        target_context=tc, fact_type="EMPLOYMENT_ROLE", target_scope="EMPLOYMENT_HEADER",
        entity_id=employment_b,
    )
    activity_b = _decision(
        target_context=tc, fact_type="EMPLOYMENT_ACTIVITY", target_scope="EXPERIENCE",
        entity_id=uuid4(), employment_scope_entity_id=employment_b,
    )
    decisions = [role_a, activity_a, role_b, activity_b]
    plan, entity_types = _build_plan(
        tc,
        decisions,
        entity_types={employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT},
        employment_entry_order={employment_a: 0, employment_b: 1},
    )
    payloads = [
        _payload_for(role_a, {"stanowisko": "Analyst A", "firma": "Co A"}),
        _payload_for(activity_a, "Did work A"),
        _payload_for(role_b, {"stanowisko": "Analyst B", "firma": "Co B"}),
        _payload_for(activity_b, "Did work B"),
    ]
    payload_set = _payload_set(payloads)
    result = _generate(plan, decisions, entity_types, payload_set)
    return plan, result, employment_a, employment_b, role_a, activity_a, role_b, activity_b


def test_experience_grouping_is_1_to_1_with_plan_entries() -> None:
    plan, result, employment_a, employment_b, *_ = _employment_plan()
    assert result.status == GenerationStatus.GENERATED
    experience_section = next(
        s for s in result.draft.generated_sections if s.kind == "EXPERIENCE"
    )
    plan_experience_section = next(s for s in plan.sections if s.kind == "EXPERIENCE")
    assert len(experience_section.generated_experience_entries) == len(
        plan_experience_section.experience_entries
    )
    generated_ids = {
        entry.employment_entity_id for entry in experience_section.generated_experience_entries
    }
    assert generated_ids == {employment_a, employment_b}


def test_placement_order_is_preserved() -> None:
    plan, result, *_ = _employment_plan()
    for element in _all_generated_elements(result.draft):
        source_use = next(
            use
            for use in _iter_planned_uses(plan)
            if use.planned_fact_use_fingerprint == element.planned_fact_use_fingerprint
        )
        assert element.placement_order == source_use.placement_order


def _iter_planned_uses(plan):
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                yield from entry.header_fact_uses
                yield from entry.responsibility_fact_uses
                yield from entry.achievement_fact_uses
        else:
            yield from section.planned_fact_uses


def test_employment_entity_id_is_preserved() -> None:
    plan, result, employment_a, employment_b, role_a, activity_a, role_b, activity_b = (
        _employment_plan()
    )
    experience_section = next(
        s for s in result.draft.generated_sections if s.kind == "EXPERIENCE"
    )
    for entry in experience_section.generated_experience_entries:
        for element in entry.header_elements:
            assert element.entity_id == entry.employment_entity_id
        for element in entry.responsibility_elements + entry.achievement_elements:
            assert element.employment_scope_entity_id == entry.employment_entity_id


def test_no_cross_employment_move() -> None:
    plan, result, employment_a, employment_b, role_a, activity_a, role_b, activity_b = (
        _employment_plan()
    )
    experience_section = next(
        s for s in result.draft.generated_sections if s.kind == "EXPERIENCE"
    )
    entry_a = next(
        e for e in experience_section.generated_experience_entries
        if e.employment_entity_id == employment_a
    )
    entry_b = next(
        e for e in experience_section.generated_experience_entries
        if e.employment_entity_id == employment_b
    )
    assert {el.fact_id for el in entry_a.header_elements} == {role_a.fact_id}
    assert {el.fact_id for el in entry_a.responsibility_elements} == {activity_a.fact_id}
    assert {el.fact_id for el in entry_b.header_elements} == {role_b.fact_id}
    assert {el.fact_id for el in entry_b.responsibility_elements} == {activity_b.fact_id}


# -- 22: every generated element carries full provenance --


def test_generated_element_has_full_provenance() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    context = _generation_context()
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set, generation_context=context)
    element = _all_generated_elements(result.draft)[0]
    provenance = element.generation_provenance
    assert provenance.generation_policy_version == context.generation_policy_version
    assert provenance.deterministic_template_version == context.deterministic_template_version
    assert provenance.payload_set_fingerprint == payload_set.payload_set_fingerprint
    assert provenance.payload_fingerprint == element.payload_fingerprint
    assert provenance.renderer_contract_version
    assert provenance.renderer_name
    assert element.content_plan_fingerprint == plan.content_plan_fingerprint
    assert element.fact_selection_decision_fingerprint == d.decision_fingerprint


# -- 23/24/25/26: fingerprint determinism and sensitivity --


def test_result_fingerprint_is_deterministic() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([_payload_for(d, "text")])
    r1 = _generate(plan, [d], entity_types, payload_set)
    r2 = _generate(plan, [d], entity_types, payload_set)
    assert r1.result_fingerprint == r2.result_fingerprint
    assert r1.draft.generated_content_draft_fingerprint == r2.draft.generated_content_draft_fingerprint


def test_changing_payload_changes_fingerprint() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    r1 = _generate(plan, [d], entity_types, _payload_set([_payload_for(d, "text one")]))
    r2 = _generate(plan, [d], entity_types, _payload_set([_payload_for(d, "text two")]))
    assert r1.result_fingerprint != r2.result_fingerprint
    assert (
        r1.draft.generated_content_draft_fingerprint
        != r2.draft.generated_content_draft_fingerprint
    )


def test_changing_plan_changes_fingerprint() -> None:
    tc1, d1, plan1, entity_types1 = _simple_summary_plan()
    tc2 = _target_context(job_or_application_id="a-different-job")
    d2 = _decision(target_context=tc2, fact_type="SUMMARY", target_scope="SUMMARY")
    plan2, entity_types2 = _build_plan(tc2, [d2])
    r1 = _generate(plan1, [d1], entity_types1, _payload_set([_payload_for(d1, "text")]))
    r2 = _generate(plan2, [d2], entity_types2, _payload_set([_payload_for(d2, "text")]))
    assert r1.result_fingerprint != r2.result_fingerprint


def test_changing_template_version_changes_fingerprint() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([_payload_for(d, "text")])
    r1 = _generate(
        plan, [d], entity_types, payload_set,
        generation_context=_generation_context(deterministic_template_version="template-v1"),
    )
    r2 = _generate(
        plan, [d], entity_types, payload_set,
        generation_context=_generation_context(deterministic_template_version="template-v2"),
    )
    assert r1.result_fingerprint != r2.result_fingerprint
    el1 = _all_generated_elements(r1.draft)[0]
    el2 = _all_generated_elements(r2.draft)[0]
    assert el1.generated_element_fingerprint != el2.generated_element_fingerprint
    assert el1.generated_text == el2.generated_text


# -- 27/28/29/30: status <-> partition <-> readiness invariants --


def test_generated_status_only_without_blocked_items() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    payload_set = _payload_set([_payload_for(d, "text")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.GENERATED
    assert result.blocked_items == ()
    assert result.ready_for_p55 is True


def test_blocked_status_when_blocked_items_present() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    result = _generate(plan, [d], entity_types, _payload_set([]))
    assert result.status == GenerationStatus.BLOCKED
    assert len(result.blocked_items) >= 1
    assert result.ready_for_p55 is False


def test_invalid_input_gives_draft_none() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    extra = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    payload_set = _payload_set([_payload_for(d, "text"), _payload_for(extra, "extra")])
    result = _generate(plan, [d], entity_types, payload_set)
    assert result.status == GenerationStatus.INVALID_INPUT
    assert result.draft is None
    assert result.ready_for_p55 is False


def test_ready_for_p55_only_for_full_generated() -> None:
    tc, d, plan, entity_types = _simple_summary_plan()
    generated = _generate(plan, [d], entity_types, _payload_set([_payload_for(d, "text")]))
    blocked = _generate(plan, [d], entity_types, _payload_set([]))
    assert generated.ready_for_p55 is True
    assert blocked.ready_for_p55 is False
