"""Pure replay and stale-plan detection for P4 CV content plans.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a ``CvContentPlanReplayInput``
derived from a previously built ``CvContentPlan`` against the caller's
current values for those same fields.
"""

from __future__ import annotations

from app.schemas.candidacy_thesis import RoleStrategyContext
from app.schemas.cv_content_plan import CvContentPlan, CvContentPlanMode, CvContentPlanReplayInput
from app.schemas.strategy_fact_selection import (
    StrategyAwareFactSelectionResult,
    StrategyFactRankingInput,
)


def replay_input_from_plan(plan: CvContentPlan) -> CvContentPlanReplayInput:
    """Extract the staleness-relevant fields from a previously built plan.

    The per-fact granular maps (``fact_revisions``/``fact_content_fingerprints``/
    ``fact_types``/``permission_snapshot_fingerprints``) are populated only
    from ``PlannedFactUse`` entries -- the only bucket carrying that data
    directly. Pending and omitted decisions remain covered, at
    decision-fingerprint granularity, through
    ``fact_selection_decision_fingerprints``.
    """

    fact_revisions: dict = {}
    fact_content_fingerprints: dict = {}
    fact_types: dict = {}
    permission_snapshot_fingerprints: dict = {}
    employment_entry_orders: list = []

    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            uses = [
                use
                for entry in section.experience_entries
                for use in (
                    entry.header_fact_uses
                    + entry.responsibility_fact_uses
                    + entry.achievement_fact_uses
                )
            ]
            employment_entry_orders.extend(
                (entry.employment_entity_id, entry.entry_order, entry.entry_order_source)
                for entry in section.experience_entries
            )
        else:
            uses = list(section.planned_fact_uses)
        for use in uses:
            fact_revisions[use.fact_id] = use.fact_revision
            fact_content_fingerprints[use.fact_id] = use.fact_content_fingerprint
            fact_types[use.fact_id] = use.fact_type
            permission_snapshot_fingerprints[use.fact_id] = use.permission_snapshot_fingerprint

    employment_entry_orders.sort(key=lambda item: str(item[0]))

    pending_approval_states = {
        pending.pending_approval_fingerprint: pending.approval_state
        for pending in plan.pending_approvals
    }

    return CvContentPlanReplayInput(
        target_context_fingerprint=plan.target_context_fingerprint,
        job_or_application_id=plan.job_or_application_id,
        fact_selection_decision_fingerprints=tuple(sorted(plan.source_decision_fingerprints)),
        fact_revisions=fact_revisions,
        fact_content_fingerprints=fact_content_fingerprints,
        fact_types=fact_types,
        permission_snapshot_fingerprints=permission_snapshot_fingerprints,
        employment_entry_orders=tuple(employment_entry_orders),
        selection_policy_version=plan.selection_policy_version,
        transformation_policy_version=plan.transformation_policy_version,
        content_policy_version=plan.content_policy_version,
        budget_profile_fingerprint=plan.budget_profile_fingerprint,
        career_positioning_snapshot_fingerprint=plan.career_positioning_snapshot_fingerprint,
        pending_approval_states=pending_approval_states,
        plan_mode=plan.plan_mode,
        role_strategy_context_fingerprint=plan.role_strategy_context_fingerprint,
        strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=plan.strategy_integration_policy_version,
    )


def current_replay_input_for_integrated_plan(
    plan: CvContentPlan,
    *,
    role_strategy_context: RoleStrategyContext,
    strategy_selection_result: StrategyAwareFactSelectionResult,
    strategy_ranking: StrategyFactRankingInput,
    strategy_integration_policy_version: str,
) -> CvContentPlanReplayInput:
    """Build the "current" replay input for a ``ROLE_STRATEGY_INTEGRATED``
    plan from the caller's actual current PRE-P4/strategy state -- never by
    copying the plan's own stored strategy fields. Only the per-fact fields
    (already sourced from the plan's own ``PlannedFactUse`` data by
    ``replay_input_from_plan``) are reused; the four strategy fields are
    always independently recomputed from the fresh objects passed here.
    """

    base = replay_input_from_plan(plan)
    return base.model_copy(
        update={
            "plan_mode": CvContentPlanMode.ROLE_STRATEGY_INTEGRATED,
            "role_strategy_context_fingerprint": role_strategy_context.context_fingerprint,
            "strategy_selection_result_fingerprint": strategy_selection_result.result_fingerprint,
            "strategy_ranking_input_fingerprint": strategy_ranking.strategy_ranking_input_fingerprint,
            "strategy_integration_policy_version": strategy_integration_policy_version,
        }
    )


def stale_fields(
    previous: CvContentPlanReplayInput, current: CvContentPlanReplayInput
) -> tuple[str, ...]:
    """Return the sorted names of fields that differ between two inputs."""

    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_cv_content_plan_stale(
    previous: CvContentPlanReplayInput, current: CvContentPlanReplayInput
) -> bool:
    """A plan is stale if any staleness-relevant field has changed.

    Takes no current time and performs no lookup: both arguments are
    supplied explicitly by the caller.
    """

    return previous != current
