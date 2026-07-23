"""A strategy-identity-only change (same visible CV facts/text, different
``RoleStrategyContext``) must invalidate the entire P5 downstream chain:
P4 -> P5a -> P5b -> P5.5 Step 1 -> P5.5 Step 2.

Models the real end-to-end pipeline the same way
``tests/integration/test_p4_p5a_p5b_p55_integration.py`` does, but builds
the P4 plan through the mandatory ``build_role_strategy_integrated_cv_content_plan``
entrypoint, then swaps only the plan's strategy identity fields (never the
visible fact content) before re-deriving each stage's "current" replay
input -- proving the change propagates through the whole chain without a
database, an LLM, or any downstream file modification.
"""

from __future__ import annotations

import pytest

from app.schemas.cv_content_approval import (
    ControlledSemanticValidationResponse,
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
    GenerationFreshnessStatus,
)
from app.schemas.cv_content_plan import CvContentPlanMode
from app.schemas.cv_content_proposal import (
    ControlledProposalResponse,
    LlmAdapterResponse,
    LlmModelConfiguration,
    ProposalFreshnessStatus,
    ProposalGenerationContext,
    TargetRoleContext,
)
from app.schemas.truth_entity import EntityType
from app.services.cv_content_approval_builder import build_approved_cv_content
from app.services.cv_content_approval_replay import (
    evaluate_approved_content_freshness,
    evaluate_semantic_validation_freshness,
    replay_input_from_approved_content_result,
    replay_input_from_semantic_validation_result,
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
from app.services.cv_content_generation_replay import (
    evaluate_generation_freshness,
    replay_input_from_generation_result,
)
from app.services.cv_content_plan_builder import compute_content_plan_fingerprint
from app.services.cv_content_plan_integrated_builder import build_role_strategy_integrated_cv_content_plan
from app.services.cv_content_proposal_builder import generate_controlled_cv_content_proposals
from app.services.cv_content_proposal_replay import evaluate_proposal_freshness, replay_input_from_proposal_result
from app.services.cv_content_semantic_validation_builder import validate_cv_content_proposals
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_candidate, make_fact


PROPOSAL_SCHEMA_VERSION = "cv-content-proposal-schema-v1"
PROPOSAL_POLICY_VERSION = "cv-content-proposal-policy-v1"


def _payload_for(decision_person_id, decision_entity_id, decision_fact_id, decision_fact_type, decision_fact_revision, decision_content_fp, value_json):
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=decision_person_id,
        entity_id=decision_entity_id,
        fact_id=decision_fact_id,
        fact_type=decision_fact_type,
        fact_revision=decision_fact_revision,
        fact_content_fingerprint=decision_content_fp,
        value_json=value_json,
        normalized_value_json=value_json,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    return ApprovedFactPayloadSnapshot(
        person_entity_id=decision_person_id,
        entity_id=decision_entity_id,
        fact_id=decision_fact_id,
        fact_type=decision_fact_type,
        fact_revision=decision_fact_revision,
        fact_content_fingerprint=decision_content_fp,
        value_json=value_json,
        normalized_value_json=value_json,
        payload_fingerprint=fingerprint,
    )


def _proposal_context() -> ProposalGenerationContext:
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
    return ProposalGenerationContext(
        locale="en",
        tone_profile="professional",
        maximum_character_count=500,
        maximum_sentence_count=5,
        target_role_context=TargetRoleContext(target_job_title="Synthetic Sales Manager"),
        prompt_template_version="controlled-rephrase-prompt-v1",
        generation_policy_version="cv-content-proposal-policy-v1",
        immutable_constraint_policy_version="immutable-constraint-policy-v1",
        retry_policy_version="proposal-model-retry-policy-v1",
        model_configuration=model_configuration,
    )


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
        locale="en",
        validation_prompt_version="controlled-semantic-validation-prompt-v1",
        validation_policy_version="cv-content-semantic-validation-policy-v1",
        retry_policy_version="semantic-validator-retry-policy-v1",
        validator_model_configuration=model_configuration,
    )


class _FakeProposalAdapter:
    async def generate(self, *, request, model_configuration):  # noqa: ANN001
        text = "Managed a synthetic sales pipeline with documented improvement."
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
    async def validate(self, *, request, model_configuration):  # noqa: ANN001
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


