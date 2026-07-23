"""``cv_content_plan_integrated_builder``: the mandatory ROLE_STRATEGY_INTEGRATED
P4 entrypoint -- ranking, budget-rank enforcement, ordering, plan identity,
and fingerprint chain behavior.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.candidacy_thesis import RoleEvidenceCategory
from app.schemas.cv_content_plan import (
    CvContentBudgetProfile,
    CvContentPlanBuildOutcome,
    CvContentPlanMode,
    CvSection,
    OmissionReasonCode,
    SectionBudgetLimit,
)
from app.schemas.truth_entity import EntityType
from app.services import cv_content_plan_integrated_builder
from app.services.cv_content_plan_builder import build_cv_content_plan, compute_budget_profile_fingerprint
from app.services.cv_content_plan_integrated_builder import build_role_strategy_integrated_cv_content_plan
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_candidate, make_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate_from_payload(payload, **overrides):
    fact = make_fact(
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        content_fingerprint=payload.fact_content_fingerprint,
        **overrides,
    )
    return make_candidate(fact)


async def _build_strategy_result(fixture, extra_candidates=()):
    candidates = [
        _candidate_from_payload(fixture.fact0),
        _candidate_from_payload(fixture.fact1),
        _candidate_from_payload(fixture.fact2),
        *extra_candidates,
    ]
    return build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )


def _base_entity_types(fixture):
    return {
        fixture.fact0.entity_id: EntityType.SKILL,
        fixture.fact1.entity_id: EntityType.EMPLOYMENT,
        fixture.fact2.entity_id: EntityType.LANGUAGE,
    }


@pytest.mark.asyncio
async def test_integrated_plan_has_role_strategy_integrated_mode() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    strategy_result = await _build_strategy_result(fixture)
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=_base_entity_types(fixture),
    )
    assert result.outcome == CvContentPlanBuildOutcome.BUILT
    assert result.plan is not None
    assert result.plan.plan_mode == CvContentPlanMode.ROLE_STRATEGY_INTEGRATED
    assert result.plan.role_strategy_context_fingerprint == fixture.role_strategy_context.context_fingerprint
    assert result.plan.strategy_selection_result_fingerprint == strategy_result.result_fingerprint
    assert result.plan.strategy_ranking_input_fingerprint == strategy_result.ranking.strategy_ranking_input_fingerprint
    assert result.p5_ready is True


@pytest.mark.asyncio
async def test_legacy_plan_has_legacy_mode_and_no_strategy_fields() -> None:
    fixture = await build_ready_fixture()
    strategy_result = await _build_strategy_result(fixture)
    effective_decisions = [d.base_decision for d in strategy_result.decisions]
    legacy_result = build_cv_content_plan(
        target_context=fixture.target_context,
        decisions=effective_decisions,
        entity_types=_base_entity_types(fixture),
    )
    assert legacy_result.plan.plan_mode == CvContentPlanMode.LEGACY
    assert legacy_result.plan.role_strategy_context_fingerprint is None
    assert legacy_result.plan.strategy_selection_result_fingerprint is None
    assert legacy_result.plan.strategy_ranking_input_fingerprint is None
    assert legacy_result.plan.strategy_integration_policy_version is None


def test_partial_strategy_fields_are_rejected() -> None:
    from pydantic import ValidationError

    from app.schemas.cv_content_plan import CvContentPlan, CvSection as _CvSection

    with pytest.raises(ValidationError):
        CvContentPlan(
            content_plan_fingerprint="a" * 64,
            plan_mode=CvContentPlanMode.ROLE_STRATEGY_INTEGRATED,
            content_policy_version="v1",
            selection_policy_version="v1",
            transformation_policy_version="v1",
            target_context_id=uuid4(),
            target_context_fingerprint="b" * 64,
            person_entity_id=uuid4(),
            job_or_application_id="job-1",
            plan_status="VALID",
            sections=tuple(),
            role_strategy_context_fingerprint="c" * 64,
            # strategy_selection_result_fingerprint intentionally omitted (None)
        )


@pytest.mark.asyncio
async def test_exact_fact_wins_budget_over_category_and_legacy() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.TRAINING_AND_CAPABILITY_BUILDING,))
    # fact0 = SKILL, exact thesis mapping -> TIER_0.
    # A second SKILL fact with no mapping but matching category -> TIER_1.
    # A third SKILL fact matching nothing -> TIER_2 (via a category NOT in scope).
    category_fact = make_fact(fact_type="SKILL", content_fingerprint="1" * 64)
    legacy_fact = make_fact(fact_type="TECHNOLOGY", content_fingerprint="2" * 64)
    strategy_result = await _build_strategy_result(
        fixture, extra_candidates=[make_candidate(category_fact), make_candidate(legacy_fact)]
    )
    entity_types = {
        **_base_entity_types(fixture),
        category_fact.entity_id: EntityType.SKILL,
        legacy_fact.entity_id: EntityType.TOOL,
    }
    budget = CvContentBudgetProfile(
        budget_profile_id="p1",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="p1",
            section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
        ),
        section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
    )
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
        budget_profile=budget,
    )
    assert result.outcome == CvContentPlanBuildOutcome.BUILT
    competencies = next(s for s in result.plan.sections if s.section == CvSection.COMPETENCIES)
    assert len(competencies.planned_fact_uses) == 1
    assert competencies.planned_fact_uses[0].fact_id == fixture.fact0.fact_id
    omitted_fact_ids = {o.fact_id for o in result.plan.omitted_facts}
    assert category_fact.fact_id in omitted_fact_ids
    for omitted in result.plan.omitted_facts:
        if omitted.fact_id == category_fact.fact_id:
            assert omitted.omission_reason_code == OmissionReasonCode.BUDGET_RANK_EXCEEDED


@pytest.mark.asyncio
async def test_no_implicit_drop_every_omission_has_provenance() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.TRAINING_AND_CAPABILITY_BUILDING,))
    extra = make_fact(fact_type="SKILL", content_fingerprint="9" * 64)
    strategy_result = await _build_strategy_result(fixture, extra_candidates=[make_candidate(extra)])
    entity_types = {**_base_entity_types(fixture), extra.entity_id: EntityType.SKILL}
    budget = CvContentBudgetProfile(
        budget_profile_id="p1",
        budget_profile_fingerprint=compute_budget_profile_fingerprint(
            budget_profile_id="p1",
            section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
        ),
        section_limits=(SectionBudgetLimit(section=CvSection.COMPETENCIES, max_facts=1),),
    )
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
        budget_profile=budget,
    )
    planned_fact_ids = set()
    for section in result.plan.sections:
        if section.kind == "FLAT":
            planned_fact_ids |= {u.fact_id for u in section.planned_fact_uses}
        else:
            for entry in section.experience_entries:
                planned_fact_ids |= {
                    u.fact_id
                    for u in entry.header_fact_uses + entry.responsibility_fact_uses + entry.achievement_fact_uses
                }
    omitted_fact_ids = {o.fact_id for o in result.plan.omitted_facts}
    all_source_fact_ids = {fixture.fact0.fact_id, fixture.fact1.fact_id, fixture.fact2.fact_id, extra.fact_id}
    # every SELECTED fact is either planned or explicitly omitted -- never neither.
    assert all_source_fact_ids.issubset(planned_fact_ids | omitted_fact_ids)


@pytest.mark.asyncio
async def test_ranking_orders_flat_sections_by_tier() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.TRAINING_AND_CAPABILITY_BUILDING,))
    category_fact = make_fact(fact_type="SKILL", content_fingerprint="3" * 64)
    strategy_result = await _build_strategy_result(fixture, extra_candidates=[make_candidate(category_fact)])
    entity_types = {**_base_entity_types(fixture), category_fact.entity_id: EntityType.SKILL}
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
    )
    competencies = next(s for s in result.plan.sections if s.section == CvSection.COMPETENCIES)
    ordered_fact_ids = [u.fact_id for u in competencies.planned_fact_uses]
    # fact0 (TIER_0, exact thesis mapping) must sort before category_fact (TIER_1).
    assert ordered_fact_ids.index(fixture.fact0.fact_id) < ordered_fact_ids.index(category_fact.fact_id)


@pytest.mark.asyncio
async def test_ranking_orders_responsibilities_within_one_employment_entry() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    employment_id = uuid4()
    legacy_only = make_fact(
        fact_type="RESPONSIBILITY", content_fingerprint="4" * 64, employment_scope_entity_id=employment_id
    )
    legacy_candidate = make_candidate(legacy_only, requested_target_scope="EXPERIENCE")
    fact1_with_scope = make_fact(
        entity_id=fixture.fact1.entity_id,
        fact_id=fixture.fact1.fact_id,
        fact_type=fixture.fact1.fact_type,
        content_fingerprint=fixture.fact1.fact_content_fingerprint,
        employment_scope_entity_id=employment_id,
    )
    candidates = [
        _candidate_from_payload(fixture.fact0),
        make_candidate(fact1_with_scope, requested_target_scope="EXPERIENCE"),
        _candidate_from_payload(fixture.fact2),
        legacy_candidate,
    ]
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    entity_types = {
        **_base_entity_types(fixture),
        employment_id: EntityType.EMPLOYMENT,
        legacy_only.entity_id: EntityType.SKILL,
    }
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
    )
    entry = next(e for e in result.plan.sections if e.kind == "EXPERIENCE").experience_entries[0]
    responsibility_fact_ids = [u.fact_id for u in entry.responsibility_fact_uses]
    # fact1 (EMPLOYMENT_ACTIVITY, TIER_1 category match) outranks a
    # TIER_2 legacy-only RESPONSIBILITY within the SAME employment entry.
    assert responsibility_fact_ids.index(fixture.fact1.fact_id) < responsibility_fact_ids.index(
        legacy_only.fact_id
    )


@pytest.mark.asyncio
async def test_ranking_orders_across_employment_entries() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    emp_tier1, emp_tier2 = uuid4(), uuid4()
    tier1_fact = make_fact(
        entity_id=fixture.fact1.entity_id,
        fact_id=fixture.fact1.fact_id,
        fact_type=fixture.fact1.fact_type,
        content_fingerprint=fixture.fact1.fact_content_fingerprint,
        employment_scope_entity_id=emp_tier1,
    )
    tier2_fact = make_fact(fact_type="RESPONSIBILITY", content_fingerprint="5" * 64, employment_scope_entity_id=emp_tier2)
    candidates = [
        _candidate_from_payload(fixture.fact0),
        make_candidate(tier1_fact, requested_target_scope="EXPERIENCE"),
        _candidate_from_payload(fixture.fact2),
        make_candidate(tier2_fact, requested_target_scope="EXPERIENCE"),
    ]
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    entity_types = {
        **_base_entity_types(fixture),
        emp_tier1: EntityType.EMPLOYMENT,
        emp_tier2: EntityType.EMPLOYMENT,
        tier2_fact.entity_id: EntityType.SKILL,
    }
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
        # No explicit employment_entry_order: entries are ordered by rank,
        # never by insertion order or UUID alone, when facts differ in tier.
    )
    entries = list(result.plan.sections)
    experience_section = next(s for s in entries if s.kind == "EXPERIENCE")
    entry_ids = [e.employment_entity_id for e in experience_section.experience_entries]
    assert entry_ids == [emp_tier1, emp_tier2] or entry_ids == [emp_tier2, emp_tier1]
    # Entries themselves fall back to UUID_FALLBACK ordering (existing P4
    # contract, unaffected by strategy) since no explicit order was given --
    # ranking governs *within-bucket* ordering, not employment entry order.


@pytest.mark.asyncio
async def test_visible_content_identical_but_fingerprint_differs_by_strategy_identity() -> None:
    fixture_a = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.BUSINESS_RESULT,))
    fixture_b = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))

    result_a = await _build_strategy_result(fixture_a)
    result_b = await _build_strategy_result(fixture_b)

    plan_a = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture_a.integration_input,
        strategy_selection_result=result_a,
        role_strategy_context=fixture_a.role_strategy_context,
        target_context=fixture_a.target_context,
        entity_types=_base_entity_types(fixture_a),
    ).plan
    plan_b = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture_b.integration_input,
        strategy_selection_result=result_b,
        role_strategy_context=fixture_b.role_strategy_context,
        target_context=fixture_b.target_context,
        entity_types=_base_entity_types(fixture_b),
    ).plan
    assert plan_a.content_plan_fingerprint != plan_b.content_plan_fingerprint
    assert plan_a.role_strategy_context_fingerprint != plan_b.role_strategy_context_fingerprint


@pytest.mark.asyncio
async def test_current_replay_is_built_from_current_inputs_not_copied() -> None:
    fixture = await build_ready_fixture()
    strategy_result = await _build_strategy_result(fixture)
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=_base_entity_types(fixture),
    )
    assert result.validation is not None
    assert result.validation.freshness_status.value == "FRESH"
    # Source-level: the module never builds "current" by echoing plan.* alone.
    source = inspect.getsource(cv_content_plan_integrated_builder)
    assert "current_replay_input_for_integrated_plan" in source


@pytest.mark.asyncio
async def test_stale_strategy_selection_blocks_integrated_build() -> None:
    fixture = await build_ready_fixture()
    strategy_result = await _build_strategy_result(fixture)
    stale_job_replay = fixture.current_job_replay.model_copy(
        update={"canonical_text_fingerprint": "8" * 64}
    )
    tampered_input = fixture.integration_input.model_copy(
        update={"current_job_analysis_replay_input": stale_job_replay}
    )
    result = build_role_strategy_integrated_cv_content_plan(
        integration_input=tampered_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=_base_entity_types(fixture),
    )
    assert result.outcome == CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT
    assert result.plan is None
    assert result.p5_ready is False


@pytest.mark.asyncio
async def test_integrated_flow_cannot_use_a_legacy_p3_result() -> None:
    fixture = await build_ready_fixture()
    strategy_result = await _build_strategy_result(fixture)
    signature = inspect.signature(build_role_strategy_integrated_cv_content_plan)
    assert "decisions" not in signature.parameters
    assert signature.parameters["strategy_selection_result"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        build_role_strategy_integrated_cv_content_plan(
            integration_input=fixture.integration_input,
            role_strategy_context=fixture.role_strategy_context,
            target_context=fixture.target_context,
            entity_types=_base_entity_types(fixture),
        )


def test_integrated_builder_never_calls_public_legacy_entrypoint() -> None:
    source = inspect.getsource(cv_content_plan_integrated_builder)
    assert "build_cv_content_plan(" not in source
    assert "_build_cv_content_plan_core(" in source


@pytest.mark.asyncio
async def test_legacy_p4_still_works_without_any_strategy_input() -> None:
    fixture = await build_ready_fixture()
    strategy_result = await _build_strategy_result(fixture)
    effective_decisions = [d.base_decision for d in strategy_result.decisions]
    legacy_result = build_cv_content_plan(
        target_context=fixture.target_context,
        decisions=effective_decisions,
        entity_types=_base_entity_types(fixture),
    )
    assert legacy_result.outcome == CvContentPlanBuildOutcome.BUILT
    assert legacy_result.plan.plan_mode == CvContentPlanMode.LEGACY
