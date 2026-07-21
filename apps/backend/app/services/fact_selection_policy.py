"""Deterministic P3 fact-selection decision logic.

This module implements three independent, composable gates over an
existing ``TruthFact`` plus its existing ``TruthPermission`` records:

1. **Eligibility** -- derived only from ``TruthFact.status``, ``use_in_cv``
   and ``requires_approval``.
2. **Transferability** -- derived only from ``TruthFact.transferability``,
   ``TruthFact.employment_scope_entity_id`` and the caller-supplied
   ``TargetContext``.
3. **Permission** -- derived only from the caller-supplied list of
   ``TruthPermissionRead`` records and the caller-supplied
   ``evaluation_time``.

A fourth, advisory-only step folds in an existing
``CareerPositioningSignal``: it can only downgrade an otherwise-``SELECTED``
decision to ``NOT_RELEVANT`` (via an explicit ``fact_id``, never a text
match), and can only add advisory flags. It never overrides eligibility,
transferability or permission.

Nothing here performs a database query, reads legacy Truth Library JSON, or
persists a decision. It runs no clock: ``evaluation_time`` is always an
explicit input.

A fifth gate -- ``evaluate_fact_type_policy`` -- sits between transferability
and permission. It consults the existing, frozen P1
``TruthTransformationPolicyRegistry`` (``app.services.truth_policy``) and is
the sole authority on which ``target_scope`` and ``requested_operation`` a
``fact_type`` may ever use. Neither the base-operation shortcut nor an
active ``TruthPermission`` can grant anything this gate has already denied:
permission can only narrow what P1 allows, never extend it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.schemas.fact_selection import (
    CareerPositioningSignal,
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_fact import (
    FactStatus,
    PermissionStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
    TruthPermissionRead,
)
from app.services.truth_fingerprint import canonical_json_bytes
from app.services.truth_policy import TruthTransformationPolicyRegistry


SELECTION_POLICY_VERSION = "fact-selection-policy-v2"
TARGET_CONTEXT_FINGERPRINT_VERSION = "fact-selection-target-context-v1"
DECISION_FINGERPRINT_VERSION = "fact-selection-decision-v1"

#: Operations that a fact may use without any explicit, active TruthPermission,
#: provided ``evaluate_fact_type_policy`` has already allowed the exact
#: fact_type/target_scope/operation combination. This is never a global
#: default-allow: reaching this set does not by itself grant anything.
BASE_OPERATIONS: frozenset[TransformationOperation] = frozenset(
    {TransformationOperation.EXACT_COPY, TransformationOperation.FORMAT_NORMALIZATION}
)

#: The existing P1 policy registry. Stateless and versioned; instantiated
#: once and only ever read from -- P3 never mutates or extends it.
_FACT_TYPE_POLICY_REGISTRY = TruthTransformationPolicyRegistry()

Reason = FactSelectionReasonCode


def compute_target_context_fingerprint(target_context: TargetContext) -> str:
    """Fingerprint the stable identity fields of a ``TargetContext``.

    ``target_context_id`` and ``selection_policy_version`` are excluded: the
    former is an opaque handle, the latter is tracked as its own explicit
    field on the decision and in stale detection.
    """

    content = {
        "person_entity_id": str(target_context.person_entity_id),
        "job_or_application_id": target_context.job_or_application_id,
        "target_role_title": target_context.target_role_title,
        "target_role_family": target_context.target_role_family,
        "target_seniority": target_context.target_seniority,
        "target_organization_or_industry": target_context.target_organization_or_industry,
        "employment_entity_id": (
            str(target_context.employment_entity_id)
            if target_context.employment_entity_id is not None
            else None
        ),
    }
    projection = {
        "fingerprint_version": TARGET_CONTEXT_FINGERPRINT_VERSION,
        "content": content,
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


@dataclass(frozen=True)
class _GateResult:
    """A terminal outcome from a gate, or ``None`` to continue the pipeline."""

    outcome: SelectionDecisionOutcome | None
    reason_codes: frozenset[FactSelectionReasonCode]


def evaluate_eligibility(
    *, status: FactStatus, use_in_cv: bool, requires_approval: bool
) -> _GateResult:
    """Fail-closed eligibility gate. Checked before transferability/permission.

    Hard status blocks (``DRAFT``/``REJECTED``/``ARCHIVED``) take precedence
    over the approval gate, which in turn takes precedence over the
    ``use_in_cv`` exclusion. Any status outside the known enum fails closed
    to ``BLOCKED``.
    """

    if status == FactStatus.DRAFT:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED,
            frozenset({Reason.FACT_STATUS_DRAFT_NOT_ELIGIBLE}),
        )
    if status == FactStatus.REJECTED:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED, frozenset({Reason.FACT_STATUS_REJECTED})
        )
    if status == FactStatus.ARCHIVED:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED, frozenset({Reason.FACT_STATUS_ARCHIVED})
        )
    if status == FactStatus.REVIEW_REQUIRED or requires_approval:
        reasons = set()
        if status == FactStatus.REVIEW_REQUIRED:
            reasons.add(Reason.FACT_STATUS_REVIEW_REQUIRED)
        if requires_approval:
            reasons.add(Reason.FACT_REQUIRES_APPROVAL)
        return _GateResult(SelectionDecisionOutcome.APPROVAL_REQUIRED, frozenset(reasons))
    if status == FactStatus.CONFIRMED and use_in_cv and not requires_approval:
        return _GateResult(None, frozenset())
    if not use_in_cv:
        return _GateResult(
            SelectionDecisionOutcome.EXCLUDED, frozenset({Reason.FACT_USE_IN_CV_DISABLED})
        )
    return _GateResult(
        SelectionDecisionOutcome.BLOCKED, frozenset({Reason.FACT_STATUS_UNKNOWN})
    )


def evaluate_transferability(
    *,
    transferability: Transferability,
    fact_entity_id: object,
    fact_employment_scope_entity_id: object | None,
    target_context: TargetContext,
    requested_operation: TransformationOperation,
) -> _GateResult:
    """Transferability gate. Checked after eligibility, before permission.

    Uses only real ``Transferability`` enum values and only the identity
    fields that already exist on ``TruthFact`` and ``TargetContext`` -- no
    text matching, no entity-graph traversal.
    """

    if (
        transferability == Transferability.EXACT_ONLY
        and requested_operation == TransformationOperation.CONTROLLED_REPHRASE
    ):
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED,
            frozenset({Reason.TRANSFERABILITY_EXACT_ONLY_REPHRASE_BLOCKED}),
        )

    if transferability == Transferability.NON_TRANSFERABLE:
        same_scope = fact_entity_id in {
            target_context.person_entity_id,
            target_context.employment_entity_id,
        }
        if not same_scope:
            return _GateResult(
                SelectionDecisionOutcome.BLOCKED,
                frozenset({Reason.TRANSFERABILITY_NON_TRANSFERABLE_SCOPE_MISMATCH}),
            )
        return _GateResult(None, frozenset())

    if transferability == Transferability.EMPLOYMENT_SCOPED:
        if fact_employment_scope_entity_id is None:
            return _GateResult(
                SelectionDecisionOutcome.BLOCKED,
                frozenset({Reason.TRANSFERABILITY_EMPLOYMENT_SCOPE_MISSING}),
            )
        if fact_employment_scope_entity_id != target_context.employment_entity_id:
            return _GateResult(
                SelectionDecisionOutcome.BLOCKED,
                frozenset({Reason.TRANSFERABILITY_EMPLOYMENT_SCOPE_MISMATCH}),
            )
        return _GateResult(None, frozenset())

    if transferability == Transferability.ENTITY_SCOPED:
        same_scope = fact_entity_id in {
            target_context.person_entity_id,
            target_context.employment_entity_id,
        }
        if not same_scope:
            return _GateResult(
                SelectionDecisionOutcome.APPROVAL_REQUIRED,
                frozenset({Reason.TRANSFERABILITY_ENTITY_SCOPE_UNVERIFIED}),
            )
        return _GateResult(None, frozenset())

    if transferability == Transferability.ROLE_TRANSFERABLE:
        if target_context.target_role_family is None:
            return _GateResult(
                SelectionDecisionOutcome.APPROVAL_REQUIRED,
                frozenset({Reason.TRANSFERABILITY_ROLE_CONTEXT_MISSING}),
            )
        return _GateResult(None, frozenset())

    if transferability == Transferability.INDUSTRY_TRANSFERABLE:
        if target_context.target_organization_or_industry is None:
            return _GateResult(
                SelectionDecisionOutcome.APPROVAL_REQUIRED,
                frozenset({Reason.TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING}),
            )
        return _GateResult(None, frozenset())

    # Transferability.GLOBAL_TRANSFERABLE and Transferability.EXACT_ONLY
    # (outside the CONTROLLED_REPHRASE hard block above) carry no additional
    # context restriction; the permission gate remains authoritative.
    return _GateResult(None, frozenset())


def evaluate_fact_type_policy(
    *,
    fact_type: str,
    requested_operation: TransformationOperation,
    requested_target_scope: str,
) -> _GateResult:
    """Fail-closed gate against the existing P1 ``TruthTransformationPolicyRegistry``.

    Checked after transferability and before any permission is considered.
    This is the sole source of truth for which ``target_scope`` and
    ``requested_operation`` a ``fact_type`` may ever use -- neither the
    base-operation shortcut nor an active ``TruthPermission`` can widen it.

    An unknown ``fact_type`` (no registered policy), a ``target_scope``
    outside ``allowed_target_scopes``, or a ``requested_operation`` outside
    ``allowed_operations`` all fail closed to ``BLOCKED``. ``APPROVAL_REQUIRED``
    is never used here: this is a hard P1 policy denial, not a request for
    human review.
    """

    policy = _FACT_TYPE_POLICY_REGISTRY.get(fact_type)
    if policy is None:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED,
            frozenset({Reason.FACT_TYPE_POLICY_NOT_FOUND}),
        )
    if requested_target_scope not in policy.allowed_target_scopes:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED,
            frozenset({Reason.TARGET_SCOPE_NOT_ALLOWED_BY_POLICY}),
        )
    if requested_operation not in policy.allowed_operations:
        return _GateResult(
            SelectionDecisionOutcome.BLOCKED,
            frozenset({Reason.OPERATION_NOT_ALLOWED_BY_POLICY}),
        )
    return _GateResult(None, frozenset())


def select_applicable_permissions(
    *,
    fact_id: object,
    requested_target_scope: str,
    permissions: Sequence[TruthPermissionRead],
) -> list[TruthPermissionRead]:
    """Deterministically select permissions relevant to this fact + scope.

    Applicability is judged only by ``fact_id`` and ``target_scope`` --
    real fields on ``TruthPermissionRead``. Status, validity window and
    constraints govern whether an applicable permission actually *grants*
    the requested operation, not whether it is applicable.
    """

    applicable = [
        permission
        for permission in permissions
        if permission.fact_id == fact_id and permission.target_scope == requested_target_scope
    ]
    return sorted(applicable, key=lambda permission: str(permission.permission_id))


def compute_permission_snapshot_fingerprint(
    applicable_permissions: Sequence[TruthPermissionRead],
) -> str:
    """Fingerprint the exact set of permissions considered for a decision."""

    content = [
        {
            "permission_id": str(permission.permission_id),
            "fact_id": str(permission.fact_id),
            "target_scope": permission.target_scope,
            "status": permission.status.value,
            "allowed_operations": sorted(
                operation.value for operation in permission.allowed_operations
            ),
            "constraints_json": permission.constraints_json,
            "valid_from": (
                permission.valid_from.isoformat() if permission.valid_from else None
            ),
            "valid_until": (
                permission.valid_until.isoformat() if permission.valid_until else None
            ),
            "revision": permission.revision,
            "content_fingerprint": permission.content_fingerprint,
        }
        for permission in sorted(
            applicable_permissions, key=lambda permission: str(permission.permission_id)
        )
    ]
    projection = {
        "fingerprint_version": "fact-selection-permission-snapshot-v1",
        "content": content,
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _constraints_match(
    constraints_json: dict[str, object], target_context: TargetContext
) -> bool:
    """Validate a permission's ``constraints_json`` against the target context.

    Only keys that name a real ``TargetContext`` field are recognized. An
    unrecognized key, or a recognized key whose value differs, fails the
    match -- fail closed rather than inventing unsupported constraint
    semantics.
    """

    for key, expected in constraints_json.items():
        if not hasattr(target_context, key):
            return False
        actual = getattr(target_context, key)
        actual_value = str(actual) if actual is not None else None
        if actual_value != expected:
            return False
    return True


def evaluate_permission_grant(
    *,
    requested_operation: TransformationOperation,
    requested_target_scope: str,
    permissions_for_fact: Sequence[TruthPermissionRead],
    target_context: TargetContext,
    evaluation_time: datetime,
) -> _GateResult:
    """Decide whether ``requested_operation`` is currently granted.

    Callers must run ``evaluate_fact_type_policy`` first: this function
    assumes the fact_type/target_scope/operation combination has already
    been cleared against P1. ``EXACT_COPY`` and ``FORMAT_NORMALIZATION`` are
    then granted with no permission at all. Every other operation requires
    an ``ACTIVE`` permission whose scope, constraints, ``allowed_operations``
    and validity window all match. No automatic downgrade to a different
    operation is ever performed, and no permission can grant an operation or
    scope P1 has not already allowed for this fact_type.

    ``permissions_for_fact`` must already be filtered to this fact's
    ``fact_id`` (regardless of scope), so a scope-only mismatch can be
    diagnosed distinctly from an outright absence of permission records.
    """

    if requested_operation in BASE_OPERATIONS:
        return _GateResult(None, frozenset({Reason.PERMISSION_NOT_REQUIRED_BASE_OPERATION}))

    applicable = [
        permission
        for permission in permissions_for_fact
        if permission.target_scope == requested_target_scope
    ]
    if permissions_for_fact and not applicable:
        return _GateResult(
            SelectionDecisionOutcome.APPROVAL_REQUIRED,
            frozenset(
                {Reason.PERMISSION_SCOPE_MISMATCH, Reason.PERMISSION_MISSING_FOR_OPERATION}
            ),
        )

    reasons: set[FactSelectionReasonCode] = set()
    for permission in applicable:
        if requested_operation not in permission.allowed_operations:
            continue
        if permission.status == PermissionStatus.REVOKED:
            reasons.add(Reason.PERMISSION_REVOKED)
            continue
        if permission.status == PermissionStatus.ARCHIVED:
            reasons.add(Reason.PERMISSION_ARCHIVED)
            continue
        if permission.status == PermissionStatus.REVIEW_REQUIRED:
            reasons.add(Reason.PERMISSION_REVIEW_REQUIRED)
            continue
        if permission.valid_from is not None and evaluation_time < permission.valid_from:
            reasons.add(Reason.PERMISSION_NOT_YET_VALID)
            continue
        if permission.valid_until is not None and evaluation_time > permission.valid_until:
            reasons.add(Reason.PERMISSION_EXPIRED)
            continue
        if not _constraints_match(permission.constraints_json, target_context):
            reasons.add(Reason.PERMISSION_CONSTRAINTS_MISMATCH)
            continue
        # status == ACTIVE and every other check passed.
        return _GateResult(None, frozenset({Reason.PERMISSION_ACTIVE_GRANT}))

    reasons.add(Reason.PERMISSION_MISSING_FOR_OPERATION)
    return _GateResult(SelectionDecisionOutcome.APPROVAL_REQUIRED, frozenset(reasons))


def evaluate_positioning_signal(
    *, fact_id: object, positioning_signal: CareerPositioningSignal | None
) -> tuple[bool, frozenset[str]]:
    """Advisory-only evaluation of an existing ``CareerPositioningSignal``.

    Returns ``(is_not_relevant, advisory_flags)``. ``is_not_relevant`` is
    only ever true when the signal names this exact ``fact_id`` in its
    ``not_relevant_fact_ids`` -- never through similarity or text matching.
    The advisory flags never change eligibility, transferability or
    permission outcomes; the caller only applies ``is_not_relevant`` when
    the decision would otherwise be ``SELECTED``.
    """

    if positioning_signal is None:
        return False, frozenset()

    flags: set[str] = set()
    if positioning_signal.requires_human_review:
        flags.add("CPE_REQUIRES_HUMAN_REVIEW")
    if positioning_signal.flattening_required:
        flags.add("CPE_FLATTENING_REQUIRED")
    if positioning_signal.transferability_risk not in (None, "NONE"):
        flags.add(f"CPE_TRANSFERABILITY_RISK_{positioning_signal.transferability_risk}")

    is_not_relevant = fact_id in positioning_signal.not_relevant_fact_ids
    return is_not_relevant, frozenset(flags)


def compute_decision_fingerprint(
    *,
    selection_policy_version: str,
    transformation_policy_version: str,
    fact_id: object,
    entity_id: object,
    fact_revision: int,
    fact_content_fingerprint: str,
    target_context_fingerprint: str,
    requested_operation: TransformationOperation,
    requested_target_scope: str,
    permission_snapshot_fingerprint: str,
    evaluation_time: datetime,
) -> str:
    """Compute the stable ``decision_fingerprint`` for a selection decision.

    Covers every input that can affect the decision. The same full input
    always yields the same fingerprint; there is no random component and no
    implicit current time. ``transformation_policy_version`` is included so
    that a P1 ``TruthTransformationPolicyRegistry`` content change (e.g. a
    future widening of ``allowed_operations``/``allowed_target_scopes``)
    invalidates a previously produced fingerprint even if
    ``selection_policy_version`` (P3's own decision-logic version) is
    unchanged -- P3 does not rely on someone remembering to bump both.
    """

    content = {
        "selection_policy_version": selection_policy_version,
        "transformation_policy_version": transformation_policy_version,
        "fact_id": str(fact_id),
        "entity_id": str(entity_id),
        "fact_revision": fact_revision,
        "fact_content_fingerprint": fact_content_fingerprint,
        "target_context_fingerprint": target_context_fingerprint,
        "requested_operation": requested_operation.value,
        "requested_target_scope": requested_target_scope,
        "permission_snapshot_fingerprint": permission_snapshot_fingerprint,
        "evaluation_time": evaluation_time,
    }
    projection = {
        "fingerprint_version": DECISION_FINGERPRINT_VERSION,
        "content": content,
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def select_fact(
    *,
    fact: TruthFactRead,
    target_context: TargetContext,
    requested_operation: TransformationOperation,
    requested_target_scope: str,
    permissions: Sequence[TruthPermissionRead],
    evaluation_time: datetime,
    positioning_signal: CareerPositioningSignal | None = None,
) -> FactSelectionDecision:
    """Produce a deterministic ``FactSelectionDecision`` for one fact.

    Pipeline order: eligibility -> transferability -> P1 fact_type policy
    (``TruthTransformationPolicyRegistry``) -> permission -> Career
    Positioning Engine advisory (``NOT_RELEVANT`` only, and only when the
    decision would otherwise be ``SELECTED``). No step is skipped for a
    fact that is already known-eligible: eligibility always runs first, and
    permission is never consulted before P1 has cleared the requested
    target_scope and operation for this fact_type.
    """

    applicable_permissions = select_applicable_permissions(
        fact_id=fact.fact_id,
        requested_target_scope=requested_target_scope,
        permissions=permissions,
    )
    permissions_for_fact = [
        permission for permission in permissions if permission.fact_id == fact.fact_id
    ]
    permission_ids = tuple(
        permission.permission_id for permission in applicable_permissions
    )
    permission_snapshot_fingerprint = compute_permission_snapshot_fingerprint(
        applicable_permissions
    )
    target_context_fingerprint = compute_target_context_fingerprint(target_context)

    reason_codes: set[FactSelectionReasonCode] = set()
    advisory_flags: set[str] = set()
    decision: SelectionDecisionOutcome

    eligibility = evaluate_eligibility(
        status=fact.status,
        use_in_cv=fact.use_in_cv,
        requires_approval=fact.requires_approval,
    )
    if eligibility.outcome is not None:
        decision = eligibility.outcome
        reason_codes |= eligibility.reason_codes
    else:
        transferability_result = evaluate_transferability(
            transferability=fact.transferability,
            fact_entity_id=fact.entity_id,
            fact_employment_scope_entity_id=fact.employment_scope_entity_id,
            target_context=target_context,
            requested_operation=requested_operation,
        )
        if transferability_result.outcome is not None:
            decision = transferability_result.outcome
            reason_codes |= transferability_result.reason_codes
        else:
            fact_type_policy_result = evaluate_fact_type_policy(
                fact_type=fact.fact_type,
                requested_operation=requested_operation,
                requested_target_scope=requested_target_scope,
            )
            if fact_type_policy_result.outcome is not None:
                decision = fact_type_policy_result.outcome
                reason_codes |= fact_type_policy_result.reason_codes
            else:
                permission_result = evaluate_permission_grant(
                    requested_operation=requested_operation,
                    requested_target_scope=requested_target_scope,
                    permissions_for_fact=permissions_for_fact,
                    target_context=target_context,
                    evaluation_time=evaluation_time,
                )
                if permission_result.outcome is not None:
                    decision = permission_result.outcome
                    reason_codes |= permission_result.reason_codes
                else:
                    reason_codes |= permission_result.reason_codes
                    is_not_relevant, cpe_flags = evaluate_positioning_signal(
                        fact_id=fact.fact_id, positioning_signal=positioning_signal
                    )
                    advisory_flags |= cpe_flags
                    if is_not_relevant:
                        decision = SelectionDecisionOutcome.NOT_RELEVANT
                        reason_codes.add(Reason.CPE_NOT_RELEVANT)
                    else:
                        decision = SelectionDecisionOutcome.SELECTED

    allowed_operation = (
        requested_operation if decision == SelectionDecisionOutcome.SELECTED else None
    )
    requires_user_approval = decision == SelectionDecisionOutcome.APPROVAL_REQUIRED

    transformation_policy_version = _FACT_TYPE_POLICY_REGISTRY.version

    decision_fingerprint = compute_decision_fingerprint(
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=transformation_policy_version,
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        fact_revision=fact.revision,
        fact_content_fingerprint=fact.content_fingerprint,
        target_context_fingerprint=target_context_fingerprint,
        requested_operation=requested_operation,
        requested_target_scope=requested_target_scope,
        permission_snapshot_fingerprint=permission_snapshot_fingerprint,
        evaluation_time=evaluation_time,
    )

    return FactSelectionDecision(
        decision_fingerprint=decision_fingerprint,
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=transformation_policy_version,
        target_context_id=target_context.target_context_id,
        target_context_fingerprint=target_context_fingerprint,
        person_entity_id=target_context.person_entity_id,
        entity_id=fact.entity_id,
        fact_id=fact.fact_id,
        fact_revision=fact.revision,
        fact_content_fingerprint=fact.content_fingerprint,
        decision=decision,
        reason_codes=tuple(reason_codes),
        requested_operation=requested_operation,
        requested_target_scope=requested_target_scope,
        allowed_operation=allowed_operation,
        transferability=fact.transferability,
        employment_scope_entity_id=fact.employment_scope_entity_id,
        permission_ids=permission_ids,
        permission_snapshot_fingerprint=permission_snapshot_fingerprint,
        requires_user_approval=requires_user_approval,
        evaluation_time=evaluation_time,
        advisory_flags=tuple(advisory_flags),
    )
