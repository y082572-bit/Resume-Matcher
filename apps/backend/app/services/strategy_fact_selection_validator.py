"""Independent, fail-closed validation of a ``StrategyAwareFactSelectionResult``.

Nothing here trusts ``result.ready_for_integrated_p4``, a stored
``strategy_decision_fingerprint``, a stored ``strategy_rank_tier``, or the
stored ``override_outcome``. Every one of those is independently
re-derived from ``result.decisions[*].base_decision`` (the untouched legacy
P3 decision) plus the exact current ``RoleStrategyFactSelectionInput``, and
compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.fact_selection import SelectionDecisionOutcome
from app.schemas.strategy_fact_selection import (
    RoleStrategyFactSelectionInput,
    StrategyAwareFactSelectionResult,
    StrategyAwareFactSelectionStatus,
    StrategyRankTier,
)
from app.services.role_strategy_prep4_revalidation import Prep4RevalidationStatus, revalidate_prep4
from app.services.strategy_fact_selection_builder import (
    compute_integration_input_fingerprint,
    compute_ranking_entry_fingerprint,
    compute_ranking_policy_version,
    compute_strategy_decision_fingerprint,
    compute_strategy_ranking_input_fingerprint,
    compute_strategy_selection_result_fingerprint,
)
from app.services.cv_content_plan_policy import FACT_TYPE_PRIORITY
from app.services.strategy_fact_selection_policy import (
    evaluate_not_relevant_override,
    find_exact_evidence_mapping,
    resolve_strategy_tier,
)


class StrategyFactSelectionValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class StrategyFactSelectionValidationViolationCode(StrEnum):
    INTEGRATION_INPUT_FINGERPRINT_MISMATCH = "INTEGRATION_INPUT_FINGERPRINT_MISMATCH"
    PREP4_REVALIDATION_FAILED = "PREP4_REVALIDATION_FAILED"
    DECISION_STRATEGY_FINGERPRINT_MISMATCH = "DECISION_STRATEGY_FINGERPRINT_MISMATCH"
    DECISION_OVERRIDE_MISMATCH = "DECISION_OVERRIDE_MISMATCH"
    DECISION_TIER_MISMATCH = "DECISION_TIER_MISMATCH"
    RANKING_ENTRY_FINGERPRINT_MISMATCH = "RANKING_ENTRY_FINGERPRINT_MISMATCH"
    RANKING_ENTRIES_MISMATCH = "RANKING_ENTRIES_MISMATCH"
    RANKING_INPUT_FINGERPRINT_MISMATCH = "RANKING_INPUT_FINGERPRINT_MISMATCH"
    RESULT_FINGERPRINT_MISMATCH = "RESULT_FINGERPRINT_MISMATCH"
    READY_FLAG_MISMATCH = "READY_FLAG_MISMATCH"


@dataclass(frozen=True)
class StrategyFactSelectionValidationViolation:
    code: StrategyFactSelectionValidationViolationCode
    detail: str


@dataclass(frozen=True)
class StrategyFactSelectionValidationResult:
    status: StrategyFactSelectionValidationStatus
    violations: tuple[StrategyFactSelectionValidationViolation, ...]
    verified_ready_for_integrated_p4: bool


def validate_strategy_fact_selection_result(
    result: StrategyAwareFactSelectionResult,
    integration_input: RoleStrategyFactSelectionInput,
) -> StrategyFactSelectionValidationResult:
    violations: list[StrategyFactSelectionValidationViolation] = []

    expected_integration_input_fp = compute_integration_input_fingerprint(integration_input)
    if expected_integration_input_fp != result.integration_input_fingerprint:
        violations.append(
            StrategyFactSelectionValidationViolation(
                StrategyFactSelectionValidationViolationCode.INTEGRATION_INPUT_FINGERPRINT_MISMATCH,
                "result.integration_input_fingerprint does not match the recomputed value",
            )
        )

    prep4_result = revalidate_prep4(integration_input)
    prep4_passed = prep4_result.status == Prep4RevalidationStatus.PASSED
    if not prep4_passed:
        violations.append(
            StrategyFactSelectionValidationViolation(
                StrategyFactSelectionValidationViolationCode.PREP4_REVALIDATION_FAILED,
                f"PRE-P4 revalidation failed: {[v.code.value for v in prep4_result.violations]}",
            )
        )

    thesis_result = integration_input.candidacy_thesis_result
    payload_set = integration_input.approved_fact_payload_set
    priority_categories = integration_input.role_strategy_context.priority_evidence_categories

    ranking_entry_fps: list[str] = []
    for decision in result.decisions:
        override_outcome, override_reason_code, override_mapping_fp, override_category = (
            evaluate_not_relevant_override(
                base_decision=decision.base_decision,
                evidence_mappings=thesis_result.evidence_mappings,
                payload_set=payload_set,
                prep4_revalidation_passed=prep4_passed,
            )
        )
        if (
            override_outcome != decision.override_outcome
            or override_reason_code != decision.override_reason_code
        ):
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.DECISION_OVERRIDE_MISMATCH,
                    f"fact_id={decision.base_decision.fact_id} override does not match recomputed value",
                )
            )

        mapping, is_ambiguous = find_exact_evidence_mapping(
            base_decision=decision.base_decision, evidence_mappings=thesis_result.evidence_mappings
        )
        has_exact_mapping = mapping is not None and not is_ambiguous
        mapping_category = override_category or (
            mapping.role_evidence_category if has_exact_mapping else None
        )

        is_final_selected = decision.final_decision == SelectionDecisionOutcome.SELECTED
        if is_final_selected:
            expected_tier, matched_category = resolve_strategy_tier(
                fact_type=decision.base_decision.fact_type,
                has_exact_evidence_mapping=has_exact_mapping,
                priority_evidence_categories=priority_categories,
            )
            expected_category = (
                mapping_category
                if expected_tier == StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT
                else matched_category
            )
        else:
            expected_tier = StrategyRankTier.TIER_2_LEGACY_ONLY
            expected_category = None

        if expected_tier != decision.strategy_rank_tier or expected_category != decision.role_evidence_category:
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.DECISION_TIER_MISMATCH,
                    f"fact_id={decision.base_decision.fact_id} strategy_rank_tier/role_evidence_category "
                    "does not match recomputed value",
                )
            )

        expected_decision_fp = compute_strategy_decision_fingerprint(
            base_decision_fingerprint=decision.base_decision_fingerprint,
            base_decision_outcome=decision.base_decision_outcome.value,
            final_decision=decision.final_decision.value,
            override_outcome=decision.override_outcome.value,
            override_reason_code=decision.override_reason_code.value,
            supporting_evidence_mapping_fingerprint=decision.supporting_evidence_mapping_fingerprint,
            role_evidence_category=(
                decision.role_evidence_category.value if decision.role_evidence_category else None
            ),
            strategy_rank_tier=decision.strategy_rank_tier.value,
            requested_operation_source=decision.requested_operation_source.value,
            evidence_category_mapping_version=result.evidence_category_mapping_version,
            integration_policy_version=result.integration_policy_version,
        )
        if expected_decision_fp != decision.strategy_decision_fingerprint:
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.DECISION_STRATEGY_FINGERPRINT_MISMATCH,
                    f"fact_id={decision.base_decision.fact_id} strategy_decision_fingerprint "
                    "does not match recomputed value",
                )
            )

        if is_final_selected:
            fact_type_priority = FACT_TYPE_PRIORITY.get(decision.base_decision.fact_type, 0)
            ranking_entry_fps.append(
                compute_ranking_entry_fingerprint(
                    fact_id=decision.base_decision.fact_id,
                    entity_id=decision.base_decision.entity_id,
                    fact_type=decision.base_decision.fact_type,
                    strategy_rank_tier=decision.strategy_rank_tier.value,
                    fact_type_priority=fact_type_priority,
                    strategy_decision_fingerprint=decision.strategy_decision_fingerprint,
                )
            )

    if result.ranking is None:
        if ranking_entry_fps:
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.RANKING_ENTRIES_MISMATCH,
                    "result carries no ranking but SELECTED decisions were found",
                )
            )
    else:
        stored_entry_fps = sorted(entry.ranking_entry_fingerprint for entry in result.ranking.entries)
        if stored_entry_fps != sorted(ranking_entry_fps):
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.RANKING_ENTRIES_MISMATCH,
                    "result.ranking.entries do not match the recomputed SELECTED fact set",
                )
            )
        expected_ranking_policy_version = compute_ranking_policy_version(
            evidence_category_mapping_version=result.evidence_category_mapping_version,
            integration_policy_version=result.integration_policy_version,
        )
        if expected_ranking_policy_version != result.ranking.ranking_policy_version:
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.RANKING_INPUT_FINGERPRINT_MISMATCH,
                    "result.ranking.ranking_policy_version does not match its recomputed value",
                )
            )
        expected_ranking_input_fp = compute_strategy_ranking_input_fingerprint(
            ranking_policy_version=expected_ranking_policy_version,
            entry_fingerprints=stored_entry_fps,
        )
        if expected_ranking_input_fp != result.ranking.strategy_ranking_input_fingerprint:
            violations.append(
                StrategyFactSelectionValidationViolation(
                    StrategyFactSelectionValidationViolationCode.RANKING_INPUT_FINGERPRINT_MISMATCH,
                    "result.ranking.strategy_ranking_input_fingerprint does not match its "
                    "recomputed value",
                )
            )

    expected_result_fp = compute_strategy_selection_result_fingerprint(
        status=result.status.value,
        integration_input_fingerprint=result.integration_input_fingerprint,
        role_strategy_context_fingerprint=result.role_strategy_context_fingerprint,
        strategy_decision_fingerprints=[d.strategy_decision_fingerprint for d in result.decisions],
        strategy_ranking_input_fingerprint=(
            result.ranking.strategy_ranking_input_fingerprint if result.ranking else None
        ),
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
    )
    if expected_result_fp != result.result_fingerprint:
        violations.append(
            StrategyFactSelectionValidationViolation(
                StrategyFactSelectionValidationViolationCode.RESULT_FINGERPRINT_MISMATCH,
                "result.result_fingerprint does not match its recomputed value",
            )
        )

    verified_ready = (
        prep4_passed
        and result.status == StrategyAwareFactSelectionStatus.READY
        and bool(result.decisions)
        and result.ranking is not None
        and not violations
    )
    if verified_ready != result.ready_for_integrated_p4:
        violations.append(
            StrategyFactSelectionValidationViolation(
                StrategyFactSelectionValidationViolationCode.READY_FLAG_MISMATCH,
                "result.ready_for_integrated_p4 does not match the independently verified value",
            )
        )

    status = (
        StrategyFactSelectionValidationStatus.VALID
        if not violations
        else StrategyFactSelectionValidationStatus.INVALID
    )
    return StrategyFactSelectionValidationResult(
        status=status, violations=tuple(violations), verified_ready_for_integrated_p4=verified_ready
    )


__all__ = [
    "StrategyFactSelectionValidationStatus",
    "StrategyFactSelectionValidationViolationCode",
    "StrategyFactSelectionValidationViolation",
    "StrategyFactSelectionValidationResult",
    "validate_strategy_fact_selection_result",
]
