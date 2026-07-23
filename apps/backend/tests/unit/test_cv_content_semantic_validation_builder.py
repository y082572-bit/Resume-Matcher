"""Unit tests for Step 1 of P5.5: ``validate_cv_content_proposals``.

Builds a real P4 plan -> real P5a result -> real P5b proposal via the
existing stage builders, then exercises Step 1 against deterministic fake
semantic validators. All data is synthetic.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_approval import (
    ControlledSemanticValidationResponse,
    ProposalSemanticVerdict,
    SemanticGuardrailViolationCode,
    SemanticValidationContext,
    SemanticValidationStatus,
    SemanticValidatorAdapterResponse,
    SemanticValidatorModelConfiguration,
    SemanticValidatorProviderErrorCode,
)
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
    GenerationStatus,
)
from app.schemas.cv_content_proposal import (
    ControlledProposalResponse,
    LlmAdapterResponse,
    LlmModelConfiguration,
    ProposalGenerationContext,
    ProposalGenerationStatus,
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


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-synthetic-p55-step1",
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
    """Deterministic fake that always returns a fixed verdict and counts calls."""

    def __init__(self, verdict: ProposalSemanticVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    async def validate(self, *, request, model_configuration):
        self.calls += 1
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


class _ProviderErrorThenSuccessValidator:
    """First attempt TIMEOUT (retryable), second attempt succeeds -- proves
    retry is bounded to 2 total attempts and reuses the same request."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_request_fingerprints: list[str] = []

    async def validate(self, *, request, model_configuration):
        from app.services.cv_content_semantic_validation_builder import (
            compute_semantic_validation_request_fingerprint,
        )

        self.calls += 1
        self.seen_request_fingerprints.append(
            compute_semantic_validation_request_fingerprint(request)
        )
        if self.calls == 1:
            return SemanticValidatorAdapterResponse(
                provider=model_configuration.provider,
                model_identifier=model_configuration.model_identifier,
                provider_error_code=SemanticValidatorProviderErrorCode.TIMEOUT,
            )
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


class _AlwaysNonRetryableErrorValidator:
    def __init__(self) -> None:
        self.calls = 0

    async def validate(self, *, request, model_configuration):
        self.calls += 1
        return SemanticValidatorAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            provider_error_code=SemanticValidatorProviderErrorCode.PROVIDER_REJECTED,
        )


class _MismatchedIdentityValidator:
    """Returns a structurally valid response whose self-reported identity
    fields do not match the request -- must never be trusted."""

    def __init__(self, *, wrong_fact_id: bool = False, wrong_proposal_fp: bool = False, wrong_operation: bool = False) -> None:
        self.wrong_fact_id = wrong_fact_id
        self.wrong_proposal_fp = wrong_proposal_fp
        self.wrong_operation = wrong_operation

    async def validate(self, *, request, model_configuration):
        source_fact_id = uuid4() if self.wrong_fact_id else request.source_fact_id
        proposal_fingerprint = ("f" * 64) if self.wrong_proposal_fp else request.proposal_fingerprint
        operation = (
            TransformationOperation.FLATTEN_FOR_LOWER_ROLE
            if self.wrong_operation and request.operation != TransformationOperation.FLATTEN_FOR_LOWER_ROLE
            else request.operation
        )
        return SemanticValidatorAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledSemanticValidationResponse(
                verdict=ProposalSemanticVerdict.PASS,
                source_fact_id=source_fact_id,
                proposal_fingerprint=proposal_fingerprint,
                operation=operation,
                source_claim_summary="s",
                proposal_claim_summary="p",
            ),
            raw_response_text="raw",
            finish_reason="stop",
        )


async def _build_pipeline():
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
    assert p5a_result.status == GenerationStatus.BLOCKED

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
    assert p5b_result.status == ProposalGenerationStatus.PROPOSED

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

    return dict(
        plan=plan,
        p5a_result=p5a_result,
        generation_context=generation_context,
        p5b_result=p5b_result,
        proposal_context=proposal_context,
        decisions_by_fp=decisions_by_fp,
        entity_types=entity_types,
        payload_set=payload_set,
        plan_replay=plan_replay,
        p5a_replay=p5a_replay,
        p5b_replay=p5b_replay,
    )


