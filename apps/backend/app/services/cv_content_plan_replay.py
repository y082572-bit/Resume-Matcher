"""Pure replay and stale-plan detection for P4 CV content plans.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a ``CvContentPlanReplayInput``
derived from a previously built ``CvContentPlan`` against the caller's
current values for those same fields.
"""

from __future__ import annotations

from app.schemas.cv_content_plan import CvContentPlan, CvContentPlanReplayInput


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