async def _build_integrated_plan_and_pipeline():
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.schemas.truth_fact import PermissionStatus, TransformationOperation, TruthPermissionRead

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fixture = await build_ready_fixture()
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type="SUMMARY",
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    permission = TruthPermissionRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=now,
        updated_at=now,
        archived_at=None,
        permission_id=uuid4(),
        fact_id=fact.fact_id,
        target_scope="SUMMARY",
        allowed_operations=[TransformationOperation.CONTROLLED_REPHRASE],
        constraints_json={},
        status=PermissionStatus.ACTIVE,
        approved_by=None,
        approved_at=None,
        valid_from=None,
        valid_until=None,
    )
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[
            make_candidate(
                fact,
                requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
                requested_target_scope="SUMMARY",
            )
        ],
        permissions=(permission,),
        evaluation_time=now,
    )
    entity_types = {fact.entity_id: EntityType.SKILL}
    build_result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
    )
    assert build_result.outcome.value == "BUILT"
    plan = build_result.plan
    decision = strategy_result.decisions[0].base_decision
    decisions_by_fp = {decision.decision_fingerprint: decision}

    payload = _payload_for(
        decision.person_entity_id,
        decision.entity_id,
        decision.fact_id,
        decision.fact_type,
        decision.fact_revision,
        decision.fact_content_fingerprint,
        "Managed a synthetic sales pipeline.",
    )
    payload_set = ApprovedFactPayloadSet(
        payloads=(payload,), payload_set_fingerprint=compute_fact_payload_set_fingerprint([payload])
    )
    generation_context = GenerationContext(
        locale="en",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )

    from app.services.cv_content_plan_replay import replay_input_from_plan

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
        semantic_validator=_FakeSemanticValidator(),
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

    approved_result = build_approved_cv_content(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        p5b_result=p5b_result,
        proposal_generation_context=proposal_context,
        semantic_validation_result=semantic_result,
        approval_decisions=(),
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        fact_payloads=payload_set,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        current_p5b_replay_input=p5b_replay,
        current_semantic_validation_replay_input=semantic_replay,
    )
    approved_replay = replay_input_from_approved_content_result(
        semantic_validation_replay_input=semantic_replay,
        approval_decisions=(),
        result=approved_result,
    )

    return dict(
        plan=plan,
        payload_set=payload_set,
        generation_context=generation_context,
        proposal_context=proposal_context,
        p5a_result=p5a_result,
        p5a_replay=p5a_replay,
        p5b_result=p5b_result,
        p5b_replay=p5b_replay,
        semantic_result=semantic_result,
        semantic_replay=semantic_replay,
        approved_result=approved_result,
        approved_replay=approved_replay,
    )


def _plan_with_swapped_strategy_identity(plan):
    """A structurally different (but still internally consistent) plan
    with the SAME visible sections/facts -- only the strategy identity
    fingerprints (and therefore ``content_plan_fingerprint``) differ."""

    new_context_fp = "9" * 64
    new_content_plan_fp = compute_content_plan_fingerprint(
        schema_version=plan.schema_version,
        target_context_fingerprint=plan.target_context_fingerprint,
        job_or_application_id=plan.job_or_application_id,
        content_policy_version=plan.content_policy_version,
        selection_policy_version=plan.selection_policy_version,
        transformation_policy_version=plan.transformation_policy_version,
        budget_profile_fingerprint=plan.budget_profile_fingerprint,
        source_decision_fingerprints=plan.source_decision_fingerprints,
        section_plan_fingerprints=[s.section_plan_fingerprint for s in plan.sections],
        pending_approval_fingerprints=[p.pending_approval_fingerprint for p in plan.pending_approvals],
        omission_fingerprints=[o.omission_fingerprint for o in plan.omitted_facts],
        conflict_fingerprints=[c.conflict_fingerprint for c in plan.conflicts],
        career_positioning_snapshot_fingerprint=plan.career_positioning_snapshot_fingerprint,
        plan_mode=CvContentPlanMode.ROLE_STRATEGY_INTEGRATED,
        role_strategy_context_fingerprint=new_context_fp,
        strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=plan.strategy_integration_policy_version,
    )
    return plan.model_copy(
        update={
            "role_strategy_context_fingerprint": new_context_fp,
            "content_plan_fingerprint": new_content_plan_fp,
        }
    )


@pytest.mark.asyncio
async def test_strategy_identity_change_alone_leaves_visible_sections_identical() -> None:
    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    assert plan_after.sections == ctx["plan"].sections
    assert plan_after.content_plan_fingerprint != ctx["plan"].content_plan_fingerprint


