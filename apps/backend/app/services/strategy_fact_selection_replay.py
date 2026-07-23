"""Pure replay and stale-result detection for strategy-aware P3 fact
selection.

No function here reads a clock, performs a lookup, or touches the
database. ``replay_input_from_strategy_selection_result`` reconstructs the
"previous" replay input from *only* a previously produced
``StrategyAwareFactSelectionResult`` (never a database, never re-deriving
external inputs it doesn't itself carry). ``current_strategy_selection_replay_input``
builds the "current" replay input independently from a fresh
``RoleStrategyFactSelectionInput`` plus a freshly (re)computed set of
decisions/ranking -- never by copying fields out of the same stored result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields

from app.schemas.strategy_fact_selection import (
    RoleStrategyFactSelectionInput,
    StrategyAwareFactSelectionDecision,
    StrategyAwareFactSelectionFreshnessStatus,
    StrategyAwareFactSelectionReplayResult,
    StrategyAwareFactSelectionResult,
    StrategyFactRankingInput,
)
from app.services.fact_selection_replay import FactSelectionReplayInput, replay_input_from_decision
from app.services.strategy_fact_selection_builder import compute_integration_input_fingerprint


@dataclass(frozen=True)
class StrategyAwareFactSelectionReplayInput:
    """The exact set of fields whose change makes a strategy-aware P3
    result stale."""

    integration_input_fingerprint: str
    role_strategy_context_fingerprint: str
    per_decision_replay_inputs: tuple[FactSelectionReplayInput, ...]
    per_strategy_decision_fingerprints: tuple[str, ...]
    strategy_ranking_input_fingerprint: str | None
    evidence_category_mapping_version: str
    integration_policy_version: str
    result_fingerprint: str


def _decision_replay_inputs(
    decisions: Sequence[StrategyAwareFactSelectionDecision],
) -> tuple[FactSelectionReplayInput, ...]:
    return tuple(
        sorted(
            (replay_input_from_decision(decision.base_decision) for decision in decisions),
            key=lambda item: (str(item.fact_id), item.fact_content_fingerprint),
        )
    )


def replay_input_from_strategy_selection_result(
    result: StrategyAwareFactSelectionResult,
) -> StrategyAwareFactSelectionReplayInput:
    """Extract the staleness-relevant fields of a previously produced
    result. Uses only data the result itself carries -- never a database,
    never a re-derivation from an upstream object the result doesn't
    already reference by fingerprint or by embedded value."""

    return StrategyAwareFactSelectionReplayInput(
        integration_input_fingerprint=result.integration_input_fingerprint,
        role_strategy_context_fingerprint=result.role_strategy_context_fingerprint,
        per_decision_replay_inputs=_decision_replay_inputs(result.decisions),
        per_strategy_decision_fingerprints=tuple(
            sorted(d.strategy_decision_fingerprint for d in result.decisions)
        ),
        strategy_ranking_input_fingerprint=(
            result.ranking.strategy_ranking_input_fingerprint if result.ranking else None
        ),
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )


def current_strategy_selection_replay_input(
    *,
    integration_input: RoleStrategyFactSelectionInput,
    decisions: Sequence[StrategyAwareFactSelectionDecision],
    ranking: StrategyFactRankingInput | None,
    evidence_category_mapping_version: str,
    integration_policy_version: str,
    result_fingerprint: str,
) -> StrategyAwareFactSelectionReplayInput:
    """Build the "current" replay input independently from fresh PRE-P4,
    payload and fact inputs -- never by copying stored strategy fields off
    an existing result."""

    return StrategyAwareFactSelectionReplayInput(
        integration_input_fingerprint=compute_integration_input_fingerprint(integration_input),
        role_strategy_context_fingerprint=integration_input.role_strategy_context.context_fingerprint,
        per_decision_replay_inputs=_decision_replay_inputs(decisions),
        per_strategy_decision_fingerprints=tuple(
            sorted(d.strategy_decision_fingerprint for d in decisions)
        ),
        strategy_ranking_input_fingerprint=(
            ranking.strategy_ranking_input_fingerprint if ranking else None
        ),
        evidence_category_mapping_version=evidence_category_mapping_version,
        integration_policy_version=integration_policy_version,
        result_fingerprint=result_fingerprint,
    )


def stale_strategy_selection_fields(
    previous: StrategyAwareFactSelectionReplayInput,
    current: StrategyAwareFactSelectionReplayInput,
) -> tuple[str, ...]:
    differing = {
        field.name
        for field in fields(StrategyAwareFactSelectionReplayInput)
        if getattr(previous, field.name) != getattr(current, field.name)
    }
    return tuple(sorted(differing))


def is_strategy_selection_stale(
    previous: StrategyAwareFactSelectionReplayInput,
    current: StrategyAwareFactSelectionReplayInput,
) -> bool:
    return previous != current


def evaluate_strategy_selection_freshness(
    previous: StrategyAwareFactSelectionReplayInput,
    current: StrategyAwareFactSelectionReplayInput | None,
) -> StrategyAwareFactSelectionReplayResult:
    """``current=None`` is always ``FRESHNESS_NOT_VERIFIED`` -- never
    guessed as ``FRESH``."""

    if current is None:
        return StrategyAwareFactSelectionReplayResult(
            freshness_status=StrategyAwareFactSelectionFreshnessStatus.FRESHNESS_NOT_VERIFIED,
            stale_fields=(),
        )
    if previous == current:
        return StrategyAwareFactSelectionReplayResult(
            freshness_status=StrategyAwareFactSelectionFreshnessStatus.FRESH, stale_fields=()
        )
    return StrategyAwareFactSelectionReplayResult(
        freshness_status=StrategyAwareFactSelectionFreshnessStatus.STALE,
        stale_fields=stale_strategy_selection_fields(previous, current),
    )


__all__ = [
    "StrategyAwareFactSelectionReplayInput",
    "replay_input_from_strategy_selection_result",
    "current_strategy_selection_replay_input",
    "stale_strategy_selection_fields",
    "is_strategy_selection_stale",
    "evaluate_strategy_selection_freshness",
]
