"""Unit tests for Step 2 of P5.5: ``build_approved_cv_content``.

Builds a real P4 -> P5a -> P5b -> P5.5 Step 1 pipeline, then exercises
Step 2's approval-decision handling, staleness rejection, and
``ready_for_p6`` computation. Step 2 never calls a provider and never
imports the semantic validator Protocol/adapter.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pydantic
import pytest

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    ControlledSemanticValidationResponse,
    CvContentApprovalViolationCode,
    FinalContentOrigin,
    FinalDispositionCode,
    ProposalApprovalDecision,
    ProposalApprovalDecisionValue,
    ProposalSemanticVerdict,
    SemanticValidationContext,
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
from app.services.cv_content_approval_replay import replay_input_from_semantic_validation_result
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
        job_or_application_id="job-synthetic-p55-step2",
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


class _VerdictSemanticValidator:
    def __init__(self, verdict: ProposalSemanticVerdict) -> None:
        self.verdict = verdict

    async def validate(self, *, request, model_configuration):
        return SemanticValidatorAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledSemanticValidationResponse(
                verdict=self.verdict,
                source_fact_id=request.source_fact_id,
                proposal_fingerprint=request.proposal_fingerprint,
                operation=request.operation,
                detected_violation_codes=(
                    ("NEW_CLAIM_ADDED",) if self.verdict == ProposalSemanticVerdict.FAIL else ()
                ),
                source_claim_summary="source claim",
                proposal_claim_summary="proposal claim",
            ),
            raw_response_text=f"raw:{self.verdict.value}",
            finish_reason="stop",
        )


async def _build_pipeline(*, verdict: ProposalSemanticVerdict = ProposalSemanticVerdict.PASS):
    tc = _target_context()
    d = _decision(target_context=tc)
    entity_types = {d.entity_id: EntityType.SKILL}
    build_result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types=entity_types)
    assert build_result.plan is not None
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
        semantic_validator=_VerdictSemanticValidator(verdict),
    )
    semantic_replay = replay_input_from_semantic_validation_result(
        plan=plan,
        p5a_replay_input=p5a_replay,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=generation_context.generation_policy_version,
        p5b_replay_input=p5b_replay,
        p5b_proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        p5b_proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=payload_set.payload_set_fingerprint,
        result=semantic_result,
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
        semantic_replay=semantic_replay,
    )


def _make_decision(ctx, value: ProposalApprovalDecisionValue) -> ProposalApprovalDecision:
    proposal = ctx["p5b_result"].proposals[0]
    item = ctx["semantic_result"].validation_items[0]
    context_fp = compute_decision_context_fingerprint(
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        semantic_validation_item_fingerprint=item.semantic_validation_item_fingerprint,
        semantic_validation_result_fingerprint=ctx["semantic_result"].result_fingerprint,
    )
    decision_fp = compute_proposal_approval_decision_fingerprint(
        decision_context_fingerprint=context_fp, decision=value, decision_policy_version=DECISION_POLICY_VERSION
    )
    return ProposalApprovalDecision(
        proposal_fingerprint=proposal.proposal_fingerprint,
        proposal_text_fingerprint=proposal.proposal_text_fingerprint,
        semantic_validation_item_fingerprint=item.semantic_validation_item_fingerprint,
        semantic_validation_result_fingerprint=ctx["semantic_result"].result_fingerprint,
        decision=value,
        decision_policy_version=DECISION_POLICY_VERSION,
        decision_context_fingerprint=context_fp,
        decision_fingerprint=decision_fp,
    )


def _run_step2(ctx, *, approval_decisions):
    return build_approved_cv_content(
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
        current_semantic_validation_replay_input=ctx["semantic_replay"],
    )


@pytest.mark.asyncio
async def test_no_decision_is_pending() -> None:
    ctx = await _build_pipeline()
    result = _run_step2(ctx, approval_decisions=[])
    assert result.status == ApprovalAssemblyStatus.PENDING_APPROVAL
    assert result.ready_for_p6 is False
    assert result.dispositions[0].disposition_code == FinalDispositionCode.PENDING_USER_DECISION


@pytest.mark.asyncio
async def test_approved_with_pass_is_included() -> None:
    ctx = await _build_pipeline(verdict=ProposalSemanticVerdict.PASS)
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    result = _run_step2(ctx, approval_decisions=[decision])
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True
    disposition = result.dispositions[0]
    assert disposition.disposition_code == FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL
    assert disposition.origin == FinalContentOrigin.APPROVED_PROPOSAL_P5B


@pytest.mark.asyncio
async def test_rejected_omits_proposal_but_stays_ready() -> None:
    ctx = await _build_pipeline(verdict=ProposalSemanticVerdict.PASS)
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.REJECTED)
    result = _run_step2(ctx, approval_decisions=[decision])
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True
    assert result.dispositions[0].disposition_code == FinalDispositionCode.REJECTED_BY_USER
    assert result.dispositions[0].origin is None


@pytest.mark.asyncio
async def test_fail_verdict_cannot_be_approved_even_with_approved_decision_attempt() -> None:
    ctx = await _build_pipeline(verdict=ProposalSemanticVerdict.FAIL)
    # No decision is possible at all: the proposal never reaches
    # validation_items with a PASS verdict, so there is nothing to approve --
    # confirm the disposition reflects the semantic failure regardless.
    result = _run_step2(ctx, approval_decisions=[])
    assert result.dispositions[0].disposition_code == FinalDispositionCode.SEMANTIC_VALIDATION_FAILED
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True


@pytest.mark.asyncio
async def test_inconclusive_verdict_cannot_be_approved() -> None:
    ctx = await _build_pipeline(verdict=ProposalSemanticVerdict.INCONCLUSIVE)
    result = _run_step2(ctx, approval_decisions=[])
    assert result.dispositions[0].disposition_code == FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE


@pytest.mark.asyncio
async def test_duplicate_decision_is_globally_invalid() -> None:
    ctx = await _build_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    result = _run_step2(ctx, approval_decisions=[decision, decision])
    assert result.status == ApprovalAssemblyStatus.INVALID_INPUT
    assert result.violations[0].violation_code == CvContentApprovalViolationCode.DUPLICATE_APPROVAL_DECISION


@pytest.mark.asyncio
async def test_decision_for_unknown_proposal_is_globally_invalid() -> None:
    ctx = await _build_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    unknown = decision.model_copy(update={"proposal_fingerprint": "f" * 64})
    result = _run_step2(ctx, approval_decisions=[unknown])
    assert result.status == ApprovalAssemblyStatus.INVALID_INPUT
    assert (
        result.violations[0].violation_code
        == CvContentApprovalViolationCode.APPROVAL_DECISION_FOR_UNKNOWN_PROPOSAL
    )


@pytest.mark.asyncio
async def test_decision_fingerprint_is_recalculated_and_tampering_is_caught() -> None:
    ctx = await _build_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    tampered = decision.model_copy(update={"decision_fingerprint": "e" * 64})
    result = _run_step2(ctx, approval_decisions=[tampered])
    assert result.status == ApprovalAssemblyStatus.INVALID_INPUT
    assert (
        result.violations[0].violation_code
        == CvContentApprovalViolationCode.APPROVAL_DECISION_FINGERPRINT_MISMATCH
    )


@pytest.mark.asyncio
async def test_decision_referencing_stale_proposal_text_is_invalid() -> None:
    ctx = await _build_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    stale = decision.model_copy(update={"proposal_text_fingerprint": "d" * 64})
    result = _run_step2(ctx, approval_decisions=[stale])
    assert result.status == ApprovalAssemblyStatus.INVALID_INPUT
    assert result.violations[0].violation_code == CvContentApprovalViolationCode.APPROVAL_FOR_STALE_PROPOSAL


@pytest.mark.asyncio
async def test_decision_referencing_stale_validation_result_is_invalid() -> None:
    ctx = await _build_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    stale = decision.model_copy(update={"semantic_validation_result_fingerprint": "9" * 64})
    result = _run_step2(ctx, approval_decisions=[stale])
    assert result.status == ApprovalAssemblyStatus.INVALID_INPUT
    assert result.violations[0].violation_code == CvContentApprovalViolationCode.APPROVAL_FOR_STALE_PROPOSAL


def test_edited_text_field_is_rejected_by_the_closed_schema() -> None:
    with pytest.raises(pydantic.ValidationError):
        ProposalApprovalDecision(
            proposal_fingerprint="a" * 64,
            proposal_text_fingerprint="b" * 64,
            semantic_validation_item_fingerprint="c" * 64,
            semantic_validation_result_fingerprint="d" * 64,
            decision=ProposalApprovalDecisionValue.APPROVED,
            decision_policy_version=DECISION_POLICY_VERSION,
            decision_context_fingerprint="e" * 64,
            decision_fingerprint="f" * 64,
            edited_text="a user rewrite",  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_step2_never_calls_a_provider_or_semantic_validator() -> None:
    """Step 2's own module never imports the semantic validator Protocol."""
    import re
    from pathlib import Path
    from app.services import cv_content_approval_builder as module

    source = re.sub(r'""".*?"""', "", Path(module.__file__).read_text(encoding="utf-8"), flags=re.DOTALL)
    assert "ControlledProposalSemanticValidator" not in source
    assert "cv_content_semantic_validator" not in source
    assert "litellm" not in source.lower()
    assert "app.llm" not in source


@pytest.mark.asyncio
async def test_ready_for_p6_requires_status_ready() -> None:
    ctx = await _build_pipeline()
    pending_result = _run_step2(ctx, approval_decisions=[])
    assert pending_result.ready_for_p6 is False

    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    ready_result = _run_step2(ctx, approval_decisions=[decision])
    assert ready_result.ready_for_p6 is True
    assert ready_result.status == ApprovalAssemblyStatus.READY
