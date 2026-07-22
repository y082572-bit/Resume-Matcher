"""``cv_content_proposal_replay``: pure replay/staleness for P5b results."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_generation import ApprovedFactPayloadSnapshot, GenerationContext
from app.schemas.cv_content_proposal import (
    LlmModelConfiguration,
    ProposalFreshnessStatus,
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
from app.schemas.cv_content_generation import CV_CONTENT_GENERATION_SCHEMA_VERSION
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    generate_cv_content_draft,
)
from app.services.cv_content_generation_policy import (
    CV_CONTENT_GENERATION_POLICY_VERSION,
    DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.cv_content_generation_replay import replay_input_from_generation_result
from app.services.cv_content_proposal_builder import generate_controlled_cv_content_proposals
from app.services.cv_content_proposal_replay import (
    evaluate_proposal_freshness,
    is_cv_content_proposal_stale,
    replay_input_from_proposal_result,
    stale_fields,
)
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_target_context_fingerprint,
)
from app.schemas.cv_content_plan import CV_CONTENT_PLAN_SCHEMA_VERSION
from app.services.cv_content_plan_policy import CV_CONTENT_POLICY_VERSION
from app.schemas.cv_content_proposal import (
    PROPOSAL_GENERATION_POLICY_VERSION,
    PROPOSAL_GENERATION_SCHEMA_VERSION,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-1",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(*, target_context, fact_type, target_scope, requested_operation) -> FactSelectionDecision:
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


def _model_configuration(**overrides) -> LlmModelConfiguration:
    values = dict(
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
    values.update(overrides)
    return LlmModelConfiguration(**values)


def _proposal_context(**overrides) -> ProposalGenerationContext:
    values = dict(
        locale="pl-PL",
        tone_profile="professional",
        maximum_character_count=500,
        maximum_sentence_count=5,
        target_role_context=TargetRoleContext(),
        prompt_template_version="controlled-rephrase-prompt-v1",
        generation_policy_version="cv-content-proposal-policy-v1",
        immutable_constraint_policy_version="immutable-constraint-policy-v1",
        retry_policy_version="proposal-model-retry-policy-v1",
        model_configuration=_model_configuration(),
    )
    values.update(overrides)
    return ProposalGenerationContext(**values)


class _SuccessAdapter:
    async def generate(self, *, request, model_configuration):
        from app.schemas.cv_content_proposal import ControlledProposalResponse, LlmAdapterResponse

        return LlmAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledProposalResponse(
                proposal_text="Achieved a 30% revenue increase within one year.",
                operation=request.operation,
                source_fact_id=request.source_fact_id,
            ),
            raw_response_text="raw-ok",
            finish_reason="stop",
        )


async def _build_result_and_inputs(proposal_context=None):
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    entity_types = {d.entity_id: EntityType.SKILL}
    build_result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types=entity_types)
    plan = build_result.plan
    decisions_by_fp = {d.decision_fingerprint: d}
    payload = _payload_for(d, "Grew revenue by 30% in one year.")
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
    proposal_context = proposal_context or _proposal_context()
    result = await generate_controlled_cv_content_proposals(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        fact_payloads=payload_set,
        proposal_context=proposal_context,
        llm_adapter=_SuccessAdapter(),
    )
    return plan, p5a_replay, proposal_context, result


@pytest.mark.asyncio
async def test_identical_replay_input_is_fresh() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    fact_payload_set_fingerprint = p5a_replay.fact_payload_set_fingerprint
    replay_input = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
    freshness = evaluate_proposal_freshness(replay_input, replay_input)
    assert freshness.freshness_status == ProposalFreshnessStatus.FRESH
    assert freshness.stale_fields == ()


@pytest.mark.asyncio
async def test_missing_current_is_not_verified() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    replay_input = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=p5a_replay.fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
    freshness = evaluate_proposal_freshness(replay_input, None)
    assert freshness.freshness_status == ProposalFreshnessStatus.FRESHNESS_NOT_VERIFIED


@pytest.mark.asyncio
async def test_changed_plan_fingerprint_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=p5a_replay.fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
    current = previous.model_copy(update={"content_plan_fingerprint": "0" * 64})
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "content_plan_fingerprint" in freshness.stale_fields
    assert is_cv_content_proposal_stale(previous, current) is True


@pytest.mark.asyncio
async def test_changed_p5a_result_fingerprint_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=p5a_replay.fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
    current = previous.model_copy(update={"p5a_result_fingerprint": "0" * 64})
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "p5a_result_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changed_payload_set_fingerprint_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=p5a_replay.fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
    current = previous.model_copy(update={"fact_payload_set_fingerprint": "0" * 64})
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "fact_payload_set_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changed_provider_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(model_configuration=_model_configuration(provider="other-provider"))
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "proposal_context_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changed_model_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(model_configuration=_model_configuration(model_identifier="other-model"))
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changed_temperature_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(model_configuration=_model_configuration(temperature=0.9))
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changed_timeout_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(model_configuration=_model_configuration(timeout_seconds=99.0))
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changed_retry_policy_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(retry_policy_version="proposal-model-retry-policy-v2")
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changed_target_context_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    other_context = _proposal_context(target_role_context=TargetRoleContext(target_job_title="Manager"))
    _, _, _, other_result = await _build_result_and_inputs(proposal_context=other_context)
    current = _replay_snapshot(plan, p5a_replay, other_context, other_result)
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "target_role_context_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changed_proposal_text_is_stale() -> None:
    plan, p5a_replay, proposal_context, result = await _build_result_and_inputs()
    previous = _replay_snapshot(plan, p5a_replay, proposal_context, result)
    current = previous.model_copy(update={"proposal_fingerprints": ("f" * 64,)})
    freshness = evaluate_proposal_freshness(previous, current)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "proposal_fingerprints" in freshness.stale_fields


def test_stale_fields_are_deterministic() -> None:
    from app.schemas.cv_content_proposal import CvContentProposalReplayInput

    base = CvContentProposalReplayInput(
        content_plan_fingerprint="a" * 64,
        content_plan_schema_version="v1",
        content_policy_version="v1",
        p5a_generation_schema_version="v1",
        p5a_generation_policy_version="v1",
        p5a_result_fingerprint="b" * 64,
        p5a_replay_input_fingerprint="c" * 64,
        proposal_schema_version="v1",
        proposal_policy_version="v1",
        fact_payload_set_fingerprint="d" * 64,
        proposal_context_fingerprint="e" * 64,
        target_role_context_fingerprint="f" * 64,
    )
    changed = base.model_copy(update={"content_plan_fingerprint": "z" * 64, "proposal_policy_version": "v2"})
    result_1 = stale_fields(base, changed)
    result_2 = stale_fields(base, changed)
    assert result_1 == result_2
    assert result_1 == ("content_plan_fingerprint", "proposal_policy_version")


def _replay_snapshot(plan, p5a_replay, proposal_context, result):
    return replay_input_from_proposal_result(
        plan=plan,
        content_policy_version=CV_CONTENT_POLICY_VERSION,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        p5a_replay_input=p5a_replay,
        proposal_schema_version=PROPOSAL_GENERATION_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_GENERATION_POLICY_VERSION,
        fact_payload_set_fingerprint=p5a_replay.fact_payload_set_fingerprint,
        proposal_context=proposal_context,
        result=result,
    )
