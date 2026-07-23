"""Deterministic strategy-aware P3 fact selection.

``build_fact_selection_with_role_strategy`` never widens what legacy
``select_fact`` decides: every candidate is still evaluated through the
existing eligibility -> transferability -> P1 policy -> permission pipeline
first. Strategy only (a) may request ``FLATTEN_FOR_LOWER_ROLE`` instead of
the caller's baseline operation, subject to the same P1 policy gate, and (b)
may conditionally raise an advisory ``NOT_RELEVANT`` outcome to ``SELECTED``
for an exact, verified thesis-supporting fact. Nothing here creates a new
fact ID, mutates a fact payload, or changes a fact's truth status.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from app.schemas.candidacy_thesis import RoleEvidenceCategory
from app.schemas.fact_selection import CareerPositioningSignal, SelectionDecisionOutcome
from app.schemas.truth_fact import TruthPermissionRead
from app.schemas.strategy_fact_selection import (
    INTEGRATION_POLICY_VERSION,
    ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
    RoleStrategyFactSelectionInput,
    StrategyAwareFactSelectionDecision,
    StrategyAwareFactSelectionResult,
    StrategyAwareFactSelectionStatus,
    StrategyFactRankingEntry,
    StrategyFactRankingInput,
    StrategyFactSelectionCandidate,
    StrategyOverrideOutcome,
    StrategyOverrideReasonCode,
    StrategyRankTier,
)
from app.services.candidacy_thesis_builder import (
    compute_approved_fact_payload_set_fingerprint,
    compute_candidacy_thesis_context_fingerprint,
)
from app.services.candidacy_thesis_replay import compute_candidacy_thesis_replay_input_fingerprint
from app.services.cv_content_plan_policy import FACT_TYPE_PRIORITY
from app.services.fact_selection_policy import compute_target_context_fingerprint, select_fact
from app.services.job_analysis_builder import compute_job_analysis_context_fingerprint
from app.services.job_analysis_replay import compute_job_analysis_replay_input_fingerprint
from app.services.role_strategy_prep4_revalidation import Prep4RevalidationStatus, revalidate_prep4
from app.services.strategy_fact_selection_policy import (
    evaluate_not_relevant_override,
    find_exact_evidence_mapping,
    resolve_requested_operation,
    resolve_strategy_tier,
)
from app.services.truth_fingerprint import canonical_json_bytes


INTEGRATION_INPUT_FINGERPRINT_VERSION = "role-strategy-fact-selection-integration-input-v1"
STRATEGY_DECISION_FINGERPRINT_VERSION = "strategy-aware-fact-selection-decision-v1"
RANKING_ENTRY_FINGERPRINT_VERSION = "strategy-fact-ranking-entry-v1"
RANKING_INPUT_FINGERPRINT_VERSION = "strategy-fact-ranking-input-v1"
STRATEGY_RESULT_FINGERPRINT_VERSION = "strategy-aware-fact-selection-result-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_integration_input_fingerprint(
    integration_input: RoleStrategyFactSelectionInput,
) -> str:
    """Independently re-derive ``RoleStrategyFactSelectionInput``'s own
    identity. Never trusts the caller-supplied
    ``integration_input_fingerprint`` field -- callers must compare this
    return value against it."""

    job_analysis_context_fp = compute_job_analysis_context_fingerprint(
        integration_input.job_analysis_context
    )
    thesis_context_fp = compute_candidacy_thesis_context_fingerprint(
        integration_input.candidacy_thesis_context
    )
    current_job_analysis_replay_fp = compute_job_analysis_replay_input_fingerprint(
        integration_input.current_job_analysis_replay_input
    )
    current_thesis_replay_fp = compute_candidacy_thesis_replay_input_fingerprint(
        integration_input.current_candidacy_thesis_replay_input
    )
    payload_set_fp = compute_approved_fact_payload_set_fingerprint(
        integration_input.approved_fact_payload_set.payloads
    )
    target_context_fp = compute_target_context_fingerprint(integration_input.target_context)

    content = {
        "integration_schema_version": integration_input.integration_schema_version,
        "integration_policy_version": integration_input.integration_policy_version,
        "job_posting_snapshot_fingerprint": integration_input.job_posting_snapshot.snapshot_fingerprint,
        "job_analysis_context_fingerprint": job_analysis_context_fp,
        "job_analysis_result_fingerprint": integration_input.job_analysis_result.result_fingerprint,
        "current_job_analysis_replay_fingerprint": current_job_analysis_replay_fp,
        "candidacy_thesis_context_fingerprint": thesis_context_fp,
        "candidacy_thesis_result_fingerprint": integration_input.candidacy_thesis_result.result_fingerprint,
        "current_candidacy_thesis_replay_fingerprint": current_thesis_replay_fp,
        "role_strategy_context_fingerprint": integration_input.role_strategy_context.context_fingerprint,
        "approved_fact_payload_set_fingerprint": payload_set_fp,
        "target_context_fingerprint": target_context_fp,
    }
    return _fingerprint(INTEGRATION_INPUT_FINGERPRINT_VERSION, content)


def compute_strategy_decision_fingerprint(
    *,
    base_decision_fingerprint: str,
    base_decision_outcome: str,
    final_decision: str,
    override_outcome: str,
    override_reason_code: str,
    supporting_evidence_mapping_fingerprint: str | None,
    role_evidence_category: str | None,
    strategy_rank_tier: str,
    requested_operation_source: str,
    evidence_category_mapping_version: str,
    integration_policy_version: str,
) -> str:
    content = {
        "base_decision_fingerprint": base_decision_fingerprint,
        "base_decision_outcome": base_decision_outcome,
        "final_decision": final_decision,
        "override_outcome": override_outcome,
        "override_reason_code": override_reason_code,
        "supporting_evidence_mapping_fingerprint": supporting_evidence_mapping_fingerprint,
        "role_evidence_category": role_evidence_category,
        "strategy_rank_tier": strategy_rank_tier,
        "requested_operation_source": requested_operation_source,
        "evidence_category_mapping_version": evidence_category_mapping_version,
        "integration_policy_version": integration_policy_version,
    }
    return _fingerprint(STRATEGY_DECISION_FINGERPRINT_VERSION, content)


def compute_ranking_entry_fingerprint(
    *,
    fact_id,  # noqa: ANN001
    entity_id,  # noqa: ANN001
    fact_type: str,
    strategy_rank_tier: str,
    fact_type_priority: int,
    strategy_decision_fingerprint: str,
) -> str:
    content = {
        "fact_id": str(fact_id),
        "entity_id": str(entity_id),
        "fact_type": fact_type,
        "strategy_rank_tier": strategy_rank_tier,
        "fact_type_priority": fact_type_priority,
        "strategy_decision_fingerprint": strategy_decision_fingerprint,
    }
    return _fingerprint(RANKING_ENTRY_FINGERPRINT_VERSION, content)


def compute_ranking_policy_version(
    *, evidence_category_mapping_version: str, integration_policy_version: str
) -> str:
    """Fold the evidence-category mapping version into the ranking policy
    identity, so a mapping-table change invalidates every previously
    produced ``strategy_ranking_input_fingerprint`` even if
    ``integration_policy_version`` is unchanged."""

    return hashlib.sha256(
        f"{evidence_category_mapping_version}:{integration_policy_version}".encode()
    ).hexdigest()


def compute_strategy_ranking_input_fingerprint(
    *, ranking_policy_version: str, entry_fingerprints: Sequence[str]
) -> str:
    content = {
        "ranking_policy_version": ranking_policy_version,
        "entry_fingerprints": sorted(entry_fingerprints),
    }
    return _fingerprint(RANKING_INPUT_FINGERPRINT_VERSION, content)


def compute_strategy_selection_result_fingerprint(
    *,
    status: str,
    integration_input_fingerprint: str,
    role_strategy_context_fingerprint: str,
    strategy_decision_fingerprints: Sequence[str],
    strategy_ranking_input_fingerprint: str | None,
    evidence_category_mapping_version: str,
    integration_policy_version: str,
) -> str:
    content = {
        "status": status,
        "integration_input_fingerprint": integration_input_fingerprint,
        "role_strategy_context_fingerprint": role_strategy_context_fingerprint,
        "strategy_decision_fingerprints": sorted(strategy_decision_fingerprints),
        "strategy_ranking_input_fingerprint": strategy_ranking_input_fingerprint,
        "evidence_category_mapping_version": evidence_category_mapping_version,
        "integration_policy_version": integration_policy_version,
    }
    return _fingerprint(STRATEGY_RESULT_FINGERPRINT_VERSION, content)


def _blocked_result(
    *, integration_input_fingerprint: str, role_strategy_context_fingerprint: str
) -> StrategyAwareFactSelectionResult:
    result_fingerprint = compute_strategy_selection_result_fingerprint(
        status=StrategyAwareFactSelectionStatus.BLOCKED.value,
        integration_input_fingerprint=integration_input_fingerprint,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        strategy_decision_fingerprints=(),
        strategy_ranking_input_fingerprint=None,
        evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
    )
    return StrategyAwareFactSelectionResult(
        result_fingerprint=result_fingerprint,
        status=StrategyAwareFactSelectionStatus.BLOCKED,
        integration_input_fingerprint=integration_input_fingerprint,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        decisions=(),
        ranking=None,
        evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
        ready_for_integrated_p4=False,
    )


def build_fact_selection_with_role_strategy(
    *,
    integration_input: RoleStrategyFactSelectionInput,
    candidate_facts: Sequence[StrategyFactSelectionCandidate],
    permissions: Sequence[TruthPermissionRead],
    evaluation_time: datetime,
    positioning_signal: CareerPositioningSignal | None = None,
) -> StrategyAwareFactSelectionResult:
    """Build every strategy-aware P3 decision for one role-strategy input.

    Always revalidates the complete PRE-P4 chain first (never trusting
    ``ready_for_candidacy_thesis``/``ready_for_p4``/a caller-supplied
    fingerprint); a failed revalidation blocks the *entire* result --
    strategy can only be trusted when every upstream fingerprint has been
    independently re-derived and found fresh.
    """

    if not candidate_facts:
        raise ValueError(
            "build_fact_selection_with_role_strategy requires at least one candidate fact"
        )

    role_strategy_context_fingerprint = integration_input.role_strategy_context.context_fingerprint
    expected_integration_input_fp = compute_integration_input_fingerprint(integration_input)
    if expected_integration_input_fp != integration_input.integration_input_fingerprint:
        return _blocked_result(
            integration_input_fingerprint=expected_integration_input_fp,
            role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        )

    prep4_result = revalidate_prep4(integration_input)
    if prep4_result.status != Prep4RevalidationStatus.PASSED:
        return _blocked_result(
            integration_input_fingerprint=expected_integration_input_fp,
            role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        )

    thesis_result = integration_input.candidacy_thesis_result
    payload_set = integration_input.approved_fact_payload_set
    priority_categories = integration_input.role_strategy_context.priority_evidence_categories
    positioning_level = integration_input.role_strategy_context.positioning_level

    decisions: list[StrategyAwareFactSelectionDecision] = []
    ranking_entries: list[StrategyFactRankingEntry] = []

    for candidate in candidate_facts:
        requested_operation, operation_source = resolve_requested_operation(
            fact_type=candidate.fact.fact_type,
            baseline_requested_operation=candidate.baseline_requested_operation,
            baseline_requested_target_scope=candidate.baseline_requested_target_scope,
            positioning_level=positioning_level,
            positioning_signal=positioning_signal,
        )

        base_decision = select_fact(
            fact=candidate.fact,
            target_context=integration_input.target_context,
            requested_operation=requested_operation,
            requested_target_scope=candidate.baseline_requested_target_scope,
            permissions=permissions,
            evaluation_time=evaluation_time,
            positioning_signal=positioning_signal,
        )

        override_outcome, override_reason_code, override_mapping_fp, override_category = (
            evaluate_not_relevant_override(
                base_decision=base_decision,
                evidence_mappings=thesis_result.evidence_mappings,
                payload_set=payload_set,
                prep4_revalidation_passed=True,
            )
        )

        final_decision = (
            base_decision.decision
            if override_outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN
            else SelectionDecisionOutcome.SELECTED
        )

        mapping, is_ambiguous = find_exact_evidence_mapping(
            base_decision=base_decision, evidence_mappings=thesis_result.evidence_mappings
        )
        has_exact_mapping = mapping is not None and not is_ambiguous
        supporting_mapping_fp = (
            override_mapping_fp
            if override_mapping_fp is not None
            else (mapping.evidence_mapping_fingerprint if has_exact_mapping else None)
        )
        mapping_category = override_category or (mapping.role_evidence_category if has_exact_mapping else None)

        is_final_selected = final_decision == SelectionDecisionOutcome.SELECTED
        if is_final_selected:
            strategy_rank_tier, matched_category = resolve_strategy_tier(
                fact_type=candidate.fact.fact_type,
                has_exact_evidence_mapping=has_exact_mapping,
                priority_evidence_categories=priority_categories,
            )
            role_evidence_category: RoleEvidenceCategory | None = (
                mapping_category if strategy_rank_tier == StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT
                else matched_category
            )
        else:
            strategy_rank_tier = StrategyRankTier.TIER_2_LEGACY_ONLY
            role_evidence_category = None
            supporting_mapping_fp = None

        strategy_decision_fingerprint = compute_strategy_decision_fingerprint(
            base_decision_fingerprint=base_decision.decision_fingerprint,
            base_decision_outcome=base_decision.decision.value,
            final_decision=final_decision.value,
            override_outcome=override_outcome.value,
            override_reason_code=override_reason_code.value,
            supporting_evidence_mapping_fingerprint=supporting_mapping_fp,
            role_evidence_category=role_evidence_category.value if role_evidence_category else None,
            strategy_rank_tier=strategy_rank_tier.value,
            requested_operation_source=operation_source.value,
            evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
            integration_policy_version=INTEGRATION_POLICY_VERSION,
        )

        decision = StrategyAwareFactSelectionDecision(
            base_decision=base_decision,
            base_decision_fingerprint=base_decision.decision_fingerprint,
            base_decision_outcome=base_decision.decision,
            final_decision=final_decision,
            override_outcome=override_outcome,
            override_reason_code=override_reason_code,
            supporting_evidence_mapping_fingerprint=supporting_mapping_fp,
            role_evidence_category=role_evidence_category,
            strategy_rank_tier=strategy_rank_tier,
            requested_operation_source=operation_source,
            strategy_decision_fingerprint=strategy_decision_fingerprint,
        )
        decisions.append(decision)

        if is_final_selected:
            fact_type_priority = FACT_TYPE_PRIORITY.get(candidate.fact.fact_type, 0)
            entry_fingerprint = compute_ranking_entry_fingerprint(
                fact_id=candidate.fact.fact_id,
                entity_id=candidate.fact.entity_id,
                fact_type=candidate.fact.fact_type,
                strategy_rank_tier=strategy_rank_tier.value,
                fact_type_priority=fact_type_priority,
                strategy_decision_fingerprint=strategy_decision_fingerprint,
            )
            ranking_entries.append(
                StrategyFactRankingEntry(
                    fact_id=candidate.fact.fact_id,
                    entity_id=candidate.fact.entity_id,
                    fact_type=candidate.fact.fact_type,
                    strategy_rank_tier=strategy_rank_tier,
                    fact_type_priority=fact_type_priority,
                    strategy_decision_fingerprint=strategy_decision_fingerprint,
                    ranking_entry_fingerprint=entry_fingerprint,
                )
            )

    ranking_entries.sort(
        key=lambda entry: (
            entry.strategy_rank_tier.value,
            entry.fact_type_priority,
            str(entry.entity_id),
            str(entry.fact_id),
        )
    )
    ranking_policy_version = compute_ranking_policy_version(
        evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
    )
    ranking_input_fingerprint = compute_strategy_ranking_input_fingerprint(
        ranking_policy_version=ranking_policy_version,
        entry_fingerprints=[entry.ranking_entry_fingerprint for entry in ranking_entries],
    )
    ranking = StrategyFactRankingInput(
        ranking_policy_version=ranking_policy_version,
        entries=tuple(ranking_entries),
        strategy_ranking_input_fingerprint=ranking_input_fingerprint,
    )

    result_fingerprint = compute_strategy_selection_result_fingerprint(
        status=StrategyAwareFactSelectionStatus.READY.value,
        integration_input_fingerprint=expected_integration_input_fp,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        strategy_decision_fingerprints=[d.strategy_decision_fingerprint for d in decisions],
        strategy_ranking_input_fingerprint=ranking_input_fingerprint,
        evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
    )

    return StrategyAwareFactSelectionResult(
        result_fingerprint=result_fingerprint,
        status=StrategyAwareFactSelectionStatus.READY,
        integration_input_fingerprint=expected_integration_input_fp,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        decisions=tuple(decisions),
        ranking=ranking,
        evidence_category_mapping_version=ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
        ready_for_integrated_p4=True,
    )


__all__ = [
    "compute_integration_input_fingerprint",
    "compute_strategy_decision_fingerprint",
    "compute_ranking_policy_version",
    "compute_ranking_entry_fingerprint",
    "compute_strategy_ranking_input_fingerprint",
    "compute_strategy_selection_result_fingerprint",
    "build_fact_selection_with_role_strategy",
]
