"""Deterministic P5a CV content generation from an already ``p5_ready`` P4 plan.

``generate_cv_content_draft`` is a pure, in-memory transformation. It always
re-runs ``validate_cv_content_plan`` itself -- it never trusts
``plan.plan_status``, a caller boolean, or a previously stored ``p5_ready``
value. It performs no database I/O, calls no LLM, creates no new fact or
entity, never changes the P4 result partition, and never reads a
``source_reference``, a legacy record key, or a list index as identity.

Every fingerprint below is ``hashlib.sha256`` over
``truth_fingerprint.canonical_json_bytes``, lowercase hex, with no
``uuid4()``, no ``datetime.now()``, and no source_reference/legacy identity
folded in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from app.schemas.cv_content_generation import (
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    BlockedGenerationItem,
    CvContentGenerationResult,
    CvContentGenerationViolation,
    CvGeneratedContentDraft,
    CvGeneratedExperienceEntry,
    CvGeneratedExperienceSection,
    CvGeneratedFlatSection,
    CvGeneratedSection,
    GeneratedCvElement,
    GenerationContext,
    GenerationProvenance,
    GenerationStatus,
    GenerationViolationCode,
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
)
from app.schemas.cv_content_plan import (
    CvContentPlan,
    CvContentPlanReplayInput,
    CvExperienceSectionPlan,
    CvFlatSectionPlan,
    CvStructuralValidationStatus,
    CvFreshnessStatus,
    PlannedFactUse,
)
from app.schemas.fact_selection import FactSelectionDecision
from app.schemas.truth_entity import EntityType
from app.services.cv_content_generation_policy import (
    classify_operation,
    resolve_payload_shape,
)
from app.services.cv_content_generation_renderer import (
    RENDERER_CONTRACT_VERSION,
    RENDERER_NAME,
    render_generated_text,
)
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.truth_fingerprint import canonical_json_bytes


FACT_PAYLOAD_FINGERPRINT_VERSION = "cv-content-generation-payload-v1"
FACT_PAYLOAD_SET_FINGERPRINT_VERSION = "cv-content-generation-payload-set-v1"
GENERATION_CONTEXT_FINGERPRINT_VERSION = "cv-content-generation-context-v1"
GENERATION_PROVENANCE_FINGERPRINT_VERSION = "cv-content-generation-provenance-v1"
GENERATED_ELEMENT_FINGERPRINT_VERSION = "cv-content-generation-element-v1"
BLOCKED_ITEM_FINGERPRINT_VERSION = "cv-content-generation-blocked-item-v1"
GENERATED_SECTION_FINGERPRINT_VERSION = "cv-content-generation-section-v1"
GENERATED_EXPERIENCE_ENTRY_FINGERPRINT_VERSION = "cv-content-generation-experience-entry-v1"
GENERATED_CONTENT_DRAFT_FINGERPRINT_VERSION = "cv-content-generation-draft-v1"
GENERATION_RESULT_FINGERPRINT_VERSION = "cv-content-generation-result-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_fact_payload_fingerprint(
    *,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    fact_revision: int,
    fact_content_fingerprint: str,
    value_json: Any,
    normalized_value_json: Any,
    generation_policy_version: str,
) -> str:
    content = {
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "fact_revision": fact_revision,
        "fact_content_fingerprint": fact_content_fingerprint,
        "value_json": value_json,
        "normalized_value_json": normalized_value_json,
        "generation_policy_version": generation_policy_version,
    }
    return _fingerprint(FACT_PAYLOAD_FINGERPRINT_VERSION, content)


def compute_fact_payload_set_fingerprint(
    payloads: Sequence[ApprovedFactPayloadSnapshot],
) -> str:
    content = {
        "payload_fingerprints": sorted(payload.payload_fingerprint for payload in payloads)
    }
    return _fingerprint(FACT_PAYLOAD_SET_FINGERPRINT_VERSION, content)


def compute_generation_context_fingerprint(context: GenerationContext) -> str:
    content = {
        "locale": context.locale,
        "deterministic_template_version": context.deterministic_template_version,
        "generation_policy_version": context.generation_policy_version,
    }
    return _fingerprint(GENERATION_CONTEXT_FINGERPRINT_VERSION, content)


def compute_generation_provenance_fingerprint(
    *,
    generation_schema_version: str,
    generation_policy_version: str,
    deterministic_template_version: str,
    generation_context_fingerprint: str,
    payload_set_fingerprint: str,
    payload_fingerprint: str,
    renderer_contract_version: str,
    renderer_name: str,
) -> str:
    content = {
        "generation_schema_version": generation_schema_version,
        "generation_policy_version": generation_policy_version,
        "deterministic_template_version": deterministic_template_version,
        "generation_context_fingerprint": generation_context_fingerprint,
        "payload_set_fingerprint": payload_set_fingerprint,
        "payload_fingerprint": payload_fingerprint,
        "renderer_contract_version": renderer_contract_version,
        "renderer_name": renderer_name,
    }
    return _fingerprint(GENERATION_PROVENANCE_FINGERPRINT_VERSION, content)


def compute_generated_element_fingerprint(
    *,
    content_plan_fingerprint: str,
    section_plan_fingerprint: str,
    planned_fact_use_fingerprint: str,
    fact_selection_decision_fingerprint: str,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    fact_revision: int,
    fact_content_fingerprint: str,
    payload_fingerprint: str,
    target_section: str,
    target_scope: str,
    allowed_operation: str,
    placement_order: int,
    employment_scope_entity_id: UUID | None,
    generated_text: str,
    generation_provenance_fingerprint: str,
) -> str:
    content = {
        "content_plan_fingerprint": content_plan_fingerprint,
        "section_plan_fingerprint": section_plan_fingerprint,
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "fact_selection_decision_fingerprint": fact_selection_decision_fingerprint,
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "fact_revision": fact_revision,
        "fact_content_fingerprint": fact_content_fingerprint,
        "payload_fingerprint": payload_fingerprint,
        "target_section": str(target_section),
        "target_scope": target_scope,
        "allowed_operation": str(allowed_operation),
        "placement_order": placement_order,
        "employment_scope_entity_id": (
            str(employment_scope_entity_id) if employment_scope_entity_id is not None else None
        ),
        "generated_text": generated_text,
        "generation_provenance_fingerprint": generation_provenance_fingerprint,
    }
    return _fingerprint(GENERATED_ELEMENT_FINGERPRINT_VERSION, content)


def compute_blocked_generation_item_fingerprint(
    *, planned_fact_use_fingerprint: str, violation_code: GenerationViolationCode
) -> str:
    content = {
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "violation_code": violation_code.value,
    }
    return _fingerprint(BLOCKED_ITEM_FINGERPRINT_VERSION, content)


def compute_generated_section_fingerprint(
    *, kind: str, section: str, member_fingerprints: Sequence[str]
) -> str:
    content = {
        "kind": kind,
        "section": str(section),
        "member_fingerprints": list(member_fingerprints),
    }
    return _fingerprint(GENERATED_SECTION_FINGERPRINT_VERSION, content)


def compute_generated_experience_entry_fingerprint(
    *,
    employment_entity_id: UUID,
    entry_order: int,
    entry_order_source: str,
    header_element_fingerprints: Sequence[str],
    responsibility_element_fingerprints: Sequence[str],
    achievement_element_fingerprints: Sequence[str],
) -> str:
    content = {
        "employment_entity_id": str(employment_entity_id),
        "entry_order": entry_order,
        "entry_order_source": str(entry_order_source),
        "header_element_fingerprints": list(header_element_fingerprints),
        "responsibility_element_fingerprints": list(responsibility_element_fingerprints),
        "achievement_element_fingerprints": list(achievement_element_fingerprints),
    }
    return _fingerprint(GENERATED_EXPERIENCE_ENTRY_FINGERPRINT_VERSION, content)


def compute_generated_content_draft_fingerprint(
    *, content_plan_fingerprint: str, generated_section_fingerprints: Sequence[str]
) -> str:
    content = {
        "content_plan_fingerprint": content_plan_fingerprint,
        "generated_section_fingerprints": list(generated_section_fingerprints),
    }
    return _fingerprint(GENERATED_CONTENT_DRAFT_FINGERPRINT_VERSION, content)


def compute_generation_result_fingerprint(
    *,
    status: GenerationStatus,
    ready_for_p55: bool,
    content_plan_fingerprint: str,
    payload_set_fingerprint: str | None,
    generation_context_fingerprint: str | None,
    draft_fingerprint: str | None,
    blocked_item_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "ready_for_p55": ready_for_p55,
        "content_plan_fingerprint": content_plan_fingerprint,
        "payload_set_fingerprint": payload_set_fingerprint,
        "generation_context_fingerprint": generation_context_fingerprint,
        "draft_fingerprint": draft_fingerprint,
        "blocked_item_fingerprints": sorted(blocked_item_fingerprints),
    }
    return _fingerprint(GENERATION_RESULT_FINGERPRINT_VERSION, content)


def _all_planned_uses(plan: CvContentPlan) -> list[PlannedFactUse]:
    uses: list[PlannedFactUse] = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                uses.extend(entry.header_fact_uses)
                uses.extend(entry.responsibility_fact_uses)
                uses.extend(entry.achievement_fact_uses)
        else:
            uses.extend(section.planned_fact_uses)
    return uses


def _classify_unready_plan_violation(
    *,
    structural_status: CvStructuralValidationStatus,
    freshness_status: CvFreshnessStatus,
    has_conflicts: bool,
) -> GenerationViolationCode:
    if structural_status != CvStructuralValidationStatus.VALID:
        return GenerationViolationCode.INPUT_PLAN_STRUCTURALLY_INVALID
    if has_conflicts:
        return GenerationViolationCode.INPUT_PLAN_HAS_CONFLICTS
    if freshness_status == CvFreshnessStatus.STALE:
        return GenerationViolationCode.INPUT_PLAN_STALE
    return GenerationViolationCode.INPUT_PLAN_NOT_READY


def _blocked_result(
    *,
    content_plan_fingerprint: str,
    violation_code: GenerationViolationCode,
    detail: str,
) -> CvContentGenerationResult:
    violation = CvContentGenerationViolation(violation_code=violation_code, detail=detail)
    result_fingerprint = compute_generation_result_fingerprint(
        status=GenerationStatus.BLOCKED,
        ready_for_p55=False,
        content_plan_fingerprint=content_plan_fingerprint,
        payload_set_fingerprint=None,
        generation_context_fingerprint=None,
        draft_fingerprint=None,
        blocked_item_fingerprints=(),
    )
    return CvContentGenerationResult(
        result_fingerprint=result_fingerprint,
        status=GenerationStatus.BLOCKED,
        ready_for_p55=False,
        content_plan_fingerprint=content_plan_fingerprint,
        draft=None,
        blocked_items=(),
        violations=(violation,),
    )


def _invalid_input_result(
    *,
    content_plan_fingerprint: str,
    violations: Sequence[CvContentGenerationViolation],
) -> CvContentGenerationResult:
    result_fingerprint = compute_generation_result_fingerprint(
        status=GenerationStatus.INVALID_INPUT,
        ready_for_p55=False,
        content_plan_fingerprint=content_plan_fingerprint,
        payload_set_fingerprint=None,
        generation_context_fingerprint=None,
        draft_fingerprint=None,
        blocked_item_fingerprints=(),
    )
    return CvContentGenerationResult(
        result_fingerprint=result_fingerprint,
        status=GenerationStatus.INVALID_INPUT,
        ready_for_p55=False,
        content_plan_fingerprint=content_plan_fingerprint,
        draft=None,
        blocked_items=(),
        violations=tuple(violations),
    )


def _process_use(
    use: PlannedFactUse,
    *,
    content_plan_fingerprint: str,
    section_plan_fingerprint: str,
    payloads_by_fact_id: Mapping[UUID, ApprovedFactPayloadSnapshot],
    generation_context: GenerationContext,
    generation_context_fingerprint: str,
    payload_set_fingerprint: str,
) -> GeneratedCvElement | BlockedGenerationItem:
    def _blocked(
        code: GenerationViolationCode, detail: str
    ) -> BlockedGenerationItem:
        return BlockedGenerationItem(
            blocked_item_fingerprint=compute_blocked_generation_item_fingerprint(
                planned_fact_use_fingerprint=use.planned_fact_use_fingerprint,
                violation_code=code,
            ),
            planned_fact_use_fingerprint=use.planned_fact_use_fingerprint,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            person_entity_id=use.person_entity_id,
            entity_id=use.entity_id,
            fact_id=use.fact_id,
            fact_type=use.fact_type,
            fact_revision=use.fact_revision,
            fact_content_fingerprint=use.fact_content_fingerprint,
            target_section=use.target_section,
            target_scope=use.target_scope,
            requested_operation=use.requested_operation,
            allowed_operation=use.allowed_operation,
            violation_code=code,
            detail=detail,
        )

    payload = payloads_by_fact_id.get(use.fact_id)
    if payload is None:
        return _blocked(
            GenerationViolationCode.PAYLOAD_MISSING,
            "no approved fact payload was supplied for this fact_id",
        )
    if payload.person_entity_id != use.person_entity_id:
        return _blocked(
            GenerationViolationCode.PAYLOAD_PERSON_MISMATCH,
            "payload.person_entity_id does not match the planned fact use",
        )
    if payload.entity_id != use.entity_id:
        return _blocked(
            GenerationViolationCode.PAYLOAD_ENTITY_MISMATCH,
            "payload.entity_id does not match the planned fact use",
        )
    if payload.fact_type != use.fact_type:
        return _blocked(
            GenerationViolationCode.PAYLOAD_FACT_TYPE_MISMATCH,
            "payload.fact_type does not match the planned fact use",
        )
    if payload.fact_revision != use.fact_revision:
        return _blocked(
            GenerationViolationCode.PAYLOAD_REVISION_MISMATCH,
            "payload.fact_revision does not match the planned fact use",
        )
    if payload.fact_content_fingerprint != use.fact_content_fingerprint:
        return _blocked(
            GenerationViolationCode.PAYLOAD_CONTENT_FINGERPRINT_MISMATCH,
            "payload.fact_content_fingerprint does not match the planned fact use",
        )

    expected_payload_fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=payload.person_entity_id,
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        fact_revision=payload.fact_revision,
        fact_content_fingerprint=payload.fact_content_fingerprint,
        value_json=payload.value_json,
        normalized_value_json=payload.normalized_value_json,
        generation_policy_version=generation_context.generation_policy_version,
    )
    if expected_payload_fingerprint != payload.payload_fingerprint:
        return _blocked(
            GenerationViolationCode.PAYLOAD_FINGERPRINT_MISMATCH,
            "payload_fingerprint does not match its recomputed value",
        )

    operation_violation = classify_operation(use.allowed_operation)
    if operation_violation is not None:
        return _blocked(
            operation_violation,
            f"{use.allowed_operation.value} cannot be generated by P5a",
        )

    shape = resolve_payload_shape(use.fact_type, payload.value_json)
    if not shape.is_valid:
        assert shape.violation_code is not None
        return _blocked(shape.violation_code, shape.detail)

    try:
        generated_text = render_generated_text(
            fact_type=use.fact_type, operation=use.allowed_operation, shape=shape
        )
    except ValueError as error:
        return _blocked(GenerationViolationCode.RENDERING_FAILED, str(error))

    provenance_fingerprint = compute_generation_provenance_fingerprint(
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        generation_policy_version=generation_context.generation_policy_version,
        deterministic_template_version=generation_context.deterministic_template_version,
        generation_context_fingerprint=generation_context_fingerprint,
        payload_set_fingerprint=payload_set_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        renderer_contract_version=RENDERER_CONTRACT_VERSION,
        renderer_name=RENDERER_NAME,
    )
    provenance = GenerationProvenance(
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        generation_policy_version=generation_context.generation_policy_version,
        deterministic_template_version=generation_context.deterministic_template_version,
        generation_context_fingerprint=generation_context_fingerprint,
        payload_set_fingerprint=payload_set_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        renderer_contract_version=RENDERER_CONTRACT_VERSION,
        renderer_name=RENDERER_NAME,
    )
    element_fingerprint = compute_generated_element_fingerprint(
        content_plan_fingerprint=content_plan_fingerprint,
        section_plan_fingerprint=section_plan_fingerprint,
        planned_fact_use_fingerprint=use.planned_fact_use_fingerprint,
        fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
        person_entity_id=use.person_entity_id,
        entity_id=use.entity_id,
        fact_id=use.fact_id,
        fact_type=use.fact_type,
        fact_revision=use.fact_revision,
        fact_content_fingerprint=use.fact_content_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        target_section=use.target_section.value,
        target_scope=use.target_scope,
        allowed_operation=use.allowed_operation.value,
        placement_order=use.placement_order,
        employment_scope_entity_id=use.employment_scope_entity_id,
        generated_text=generated_text,
        generation_provenance_fingerprint=provenance_fingerprint,
    )
    return GeneratedCvElement(
        generated_element_fingerprint=element_fingerprint,
        content_plan_fingerprint=content_plan_fingerprint,
        section_plan_fingerprint=section_plan_fingerprint,
        planned_fact_use_fingerprint=use.planned_fact_use_fingerprint,
        fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
        person_entity_id=use.person_entity_id,
        entity_id=use.entity_id,
        fact_id=use.fact_id,
        fact_type=use.fact_type,
        fact_revision=use.fact_revision,
        fact_content_fingerprint=use.fact_content_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        target_section=use.target_section,
        target_scope=use.target_scope,
        allowed_operation=use.allowed_operation,
        placement_order=use.placement_order,
        employment_scope_entity_id=use.employment_scope_entity_id,
        generated_text=generated_text,
        generation_provenance=provenance,
    )


def generate_cv_content_draft(
    *,
    plan: CvContentPlan,
    decisions_by_fingerprint: Mapping[str, FactSelectionDecision],
    entity_types: Mapping[UUID, EntityType],
    current_replay_input: CvContentPlanReplayInput | None,
    fact_payloads: ApprovedFactPayloadSet,
    generation_context: GenerationContext,
) -> CvContentGenerationResult:
    """Deterministically render an already-approved P4 plan's facts.

    Always re-validates ``plan`` via ``validate_cv_content_plan`` before
    generating anything -- never trusts ``plan.plan_status`` or a
    previously computed ``p5_ready`` value supplied by the caller.
    """

    validation = validate_cv_content_plan(
        plan,
        decisions_by_fingerprint,
        entity_types=entity_types,
        current_replay_input=current_replay_input,
    )
    if not validation.p5_ready:
        violation_code = _classify_unready_plan_violation(
            structural_status=validation.structural_status,
            freshness_status=validation.freshness_status,
            has_conflicts=len(plan.conflicts) > 0,
        )
        return _blocked_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violation_code=violation_code,
            detail=f"P4 plan is not p5_ready: {violation_code.value}",
        )

    all_uses = _all_planned_uses(plan)

    expected_payload_set_fingerprint = compute_fact_payload_set_fingerprint(
        fact_payloads.payloads
    )
    if expected_payload_set_fingerprint != fact_payloads.payload_set_fingerprint:
        return _invalid_input_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentGenerationViolation(
                    violation_code=GenerationViolationCode.PAYLOAD_SET_FINGERPRINT_MISMATCH,
                    detail="payload_set_fingerprint does not match its recomputed value",
                ),
            ),
        )

    fact_ids = [payload.fact_id for payload in fact_payloads.payloads]
    if len(fact_ids) != len(set(fact_ids)):
        return _invalid_input_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentGenerationViolation(
                    violation_code=GenerationViolationCode.PAYLOAD_DUPLICATE_FACT_ID,
                    detail="the same fact_id appears more than once in the payload set",
                ),
            ),
        )

    payload_fingerprints = [payload.payload_fingerprint for payload in fact_payloads.payloads]
    if len(payload_fingerprints) != len(set(payload_fingerprints)):
        return _invalid_input_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentGenerationViolation(
                    violation_code=GenerationViolationCode.PAYLOAD_DUPLICATE_FINGERPRINT,
                    detail="the same payload_fingerprint appears more than once in the payload set",
                ),
            ),
        )

    planned_fact_ids = {use.fact_id for use in all_uses}
    extra_payloads = [
        payload for payload in fact_payloads.payloads if payload.fact_id not in planned_fact_ids
    ]
    if extra_payloads:
        return _invalid_input_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=tuple(
                CvContentGenerationViolation(
                    violation_code=GenerationViolationCode.PAYLOAD_EXTRA_FACT,
                    detail="payload does not correspond to any planned fact use",
                    fact_id=payload.fact_id,
                )
                for payload in extra_payloads
            ),
        )

    payloads_by_fact_id = {payload.fact_id: payload for payload in fact_payloads.payloads}
    generation_context_fingerprint = compute_generation_context_fingerprint(generation_context)
    payload_set_fingerprint = fact_payloads.payload_set_fingerprint

    blocked_items: list[BlockedGenerationItem] = []
    generated_sections: list[CvGeneratedSection] = []
    generated_section_fingerprints: list[str] = []

    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            assert isinstance(section, CvExperienceSectionPlan)
            generated_entries: list[CvGeneratedExperienceEntry] = []
            for entry in section.experience_entries:
                bucket_results: dict[str, list[GeneratedCvElement]] = {
                    "header": [],
                    "responsibility": [],
                    "achievement": [],
                }
                for bucket_name, bucket_uses in (
                    ("header", entry.header_fact_uses),
                    ("responsibility", entry.responsibility_fact_uses),
                    ("achievement", entry.achievement_fact_uses),
                ):
                    for use in bucket_uses:
                        outcome = _process_use(
                            use,
                            content_plan_fingerprint=plan.content_plan_fingerprint,
                            section_plan_fingerprint=section.section_plan_fingerprint,
                            payloads_by_fact_id=payloads_by_fact_id,
                            generation_context=generation_context,
                            generation_context_fingerprint=generation_context_fingerprint,
                            payload_set_fingerprint=payload_set_fingerprint,
                        )
                        if isinstance(outcome, BlockedGenerationItem):
                            blocked_items.append(outcome)
                        else:
                            bucket_results[bucket_name].append(outcome)

                entry_fingerprint = compute_generated_experience_entry_fingerprint(
                    employment_entity_id=entry.employment_entity_id,
                    entry_order=entry.entry_order,
                    entry_order_source=entry.entry_order_source.value,
                    header_element_fingerprints=[
                        element.generated_element_fingerprint
                        for element in bucket_results["header"]
                    ],
                    responsibility_element_fingerprints=[
                        element.generated_element_fingerprint
                        for element in bucket_results["responsibility"]
                    ],
                    achievement_element_fingerprints=[
                        element.generated_element_fingerprint
                        for element in bucket_results["achievement"]
                    ],
                )
                generated_entries.append(
                    CvGeneratedExperienceEntry(
                        experience_entry_fingerprint=entry.experience_entry_fingerprint,
                        generated_experience_entry_fingerprint=entry_fingerprint,
                        employment_entity_id=entry.employment_entity_id,
                        entry_order=entry.entry_order,
                        entry_order_source=entry.entry_order_source,
                        header_elements=tuple(bucket_results["header"]),
                        responsibility_elements=tuple(bucket_results["responsibility"]),
                        achievement_elements=tuple(bucket_results["achievement"]),
                    )
                )

            section_fingerprint = compute_generated_section_fingerprint(
                kind="EXPERIENCE",
                section=section.section.value,
                member_fingerprints=[
                    entry.generated_experience_entry_fingerprint for entry in generated_entries
                ],
            )
            generated_sections.append(
                CvGeneratedExperienceSection(
                    section_plan_fingerprint=section.section_plan_fingerprint,
                    generated_section_fingerprint=section_fingerprint,
                    generated_experience_entries=tuple(generated_entries),
                )
            )
            generated_section_fingerprints.append(section_fingerprint)
        else:
            assert isinstance(section, CvFlatSectionPlan)
            flat_elements: list[GeneratedCvElement] = []
            for use in section.planned_fact_uses:
                outcome = _process_use(
                    use,
                    content_plan_fingerprint=plan.content_plan_fingerprint,
                    section_plan_fingerprint=section.section_plan_fingerprint,
                    payloads_by_fact_id=payloads_by_fact_id,
                    generation_context=generation_context,
                    generation_context_fingerprint=generation_context_fingerprint,
                    payload_set_fingerprint=payload_set_fingerprint,
                )
                if isinstance(outcome, BlockedGenerationItem):
                    blocked_items.append(outcome)
                else:
                    flat_elements.append(outcome)

            section_fingerprint = compute_generated_section_fingerprint(
                kind="FLAT",
                section=section.section.value,
                member_fingerprints=[
                    element.generated_element_fingerprint for element in flat_elements
                ],
            )
            generated_sections.append(
                CvGeneratedFlatSection(
                    section=section.section,
                    section_plan_fingerprint=section.section_plan_fingerprint,
                    generated_section_fingerprint=section_fingerprint,
                    generated_elements=tuple(flat_elements),
                )
            )
            generated_section_fingerprints.append(section_fingerprint)

    draft_fingerprint = compute_generated_content_draft_fingerprint(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        generated_section_fingerprints=generated_section_fingerprints,
    )
    draft = CvGeneratedContentDraft(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        generated_content_draft_fingerprint=draft_fingerprint,
        generated_sections=tuple(generated_sections),
    )

    status = GenerationStatus.BLOCKED if blocked_items else GenerationStatus.GENERATED
    ready_for_p55 = status == GenerationStatus.GENERATED

    result_fingerprint = compute_generation_result_fingerprint(
        status=status,
        ready_for_p55=ready_for_p55,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        payload_set_fingerprint=payload_set_fingerprint,
        generation_context_fingerprint=generation_context_fingerprint,
        draft_fingerprint=draft_fingerprint,
        blocked_item_fingerprints=[item.blocked_item_fingerprint for item in blocked_items],
    )

    return CvContentGenerationResult(
        result_fingerprint=result_fingerprint,
        status=status,
        ready_for_p55=ready_for_p55,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        payload_set_fingerprint=payload_set_fingerprint,
        generation_context_fingerprint=generation_context_fingerprint,
        draft=draft,
        blocked_items=tuple(blocked_items),
        violations=(),
        pending_approval_fingerprints=tuple(
            sorted(item.pending_approval_fingerprint for item in plan.pending_approvals)
        ),
        omitted_fact_fingerprints=tuple(
            sorted(item.omission_fingerprint for item in plan.omitted_facts)
        ),
        conflict_fingerprints=tuple(sorted(item.conflict_fingerprint for item in plan.conflicts)),
    )
