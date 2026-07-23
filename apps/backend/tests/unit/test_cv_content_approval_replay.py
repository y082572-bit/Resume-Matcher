"""Unit tests for ``cv_content_approval_replay``: FRESH/STALE/NOT_VERIFIED
for both P5.5 Step 1 (semantic validation) and Step 2 (approved content)
replay families. No function under test reads a clock.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_approval import (
    ApprovedContentFreshnessStatus,
    ControlledSemanticValidationResponse,
    ProposalApprovalDecision,
    ProposalApprovalDecisionValue,
    ProposalSemanticVerdict,
    SemanticValidationContext,
    SemanticValidationFreshnessStatus,
    SemanticValidatorAdapterResponse,
    SemanticValidatorModelConfiguration,
)
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
)
from app.schemas.cv_content_proposal import (
    ControlledProposalResponse,
    LlmAdapterResponse,
    LlmModelConfiguration,
    ProposalGenerationContext,
    TargetRoleContext,
)
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_approval_builder import (
    build_approved_cv_content,
    compute_decision_context_fingerprint,
    compute_proposal_approval_decision_fingerprint,
)
from app.services.cv_content_approval_replay import (
    compute_approved_content_replay_input_fingerprint,
    compute_semantic_validation_replay_input_fingerprint,
    evaluate_approved_content_freshness,
    evaluate_semantic_validation_freshness,
    is_approved_content_stale,
    is_semantic_validation_stale,
    replay_input_from_approved_content_result,
    replay_input_from_semantic_validation_result,
    stale_approved_content_fields,
    stale_semantic_validation_fields,
)
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    generate_cv_content_draft,
)
from app.services.cv_content_generation_policy import (
    CV_CONTENT_GENERATION_POLICY_VERSION,
    DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
)
from app.services.cv_content_generation_replay import replay_input_from_generation_result
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.cv_content_proposal_builder import generate_controlled_cv_content_proposals
from app.services.cv_content_proposal_replay import replay_input_from_proposal_result
from app.services.cv_content_semantic_validation_builder import validate_cv_content_proposals
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, compute_target_context_fingerprint


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"
PROPOSAL_SCHEMA_VERSION = "cv-content-proposal-schema-v1"
PROPOSAL_POLICY_VERSION = "cv-content-proposal-policy-v1"
DECISION_POLICY_VERSION = "proposal-approval-decision-policy-v1"


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-synthetic-p55-replay",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(*, target_context, fact_type="SUMMARY", target_scope="SUMMARY", requested_operation=TransformationOperation.CONTROLLED_REPHRASE):
    fact_id = uuid4()
    entity_id = uuid4()
    seed = f"{fact_id}{entity_id}{fact_type}{target_scope}{requested_operation}{uuid4()}"
    return FactSelectionDecision(
        decision_fingerprint=hashlib.sha256(seed.encode()).hexdigest(),
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=TRANSFORMATION_POLICY_VERSION,
        target_context_id=target_context.target_context_id,
        target_context_fingerprint=compute_target_context_fingerprint(target_context),
        person_entity_id=target_context.person_entity_id,
        entity_id=entity_id,
        fact_id=fact_id,
        fact_type=fact_type,
        fact_revision=1,
        fact_content_fingerprint="b" * 64,
        decision=SelectionDecisionOutcome.SELECTED,
        reason_codes=(FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,),
        requested_operation=requested_operation,
        requested_target_scope=target_scope,
        allowed_operation=requested_operation,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        permission_ids=(),
        permission_snapshot_fingerprint="c" * 64,
        requires_user_approval=False,
        evaluation_time=NOW,
        advisory_flags=(),
    )


def _payload_for(decision, value_json):
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=value_json,
        normalized_value_json=value_json,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    return ApprovedFactPayloadSnapshot(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=value_json,
        normalized_value_json=value_json,
        payload_fingerprint=fingerprint,
    )


def _proposal_context(**overrides) -> ProposalGenerationContext:
    model_configuration = LlmModelConfiguration(
        provider="test-provider",
        model_identifier="test-model",
        temperature=0.2,
        top_p=0.9,
        max_output_tokens=500,
        timeout_seconds=10.0,
        maximum_attempts=2,
        response_schema_version="controlled-proposal-response-v1",
        retry_policy_version="proposal-model-retry-policy-v1",
    )
    values = dict(
        locale="pl-PL",
        tone_profile="professional",
        maximum_character_count=500,
        maximum_sentence_count=5,
        target_role_context=TargetRoleContext(target_job_title="Synthetic Senior Analyst"),
        prompt_template_version="controlled-rephrase-prompt-v1",
        generation_policy_version="cv-content-proposal-policy-v1",
        immutable_constraint_policy_version="immutable-constraint-policy-v1",
        retry_policy_version="proposal-model-retry-policy-v1",
        model_configuration=model_configuration,
    )
    values.update(overrides)
    return ProposalGenerationContext(**values)


def _validation_context() -> SemanticValidationContext:
    model_configuration = SemanticValidatorModelConfiguration(
        provider="semantic-test-provider",
        model_identifier="semantic-test-model",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=300,
        timeout_seconds=10.0,
        maximum_attempts=2,
        response_schema_version="controlled-semantic-validation-response-v1",
        retry_policy_version="semantic-validator-retry-policy-v1",
    )
    return SemanticValidationContext(
        locale="pl-PL",
        validation_prompt_version="controlled-semantic-validation-prompt-v1",
        validation_policy_version="cv-content-semantic-validation-policy-v1",
        retry_policy_version="semantic-validator-retry-policy-v1",
        validator_model_configuration=model_configuration,
    )


class _FakeProposalAdapter:
    async def generate(self, *, request, model_configuration):
        text = "Achieved a 30% revenue increase within one synthetic year."
        return LlmAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledProposalResponse(
                proposal_text=text, operation=request.operation, source_fact_id=request.source_fact_id
            ),
            raw_response_text=f"raw:{text}",
            finish_reason="stop",
        )


class _PassSemanticValidator:
    async def validate(self, *, request, model_configuration):
        return SemanticValidatorAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledSemanticValidationResponse(
                verdict=ProposalSemanticVerdict.PASS,
                source_fact_id=request.source_fact_id,
                proposal_fingerprint=request.proposal_fingerprint,
                operation=request.operation,
                source_claim_summary="s",
                proposal_claim_summary="p",
            ),
            raw_response_text="raw:pass",
            finish_reason="stop",
        )


async def _build_pipeline():
    tc = _target_context()
    d = _decision(target_context=tc)
    entity_types = {d.entity_id: EntityType.SKILL}
    build_result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types=entity_types)
    plan = build_result.plan
    decisions_by_fp = {d.decision_fingerprint: d}
    payload = _payload_for(d, "Grew revenue by 30% in one synthetic year.")
    payload_set = ApprovedFactPayloadSet(
        payloads=(payload,), payload_set_fingerprint=compute_fact_payload_set_fingerprint([payload])
    )
    generation_context = GenerationContext(
        locale="pl-PL",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    plan_replay = replay_input_from_plan(plan)
    p5a_result = generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_replay_input=plan_replay,
        fact_payloads=payload_set,
        generation_context=generation_context,
    )
    p5a_replay = replay_input_from_generation_result(
        plan=plan,
        payload_set=payload_set,
        generation_context=generation_context,
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=p5a_result,
    )
    proposal_context = _proposal_context()
    p5b_result = await generate_controlled_cv_content_proposals(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        fact_payloads=payload_set,
        proposal_context=proposal_context,
        llm_adapter=_FakeProposalAdapter(),
    )
    p5b_replay = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=plan.content_policy_version,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=generation_context.generation_policy_version,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=payload_set.payload_set_fingerprint,
        proposal_context=proposal_context,
        result=p5b_result,
    )
    semantic_result = await validate_cv_content_proposals(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        p5b_result=p5b_result,
        proposal_generation_context=proposal_context,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        current_p5b_replay_input=p5b_replay,
        fact_payloads=payload_set,
        validation_context=_validation_context(),
        semantic_validator=_PassSemanticValidator(),
    )
    return dict(
        plan=plan,
        p5a_result=p5a_result,
        generation_context=generation_context,
        p5b_result=p5b_result,
        proposal_context=proposal_context,
        semantic_result=semantic_result,
        decisions_by_fp=decisions_by_fp,
        entity_types=entity_types,
        payload_set=payload_set,
        plan_replay=plan_replay,
        p5a_replay=p5a_replay,
        p5b_replay=p5b_replay,
    )


def _semantic_replay_input(ctx):
    return replay_input_from_semantic_validation_result(
        plan=ctx["plan"],
        p5a_replay_input=ctx["p5a_replay"],
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=ctx["generation_context"].generation_policy_version,
        p5b_replay_input=ctx["p5b_replay"],
        p5b_proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        p5b_proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=ctx["payload_set"].payload_set_fingerprint,
        result=ctx["semantic_result"],
    )


# -- Step 1: semantic validation replay --------------------------------------


@pytest.mark.asyncio
async def test_semantic_replay_is_fresh_against_itself() -> None:
    ctx = await _build_pipeline()
    replay_input = _semantic_replay_input(ctx)
    result = evaluate_semantic_validation_freshness(replay_input, replay_input)
    assert result.freshness_status == SemanticValidationFreshnessStatus.FRESH
    assert result.stale_fields == ()


@pytest.mark.asyncio
async def test_semantic_replay_is_not_verified_without_current() -> None:
    ctx = await _build_pipeline()
    replay_input = _semantic_replay_input(ctx)
    result = evaluate_semantic_validation_freshness(replay_input, None)
    assert result.freshness_status == SemanticValidationFreshnessStatus.FRESHNESS_NOT_VERIFIED


@pytest.mark.asyncio
async def test_semantic_replay_detects_content_plan_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"content_plan_fingerprint": "9" * 64})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert result.freshness_status == SemanticValidationFreshnessStatus.STALE
    assert "content_plan_fingerprint" in result.stale_fields
    assert is_semantic_validation_stale(previous, current) is True


@pytest.mark.asyncio
async def test_semantic_replay_detects_p5a_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"p5a_result_fingerprint": "8" * 64})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert result.freshness_status == SemanticValidationFreshnessStatus.STALE
    assert "p5a_result_fingerprint" in result.stale_fields


@pytest.mark.asyncio
async def test_semantic_replay_detects_p5b_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"p5b_result_fingerprint": "7" * 64})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert "p5b_result_fingerprint" in result.stale_fields


@pytest.mark.asyncio
async def test_semantic_replay_detects_payload_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"fact_payload_set_fingerprint": "6" * 64})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert "fact_payload_set_fingerprint" in result.stale_fields


@pytest.mark.asyncio
async def test_semantic_replay_detects_validator_model_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"validator_model_configuration_fingerprint": "5" * 64})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert "validator_model_configuration_fingerprint" in result.stale_fields


@pytest.mark.asyncio
async def test_semantic_replay_detects_prompt_version_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"validation_prompt_version": "different-prompt-v2"})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert "validation_prompt_version" in result.stale_fields


@pytest.mark.asyncio
async def test_semantic_replay_detects_validation_item_change() -> None:
    ctx = await _build_pipeline()
    previous = _semantic_replay_input(ctx)
    current = previous.model_copy(update={"validation_item_fingerprints": ("0" * 64,)})
    result = evaluate_semantic_validation_freshness(previous, current)
    assert "validation_item_fingerprints" in result.stale_fields


def test_stale_semantic_validation_fields_helper_matches_evaluate() -> None:
    from app.schemas.cv_content_approval import ProposalSemanticValidationReplayInput

    base_kwargs = dict(
        content_plan_fingerprint="a" * 64,
        content_plan_schema_version="v1",
        content_policy_version="v1",
        p5a_result_fingerprint="a" * 64,
        p5a_replay_input_fingerprint="a" * 64,
        p5a_generation_schema_version="v1",
        p5a_generation_policy_version="v1",
        p5b_result_fingerprint="a" * 64,
        p5b_replay_input_fingerprint="a" * 64,
        p5b_proposal_schema_version="v1",
        p5b_proposal_policy_version="v1",
        fact_payload_set_fingerprint="a" * 64,
        validation_context_fingerprint="a" * 64,
        validator_model_configuration_fingerprint="a" * 64,
        validation_prompt_version="v1",
        validation_policy_version="v1",
        validation_schema_version="v1",
    )
    previous = ProposalSemanticValidationReplayInput(**base_kwargs)
    current = previous.model_copy(update={"content_policy_version": "v2"})
    assert stale_semantic_validation_fields(previous, current) == ("content_policy_version",)


def test_semantic_validation_replay_input_fingerprint_is_deterministic() -> None:
    from app.schemas.cv_content_approval import ProposalSemanticValidationReplayInput

    kwargs = dict(
        content_plan_fingerprint="a" * 64,
        content_plan_schema_version="v1",
        content_policy_version="v1",
        p5a_result_fingerprint="a" * 64,
        p5a_replay_input_fingerprint="a" * 64,
        p5a_generation_schema_version="v1",
        p5a_generation_policy_version="v1",
        p5b_result_fingerprint="a" * 64,
        p5b_replay_input_fingerprint="a" * 64,
        p5b_proposal_schema_version="v1",
        p5b_proposal_policy_version="v1",
        fact_payload_set_fingerprint="a" * 64,
        validation_context_fingerprint="a" * 64,
        validator_model_configuration_fingerprint="a" * 64,
        validation_prompt_version="v1",
        validation_policy_version="v1",
        validation_schema_version="v1",
    )
    a = ProposalSemanticValidationReplayInput(**kwargs)
    b = ProposalSemanticValidationReplayInput(**kwargs)
    assert compute_semantic_validation_replay_input_fingerprint(
        a
    ) == compute_semantic_validation_replay_input_fingerprint(b)


# -- Step 2: approved content replay -----------------------------------------


def _approved_content_replay_input(ctx, approval_decisions):
    semantic_replay_input = _semantic_replay_input(ctx)
    result = build_approved_cv_content(
        plan=ctx["plan"],
        p5a_result=ctx["p5a_result"],
        p5a_generation_context=ctx["generation_context"],
        p5b_result=ctx["p5b_result"],
        proposal_generation_context=ctx["proposal_context"],
        semantic_validation_result=ctx["semantic_result"],
        approval_decisions=approval_decisions,
        decisions_by_fingerprint=ctx["decisions_by_fp"],
        entity_types=ctx["entity_types"],
        fact_payloads=ctx["payload_set"],
        current_plan_replay_input=ctx["plan_replay"],
        current_p5a_replay_input=ctx["p5a_replay"],
        current_p5b_replay_input=ctx["p5b_replay"],
        current_semantic_validation_replay_input=semantic_replay_input,
    )
    replay_input = replay_input_from_approved_content_result(
        semantic_validation_replay_input=semantic_replay_input,
        approval_decisions=approval_decisions,
        result=result,
    )
    return result, replay_input


@pytest.mark.asyncio
async def test_approved_content_replay_is_fresh_against_itself() -> None:
    ctx = await _build_pipeline()
    _, replay_input = _approved_content_replay_input(ctx, [])
    result = evaluate_approved_content_freshness(replay_input, replay_input)
    assert result.freshness_status == ApprovedContentFreshnessStatus.FRESH


@pytest.mark.asyncio
async def test_approved_content_replay_not_verified_without_current() -> None:
    ctx = await _build_pipeline()
    _, replay_input = _approved_content_replay_input(ctx, [])
    result = evaluate_approved_content_freshness(replay_input, None)
    assert result.freshness_status == ApprovedContentFreshnessStatus.FRESHNESS_NOT_VERIFIED


@pytest.mark.asyncio
async def test_approved_content_replay_detects_disposition_change() -> None:
    ctx = await _build_pipeline()
    _, previous = _approved_content_replay_input(ctx, [])
    current = previous.model_copy(update={"disposition_fingerprints": ("0" * 64,)})
    result = evaluate_approved_content_freshness(previous, current)
    assert result.freshness_status == ApprovedContentFreshnessStatus.STALE
    assert "disposition_fingerprints" in result.stale_fields
    assert is_approved_content_stale(previous, current) is True


@pytest.mark.asyncio
async def test_approved_content_replay_detects_draft_fingerprint_change() -> None:
    ctx = await _build_pipeline()
    _, previous = _approved_content_replay_input(ctx, [])
    current = previous.model_copy(update={"draft_fingerprint": "1" * 64})
    result = evaluate_approved_content_freshness(previous, current)
    assert "draft_fingerprint" in result.stale_fields


@pytest.mark.asyncio
async def test_approved_content_replay_detects_decision_change() -> None:
    ctx = await _build_pipeline()
    proposal = ctx["p5b_result"].proposals[0]
    item = ctx["semantic_result"].validation_items[0]
    context_fp = compute_decision_context_fingerprint(
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        semantic_validation_item_fingerprint=item.semantic_validation_item_fingerprint,
        semantic_validation_result_fingerprint=ctx["semantic_result"].result_fingerprint,
    )
    decision_fp = compute_proposal_approval_decision_fingerprint(
        decision_context_fingerprint=context_fp,
        decision=ProposalApprovalDecisionValue.APPROVED,
        decision_policy_version=DECISION_POLICY_VERSION,
    )
    decision = ProposalApprovalDecision(
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        semantic_validation_item_fingerprint=item.semantic_validation_item_fingerprint,
        semantic_validation_result_fingerprint=ctx["semantic_result"].result_fingerprint,
        decision=ProposalApprovalDecisionValue.APPROVED,
        decision_policy_version=DECISION_POLICY_VERSION,
        decision_context_fingerprint=context_fp,
        decision_fingerprint=decision_fp,
    )
    _, previous = _approved_content_replay_input(ctx, [decision])
    current = previous.model_copy(update={"approval_decision_fingerprints": ()})
    result = evaluate_approved_content_freshness(previous, current)
    assert "approval_decision_fingerprints" in result.stale_fields


def test_approved_content_replay_input_fingerprint_is_deterministic() -> None:
    from app.schemas.cv_content_approval import ApprovedCvContentReplayInput

    kwargs = dict(
        semantic_validation_result_fingerprint="a" * 64,
        semantic_validation_replay_input_fingerprint="a" * 64,
        approval_decision_schema_version="v1",
        approval_decision_policy_version="v1",
        approved_content_schema_version="v1",
        approved_content_policy_version="v1",
        approved_content_result_fingerprint="a" * 64,
    )
    a = ApprovedCvContentReplayInput(**kwargs)
    b = ApprovedCvContentReplayInput(**kwargs)
    assert compute_approved_content_replay_input_fingerprint(
        a
    ) == compute_approved_content_replay_input_fingerprint(b)
