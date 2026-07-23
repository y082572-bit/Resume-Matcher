"""Step 2 of Explicit Provenance Stage P5.5: user approval and content
assembly.

``build_approved_cv_content`` never calls a provider and never performs an
LLM validation -- it consumes an already-computed
``ProposalSemanticValidationResult`` plus explicit
``ProposalApprovalDecision`` values. It never imports or calls
``ControlledProposalSemanticValidator``: only pure, deterministic
fingerprint/replay helpers are reused from Step 1's builder module. A
missing decision is always interpreted as PENDING, never as an implicit
APPROVED. The function is fully synchronous and deterministic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.schemas.cv_content_approval import (
    APPROVAL_DECISION_SCHEMA_VERSION,
    APPROVED_CONTENT_SCHEMA_VERSION,
    ApprovalAssemblyStatus,
    ApprovedCvContentDraft,
    ApprovedCvContentElement,
    ApprovedCvContentResult,
    ApprovedCvExperienceEntry,
    ApprovedCvExperienceSection,
    ApprovedCvFlatSection,
    ApprovedCvSection,
    CvContentApprovalViolation,
    CvContentApprovalViolationCode,
    CvContentFinalDisposition,
    FinalContentOrigin,
    FinalDispositionCode,
    ProposalApprovalDecision,
    ProposalApprovalDecisionValue,
    ProposalSemanticValidationItem,
    ProposalSemanticValidationReplayInput,
    ProposalSemanticValidationResult,
    ProposalSemanticVerdict,
    SemanticValidationFreshnessStatus,
    SemanticValidationStatus,
)
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    CvContentGenerationReplayInput,
    CvContentGenerationResult,
    GenerationContext,
    GenerationFreshnessStatus,
)
from app.schemas.cv_content_plan import (
    CvContentPlan,
    CvContentPlanReplayInput,
    CvExperienceSectionPlan,
    CvFlatSectionPlan,
    CvFreshnessStatus,
    CvSection,
    CvStructuralValidationStatus,
    PlannedFactUse,
)
from app.schemas.cv_content_proposal import (
    PROPOSAL_GENERATION_POLICY_VERSION,
    PROPOSAL_GENERATION_SCHEMA_VERSION,
    CvContentProposalGenerationResult,
    CvContentProposalReplayInput,
    GeneratedCvContentProposal,
    ProposalFreshnessStatus,
    ProposalGenerationContext,
)
from app.schemas.fact_selection import FactSelectionDecision
from app.schemas.truth_entity import EntityType
from app.services.cv_content_approval_policy import (
    APPROVAL_DECISION_POLICY_VERSION,
    APPROVED_CONTENT_POLICY_VERSION,
    is_verdict_approvable,
)
from app.services.cv_content_approval_replay import (
    evaluate_approved_content_freshness,
    evaluate_semantic_validation_freshness,
    replay_input_from_semantic_validation_result,
)
from app.services.cv_content_generation_builder import compute_generation_result_fingerprint
from app.services.cv_content_generation_replay import (
    evaluate_generation_freshness,
    replay_input_from_generation_result,
)
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.cv_content_proposal_builder import (
    compute_llm_generation_provenance_fingerprint,
    compute_proposal_result_fingerprint,
)
from app.services.cv_content_proposal_replay import (
    evaluate_proposal_freshness,
    replay_input_from_proposal_result,
)
from app.services.cv_content_generation_builder import compute_generation_provenance_fingerprint
from app.services.cv_content_semantic_validation_builder import (
    compute_semantic_validation_provenance_fingerprint,
    compute_semantic_validation_result_fingerprint,
)
from app.services.truth_fingerprint import canonical_json_bytes


DECISION_CONTEXT_FINGERPRINT_VERSION = "cv-content-approval-decision-context-v1"
DECISION_FINGERPRINT_VERSION = "cv-content-approval-decision-v1"
ELEMENT_FINGERPRINT_VERSION = "cv-content-approval-element-v1"
DISPOSITION_FINGERPRINT_VERSION = "cv-content-approval-disposition-v1"
FLAT_SECTION_FINGERPRINT_VERSION = "cv-content-approval-flat-section-v1"
EXPERIENCE_ENTRY_FINGERPRINT_VERSION = "cv-content-approval-experience-entry-v1"
EXPERIENCE_SECTION_FINGERPRINT_VERSION = "cv-content-approval-experience-section-v1"
DRAFT_FINGERPRINT_VERSION = "cv-content-approval-draft-v1"
RESULT_FINGERPRINT_VERSION = "cv-content-approval-result-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_decision_context_fingerprint(
    *,
    proposal_fingerprint: str,
    proposal_text_fingerprint: str,
    semantic_validation_item_fingerprint: str,
    semantic_validation_result_fingerprint: str,
) -> str:
    content = {
        "proposal_fingerprint": proposal_fingerprint,
        "proposal_text_fingerprint": proposal_text_fingerprint,
        "semantic_validation_item_fingerprint": semantic_validation_item_fingerprint,
        "semantic_validation_result_fingerprint": semantic_validation_result_fingerprint,
    }
    return _fingerprint(DECISION_CONTEXT_FINGERPRINT_VERSION, content)


def compute_proposal_approval_decision_fingerprint(
    *,
    decision_context_fingerprint: str,
    decision: ProposalApprovalDecisionValue,
    decision_policy_version: str,
) -> str:
    content = {
        "decision_context_fingerprint": decision_context_fingerprint,
        "decision": decision.value,
        "decision_policy_version": decision_policy_version,
    }
    return _fingerprint(DECISION_FINGERPRINT_VERSION, content)


def compute_approved_content_element_fingerprint(
    *,
    origin: FinalContentOrigin,
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
    employment_scope_entity_id: UUID | None,
    placement_order: int,
    allowed_operation: str,
    content_text: str,
    origin_provenance_fingerprints: Sequence[str],
) -> str:
    content = {
        "origin": origin.value,
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
        "employment_scope_entity_id": (
            str(employment_scope_entity_id) if employment_scope_entity_id is not None else None
        ),
        "placement_order": placement_order,
        "allowed_operation": str(allowed_operation),
        "content_text": content_text,
        "origin_provenance_fingerprints": list(origin_provenance_fingerprints),
    }
    return _fingerprint(ELEMENT_FINGERPRINT_VERSION, content)


def compute_final_disposition_fingerprint(
    *,
    planned_fact_use_fingerprint: str,
    fact_selection_decision_fingerprint: str,
    fact_id: UUID,
    disposition_code: FinalDispositionCode,
    origin: FinalContentOrigin | None,
    element_fingerprint: str | None,
) -> str:
    content = {
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "fact_selection_decision_fingerprint": fact_selection_decision_fingerprint,
        "fact_id": str(fact_id),
        "disposition_code": disposition_code.value,
        "origin": origin.value if origin is not None else None,
        "element_fingerprint": element_fingerprint,
    }
    return _fingerprint(DISPOSITION_FINGERPRINT_VERSION, content)


def compute_approved_flat_section_fingerprint(
    *, section: str, member_fingerprints: Sequence[str]
) -> str:
    content = {"section": str(section), "member_fingerprints": list(member_fingerprints)}
    return _fingerprint(FLAT_SECTION_FINGERPRINT_VERSION, content)


def compute_approved_experience_entry_fingerprint(
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
    return _fingerprint(EXPERIENCE_ENTRY_FINGERPRINT_VERSION, content)


def compute_approved_experience_section_fingerprint(
    *, member_fingerprints: Sequence[str]
) -> str:
    content = {"member_fingerprints": list(member_fingerprints)}
    return _fingerprint(EXPERIENCE_SECTION_FINGERPRINT_VERSION, content)


def compute_approved_content_draft_fingerprint(
    *, content_plan_fingerprint: str, section_fingerprints: Sequence[str]
) -> str:
    content = {
        "content_plan_fingerprint": content_plan_fingerprint,
        "section_fingerprints": list(section_fingerprints),
    }
    return _fingerprint(DRAFT_FINGERPRINT_VERSION, content)


def compute_approved_content_result_fingerprint(
    *,
    status: ApprovalAssemblyStatus,
    ready_for_p6: bool,
    content_plan_fingerprint: str,
    p5a_result_fingerprint: str | None,
    p5b_result_fingerprint: str | None,
    semantic_validation_result_fingerprint: str | None,
    draft_fingerprint: str | None,
    disposition_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "ready_for_p6": ready_for_p6,
        "content_plan_fingerprint": content_plan_fingerprint,
        "p5a_result_fingerprint": p5a_result_fingerprint,
        "p5b_result_fingerprint": p5b_result_fingerprint,
        "semantic_validation_result_fingerprint": semantic_validation_result_fingerprint,
        "draft_fingerprint": draft_fingerprint,
        "disposition_fingerprints": sorted(disposition_fingerprints),
    }
    return _fingerprint(RESULT_FINGERPRINT_VERSION, content)


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


def _index_p5a_elements(p5a_result: CvContentGenerationResult) -> dict:
    index = {}
    if p5a_result.draft is not None:
        for section in p5a_result.draft.generated_sections:
            if section.kind == "EXPERIENCE":
                for entry in section.generated_experience_entries:
                    for element in (
                        entry.header_elements + entry.responsibility_elements + entry.achievement_elements
                    ):
                        index[element.planned_fact_use_fingerprint] = element
            else:
                for element in section.generated_elements:
                    index[element.planned_fact_use_fingerprint] = element
    return index


def _classify_unready_plan_violation(
    *,
    structural_status: CvStructuralValidationStatus,
    freshness_status: CvFreshnessStatus,
    has_conflicts: bool,
) -> CvContentApprovalViolationCode:
    if structural_status != CvStructuralValidationStatus.VALID:
        return CvContentApprovalViolationCode.INPUT_PLAN_INVALID
    if has_conflicts:
        return CvContentApprovalViolationCode.INPUT_PLAN_HAS_CONFLICTS
    if freshness_status == CvFreshnessStatus.STALE:
        return CvContentApprovalViolationCode.INPUT_PLAN_STALE
    return CvContentApprovalViolationCode.INPUT_PLAN_INVALID


def _invalid_result(
    *, content_plan_fingerprint: str, violations: Sequence[CvContentApprovalViolation]
) -> ApprovedCvContentResult:
    result_fingerprint = compute_approved_content_result_fingerprint(
        status=ApprovalAssemblyStatus.INVALID_INPUT,
        ready_for_p6=False,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=None,
        p5b_result_fingerprint=None,
        semantic_validation_result_fingerprint=None,
        draft_fingerprint=None,
        disposition_fingerprints=(),
    )
    return ApprovedCvContentResult(
        result_fingerprint=result_fingerprint,
        status=ApprovalAssemblyStatus.INVALID_INPUT,
        ready_for_p6=False,
        content_plan_fingerprint=content_plan_fingerprint,
        violations=tuple(violations),
    )


def build_approved_cv_content(
    *,
    plan: CvContentPlan,
    p5a_result: CvContentGenerationResult,
    p5a_generation_context: GenerationContext,
    p5b_result: CvContentProposalGenerationResult,
    proposal_generation_context: ProposalGenerationContext,
    semantic_validation_result: ProposalSemanticValidationResult,
    approval_decisions: Sequence[ProposalApprovalDecision],
    decisions_by_fingerprint: Mapping[str, FactSelectionDecision],
    entity_types: Mapping[UUID, EntityType],
    fact_payloads: ApprovedFactPayloadSet,
    current_plan_replay_input: CvContentPlanReplayInput | None,
    current_p5a_replay_input: CvContentGenerationReplayInput | None,
    current_p5b_replay_input: CvContentProposalReplayInput | None,
    current_semantic_validation_replay_input: ProposalSemanticValidationReplayInput | None,
) -> ApprovedCvContentResult:
    """Step 2: assemble final CV content from deterministic P5a elements and
    semantic-PASS, user-APPROVED P5b proposals. Never calls a provider,
    never runs an LLM validation, and never auto-approves anything."""

    validation = validate_cv_content_plan(
        plan,
        decisions_by_fingerprint,
        entity_types=entity_types,
        current_replay_input=current_plan_replay_input,
    )
    if not validation.p5_ready:
        violation_code = _classify_unready_plan_violation(
            structural_status=validation.structural_status,
            freshness_status=validation.freshness_status,
            has_conflicts=len(plan.conflicts) > 0,
        )
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=violation_code,
                    detail=f"P4 plan is not p5_ready: {violation_code.value}",
                ),
            ),
        )

    if p5a_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail="p5a_result.content_plan_fingerprint does not match plan.content_plan_fingerprint",
                ),
            ),
        )
    expected_p5a_result_fp = compute_generation_result_fingerprint(
        status=p5a_result.status,
        ready_for_p55=p5a_result.ready_for_p55,
        content_plan_fingerprint=p5a_result.content_plan_fingerprint,
        payload_set_fingerprint=p5a_result.payload_set_fingerprint,
        generation_context_fingerprint=p5a_result.generation_context_fingerprint,
        draft_fingerprint=(
            p5a_result.draft.generated_content_draft_fingerprint
            if p5a_result.draft is not None
            else None
        ),
        blocked_item_fingerprints=[b.blocked_item_fingerprint for b in p5a_result.blocked_items],
    )
    if expected_p5a_result_fp != p5a_result.result_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5a_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )
    if current_p5a_replay_input is None:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5A_RESULT_STALE,
                    detail="no current_p5a_replay_input was supplied: freshness not verified",
                ),
            ),
        )
    expected_p5a_replay_input = replay_input_from_generation_result(
        plan=plan,
        payload_set=fact_payloads,
        generation_context=p5a_generation_context,
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=p5a_result,
    )
    p5a_freshness = evaluate_generation_freshness(expected_p5a_replay_input, current_p5a_replay_input)
    if p5a_freshness.freshness_status != GenerationFreshnessStatus.FRESH:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5A_RESULT_STALE,
                    detail=f"P5a result is not fresh: stale_fields={p5a_freshness.stale_fields}",
                ),
            ),
        )

    if p5b_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail="p5b_result.content_plan_fingerprint does not match plan.content_plan_fingerprint",
                ),
            ),
        )
    if p5b_result.p5a_result_fingerprint != p5a_result.result_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5b_result.p5a_result_fingerprint does not match p5a_result.result_fingerprint",
                ),
            ),
        )
    expected_p5b_result_fp = compute_proposal_result_fingerprint(
        status=p5b_result.status,
        content_plan_fingerprint=p5b_result.content_plan_fingerprint,
        p5a_result_fingerprint=p5b_result.p5a_result_fingerprint,
        proposal_context_fingerprint=p5b_result.proposal_context_fingerprint,
        proposal_fingerprints=[p.proposal_fingerprint for p in p5b_result.proposals],
        blocked_proposal_item_fingerprints=[
            b.blocked_proposal_item_fingerprint for b in p5b_result.blocked_proposal_items
        ],
    )
    if expected_p5b_result_fp != p5b_result.result_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.P5B_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5b_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )
    if current_p5b_replay_input is None:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5B_RESULT_STALE,
                    detail="no current_p5b_replay_input was supplied: freshness not verified",
                ),
            ),
        )
    expected_p5b_replay_input = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=plan.content_policy_version,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=p5a_generation_context.generation_policy_version,
        p5a_replay_input=expected_p5a_replay_input,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
        proposal_context=proposal_generation_context,
        result=p5b_result,
    )
    p5b_freshness = evaluate_proposal_freshness(expected_p5b_replay_input, current_p5b_replay_input)
    if p5b_freshness.freshness_status != ProposalFreshnessStatus.FRESH:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5B_RESULT_STALE,
                    detail=f"P5b result is not fresh: stale_fields={p5b_freshness.stale_fields}",
                ),
            ),
        )

    if semantic_validation_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail="semantic_validation_result.content_plan_fingerprint mismatch",
                ),
            ),
        )
    if (
        semantic_validation_result.p5a_result_fingerprint != p5a_result.result_fingerprint
        or semantic_validation_result.p5b_result_fingerprint != p5b_result.result_fingerprint
    ):
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.SEMANTIC_VALIDATION_RESULT_INVALID,
                    detail="semantic_validation_result does not reference the supplied p5a/p5b results",
                ),
            ),
        )
    if semantic_validation_result.status != SemanticValidationStatus.VALIDATED:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.SEMANTIC_VALIDATION_RESULT_INVALID,
                    detail=f"semantic_validation_result.status is not VALIDATED: {semantic_validation_result.status.value}",
                ),
            ),
        )
    expected_semantic_result_fp = compute_semantic_validation_result_fingerprint(
        status=semantic_validation_result.status,
        content_plan_fingerprint=semantic_validation_result.content_plan_fingerprint,
        p5a_result_fingerprint=semantic_validation_result.p5a_result_fingerprint,
        p5b_result_fingerprint=semantic_validation_result.p5b_result_fingerprint,
        validation_context_fingerprint=semantic_validation_result.validation_context_fingerprint,
        validation_item_fingerprints=[
            item.semantic_validation_item_fingerprint
            for item in semantic_validation_result.validation_items
        ],
        blocked_item_fingerprints=[
            item.blocked_semantic_validation_item_fingerprint
            for item in semantic_validation_result.blocked_items
        ],
    )
    if expected_semantic_result_fp != semantic_validation_result.result_fingerprint:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=(
                        CvContentApprovalViolationCode.SEMANTIC_VALIDATION_RESULT_FINGERPRINT_MISMATCH
                    ),
                    detail="semantic_validation_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )
    if current_semantic_validation_replay_input is None:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.SEMANTIC_VALIDATION_RESULT_STALE,
                    detail="no current_semantic_validation_replay_input was supplied: freshness not verified",
                ),
            ),
        )
    expected_semantic_replay_input = replay_input_from_semantic_validation_result(
        plan=plan,
        p5a_replay_input=expected_p5a_replay_input,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=p5a_generation_context.generation_policy_version,
        p5b_replay_input=expected_p5b_replay_input,
        p5b_proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        p5b_proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
        result=semantic_validation_result,
    )
    semantic_freshness = evaluate_semantic_validation_freshness(
        expected_semantic_replay_input, current_semantic_validation_replay_input
    )
    if semantic_freshness.freshness_status != SemanticValidationFreshnessStatus.FRESH:
        return _invalid_result(
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.SEMANTIC_VALIDATION_RESULT_STALE,
                    detail=f"semantic_validation_result is not fresh: stale_fields={semantic_freshness.stale_fields}",
                ),
            ),
        )

    # -- decision integrity: duplicate / extra / unknown / tampered --------
    proposal_by_fp: dict[str, GeneratedCvContentProposal] = {
        p.proposal_fingerprint: p for p in p5b_result.proposals
    }
    semantic_item_by_proposal_fp: dict[str, ProposalSemanticValidationItem] = {
        item.proposal_fingerprint: item for item in semantic_validation_result.validation_items
    }

    seen_proposal_fps: set[str] = set()
    for decision in approval_decisions:
        if decision.proposal_fingerprint in seen_proposal_fps:
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.DUPLICATE_APPROVAL_DECISION,
                        detail="duplicate approval decision for the same proposal_fingerprint",
                        proposal_fingerprint=decision.proposal_fingerprint,
                    ),
                ),
            )
        seen_proposal_fps.add(decision.proposal_fingerprint)

        if decision.proposal_fingerprint not in proposal_by_fp:
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.APPROVAL_DECISION_FOR_UNKNOWN_PROPOSAL,
                        detail="approval decision references a proposal_fingerprint not in p5b_result.proposals",
                        proposal_fingerprint=decision.proposal_fingerprint,
                    ),
                ),
            )

        semantic_item = semantic_item_by_proposal_fp.get(decision.proposal_fingerprint)
        if semantic_item is None:
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.EXTRA_APPROVAL_DECISION,
                        detail="approval decision references a proposal with no semantic validation item",
                        proposal_fingerprint=decision.proposal_fingerprint,
                    ),
                ),
            )

        proposal = proposal_by_fp[decision.proposal_fingerprint]
        if (
            decision.proposal_text_fingerprint != proposal.proposal_text_fingerprint
            or decision.semantic_validation_item_fingerprint
            != semantic_item.semantic_validation_item_fingerprint
            or decision.semantic_validation_result_fingerprint
            != semantic_validation_result.result_fingerprint
        ):
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.APPROVAL_FOR_STALE_PROPOSAL,
                        detail="approval decision references a stale proposal/validation snapshot",
                        proposal_fingerprint=decision.proposal_fingerprint,
                    ),
                ),
            )

        expected_context_fp = compute_decision_context_fingerprint(
            proposal_fingerprint=decision.proposal_fingerprint,
            proposal_text_fingerprint=decision.proposal_text_fingerprint,
            semantic_validation_item_fingerprint=decision.semantic_validation_item_fingerprint,
            semantic_validation_result_fingerprint=decision.semantic_validation_result_fingerprint,
        )
        expected_decision_fp = compute_proposal_approval_decision_fingerprint(
            decision_context_fingerprint=expected_context_fp,
            decision=decision.decision,
            decision_policy_version=decision.decision_policy_version,
        )
        if (
            expected_context_fp != decision.decision_context_fingerprint
            or expected_decision_fp != decision.decision_fingerprint
        ):
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.APPROVAL_DECISION_FINGERPRINT_MISMATCH,
                        detail="decision_fingerprint does not match its recomputed value",
                        proposal_fingerprint=decision.proposal_fingerprint,
                    ),
                ),
            )

    decision_by_proposal_fp: dict[str, ProposalApprovalDecision] = {
        d.proposal_fingerprint: d for d in approval_decisions
    }

    # -- final partition over every P4 PlannedFactUse -----------------------
    p5a_elements_by_use_fp = _index_p5a_elements(p5a_result)
    proposal_by_use_fp: dict[str, GeneratedCvContentProposal] = {
        p.planned_fact_use_fingerprint: p for p in p5b_result.proposals
    }
    semantic_item_by_use_fp: dict[str, ProposalSemanticValidationItem] = {
        item.planned_fact_use_fingerprint: item for item in semantic_validation_result.validation_items
    }

    element_by_use_fp: dict[str, ApprovedCvContentElement] = {}
    disposition_by_use_fp: dict[str, CvContentFinalDisposition] = {}
    all_uses = _all_planned_uses(plan)
    seen_use_fps: set[str] = set()
    for use in all_uses:
        use_fp = use.planned_fact_use_fingerprint
        if use_fp in seen_use_fps:
            return _invalid_result(
                content_plan_fingerprint=plan.content_plan_fingerprint,
                violations=(
                    CvContentApprovalViolation(
                        violation_code=CvContentApprovalViolationCode.FINAL_PARTITION_OVERLAP,
                        detail="the same planned_fact_use_fingerprint appears more than once in the plan",
                        planned_fact_use_fingerprint=use_fp,
                    ),
                ),
            )
        seen_use_fps.add(use_fp)

        p5a_element = p5a_elements_by_use_fp.get(use_fp)
        if p5a_element is not None:
            provenance_fp = compute_generation_provenance_fingerprint(
                **p5a_element.generation_provenance.model_dump()
            )
            element = ApprovedCvContentElement(
                element_fingerprint="0" * 64,
                origin=FinalContentOrigin.DETERMINISTIC_P5A,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                person_entity_id=use.person_entity_id,
                entity_id=use.entity_id,
                fact_id=use.fact_id,
                fact_type=use.fact_type,
                fact_revision=use.fact_revision,
                fact_content_fingerprint=use.fact_content_fingerprint,
                payload_fingerprint=p5a_element.payload_fingerprint,
                target_section=use.target_section,
                target_scope=use.target_scope,
                employment_scope_entity_id=use.employment_scope_entity_id,
                placement_order=use.placement_order,
                allowed_operation=use.allowed_operation,
                content_text=p5a_element.generated_text,
                generated_element_fingerprint=p5a_element.generated_element_fingerprint,
                generation_provenance_fingerprint=provenance_fp,
            )
            element_fp = compute_approved_content_element_fingerprint(
                origin=FinalContentOrigin.DETERMINISTIC_P5A,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                person_entity_id=use.person_entity_id,
                entity_id=use.entity_id,
                fact_id=use.fact_id,
                fact_type=use.fact_type,
                fact_revision=use.fact_revision,
                fact_content_fingerprint=use.fact_content_fingerprint,
                payload_fingerprint=p5a_element.payload_fingerprint,
                target_section=use.target_section.value,
                target_scope=use.target_scope,
                employment_scope_entity_id=use.employment_scope_entity_id,
                placement_order=use.placement_order,
                allowed_operation=use.allowed_operation.value,
                content_text=p5a_element.generated_text,
                origin_provenance_fingerprints=(
                    p5a_element.generated_element_fingerprint,
                    provenance_fp,
                ),
            )
            element = element.model_copy(update={"element_fingerprint": element_fp})
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.INCLUDED_DETERMINISTIC,
                origin=FinalContentOrigin.DETERMINISTIC_P5A,
                element_fingerprint=element_fp,
            )
            element_by_use_fp[use_fp] = element
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.INCLUDED_DETERMINISTIC,
                origin=FinalContentOrigin.DETERMINISTIC_P5A,
                element_fingerprint=element_fp,
                detail="rendered deterministically by P5a",
            )
            continue

        proposal = proposal_by_use_fp.get(use_fp)
        if proposal is None:
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
                origin=None,
                element_fingerprint=None,
            )
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
                detail="blocked by P5a/P5b before reaching P5.5 semantic validation",
            )
            continue

        semantic_item = semantic_item_by_use_fp.get(use_fp)
        if semantic_item is None:
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
                origin=None,
                element_fingerprint=None,
            )
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
                detail="proposal failed a P5.5 deterministic guardrail before semantic validation",
            )
            continue

        if not is_verdict_approvable(semantic_item.verdict):
            code = _verdict_to_disposition_code(semantic_item.verdict)
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=code,
                origin=None,
                element_fingerprint=None,
            )
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=code,
                detail=f"semantic verdict is {semantic_item.verdict.value}, cannot be approved",
            )
            continue

        decision = decision_by_proposal_fp.get(proposal.proposal_fingerprint)
        if decision is None:
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.PENDING_USER_DECISION,
                origin=None,
                element_fingerprint=None,
            )
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.PENDING_USER_DECISION,
                detail="semantic verdict is PASS but no explicit approval decision was supplied",
            )
            continue

        if decision.decision == ProposalApprovalDecisionValue.REJECTED:
            disposition_fp = compute_final_disposition_fingerprint(
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.REJECTED_BY_USER,
                origin=None,
                element_fingerprint=None,
            )
            disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
                disposition_fingerprint=disposition_fp,
                planned_fact_use_fingerprint=use_fp,
                fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
                fact_id=use.fact_id,
                disposition_code=FinalDispositionCode.REJECTED_BY_USER,
                detail="user explicitly rejected this proposal",
            )
            continue

        # decision.decision == APPROVED, verdict == PASS: include it.
        proposal_provenance_fp = compute_llm_generation_provenance_fingerprint(
            proposal.llm_generation_provenance
        )
        semantic_provenance_fp = compute_semantic_validation_provenance_fingerprint(
            semantic_item.provenance
        )
        element = ApprovedCvContentElement(
            element_fingerprint="0" * 64,
            origin=FinalContentOrigin.APPROVED_PROPOSAL_P5B,
            planned_fact_use_fingerprint=use_fp,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            person_entity_id=use.person_entity_id,
            entity_id=use.entity_id,
            fact_id=use.fact_id,
            fact_type=use.fact_type,
            fact_revision=use.fact_revision,
            fact_content_fingerprint=use.fact_content_fingerprint,
            payload_fingerprint=proposal.payload_fingerprint,
            target_section=use.target_section,
            target_scope=use.target_scope,
            employment_scope_entity_id=use.employment_scope_entity_id,
            placement_order=use.placement_order,
            allowed_operation=use.allowed_operation,
            content_text=proposal.proposal_text,
            proposal_fingerprint=proposal.proposal_fingerprint,
            semantic_validation_item_fingerprint=semantic_item.semantic_validation_item_fingerprint,
            approval_decision_fingerprint=decision.decision_fingerprint,
            proposal_provenance_fingerprint=proposal_provenance_fp,
            semantic_validation_provenance_fingerprint=semantic_provenance_fp,
        )
        element_fp = compute_approved_content_element_fingerprint(
            origin=FinalContentOrigin.APPROVED_PROPOSAL_P5B,
            planned_fact_use_fingerprint=use_fp,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            person_entity_id=use.person_entity_id,
            entity_id=use.entity_id,
            fact_id=use.fact_id,
            fact_type=use.fact_type,
            fact_revision=use.fact_revision,
            fact_content_fingerprint=use.fact_content_fingerprint,
            payload_fingerprint=proposal.payload_fingerprint,
            target_section=use.target_section.value,
            target_scope=use.target_scope,
            employment_scope_entity_id=use.employment_scope_entity_id,
            placement_order=use.placement_order,
            allowed_operation=use.allowed_operation.value,
            content_text=proposal.proposal_text,
            origin_provenance_fingerprints=(
                proposal.proposal_fingerprint,
                semantic_item.semantic_validation_item_fingerprint,
                decision.decision_fingerprint,
                proposal_provenance_fp,
                semantic_provenance_fp,
            ),
        )
        element = element.model_copy(update={"element_fingerprint": element_fp})
        disposition_fp = compute_final_disposition_fingerprint(
            planned_fact_use_fingerprint=use_fp,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            fact_id=use.fact_id,
            disposition_code=FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL,
            origin=FinalContentOrigin.APPROVED_PROPOSAL_P5B,
            element_fingerprint=element_fp,
        )
        element_by_use_fp[use_fp] = element
        disposition_by_use_fp[use_fp] = CvContentFinalDisposition(
            disposition_fingerprint=disposition_fp,
            planned_fact_use_fingerprint=use_fp,
            fact_selection_decision_fingerprint=use.fact_selection_decision_fingerprint,
            fact_id=use.fact_id,
            disposition_code=FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL,
            origin=FinalContentOrigin.APPROVED_PROPOSAL_P5B,
            element_fingerprint=element_fp,
            detail="semantic PASS and user APPROVED",
        )

    dispositions = tuple(disposition_by_use_fp[use.planned_fact_use_fingerprint] for use in all_uses)

    # -- assemble the draft, preserving P4's exact structure ----------------
    approved_sections: list[ApprovedCvSection] = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            assert isinstance(section, CvExperienceSectionPlan)
            approved_entries: list[ApprovedCvExperienceEntry] = []
            for entry in section.experience_entries:
                header = tuple(
                    element_by_use_fp[u.planned_fact_use_fingerprint]
                    for u in entry.header_fact_uses
                    if u.planned_fact_use_fingerprint in element_by_use_fp
                )
                responsibility = tuple(
                    element_by_use_fp[u.planned_fact_use_fingerprint]
                    for u in entry.responsibility_fact_uses
                    if u.planned_fact_use_fingerprint in element_by_use_fp
                )
                achievement = tuple(
                    element_by_use_fp[u.planned_fact_use_fingerprint]
                    for u in entry.achievement_fact_uses
                    if u.planned_fact_use_fingerprint in element_by_use_fp
                )
                entry_fp = compute_approved_experience_entry_fingerprint(
                    employment_entity_id=entry.employment_entity_id,
                    entry_order=entry.entry_order,
                    entry_order_source=entry.entry_order_source.value,
                    header_element_fingerprints=[e.element_fingerprint for e in header],
                    responsibility_element_fingerprints=[e.element_fingerprint for e in responsibility],
                    achievement_element_fingerprints=[e.element_fingerprint for e in achievement],
                )
                approved_entries.append(
                    ApprovedCvExperienceEntry(
                        entry_fingerprint=entry_fp,
                        employment_entity_id=entry.employment_entity_id,
                        entry_order=entry.entry_order,
                        entry_order_source=entry.entry_order_source,
                        header_elements=header,
                        responsibility_elements=responsibility,
                        achievement_elements=achievement,
                    )
                )
            section_fp = compute_approved_experience_section_fingerprint(
                member_fingerprints=[e.entry_fingerprint for e in approved_entries]
            )
            approved_sections.append(
                ApprovedCvExperienceSection(
                    section_fingerprint=section_fp, experience_entries=tuple(approved_entries)
                )
            )
        else:
            assert isinstance(section, CvFlatSectionPlan)
            elements = tuple(
                element_by_use_fp[u.planned_fact_use_fingerprint]
                for u in section.planned_fact_uses
                if u.planned_fact_use_fingerprint in element_by_use_fp
            )
            section_fp = compute_approved_flat_section_fingerprint(
                section=section.section.value,
                member_fingerprints=[e.element_fingerprint for e in elements],
            )
            approved_sections.append(
                ApprovedCvFlatSection(
                    section=section.section, section_fingerprint=section_fp, elements=elements
                )
            )

    draft_fingerprint = compute_approved_content_draft_fingerprint(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        section_fingerprints=[s.section_fingerprint for s in approved_sections],
    )
    draft = ApprovedCvContentDraft(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        draft_fingerprint=draft_fingerprint,
        sections=tuple(approved_sections),
    )

    has_pending = any(
        d.disposition_code == FinalDispositionCode.PENDING_USER_DECISION for d in dispositions
    )
    if has_pending:
        status = ApprovalAssemblyStatus.PENDING_APPROVAL
    else:
        status = ApprovalAssemblyStatus.READY
    ready_for_p6 = status == ApprovalAssemblyStatus.READY

    result_fingerprint = compute_approved_content_result_fingerprint(
        status=status,
        ready_for_p6=ready_for_p6,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        p5b_result_fingerprint=p5b_result.result_fingerprint,
        semantic_validation_result_fingerprint=semantic_validation_result.result_fingerprint,
        draft_fingerprint=draft_fingerprint,
        disposition_fingerprints=[d.disposition_fingerprint for d in dispositions],
    )

    return ApprovedCvContentResult(
        result_fingerprint=result_fingerprint,
        status=status,
        ready_for_p6=ready_for_p6,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        p5b_result_fingerprint=p5b_result.result_fingerprint,
        semantic_validation_result_fingerprint=semantic_validation_result.result_fingerprint,
        draft=draft,
        dispositions=dispositions,
        violations=(),
        pending_approval_fingerprints=tuple(
            sorted(item.pending_approval_fingerprint for item in plan.pending_approvals)
        ),
        omitted_fact_fingerprints=tuple(
            sorted(item.omission_fingerprint for item in plan.omitted_facts)
        ),
        conflict_fingerprints=tuple(sorted(item.conflict_fingerprint for item in plan.conflicts)),
    )


def _verdict_to_disposition_code(verdict: ProposalSemanticVerdict) -> FinalDispositionCode:
    from app.services.cv_content_approval_policy import DISPOSITION_BY_NON_APPROVABLE_VERDICT

    return DISPOSITION_BY_NON_APPROVABLE_VERDICT[verdict]