@pytest.mark.asyncio
async def test_p4_replay_is_stale_after_strategy_identity_change() -> None:
    from app.services.cv_content_plan_replay import replay_input_from_plan
    from app.services.cv_content_plan_validator import validate_cv_content_plan

    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    previous_plan_replay = replay_input_from_plan(ctx["plan"])
    current_plan_replay = replay_input_from_plan(plan_after)
    assert previous_plan_replay != current_plan_replay
    assert "content_plan_fingerprint" not in type(previous_plan_replay).model_fields  # sanity: replay tracks parts, not the plan fp itself
    assert previous_plan_replay.role_strategy_context_fingerprint != current_plan_replay.role_strategy_context_fingerprint


@pytest.mark.asyncio
async def test_p5a_becomes_stale_after_strategy_identity_change() -> None:
    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    current_p5a_replay = replay_input_from_generation_result(
        plan=plan_after,
        payload_set=ctx["payload_set"],
        generation_context=ctx["generation_context"],
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=ctx["p5a_result"],
    )
    freshness = evaluate_generation_freshness(ctx["p5a_replay"], current_p5a_replay)
    assert freshness.freshness_status == GenerationFreshnessStatus.STALE
    assert "content_plan_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_p5b_becomes_stale_after_strategy_identity_change() -> None:
    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    current_p5b_replay = replay_input_from_proposal_result(
        plan=plan_after,
        content_policy_version=plan_after.content_policy_version,
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=ctx["generation_context"].generation_policy_version,
        p5a_replay_input=ctx["p5a_replay"],
        proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=ctx["payload_set"].payload_set_fingerprint,
        proposal_context=ctx["proposal_context"],
        result=ctx["p5b_result"],
    )
    freshness = evaluate_proposal_freshness(ctx["p5b_replay"], current_p5b_replay)
    assert freshness.freshness_status == ProposalFreshnessStatus.STALE
    assert "content_plan_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_p55_step1_becomes_stale_after_strategy_identity_change() -> None:
    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    current_semantic_replay = replay_input_from_semantic_validation_result(
        plan=plan_after,
        p5a_replay_input=ctx["p5a_replay"],
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=ctx["generation_context"].generation_policy_version,
        p5b_replay_input=ctx["p5b_replay"],
        p5b_proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        p5b_proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=ctx["payload_set"].payload_set_fingerprint,
        result=ctx["semantic_result"],
    )
    freshness = evaluate_semantic_validation_freshness(ctx["semantic_replay"], current_semantic_replay)
    assert freshness.freshness_status == SemanticValidationFreshnessStatus.STALE
    assert "content_plan_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_p55_step2_becomes_stale_after_strategy_identity_change() -> None:
    ctx = await _build_integrated_plan_and_pipeline()
    plan_after = _plan_with_swapped_strategy_identity(ctx["plan"])
    current_semantic_replay = replay_input_from_semantic_validation_result(
        plan=plan_after,
        p5a_replay_input=ctx["p5a_replay"],
        p5a_generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        p5a_generation_policy_version=ctx["generation_context"].generation_policy_version,
        p5b_replay_input=ctx["p5b_replay"],
        p5b_proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        p5b_proposal_policy_version=PROPOSAL_POLICY_VERSION,
        fact_payload_set_fingerprint=ctx["payload_set"].payload_set_fingerprint,
        result=ctx["semantic_result"],
    )
    current_approved_replay = replay_input_from_approved_content_result(
        semantic_validation_replay_input=current_semantic_replay,
        approval_decisions=(),
        result=ctx["approved_result"],
    )
    freshness = evaluate_approved_content_freshness(ctx["approved_replay"], current_approved_replay)
    assert freshness.freshness_status.value == "STALE"
    assert "semantic_validation_replay_input_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_p5_stages_never_require_direct_prep4_fields() -> None:
    """P5a/P5b/P5.5 never import PRE-P4 or strategy schemas directly -- the
    entire strategy-identity dependency is carried transitively through
    ``content_plan_fingerprint``, never a direct import."""

    import inspect

    from app.services import (
        cv_content_approval_builder,
        cv_content_approval_replay,
        cv_content_generation_builder,
        cv_content_generation_replay,
        cv_content_proposal_builder,
        cv_content_proposal_replay,
        cv_content_semantic_validation_builder,
    )

    for module in (
        cv_content_generation_builder,
        cv_content_generation_replay,
        cv_content_proposal_builder,
        cv_content_proposal_replay,
        cv_content_semantic_validation_builder,
        cv_content_approval_builder,
        cv_content_approval_replay,
    ):
        source = inspect.getsource(module)
        assert "strategy_fact_selection" not in source
        assert "role_strategy_prep4_revalidation" not in source
        assert "RoleStrategyContext" not in source
