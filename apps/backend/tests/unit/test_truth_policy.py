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
