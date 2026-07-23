"""Unit tests for the P5.5 final partition: every P4 ``PlannedFactUse`` maps
to exactly one disposition, header facts are always deterministic, section
and employment-entry order is preserved, and ``REQUIRES_REPLAN`` is
unreachable under the current closed set of P5b operations.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
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
    SemanticValidatorAdapterResponse,
    SemanticValidatorModelConfiguration,
)
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
)
from app.schemas.cv_content_plan import CvSection
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
        job_or_application_id="job-synthetic-p55-partition",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(*, target_context, entity_id, fact_type, target_scope, requested_operation, employment_scope_entity_id=None):
    fact_id = uuid4()
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
        employment_scope_entity_id=employment_scope_entity_id,
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
        text = "Delivered measurable impact within one synthetic year."
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


async def _build_mixed_pipeline():
    """One EXPERIENCE header (EXACT_COPY, always P5a), one EXPERIENCE
    achievement (CONTROLLED_REPHRASE, eligible for P5b), one PROFILE
    summary (EXACT_COPY, always P5a)."""

    tc = _target_context()
    employment_id = uuid4()
    header = _decision(
        target_context=tc,
        entity_id=employment_id,
        fact_type="EMPLOYMENT_ROLE",
        target_scope="EMPLOYMENT_HEADER",
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    achievement = _decision(
        target_context=tc,
        entity_id=uuid4(),
        fact_type="ACHIEVEMENT",
        target_scope="EXPERIENCE",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        employment_scope_entity_id=employment_id,
    )
    summary = _decision(
        target_context=tc,
        entity_id=uuid4(),
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    decisions = [header, achievement, summary]
    entity_types = {
        employment_id: EntityType.EMPLOYMENT,
        achievement.entity_id: EntityType.SKILL,
        summary.entity_id: EntityType.SKILL,
    }
    build_result = build_cv_content_plan(target_context=tc, decisions=decisions, entity_types=entity_types)
    assert build_result.plan is not None, build_result.snapshot_violations
    plan = build_result.plan
    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}

    payloads = [
        _payload_for(header, {"stanowisko": "Senior Analyst", "firma": "Synthetic Corp"}),
        _payload_for(achievement, "Delivered measurable impact for one synthetic year."),
        _payload_for(summary, "Synthetic summary of professional experience."),
    ]
    payload_set = ApprovedFactPayloadSet(
        payloads=tuple(payloads), payload_set_fingerprint=compute_fact_payload_set_fingerprint(payloads)
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
        header=header,
        achievement=achievement,
        summary=summary,
        employment_id=employment_id,
    )


def _approve_all(ctx):
    decisions = []
    for item in ctx["semantic_result"].validation_items:
        proposal = next(p for p in ctx["p5b_result"].proposals if p.proposal_fingerprint == item.proposal_fingerprint)
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
        decisions.append(
            ProposalApprovalDecision(
                proposal_fingerprint=proposal.proposal_fingerprint,
                proposal_text_fingerprint=proposal.proposal_text_fingerprint,
                semantic_validation_item_fingerprint=item.semantic_validation_item_fingerprint,
                semantic_validation_result_fingerprint=ctx["semantic_result"].result_fingerprint,
                decision=ProposalApprovalDecisionValue.APPROVED,
                decision_policy_version=DECISION_POLICY_VERSION,
                decision_context_fingerprint=context_fp,
                decision_fingerprint=decision_fp,
            )
        )
    return decisions


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
async def test_every_planned_fact_use_has_exactly_one_disposition() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    use_fps = set()
    for section in ctx["plan"].sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                for use in entry.header_fact_uses + entry.responsibility_fact_uses + entry.achievement_fact_uses:
                    use_fps.add(use.planned_fact_use_fingerprint)
        else:
            for use in section.planned_fact_uses:
                use_fps.add(use.planned_fact_use_fingerprint)
    disposition_fps = [d.planned_fact_use_fingerprint for d in result.dispositions]
    assert set(disposition_fps) == use_fps
    assert len(disposition_fps) == len(set(disposition_fps))  # no duplicates: disjoint


@pytest.mark.asyncio
async def test_header_fact_is_always_included_deterministic() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    header_disposition = next(
        d for d in result.dispositions if d.fact_id == ctx["header"].fact_id
    )
    assert header_disposition.disposition_code == FinalDispositionCode.INCLUDED_DETERMINISTIC
    assert header_disposition.origin == FinalContentOrigin.DETERMINISTIC_P5A


@pytest.mark.asyncio
async def test_no_overlap_between_deterministic_and_proposal_origin() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    deterministic_fps = {
        d.planned_fact_use_fingerprint
        for d in result.dispositions
        if d.origin == FinalContentOrigin.DETERMINISTIC_P5A
    }
    proposal_fps = {
        d.planned_fact_use_fingerprint
        for d in result.dispositions
        if d.origin == FinalContentOrigin.APPROVED_PROPOSAL_P5B
    }
    assert deterministic_fps & proposal_fps == set()
    assert len(deterministic_fps) == 2  # header + summary
    assert len(proposal_fps) == 1  # achievement


@pytest.mark.asyncio
async def test_requires_replan_is_unreachable_for_current_closed_operations() -> None:
    """Header facts are copy-only under the current closed P4/P5a/P5b
    policy, so a single P5b rejection never forces a replan."""

    ctx = await _build_mixed_pipeline()
    for approval_decisions in ([], _approve_all(ctx)):
        result = _run_step2(ctx, approval_decisions)
        assert result.status != ApprovalAssemblyStatus.REQUIRES_REPLAN
        assert all(d.disposition_code != FinalDispositionCode.REQUIRES_REPLAN for d in result.dispositions)


@pytest.mark.asyncio
async def test_section_order_matches_plan_section_order() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    approved_order = [s.section if s.kind == "FLAT" else CvSection.EXPERIENCE for s in result.draft.sections]
    plan_order = [s.section if s.kind == "FLAT" else CvSection.EXPERIENCE for s in ctx["plan"].sections]
    assert approved_order == plan_order


@pytest.mark.asyncio
async def test_employment_entry_grouping_and_order_preserved() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    experience_section = next(s for s in result.draft.sections if s.kind == "EXPERIENCE")
    assert len(experience_section.experience_entries) == 1
    entry = experience_section.experience_entries[0]
    assert entry.employment_entity_id == ctx["employment_id"]
    assert len(entry.header_elements) == 1
    assert len(entry.achievement_elements) == 1
    assert entry.header_elements[0].fact_id == ctx["header"].fact_id
    assert entry.achievement_elements[0].fact_id == ctx["achievement"].fact_id


@pytest.mark.asyncio
async def test_no_cross_section_or_cross_employment_movement() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    for section in result.draft.sections:
        if section.kind == "FLAT":
            for element in section.elements:
                assert element.target_section == section.section
        else:
            for entry in section.experience_entries:
                for element in entry.header_elements + entry.responsibility_elements + entry.achievement_elements:
                    assert element.employment_scope_entity_id in (None, entry.employment_entity_id) or (
                        element.fact_id == ctx["header"].fact_id
                    )


@pytest.mark.asyncio
async def test_content_text_is_never_rewritten_by_step2() -> None:
    ctx = await _build_mixed_pipeline()
    proposal_text = ctx["p5b_result"].proposals[0].proposal_text
    result = _run_step2(ctx, _approve_all(ctx))
    approved_texts = [d.element_fingerprint for d in result.dispositions if d.element_fingerprint]
    experience_section = next(s for s in result.draft.sections if s.kind == "EXPERIENCE")
    achievement_element = experience_section.experience_entries[0].achievement_elements[0]
    assert achievement_element.content_text == proposal_text


@pytest.mark.asyncio
async def test_origin_provenance_is_populated_per_origin() -> None:
    ctx = await _build_mixed_pipeline()
    result = _run_step2(ctx, _approve_all(ctx))
    for section in result.draft.sections:
        elements = section.elements if section.kind == "FLAT" else [
            e for entry in section.experience_entries for e in entry.header_elements + entry.responsibility_elements + entry.achievement_elements
        ]
        for element in elements:
            if element.origin == FinalContentOrigin.DETERMINISTIC_P5A:
                assert element.generated_element_fingerprint is not None
                assert element.generation_provenance_fingerprint is not None
                assert element.proposal_fingerprint is None
            else:
                assert element.proposal_fingerprint is not None
                assert element.semantic_validation_item_fingerprint is not None
                assert element.approval_decision_fingerprint is not None
                assert element.generated_element_fingerprint is None
