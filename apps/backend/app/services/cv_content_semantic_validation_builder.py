"""Step 1 of Explicit Provenance Stage P5.5: semantic proposal validation.

``validate_cv_content_proposals`` always re-validates the P4 plan, the P5a
generation result, and the P5b proposal result -- it never trusts a
caller-supplied ``p5_ready``, freshness, or "trusted fingerprint" value. It
re-runs every P5b deterministic guardrail before ever calling the injected
``ControlledProposalSemanticValidator``, makes exactly one validator call
per P5b proposal (with at most one retry, only for TIMEOUT/TRANSPORT_ERROR),
and never approves a proposal or changes its text. It performs no database
I/O, creates no DOCX/PDF, and never imports ``app.llm``/``litellm``/Stage
10C.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.schemas.cv_content_approval import (
    SEMANTIC_VALIDATION_RESPONSE_SCHEMA_VERSION,
    SEMANTIC_VALIDATION_SCHEMA_VERSION,
    BlockedSemanticValidationItem,
    ControlledSemanticValidationRequest,
    CvContentApprovalViolation,
    CvContentApprovalViolationCode,
    ProposalSemanticValidationItem,
    ProposalSemanticValidationResult,
    ProposalSemanticVerdict,
    SemanticGuardrailViolationCode,
    SemanticValidationAttempt,
    SemanticValidationContext,
    SemanticValidationProvenance,
    SemanticValidationStatus,
    SemanticValidatorAdapterResponse,
)
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    BlockedGenerationItem,
    CvContentGenerationReplayInput,
    CvContentGenerationResult,
    GenerationContext,
    GenerationFreshnessStatus,
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
    CvContentProposalGenerationResult,
    CvContentProposalReplayInput,
    GeneratedCvContentProposal,
    ProposalFreshnessStatus,
    ProposalGenerationContext,
)
from app.schemas.fact_selection import FactSelectionDecision
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_generation_builder import (
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
from app.services.cv_content_proposal_builder import (
    compute_generated_proposal_fingerprint,
    compute_llm_generation_provenance_fingerprint,
    compute_model_configuration_fingerprint,
    compute_proposal_result_fingerprint,
    compute_proposal_text_fingerprint,
    compute_source_text_fingerprint,
)
from app.services.cv_content_proposal_constraints import (
    build_immutable_constraint_set,
    check_immutable_constraints,
)
from app.services.cv_content_proposal_policy import is_award_fact_type
from app.services.cv_content_proposal_prompt import (
    build_controlled_proposal_request,
    compute_prompt_fingerprint,
)
from app.services.cv_content_proposal_replay import (
    evaluate_proposal_freshness,
    replay_input_from_proposal_result,
)
from app.services.cv_content_semantic_validation_prompt import (
    build_controlled_semantic_validation_request,
    compute_semantic_validation_prompt_fingerprint,
)
from app.services.cv_content_semantic_validator import (
    MAXIMUM_TOTAL_ATTEMPTS,
    RETRYABLE_PROVIDER_ERROR_CODES,
    ControlledProposalSemanticValidator,
)
from app.services.truth_fingerprint import canonical_json_bytes


VALIDATION_CONTEXT_FINGERPRINT_VERSION = "cv-content-semantic-validation-context-v1"
MODEL_CONFIGURATION_FINGERPRINT_VERSION = "cv-content-semantic-validation-model-configuration-v1"
ATTEMPT_FINGERPRINT_VERSION = "cv-content-semantic-validation-attempt-v1"
PROVENANCE_FINGERPRINT_VERSION = "cv-content-semantic-validation-provenance-v1"
ITEM_FINGERPRINT_VERSION = "cv-content-semantic-validation-item-v1"
BLOCKED_ITEM_FINGERPRINT_VERSION = "cv-content-semantic-validation-blocked-item-v1"
RESULT_FINGERPRINT_VERSION = "cv-content-semantic-validation-result-v1"
RAW_RESPONSE_FINGERPRINT_VERSION = "cv-content-semantic-validation-raw-response-v1"
STRUCTURED_RESPONSE_FINGERPRINT_VERSION = "cv-content-semantic-validation-structured-response-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_semantic_validator_model_configuration_fingerprint(
    configuration,
) -> str:
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


def compute_semantic_validation_context_fingerprint(context: SemanticValidationContext) -> str:
    content = {
        "locale": context.locale,
        "validation_prompt_version": context.validation_prompt_version,
        "validation_policy_version": context.validation_policy_version,
        "retry_policy_version": context.retry_policy_version,
        "validator_model_configuration_fingerprint": (
            compute_semantic_validator_model_configuration_fingerprint(
                context.validator_model_configuration
            )
        ),
    }
    return _fingerprint(VALIDATION_CONTEXT_FINGERPRINT_VERSION, content)


#: The request fingerprint IS the prompt fingerprint in this implementation:
#: the request object fully and solely determines the rendered prompt.
def compute_semantic_validation_request_fingerprint(
    request: ControlledSemanticValidationRequest,
) -> str:
    return compute_semantic_validation_prompt_fingerprint(request)


def compute_semantic_validation_attempt_fingerprint(
    *,
    attempt_number: int,
    request_fingerprint: str,
    response_fingerprint: str | None,
    provider_error_code,
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


def compute_semantic_validation_provenance_fingerprint(
    provenance: SemanticValidationProvenance,
) -> str:
    return _fingerprint(PROVENANCE_FINGERPRINT_VERSION, provenance.model_dump(mode="json"))


def _compute_raw_response_fingerprint(raw_response_text: str) -> str:
    return _fingerprint(RAW_RESPONSE_FINGERPRINT_VERSION, {"raw_response_text": raw_response_text})


def _compute_structured_response_fingerprint(response) -> str:
    content = response.model_dump(mode="json") if response is not None else None
    return _fingerprint(STRUCTURED_RESPONSE_FINGERPRINT_VERSION, {"structured_response": content})


def compute_semantic_validation_item_fingerprint(
    *,
    planned_fact_use_fingerprint: str,
    proposal_fingerprint: str,
    proposal_text_fingerprint: str,
    fact_selection_decision_fingerprint: str,
    person_entity_id: UUID,
    entity_id: UUID,
    fact_id: UUID,
    fact_type: str,
    fact_revision: int,
    fact_content_fingerprint: str,
    target_section: str,
    target_scope: str,
    employment_scope_entity_id: UUID | None,
    allowed_operation: str,
    placement_order: int,
    verdict: str,
    detected_violation_codes: Sequence[str],
    provenance_fingerprint: str,
) -> str:
    content = {
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "proposal_fingerprint": proposal_fingerprint,
        "proposal_text_fingerprint": proposal_text_fingerprint,
        "fact_selection_decision_fingerprint": fact_selection_decision_fingerprint,
        "person_entity_id": str(person_entity_id),
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_type": fact_type,
        "fact_revision": fact_revision,
        "fact_content_fingerprint": fact_content_fingerprint,
        "target_section": str(target_section),
        "target_scope": target_scope,
        "employment_scope_entity_id": (
            str(employment_scope_entity_id) if employment_scope_entity_id is not None else None
        ),
        "allowed_operation": str(allowed_operation),
        "placement_order": placement_order,
        "verdict": str(verdict),
        "detected_violation_codes": sorted(str(code) for code in detected_violation_codes),
        "provenance_fingerprint": provenance_fingerprint,
    }
    return _fingerprint(ITEM_FINGERPRINT_VERSION, content)


def compute_blocked_semantic_validation_item_fingerprint(
    *, planned_fact_use_fingerprint: str, violation_code: SemanticGuardrailViolationCode
) -> str:
    content = {
        "planned_fact_use_fingerprint": planned_fact_use_fingerprint,
        "violation_code": violation_code.value,
    }
    return _fingerprint(BLOCKED_ITEM_FINGERPRINT_VERSION, content)


def compute_semantic_validation_result_fingerprint(
    *,
    status: SemanticValidationStatus,
    content_plan_fingerprint: str,
    p5a_result_fingerprint: str | None,
    p5b_result_fingerprint: str | None,
    validation_context_fingerprint: str | None,
    validation_item_fingerprints: Sequence[str],
    blocked_item_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "content_plan_fingerprint": content_plan_fingerprint,
        "p5a_result_fingerprint": p5a_result_fingerprint,
        "p5b_result_fingerprint": p5b_result_fingerprint,
        "validation_context_fingerprint": validation_context_fingerprint,
        "validation_item_fingerprints": sorted(validation_item_fingerprints),
        "blocked_item_fingerprints": sorted(blocked_item_fingerprints),
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
) -> PlannedFactUse | None:
    for use, _section_fp in _all_uses_with_section_fingerprint(plan):
        if use.planned_fact_use_fingerprint == planned_fact_use_fingerprint:
            return use
    return None


def _locate_blocked_generation_item(
    p5a_result: CvContentGenerationResult, blocked_generation_item_fingerprint: str
) -> BlockedGenerationItem | None:
    for item in p5a_result.blocked_items:
        if item.blocked_item_fingerprint == blocked_generation_item_fingerprint:
            return item
    return None


@dataclass(frozen=True)
class _PreparedProposal:
    use: PlannedFactUse
    payload: ApprovedFactPayloadSnapshot
    source_text: str
    constraint_set: object


def _prepare_and_recheck_proposal(
    proposal: GeneratedCvContentProposal,
    *,
    plan: CvContentPlan,
    p5a_result: CvContentGenerationResult,
    payloads_by_fact_id: Mapping[UUID, ApprovedFactPayloadSnapshot],
    proposal_generation_context: ProposalGenerationContext,
    generation_policy_version: str,
) -> _PreparedProposal | tuple[SemanticGuardrailViolationCode, str]:
    use = _locate_planned_fact_use(plan, proposal.planned_fact_use_fingerprint)
    if use is None:
        return (
            SemanticGuardrailViolationCode.PLANNED_FACT_USE_NOT_FOUND,
            "planned_fact_use_fingerprint was not found in the P4 plan",
        )

    blocked_item = _locate_blocked_generation_item(
        p5a_result, proposal.blocked_generation_item_fingerprint
    )
    if blocked_item is None:
        return (
            SemanticGuardrailViolationCode.BLOCKED_GENERATION_ITEM_FINGERPRINT_MISMATCH,
            "blocked_generation_item_fingerprint was not found in the P5a result",
        )

    if is_award_fact_type(proposal.fact_type):
        return (
            SemanticGuardrailViolationCode.AWARD_NOT_SUPPORTED,
            "AWARD fact types are out of scope for P5.5",
        )

    payload = payloads_by_fact_id.get(proposal.fact_id)
    if payload is None:
        return (
            SemanticGuardrailViolationCode.PAYLOAD_MISSING,
            "no approved fact payload was supplied for this fact_id",
        )

    identity_mismatch = (
        payload.person_entity_id != proposal.person_entity_id
        or payload.entity_id != proposal.entity_id
        or payload.fact_type != proposal.fact_type
        or payload.fact_revision != proposal.fact_revision
        or payload.fact_content_fingerprint != proposal.fact_content_fingerprint
    )
    if identity_mismatch:
        return (
            SemanticGuardrailViolationCode.PAYLOAD_IDENTITY_MISMATCH,
            "payload identity fields do not match the proposal",
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
    if expected_payload_fp != payload.payload_fingerprint or expected_payload_fp != proposal.payload_fingerprint:
        return (
            SemanticGuardrailViolationCode.PAYLOAD_FINGERPRINT_MISMATCH,
            "payload_fingerprint does not match its recomputed value",
        )

    shape = resolve_payload_shape(proposal.fact_type, payload.value_json)
    if not shape.is_valid:
        return (
            SemanticGuardrailViolationCode.PAYLOAD_SHAPE_INVALID,
            f"payload shape is no longer valid: {shape.detail}",
        )

    source_text = render_generated_text(
        fact_type=proposal.fact_type, operation=TransformationOperation.EXACT_COPY, shape=shape
    )
    source_text_fp = compute_source_text_fingerprint(source_text)
    if source_text_fp != proposal.source_text_fingerprint:
        return (
            SemanticGuardrailViolationCode.SOURCE_TEXT_FINGERPRINT_MISMATCH,
            "source_text_fingerprint does not match its recomputed value",
        )

    constraint_set = build_immutable_constraint_set(
        fact_id=proposal.fact_id,
        fact_type=proposal.fact_type,
        source_text=source_text,
        structured_fields=shape.structured_fields,
    )
    if constraint_set.immutable_constraint_set_fingerprint != proposal.immutable_constraint_set_fingerprint:
        return (
            SemanticGuardrailViolationCode.IMMUTABLE_CONSTRAINT_VIOLATION,
            "immutable_constraint_set_fingerprint does not match its recomputed value",
        )

    constraint_violation = check_immutable_constraints(constraint_set, proposal.proposal_text)
    if constraint_violation is not None:
        code, detail = constraint_violation
        return (SemanticGuardrailViolationCode.IMMUTABLE_CONSTRAINT_VIOLATION, f"{code.value}: {detail}")

    proposal_text_fp = compute_proposal_text_fingerprint(proposal.proposal_text)
    if proposal_text_fp != proposal.proposal_text_fingerprint:
        return (
            SemanticGuardrailViolationCode.PROPOSAL_TEXT_FINGERPRINT_MISMATCH,
            "proposal_text_fingerprint does not match its recomputed value",
        )

    if proposal.allowed_operation != use.allowed_operation or proposal.allowed_operation != blocked_item.allowed_operation:
        return (
            SemanticGuardrailViolationCode.OPERATION_MISMATCH,
            "allowed_operation is inconsistent across the proposal/planned use/blocked item",
        )
    if proposal.target_section != use.target_section:
        return (
            SemanticGuardrailViolationCode.TARGET_SECTION_MISMATCH,
            "target_section does not match the planned fact use",
        )
    if proposal.target_scope != use.target_scope:
        return (
            SemanticGuardrailViolationCode.TARGET_SCOPE_MISMATCH,
            "target_scope does not match the planned fact use",
        )
    if proposal.employment_scope_entity_id != use.employment_scope_entity_id:
        return (
            SemanticGuardrailViolationCode.EMPLOYMENT_SCOPE_MISMATCH,
            "employment_scope_entity_id does not match the planned fact use",
        )
    if proposal.placement_order != use.placement_order:
        return (
            SemanticGuardrailViolationCode.PLACEMENT_ORDER_MISMATCH,
            "placement_order does not match the planned fact use",
        )

    request = build_controlled_proposal_request(
        operation=proposal.allowed_operation,
        fact_type=proposal.fact_type,
        target_section=use.target_section,
        target_scope=use.target_scope,
        employment_scope_entity_id=use.employment_scope_entity_id,
        source_fact_id=proposal.fact_id,
        source_text=source_text,
        immutable_constraints=constraint_set,
        target_role_context=proposal_generation_context.target_role_context,
        tone_profile=proposal_generation_context.tone_profile,
        maximum_character_count=proposal_generation_context.maximum_character_count,
        maximum_sentence_count=proposal_generation_context.maximum_sentence_count,
    )
    prompt_fingerprint = compute_prompt_fingerprint(request)
    if prompt_fingerprint != proposal.prompt_fingerprint:
        return (
            SemanticGuardrailViolationCode.PROMPT_FINGERPRINT_MISMATCH,
            "prompt_fingerprint does not match its recomputed value",
        )

    model_configuration_fingerprint = compute_model_configuration_fingerprint(
        proposal_generation_context.model_configuration
    )
    if model_configuration_fingerprint != proposal.model_configuration_fingerprint:
        return (
            SemanticGuardrailViolationCode.MODEL_CONFIGURATION_FINGERPRINT_MISMATCH,
            "model_configuration_fingerprint does not match its recomputed value",
        )

    llm_provenance_fingerprint = compute_llm_generation_provenance_fingerprint(
        proposal.llm_generation_provenance
    )
    expected_proposal_fp = compute_generated_proposal_fingerprint(
        content_plan_fingerprint=proposal.content_plan_fingerprint,
        p5a_result_fingerprint=proposal.p5a_result_fingerprint,
        section_plan_fingerprint=proposal.section_plan_fingerprint,
        planned_fact_use_fingerprint=proposal.planned_fact_use_fingerprint,
        blocked_generation_item_fingerprint=proposal.blocked_generation_item_fingerprint,
        fact_selection_decision_fingerprint=proposal.fact_selection_decision_fingerprint,
        person_entity_id=proposal.person_entity_id,
        entity_id=proposal.entity_id,
        fact_id=proposal.fact_id,
        fact_type=proposal.fact_type,
        fact_revision=proposal.fact_revision,
        fact_content_fingerprint=proposal.fact_content_fingerprint,
        payload_fingerprint=proposal.payload_fingerprint,
        target_section=proposal.target_section.value,
        target_scope=proposal.target_scope,
        employment_scope_entity_id=proposal.employment_scope_entity_id,
        allowed_operation=proposal.allowed_operation.value,
        placement_order=proposal.placement_order,
        source_text_fingerprint=proposal.source_text_fingerprint,
        proposal_text=proposal.proposal_text,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        immutable_constraint_set_fingerprint=proposal.immutable_constraint_set_fingerprint,
        proposal_context_fingerprint=proposal.proposal_context_fingerprint,
        prompt_fingerprint=proposal.prompt_fingerprint,
        model_configuration_fingerprint=proposal.model_configuration_fingerprint,
        llm_generation_provenance_fingerprint=llm_provenance_fingerprint,
    )
    if expected_proposal_fp != proposal.proposal_fingerprint:
        return (
            SemanticGuardrailViolationCode.PROPOSAL_FINGERPRINT_MISMATCH,
            "proposal_fingerprint does not match its recomputed value",
        )

    return _PreparedProposal(use=use, payload=payload, source_text=source_text, constraint_set=constraint_set)


async def _call_validator_with_retry(
    *,
    request: ControlledSemanticValidationRequest,
    validation_context: SemanticValidationContext,
    semantic_validator: ControlledProposalSemanticValidator,
) -> tuple[SemanticValidatorAdapterResponse, list]:
    request_fingerprint = compute_semantic_validation_request_fingerprint(request)
    attempts: list[SemanticValidationAttempt] = []
    max_attempts = min(
        validation_context.validator_model_configuration.maximum_attempts, MAXIMUM_TOTAL_ATTEMPTS
    )
    response: SemanticValidatorAdapterResponse | None = None
    for attempt_number in range(1, max_attempts + 1):
        response = await semantic_validator.validate(
            request=request,
            model_configuration=validation_context.validator_model_configuration,
        )
        response_fingerprint = None
        if response.structured_response is not None or response.raw_response_text:
            response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
        attempt_fingerprint = compute_semantic_validation_attempt_fingerprint(
            attempt_number=attempt_number,
            request_fingerprint=request_fingerprint,
            response_fingerprint=response_fingerprint,
            provider_error_code=response.provider_error_code,
            finish_reason=response.finish_reason,
        )
        attempts.append(
            SemanticValidationAttempt(
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


async def _validate_one_proposal(
    proposal: GeneratedCvContentProposal,
    *,
    plan: CvContentPlan,
    p5a_result: CvContentGenerationResult,
    p5b_result: CvContentProposalGenerationResult,
    payloads_by_fact_id: Mapping[UUID, ApprovedFactPayloadSnapshot],
    proposal_generation_context: ProposalGenerationContext,
    generation_policy_version: str,
    validation_context: SemanticValidationContext,
    validation_context_fingerprint: str,
    semantic_validator: ControlledProposalSemanticValidator,
) -> ProposalSemanticValidationItem | BlockedSemanticValidationItem:
    def _blocked(code: SemanticGuardrailViolationCode, detail: str) -> BlockedSemanticValidationItem:
        return BlockedSemanticValidationItem(
            blocked_semantic_validation_item_fingerprint=compute_blocked_semantic_validation_item_fingerprint(
                planned_fact_use_fingerprint=proposal.planned_fact_use_fingerprint,
                violation_code=code,
            ),
            planned_fact_use_fingerprint=proposal.planned_fact_use_fingerprint,
            proposal_fingerprint=proposal.proposal_fingerprint,
            fact_selection_decision_fingerprint=proposal.fact_selection_decision_fingerprint,
            person_entity_id=proposal.person_entity_id,
            entity_id=proposal.entity_id,
            fact_id=proposal.fact_id,
            fact_type=proposal.fact_type,
            fact_revision=proposal.fact_revision,
            fact_content_fingerprint=proposal.fact_content_fingerprint,
            target_section=proposal.target_section,
            target_scope=proposal.target_scope,
            requested_operation=proposal.allowed_operation,
            allowed_operation=proposal.allowed_operation,
            violation_code=code,
            detail=detail,
        )

    prepared = _prepare_and_recheck_proposal(
        proposal,
        plan=plan,
        p5a_result=p5a_result,
        payloads_by_fact_id=payloads_by_fact_id,
        proposal_generation_context=proposal_generation_context,
        generation_policy_version=generation_policy_version,
    )
    if not isinstance(prepared, _PreparedProposal):
        code, detail = prepared
        return _blocked(code, detail)

    request = build_controlled_semantic_validation_request(
        operation=proposal.allowed_operation,
        fact_type=proposal.fact_type,
        target_section=prepared.use.target_section,
        target_scope=prepared.use.target_scope,
        employment_scope_entity_id=prepared.use.employment_scope_entity_id,
        source_fact_id=proposal.fact_id,
        source_text=prepared.source_text,
        proposal_text=proposal.proposal_text,
        proposal_fingerprint=proposal.proposal_fingerprint,
        immutable_constraints=prepared.constraint_set,
        target_role_context=proposal_generation_context.target_role_context,
    )
    request_fingerprint = compute_semantic_validation_request_fingerprint(request)

    response, attempts = await _call_validator_with_retry(
        request=request,
        validation_context=validation_context,
        semantic_validator=semantic_validator,
    )

    structured = response.structured_response
    if response.provider_error_code is not None:
        verdict = ProposalSemanticVerdict.PROVIDER_ERROR
        detected_violation_codes: tuple = ()
    elif structured is None:
        verdict = ProposalSemanticVerdict.INVALID_INPUT
        detected_violation_codes = ()
    elif (
        response.provider != validation_context.validator_model_configuration.provider
        or response.model_identifier
        != validation_context.validator_model_configuration.model_identifier
        or structured.source_fact_id != proposal.fact_id
        or structured.proposal_fingerprint != proposal.proposal_fingerprint
        or structured.operation != proposal.allowed_operation
    ):
        verdict = ProposalSemanticVerdict.INVALID_INPUT
        detected_violation_codes = ()
    else:
        verdict = structured.verdict
        detected_violation_codes = structured.detected_violation_codes

    raw_response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
    structured_response_fingerprint = _compute_structured_response_fingerprint(structured)
    model_configuration_fingerprint = compute_semantic_validator_model_configuration_fingerprint(
        validation_context.validator_model_configuration
    )

    provenance = SemanticValidationProvenance(
        schema_version=SEMANTIC_VALIDATION_SCHEMA_VERSION,
        policy_version=validation_context.validation_policy_version,
        prompt_version=validation_context.validation_prompt_version,
        response_schema_version=SEMANTIC_VALIDATION_RESPONSE_SCHEMA_VERSION,
        context_fingerprint=validation_context_fingerprint,
        source_payload_fingerprint=prepared.payload.payload_fingerprint,
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        p5b_result_fingerprint=p5b_result.result_fingerprint,
        immutable_constraint_set_fingerprint=proposal.immutable_constraint_set_fingerprint,
        provider=response.provider,
        model_identifier=response.model_identifier,
        model_configuration_fingerprint=model_configuration_fingerprint,
        request_fingerprint=request_fingerprint,
        prompt_fingerprint=request_fingerprint,
        attempt_count=len(attempts),
        attempt_fingerprints=tuple(a.attempt_fingerprint for a in attempts),
        raw_response_fingerprint=raw_response_fingerprint,
        structured_response_fingerprint=structured_response_fingerprint,
    )
    provenance_fingerprint = compute_semantic_validation_provenance_fingerprint(provenance)

    item_fingerprint = compute_semantic_validation_item_fingerprint(
        planned_fact_use_fingerprint=proposal.planned_fact_use_fingerprint,
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        fact_selection_decision_fingerprint=proposal.fact_selection_decision_fingerprint,
        person_entity_id=proposal.person_entity_id,
        entity_id=proposal.entity_id,
        fact_id=proposal.fact_id,
        fact_type=proposal.fact_type,
        fact_revision=proposal.fact_revision,
        fact_content_fingerprint=proposal.fact_content_fingerprint,
        target_section=proposal.target_section.value,
        target_scope=proposal.target_scope,
        employment_scope_entity_id=proposal.employment_scope_entity_id,
        allowed_operation=proposal.allowed_operation.value,
        placement_order=proposal.placement_order,
        verdict=verdict.value,
        detected_violation_codes=[code.value for code in detected_violation_codes],
        provenance_fingerprint=provenance_fingerprint,
    )

    return ProposalSemanticValidationItem(
        semantic_validation_item_fingerprint=item_fingerprint,
        planned_fact_use_fingerprint=proposal.planned_fact_use_fingerprint,
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        fact_selection_decision_fingerprint=proposal.fact_selection_decision_fingerprint,
        person_entity_id=proposal.person_entity_id,
        entity_id=proposal.entity_id,
        fact_id=proposal.fact_id,
        fact_type=proposal.fact_type,
        fact_revision=proposal.fact_revision,
        fact_content_fingerprint=proposal.fact_content_fingerprint,
        target_section=proposal.target_section,
        target_scope=proposal.target_scope,
        employment_scope_entity_id=proposal.employment_scope_entity_id,
        allowed_operation=proposal.allowed_operation,
        placement_order=proposal.placement_order,
        verdict=verdict,
        detected_violation_codes=detected_violation_codes,
        provenance=provenance,
    )


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


def _fail_closed_result(
    *,
    status: SemanticValidationStatus,
    content_plan_fingerprint: str,
    violations: Sequence[CvContentApprovalViolation],
) -> ProposalSemanticValidationResult:
    result_fingerprint = compute_semantic_validation_result_fingerprint(
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        p5a_result_fingerprint=None,
        p5b_result_fingerprint=None,
        validation_context_fingerprint=None,
        validation_item_fingerprints=(),
        blocked_item_fingerprints=(),
    )
    return ProposalSemanticValidationResult(
        result_fingerprint=result_fingerprint,
        status=status,
        content_plan_fingerprint=content_plan_fingerprint,
        violations=tuple(violations),
    )


async def validate_cv_content_proposals(
    *,
    plan: CvContentPlan,
    p5a_result: CvContentGenerationResult,
    p5a_generation_context: GenerationContext,
    p5b_result: CvContentProposalGenerationResult,
    proposal_generation_context: ProposalGenerationContext,
    decisions_by_fingerprint: Mapping[str, FactSelectionDecision],
    entity_types: Mapping[UUID, EntityType],
    current_plan_replay_input: CvContentPlanReplayInput | None,
    current_p5a_replay_input: CvContentGenerationReplayInput | None,
    current_p5b_replay_input: CvContentProposalReplayInput | None,
    fact_payloads: ApprovedFactPayloadSet,
    validation_context: SemanticValidationContext,
    semantic_validator: ControlledProposalSemanticValidator,
) -> ProposalSemanticValidationResult:
    """Step 1: re-validate P4/P5a/P5b and semantically validate each P5b
    proposal exactly once. Never approves anything, never mutates text."""

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
        return _fail_closed_result(
            status=SemanticValidationStatus.BLOCKED,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=violation_code,
                    detail=f"P4 plan is not p5_ready: {violation_code.value}",
                ),
            ),
        )

    if p5a_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail="p5a_result.content_plan_fingerprint does not match plan.content_plan_fingerprint",
                ),
            ),
        )

    expected_payload_set_fp = compute_fact_payload_set_fingerprint(fact_payloads.payloads)
    if (
        expected_payload_set_fp != fact_payloads.payload_set_fingerprint
        or p5a_result.payload_set_fingerprint != fact_payloads.payload_set_fingerprint
    ):
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.PAYLOAD_SET_FINGERPRINT_MISMATCH,
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
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5a_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )

    if current_p5a_replay_input is None:
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
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
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5A_RESULT_STALE,
                    detail=f"P5a result is not fresh: stale_fields={p5a_freshness.stale_fields}",
                ),
            ),
        )

    if p5b_result.content_plan_fingerprint != plan.content_plan_fingerprint:
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                    detail="p5b_result.content_plan_fingerprint does not match plan.content_plan_fingerprint",
                ),
            ),
        )
    if p5b_result.p5a_result_fingerprint != p5a_result.result_fingerprint:
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
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
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.P5B_RESULT_FINGERPRINT_MISMATCH,
                    detail="p5b_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )

    if current_p5b_replay_input is None:
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
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
        return _fail_closed_result(
            status=SemanticValidationStatus.INVALID_INPUT,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            violations=(
                CvContentApprovalViolation(
                    violation_code=CvContentApprovalViolationCode.INPUT_P5B_RESULT_STALE,
                    detail=f"P5b result is not fresh: stale_fields={p5b_freshness.stale_fields}",
                ),
            ),
        )

    generation_policy_version = p5a_generation_context.generation_policy_version
    payloads_by_fact_id = {p.fact_id: p for p in fact_payloads.payloads}
    validation_context_fingerprint = compute_semantic_validation_context_fingerprint(
        validation_context
    )

    validation_items: list[ProposalSemanticValidationItem] = []
    blocked_items: list[BlockedSemanticValidationItem] = []
    for proposal in p5b_result.proposals:
        outcome = await _validate_one_proposal(
            proposal,
            plan=plan,
            p5a_result=p5a_result,
            p5b_result=p5b_result,
            payloads_by_fact_id=payloads_by_fact_id,
            proposal_generation_context=proposal_generation_context,
            generation_policy_version=generation_policy_version,
            validation_context=validation_context,
            validation_context_fingerprint=validation_context_fingerprint,
            semantic_validator=semantic_validator,
        )
        if isinstance(outcome, ProposalSemanticValidationItem):
            validation_items.append(outcome)
        else:
            blocked_items.append(outcome)

    deterministic_element_fingerprints = tuple(sorted(p5b_result.deterministic_element_fingerprints))
    non_eligible_blocked_item_fingerprints = tuple(
        sorted(p5b_result.non_eligible_blocked_item_fingerprints)
    )
    pending_approval_fingerprints = tuple(
        sorted(item.pending_approval_fingerprint for item in plan.pending_approvals)
    )
    omitted_fact_fingerprints = tuple(sorted(item.omission_fingerprint for item in plan.omitted_facts))
    conflict_fingerprints = tuple(sorted(item.conflict_fingerprint for item in plan.conflicts))

    has_provider_error = any(
        item.verdict == ProposalSemanticVerdict.PROVIDER_ERROR for item in validation_items
    )
    if has_provider_error:
        status = SemanticValidationStatus.PROVIDER_ERROR
    elif not p5b_result.proposals and not blocked_items:
        status = SemanticValidationStatus.VALIDATED
    elif blocked_items and not validation_items:
        status = SemanticValidationStatus.BLOCKED
    else:
        status = SemanticValidationStatus.VALIDATED

    result_fingerprint = compute_semantic_validation_result_fingerprint(
        status=status,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        p5b_result_fingerprint=p5b_result.result_fingerprint,
        validation_context_fingerprint=validation_context_fingerprint,
        validation_item_fingerprints=[
            item.semantic_validation_item_fingerprint for item in validation_items
        ],
        blocked_item_fingerprints=[
            item.blocked_semantic_validation_item_fingerprint for item in blocked_items
        ],
    )

    return ProposalSemanticValidationResult(
        result_fingerprint=result_fingerprint,
        status=status,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        p5b_result_fingerprint=p5b_result.result_fingerprint,
        validation_context_fingerprint=validation_context_fingerprint,
        validation_items=tuple(validation_items),
        blocked_items=tuple(blocked_items),
        violations=(),
        non_eligible_blocked_item_fingerprints=non_eligible_blocked_item_fingerprints,
        deterministic_element_fingerprints=deterministic_element_fingerprints,
        pending_approval_fingerprints=pending_approval_fingerprints,
        omitted_fact_fingerprints=omitted_fact_fingerprints,
        conflict_fingerprints=conflict_fingerprints,
    )