async def _run_step1(ctx, *, semantic_validator, **overrides):
    kwargs = dict(
        plan=ctx["plan"],
        p5a_result=ctx["p5a_result"],
        p5a_generation_context=ctx["generation_context"],
        p5b_result=ctx["p5b_result"],
        proposal_generation_context=ctx["proposal_context"],
        decisions_by_fingerprint=ctx["decisions_by_fp"],
        entity_types=ctx["entity_types"],
        current_plan_replay_input=ctx["plan_replay"],
        current_p5a_replay_input=ctx["p5a_replay"],
        current_p5b_replay_input=ctx["p5b_replay"],
        fact_payloads=ctx["payload_set"],
        validation_context=_validation_context(),
        semantic_validator=semantic_validator,
    )
    kwargs.update(overrides)
    return await validate_cv_content_proposals(**kwargs)


# -- P4/P5a/P5b re-validation, fail-closed --------------------------------


@pytest.mark.asyncio
async def test_stale_plan_replay_yields_zero_validator_calls() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator, current_plan_replay_input=None)
    assert result.status == SemanticValidationStatus.BLOCKED
    assert validator.calls == 0
    assert result.validation_items == ()
    assert result.blocked_items == ()


@pytest.mark.asyncio
async def test_missing_p5a_replay_input_is_invalid_input_with_zero_calls() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator, current_p5a_replay_input=None)
    assert result.status == SemanticValidationStatus.INVALID_INPUT
    assert validator.calls == 0


@pytest.mark.asyncio
async def test_missing_p5b_replay_input_is_invalid_input_with_zero_calls() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator, current_p5b_replay_input=None)
    assert result.status == SemanticValidationStatus.INVALID_INPUT
    assert validator.calls == 0


@pytest.mark.asyncio
async def test_tampered_p5a_result_fingerprint_is_invalid_input() -> None:
    ctx = await _build_pipeline()
    tampered_p5a = ctx["p5a_result"].model_copy(update={"result_fingerprint": "9" * 64})
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator, p5a_result=tampered_p5a)
    assert result.status == SemanticValidationStatus.INVALID_INPUT
    assert validator.calls == 0


@pytest.mark.asyncio
async def test_tampered_p5b_result_fingerprint_is_invalid_input() -> None:
    ctx = await _build_pipeline()
    tampered_p5b = ctx["p5b_result"].model_copy(update={"result_fingerprint": "9" * 64})
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator, p5b_result=tampered_p5b)
    assert result.status == SemanticValidationStatus.INVALID_INPUT
    assert validator.calls == 0


# -- deterministic guardrails: no validator call on tamper -----------------


@pytest.mark.asyncio
async def test_tampered_proposal_text_fingerprint_blocks_without_validator_call() -> None:
    ctx = await _build_pipeline()
    original = ctx["p5b_result"].proposals[0]
    # Preserve every immutable token ("30%") so the immutable-constraint
    # guardrail (which runs earlier) does not also fire -- this isolates the
    # proposal_text_fingerprint check itself.
    tampered_text = original.proposal_text + " Indeed."
    tampered = original.model_copy(update={"proposal_text": tampered_text})
    ctx["p5b_result"] = ctx["p5b_result"].model_copy(update={"proposals": (tampered,)})
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 0
    assert len(result.blocked_items) == 1
    assert result.blocked_items[0].violation_code == SemanticGuardrailViolationCode.PROPOSAL_TEXT_FINGERPRINT_MISMATCH
    assert result.validation_items == ()


@pytest.mark.asyncio
async def test_missing_payload_blocks_without_validator_call() -> None:
    ctx = await _build_pipeline()
    ctx["payload_set"] = ApprovedFactPayloadSet(payloads=(), payload_set_fingerprint=compute_fact_payload_set_fingerprint([]))
    # payload_set changed => p5a result no longer matches its own payload_set_fingerprint,
    # which is itself a fail-closed INVALID_INPUT case re-checked before any per-proposal work.
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 0
    assert result.status == SemanticValidationStatus.INVALID_INPUT


@pytest.mark.asyncio
async def test_immutable_constraint_violation_blocks_without_validator_call() -> None:
    ctx = await _build_pipeline()
    original = ctx["p5b_result"].proposals[0]
    # Drop the "30%" figure entirely -- violates the immutable percentage
    # constraint. proposal_fingerprint/proposal_text_fingerprint are left
    # untouched (stale) on purpose: the immutable-constraint guardrail reads
    # proposal.proposal_text directly and fires before either fingerprint
    # check, and the outer proposals tuple keeps the same
    # proposal_fingerprint value so the P5b result-level consistency check
    # upstream of per-proposal processing still passes.
    tampered_text = "Achieved a strong revenue increase within one synthetic year."
    tampered = original.model_copy(update={"proposal_text": tampered_text})
    ctx["p5b_result"] = ctx["p5b_result"].model_copy(update={"proposals": (tampered,)})

    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 0
    assert len(result.blocked_items) == 1
    assert result.blocked_items[0].violation_code == SemanticGuardrailViolationCode.IMMUTABLE_CONSTRAINT_VIOLATION


