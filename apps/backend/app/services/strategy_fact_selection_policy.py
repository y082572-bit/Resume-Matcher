"""Closed, versioned strategy-aware P3 policy: the ``NOT_RELEVANT`` override
gate, the ``RoleEvidenceCategory`` -> ``fact_type`` mapping, and the
positioning-driven ``FLATTEN_FOR_LOWER_ROLE`` request.

Nothing here can widen a legacy P3 outcome: ``evaluate_not_relevant_override``
only ever raises an *advisory* ``NOT_RELEVANT`` to ``SELECTED``, and only for
an exact, verified thesis-supporting fact. ``BLOCKED``/``EXCLUDED``/
``APPROVAL_REQUIRED`` are never touched here -- that hard-gate preservation
is additionally enforced structurally by
``StrategyAwareFactSelectionDecision``'s own model validators.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.candidacy_thesis import (
    CandidacyEvidenceMapping,
    PositioningLevel,
    RoleEvidenceCategory,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet
from app.schemas.fact_selection import CareerPositioningSignal, FactSelectionDecision, SelectionDecisionOutcome
from app.schemas.strategy_fact_selection import (
    StrategyOverrideOutcome,
    StrategyOverrideReasonCode,
    StrategyRankTier,
    StrategyRequestedOperationSource,
    ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION,
)
from app.schemas.truth_fact import TransformationOperation
from app.services.truth_policy import TruthTransformationPolicyRegistry


#: The single, closed, versioned source of truth mapping a
#: ``RoleEvidenceCategory`` onto the real, already-registered
#: (``TruthTransformationPolicyRegistry``) fact_types it may match. Never
#: matched by prefix/substring, never inferred from co-located entities.
#: Bump ``ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION`` whenever this changes.
ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES: dict[RoleEvidenceCategory, frozenset[str]] = {
    RoleEvidenceCategory.SCALE_OF_RESPONSIBILITY: frozenset(
        {"EMPLOYMENT_RESPONSIBILITY_SCALE", "EMPLOYMENT_NUMERIC_RESULT"}
    ),
    RoleEvidenceCategory.BUSINESS_RESULT: frozenset({"EMPLOYMENT_NUMERIC_RESULT", "ACHIEVEMENT"}),
    RoleEvidenceCategory.TRANSFORMATION: frozenset({"ACHIEVEMENT", "EMPLOYMENT_NUMERIC_RESULT"}),
    RoleEvidenceCategory.TEAM_LEADERSHIP: frozenset(
        {"EMPLOYMENT_RESPONSIBILITY_SCALE", "RESPONSIBILITY"}
    ),
    RoleEvidenceCategory.DISTRIBUTION_DEVELOPMENT: frozenset({"EMPLOYMENT_ACTIVITY", "RESPONSIBILITY"}),
    RoleEvidenceCategory.PARTNERSHIP_MANAGEMENT: frozenset({"EMPLOYMENT_ACTIVITY", "RESPONSIBILITY"}),
    RoleEvidenceCategory.PRODUCT_IMPLEMENTATION: frozenset({"EMPLOYMENT_ACTIVITY", "TOOL", "TECHNOLOGY"}),
    RoleEvidenceCategory.PROCESS_IMPROVEMENT: frozenset({"EMPLOYMENT_ACTIVITY", "RESPONSIBILITY"}),
    RoleEvidenceCategory.STAKEHOLDER_MANAGEMENT: frozenset({"RESPONSIBILITY", "EMPLOYMENT_ACTIVITY"}),
    RoleEvidenceCategory.REGULATORY_OR_RISK_RESPONSIBILITY: frozenset(
        {"RESPONSIBILITY", "EMPLOYMENT_RESPONSIBILITY_SCALE"}
    ),
    RoleEvidenceCategory.CUSTOMER_EXPERIENCE: frozenset({"EMPLOYMENT_ACTIVITY", "RESPONSIBILITY"}),
    RoleEvidenceCategory.TRAINING_AND_CAPABILITY_BUILDING: frozenset({"SKILL", "COURSE_NAME"}),
}

#: PositioningLevel values that may ever request FLATTEN_FOR_LOWER_ROLE.
#: EXECUTIVE/SENIOR_LEADERSHIP never auto-request it; MANAGEMENT does not
#: force it either -- it simply is not a member of this set.
FLATTEN_ELIGIBLE_POSITIONING_LEVELS: frozenset[PositioningLevel] = frozenset(
    {PositioningLevel.EXPERT, PositioningLevel.SPECIALIST, PositioningLevel.OPERATIONAL}
)

_FACT_TYPE_POLICY_REGISTRY = TruthTransformationPolicyRegistry()


def resolve_strategy_tier(
    *,
    fact_type: str,
    has_exact_evidence_mapping: bool,
    priority_evidence_categories: Sequence[RoleEvidenceCategory],
) -> tuple[StrategyRankTier, RoleEvidenceCategory | None]:
    """Classify a SELECTED (post-override) fact into its strategy tier.

    Tier 0 requires an already-verified exact evidence mapping (checked by
    the caller, never re-derived here from entity co-location). Tier 1
    requires the fact's real ``fact_type`` to belong to a priority
    ``RoleEvidenceCategory`` per the closed table above -- never because it
    shares an ``employment_scope_entity_id`` or ``entity_id`` with another
    fact. Ties among matching categories are broken deterministically by
    ``RoleEvidenceCategory.value``.
    """

    if has_exact_evidence_mapping:
        return StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT, None

    for category in sorted(priority_evidence_categories, key=lambda item: item.value):
        if fact_type in ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES.get(category, frozenset()):
            return StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH, category

    return StrategyRankTier.TIER_2_LEGACY_ONLY, None


def find_exact_evidence_mapping(
    *,
    base_decision: FactSelectionDecision,
    evidence_mappings: Sequence[CandidacyEvidenceMapping],
) -> tuple[CandidacyEvidenceMapping | None, bool]:
    """Return ``(mapping, is_ambiguous)`` for the exact-identity match.

    A match requires ``fact_id``, ``entity_id`` and ``fact_revision`` to
    agree exactly -- never a category-only or text-similarity match.
    ``is_ambiguous=True`` when more than one mapping matches, which is
    always a fail-closed condition for the caller.
    """

    matches = [
        mapping
        for mapping in evidence_mappings
        if mapping.fact_id == base_decision.fact_id
        and mapping.entity_id == base_decision.entity_id
        and mapping.fact_revision == base_decision.fact_revision
    ]
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        return None, True
    return None, False


def evaluate_not_relevant_override(
    *,
    base_decision: FactSelectionDecision,
    evidence_mappings: Sequence[CandidacyEvidenceMapping],
    payload_set: ApprovedFactPayloadSet,
    prep4_revalidation_passed: bool,
) -> tuple[StrategyOverrideOutcome, StrategyOverrideReasonCode, str | None, RoleEvidenceCategory | None]:
    """Decide whether an advisory ``NOT_RELEVANT`` decision may become
    ``SELECTED``. Returns
    ``(override_outcome, reason_code, supporting_evidence_mapping_fingerprint, role_evidence_category)``.

    Fail-closed at every step: an unknown fact, a payload mismatch, an
    ambiguous or missing evidence mapping, or a failed PRE-P4 revalidation
    (including a stale ``CandidacyThesisResult``) all leave the decision at
    ``NOT_OVERRIDDEN`` -- the original ``NOT_RELEVANT`` outcome stands.
    """

    if not prep4_revalidation_passed:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.PREP4_REVALIDATION_FAILED,
            None,
            None,
        )
    if base_decision.decision != SelectionDecisionOutcome.NOT_RELEVANT:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.BASE_DECISION_NOT_NOT_RELEVANT,
            None,
            None,
        )

    mapping, is_ambiguous = find_exact_evidence_mapping(
        base_decision=base_decision, evidence_mappings=evidence_mappings
    )
    if is_ambiguous:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.MULTIPLE_EVIDENCE_MAPPINGS,
            None,
            None,
        )
    if mapping is None:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.NO_EVIDENCE_MAPPING,
            None,
            None,
        )

    payload = next(
        (
            item
            for item in payload_set.payloads
            if item.fact_id == base_decision.fact_id and item.entity_id == base_decision.entity_id
        ),
        None,
    )
    if payload is None:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.PAYLOAD_SNAPSHOT_MISSING,
            None,
            None,
        )
    if (
        payload.fact_revision != base_decision.fact_revision
        or payload.fact_type != base_decision.fact_type
    ):
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.PAYLOAD_IDENTITY_MISMATCH,
            None,
            None,
        )
    if payload.fact_content_fingerprint != base_decision.fact_content_fingerprint:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.PAYLOAD_CONTENT_FINGERPRINT_MISMATCH,
            None,
            None,
        )
    if mapping.payload_fingerprint != payload.payload_fingerprint:
        return (
            StrategyOverrideOutcome.NOT_OVERRIDDEN,
            StrategyOverrideReasonCode.MAPPING_PAYLOAD_FINGERPRINT_MISMATCH,
            None,
            None,
        )

    return (
        StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED,
        StrategyOverrideReasonCode.EXACT_THESIS_SUPPORTING_FACT,
        mapping.evidence_mapping_fingerprint,
        mapping.role_evidence_category,
    )


def resolve_requested_operation(
    *,
    fact_type: str,
    baseline_requested_operation: TransformationOperation,
    baseline_requested_target_scope: str,
    positioning_level: PositioningLevel,
    positioning_signal: CareerPositioningSignal | None = None,
) -> tuple[TransformationOperation, StrategyRequestedOperationSource]:
    """Decide whether this fact's requested operation becomes
    ``FLATTEN_FOR_LOWER_ROLE``.

    Precedence: (1) the existing P1 ``TruthTransformationPolicyRegistry``
    must already allow ``FLATTEN_FOR_LOWER_ROLE`` for this exact
    ``(fact_type, target_scope)`` pair; (2) a fresh
    ``RoleStrategyContext.positioning_level`` in
    ``FLATTEN_ELIGIBLE_POSITIONING_LEVELS`` is the primary trigger; (3) an
    advisory ``CareerPositioningSignal`` is consulted only to detect a
    conflict (``seniority_direction=="ELEVATE"``); (4) on conflict, no
    automatic change -- the legacy default is kept. Legacy ``select_fact``
    still decides SELECTED/BLOCKED/APPROVAL_REQUIRED for whatever operation
    is requested here; this function never grants anything by itself.
    """

    if positioning_level not in FLATTEN_ELIGIBLE_POSITIONING_LEVELS:
        return baseline_requested_operation, StrategyRequestedOperationSource.LEGACY_DEFAULT

    policy = _FACT_TYPE_POLICY_REGISTRY.get(fact_type)
    if policy is None or TransformationOperation.FLATTEN_FOR_LOWER_ROLE not in policy.allowed_operations:
        return baseline_requested_operation, StrategyRequestedOperationSource.LEGACY_DEFAULT
    if baseline_requested_target_scope not in policy.allowed_target_scopes:
        return baseline_requested_operation, StrategyRequestedOperationSource.LEGACY_DEFAULT

    if positioning_signal is not None and positioning_signal.seniority_direction == "ELEVATE":
        return baseline_requested_operation, StrategyRequestedOperationSource.LEGACY_DEFAULT

    return (
        TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        StrategyRequestedOperationSource.POSITIONING_FLATTEN_REQUESTED,
    )


__all__ = [
    "ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION",
    "ROLE_EVIDENCE_CATEGORY_TO_FACT_TYPES",
    "FLATTEN_ELIGIBLE_POSITIONING_LEVELS",
    "resolve_strategy_tier",
    "find_exact_evidence_mapping",
    "evaluate_not_relevant_override",
    "resolve_requested_operation",
]
