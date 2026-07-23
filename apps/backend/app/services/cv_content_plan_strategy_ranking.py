"""Deterministic strategy-aware ranking helpers for ``ROLE_STRATEGY_INTEGRATED``
P4 plans.

Every helper here is a pure function over already-computed
``StrategyRankTier`` assignments (from strategy-aware P3) plus the
existing, unmodified ``FACT_TYPE_PRIORITY`` table. Nothing here uses
similarity, fuzzy text matching, embeddings, an LLM, or bare entity
adjacency -- ranking is entirely determined by
``(tier, fact_type_priority, entity_id, fact_id, decision_fingerprint)``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.schemas.fact_selection import FactSelectionDecision, SelectionDecisionOutcome
from app.schemas.strategy_fact_selection import (
    StrategyAwareFactSelectionResult,
    StrategyRankTier,
)
from app.services.cv_content_plan_policy import FACT_TYPE_PRIORITY


#: Deterministic ordinal for each tier -- lower sorts first (wins ranking
#: and budget contention). Exact thesis-supporting facts always outrank
#: category-matched facts, which always outrank legacy-only facts.
STRATEGY_RANK_TIER_ORDER: dict[StrategyRankTier, int] = {
    StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT: 0,
    StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH: 1,
    StrategyRankTier.TIER_2_LEGACY_ONLY: 2,
}

StrategySortKeyFn = Callable[[FactSelectionDecision], tuple]


def tier_by_decision_fingerprint(
    result: StrategyAwareFactSelectionResult,
) -> dict[str, StrategyRankTier]:
    """Map every final-SELECTED decision's ``decision_fingerprint`` (shared
    by ``base_decision`` and its P4-facing effective projection, since
    ``compute_decision_fingerprint`` never covers the outcome itself) to
    its ``StrategyRankTier``."""

    return {
        decision.base_decision.decision_fingerprint: decision.strategy_rank_tier
        for decision in result.decisions
        if decision.final_decision == SelectionDecisionOutcome.SELECTED
    }


def build_strategy_sort_key_fn(
    tier_by_fingerprint: Mapping[str, StrategyRankTier],
) -> StrategySortKeyFn:
    """Build the P4-core-facing sort key: tier, then the existing
    ``FACT_TYPE_PRIORITY``, then ``entity_id``, ``fact_id``, and the source
    decision fingerprint. Used for both ordering (responsibilities,
    achievements, flat sections, within one employment entry, and across
    employment entries) and for budget-rank trimming -- the same key drives
    both, so "who is kept" and "who sorts first" are always consistent.
    """

    def _key(decision: FactSelectionDecision) -> tuple:
        tier = tier_by_fingerprint.get(
            decision.decision_fingerprint, StrategyRankTier.TIER_2_LEGACY_ONLY
        )
        return (
            STRATEGY_RANK_TIER_ORDER[tier],
            FACT_TYPE_PRIORITY.get(decision.fact_type, len(FACT_TYPE_PRIORITY) + 1),
            str(decision.entity_id),
            str(decision.fact_id),
            decision.decision_fingerprint,
        )

    return _key


def strategy_sort_key_fn_from_result(result: StrategyAwareFactSelectionResult) -> StrategySortKeyFn:
    """Convenience: build the sort key directly from a
    ``StrategyAwareFactSelectionResult``."""

    return build_strategy_sort_key_fn(tier_by_decision_fingerprint(result))


__all__ = [
    "STRATEGY_RANK_TIER_ORDER",
    "StrategySortKeyFn",
    "tier_by_decision_fingerprint",
    "build_strategy_sort_key_fn",
    "strategy_sort_key_fn_from_result",
]
