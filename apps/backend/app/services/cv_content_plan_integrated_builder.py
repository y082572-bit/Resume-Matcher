"""The mandatory ``ROLE_STRATEGY_INTEGRATED`` P4 entrypoint.

``build_role_strategy_integrated_cv_content_plan`` is the only production
path that produces a ``CvContentPlanMode.ROLE_STRATEGY_INTEGRATED`` plan. It
never calls the public legacy ``build_cv_content_plan`` -- both entrypoints
call the same private ``_build_cv_content_plan_core`` helper directly, so
neither can silently bypass the other. It requires a real, already-built
``StrategyAwareFactSelectionResult`` (never ``None``, never an empty
strategy input, never a bare ``career_positioning_snapshot_fingerprint``
substitute) and always re-verifies that result against the caller's current
PRE-P4 state before trusting it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.schemas.candidacy_thesis import RoleStrategyContext
from app.schemas.cv_content_plan import (
    CvContentBudgetProfile,
    CvContentPlan,
    CvContentPlanBuildOutcome,
    CvContentPlanMode,
    CvContentPlanValidationResult,
)
from app.schemas.fact_selection import FactSelectionDecision, SelectionDecisionOutcome, TargetContext
from app.schemas.strategy_fact_selection import (
    RoleStrategyFactSelectionInput,
    StrategyAwareFactSelectionDecision,
    StrategyAwareFactSelectionResult,
    StrategyAwareFactSelectionStatus,
    StrategyOverrideOutcome,
)
from app.schemas.truth_entity import EntityType
from app.services.cv_content_plan_builder import _build_cv_content_plan_core
from app.services.cv_content_plan_replay import current_replay_input_for_integrated_plan
from app.services.cv_content_plan_strategy_ranking import strategy_sort_key_fn_from_result
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.strategy_fact_selection_replay import (
    current_strategy_selection_replay_input,
    evaluate_strategy_selection_freshness,
    replay_input_from_strategy_selection_result,
)
from app.services.strategy_fact_selection_validator import (
    StrategyFactSelectionValidationStatus,
    validate_strategy_fact_selection_result,
)


def _project_effective_decision(
    decision: StrategyAwareFactSelectionDecision,
) -> FactSelectionDecision:
    """Project one strategy-aware decision into the ``FactSelectionDecision``
    the low-level P4 core understands -- never mutating ``base_decision``.

    Only the ``NOT_RELEVANT`` -> ``SELECTED`` override path produces a new
    object; every other outcome (including a base-``SELECTED`` decision)
    passes ``base_decision`` through completely unchanged.
    """

    if decision.override_outcome == StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED:
        return decision.base_decision.model_copy(
            update={
                "decision": SelectionDecisionOutcome.SELECTED,
                "allowed_operation": decision.base_decision.requested_operation,
            }
        )
    return decision.base_decision


@dataclass(frozen=True)
class RoleStrategyIntegratedCvContentPlanResult:
    outcome: CvContentPlanBuildOutcome
    plan: CvContentPlan | None
    validation: CvContentPlanValidationResult | None
    snapshot_violations: tuple[str, ...]
    p5_ready: bool


def _invalid(*violations: str) -> RoleStrategyIntegratedCvContentPlanResult:
    return RoleStrategyIntegratedCvContentPlanResult(
        outcome=CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT,
        plan=None,
        validation=None,
        snapshot_violations=tuple(violations),
        p5_ready=False,
    )


def build_role_strategy_integrated_cv_content_plan(
    *,
    integration_input: RoleStrategyFactSelectionInput,
    strategy_selection_result: StrategyAwareFactSelectionResult,
    role_strategy_context: RoleStrategyContext,
    target_context: TargetContext,
    entity_types: Mapping[UUID, EntityType],
    budget_profile: CvContentBudgetProfile | None = None,
    employment_entry_order: Mapping[UUID, int] | None = None,
) -> RoleStrategyIntegratedCvContentPlanResult:
    """Build a ``ROLE_STRATEGY_INTEGRATED`` ``CvContentPlan``.

    Sequence: (1) re-verify ``strategy_selection_result`` against
    ``integration_input``'s current PRE-P4 state, (2) require the strategy
    selection replay to be ``FRESH``, (3) project effective decisions
    without touching any ``base_decision``, (4) hand the strategy ranking
    to the shared low-level P4 core, (5) apply integrated budget behavior,
    (6) build the plan in ``ROLE_STRATEGY_INTEGRATED`` mode with a real
    ``content_plan_fingerprint``, (7) run the P4 validator against a
    freshly (not copied) built current replay input, (8) compute
    ``p5_ready`` from those verified invariants.
    """

    if strategy_selection_result.status != StrategyAwareFactSelectionStatus.READY or not (
        strategy_selection_result.ready_for_integrated_p4
    ):
        return _invalid("STRATEGY_SELECTION_RESULT_NOT_READY")
    if strategy_selection_result.ranking is None or not strategy_selection_result.decisions:
        return _invalid("STRATEGY_SELECTION_RESULT_EMPTY")
    if role_strategy_context != integration_input.role_strategy_context:
        return _invalid("ROLE_STRATEGY_CONTEXT_MISMATCH")
    if role_strategy_context.context_fingerprint != strategy_selection_result.role_strategy_context_fingerprint:
        return _invalid("ROLE_STRATEGY_CONTEXT_FINGERPRINT_MISMATCH")
    if target_context != integration_input.target_context:
        return _invalid("TARGET_CONTEXT_MISMATCH")

    strategy_validation = validate_strategy_fact_selection_result(
        strategy_selection_result, integration_input
    )
    if strategy_validation.status != StrategyFactSelectionValidationStatus.VALID:
        return _invalid(
            *(violation.code.value for violation in strategy_validation.violations)
        )

    previous_strategy_replay = replay_input_from_strategy_selection_result(strategy_selection_result)
    current_strategy_replay = current_strategy_selection_replay_input(
        integration_input=integration_input,
        decisions=strategy_selection_result.decisions,
        ranking=strategy_selection_result.ranking,
        evidence_category_mapping_version=strategy_selection_result.evidence_category_mapping_version,
        integration_policy_version=strategy_selection_result.integration_policy_version,
        result_fingerprint=strategy_selection_result.result_fingerprint,
    )
    strategy_freshness = evaluate_strategy_selection_freshness(
        previous_strategy_replay, current_strategy_replay
    )
    if strategy_freshness.freshness_status.value != "FRESH":
        return _invalid(
            f"STRATEGY_SELECTION_RESULT_NOT_FRESH:{strategy_freshness.freshness_status.value}",
            *(f"stale_field:{field}" for field in strategy_freshness.stale_fields),
        )

    effective_decisions = [
        _project_effective_decision(decision) for decision in strategy_selection_result.decisions
    ]
    sort_key_fn = strategy_sort_key_fn_from_result(strategy_selection_result)

    build_result = _build_cv_content_plan_core(
        target_context=target_context,
        decisions=effective_decisions,
        entity_types=entity_types,
        employment_entry_order=employment_entry_order,
        budget_profile=budget_profile,
        career_positioning_snapshot_fingerprint=None,
        plan_mode=CvContentPlanMode.ROLE_STRATEGY_INTEGRATED,
        strategy_sort_key_fn=sort_key_fn,
        role_strategy_context_fingerprint=role_strategy_context.context_fingerprint,
        strategy_selection_result_fingerprint=strategy_selection_result.result_fingerprint,
        strategy_ranking_input_fingerprint=strategy_selection_result.ranking.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=strategy_selection_result.integration_policy_version,
    )

    if build_result.outcome != CvContentPlanBuildOutcome.BUILT or build_result.plan is None:
        return RoleStrategyIntegratedCvContentPlanResult(
            outcome=build_result.outcome,
            plan=None,
            validation=None,
            snapshot_violations=build_result.snapshot_violations,
            p5_ready=False,
        )

    plan = build_result.plan
    decisions_by_fingerprint = {
        decision.decision_fingerprint: decision for decision in effective_decisions
    }
    current_replay_input = current_replay_input_for_integrated_plan(
        plan,
        role_strategy_context=role_strategy_context,
        strategy_selection_result=strategy_selection_result,
        strategy_ranking=strategy_selection_result.ranking,
        strategy_integration_policy_version=strategy_selection_result.integration_policy_version,
    )
    validation = validate_cv_content_plan(
        plan,
        decisions_by_fingerprint,
        entity_types=entity_types,
        current_replay_input=current_replay_input,
    )

    return RoleStrategyIntegratedCvContentPlanResult(
        outcome=CvContentPlanBuildOutcome.BUILT,
        plan=plan,
        validation=validation,
        snapshot_violations=(),
        p5_ready=validation.p5_ready,
    )


__all__ = [
    "RoleStrategyIntegratedCvContentPlanResult",
    "build_role_strategy_integrated_cv_content_plan",
]
