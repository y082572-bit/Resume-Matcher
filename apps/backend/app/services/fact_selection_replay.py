"""Pure replay and stale-decision detection for P3 fact selection.

No function here reads a clock, performs a lookup, or touches legacy Truth
Library data. Staleness is judged only by comparing the explicit fields
listed below between a previously produced ``FactSelectionDecision`` and
the caller's current values for those same fields.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from uuid import UUID

from app.schemas.fact_selection import FactSelectionDecision
from app.schemas.truth_fact import TransformationOperation


@dataclass(frozen=True)
class FactSelectionReplayInput:
    """The exact set of fields whose change makes a decision stale."""

    fact_id: UUID
    entity_id: UUID
    fact_revision: int
    fact_content_fingerprint: str
    target_context_fingerprint: str
    requested_operation: TransformationOperation
    selection_policy_version: str
    transformation_policy_version: str
    permission_snapshot_fingerprint: str


def replay_input_from_decision(decision: FactSelectionDecision) -> FactSelectionReplayInput:
    """Extract the staleness-relevant fields from a produced decision."""

    return FactSelectionReplayInput(
        fact_id=decision.fact_id,
        entity_id=decision.entity_id,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        target_context_fingerprint=decision.target_context_fingerprint,
        requested_operation=decision.requested_operation,
        selection_policy_version=decision.selection_policy_version,
        transformation_policy_version=decision.transformation_policy_version,
        permission_snapshot_fingerprint=decision.permission_snapshot_fingerprint,
    )


def stale_fields(
    previous: FactSelectionReplayInput, current: FactSelectionReplayInput
) -> tuple[str, ...]:
    """Return the sorted names of fields that differ between two inputs."""

    differing = {
        field.name
        for field in fields(FactSelectionReplayInput)
        if getattr(previous, field.name) != getattr(current, field.name)
    }
    return tuple(sorted(differing))


def is_decision_stale(
    previous: FactSelectionReplayInput, current: FactSelectionReplayInput
) -> bool:
    """A decision is stale if any staleness-relevant field has changed.

    Takes no current time and performs no lookup: both arguments are
    supplied explicitly by the caller.
    """

    return previous != current
