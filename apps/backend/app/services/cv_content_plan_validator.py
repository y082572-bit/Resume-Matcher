"""Fail-closed structural + freshness validation for a built P4 plan.

``validate_cv_content_plan`` re-derives every fingerprint and every
placement rule from the plan's own stored data plus the original decisions
supplied by the caller (never re-read from a database), and reports every
mismatch as a ``CvContentPlanViolation``. Nothing here mutates the plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from app.schemas.cv_content_plan import (
    CvContentPlan,
    CvContentPlanReplayInput,
    CvContentPlanStatus,
    CvContentPlanValidationResult,
    CvContentPlanViolation,
    CvContentPlanViolationCode,
    CvFreshnessStatus,
    CvSection,
    CvStructuralValidationStatus,
    OmissionReasonCode,
    SectionBudgetStatus,
)
from app.schemas.fact_selection import FactSelectionDecision, SelectionDecisionOutcome
from app.schemas.truth_entity import EntityType
from app.services.cv_content_plan_builder import (
    compute_conflict_fingerprint,
    compute_content_plan_fingerprint,
    compute_experience_entry_fingerprint,
    compute_omission_fingerprint,
    compute_pending_approval_fingerprint,
    compute_planned_fact_use_fingerprint,
    compute_section_plan_fingerprint,
)
from app.services.cv_content_plan_policy import (
    OWNER_ENTITY_TYPE_BY_FACT_TYPE,
    SECTION_ORDER,
    resolve_approval_type,
    resolve_section_mapping,
)
from app.services.cv_content_plan_replay import replay_input_from_plan, stale_fields


#: Violations that describe an internal-consistency defect only informational
#: to plan readers (budget overrun already reflected in ``plan.plan_status``)
#: -- never by themselves flip ``structural_status`` to ``INVALID``.
_NON_FATAL_VIOLATION_CODES = frozenset({CvContentPlanViolationCode.SECTION_BUDGET_EXCEEDED})

_OMISSION_REASONS_BY_OUTCOME: dict[SelectionDecisionOutcome, frozenset[OmissionReasonCode]] = {
    SelectionDecisionOutcome.BLOCKED: frozenset({OmissionReasonCode.DECISION_BLOCKED}),
    SelectionDecisionOutcome.EXCLUDED: frozenset({OmissionReasonCode.DECISION_EXCLUDED}),
    SelectionDecisionOutcome.NOT_RELEVANT: frozenset({OmissionReasonCode.DECISION_NOT_RELEVANT}),
    SelectionDecisionOutcome.SELECTED: frozenset(
        {
            OmissionReasonCode.SECTION_NOT_ALLOWED_FOR_FACT_TYPE,
            OmissionReasonCode.EMPLOYMENT_SCOPE_REQUIRED,
            OmissionReasonCode.BUDGET_NOT_AVAILABLE,
        }
    ),
}


def _all_planned_uses(plan: CvContentPlan) -> list:
    uses = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                uses.extend(entry.header_fact_uses)
                uses.extend(entry.responsibility_fact_uses)
                uses.extend(entry.achievement_fact_uses)
        else:
            uses.extend(section.planned_fact_uses)
    return uses


def validate_cv_content_plan(
    plan: CvContentPlan,
    decisions_by_fingerprint: Mapping[str, FactSelectionDecision],
    *,
    entity_types: Mapping[UUID, EntityType],
    current_replay_input: CvContentPlanReplayInput | None = None,
) -> CvContentPlanValidationResult:
    violations: list[CvContentPlanViolation] = []

    def _violate(
        code: CvContentPlanViolationCode,
        detail: str,
        *,
        decision_fingerprint: str | None = None,
        fact_id=None,
        section: CvSection | None = None,
    ) -> None:
        violations.append(
            CvContentPlanViolation(
                violation_code=code,
                detail=detail,
                decision_fingerprint=decision_fingerprint,
                fact_id=fact_id,
                section=section,
            )
        )

    all_planned_uses = _all_planned_uses(plan)

    planned_decision_fps = [use.fact_selection_decision_fingerprint for use in all_planned_uses]
    pending_decision_fps = [item.fact_selection_decision_fingerprint for item in plan.pending_approvals]
    omitted_decision_fps = [item.decision_fingerprint for item in plan.omitted_facts]
    conflict_decision_fps: list[str] = []
    for conflict in plan.conflicts:
        conflict_decision_fps.extend(conflict.conflicting_decision_fingerprints)

    buckets = {
        "planned": set(planned_decision_fps),
        "pending": set(pending_decision_fps),
        "omitted": set(omitted_decision_fps),
        "conflict": set(conflict_decision_fps),
    }
    if len(planned_decision_fps) != len(buckets["planned"]):
        _violate(
            CvContentPlanViolationCode.SOURCE_PARTITION_OVERLAP,
            "duplicate fact_selection_decision_fingerprint across planned fact uses",
        )

    union_all = set().union(*buckets.values())
    declared = set(plan.source_decision_fingerprints)
    if union_all != declared:
        _violate(
            CvContentPlanViolationCode.SOURCE_PARTITION_INCOMPLETE,
            "union of planned/pending/omitted/conflict decision fingerprints does not equal "
            "source_decision_fingerprints",
        )
    names = list(buckets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            overlap = buckets[names[i]] & buckets[names[j]]
            if overlap:
                _violate(
                    CvContentPlanViolationCode.SOURCE_PARTITION_OVERLAP,
                    f"{names[i]} and {names[j]} share decision fingerprints: {sorted(overlap)}",
                )

    # Stage P4.5a: independently re-check owner EntityType for every decision
    # belonging to source_decision_fingerprints whose fact_type owns a closed
    # non-employment entity type -- across every bucket (planned, pending,
    # omitted, conflict member), never limited to PlannedFactUse or to
    # plan.sections. A decision fingerprint absent from decisions_by_fingerprint
    # is already reported elsewhere (DECISION_NOT_FOUND / CONFLICT_MEMBER_NOT_FOUND)
    # and is skipped here to avoid duplicate reporting.
    for fingerprint in plan.source_decision_fingerprints:
        decision = decisions_by_fingerprint.get(fingerprint)
        if decision is None:
            continue
        expected_owner_type = OWNER_ENTITY_TYPE_BY_FACT_TYPE.get(decision.fact_type)
        if expected_owner_type is None:
            continue
        actual_owner_type = entity_types.get(decision.entity_id)
        if actual_owner_type is None:
            _violate(
                CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISSING,
                "decision's entity_id has no entry in the supplied entity_types mapping",
                decision_fingerprint=fingerprint,
                fact_id=decision.fact_id,
            )
        elif actual_owner_type != expected_owner_type:
            _violate(
                CvContentPlanViolationCode.OWNER_ENTITY_TYPE_MISMATCH,
                f"decision's entity_id resolves to {actual_owner_type.value}, expected "
                f"{expected_owner_type.value} for fact_type {decision.fact_type}",
                decision_fingerprint=fingerprint,
                fact_id=decision.fact_id,
            )

    planned_fact_ids = [use.fact_id for use in all_planned_uses]
    if len(planned_fact_ids) != len(set(planned_fact_ids)):
        _violate(
            CvContentPlanViolationCode.DUPLICATE_FACT_IN_PLANNED_CONTENT,
            "the same fact_id appears more than once across planned content",
        )

    for use in all_planned_uses:
        decision = decisions_by_fingerprint.get(use.fact_selection_decision_fingerprint)
        if decision is None:
            _violate(
                CvContentPlanViolationCode.DECISION_NOT_FOUND,
                "planned fact use references a decision that was not supplied",
                decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
            )
            continue
        if decision.decision != SelectionDecisionOutcome.SELECTED:
            _violate(
                CvContentPlanViolationCode.PLANNED_FACT_USE_DECISION_MISMATCH,
                "planned fact use's source decision outcome is not SELECTED",
                decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
            )
        mismatched_fields = [
            name
            for name, matches in (
                ("person_entity_id", use.person_entity_id == decision.person_entity_id),
                ("entity_id", use.entity_id == decision.entity_id),
                ("fact_id", use.fact_id == decision.fact_id),
                ("fact_type", use.fact_type == decision.fact_type),
                ("fact_revision", use.fact_revision == decision.fact_revision),
                (
                    "fact_content_fingerprint",
                    use.fact_content_fingerprint == decision.fact_content_fingerprint,
                ),
                ("target_scope", use.target_scope == decision.requested_target_scope),
                ("requested_operation", use.requested_operation == decision.requested_operation),
                ("allowed_operation", use.allowed_operation == decision.allowed_operation),
                ("transferability", use.transferability == decision.transferability),
                (
                    "employment_scope_entity_id",
                    use.employment_scope_entity_id == decision.employment_scope_entity_id,
                ),
                (
                    "permission_ids",
                    tuple(sorted(use.permission_ids, key=str))
                    == tuple(sorted(decision.permission_ids, key=str)),
                ),
                (
                    "permission_snapshot_fingerprint",
                    use.permission_snapshot_fingerprint == decision.permission_snapshot_fingerprint,
                ),
                (
                    "transformation_policy_version",
                    use.transformation_policy_version == decision.transformation_policy_version,
                ),
                (
                    "selection_policy_version",
                    use.selection_policy_version == decision.selection_policy_version,
                ),
                ("reason_codes", tuple(use.reason_codes) == tuple(decision.reason_codes)),
                (
                    "advisory_flags",
                    tuple(sorted(use.advisory_flags)) == tuple(sorted(decision.advisory_flags)),
                ),
            )
            if not matches
        ]
        if mismatched_fields:
            _violate(
                CvContentPlanViolationCode.PLANNED_FACT_USE_DECISION_MISMATCH,
                f"fields differ from source decision: {', '.join(mismatched_fields)}",
                decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
            )

        mapping = resolve_section_mapping(use.fact_type, use.target_scope)
        if mapping is None or mapping[0] != use.target_section:
            _violate(
                CvContentPlanViolationCode.SECTION_NOT_ALLOWED_FOR_FACT_TYPE,
                "planned fact use's target_section does not match the closed policy mapping",
                decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                section=use.target_section,
            )

        expected_use_fp = compute_planned_fact_use_fingerprint(
            person_entity_id=use.person_entity_id,
            entity_id=use.entity_id,
            fact_id=use.fact_id,
            fact_type=use.fact_type,
            fact_revision=use.fact_revision,
            fact_content_fingerprint=use.fact_content_fingerprint,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            target_section=use.target_section,
            target_scope=use.target_scope,
            requested_operation=use.requested_operation,
            allowed_operation=use.allowed_operation,
            placement_order=use.placement_order,
            employment_scope_entity_id=use.employment_scope_entity_id,
            transferability=use.transferability,
            permission_ids=use.permission_ids,
            permission_snapshot_fingerprint=use.permission_snapshot_fingerprint,
            transformation_policy_version=use.transformation_policy_version,
            selection_policy_version=use.selection_policy_version,
            reason_codes=use.reason_codes,
            advisory_flags=use.advisory_flags,
        )
        if expected_use_fp != use.planned_fact_use_fingerprint:
            _violate(
                CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                "planned_fact_use_fingerprint does not match recomputed value",
                decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
            )

    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                for use in entry.header_fact_uses:
                    decision = decisions_by_fingerprint.get(use.fact_selection_decision_fingerprint)
                    if decision is not None and decision.entity_id != entry.employment_entity_id:
                        _violate(
                            CvContentPlanViolationCode.EMPLOYMENT_GROUPING_MISMATCH,
                            "header fact use's decision.entity_id does not match its entry's "
                            "employment_entity_id",
                            decision_fingerprint=use.fact_selection_decision_fingerprint,
                            fact_id=use.fact_id,
                        )
                for use in entry.responsibility_fact_uses + entry.achievement_fact_uses:
                    decision = decisions_by_fingerprint.get(use.fact_selection_decision_fingerprint)
                    if decision is not None and decision.employment_scope_entity_id != entry.employment_entity_id:
                        _violate(
                            CvContentPlanViolationCode.EMPLOYMENT_GROUPING_MISMATCH,
                            "responsibility/achievement fact use's decision.employment_scope_entity_id "
                            "does not match its entry's employment_entity_id",
                            decision_fingerprint=use.fact_selection_decision_fingerprint,
                            fact_id=use.fact_id,
                        )
                for bucket_uses in (
                    entry.header_fact_uses,
                    entry.responsibility_fact_uses,
                    entry.achievement_fact_uses,
                ):
                    orders = [use.placement_order for use in bucket_uses]
                    if orders != list(range(len(orders))):
                        _violate(
                            CvContentPlanViolationCode.PLACEMENT_ORDER_INVALID,
                            "placement_order must be exactly 0..n-1 in list order",
                            section=CvSection.EXPERIENCE,
                        )
                expected_entry_fp = compute_experience_entry_fingerprint(
                    employment_entity_id=entry.employment_entity_id,
                    entry_order=entry.entry_order,
                    entry_order_source=entry.entry_order_source,
                    header_fact_uses=entry.header_fact_uses,
                    responsibility_fact_uses=entry.responsibility_fact_uses,
                    achievement_fact_uses=entry.achievement_fact_uses,
                )
                if expected_entry_fp != entry.experience_entry_fingerprint:
                    _violate(
                        CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                        "experience_entry_fingerprint does not match recomputed value",
                        section=CvSection.EXPERIENCE,
                    )

            entry_orders = [entry.entry_order for entry in section.experience_entries]
            if entry_orders != list(range(len(entry_orders))):
                _violate(
                    CvContentPlanViolationCode.PLACEMENT_ORDER_INVALID,
                    "experience entry_order must be exactly 0..n-1",
                    section=CvSection.EXPERIENCE,
                )

            expected_section_fp = compute_section_plan_fingerprint(
                kind="EXPERIENCE",
                section=CvSection.EXPERIENCE,
                skipped_empty=section.skipped_empty,
                budget_status=section.budget_status,
                member_fingerprints=[
                    entry.experience_entry_fingerprint for entry in section.experience_entries
                ],
            )
            if expected_section_fp != section.section_plan_fingerprint:
                _violate(
                    CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                    "EXPERIENCE section_plan_fingerprint does not match recomputed value",
                    section=CvSection.EXPERIENCE,
                )
            if section.budget_status == SectionBudgetStatus.EXCEEDS_BUDGET:
                _violate(
                    CvContentPlanViolationCode.SECTION_BUDGET_EXCEEDED,
                    "EXPERIENCE section exceeds its configured budget",
                    section=CvSection.EXPERIENCE,
                )
        else:
            orders = [use.placement_order for use in section.planned_fact_uses]
            if orders != list(range(len(orders))):
                _violate(
                    CvContentPlanViolationCode.PLACEMENT_ORDER_INVALID,
                    "placement_order must be exactly 0..n-1 in list order",
                    section=section.section,
                )
            if section.section == CvSection.PROFILE and len(section.planned_fact_uses) > 1:
                _violate(
                    CvContentPlanViolationCode.PROFILE_MULTIPLE_FACTS,
                    "PROFILE must contain at most one PlannedFactUse",
                    section=CvSection.PROFILE,
                )
            expected_section_fp = compute_section_plan_fingerprint(
                kind="FLAT",
                section=section.section,
                skipped_empty=section.skipped_empty,
                budget_status=section.budget_status,
                member_fingerprints=[
                    use.planned_fact_use_fingerprint for use in section.planned_fact_uses
                ],
            )
            if expected_section_fp != section.section_plan_fingerprint:
                _violate(
                    CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                    f"{section.section.value} section_plan_fingerprint does not match recomputed value",
                    section=section.section,
                )
            if section.budget_status == SectionBudgetStatus.EXCEEDS_BUDGET:
                _violate(
                    CvContentPlanViolationCode.SECTION_BUDGET_EXCEEDED,
                    f"{section.section.value} section exceeds its configured budget",
                    section=section.section,
                )

    actual_section_order = tuple(section.section for section in plan.sections)
    if actual_section_order != SECTION_ORDER:
        _violate(
            CvContentPlanViolationCode.SECTION_ORDER_MISMATCH,
            "plan.sections order does not match the versioned SECTION_ORDER",
        )

    for pending in plan.pending_approvals:
        decision = decisions_by_fingerprint.get(pending.fact_selection_decision_fingerprint)
        if decision is None:
            _violate(
                CvContentPlanViolationCode.DECISION_NOT_FOUND,
                "pending approval references a decision that was not supplied",
                decision_fingerprint=pending.fact_selection_decision_fingerprint,
                fact_id=pending.fact_id,
            )
            continue
        if decision.decision != SelectionDecisionOutcome.APPROVAL_REQUIRED:
            _violate(
                CvContentPlanViolationCode.PENDING_APPROVAL_DECISION_MISMATCH,
                "pending approval's source decision outcome is not APPROVAL_REQUIRED",
                decision_fingerprint=pending.fact_selection_decision_fingerprint,
                fact_id=pending.fact_id,
            )
        expected_approval_type = resolve_approval_type(decision.reason_codes)
        if expected_approval_type != pending.approval_type:
            _violate(
                CvContentPlanViolationCode.PENDING_APPROVAL_DECISION_MISMATCH,
                "approval_type does not match the reason_codes classification",
                decision_fingerprint=pending.fact_selection_decision_fingerprint,
                fact_id=pending.fact_id,
            )
        expected_pending_fp = compute_pending_approval_fingerprint(
            person_entity_id=pending.person_entity_id,
            entity_id=pending.entity_id,
            fact_id=pending.fact_id,
            fact_type=pending.fact_type,
            fact_selection_decision_fingerprint=pending.fact_selection_decision_fingerprint,
            requested_operation=pending.requested_operation,
            target_section=pending.target_section,
            target_scope=pending.target_scope,
            reason_codes=pending.reason_codes,
            approval_type=pending.approval_type,
        )
        if expected_pending_fp != pending.pending_approval_fingerprint:
            _violate(
                CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                "pending_approval_fingerprint does not match recomputed value",
                decision_fingerprint=pending.fact_selection_decision_fingerprint,
                fact_id=pending.fact_id,
            )

    for omitted in plan.omitted_facts:
        decision = decisions_by_fingerprint.get(omitted.decision_fingerprint)
        if decision is None:
            _violate(
                CvContentPlanViolationCode.DECISION_NOT_FOUND,
                "omitted fact references a decision that was not supplied",
                decision_fingerprint=omitted.decision_fingerprint,
                fact_id=omitted.fact_id,
            )
            continue
        if decision.decision != omitted.original_outcome:
            _violate(
                CvContentPlanViolationCode.OMITTED_FACT_DECISION_MISMATCH,
                "original_outcome does not match the source decision's outcome",
                decision_fingerprint=omitted.decision_fingerprint,
                fact_id=omitted.fact_id,
            )
        allowed_reasons = _OMISSION_REASONS_BY_OUTCOME.get(omitted.original_outcome, frozenset())
        if omitted.omission_reason_code not in allowed_reasons:
            _violate(
                CvContentPlanViolationCode.OMITTED_FACT_DECISION_MISMATCH,
                "omission_reason_code is inconsistent with original_outcome",
                decision_fingerprint=omitted.decision_fingerprint,
                fact_id=omitted.fact_id,
            )
        expected_omission_fp = compute_omission_fingerprint(
            decision_fingerprint=omitted.decision_fingerprint,
            person_entity_id=omitted.person_entity_id,
            entity_id=omitted.entity_id,
            fact_id=omitted.fact_id,
            fact_type=omitted.fact_type,
            original_outcome=omitted.original_outcome,
            omission_reason_code=omitted.omission_reason_code,
            target_scope=omitted.target_scope,
            requested_operation=omitted.requested_operation,
        )
        if expected_omission_fp != omitted.omission_fingerprint:
            _violate(
                CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                "omission_fingerprint does not match recomputed value",
                decision_fingerprint=omitted.decision_fingerprint,
                fact_id=omitted.fact_id,
            )

    for conflict in plan.conflicts:
        for fingerprint in conflict.conflicting_decision_fingerprints:
            if fingerprint not in decisions_by_fingerprint:
                _violate(
                    CvContentPlanViolationCode.CONFLICT_MEMBER_NOT_FOUND,
                    "conflict references a decision fingerprint that was not supplied",
                    decision_fingerprint=fingerprint,
                )
        expected_conflict_fp = compute_conflict_fingerprint(
            person_entity_id=conflict.person_entity_id,
            fact_id=conflict.fact_id,
            entity_id=conflict.entity_id,
            conflicting_decision_fingerprints=conflict.conflicting_decision_fingerprints,
            conflict_reason_code=conflict.conflict_reason_code,
        )
        if expected_conflict_fp != conflict.conflict_fingerprint:
            _violate(
                CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
                "conflict_fingerprint does not match recomputed value",
            )

    expected_content_plan_fp = compute_content_plan_fingerprint(
        schema_version=plan.schema_version,
        target_context_fingerprint=plan.target_context_fingerprint,
        job_or_application_id=plan.job_or_application_id,
        content_policy_version=plan.content_policy_version,
        selection_policy_version=plan.selection_policy_version,
        transformation_policy_version=plan.transformation_policy_version,
        budget_profile_fingerprint=plan.budget_profile_fingerprint,
        source_decision_fingerprints=plan.source_decision_fingerprints,
        section_plan_fingerprints=[section.section_plan_fingerprint for section in plan.sections],
        pending_approval_fingerprints=[
            item.pending_approval_fingerprint for item in plan.pending_approvals
        ],
        omission_fingerprints=[item.omission_fingerprint for item in plan.omitted_facts],
        conflict_fingerprints=[item.conflict_fingerprint for item in plan.conflicts],
        career_positioning_snapshot_fingerprint=plan.career_positioning_snapshot_fingerprint,
    )
    if expected_content_plan_fp != plan.content_plan_fingerprint:
        _violate(
            CvContentPlanViolationCode.FINGERPRINT_MISMATCH,
            "content_plan_fingerprint does not match recomputed value",
        )

    has_conflicts = len(plan.conflicts) > 0
    has_budget_exceed = any(
        section.budget_status == SectionBudgetStatus.EXCEEDS_BUDGET for section in plan.sections
    )
    if has_conflicts and plan.plan_status != CvContentPlanStatus.INVALID:
        _violate(
            CvContentPlanViolationCode.PLAN_STATUS_INCONSISTENT,
            "plan has conflicts but plan_status is not INVALID",
        )
    elif (
        not has_conflicts
        and has_budget_exceed
        and plan.plan_status == CvContentPlanStatus.VALID
    ):
        _violate(
            CvContentPlanViolationCode.PLAN_STATUS_INCONSISTENT,
            "a section exceeds its budget but plan_status is VALID",
        )

    structural_status = (
        CvStructuralValidationStatus.INVALID
        if any(v.violation_code not in _NON_FATAL_VIOLATION_CODES for v in violations)
        else CvStructuralValidationStatus.VALID
    )

    if current_replay_input is None:
        freshness_status = CvFreshnessStatus.FRESHNESS_NOT_VERIFIED
        stale_field_names: tuple[str, ...] = ()
    else:
        expected_replay_input = replay_input_from_plan(plan)
        if expected_replay_input == current_replay_input:
            freshness_status = CvFreshnessStatus.FRESH
            stale_field_names = ()
        else:
            freshness_status = CvFreshnessStatus.STALE
            stale_field_names = stale_fields(expected_replay_input, current_replay_input)

    p5_ready = (
        plan.plan_status == CvContentPlanStatus.VALID
        and structural_status == CvStructuralValidationStatus.VALID
        and freshness_status == CvFreshnessStatus.FRESH
        and len(plan.conflicts) == 0
    )

    return CvContentPlanValidationResult(
        structural_status=structural_status,
        freshness_status=freshness_status,
        p5_ready=p5_ready,
        violations=tuple(violations),
        stale_fields=stale_field_names,
    )
