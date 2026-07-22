"""Controlled LLM proposal generation for Explicit Provenance Stage P5b.

``generate_controlled_cv_content_proposals`` always re-validates the P4
plan via ``validate_cv_content_plan`` and independently re-verifies the P5a
result's fingerprints and freshness -- it never trusts ``plan.plan_status``,
``p5a_result.status``, ``p5a_result.ready_for_p55``, a blocked item's own
``violation_code`` message, or any caller-supplied eligibility/trust
boolean. It performs no database I/O, reads no ``source_reference`` or
legacy record key as identity, creates no DOCX/PDF, and never imports
``app.llm``/``litellm``/Stage 10C. Every proposal it returns always carries
``approval_state=REQUIRES_USER_APPROVAL`` and the result always carries
``ready_for_p55=False``: only Stage P5.5 validates and approves a proposal.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    BlockedGenerationItem,
    CvContentGenerationReplayInput,
    CvContentGenerationResult,
    GenerationContext,
    GenerationFreshnessStatus,
    GenerationViolationCode,
)
from app.schemas.cv_content_plan import (
    CvContentPlan,
    CvContentPlanReplayInput,
    CvFreshnessStatus,
    CvStructuralValidationStatus,
    PlannedFactUse,
)
from app.schemas.cv_content_proposal import (
    PROPOSAL_GENERATION_POLICY_VERSION,
    PROPOSAL_GENERATION_SCHEMA_VERSION,
    BlockedProposalItem,
    ControlledProposalRequest,
    ControlledProposalResponse,
    CvContentProposalGenerationResult,
    CvContentProposalViolation,
    GeneratedCvContentProposal,
    LlmGenerationAttempt,
    LlmGenerationProvenance,
    LlmModelConfiguration,
    ProposalGenerationContext,
    ProposalGenerationStatus,
    ProposalProviderErrorCode,
    ProposalViolationCode,
)
from app.schemas.fact_selection import FactSelectionDecision
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_generation_builder import (
    compute_blocked_generation_item_fingerprint,
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    compute_generation_result_fingerprint,
)
from app.services.cv_content_generation_policy import resolve_payload_shape
from app.services.cv_content_generation_renderer import render_generated_text
from app.services.cv_content_generation_replay import (
    evaluate_generation_freshness,
    replay_input_from_generation_result,
)
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.cv_content_proposal_constraints import (
    build_immutable_constraint_set,
    check_immutable_constraints,
)
from app.services.cv_content_proposal_llm import (
    MAXIMUM_TOTAL_ATTEMPTS,
    ControlledProposalLlmAdapter,
)
from app.services.cv_content_proposal_policy import (
    RETRYABLE_PROVIDER_ERROR_CODES,
    classify_eligibility,
    is_award_fact_type,
)
from app.services.cv_content_proposal_prompt import (
    build_controlled_proposal_request,
    compute_prompt_fingerprint,
    compute_target_role_context_fingerprint,
)
from app.services.truth_fingerprint import canonical_json_bytes


PROPOSAL_CONTEXT_FINGERPRINT_VERSION = "cv-content-proposal-context-v1"
MODEL_CONFIGURATION_FINGERPRINT_VERSION = "cv-content-proposal-model-configuration-v1"
ATTEMPT_FINGERPRINT_VERSION = "cv-content-proposal-attempt-v1"
PROVENANCE_FINGERPRINT_VERSION = "cv-content-proposal-provenance-v1"
PROPOSAL_TEXT_FINGERPRINT_VERSION = "cv-content-proposal-text-v1"
SOURCE_TEXT_FINGERPRINT_VERSION = "cv-content-proposal-source-text-v1"
PROPOSAL_FINGERPRINT_VERSION = "cv-content-proposal-v1"
BLOCKED_PROPOSAL_ITEM_FINGERPRINT_VERSION = "cv-content-proposal-blocked-item-v1"
RESULT_FINGERPRINT_VERSION = "cv-content-proposal-result-v1"
RAW_RESPONSE_FINGERPRINT_VERSION = "cv-content-proposal-raw-response-v1"
STRUCTURED_RESPONSE_FINGERPRINT_VERSION = "cv-content-proposal-structured-response-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_model_configuration_fingerprint(configuration: LlmModelConfiguration) -> str:
    content = {
        "provider": configuration.provider,
        "model_identifier": configuration.model_identifier,
        "temperature": configuration.temperature,
        "top_p": configuration.top_p,
        "max_output_tokens": configuration.max_output_tokens,
        "timeout_seconds": configuration.timeout_seconds,
        "maximum_attempts": configuration.maximum_attempts,
        "response_schema_version": configuration.response_schema_version,
        "retry_policy_version": configuration.retry_policy_version,
    }
    return _fingerprint(MODEL_CONFIGURATION_FINGERPRINT_VERSION, content)


def compute_proposal_generation_context_fingerprint(context: ProposalGenerationContext) -> str:
    content = {
        "locale": context.locale,
        "tone_profile": context.tone_profile,
        "maximum_character_count": context.maximum_character_count,
        "maximum_sentence_count": context.maximum_sentence_count,
        "target_role_context_fingerprint": compute_target_role_context_fingerprint(
            context.target_role_context
        ),
        "prompt_template_version": context.prompt_template_version,
        "generation_policy_version": context.generation_policy_version,
        "immutable_constraint_policy_version": context.immutable_constraint_policy_version,
        "retry_policy_version": context.retry_policy_version,
        "model_configuration_fingerprint": compute_model_configuration_fingerprint(
            context.model_configuration
        ),
    }
    return _fingerprint(PROPOSAL_CONTEXT_FINGERPRINT_VERSION, content)


#: The request fingerprint IS the prompt fingerprint in this implementation:
#: the request object fully and solely determines the rendered prompt, so a
#: second, independently-drifting hash would only invite inconsistency.
def compute_controlled_proposal_request_fingerprint(request: ControlledProposalRequest) -> str:
    return compute_prompt_fingerprint(request)


def compute_llm_attempt_fingerprint(
    *,
    attempt_number: int,
    request_fingerprint: str,
    response_fingerprint: str | None,
    provider_error_code: ProposalProviderErrorCode | None,
    finish_reason: str | None,
) -> str:
    content = {
        "attempt_number": attempt_number,
        "request_fingerprint": request_fingerprint,
        "response_fingerprint": response_fingerprint,
        "provider_error_code": provider_error_code.value if provider_error_code else None,
        "finish_reason": finish_reason,
    }
    return _fingerprint(ATTEMPT_FINGERPRINT_VERSION, content)


def compute_llm_generation_provenance_fingerprint(provenance: LlmGenerationProvenance) -> str:
    return _fingerprint(PROVENANCE_FINGERPRINT_VERSION, provenance.model_dump(mode="json"))


def compute_proposal_text_fingerprint(text: str) -> str:
    return _fingerprint(PROPOSAL_TEXT_FINGERPRINT_VERSION, {"proposal_text": text})


def compute_source_text_fingerprint(text: str) -> str:
    return _fingerprint(SOURCE_TEXT_FINGERPRINT_VERSION, {"source_text": text})


def compute_raw_response_fingerprint(raw_response_text: str) -> str:
    return _fingerprint(RAW_RESPONSE_FINGERPRINT_VERSION, {"raw_response_text": raw_response_text})


def compute_structured_response_fingerprint(
    response: ControlledProposalResponse | None,
) -> str:
    content = response.model_dump(mode="json") if response is not None else None
    return _fingerprint(STRUCTURED_RESPONSE_FINGERPRINT_VERSION, {"structured_response": content})


def compute_generated_proposal_fingerprint(
    *,
    content_plan_fingerprint: str,
    p5a_result_fingerprint: str,
    section_plan_fingerprint: str,
    planned_fact_use_fingerprint: str,
    blocked_generation_item_fingerprint: str,
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
    allowed_operation: str,
    placement_order: int,
    source_text_fingerprint: str,
    proposal_text: str,
    proposal_text_fingerprint: str,
    immutable_constraint_set_fingerprint: str,
    proposal_context_fingerprint: str,
    prompt_fingerprint: str,
    model_configuration_fingerprint: str,
    llm_generation_provenance_fingerprint: str,
) -> str:
    content = {
        "content_plan_fingerprint": content_plan_fingerprint,
        "p5a_result_fingerprint": p5a_result_fingerprint,
        "section_plan_fingerprint": section_plan_fingerprint,
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "blocked_generation_item_fingerprint": blocked_generation_item_fingerprint,
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
        "allowed_operation": str(allowed_operation),
        "placement_order": placement_order,
        "source_text_fingerprint": source_text_fingerprint,
        "proposal_text": proposal_text,
        "proposal_text_fingerprint": proposal_text_fingerprint,
        "immutable_constraint_set_fingerprint": immutable_constraint_set_fingerprint,
        "proposal_context_fingerprint": proposal_context_fingerprint,
        "prompt_fingerprint": prompt_fingerprint,
        "model_configuration_fingerprint": model_configuration_fingerprint,
        "llm_generation_provenance_fingerprint": llm_generation_provenance_fingerprint,
    }
    return _fingerprint(PROPOSAL_FINGERPRINT_VERSION, content)


def compute_blocked_proposal_item_fingerprint(
    *,
    planned_fact_use_fingerprint: str,
    violation_code: ProposalViolationCode,
    provider_error_code: ProposalProviderErrorCode | None = None,
) -> str:
    content = {
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "violation_code": violation_code.value,
        "provider_error_code": provider_error_code.value if provider_error_code else None,
    }
    return _fingerprint(BLOCKED_PROPOSAL_ITEM_FINGERPRINT_VERSION, content)


def compute_proposal_result_fingerprint(
    *,
    status: ProposalGenerationStatus,
    content_plan_fingerprint: str,
    p5a_result_fingerprint: str | None,
    proposal_context_fingerprint: str | None,
    proposal_fingerprints: Sequence[str],
    blocked_proposal_item_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "content_plan_fingerprint": content_plan_fingerprint,
        "p5a_result_fingerprint": p5a_result_fingerprint,
        "proposal_context_fingerprint": proposal_context_fingerprint,
        "proposal_fingerprints": sorted(proposal_fingerprints),
        "blocked_proposal_item_fingerprints": sorted(blocked_proposal_item_fingerprints),
    }
    return _fingerprint(RESULT_FINGERPRINT_VERSION, content)


def _all_uses_with_section_fingerprint(
    plan: CvContentPlan,
) -> list[tuple[PlannedFactUse, str]]:
    pairs: list[tuple[PlannedFactUse, str]] = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                for use in (
                    entry.header_fact_uses
                    + entry.responsibility_fact_uses
                    + entry.achievement_fact_uses
                ):
                    pairs.append((use, section.section_plan_fingerprint))
        else:
            for use in section.planned_fact_uses:
                pairs.append((use, section.section_plan_fingerprint))
    return pairs


def _locate_planned_fact_use(
    plan: CvContentPlan, planned_fact_use_fingerprint: str
) -> tuple[PlannedFactUse, str] | None:
    for use, section_fp in _all_uses_with_section_fingerprint(plan):
        if use.planned_fact_use_fingerprint == planned_fact_use_fingerprint:
            return use, section_fp
    return None


def _classify_unready_plan_violation(
    *,
    structural_status: CvStructuralValidationStatus,
    freshness_status: CvFreshnessStatus,
    has_conflicts: bool,
) -> ProposalViolationCode:
    if structural_status != CvStructuralValidationStatus.VALID:
        return ProposalViolationCode.INPUT_PLAN_STRUCTURALLY_INVALID
    if has_conflicts:
        return ProposalViolationCode.INPUT_PLAN_HAS_CONFLICTS
    if freshness_status == CvFreshnessStatus.STALE:
        return ProposalViolationCode.INPUT_PLAN_STALE
    return ProposalViolationCode.INPUT_PLAN_NOT_READY


def _empty_result(
    *,
    status: ProposalGenerationStatus,
    content_plan_fingerprint: str,
    violations: Sequence[CvContentProposalViolation],
) -> CvContentProposalGenerationResult:
    result_fingerprint = compute_proposal_result_fingerprint(
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=None,
        proposal_context_fingerprint=None,
        proposal_fingerprints=(),
        blocked_proposal_item_fingerprints=(),
    )
    return CvContentProposalGenerationResult(
        result_fingerprint=result_fingerprint,
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        violations=tuple(violations),
    )


def _blocked_proposal_item(
    *,
    item: BlockedGenerationItem,
    violation_code: ProposalViolationCode,
    detail: str,
    provider_error_code: ProposalProviderErrorCode | None = None,
) -> BlockedProposalItem:
    return BlockedProposalItem(
        blocked_proposal_item_fingerprint=compute_blocked_proposal_item_fingerprint(
            planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
            violation_code=violation_code,
            provider_error_code=provider_error_code,
        ),
        planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
        blocked_generation_item_fingerprint=item.blocked_item_fingerprint,
        fact_selection_decision_fingerprint=item.fact_selection_decision_fingerprint,
        person_entity_id=item.person_entity_id,
        entity_id=item.entity_id,
        fact_id=item.fact_id,
        fact_type=item.fact_type,
        fact_revision=item.fact_revision,
        fact_content_fingerprint=item.fact_content_fingerprint,
        target_section=item.target_section,
        target_scope=item.target_scope,
        requested_operation=item.requested_operation,
        allowed_operation=item.allowed_operation,
        violation_code=violation_code,
        provider_error_code=provider_error_code,
        detail=detail,
    )


async def _call_adapter_with_retry(
    *,
    request: ControlledProposalRequest,
    model_configuration: LlmModelConfiguration,
    llm_adapter: ControlledProposalLlmAdapter,
) -> tuple[object, list[LlmGenerationAttempt]]:
    request_fingerprint = compute_controlled_proposal_request_fingerprint(request)
    attempts: list[LlmGenerationAttempt] = []
    max_attempts = min(model_configuration.maximum_attempts, MAXIMUM_TOTAL_ATTEMPTS)
    response = None
    for attempt_number in range(1, max_attempts + 1):
        response = await llm_adapter.generate(
            request=request, model_configuration=model_configuration
        )
        response_fingerprint = None
        if response.structured_response is not None or response.raw_response_text:
            response_fingerprint = compute_raw_response_fingerprint(response.raw_response_text)
        attempt_fingerprint = compute_llm_attempt_fingerprint(
            attempt_number=attempt_number,
            request_fingerprint=request_fingerprint,
            response_fingerprint=response_fingerprint,
            provider_error_code=response.provider_error_code,
            finish_reason=response.finish_reason,
        )
        attempts.append(
            LlmGenerationAttempt(
                attempt_number=attempt_number,
                request_fingerprint=request_fingerprint,
                response_fingerprint=response_fingerprint,
                provider_error_code=response.provider_error_code,
                finish_reason=response.finish_reason,
                attempt_fingerprint=attempt_fingerprint,
            )
        )
        if response.provider_error_code is None:
            break
        if response.provider_error_code not in RETRYABLE_PROVIDER_ERROR_CODES:
            break
        if attempt_number >= max_attempts:
            break
    assert response is not None
    return response, attempts


def _count_sentences(text: str) -> int:
    return len([segment for segment in re.split(r"[.!?]+", text) if segment.strip()])


async def _process_eligible_item(
    item: BlockedGenerationItem,
    *,
    plan: CvContentPlan,
    payloads_by_fact_id: Mapping[UUID, ApprovedFactPayloadSnapshot],
    proposal_context: ProposalGenerationContext,
    generation_policy_version: str,
    content_plan_fingerprint: str,
    p5a_result_fingerprint: str,
    llm_adapter: ControlledProposalLlmAdapter,
) -> GeneratedCvContentProposal | BlockedProposalItem:
    located = _locate_planned_fact_use(plan, item.planned_fact_use_fingerprint)
    if located is None:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.BLOCKED_ITEM_NOT_FOUND_IN_PLAN,
            detail="planned_fact_use_fingerprint was not found in the P4 plan",
        )
    use, section_plan_fingerprint = located

    expected_item_fp = _recompute_blocked_generation_item_fingerprint(item)
    if expected_item_fp != item.blocked_item_fingerprint:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.BLOCKED_ITEM_FINGERPRINT_MISMATCH,
            detail="blocked_item_fingerprint does not match its recomputed value",
        )

    if use.allowed_operation != item.allowed_operation:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.BLOCKED_ITEM_OPERATION_MISMATCH,
            detail="planned fact use's allowed_operation does not match the blocked item",
        )

    if is_award_fact_type(item.fact_type):
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.AWARD_NOT_SUPPORTED,
            detail="AWARD fact types are out of scope for P5b",
        )

    payload = payloads_by_fact_id.get(item.fact_id)
    if payload is None:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PAYLOAD_MISSING,
            detail="no approved fact payload was supplied for this fact_id",
        )

    identity_mismatch = (
        payload.person_entity_id != item.person_entity_id
        or payload.entity_id != item.entity_id
        or payload.fact_type != item.fact_type
        or payload.fact_revision != item.fact_revision
        or payload.fact_content_fingerprint != item.fact_content_fingerprint
    )
    if identity_mismatch:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PAYLOAD_IDENTITY_MISMATCH,
            detail="payload identity fields do not match the blocked item",
        )

    expected_payload_fp = compute_fact_payload_fingerprint(
        person_entity_id=payload.person_entity_id,
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        fact_revision=payload.fact_revision,
        fact_content_fingerprint=payload.fact_content_fingerprint,
        value_json=payload.value_json,
        normalized_value_json=payload.normalized_value_json,
        generation_policy_version=generation_policy_version,
    )
    if expected_payload_fp != payload.payload_fingerprint:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PAYLOAD_FINGERPRINT_MISMATCH,
            detail="payload_fingerprint does not match its recomputed value",
        )

    shape = resolve_payload_shape(item.fact_type, payload.value_json)
    if not shape.is_valid:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PAYLOAD_SHAPE_INVALID,
            detail=shape.detail,
        )

    source_text = render_generated_text(
        fact_type=item.fact_type, operation=TransformationOperation.EXACT_COPY, shape=shape
    )
    constraint_set = build_immutable_constraint_set(
        fact_id=item.fact_id,
        fact_type=item.fact_type,
        source_text=source_text,
        structured_fields=shape.structured_fields,
    )

    request = build_controlled_proposal_request(
        operation=item.allowed_operation,
        fact_type=item.fact_type,
        target_section=use.target_section,
        target_scope=use.target_scope,
        employment_scope_entity_id=use.employment_scope_entity_id,
        source_fact_id=item.fact_id,
        source_text=source_text,
        immutable_constraints=constraint_set,
        target_role_context=proposal_context.target_role_context,
        tone_profile=proposal_context.tone_profile,
        maximum_character_count=proposal_context.maximum_character_count,
        maximum_sentence_count=proposal_context.maximum_sentence_count,
    )
    prompt_fingerprint = compute_prompt_fingerprint(request)
    model_configuration_fingerprint = compute_model_configuration_fingerprint(
        proposal_context.model_configuration
    )
    proposal_context_fingerprint = compute_proposal_generation_context_fingerprint(
        proposal_context
    )
    target_role_context_fingerprint = compute_target_role_context_fingerprint(
        proposal_context.target_role_context
    )

    response, attempts = await _call_adapter_with_retry(
        request=request,
        model_configuration=proposal_context.model_configuration,
        llm_adapter=llm_adapter,
    )

    if response.provider_error_code is not None:
        violation_code = (
            ProposalViolationCode.RETRY_EXHAUSTED
            if len(attempts) > 1
            else ProposalViolationCode.PROVIDER_ERROR
        )
        return _blocked_proposal_item(
            item=item,
            violation_code=violation_code,
            detail=f"provider error: {response.provider_error_code.value}",
            provider_error_code=response.provider_error_code,
        )

    structured = response.structured_response
    if structured is None:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.RESPONSE_SCHEMA_MISMATCH,
            detail="adapter returned no structured_response",
            provider_error_code=ProposalProviderErrorCode.RESPONSE_SCHEMA_MISMATCH,
        )

    if (
        response.provider != proposal_context.model_configuration.provider
        or response.model_identifier != proposal_context.model_configuration.model_identifier
    ):
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.MODEL_CONFIGURATION_FINGERPRINT_MISMATCH,
            detail="adapter response provider/model does not match the configured model",
        )

    if structured.operation != item.allowed_operation:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.RESPONSE_OPERATION_MISMATCH,
            detail="response.operation does not match the eligible item's allowed_operation",
        )
    if structured.source_fact_id != item.fact_id:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.RESPONSE_FACT_ID_MISMATCH,
            detail="response.source_fact_id does not match the eligible item's fact_id",
        )

    proposal_text = structured.proposal_text
    if not proposal_text.strip():
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.EMPTY_PROPOSAL_TEXT,
            detail="proposal_text is blank",
        )
    if len(proposal_text) > proposal_context.maximum_character_count:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PROPOSAL_CHARACTER_LIMIT_EXCEEDED,
            detail="proposal_text exceeds maximum_character_count",
        )
    if _count_sentences(proposal_text) > proposal_context.maximum_sentence_count:
        return _blocked_proposal_item(
            item=item,
            violation_code=ProposalViolationCode.PROPOSAL_SENTENCE_LIMIT_EXCEEDED,
            detail="proposal_text exceeds maximum_sentence_count",
        )

    constraint_violation = check_immutable_constraints(constraint_set, proposal_text)
    if constraint_violation is not None:
        code, detail = constraint_violation
        return _blocked_proposal_item(item=item, violation_code=code, detail=detail)

    raw_response_fingerprint = compute_raw_response_fingerprint(response.raw_response_text)
    structured_response_fingerprint = compute_structured_response_fingerprint(structured)
    provenance = LlmGenerationProvenance(
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        system_prompt_template_version=request.prompt_template_version,
        user_prompt_template_version=request.prompt_template_version,
        response_schema_version=request.response_schema_version,
        proposal_context_fingerprint=proposal_context_fingerprint,
        target_role_context_fingerprint=target_role_context_fingerprint,
        immutable_constraint_set_fingerprint=constraint_set.immutable_constraint_set_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        provider=response.provider,
        model_identifier=response.model_identifier,
        model_configuration_fingerprint=model_configuration_fingerprint,
        attempt_count=len(attempts),
        attempt_fingerprints=tuple(a.attempt_fingerprint for a in attempts),
        raw_response_fingerprint=raw_response_fingerprint,
        structured_response_fingerprint=structured_response_fingerprint,
        source_payload_fingerprint=payload.payload_fingerprint,
        source_planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
        p5a_result_fingerprint=p5a_result_fingerprint,
    )
    provenance_fingerprint = compute_llm_generation_provenance_fingerprint(provenance)
    source_text_fingerprint = compute_source_text_fingerprint(source_text)
    proposal_text_fingerprint = compute_proposal_text_fingerprint(proposal_text)

    proposal_fingerprint = compute_generated_proposal_fingerprint(
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result_fingerprint,
        section_plan_fingerprint=section_plan_fingerprint,
        planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
        blocked_generation_item_fingerprint=item.blocked_item_fingerprint,
        fact_selection_decision_fingerprint=item.fact_selection_decision_fingerprint,
        person_entity_id=item.person_entity_id,
        entity_id=item.entity_id,
        fact_id=item.fact_id,
        fact_type=item.fact_type,
        fact_revision=item.fact_revision,
        fact_content_fingerprint=item.fact_content_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        target_section=use.target_section.value,
        target_scope=use.target_scope,
        employment_scope_entity_id=use.employment_scope_entity_id,
        allowed_operation=item.allowed_operation.value,
        placement_order=use.placement_order,
        source_text_fingerprint=source_text_fingerprint,
        proposal_text=proposal_text,
        proposal_text_fingerprint=proposal_text_fingerprint,
        immutable_constraint_set_fingerprint=constraint_set.immutable_constraint_set_fingerprint,
        proposal_context_fingerprint=proposal_context_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        model_configuration_fingerprint=model_configuration_fingerprint,
        llm_generation_provenance_fingerprint=provenance_fingerprint,
    )

    return GeneratedCvContentProposal(
        proposal_fingerprint=proposal_fingerprint,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result_fingerprint,
        section_plan_fingerprint=section_plan_fingerprint,
        planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
        blocked_generation_item_fingerprint=item.blocked_item_fingerprint,
        fact_selection_decision_fingerprint=item.fact_selection_decision_fingerprint,
        person_entity_id=item.person_entity_id,
        entity_id=item.entity_id,
        fact_id=item.fact_id,
        fact_type=item.fact_type,
        fact_revision=item.fact_revision,
        fact_content_fingerprint=item.fact_content_fingerprint,
        payload_fingerprint=payload.payload_fingerprint,
        target_section=use.target_section,
        target_scope=use.target_scope,
        employment_scope_entity_id=use.employment_scope_entity_id,
        allowed_operation=item.allowed_operation,
        placement_order=use.placement_order,
        source_text_fingerprint=source_text_fingerprint,
        proposal_text=proposal_text,
        proposal_text_fingerprint=proposal_text_fingerprint,
        immutable_constraint_set_fingerprint=constraint_set.immutable_constraint_set_fingerprint,
        proposal_context_fingerprint=proposal_context_fingerprint,
        prompt_template_version=request.prompt_template_version,
        prompt_fingerprint=prompt_fingerprint,
        model_configuration_fingerprint=model_configuration_fingerprint,
        llm_generation_provenance=provenance,
    )


def _recompute_blocked_generation_item_fingerprint(item: BlockedGenerationItem) -> str:
    # Delegates to P5a's own formula so a tampered ``blocked_item_fingerprint``
    # is always caught, without redeclaring the fingerprint algorithm here.
    return compute_blocked_generation_item_fingerprint(
        planned_fact_use_fingerprint=item.planned_fact_use_fingerprint,
        violation_code=item.violation_code,
    )


async def generate_controlled_cv_content_proposals(
    *,
    plan: CvContentPlan,
    p5a_result: CvContentGenerationResult,
    p5a_generation_context: GenerationContext,
    decisions_by_fingerprint: Mapping[str, FactSelectionDecision],
    entity_types: Mapping[UUID, EntityType],
    current_plan_replay_input: CvContentPlanReplayInput | None,
    current_p5a_replay_input: CvContentGenerationReplayInput | None,
    fact_payloads: ApprovedFactPayloadSet,
    proposal_context: ProposalGenerationContext,
    llm_adapter: ControlledProposalLlmAdapter,
) -> CvContentProposalGenerationResult:
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
        return _empty_result(
            status=ProposalGenerationStatus.BLOCKED,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=violation_code,
                    detail=f"P4 plan is not p5_ready: {violation_code.value}",
                ),
            ),
        )

    if p5a_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _empty_result(
            status=ProposalGenerationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=ProposalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail=(
                        "p5a_result.content_plan_fingerprint does not match "
                        "plan.content_plan_fingerprint"
                    ),
                ),
            ),
        )

    expected_payload_set_fp = compute_fact_payload_set_fingerprint(fact_payloads.payloads)
    if (
        expected_payload_set_fp != fact_payloads.payload_set_fingerprint
        or p5a_result.payload_set_fingerprint != fact_payloads.payload_set_fingerprint
    ):
        return _empty_result(
            status=ProposalGenerationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=ProposalViolationCode.PAYLOAD_SET_FINGERPRINT_MISMATCH,
                    detail="fact_payloads.payload_set_fingerprint is inconsistent",
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
        return _empty_result(
            status=ProposalGenerationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=ProposalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5a_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )

    if current_p5a_replay_input is None:
        return _empty_result(
            status=ProposalGenerationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=ProposalViolationCode.INPUT_P5A_RESULT_STALE,
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
    p5a_freshness = evaluate_generation_freshness(
        expected_p5a_replay_input, current_p5a_replay_input
    )
    if p5a_freshness.freshness_status != GenerationFreshnessStatus.FRESH:
        return _empty_result(
            status=ProposalGenerationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentProposalViolation(
                    violation_code=ProposalViolationCode.INPUT_P5A_RESULT_STALE,
                    detail=f"P5a result is not fresh: stale_fields={p5a_freshness.stale_fields}",
                ),
            ),
        )

    generation_policy_version = p5a_generation_context.generation_policy_version
    payloads_by_fact_id = {p.fact_id: p for p in fact_payloads.payloads}
    content_plan_fingerprint = plan.content_plan_fingerprint
    p5a_result_fingerprint = p5a_result.result_fingerprint

    eligible_items: list[BlockedGenerationItem] = []
    non_eligible_fingerprints: list[str] = []
    for item in p5a_result.blocked_items:
        if item.violation_code != GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A:
            non_eligible_fingerprints.append(item.blocked_item_fingerprint)
            continue
        if classify_eligibility(item.allowed_operation) is not None:
            non_eligible_fingerprints.append(item.blocked_item_fingerprint)
            continue
        eligible_items.append(item)

    deterministic_element_fingerprints: list[str] = []
    if p5a_result.draft is not None:
        for section in p5a_result.draft.generated_sections:
            if section.kind == "EXPERIENCE":
                for entry in section.generated_experience_entries:
                    for element in (
                        entry.header_elements
                        + entry.responsibility_elements
                        + entry.achievement_elements
                    ):
                        deterministic_element_fingerprints.append(
                            element.generated_element_fingerprint
                        )
            else:
                for element in section.generated_elements:
                    deterministic_element_fingerprints.append(
                        element.generated_element_fingerprint
                    )

    proposals: list[GeneratedCvContentProposal] = []
    blocked_proposal_items: list[BlockedProposalItem] = []
    for item in eligible_items:
        outcome = await _process_eligible_item(
            item,
            plan=plan,
            payloads_by_fact_id=payloads_by_fact_id,
            proposal_context=proposal_context,
            generation_policy_version=generation_policy_version,
            content_plan_fingerprint=content_plan_fingerprint,
            p5a_result_fingerprint=p5a_result_fingerprint,
            llm_adapter=llm_adapter,
        )
        if isinstance(outcome, GeneratedCvContentProposal):
            proposals.append(outcome)
        else:
            blocked_proposal_items.append(outcome)

    proposal_context_fingerprint = compute_proposal_generation_context_fingerprint(
        proposal_context
    )

    violations: tuple[CvContentProposalViolation, ...] = ()
    if not eligible_items:
        violations = (
            CvContentProposalViolation(
                violation_code=ProposalViolationCode.NO_ELIGIBLE_ITEMS,
                detail="the P5a result carries no eligible blocked items for P5b",
            ),
        )

    has_provider_error = any(b.provider_error_code is not None for b in blocked_proposal_items)
    if has_provider_error:
        status = ProposalGenerationStatus.PROVIDER_ERROR
    elif blocked_proposal_items:
        status = ProposalGenerationStatus.BLOCKED
    elif proposals:
        status = ProposalGenerationStatus.PROPOSED
    else:
        status = ProposalGenerationStatus.BLOCKED

    result_fingerprint = compute_proposal_result_fingerprint(
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result_fingerprint,
        proposal_context_fingerprint=proposal_context_fingerprint,
        proposal_fingerprints=[p.proposal_fingerprint for p in proposals],
        blocked_proposal_item_fingerprints=[
            b.blocked_proposal_item_fingerprint for b in blocked_proposal_items
        ],
    )

    return CvContentProposalGenerationResult(
        result_fingerprint=result_fingerprint,
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result_fingerprint,
        proposal_context_fingerprint=proposal_context_fingerprint,
        proposals=tuple(proposals),
        blocked_proposal_items=tuple(blocked_proposal_items),
        violations=violations,
        non_eligible_blocked_item_fingerprints=tuple(sorted(non_eligible_fingerprints)),
        deterministic_element_fingerprints=tuple(sorted(deterministic_element_fingerprints)),
    )
