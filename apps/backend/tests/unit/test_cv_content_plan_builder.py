"""``build_cv_content_plan``: partitioning, grouping, ordering, fingerprints.

All facts/entities are synthetic UUIDs; no real person or CV data is used.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_plan import (
    ApprovalState,
    CvContentBudgetProfile,
    CvContentPlanBuildOutcome,
    CvContentPlanStatus,
    CvSection,
    EntryOrderSource,
    OmissionReasonCode,
    SectionBudgetLimit,
    SectionBudgetStatus,
)
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_plan_builder import build_cv_content_plan, compute_budget_profile_fingerprint
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
    reason_codes: tuple = (),
    fact_revision: int = 1,
    fact_content_fingerprint: str = "b" * 64,
    transformation_policy_version: str = TRANSFORMATION_POLICY_VERSION,
    salt: str = "",
) -> FactSelectionDecision:
    fact_id = fact_id or uuid4()
    entity_id = entity_id or uuid4()
    allowed_operation = requested_operation if decision == SelectionDecisionOutcome.SELECTED else None
    if not reason_codes:
        reason_codes = (
            (FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,)
            if decision == SelectionDecisionOutcome.SELECTED
            else (FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED,)
        )
    fingerprint_seed = (
        f"{fact_id}{entity_id}{fact_type}{target_scope}{decision}{fact_content_fingerprint}"
        f"{fact_revision}{requested_operation}{salt}{uuid4()}"
    )
    return FactSelectionDecision(
        decision_fingerprint=hashlib.sha256(fingerprint_seed.encode()).hexdigest(),
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=transformation_policy_version,
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


def _build(target_context, decisions, entity_types=None, **kwargs):
    entity_types = dict(entity_types or {})
    for d in decisions:
        entity_types.setdefault(d.entity_id, EntityType.SKILL)
        if d.employment_scope_entity_id is not None:
            entity_types.setdefault(d.employment_scope_entity_id, EntityType.EMPLOYMENT)
    return build_cv_content_plan(
        target_context=target_context, decisions=decisions, entity_types=entity_types, **kwargs
    )


# -- SELECTED routes to the correct section --


def test_selected_summary_routes_to_profile() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    result = _build(tc, [d])
    assert result.outcome == CvContentPlanBuildOutcome.BUILT
    profile = next(s for s in result.plan.sections if s.section == CvSection.PROFILE)
    assert len(profile.planned_fact_uses) == 1
    assert profile.planned_fact_uses[0].fact_id == d.fact_id
    assert profile.planned_fact_uses[0].approval_state == ApprovalState.NOT_REQUIRED
    assert profile.planned_fact_uses[0].requires_user_approval is False


def test_selected_skill_routes_to_competencies() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    result = _build(tc, [d])
    competencies = next(s for s in result.plan.sections if s.section == CvSection.COMPETENCIES)
    assert len(competencies.planned_fact_uses) == 1


def test_selected_tool_and_technology_route_to_tools_technologies() -> None:
    tc = _target_context()
    tool = _decision(target_context=tc, fact_type="TOOL", target_scope="SKILLS")
    tech = _decision(target_context=tc, fact_type="TECHNOLOGY", target_scope="SKILLS")
    result = _build(tc, [tool, tech])
    section = next(s for s in result.plan.sections if s.section == CvSection.TOOLS_TECHNOLOGIES)
    assert len(section.planned_fact_uses) == 2


def test_selected_project_achievement_routes_to_achievements() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="ACHIEVEMENT", target_scope="PROJECT")
    result = _build(tc, [d])
    section = next(s for s in result.plan.sections if s.section == CvSection.ACHIEVEMENTS)
    assert len(section.planned_fact_uses) == 1


# -- Outcome partitioning --


def test_approval_required_only_pending_never_planned_or_omitted() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        decision=SelectionDecisionOutcome.APPROVAL_REQUIRED,
    )
    result = _build(tc, [d])
    plan = result.plan
    assert len(plan.pending_approvals) == 1
    assert plan.pending_approvals[0].fact_id == d.fact_id
    assert plan.omitted_facts == ()
    for section in plan.sections:
        uses = (
            section.planned_fact_uses
            if section.kind == "FLAT"
            else [u for e in section.experience_entries for u in e.header_fact_uses]
        )
        assert all(u.fact_id != d.fact_id for u in uses)


def test_blocked_only_omitted() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.BLOCKED
    )
    plan = _build(tc, [d]).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.DECISION_BLOCKED
    assert plan.pending_approvals == ()


def test_excluded_only_omitted() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.EXCLUDED
    )
    plan = _build(tc, [d]).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.DECISION_EXCLUDED


def test_not_relevant_only_omitted() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        decision=SelectionDecisionOutcome.NOT_RELEVANT,
    )
    plan = _build(tc, [d]).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.DECISION_NOT_RELEVANT


def test_partition_is_complete_and_disjoint() -> None:
    tc = _target_context()
    decisions = [
        _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS"),
        _decision(
            target_context=tc,
            fact_type="SKILL",
            target_scope="SKILLS",
            decision=SelectionDecisionOutcome.APPROVAL_REQUIRED,
        ),
        _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.BLOCKED),
        _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.EXCLUDED),
        _decision(
            target_context=tc,
            fact_type="SKILL",
            target_scope="SKILLS",
            decision=SelectionDecisionOutcome.NOT_RELEVANT,
        ),
    ]
    plan = _build(tc, decisions).plan
    planned_fps = {
        u.fact_selection_decision_fingerprint
        for s in plan.sections
        for u in (
            s.planned_fact_uses
            if s.kind == "FLAT"
            else [use for e in s.experience_entries for use in (e.header_fact_uses + e.responsibility_fact_uses + e.achievement_fact_uses)]
        )
    }
    pending_fps = {p.fact_selection_decision_fingerprint for p in plan.pending_approvals}
    omitted_fps = {o.decision_fingerprint for o in plan.omitted_facts}
    conflict_fps: set[str] = set()
    for c in plan.conflicts:
        conflict_fps.update(c.conflicting_decision_fingerprints)

    all_buckets = [planned_fps, pending_fps, omitted_fps, conflict_fps]
    union = set().union(*all_buckets)
    assert union == set(plan.source_decision_fingerprints)
    total = sum(len(b) for b in all_buckets)
    assert total == len(union)  # pairwise disjoint


def test_identical_decision_fingerprint_deduplicated() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d, d, d]).plan
    assert plan.source_decision_fingerprints == (d.decision_fingerprint,)
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    assert len(competencies.planned_fact_uses) == 1


# -- Snapshot / homogeneity guards --


def test_mixed_target_context_fails_closed() -> None:
    tc = _target_context()
    other_tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    d2 = _decision(target_context=other_tc, fact_type="SKILL", target_scope="SKILLS")
    result = _build(tc, [d1, d2])
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert result.plan is None
    assert "target_context_fingerprint" in result.snapshot_violations


def test_mixed_selection_policy_version_fails_closed() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    d2 = d1.model_copy(
        update={
            "selection_policy_version": "some-other-policy-v9",
            "decision_fingerprint": "f" * 64,
        }
    )
    result = _build(tc, [d1, d2])
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert "selection_policy_version" in result.snapshot_violations


def test_mixed_transformation_policy_version_fails_closed() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    d2 = d1.model_copy(
        update={
            "transformation_policy_version": "some-other-transform-v9",
            "decision_fingerprint": "e" * 64,
        }
    )
    result = _build(tc, [d1, d2])
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert "transformation_policy_version" in result.snapshot_violations


def test_mixed_evaluation_time_fails_closed() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    d2 = d1.model_copy(
        update={
            "evaluation_time": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "decision_fingerprint": "d" * 64,
        }
    )
    result = _build(tc, [d1, d2])
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert "evaluation_time" in result.snapshot_violations


def test_job_or_application_id_required() -> None:
    tc = _target_context(job_or_application_id=None)
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    result = _build(tc, [d])
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert "job_or_application_id" in result.snapshot_violations


def test_missing_entity_type_fails_closed() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types={})
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert result.plan is None
    assert "entity_types" in result.snapshot_violations


def test_empty_decisions_sequence_raises() -> None:
    tc = _target_context()
    with pytest.raises(ValueError):
        build_cv_content_plan(target_context=tc, decisions=[], entity_types={})


# -- Employment grouping --


def test_employment_role_header_grouped_by_entity_id() -> None:
    tc = _target_context()
    employment_id = uuid4()
    d = _decision(
        target_context=tc, entity_id=employment_id, fact_type="EMPLOYMENT_ROLE", target_scope="EMPLOYMENT_HEADER"
    )
    plan = _build(tc, [d], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert len(experience.experience_entries) == 1
    entry = experience.experience_entries[0]
    assert entry.employment_entity_id == employment_id
    assert len(entry.header_fact_uses) == 1


def test_employment_activity_grouped_by_employment_scope_entity_id() -> None:
    tc = _target_context()
    employment_id = uuid4()
    activity_entity_id = uuid4()
    d = _decision(
        target_context=tc,
        entity_id=activity_entity_id,
        fact_type="EMPLOYMENT_ACTIVITY",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_id,
    )
    plan = _build(
        tc, [d], entity_types={employment_id: EntityType.EMPLOYMENT, activity_entity_id: EntityType.SKILL}
    ).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    entry = experience.experience_entries[0]
    assert entry.employment_entity_id == employment_id
    assert len(entry.responsibility_fact_uses) == 1


def test_employment_numeric_result_grouped_by_employment_scope_entity_id() -> None:
    tc = _target_context()
    employment_id = uuid4()
    d = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_NUMERIC_RESULT",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_id,
    )
    plan = _build(tc, [d], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    entry = experience.experience_entries[0]
    assert entry.employment_entity_id == employment_id
    assert len(entry.achievement_fact_uses) == 1


def test_responsibility_never_crosses_into_another_employment_entry() -> None:
    tc = _target_context()
    employment_a, employment_b = uuid4(), uuid4()
    d_a = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_ACTIVITY",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_a,
    )
    d_b = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_ACTIVITY",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_b,
    )
    plan = _build(
        tc,
        [d_a, d_b],
        entity_types={employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT},
    ).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert len(experience.experience_entries) == 2
    for entry in experience.experience_entries:
        uses = entry.responsibility_fact_uses
        assert len(uses) == 1
        expected = d_a if entry.employment_entity_id == employment_a else d_b
        assert uses[0].fact_id == expected.fact_id


def test_achievement_never_crosses_into_another_employment_entry() -> None:
    tc = _target_context()
    employment_a, employment_b = uuid4(), uuid4()
    d_a = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_NUMERIC_RESULT",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_a,
    )
    d_b = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_NUMERIC_RESULT",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_b,
    )
    plan = _build(
        tc,
        [d_a, d_b],
        entity_types={employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT},
    ).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    for entry in experience.experience_entries:
        expected = d_a if entry.employment_entity_id == employment_a else d_b
        assert entry.achievement_fact_uses[0].fact_id == expected.fact_id


def test_project_achievement_never_grouped_into_experience_entry() -> None:
    tc = _target_context()
    employment_id = uuid4()
    header = _decision(
        target_context=tc, entity_id=employment_id, fact_type="EMPLOYMENT_ROLE", target_scope="EMPLOYMENT_HEADER"
    )
    project_achievement = _decision(target_context=tc, fact_type="ACHIEVEMENT", target_scope="PROJECT")
    plan = _build(tc, [header, project_achievement], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert len(experience.experience_entries) == 1
    entry = experience.experience_entries[0]
    assert all(u.fact_id != project_achievement.fact_id for u in entry.achievement_fact_uses)
    achievements = next(s for s in plan.sections if s.section == CvSection.ACHIEVEMENTS)
    assert achievements.planned_fact_uses[0].fact_id == project_achievement.fact_id


def test_missing_employment_scope_entity_id_fails_closed_to_omission() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc, fact_type="EMPLOYMENT_ACTIVITY", target_scope="EXPERIENCE", employment_scope_entity_id=None
    )
    plan = _build(tc, [d]).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.EMPLOYMENT_SCOPE_REQUIRED


def test_header_entity_not_typed_employment_fails_closed_to_omission() -> None:
    tc = _target_context()
    non_employment_id = uuid4()
    d = _decision(target_context=tc, entity_id=non_employment_id, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    plan = _build(tc, [d], entity_types={non_employment_id: EntityType.SKILL}).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.EMPLOYMENT_SCOPE_REQUIRED


# -- PROFILE --


def test_profile_holds_at_most_one_summary() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY")
    plan = _build(tc, [d]).plan
    profile = next(s for s in plan.sections if s.section == CvSection.PROFILE)
    assert len(profile.planned_fact_uses) == 1


def test_no_summary_leaves_profile_skipped_empty() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    profile = next(s for s in plan.sections if s.section == CvSection.PROFILE)
    assert profile.skipped_empty is True
    assert profile.planned_fact_uses == ()


def test_multiple_summary_candidates_create_conflict_not_arbitrary_pick() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", salt="one")
    d2 = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", salt="two")
    plan = _build(tc, [d1, d2]).plan
    profile = next(s for s in plan.sections if s.section == CvSection.PROFILE)
    assert profile.planned_fact_uses == ()
    assert profile.skipped_empty is True
    assert len(plan.conflicts) == 1
    conflict = plan.conflicts[0]
    assert conflict.conflict_reason_code.value == "PROFILE_MULTIPLE_SUMMARY_CANDIDATES"
    assert set(conflict.conflicting_decision_fingerprints) == {d1.decision_fingerprint, d2.decision_fingerprint}
    assert plan.plan_status == CvContentPlanStatus.INVALID


def test_multiple_selected_decisions_for_same_fact_id_create_conflict() -> None:
    tc = _target_context()
    shared_fact_id = uuid4()
    entity_id = uuid4()
    d1 = _decision(target_context=tc, fact_id=shared_fact_id, entity_id=entity_id, fact_type="SKILL", target_scope="SKILLS", salt="a")
    d2 = _decision(target_context=tc, fact_id=shared_fact_id, entity_id=entity_id, fact_type="SKILL", target_scope="SKILLS", salt="b")
    plan = _build(tc, [d1, d2]).plan
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].fact_id == shared_fact_id
    assert plan.conflicts[0].conflict_reason_code.value == "CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT"
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    assert competencies.planned_fact_uses == ()
    assert plan.omitted_facts == ()
    assert plan.pending_approvals == ()
    assert plan.plan_status == CvContentPlanStatus.INVALID


def test_fact_id_appears_at_most_once_across_planned_content() -> None:
    tc = _target_context()
    fact_id = uuid4()
    d = _decision(target_context=tc, fact_id=fact_id, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    seen = []
    for section in plan.sections:
        uses = (
            section.planned_fact_uses
            if section.kind == "FLAT"
            else [
                u
                for e in section.experience_entries
                for u in (e.header_fact_uses + e.responsibility_fact_uses + e.achievement_fact_uses)
            ]
        )
        seen.extend(u.fact_id for u in uses)
    assert seen.count(fact_id) == 1


# -- Discriminated union / no silent trimming --


def test_experience_section_is_discriminated_and_has_no_planned_fact_uses_attr() -> None:
    tc = _target_context()
    employment_id = uuid4()
    d = _decision(target_context=tc, entity_id=employment_id, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    plan = _build(tc, [d], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert experience.kind == "EXPERIENCE"
    assert not hasattr(experience, "planned_fact_uses")


def test_flat_section_has_no_experience_entries_attr() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    plan = _build(tc, [d]).plan
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    assert competencies.kind == "FLAT"
    assert not hasattr(competencies, "experience_entries")


def test_budget_exceeded_keeps_every_item_no_trimming() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", salt="1")
    d2 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", salt="2")
    d3 = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", salt="3")
    limits = (SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),)
    profile = CvContentBudgetProfile(
        budget_profile_id="bp",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="bp", section_limits=limits
        ),
        section_limits=limits,
    )
    plan = _build(tc, [d1, d2, d3], budget_profile=profile).plan
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    assert len(competencies.planned_fact_uses) == 3
    assert competencies.budget_status == SectionBudgetStatus.EXCEEDS_BUDGET
    assert plan.plan_status == CvContentPlanStatus.REQUIRES_REVIEW


def test_budget_max_facts_zero_disables_section_via_omission() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    limits = (SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=0),)
    profile = CvContentBudgetProfile(
        budget_profile_id="bp0",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="bp0", section_limits=limits
        ),
        section_limits=limits,
    )
    plan = _build(tc, [d], budget_profile=profile).plan
    competencies = next(s for s in plan.sections if s.section == CvSection.COMPETENCIES)
    assert competencies.planned_fact_uses == ()
    assert competencies.skipped_empty is True
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code == OmissionReasonCode.BUDGET_NOT_AVAILABLE


# -- Determinism / fingerprints --


def test_identical_input_gives_identical_plan_fingerprint() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS")
    first = _build(tc, [d]).plan
    second = _build(tc, [d]).plan
    assert first.content_plan_fingerprint == second.content_plan_fingerprint
    assert first == second


def test_fact_type_change_changes_plan_fingerprint() -> None:
    tc = _target_context()
    fact_id = uuid4()
    entity_id = uuid4()
    d_skill = _decision(target_context=tc, fact_id=fact_id, entity_id=entity_id, fact_type="SKILL", target_scope="SKILLS")
    d_tool = _decision(target_context=tc, fact_id=fact_id, entity_id=entity_id, fact_type="TOOL", target_scope="SKILLS")
    first = _build(tc, [d_skill]).plan
    second = _build(tc, [d_tool]).plan
    assert first.content_plan_fingerprint != second.content_plan_fingerprint


def test_employment_grouping_change_changes_plan_fingerprint() -> None:
    tc = _target_context()
    employment_a, employment_b = uuid4(), uuid4()
    entity_types = {employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT}
    d_a = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_ACTIVITY",
        target_scope="EXPERIENCE",
        employment_scope_entity_id=employment_a,
    )
    d_b = d_a.model_copy(
        update={"employment_scope_entity_id": employment_b, "decision_fingerprint": "9" * 64}
    )
    first = _build(tc, [d_a], entity_types=entity_types).plan
    second = _build(tc, [d_b], entity_types=entity_types).plan
    assert first.content_plan_fingerprint != second.content_plan_fingerprint


def test_omission_reason_change_changes_plan_fingerprint() -> None:
    tc = _target_context()
    d_blocked = _decision(target_context=tc, fact_type="SKILL", target_scope="SKILLS", decision=SelectionDecisionOutcome.BLOCKED)
    d_excluded = d_blocked.model_copy(
        update={"decision": SelectionDecisionOutcome.EXCLUDED, "decision_fingerprint": "8" * 64}
    )
    first = _build(tc, [d_blocked]).plan
    second = _build(tc, [d_excluded]).plan
    assert first.omitted_facts[0].omission_reason_code != second.omitted_facts[0].omission_reason_code
    assert first.content_plan_fingerprint != second.content_plan_fingerprint


def test_pending_approval_has_its_own_fingerprint() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        decision=SelectionDecisionOutcome.APPROVAL_REQUIRED,
    )
    plan = _build(tc, [d]).plan
    assert len(plan.pending_approvals[0].pending_approval_fingerprint) == 64


def test_conflicts_participate_in_content_plan_fingerprint() -> None:
    tc = _target_context()
    d1 = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", salt="x")
    d2 = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", salt="y")
    with_conflict = _build(tc, [d1, d2]).plan
    without_conflict = _build(tc, [d1]).plan
    assert with_conflict.content_plan_fingerprint != without_conflict.content_plan_fingerprint


def test_no_uuid4_or_current_time_affects_plan_fingerprint() -> None:
    tc = _target_context()
    fact_id = uuid4()
    entity_id = uuid4()
    d1 = _decision(target_context=tc, fact_id=fact_id, entity_id=entity_id, fact_type="SKILL", target_scope="SKILLS", salt="stable-salt")
    # Rebuild the exact same logical decision with an identical decision_fingerprint,
    # to prove the builder is a pure function of its input, not of call-time entropy.
    d2 = d1.model_copy()
    first = _build(tc, [d1]).plan
    second = _build(tc, [d2]).plan
    assert first.content_plan_fingerprint == second.content_plan_fingerprint


# -- Sorting / ordering --


def test_employment_entry_order_uses_explicit_input_when_given() -> None:
    tc = _target_context()
    employment_a, employment_b = uuid4(), uuid4()
    d_a = _decision(target_context=tc, entity_id=employment_a, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    d_b = _decision(target_context=tc, entity_id=employment_b, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    entity_types = {employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT}
    plan = _build(
        tc,
        [d_a, d_b],
        entity_types=entity_types,
        employment_entry_order={employment_b: 0, employment_a: 1},
    ).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    ordered_ids = [e.employment_entity_id for e in experience.experience_entries]
    assert ordered_ids == [employment_b, employment_a]
    assert all(e.entry_order_source == EntryOrderSource.EXPLICIT_INPUT for e in experience.experience_entries)


def test_employment_entry_order_falls_back_to_uuid_string_order() -> None:
    tc = _target_context()
    employment_a, employment_b = uuid4(), uuid4()
    d_a = _decision(target_context=tc, entity_id=employment_a, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    d_b = _decision(target_context=tc, entity_id=employment_b, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    entity_types = {employment_a: EntityType.EMPLOYMENT, employment_b: EntityType.EMPLOYMENT}
    plan = _build(tc, [d_a, d_b], entity_types=entity_types).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    ordered_ids = [e.employment_entity_id for e in experience.experience_entries]
    assert ordered_ids == sorted([employment_a, employment_b], key=str)
    assert all(e.entry_order_source == EntryOrderSource.UUID_FALLBACK for e in experience.experience_entries)


def test_placement_order_is_zero_indexed_and_unique_within_bucket() -> None:
    tc = _target_context()
    employment_id = uuid4()
    company = _decision(target_context=tc, entity_id=employment_id, fact_type="COMPANY", target_scope="EMPLOYMENT_HEADER")
    role = _decision(target_context=tc, entity_id=employment_id, fact_type="ROLE", target_scope="EMPLOYMENT_HEADER")
    plan = _build(tc, [company, role], entity_types={employment_id: EntityType.EMPLOYMENT}).plan
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    orders = sorted(u.placement_order for u in experience.experience_entries[0].header_fact_uses)
    assert orders == [0, 1]
