"""``strategy_fact_selection_policy``: NOT_RELEVANT override gate, evidence
category mapping, and positioning-driven FLATTEN_FOR_LOWER_ROLE requests.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.candidacy_thesis import PositioningLevel, RoleEvidenceCategory
from app.schemas.fact_selection import CareerPositioningSignal, SelectionDecisionOutcome, TargetContext
from app.schemas.strategy_fact_selection import (
    StrategyOverrideOutcome,
    StrategyOverrideReasonCode,
    StrategyRankTier,
)
from app.schemas.truth_fact import TransformationOperation
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact
from app.services.strategy_fact_selection_policy import (
    FLATTEN_ELIGIBLE_POSITIONING_LEVELS,
    ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES,
    evaluate_not_relevant_override,
    find_exact_evidence_mapping,
    resolve_requested_operation,
    resolve_strategy_tier,
)
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _target_context(person_entity_id) -> TargetContext:
    return TargetContext(
        target_context_id=__import__("uuid").uuid4(),
        person_entity_id=person_entity_id,
        job_or_application_id="job-1",
        selection_policy_version=SELECTION_POLICY_VERSION,
    )


# -- ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES sanity --


def test_category_mapping_uses_only_closed_categories_and_known_fact_types() -> None:
    from app.services.truth_policy import TruthTransformationPolicyRegistry

    registry = TruthTransformationPolicyRegistry()
    for category, fact_types in ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES.items():
        assert isinstance(category, RoleEvidenceCategory)
        for fact_type in fact_types:
            assert registry.get(fact_type) is not None, f"{fact_type} is not a registered fact_type"


def test_flatten_eligible_levels_exclude_executive_and_senior_and_management() -> None:
    assert PositioningLevel.EXECUTIVE not in FLATTEN_ELIGIBLE_POSITIONING_LEVELS
    assert PositioningLevel.SENIOR_LEADERSHIP not in FLATTEN_ELIGIBLE_POSITIONING_LEVELS
    assert PositioningLevel.MANAGEMENT not in FLATTEN_ELIGIBLE_POSITIONING_LEVELS
    assert PositioningLevel.EXPERT in FLATTEN_ELIGIBLE_POSITIONING_LEVELS
    assert PositioningLevel.SPECIALIST in FLATTEN_ELIGIBLE_POSITIONING_LEVELS
    assert PositioningLevel.OPERATIONAL in FLATTEN_ELIGIBLE_POSITIONING_LEVELS


# -- resolve_strategy_tier --


def test_exact_mapping_gives_tier_0() -> None:
    tier, category = resolve_strategy_tier(
        fact_type="SKILL", has_exact_evidence_mapping=True, priority_evidence_categories=()
    )
    assert tier == StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT
    assert category is None


def test_category_match_gives_tier_1() -> None:
    tier, category = resolve_strategy_tier(
        fact_type="EMPLOYMENT_ACTIVITY",
        has_exact_evidence_mapping=False,
        priority_evidence_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,),
    )
    assert tier == StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH
    assert category == RoleEvidenceCategory.PROCESS_IMPROVEMENT


def test_no_category_match_gives_tier_2() -> None:
    tier, category = resolve_strategy_tier(
        fact_type="LANGUAGE_NAME",
        has_exact_evidence_mapping=False,
        priority_evidence_categories=(RoleEvidenceCategory.BUSINESS_RESULT,),
    )
    assert tier == StrategyRankTier.TIER_2_LEGACY_ONLY
    assert category is None


def test_same_employment_entity_alone_never_yields_tier_1() -> None:
    """Co-location in the same employment entity (or sharing an
    employment_scope_entity_id) is never itself evidence of a category
    match -- only a real, registered fact_type membership in the closed
    table can produce TIER_1."""

    tier, category = resolve_strategy_tier(
        fact_type="COMPANY",  # never present in any category mapping
        has_exact_evidence_mapping=False,
        priority_evidence_categories=tuple(RoleEvidenceCategory),
    )
    assert tier == StrategyRankTier.TIER_2_LEGACY_ONLY
    assert category is None


# -- find_exact_evidence_mapping --


@pytest.mark.asyncio
async def test_find_exact_evidence_mapping_matches_identity_only() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
    )
    mapping, ambiguous = find_exact_evidence_mapping(
        base_decision=decision, evidence_mappings=fixture.thesis_result.evidence_mappings
    )
    assert ambiguous is False
    assert mapping is not None
    assert mapping.fact_id == fixture.fact0.fact_id


@pytest.mark.asyncio
async def test_find_exact_evidence_mapping_no_match_for_unrelated_fact() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(fact_type="SKILL", content_fingerprint="9" * 64)
    decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
    )
    mapping, ambiguous = find_exact_evidence_mapping(
        base_decision=decision, evidence_mappings=fixture.thesis_result.evidence_mappings
    )
    assert mapping is None
    assert ambiguous is False


# -- evaluate_not_relevant_override --


@pytest.mark.asyncio
async def test_exact_thesis_fact_overrides_not_relevant() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    assert base_decision.decision == SelectionDecisionOutcome.NOT_RELEVANT

    outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert outcome == StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED
    assert reason == StrategyOverrideReasonCode.EXACT_THESIS_SUPPORTING_FACT
    assert mapping_fp == fixture.evidence_mapping.evidence_mapping_fingerprint
    assert category == RoleEvidenceCategory.BUSINESS_RESULT
    # base decision itself is never touched by the override.
    assert base_decision.decision == SelectionDecisionOutcome.NOT_RELEVANT


@pytest.mark.asyncio
async def test_category_only_match_never_overrides_not_relevant() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    tc = _target_context(fixture.fact1.person_entity_id)
    # fact1 is EMPLOYMENT_ACTIVITY, which participates in PROCESS_IMPROVEMENT
    # (a category match), but it is never referenced by any evidence
    # mapping in the thesis -- category alone must never grant an override.
    fact = make_fact(
        entity_id=fixture.fact1.entity_id,
        fact_id=fixture.fact1.fact_id,
        fact_type=fixture.fact1.fact_type,
        content_fingerprint=fixture.fact1.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    assert base_decision.decision == SelectionDecisionOutcome.NOT_RELEVANT
    outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
    assert reason == StrategyOverrideReasonCode.NO_EVIDENCE_MAPPING
    assert mapping_fp is None


@pytest.mark.parametrize(
    "outcome",
    [SelectionDecisionOutcome.BLOCKED, SelectionDecisionOutcome.EXCLUDED, SelectionDecisionOutcome.APPROVAL_REQUIRED],
)
@pytest.mark.asyncio
async def test_hard_gated_outcomes_are_never_overridden(outcome: SelectionDecisionOutcome) -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    from app.schemas.truth_fact import FactStatus

    if outcome == SelectionDecisionOutcome.BLOCKED:
        fact = fact.model_copy(update={"status": FactStatus.REJECTED})
    elif outcome == SelectionDecisionOutcome.EXCLUDED:
        fact = fact.model_copy(update={"use_in_cv": False})
    else:
        fact = fact.model_copy(update={"requires_approval": True})

    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
    )
    assert base_decision.decision == outcome
    result_outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert result_outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
    assert reason == StrategyOverrideReasonCode.BASE_DECISION_NOT_NOT_RELEVANT


@pytest.mark.asyncio
async def test_stale_thesis_prevents_override() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=False,  # simulates a stale/failed PRE-P4 revalidation
    )
    assert outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
    assert reason == StrategyOverrideReasonCode.PREP4_REVALIDATION_FAILED


@pytest.mark.asyncio
async def test_payload_mismatch_blocks_override() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    # A fact identity that matches the mapping, but whose content_fingerprint
    # differs from what the payload snapshot and mapping actually recorded.
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint="deadbeef" * 8,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
    assert reason == StrategyOverrideReasonCode.PAYLOAD_CONTENT_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_unknown_fact_blocks_override() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(fact_type="SKILL", content_fingerprint="1" * 64)
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    outcome, reason, mapping_fp, category = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
    assert reason == StrategyOverrideReasonCode.NO_EVIDENCE_MAPPING


@pytest.mark.asyncio
async def test_override_has_its_own_fingerprint_and_base_fingerprint_unchanged() -> None:
    fixture = await build_ready_fixture()
    tc = _target_context(fixture.fact0.person_entity_id)
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    base_decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    original_fp = base_decision.decision_fingerprint
    outcome, reason, mapping_fp, _ = evaluate_not_relevant_override(
        base_decision=base_decision,
        evidence_mappings=fixture.thesis_result.evidence_mappings,
        payload_set=fixture.payload_set,
        prep4_revalidation_passed=True,
    )
    assert base_decision.decision_fingerprint == original_fp
    assert mapping_fp is not None
    assert mapping_fp != original_fp


# -- Positioning / flattening --


@pytest.mark.parametrize(
    "level", [PositioningLevel.EXPERT, PositioningLevel.SPECIALIST, PositioningLevel.OPERATIONAL]
)
def test_eligible_levels_may_request_flattening(level: PositioningLevel) -> None:
    from app.schemas.strategy_fact_selection import StrategyRequestedOperationSource

    operation, source = resolve_requested_operation(
        fact_type="ACHIEVEMENT",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=level,
    )
    assert operation == TransformationOperation.FLATTEN_FOR_LOWER_ROLE
    assert source == StrategyRequestedOperationSource.POSITIONING_FLATTEN_REQUESTED


def test_executive_never_auto_requests_flattening() -> None:
    from app.schemas.strategy_fact_selection import StrategyRequestedOperationSource

    operation, source = resolve_requested_operation(
        fact_type="ACHIEVEMENT",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=PositioningLevel.EXECUTIVE,
    )
    assert operation == TransformationOperation.EXACT_COPY
    assert source == StrategyRequestedOperationSource.LEGACY_DEFAULT


def test_management_does_not_force_flattening() -> None:
    operation, _ = resolve_requested_operation(
        fact_type="ACHIEVEMENT",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=PositioningLevel.MANAGEMENT,
    )
    assert operation == TransformationOperation.EXACT_COPY


def test_flattening_not_requested_when_policy_forbids_it_for_fact_type() -> None:
    # COMPANY only allows copy_only operations for EMPLOYMENT_HEADER; it can
    # never gain FLATTEN_FOR_LOWER_ROLE even under an eligible positioning level.
    operation, source = resolve_requested_operation(
        fact_type="COMPANY",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EMPLOYMENT_HEADER",
        positioning_level=PositioningLevel.EXPERT,
    )
    assert operation == TransformationOperation.EXACT_COPY
    from app.schemas.strategy_fact_selection import StrategyRequestedOperationSource

    assert source == StrategyRequestedOperationSource.LEGACY_DEFAULT


def test_permission_gate_can_still_block_a_flatten_request() -> None:
    """Positioning only decides the *requested* operation; the legacy
    eligibility/transferability/permission pipeline still governs the
    final decision -- flattening never bypasses it."""

    tc = _target_context(__import__("uuid").uuid4())
    fact = make_fact(fact_type="ACHIEVEMENT", content_fingerprint="7" * 64, requires_approval=True)
    operation, _ = resolve_requested_operation(
        fact_type="ACHIEVEMENT",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=PositioningLevel.EXPERT,
    )
    decision = select_fact(
        fact=fact,
        target_context=tc,
        requested_operation=operation,
        requested_target_scope="EXPERIENCE",
        permissions=(),
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED


def test_positioning_never_mutates_the_underlying_truth_fact() -> None:
    fact = make_fact(fact_type="ACHIEVEMENT", content_fingerprint="7" * 64)
    original = fact.model_copy(deep=True)
    resolve_requested_operation(
        fact_type=fact.fact_type,
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=PositioningLevel.EXPERT,
    )
    assert fact == original


def test_conflicting_elevate_signal_prevents_flattening() -> None:
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, seniority_direction="ELEVATE"
    )
    operation, source = resolve_requested_operation(
        fact_type="ACHIEVEMENT",
        baseline_requested_operation=TransformationOperation.EXACT_COPY,
        baseline_requested_target_scope="EXPERIENCE",
        positioning_level=PositioningLevel.EXPERT,
        positioning_signal=signal,
    )
    assert operation == TransformationOperation.EXACT_COPY
    from app.schemas.strategy_fact_selection import StrategyRequestedOperationSource

    assert source == StrategyRequestedOperationSource.LEGACY_DEFAULT
