"""Deterministic P4 CV content plan construction.

``build_cv_content_plan`` is a pure, in-memory transformation of an
existing sequence of P3 ``FactSelectionDecision`` records into a
``CvContentPlanBuildResult``. It performs no database I/O, calls
``select_fact`` for nothing, creates no new fact or entity, never changes a
P3 decision outcome, and generates no CV text.

Every fingerprint below is ``hashlib.sha256`` over
``truth_fingerprint.canonical_json_bytes``, lowercase hex, with no
``uuid4()``, no ``datetime.now()``, no ``source_reference`` and no list
index folded in.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.schemas.cv_content_plan import (
    ApprovalType,
    CvContentBudgetProfile,
    CvContentPlan,
    CvContentPlanBuildOutcome,
    CvContentPlanBuildResult,
    CvContentPlanConflict,
    CvContentPlanStatus,
    CvExperienceEntryPlan,
    CvExperienceSectionPlan,
    CvFlatSectionPlan,
    CvSection,
    CvSectionPlan,
    ConflictReasonCode,
    EntryOrderSource,
    OmissionReasonCode,
    OmittedFactDecision,
    PendingFactApproval,
    PlannedFactUse,
    SectionBudgetLimit,
    SectionBudgetStatus,
)
from app.schemas.fact_selection import FactSelectionDecision, SelectionDecisionOutcome, TargetContext
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_plan_policy import (
    CV_CONTENT_POLICY_VERSION,
    FACT_TYPE_PRIORITY,
    SECTION_ORDER,
    requires_employment_entity_grouping,
    requires_employment_scope_grouping,
    resolve_approval_type,
    resolve_section_mapping,
)
from app.services.fact_selection_policy import compute_target_context_fingerprint
from app.services.truth_fingerprint import canonical_json_bytes


CV_CONTENT_PLAN_SCHEMA_VERSION = "cv-content-plan-schema-v1"

PLANNED_FACT_USE_FINGERPRINT_VERSION = "cv-content-plan-planned-fact-use-v1"
EXPERIENCE_ENTRY_FINGERPRINT_VERSION = "cv-content-plan-experience-entry-v1"
SECTION_PLAN_FINGERPRINT_VERSION = "cv-content-plan-section-plan-v1"
PENDING_APPROVAL_FINGERPRINT_VERSION = "cv-content-plan-pending-approval-v1"
OMISSION_FINGERPRINT_VERSION = "cv-content-plan-omission-v1"
CONFLICT_FINGERPRINT_VERSION = "cv-content-plan-conflict-v1"
CONTENT_PLAN_FINGERPRINT_VERSION = "cv-content-plan-content-plan-v1"
BUDGET_PROFILE_FINGERPRINT_VERSION = "cv-content-plan-budget-profile-v1"

#: Homogeneity-relevant fields every deduplicated input decision must share.
_SNAPSHOT_HOMOGENEITY_FIELDS: tuple[str, ...] = (
    "person_entity_id",
    "target_context_id",
    "target_context_fingerprint",
    "selection_policy_version",
    "transformation_policy_version",
    "evaluation_time",
)


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_planned_fact_use_fingerprint(
    *,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    fact_revision: int,
    fact_content_fingerprint: str,
    fact_selection_decision_fingerprint: str,
    target_section: CvSection,
    target_scope: str,
    requested_operation: TransformationOperation,
    allowed_operation: TransformationOperation,
    placement_order: int,
    employment_scope_entity_id: UUID | None,
    transferability: Transferability,
    permission_ids: Sequence[UUID],
    permission_snapshot_fingerprint: str,
    transformation_policy_version: str,
    selection_policy_version: str,
    reason_codes: Sequence[object],
    advisory_flags: Sequence[str],
) -> str:
    content = {
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "fact_revision": fact_revision,
        "fact_content_fingerprint": fact_content_fingerprint,
        "fact_selection_decision_fingerprint": fact_selection_decision_fingerprint,
        "target_section": target_section.value,
        "target_scope": target_scope,
        "requested_operation": requested_operation.value,
        "allowed_operation": allowed_operation.value,
        "placement_order": placement_order,
        "employment_scope_entity_id": (
            str(employment_scope_entity_id) if employment_scope_entity_id is not None else None
        ),
        "transferability": transferability.value,
        "permission_ids": sorted(str(item) for item in permission_ids),
        "permission_snapshot_fingerprint": permission_snapshot_fingerprint,
        "transformation_policy_version": transformation_policy_version,
        "selection_policy_version": selection_policy_version,
        "reason_codes": sorted(str(getattr(code, "value", code)) for code in reason_codes),
        "advisory_flags": sorted(advisory_flags),
    }
    return _fingerprint(PLANNED_FACT_USE_FINGERPRINT_VERSION, content)


def compute_experience_entry_fingerprint(
    *,
    employment_entity_id: UUID,
    entry_order: int,
    entry_order_source: EntryOrderSource,
    header_fact_uses: Sequence[PlannedFactUse],
    responsibility_fact_uses: Sequence[PlannedFactUse],
    achievement_fact_uses: Sequence[PlannedFactUse],
) -> str:
    content = {
        "employment_entity_id": str(employment_entity_id),
        "entry_order": entry_order,
        "entry_order_source": entry_order_source.value,
        "header_fact_uses": [use.planned_fact_use_fingerprint for use in header_fact_uses],
        "responsibility_fact_uses": [
            use.planned_fact_use_fingerprint for use in responsibility_fact_uses
        ],
        "achievement_fact_uses": [
            use.planned_fact_use_fingerprint for use in achievement_fact_uses
        ],
    }
    return _fingerprint(EXPERIENCE_ENTRY_FINGERPRINT_VERSION, content)


def compute_section_plan_fingerprint(
    *,
    kind: str,
    section: CvSection,
    skipped_empty: bool,
    budget_status: SectionBudgetStatus,
    member_fingerprints: Sequence[str],
) -> str:
    content = {
        "kind": kind,
        "section": section.value,
        "skipped_empty": skipped_empty,
        "budget_status": budget_status.value,
        "member_fingerprints": list(member_fingerprints),
    }
    return _fingerprint(SECTION_PLAN_FINGERPRINT_VERSION, content)


def compute_pending_approval_fingerprint(
    *,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    fact_selection_decision_fingerprint: str,
    requested_operation: TransformationOperation,
    target_section: CvSection | None,
    target_scope: str,
    reason_codes: Sequence[object],
    approval_type: ApprovalType,
) -> str:
    content = {
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "fact_selection_decision_fingerprint": fact_selection_decision_fingerprint,
        "requested_operation": requested_operation.value,
        "target_section": target_section.value if target_section is not None else None,
        "target_scope": target_scope,
        "reason_codes": sorted(str(getattr(code, "value", code)) for code in reason_codes),
        "approval_type": approval_type.value,
    }
    return _fingerprint(PENDING_APPROVAL_FINGERPRINT_VERSION, content)


def compute_omission_fingerprint(
    *,
    decision_fingerprint: str,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    original_outcome: SelectionDecisionOutcome,
    omission_reason_code: OmissionReasonCode,
    target_scope: str,
    requested_operation: TransformationOperation,
) -> str:
    content = {
        "decision_fingerprint": decision_fingerprint,
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "original_outcome": original_outcome.value,
        "omission_reason_code": omission_reason_code.value,
        "target_scope": target_scope,
        "requested_operation": requested_operation.value,
    }
    return _fingerprint(OMISSION_FINGERPRINT_VERSION, content)


def compute_conflict_fingerprint(
    *,
    person_entity_id: UUID,
    fact_id: UUID | None,
    entity_id: UUID | None,
    conflicting_decision_fingerprints: Sequence[str],
    conflict_reason_code: ConflictReasonCode,
) -> str:
    content = {
        "person_entity_id": str(person_entity_id),
        "fact_id": str(fact_id) if fact_id is not None else None,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "conflicting_decision_fingerprints": sorted(conflicting_decision_fingerprints),
        "conflict_reason_code": conflict_reason_code.value,
    }
    return _fingerprint(CONFLICT_FINGERPRINT_VERSION, content)


def compute_budget_profile_fingerprint(
    *, budget_profile_id: str, section_limits: Sequence[SectionBudgetLimit]
) -> str:
    content = {
        "budget_profile_id": budget_profile_id,
        "section_limits": sorted(
            (
                {"section": limit.section.value, "max_facts": limit.max_facts}
                for limit in section_limits
            ),
            key=lambda item: item["section"],
        ),
    }
    return _fingerprint(BUDGET_PROFILE_FINGERPRINT_VERSION, content)


def compute_content_plan_fingerprint(
    *,
    schema_version: str,
    target_context_fingerprint: str,
    job_or_application_id: str,
    content_policy_version: str,
    selection_policy_version: str,
    transformation_policy_version: str,
    budget_profile_fingerprint: str | None,
    source_decision_fingerprints: Sequence[str],
    section_plan_fingerprints: Sequence[str],
    pending_approval_fingerprints: Sequence[str],
    omission_fingerprints: Sequence[str],
    conflict_fingerprints: Sequence[str],
    career_positioning_snapshot_fingerprint: str | None,
) -> str:
    content = {
        "schema_version": schema_version,
        "target_context_fingerprint": target_context_fingerprint,
        "job_or_application_id": job_or_application_id,
        "content_policy_version": content_policy_version,
        "selection_policy_version": selection_policy_version,
        "transformation_policy_version": transformation_policy_version,
        "budget_profile_fingerprint": budget_profile_fingerprint,
        "source_decision_fingerprints": sorted(source_decision_fingerprints),
        "section_plan_fingerprints": sorted(section_plan_fingerprints),
        "pending_approval_fingerprints": sorted(pending_approval_fingerprints),
        "omission_fingerprints": sorted(omission_fingerprints),
        "conflict_fingerprints": sorted(conflict_fingerprints),
        "career_positioning_snapshot_fingerprint": career_positioning_snapshot_fingerprint,
    }
    return _fingerprint(CONTENT_PLAN_FINGERPRINT_VERSION, content)


def _sort_key(decision: FactSelectionDecision) -> tuple:
    return (
        FACT_TYPE_PRIORITY[decision.fact_type],
        str(decision.entity_id),
        str(decision.fact_id),
        decision.decision_fingerprint,
    )


def _build_planned_fact_use(
    decision: FactSelectionDecision, *, target_section: CvSection, placement_order: int
) -> PlannedFactUse:
    assert decision.allowed_operation is not None
    fingerprint = compute_planned_fact_use_fingerprint(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        fact_selection_decision_fingerprint=decision.decision_fingerprint,
        target_section=target_section,
        target_scope=decision.requested_target_scope,
        requested_operation=decision.requested_operation,
        allowed_operation=decision.allowed_operation,
        placement_order=placement_order,
        employment_scope_entity_id=decision.employment_scope_entity_id,
        transferability=decision.transferability,
        permission_ids=decision.permission_ids,
        permission_snapshot_fingerprint=decision.permission_snapshot_fingerprint,
        transformation_policy_version=decision.transformation_policy_version,
        selection_policy_version=decision.selection_policy_version,
        reason_codes=decision.reason_codes,
        advisory_flags=decision.advisory_flags,
    )
    return PlannedFactUse(
        planned_fact_use_fingerprint=fingerprint,
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        fact_selection_decision_fingerprint=decision.decision_fingerprint,
        target_section=target_section,
        target_scope=decision.requested_target_scope,
        requested_operation=decision.requested_operation,
        allowed_operation=decision.allowed_operation,
        placement_order=placement_order,
        employment_scope_entity_id=decision.employment_scope_entity_id,
        transferability=decision.transferability,
        permission_ids=decision.permission_ids,
        permission_snapshot_fingerprint=decision.permission_snapshot_fingerprint,
        transformation_policy_version=decision.transformation_policy_version,
        selection_policy_version=decision.selection_policy_version,
        reason_codes=decision.reason_codes,
        advisory_flags=decision.advisory_flags,
    )


def _build_uses(
    candidates: Sequence[FactSelectionDecision], *, target_section: CvSection
) -> tuple[PlannedFactUse, ...]:
    ordered = sorted(candidates, key=_sort_key)
    return tuple(
        _build_planned_fact_use(decision, target_section=target_section, placement_order=index)
        for index, decision in enumerate(ordered)
    )


def _evaluate_budget_status(count: int, max_facts: int | None) -> SectionBudgetStatus:
    if max_facts is None:
        return SectionBudgetStatus.NOT_EVALUATED
    if count <= max_facts:
        return SectionBudgetStatus.WITHIN_BUDGET
    return SectionBudgetStatus.EXCEEDS_BUDGET


def build_cv_content_plan(
    *,
    target_context: TargetContext,
    decisions: Sequence[FactSelectionDecision],
    entity_types: Mapping[UUID, EntityType],
    employment_entry_order: Mapping[UUID, int] | None = None,
    budget_profile: CvContentBudgetProfile | None = None,
    career_positioning_snapshot_fingerprint: str | None = None,
) -> CvContentPlanBuildResult:
    """Build a deterministic ``CvContentPlan`` from existing P3 decisions.

    Pure and in-memory: no database read, no ``select_fact`` call, no new
    fact or entity, no CV text, no LLM call. ``decisions`` must be
    non-empty -- an empty sequence is a caller precondition error, not a
    fail-closed data-consistency case, since P4 has no other source for
    ``transformation_policy_version``.
    """

    if not decisions:
        raise ValueError("build_cv_content_plan requires at least one decision")

    unique_decisions: dict[str, FactSelectionDecision] = {}
    for decision in decisions:
        unique_decisions.setdefault(decision.decision_fingerprint, decision)
    decisions_list = list(unique_decisions.values())

    violations: set[str] = set()
    for field_name in _SNAPSHOT_HOMOGENEITY_FIELDS:
        values = {getattr(decision, field_name) for decision in decisions_list}
        if len(values) > 1:
            violations.add(field_name)

    common_person_entity_id = decisions_list[0].person_entity_id
    common_target_context_id = decisions_list[0].target_context_id
    common_target_context_fingerprint = decisions_list[0].target_context_fingerprint
    common_selection_policy_version = decisions_list[0].selection_policy_version
    common_transformation_policy_version = decisions_list[0].transformation_policy_version

    if "person_entity_id" not in violations and common_person_entity_id != target_context.person_entity_id:
        violations.add("person_entity_id")
    if (
        "target_context_id" not in violations
        and common_target_context_id != target_context.target_context_id
    ):
        violations.add("target_context_id")

    expected_target_context_fingerprint = compute_target_context_fingerprint(target_context)
    if (
        "target_context_fingerprint" not in violations
        and common_target_context_fingerprint != expected_target_context_fingerprint
    ):
        violations.add("target_context_fingerprint")

    if not target_context.job_or_application_id:
        violations.add("job_or_application_id")

    required_entity_ids = {decision.entity_id for decision in decisions_list}
    required_employment_scope_ids = {
        decision.employment_scope_entity_id
        for decision in decisions_list
        if decision.employment_scope_entity_id is not None
        and requires_employment_scope_grouping(decision.fact_type, decision.requested_target_scope)
    }
    required_entity_type_keys = required_entity_ids | required_employment_scope_ids
    if required_entity_type_keys - set(entity_types.keys()):
        violations.add("entity_types")

    if violations:
        return CvContentPlanBuildResult(
            outcome=CvContentPlanBuildOutcome.INVALID_INPUT_SNAPSHOT,
            plan=None,
            snapshot_violations=tuple(sorted(violations)),
        )

    # -- Step 1: same-fact_id, multiple-SELECTED-decisions conflicts. --
    conflicts: list[CvContentPlanConflict] = []
    conflict_decision_fingerprints: set[str] = set()

    selected = [
        decision
        for decision in decisions_list
        if decision.decision == SelectionDecisionOutcome.SELECTED
    ]
    by_fact_id: dict[UUID, list[FactSelectionDecision]] = defaultdict(list)
    for decision in selected:
        by_fact_id[decision.fact_id].append(decision)

    for fact_id in sorted((fid for fid, ds in by_fact_id.items() if len(ds) > 1), key=str):
        group = by_fact_id[fact_id]
        fingerprints = tuple(sorted(decision.decision_fingerprint for decision in group))
        entity_ids = {decision.entity_id for decision in group}
        entity_id_value = entity_ids.pop() if len(entity_ids) == 1 else None
        fingerprint = compute_conflict_fingerprint(
            person_entity_id=group[0].person_entity_id,
            fact_id=fact_id,
            entity_id=entity_id_value,
            conflicting_decision_fingerprints=fingerprints,
            conflict_reason_code=ConflictReasonCode.CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT,
        )
        conflicts.append(
            CvContentPlanConflict(
                conflict_fingerprint=fingerprint,
                person_entity_id=group[0].person_entity_id,
                fact_id=fact_id,
                entity_id=entity_id_value,
                conflicting_decision_fingerprints=fingerprints,
                conflict_reason_code=ConflictReasonCode.CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT,
            )
        )
        conflict_decision_fingerprints.update(fingerprints)

    selected = [
        decision
        for decision in selected
        if decision.decision_fingerprint not in conflict_decision_fingerprints
    ]

    # -- Step 2: non-SELECTED outcomes. --
    pending_approvals_data: list[tuple[FactSelectionDecision, CvSection | None]] = []
    omitted_data: list[tuple[FactSelectionDecision, OmissionReasonCode]] = []

    for decision in decisions_list:
        if decision.decision_fingerprint in conflict_decision_fingerprints:
            continue
        if decision.decision == SelectionDecisionOutcome.SELECTED:
            continue
        if decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED:
            mapping = resolve_section_mapping(decision.fact_type, decision.requested_target_scope)
            target_section = mapping[0] if mapping is not None else None
            pending_approvals_data.append((decision, target_section))
        elif decision.decision == SelectionDecisionOutcome.BLOCKED:
            omitted_data.append((decision, OmissionReasonCode.DECISION_BLOCKED))
        elif decision.decision == SelectionDecisionOutcome.EXCLUDED:
            omitted_data.append((decision, OmissionReasonCode.DECISION_EXCLUDED))
        elif decision.decision == SelectionDecisionOutcome.NOT_RELEVANT:
            omitted_data.append((decision, OmissionReasonCode.DECISION_NOT_RELEVANT))

    # -- Step 3: SELECTED placement into header/responsibility/achievement/flat. --
    header_by_employment: dict[UUID, list[FactSelectionDecision]] = defaultdict(list)
    responsibility_by_employment: dict[UUID, list[FactSelectionDecision]] = defaultdict(list)
    achievement_by_employment: dict[UUID, list[FactSelectionDecision]] = defaultdict(list)
    flat_by_section: dict[CvSection, list[FactSelectionDecision]] = {
        section: [] for section in CvSection if section != CvSection.EXPERIENCE
    }

    for decision in selected:
        mapping = resolve_section_mapping(decision.fact_type, decision.requested_target_scope)
        if mapping is None:
            omitted_data.append((decision, OmissionReasonCode.SECTION_NOT_ALLOWED_FOR_FACT_TYPE))
            continue
        section, bucket = mapping
        if bucket == "header":
            if entity_types.get(decision.entity_id) != EntityType.EMPLOYMENT:
                omitted_data.append((decision, OmissionReasonCode.EMPLOYMENT_SCOPE_REQUIRED))
                continue
            header_by_employment[decision.entity_id].append(decision)
        elif bucket in ("responsibility", "achievement"):
            scope_id = decision.employment_scope_entity_id
            if scope_id is None or entity_types.get(scope_id) != EntityType.EMPLOYMENT:
                omitted_data.append((decision, OmissionReasonCode.EMPLOYMENT_SCOPE_REQUIRED))
                continue
            target = responsibility_by_employment if bucket == "responsibility" else achievement_by_employment
            target[scope_id].append(decision)
        else:
            flat_by_section[section].append(decision)

    # -- Step 4: PROFILE multi-summary-candidate conflict resolution. --
    profile_candidates = flat_by_section[CvSection.PROFILE]
    distinct_profile_fact_ids = {decision.fact_id for decision in profile_candidates}
    if len(distinct_profile_fact_ids) > 1:
        fingerprints = tuple(sorted(decision.decision_fingerprint for decision in profile_candidates))
        fingerprint = compute_conflict_fingerprint(
            person_entity_id=profile_candidates[0].person_entity_id,
            fact_id=None,
            entity_id=None,
            conflicting_decision_fingerprints=fingerprints,
            conflict_reason_code=ConflictReasonCode.PROFILE_MULTIPLE_SUMMARY_CANDIDATES,
        )
        conflicts.append(
            CvContentPlanConflict(
                conflict_fingerprint=fingerprint,
                person_entity_id=profile_candidates[0].person_entity_id,
                fact_id=None,
                entity_id=None,
                conflicting_decision_fingerprints=fingerprints,
                conflict_reason_code=ConflictReasonCode.PROFILE_MULTIPLE_SUMMARY_CANDIDATES,
            )
        )
        conflict_decision_fingerprints.update(fingerprints)
        flat_by_section[CvSection.PROFILE] = []

    # -- Step 5: budget evaluation (may clear buckets to BUDGET_NOT_AVAILABLE). --
    budget_limits_by_section: dict[CvSection, int] = (
        {limit.section: limit.max_facts for limit in budget_profile.section_limits}
        if budget_profile is not None
        else {}
    )

    experience_count = (
        sum(len(items) for items in header_by_employment.values())
        + sum(len(items) for items in responsibility_by_employment.values())
        + sum(len(items) for items in achievement_by_employment.values())
    )
    experience_max = budget_limits_by_section.get(CvSection.EXPERIENCE)
    if experience_max == 0:
        for items in header_by_employment.values():
            omitted_data.extend((d, OmissionReasonCode.BUDGET_NOT_AVAILABLE) for d in items)
        for items in responsibility_by_employment.values():
            omitted_data.extend((d, OmissionReasonCode.BUDGET_NOT_AVAILABLE) for d in items)
        for items in achievement_by_employment.values():
            omitted_data.extend((d, OmissionReasonCode.BUDGET_NOT_AVAILABLE) for d in items)
        header_by_employment.clear()
        responsibility_by_employment.clear()
        achievement_by_employment.clear()
        experience_budget_status = SectionBudgetStatus.WITHIN_BUDGET
    else:
        experience_budget_status = _evaluate_budget_status(experience_count, experience_max)

    flat_budget_status: dict[CvSection, SectionBudgetStatus] = {}
    for section, candidates in flat_by_section.items():
        max_facts = budget_limits_by_section.get(section)
        if max_facts == 0:
            omitted_data.extend((d, OmissionReasonCode.BUDGET_NOT_AVAILABLE) for d in candidates)
            flat_by_section[section] = []
            flat_budget_status[section] = SectionBudgetStatus.WITHIN_BUDGET
        else:
            flat_budget_status[section] = _evaluate_budget_status(len(candidates), max_facts)

    # -- Step 6: employment entry ordering. --
    employment_entry_order = employment_entry_order or {}
    employment_ids = (
        set(header_by_employment) | set(responsibility_by_employment) | set(achievement_by_employment)
    )
    explicit_ids = sorted(
        (eid for eid in employment_ids if eid in employment_entry_order),
        key=lambda eid: employment_entry_order[eid],
    )
    fallback_ids = sorted(
        (eid for eid in employment_ids if eid not in employment_entry_order), key=str
    )
    ordered_employment_ids = explicit_ids + fallback_ids
    entry_order_by_id = {eid: index for index, eid in enumerate(ordered_employment_ids)}
    entry_order_source_by_id = {
        eid: (
            EntryOrderSource.EXPLICIT_INPUT
            if eid in employment_entry_order
            else EntryOrderSource.UUID_FALLBACK
        )
        for eid in employment_ids
    }

    # -- Step 7: build PlannedFactUse + CvExperienceEntryPlan + sections. --
    entries: list[CvExperienceEntryPlan] = []
    for eid in ordered_employment_ids:
        header_uses = _build_uses(header_by_employment.get(eid, []), target_section=CvSection.EXPERIENCE)
        responsibility_uses = _build_uses(
            responsibility_by_employment.get(eid, []), target_section=CvSection.EXPERIENCE
        )
        achievement_uses = _build_uses(
            achievement_by_employment.get(eid, []), target_section=CvSection.EXPERIENCE
        )
        entry_order = entry_order_by_id[eid]
        entry_order_source = entry_order_source_by_id[eid]
        entry_fingerprint = compute_experience_entry_fingerprint(
            employment_entity_id=eid,
            entry_order=entry_order,
            entry_order_source=entry_order_source,
            header_fact_uses=header_uses,
            responsibility_fact_uses=responsibility_uses,
            achievement_fact_uses=achievement_uses,
        )
        entries.append(
            CvExperienceEntryPlan(
                experience_entry_fingerprint=entry_fingerprint,
                employment_entity_id=eid,
                entry_order=entry_order,
                entry_order_source=entry_order_source,
                header_fact_uses=header_uses,
                responsibility_fact_uses=responsibility_uses,
                achievement_fact_uses=achievement_uses,
            )
        )
    entries.sort(key=lambda entry: entry.entry_order)

    experience_section_fingerprint = compute_section_plan_fingerprint(
        kind="EXPERIENCE",
        section=CvSection.EXPERIENCE,
        skipped_empty=(len(entries) == 0),
        budget_status=experience_budget_status,
        member_fingerprints=[entry.experience_entry_fingerprint for entry in entries],
    )
    experience_section = CvExperienceSectionPlan(
        section_plan_fingerprint=experience_section_fingerprint,
        skipped_empty=(len(entries) == 0),
        budget_status=experience_budget_status,
        experience_entries=tuple(entries),
    )

    sections_by_key: dict[CvSection, CvSectionPlan] = {CvSection.EXPERIENCE: experience_section}
    for section in SECTION_ORDER:
        if section == CvSection.EXPERIENCE:
            continue
        candidates = flat_by_section.get(section, [])
        uses = _build_uses(candidates, target_section=section)
        status = flat_budget_status.get(section, SectionBudgetStatus.NOT_EVALUATED)
        fingerprint = compute_section_plan_fingerprint(
            kind="FLAT",
            section=section,
            skipped_empty=(len(uses) == 0),
            budget_status=status,
            member_fingerprints=[use.planned_fact_use_fingerprint for use in uses],
        )
        sections_by_key[section] = CvFlatSectionPlan(
            section=section,
            section_plan_fingerprint=fingerprint,
            skipped_empty=(len(uses) == 0),
            budget_status=status,
            planned_fact_uses=uses,
        )

    sections = tuple(sections_by_key[section] for section in SECTION_ORDER)

    # -- Step 8: pending approvals + omissions. --
    pending_approvals: list[PendingFactApproval] = []
    for decision, target_section in pending_approvals_data:
        approval_type = resolve_approval_type(decision.reason_codes)
        fingerprint = compute_pending_approval_fingerprint(
            person_entity_id=decision.person_entity_id,
            entity_id=decision.entity_id,
            fact_id=decision.fact_id,
            fact_type=decision.fact_type,
            fact_selection_decision_fingerprint=decision.decision_fingerprint,
            requested_operation=decision.requested_operation,
            target_section=target_section,
            target_scope=decision.requested_target_scope,
            reason_codes=decision.reason_codes,
            approval_type=approval_type,
        )
        pending_approvals.append(
            PendingFactApproval(
                pending_approval_fingerprint=fingerprint,
                person_entity_id=decision.person_entity_id,
                entity_id=decision.entity_id,
                fact_id=decision.fact_id,
                fact_type=decision.fact_type,
                fact_selection_decision_fingerprint=decision.decision_fingerprint,
                requested_operation=decision.requested_operation,
                target_section=target_section,
                target_scope=decision.requested_target_scope,
                reason_codes=decision.reason_codes,
                approval_type=approval_type,
            )
        )
    pending_approvals.sort(key=lambda item: item.pending_approval_fingerprint)

    omitted_facts: list[OmittedFactDecision] = []
    for decision, reason_code in omitted_data:
        fingerprint = compute_omission_fingerprint(
            decision_fingerprint=decision.decision_fingerprint,
            person_entity_id=decision.person_entity_id,
            entity_id=decision.entity_id,
            fact_id=decision.fact_id,
            fact_type=decision.fact_type,
            original_outcome=decision.decision,
            omission_reason_code=reason_code,
            target_scope=decision.requested_target_scope,
            requested_operation=decision.requested_operation,
        )
        omitted_facts.append(
            OmittedFactDecision(
                omission_fingerprint=fingerprint,
                decision_fingerprint=decision.decision_fingerprint,
                person_entity_id=decision.person_entity_id,
                entity_id=decision.entity_id,
                fact_id=decision.fact_id,
                fact_type=decision.fact_type,
                original_outcome=decision.decision,
                omission_reason_code=reason_code,
                target_scope=decision.requested_target_scope,
                requested_operation=decision.requested_operation,
            )
        )
    omitted_facts.sort(key=lambda item: item.omission_fingerprint)

    conflicts.sort(key=lambda item: item.conflict_fingerprint)

    # -- Step 9: assemble the plan + top-level fingerprint. --
    budget_profile_fingerprint = (
        budget_profile.budget_profile_fingerprint if budget_profile is not None else None
    )
    source_decision_fingerprints = tuple(sorted(unique_decisions.keys()))

    content_plan_fingerprint = compute_content_plan_fingerprint(
        schema_version=CV_CONTENT_PLAN_SCHEMA_VERSION,
        target_context_fingerprint=common_target_context_fingerprint,
        job_or_application_id=target_context.job_or_application_id,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        selection_policy_version=common_selection_policy_version,
        transformation_policy_version=common_transformation_policy_version,
        budget_profile_fingerprint=budget_profile_fingerprint,
        source_decision_fingerprints=source_decision_fingerprints,
        section_plan_fingerprints=[section.section_plan_fingerprint for section in sections],
        pending_approval_fingerprints=[item.pending_approval_fingerprint for item in pending_approvals],
        omission_fingerprints=[item.omission_fingerprint for item in omitted_facts],
        conflict_fingerprints=[item.conflict_fingerprint for item in conflicts],
        career_positioning_snapshot_fingerprint=career_positioning_snapshot_fingerprint,
    )

    if conflicts:
        plan_status = CvContentPlanStatus.INVALID
    elif experience_budget_status == SectionBudgetStatus.EXCEEDS_BUDGET or any(
        status == SectionBudgetStatus.EXCEEDS_BUDGET for status in flat_budget_status.values()
    ):
        plan_status = CvContentPlanStatus.REQUIRES_REVIEW
    else:
        plan_status = CvContentPlanStatus.VALID

    plan = CvContentPlan(
        content_plan_fingerprint=content_plan_fingerprint,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        selection_policy_version=common_selection_policy_version,
        transformation_policy_version=common_transformation_policy_version,
        target_context_id=target_context.target_context_id,
        target_context_fingerprint=common_target_context_fingerprint,
        person_entity_id=target_context.person_entity_id,
        job_or_application_id=target_context.job_or_application_id,
        plan_status=plan_status,
        sections=sections,
        pending_approvals=tuple(pending_approvals),
        omitted_facts=tuple(omitted_facts),
        conflicts=tuple(conflicts),
        source_decision_fingerprints=source_decision_fingerprints,
        budget_profile_fingerprint=budget_profile_fingerprint,
        career_positioning_snapshot_fingerprint=career_positioning_snapshot_fingerprint,
    )

    return CvContentPlanBuildResult(
        outcome=CvContentPlanBuildOutcome.BUILT, plan=plan, snapshot_violations=()
    )
