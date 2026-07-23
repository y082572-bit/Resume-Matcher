"""``cv_content_plan_strategy_ranking``: deterministic P4 strategy ranking helpers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.fact_selection import FactSelectionDecision, FactSelectionReasonCode, SelectionDecisionOutcome, TargetContext
from app.schemas.strategy_fact_selection import StrategyRankTier
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_plan_strategy_ranking import (
    STRATEGY_RANK_TIER_ORDER,
    build_strategy_sort_key_fn,
)
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, compute_target_context_fingerprint


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v4"


def _target_context() -> TargetContext:
    return TargetContext(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-1",
        selection_policy_version=SELECTION_POLICY_VERSION,
    )


def _decision(
    *,
    target_context: TargetContext,
    fact_type: str,
    entity_id=None,
    fact_id=None,
    employment_scope_entity_id=None,
    salt: str = "",
) -> FactSelectionDecision:
    fact_id = fact_id or uuid4()
    entity_id = entity_id or uuid4()
    seed = f"{fact_id}{entity_id}{fact_type}{salt}{uuid4()}"
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
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        decision=SelectionDecisionOutcome.SELECTED,
        reason_codes=(FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        allowed_operation=TransformationOperation.EXACT_COPY,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=employment_scope_entity_id,
        permission_ids=(),
        permission_snapshot_fingerprint="b" * 64,
        requires_user_approval=False,
        evaluation_time=NOW,
        advisory_flags=(),
    )


def test_tier_order_is_exact_then_category_then_legacy() -> None:
    assert STRATEGY_RANK_TIER_ORDER[StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT] < (
        STRATEGY_RANK_TIER_ORDER[StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH]
    )
    assert STRATEGY_RANK_TIER_ORDER[StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH] < (
        STRATEGY_RANK_TIER_ORDER[StrategyRankTier.TIER_2_LEGACY_ONLY]
    )


def test_exact_fact_ranks_before_category_and_legacy() -> None:
    tc = _target_context()
    exact = _decision(target_context=tc, fact_type="SKILL", salt="exact")
    category = _decision(target_context=tc, fact_type="SKILL", salt="category")
    legacy = _decision(target_context=tc, fact_type="SKILL", salt="legacy")
    tiers = {
        exact.decision_fingerprint: StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT,
        category.decision_fingerprint: StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH,
        legacy.decision_fingerprint: StrategyRankTier.TIER_2_LEGACY_ONLY,
    }
    key_fn = build_strategy_sort_key_fn(tiers)
    ordered = sorted([legacy, category, exact], key=key_fn)
    assert ordered == [exact, category, legacy]


def test_ties_within_a_tier_are_deterministic_by_fact_type_priority_then_ids() -> None:
    tc = _target_context()
    responsibility = _decision(target_context=tc, fact_type="RESPONSIBILITY", salt="r")
    achievement = _decision(target_context=tc, fact_type="ACHIEVEMENT", salt="a")
    tiers = {
        responsibility.decision_fingerprint: StrategyRankTier.TIER_2_LEGACY_ONLY,
        achievement.decision_fingerprint: StrategyRankTier.TIER_2_LEGACY_ONLY,
    }
    key_fn = build_strategy_sort_key_fn(tiers)
    # RESPONSIBILITY (priority 70) sorts before ACHIEVEMENT (priority 90).
    ordered = sorted([achievement, responsibility], key=key_fn)
    assert ordered == [responsibility, achievement]


def test_sort_is_deterministic_and_repeatable() -> None:
    tc = _target_context()
    decisions = [_decision(target_context=tc, fact_type="SKILL", salt=str(i)) for i in range(5)]
    tiers = {d.decision_fingerprint: StrategyRankTier.TIER_2_LEGACY_ONLY for d in decisions}
    key_fn = build_strategy_sort_key_fn(tiers)
    ordered_a = sorted(decisions, key=key_fn)
    ordered_b = sorted(list(reversed(decisions)), key=key_fn)
    assert [d.decision_fingerprint for d in ordered_a] == [d.decision_fingerprint for d in ordered_b]


def test_missing_tier_falls_back_to_legacy_only() -> None:
    tc = _target_context()
    known = _decision(target_context=tc, fact_type="SKILL", salt="known")
    unknown = _decision(target_context=tc, fact_type="SKILL", salt="unknown")
    tiers = {known.decision_fingerprint: StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT}
    key_fn = build_strategy_sort_key_fn(tiers)
    ordered = sorted([unknown, known], key=key_fn)
    assert ordered == [known, unknown]


def test_ranking_never_uses_entity_adjacency_alone() -> None:
    """Two facts sharing the same entity_id/employment_scope_entity_id must
    not automatically rank together -- only their own tier (from strategy
    P3) and fact_type_priority drive the order."""

    tc = _target_context()
    shared_entity = uuid4()
    a = _decision(target_context=tc, fact_type="ACHIEVEMENT", entity_id=shared_entity, salt="a")
    b = _decision(target_context=tc, fact_type="ACHIEVEMENT", entity_id=shared_entity, salt="b")
    tiers = {
        a.decision_fingerprint: StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT,
        b.decision_fingerprint: StrategyRankTier.TIER_2_LEGACY_ONLY,
    }
    key_fn = build_strategy_sort_key_fn(tiers)
    ordered = sorted([b, a], key=key_fn)
    # a (TIER_0) still ranks first despite sharing entity_id with b (TIER_2).
    assert ordered == [a, b]
