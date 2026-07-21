"""Hard safety remains stronger than policy and explicit permission."""

import pytest

from app.schemas.truth_fact import FactStatus, Transferability, TransformationOperation
from app.services.truth_policy import (
    HardSafetyPolicy,
    PolicyContext,
    TruthTransformationPolicyRegistry,
)


def _context(**changes: object) -> PolicyContext:
    values = {
        "fact_type": "SKILL",
        "fact_status": FactStatus.CONFIRMED,
        "transferability": Transferability.GLOBAL_TRANSFERABLE,
        "operation": TransformationOperation.EXACT_COPY,
        "target_scope": "SKILLS",
    }
    values.update(changes)
    return PolicyContext(**values)


@pytest.mark.parametrize("status", [FactStatus.REJECTED, FactStatus.ARCHIVED])
def test_ineligible_fact_is_always_denied(status: FactStatus) -> None:
    registry = TruthTransformationPolicyRegistry()
    decision = registry.evaluate(
        _context(fact_status=status),
        permission_operations=frozenset(TransformationOperation),
        permission_target_scope="SKILLS",
        permission_active=True,
    )
    assert not decision.allowed
    assert decision.reason_code == "HARD_SAFETY_FACT_INELIGIBLE"


def test_permission_cannot_rephrase_exact_only_fact() -> None:
    context = _context(
        transferability=Transferability.EXACT_ONLY,
        operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    assert not HardSafetyPolicy.evaluate(context).allowed


def test_permission_cannot_cross_employment_or_change_number() -> None:
    assert not HardSafetyPolicy.evaluate(
        _context(
            transferability=Transferability.EMPLOYMENT_SCOPED,
            same_employment_scope=False,
        )
    ).allowed
    assert not HardSafetyPolicy.evaluate(
        _context(protected_numbers_unchanged=False)
    ).allowed


def test_missing_policy_or_permission_denies() -> None:
    registry = TruthTransformationPolicyRegistry()
    missing = registry.evaluate(
        _context(fact_type="CUSTOM_UNKNOWN"),
        permission_operations=frozenset({TransformationOperation.EXACT_COPY}),
        permission_target_scope="SKILLS",
        permission_active=True,
    )
    inactive = registry.evaluate(
        _context(),
        permission_operations=frozenset({TransformationOperation.EXACT_COPY}),
        permission_target_scope="SKILLS",
        permission_active=False,
    )
    assert (missing.allowed, missing.reason_code) == (False, "TRANSFORMATION_POLICY_MISSING")
    assert (inactive.allowed, inactive.reason_code) == (False, "EXPLICIT_PERMISSION_INACTIVE")


def test_exact_intersection_is_allowed() -> None:
    decision = TruthTransformationPolicyRegistry().evaluate(
        _context(),
        permission_operations=frozenset({TransformationOperation.EXACT_COPY}),
        permission_target_scope="SKILLS",
        permission_active=True,
    )
    assert (decision.allowed, decision.reason_code) == (
        True,
        "EFFECTIVE_PERMISSION_ALLOWED",
    )


# --- Remediation R2: narrow FLATTEN_FOR_LOWER_ROLE matrix ------------------
#
# Approved minimal matrix: ACHIEVEMENT / {EXPERIENCE, PROJECT} only.
# ACHIEVEMENT is a free-text accomplishment description -- flattening it
# changes only presentation level, never the company, employment period,
# formal role title, a protected number, or the fact's assigned employment.
# COMPANY, ROLE and EMPLOYMENT_PERIOD are identity/structural fact types and
# must never gain FLATTEN_FOR_LOWER_ROLE; no other fact_type gains it either.


def test_achievement_flatten_for_lower_role_is_allowed_in_experience_scope() -> None:
    registry = TruthTransformationPolicyRegistry()
    decision = registry.evaluate(
        _context(
            fact_type="ACHIEVEMENT",
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
            target_scope="EXPERIENCE",
        ),
        permission_operations=frozenset({TransformationOperation.FLATTEN_FOR_LOWER_ROLE}),
        permission_target_scope="EXPERIENCE",
        permission_active=True,
    )
    assert (decision.allowed, decision.reason_code) == (
        True,
        "EFFECTIVE_PERMISSION_ALLOWED",
    )


def test_achievement_flatten_for_lower_role_rejects_scope_outside_policy() -> None:
    """Only ACHIEVEMENT's already-approved target_scopes (EXPERIENCE,
    PROJECT) may ever grant FLATTEN_FOR_LOWER_ROLE -- an unrelated scope
    like SUMMARY must still be denied even with an active permission."""

    registry = TruthTransformationPolicyRegistry()
    decision = registry.evaluate(
        _context(
            fact_type="ACHIEVEMENT",
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
            target_scope="SUMMARY",
        ),
        permission_operations=frozenset({TransformationOperation.FLATTEN_FOR_LOWER_ROLE}),
        permission_target_scope="SUMMARY",
        permission_active=True,
    )
    assert (decision.allowed, decision.reason_code) == (
        False,
        "TRANSFORMATION_POLICY_DENIED",
    )


@pytest.mark.parametrize("fact_type", ["COMPANY", "EMPLOYMENT_PERIOD"])
def test_identity_fact_types_never_allow_flattening(fact_type: str) -> None:
    registry = TruthTransformationPolicyRegistry()
    decision = registry.evaluate(
        _context(
            fact_type=fact_type,
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
            target_scope="EMPLOYMENT_HEADER",
        ),
        permission_operations=frozenset({TransformationOperation.FLATTEN_FOR_LOWER_ROLE}),
        permission_target_scope="EMPLOYMENT_HEADER",
        permission_active=True,
    )
    assert (decision.allowed, decision.reason_code) == (
        False,
        "TRANSFORMATION_POLICY_DENIED",
    )


def test_fact_type_outside_flattening_matrix_still_denies_flattening() -> None:
    registry = TruthTransformationPolicyRegistry()
    decision = registry.evaluate(
        _context(
            fact_type="SKILL",
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
            target_scope="SKILLS",
        ),
        permission_operations=frozenset({TransformationOperation.FLATTEN_FOR_LOWER_ROLE}),
        permission_target_scope="SKILLS",
        permission_active=True,
    )
    assert (decision.allowed, decision.reason_code) == (
        False,
        "TRANSFORMATION_POLICY_DENIED",
    )


def test_only_achievement_gains_flatten_for_lower_role() -> None:
    """No fact_type other than ACHIEVEMENT was accidentally widened."""

    registry = TruthTransformationPolicyRegistry()
    for fact_type in (
        "COMPANY",
        "ROLE",
        "EMPLOYMENT_PERIOD",
        "RESPONSIBILITY",
        "SKILL",
        "TOOL",
        "TECHNOLOGY",
        "SUMMARY",
    ):
        policy = registry.get(fact_type)
        assert policy is not None
        assert (
            TransformationOperation.FLATTEN_FOR_LOWER_ROLE not in policy.allowed_operations
        ), fact_type

    achievement_policy = registry.get("ACHIEVEMENT")
    assert achievement_policy is not None
    assert TransformationOperation.FLATTEN_FOR_LOWER_ROLE in achievement_policy.allowed_operations


def test_policy_registry_version_was_bumped_for_flattening_change() -> None:
    assert TruthTransformationPolicyRegistry.version == "truth-transformation-policy-v2"


def test_hard_safety_policy_unchanged_for_flatten_for_lower_role() -> None:
    """HardSafetyPolicy gets no new FLATTEN_FOR_LOWER_ROLE-specific
    carve-out: REJECTED/ARCHIVED still hard-block it exactly like every
    other operation, and EXACT_ONLY's CONTROLLED_REPHRASE-only block does
    not extend to it."""

    rejected = HardSafetyPolicy.evaluate(
        _context(
            fact_status=FactStatus.REJECTED,
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        )
    )
    assert (rejected.allowed, rejected.reason_code) == (
        False,
        "HARD_SAFETY_FACT_INELIGIBLE",
    )

    exact_only = HardSafetyPolicy.evaluate(
        _context(
            transferability=Transferability.EXACT_ONLY,
            operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        )
    )
    assert (exact_only.allowed, exact_only.reason_code) == (True, "HARD_SAFETY_ALLOWED")
