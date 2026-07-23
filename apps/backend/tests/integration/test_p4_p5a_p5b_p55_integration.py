"""P4 -> P5a -> P5b -> P5.5 end-to-end proof: real ``build_cv_content_plan``
-> real ``generate_cv_content_draft`` -> real
``generate_controlled_cv_content_proposals`` -> P5.5 Step 1
(``validate_cv_content_proposals``) -> P5.5 Step 2
(``build_approved_cv_content``), against deterministic fake
``ControlledProposalLlmAdapter``/``ControlledProposalSemanticValidator``
implementations.

Confirms P5.5's own modules never touch the database, SQLAlchemy, LiteLLM,
``app.llm``, Stage 10C, the Master Resume, or a DOCX/PDF renderer -- and
exercises the semantic-PASS + user-APPROVED path, the REJECTED path, the
PENDING path, full provenance, ``ready_for_p6``, and the AWARD boundary.

All data is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    ControlledSemanticValidationResponse,
    FinalContentOrigin,
    FinalDispositionCode,
    ProposalApprovalDecision,
    ProposalApprovalDecisionValue,
    ProposalSemanticVerdict,
    SemanticValidationContext,
    SemanticValidationStatus,
    SemanticValidatorAdapterResponse,
    SemanticValidatorModelConfiguration,
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
from app.services import (
    cv_content_approval_builder,
    cv_content_approval_policy,
    cv_content_approval_replay,
    cv_content_semantic_validation_builder,
    cv_content_semantic_validation_prompt,
    cv_content_semantic_validator,
)
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
from app.services.cv_content_proposal_policy import is_award_fact_type
from app.services.cv_content_proposal_replay import replay_input_from_proposal_result
from app.services.cv_content_semantic_validation_builder import validate_cv_content_proposals
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, compute_target_context_fingerprint


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"
PROPOSAL_SCHEMA_VERSION = "cv-content-proposal-schema-v1"
PROPOSAL_POLICY_VERSION = "cv-content-proposal-policy-v1"
DECISION_POLICY_VERSION = "proposal-approval-decision-policy-v1"

_P55_SOURCE_FILES = (
    Path(cv_content_semantic_validation_builder.__file__),
    Path(cv_content_semantic_validation_prompt.__file__),
    Path(cv_content_semantic_validator.__file__),
    Path(cv_content_approval_builder.__file__),
    Path(cv_content_approval_replay.__file__),
    Path(cv_content_approval_policy.__file__),
    Path(cv_content_approval_builder.__file__).parent.parent / "schemas" / "cv_content_approval.py",
)


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-synthetic-p55-integration",
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
    """A synthetic ``ControlledProposalLlmAdapter`` -- never calls a real
    provider, never imports LiteLLM."""

    def __init__(self) -> None:
        self.calls: list = []

    async def generate(self, *, request, model_configuration):
        self.calls.append(request)
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


class _FakeSemanticValidator:
    """A synthetic ``ControlledProposalSemanticValidator`` -- never calls a
    real provider, never imports LiteLLM. Always returns PASS."""

    def __init__(self) -> None:
        self.calls: list = []

    async def validate(self, *, request, model_configuration):
        self.calls.append(request)
        return SemanticValidatorAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledSemanticValidationResponse(
                verdict=ProposalSemanticVerdict.PASS,
                source_fact_id=request.source_fact_id,
                proposal_fingerprint=request.proposal_fingerprint,
                operation=request.operation,
                source_claim_summary="source claim summary",
                proposal_claim_summary="proposal claim summary",
            ),
            raw_response_text="raw:pass",
            finish_reason="stop",
        )


async def _run_full_pipeline():
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", requested_operation=TransformationOperation.CONTROLLED_REPHRASE)
    entity_types = {d.entity_id: EntityType.SKILL}
    build_result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types=entity_types)
    assert build_result.plan is not None, build_result.snapshot_violations
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
    proposal_adapter = _FakeProposalAdapter()
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
        llm_adapter=proposal_adapter,
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

    semantic_validator = _FakeSemanticValidator()
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
        semantic_validator=semantic_validator,
    )
    assert semantic_result.status == SemanticValidationStatus.VALIDATED
    assert len(semantic_validator.calls) == 1

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
        proposal_adapter=proposal_adapter,
        semantic_validator=semantic_validator,
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


def _run_step2(ctx, approval_decisions):
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
async def test_approved_end_to_end_produces_ready_draft() -> None:
    ctx = await _run_full_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    result = _run_step2(ctx, [decision])
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True
    assert result.draft is not None
    profile_section = next(s for s in result.draft.sections if s.section == "PROFILE")
    assert len(profile_section.elements) == 1
    assert profile_section.elements[0].origin == FinalContentOrigin.APPROVED_PROPOSAL_P5B
    assert profile_section.elements[0].content_text == ctx["p5b_result"].proposals[0].proposal_text


@pytest.mark.asyncio
async def test_rejected_end_to_end_omits_content_but_is_ready() -> None:
    ctx = await _run_full_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.REJECTED)
    result = _run_step2(ctx, [decision])
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True
    profile_section = next(s for s in result.draft.sections if s.section == "PROFILE")
    assert profile_section.elements == ()
    assert result.dispositions[0].disposition_code == FinalDispositionCode.REJECTED_BY_USER


@pytest.mark.asyncio
async def test_pending_end_to_end_blocks_ready_for_p6() -> None:
    ctx = await _run_full_pipeline()
    result = _run_step2(ctx, [])
    assert result.status == ApprovalAssemblyStatus.PENDING_APPROVAL
    assert result.ready_for_p6 is False


@pytest.mark.asyncio
async def test_full_provenance_chain_is_intact() -> None:
    ctx = await _run_full_pipeline()
    decision = _make_decision(ctx, ProposalApprovalDecisionValue.APPROVED)
    result = _run_step2(ctx, [decision])
    assert result.p5a_result_fingerprint == ctx["p5a_result"].result_fingerprint
    assert result.p5b_result_fingerprint == ctx["p5b_result"].result_fingerprint
    assert result.semantic_validation_result_fingerprint == ctx["semantic_result"].result_fingerprint
    disposition = result.dispositions[0]
    assert disposition.element_fingerprint is not None


# -- AWARD stays out of scope for P5.5 too -----------------------------------


@pytest.mark.asyncio
async def test_award_name_is_unsupported_in_p55() -> None:
    tc = _target_context()
    d = _decision(target_context=tc, fact_type="AWARD_NAME", target_scope="SUMMARY", requested_operation=TransformationOperation.CONTROLLED_REPHRASE)
    assert is_award_fact_type(d.fact_type) is True
    build_result = build_cv_content_plan(
        target_context=tc, decisions=[d], entity_types={d.entity_id: EntityType.AWARD}
    )
    assert build_result.plan is not None
    omitted_fact_ids = {item.fact_id for item in build_result.plan.omitted_facts}
    assert d.fact_id in omitted_fact_ids


# -- source boundary checks: no DB, no legacy coupling, no DOCX/PDF/frontend --


def _code_only(text: str) -> str:
    return re.sub(r'""".*?"""', "", text, flags=re.DOTALL)


def _p55_source_texts() -> dict[str, str]:
    return {path.name: _code_only(path.read_text(encoding="utf-8")) for path in _P55_SOURCE_FILES}


def test_p55_modules_never_import_the_database_or_orm() -> None:
    forbidden = ("from app.database", "from app import database", "sqlalchemy", "app.models")
    for name, text in _p55_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden DB symbol: {needle}"


def test_p55_modules_never_import_legacy_stage_10c() -> None:
    forbidden = (
        "truth_legacy_migrator",
        "cv_transformation_generation",
        "cv_transformation_plan",
        "cv_transformation_approval",
    )
    for name, text in _p55_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden legacy module: {needle}"


def test_p55_modules_never_reference_source_reference_or_legacy_record_key() -> None:
    forbidden = ("source_reference", "legacy_record_key")
    for name, text in _p55_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden identity field: {needle}"


def test_p55_modules_never_reference_master_resume() -> None:
    forbidden = ("master_resume", "MasterResume")
    for name, text in _p55_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references Master Resume: {needle}"


def test_p55_modules_never_reference_docx_pdf_router_or_frontend() -> None:
    forbidden = (
        "docx",
        "playwright",
        "pdf",
        "frontend",
        "litellm",
        "llm_client",
        "app.llm",
        "fastapi",
        "apirouter",
    )
    for name, text in _p55_source_texts().items():
        lowered = text.lower()
        for needle in forbidden:
            assert needle not in lowered, f"{name} references forbidden output/router symbol: {needle}"


def test_named_synthetic_data_only_used() -> None:
    for value in ("Synthetic Senior Analyst", "synthetic year", "job-synthetic-p55-integration"):
        assert "synthetic" in value.lower()
