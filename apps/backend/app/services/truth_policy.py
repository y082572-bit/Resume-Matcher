"""Fail-closed P1 policy hierarchy; not connected to legacy CV flows."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.truth_fact import (
    FactStatus,
    Transferability,
    TransformationOperation,
)

#: Registry-local policy version. Deliberately independent of
#: ``truth_fingerprint.POLICY_REGISTRY_VERSION`` (which versions the overall
#: Truth snapshot projection for P1 persistence, a different concern): this
#: constant versions only the ``TransformationPolicy`` content below, and
#: must be bumped whenever ``allowed_operations``/``allowed_target_scopes``
#: change for any fact_type. Bumped to v2 in remediation R2 for the
#: ACHIEVEMENT/FLATTEN_FOR_LOWER_ROLE addition.
POLICY_REGISTRY_VERSION = "truth-transformation-policy-v2"


@dataclass(frozen=True)
class PolicyContext:
    fact_type: str
    fact_status: FactStatus
    transferability: Transferability
    operation: TransformationOperation
    target_scope: str
    same_entity_scope: bool = True
    same_employment_scope: bool = True
    protected_numbers_unchanged: bool = True


@dataclass(frozen=True)
class TransformationPolicy:
    fact_type: str
    allowed_operations: frozenset[TransformationOperation]
    allowed_target_scopes: frozenset[str]


@dataclass(frozen=True)
class EffectivePermissionDecision:
    allowed: bool
    reason_code: str


class HardSafetyPolicy:
    """Rules which an explicit permission can never override."""

    @staticmethod
    def evaluate(context: PolicyContext) -> EffectivePermissionDecision:
        if context.fact_status in {FactStatus.REJECTED, FactStatus.ARCHIVED}:
            return EffectivePermissionDecision(False, "HARD_SAFETY_FACT_INELIGIBLE")
        if (
            context.transferability == Transferability.EXACT_ONLY
            and context.operation == TransformationOperation.CONTROLLED_REPHRASE
        ):
            return EffectivePermissionDecision(False, "HARD_SAFETY_EXACT_ONLY_REPHRASE")
        if (
            context.transferability == Transferability.NON_TRANSFERABLE
            and not context.same_entity_scope
        ):
            return EffectivePermissionDecision(False, "HARD_SAFETY_ENTITY_TRANSFER")
        if (
            context.transferability == Transferability.EMPLOYMENT_SCOPED
            and not context.same_employment_scope
        ):
            return EffectivePermissionDecision(False, "HARD_SAFETY_EMPLOYMENT_TRANSFER")
        if not context.protected_numbers_unchanged:
            return EffectivePermissionDecision(False, "HARD_SAFETY_PROTECTED_NUMBER_CHANGED")
        return EffectivePermissionDecision(True, "HARD_SAFETY_ALLOWED")


class TruthTransformationPolicyRegistry:
    """Small, versioned registry. Unknown fact types deny by default."""

    version = POLICY_REGISTRY_VERSION

    def __init__(self) -> None:
        copy_only = frozenset(
            {TransformationOperation.EXACT_COPY, TransformationOperation.FORMAT_NORMALIZATION}
        )
        narrative = frozenset(
            {
                TransformationOperation.EXACT_COPY,
                TransformationOperation.CONTROLLED_REPHRASE,
                TransformationOperation.OMIT,
                TransformationOperation.REORDER,
            }
        )
        # ACHIEVEMENT only: a narrative fact_type whose value is a
        # free-text accomplishment description, never a company name,
        # employment period, formal role title, or a protected number.
        # FLATTEN_FOR_LOWER_ROLE changes only how that description is
        # presented (e.g. de-emphasizing scope of ownership for a lower
        # target role); it cannot alter the fact's truth, its assigned
        # employment, or create a new competency. Deliberately not added to
        # the shared `narrative` set: RESPONSIBILITY/SKILL/TOOL/TECHNOLOGY/
        # SUMMARY do not gain this operation.
        achievement_narrative = narrative | frozenset(
            {TransformationOperation.FLATTEN_FOR_LOWER_ROLE}
        )
        self._policies = {
            "COMPANY": TransformationPolicy(
                "COMPANY", copy_only, frozenset({"EMPLOYMENT_HEADER"})
            ),
            "ROLE": TransformationPolicy(
                "ROLE", copy_only, frozenset({"EMPLOYMENT_HEADER"})
            ),
            "EMPLOYMENT_PERIOD": TransformationPolicy(
                "EMPLOYMENT_PERIOD", copy_only, frozenset({"EMPLOYMENT_HEADER"})
            ),
            "RESPONSIBILITY": TransformationPolicy(
                "RESPONSIBILITY", narrative, frozenset({"EXPERIENCE"})
            ),
            "ACHIEVEMENT": TransformationPolicy(
                "ACHIEVEMENT", achievement_narrative, frozenset({"EXPERIENCE", "PROJECT"})
            ),
            "SKILL": TransformationPolicy(
                "SKILL", narrative, frozenset({"SUMMARY", "SKILLS", "EXPERIENCE"})
            ),
            "TOOL": TransformationPolicy(
                "TOOL", narrative, frozenset({"SKILLS", "EXPERIENCE"})
            ),
            "TECHNOLOGY": TransformationPolicy(
                "TECHNOLOGY", narrative, frozenset({"SKILLS", "EXPERIENCE"})
            ),
            "SUMMARY": TransformationPolicy(
                "SUMMARY", narrative, frozenset({"SUMMARY"})
            ),
        }

    def get(self, fact_type: str) -> TransformationPolicy | None:
        return self._policies.get(fact_type)

    def evaluate(
        self,
        context: PolicyContext,
        *,
        permission_operations: frozenset[TransformationOperation],
        permission_target_scope: str,
        permission_active: bool,
    ) -> EffectivePermissionDecision:
        hard = HardSafetyPolicy.evaluate(context)
        if not hard.allowed:
            return hard
        policy = self.get(context.fact_type)
        if policy is None:
            return EffectivePermissionDecision(False, "TRANSFORMATION_POLICY_MISSING")
        if (
            context.operation not in policy.allowed_operations
            or context.target_scope not in policy.allowed_target_scopes
        ):
            return EffectivePermissionDecision(False, "TRANSFORMATION_POLICY_DENIED")
        if not permission_active:
            return EffectivePermissionDecision(False, "EXPLICIT_PERMISSION_INACTIVE")
        if (
            context.operation not in permission_operations
            or context.target_scope != permission_target_scope
        ):
            return EffectivePermissionDecision(False, "EXPLICIT_PERMISSION_DENIED")
        return EffectivePermissionDecision(True, "EFFECTIVE_PERMISSION_ALLOWED")