# -- one proposal, one validator call; verdict handling ---------------------


@pytest.mark.asyncio
async def test_exactly_one_validator_call_per_proposal_on_pass() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 1
    assert len(result.validation_items) == 1
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.PASS
    assert result.status == SemanticValidationStatus.VALIDATED


@pytest.mark.asyncio
async def test_fail_verdict_is_carried_through_with_violation_codes() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.FAIL)
    result = await _run_step1(ctx, semantic_validator=validator)
    item = result.validation_items[0]
    assert item.verdict == ProposalSemanticVerdict.FAIL
    assert "NEW_CLAIM_ADDED" in [c.value for c in item.detected_violation_codes]


@pytest.mark.asyncio
async def test_inconclusive_verdict_is_carried_through() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.INCONCLUSIVE)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.INCONCLUSIVE


@pytest.mark.asyncio
async def test_non_retryable_provider_error_makes_exactly_one_attempt() -> None:
    ctx = await _build_pipeline()
    validator = _AlwaysNonRetryableErrorValidator()
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 1
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.PROVIDER_ERROR
    assert result.status == SemanticValidationStatus.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_retryable_timeout_then_success_makes_exactly_two_attempts_same_request() -> None:
    ctx = await _build_pipeline()
    validator = _ProviderErrorThenSuccessValidator()
    result = await _run_step1(ctx, semantic_validator=validator)
    assert validator.calls == 2
    assert len(set(validator.seen_request_fingerprints)) == 1
    item = result.validation_items[0]
    assert item.verdict == ProposalSemanticVerdict.PASS
    assert item.provenance.attempt_count == 2


@pytest.mark.asyncio
async def test_response_fact_id_mismatch_yields_invalid_input_verdict() -> None:
    ctx = await _build_pipeline()
    validator = _MismatchedIdentityValidator(wrong_fact_id=True)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.INVALID_INPUT


@pytest.mark.asyncio
async def test_response_proposal_fingerprint_mismatch_yields_invalid_input_verdict() -> None:
    ctx = await _build_pipeline()
    validator = _MismatchedIdentityValidator(wrong_proposal_fp=True)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.INVALID_INPUT


@pytest.mark.asyncio
async def test_response_operation_mismatch_yields_invalid_input_verdict() -> None:
    ctx = await _build_pipeline()
    validator = _MismatchedIdentityValidator(wrong_operation=True)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert result.validation_items[0].verdict == ProposalSemanticVerdict.INVALID_INPUT


# -- provenance & partition --------------------------------------------------


@pytest.mark.asyncio
async def test_validation_item_has_full_provenance() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    provenance = result.validation_items[0].provenance
    assert provenance.content_plan_fingerprint == ctx["plan"].content_plan_fingerprint
    assert provenance.p5a_result_fingerprint == ctx["p5a_result"].result_fingerprint
    assert provenance.p5b_result_fingerprint == ctx["p5b_result"].result_fingerprint
    assert provenance.provider == "semantic-test-provider"
    assert provenance.model_identifier == "semantic-test-model"
    assert provenance.attempt_count == 1
    assert len(provenance.attempt_fingerprints) == 1


@pytest.mark.asyncio
async def test_partition_is_complete_and_disjoint() -> None:
    ctx = await _build_pipeline()
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    proposal_fps = {p.proposal_fingerprint for p in ctx["p5b_result"].proposals}
    item_fps = {item.proposal_fingerprint for item in result.validation_items}
    blocked_fps = {item.proposal_fingerprint for item in result.blocked_items}
    assert item_fps | blocked_fps == proposal_fps
    assert item_fps & blocked_fps == set()


@pytest.mark.asyncio
async def test_step1_never_mutates_proposal_text() -> None:
    ctx = await _build_pipeline()
    original_text = ctx["p5b_result"].proposals[0].proposal_text
    validator = _VerdictSemanticValidator(ProposalSemanticVerdict.PASS)
    result = await _run_step1(ctx, semantic_validator=validator)
    assert result.validation_items[0].proposal_fingerprint == ctx["p5b_result"].proposals[0].proposal_fingerprint
    assert ctx["p5b_result"].proposals[0].proposal_text == original_text
