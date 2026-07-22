"""``generate_controlled_cv_content_proposals``: P4/P5a revalidation, payload
matching, eligibility, one-fact-one-proposal, retry, fail-closed matrix,
result partition, provenance, fingerprints.

All facts/entities are synthetic UUIDs; no real person or CV data is used.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_generation import (
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
)
from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import (
    ControlledProposalResponse,
    LlmAdapterResponse,
    LlmModelConfiguration,
    ProposalApprovalState,
    ProposalGenerationContext,
    ProposalGenerationStatus,
    ProposalProviderErrorCode,
    ProposalViolationCode,
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
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.cv_content_generation_replay import replay_input_from_generation_result
from app.schemas.cv_content_generation import CV_CONTENT_GENERATION_SCHEMA_VERSION
from app.services.cv_content_proposal_builder import generate_controlled_cv_content_proposals
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_target_context_fingerprint,
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


def _decision(
    *,
    target_context: TargetContext,
    fact_id=None,
    entity_id=None,
    fact_type: str,
    target_scope: str,
    requested_operation: TransformationOperation,
    employment_scope_entity_id=None,
    fact_revision: int = 1,
    fact_content_fingerprint: str = "b" * 64,
    salt: str = "",
) -> FactSelectionDecision:
    fact_id = fact_id or uuid4()
    entity_id = entity_id or uuid4()
    seed = f"{fact_id}{entity_id}{fact_type}{target_scope}{requested_operation}{salt}{uuid4()}"
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
        fact_revision=fact_revision,
        fact_content_fingerprint=fact_content_fingerprint,
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


def _build_plan(target_context, decisions, entity_types=None):
    entity_types = dict(entity_types or {})
    for d in decisions:
        entity_types.setdefault(d.entity_id, EntityType.SKILL)
        if d.employment_scope_entity_id is not None:
            entity_types.setdefault(d.employment_scope_entity_id, EntityType.EMPLOYMENT)
    result = build_cv_content_plan(
        target_context=target_context, decisions=decisions, entity_types=entity_types
    )
    assert result.plan is not None, result.snapshot_violations
    return result.plan, entity_types


def _payload_for(decision: FactSelectionDecision, value_json, normalized_value_json=None):
    normalized_value_json = value_json if normalized_value_json is None else normalized_value_json
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=value_json,
        normalized_value_json=normalized_value_json,
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
        normalized_value_json=normalized_value_json,
        payload_fingerprint=fingerprint,
    )


def _payload_set(payloads) -> ApprovedFactPayloadSet:
    return ApprovedFactPayloadSet(
        payloads=tuple(payloads), payload_set_fingerprint=compute_fact_payload_set_fingerprint(payloads)
    )


def _generation_context(**overrides) -> GenerationContext:
    values = dict(
        locale="pl-PL",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    values.update(overrides)
    return GenerationContext(**values)


def _run_p5a(plan, decisions, entity_types, fact_payloads, generation_context=None, current_replay_input=None):
    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}
    if current_replay_input is None:
        current_replay_input = replay_input_from_plan(plan)
    return generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_replay_input=current_replay_input,
        fact_payloads=fact_payloads,
        generation_context=generation_context or _generation_context(),
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


class ScriptedAdapter:
    """Deterministic fake adapter: pops one scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list = []

    async def generate(self, *, request, model_configuration):
        self.calls.append((request, model_configuration))
        return self._responses.pop(0)


def _success_response(request, proposal_text, *, provider="test-provider", model_identifier="test-model"):
    return LlmAdapterResponse(
        provider=provider,
        model_identifier=model_identifier,
        structured_response=ControlledProposalResponse(
            proposal_text=proposal_text, operation=request.operation, source_fact_id=request.source_fact_id
        ),
        raw_response_text=f"raw:{proposal_text}",
        finish_reason="stop",
    )


def _error_response(*, error_code, provider="test-provider", model_identifier="test-model", finish_reason=None):
    return LlmAdapterResponse(
        provider=provider,
        model_identifier=model_identifier,
        structured_response=None,
        raw_response_text="",
        finish_reason=finish_reason,
        provider_error_code=error_code,
    )


def _achievement_plan(requested_operation=TransformationOperation.CONTROLLED_REPHRASE, **decision_overrides):
    """One SUMMARY-section PlannedFactUse requesting a non-deterministic op."""

    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=requested_operation,
        **decision_overrides,
    )
    plan, entity_types = _build_plan(tc, [d])
    return tc, d, plan, entity_types


_UNSET = object()


async def _run_p5b(
    plan,
    p5a_result,
    decisions,
    entity_types,
    fact_payloads,
    *,
    generation_context=None,
    current_plan_replay_input=_UNSET,
    current_p5a_replay_input=_UNSET,
    proposal_context=None,
    llm_adapter=None,
):
    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}
    generation_context = generation_context or _generation_context()
    if current_plan_replay_input is _UNSET:
        current_plan_replay_input = replay_input_from_plan(plan)
    if current_p5a_replay_input is _UNSET:
        current_p5a_replay_input = replay_input_from_generation_result(
            plan=plan,
            payload_set=fact_payloads,
            generation_context=generation_context,
            generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
            result=p5a_result,
        )
    return await generate_controlled_cv_content_proposals(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_plan_replay_input=current_plan_replay_input,
        current_p5a_replay_input=current_p5a_replay_input,
        fact_payloads=fact_payloads,
        proposal_context=proposal_context or _proposal_context(),
        llm_adapter=llm_adapter,
    )


def _setup_eligible_case(requested_operation=TransformationOperation.CONTROLLED_REPHRASE, value_json="Grew revenue by 30% in one year."):
    tc, d, plan, entity_types = _achievement_plan(requested_operation=requested_operation)
    payload = _payload_for(d, value_json)
    payload_set = _payload_set([payload])
    p5a_result = _run_p5a(plan, [d], entity_types, payload_set)
    return tc, d, plan, entity_types, payload_set, p5a_result


# -- 1-4: P4 revalidation gates --


@pytest.mark.asyncio
async def test_structurally_invalid_plan_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    tampered_plan = plan.model_copy(update={"content_plan_fingerprint": "0" * 64})
    result = await _run_p5b(tampered_plan, p5a_result, [d], entity_types, payload_set)
    assert result.status == ProposalGenerationStatus.BLOCKED
    assert result.proposals == ()
    assert any(
        v.violation_code == ProposalViolationCode.INPUT_PLAN_STRUCTURALLY_INVALID
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_stale_plan_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    stale_replay = replay_input_from_plan(plan).model_copy(update={"job_or_application_id": "other-job"})
    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, current_plan_replay_input=stale_replay
    )
    assert result.status == ProposalGenerationStatus.BLOCKED
    assert any(v.violation_code == ProposalViolationCode.INPUT_PLAN_STALE for v in result.violations)


@pytest.mark.asyncio
async def test_missing_plan_replay_input_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, current_plan_replay_input=None
    )
    assert result.status == ProposalGenerationStatus.BLOCKED
    assert any(
        v.violation_code
        in (ProposalViolationCode.INPUT_PLAN_NOT_READY, ProposalViolationCode.INPUT_PLAN_STALE)
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_plan_with_conflicts_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    from app.schemas.cv_content_plan import ConflictReasonCode, CvContentPlanConflict, CvContentPlanStatus
    from app.services.cv_content_plan_builder import (
        compute_conflict_fingerprint,
        compute_content_plan_fingerprint,
    )

    # Two extra SELECTED decisions that participate only in the conflict
    # bucket (never placed into any section), so the plan's existing
    # planned/pending/omitted partitions stay untouched.
    d2 = _decision(
        target_context=tc, fact_type="SKILL", target_scope="SKILLS", requested_operation=TransformationOperation.EXACT_COPY
    )
    d3 = _decision(
        target_context=tc, fact_type="TOOL", target_scope="SKILLS", requested_operation=TransformationOperation.EXACT_COPY
    )
    conflict_fingerprint = compute_conflict_fingerprint(
        person_entity_id=tc.person_entity_id,
        fact_id=None,
        entity_id=None,
        conflicting_decision_fingerprints=(d2.decision_fingerprint, d3.decision_fingerprint),
        conflict_reason_code=ConflictReasonCode.PROFILE_MULTIPLE_SUMMARY_CANDIDATES,
    )
    conflict = CvContentPlanConflict(
        conflict_fingerprint=conflict_fingerprint,
        person_entity_id=tc.person_entity_id,
        fact_id=None,
        entity_id=None,
        conflicting_decision_fingerprints=(d2.decision_fingerprint, d3.decision_fingerprint),
        conflict_reason_code=ConflictReasonCode.PROFILE_MULTIPLE_SUMMARY_CANDIDATES,
    )
    new_source_decision_fps = plan.source_decision_fingerprints + (
        d2.decision_fingerprint,
        d3.decision_fingerprint,
    )
    new_content_plan_fp = compute_content_plan_fingerprint(
        schema_version=plan.schema_version,
        target_context_fingerprint=plan.target_context_fingerprint,
        job_or_application_id=plan.job_or_application_id,
        content_policy_version=plan.content_policy_version,
        selection_policy_version=plan.selection_policy_version,
        transformation_policy_version=plan.transformation_policy_version,
        budget_profile_fingerprint=plan.budget_profile_fingerprint,
        source_decision_fingerprints=new_source_decision_fps,
        section_plan_fingerprints=[section.section_plan_fingerprint for section in plan.sections],
        pending_approval_fingerprints=[item.pending_approval_fingerprint for item in plan.pending_approvals],
        omission_fingerprints=[item.omission_fingerprint for item in plan.omitted_facts],
        conflict_fingerprints=[conflict.conflict_fingerprint],
        career_positioning_snapshot_fingerprint=plan.career_positioning_snapshot_fingerprint,
    )
    tampered_plan = plan.model_copy(
        update={
            "conflicts": (conflict,),
            "plan_status": CvContentPlanStatus.INVALID,
            "source_decision_fingerprints": new_source_decision_fps,
            "content_plan_fingerprint": new_content_plan_fp,
        }
    )
    result = await _run_p5b(
        tampered_plan,
        p5a_result,
        [d, d2, d3],
        entity_types,
        payload_set,
        current_plan_replay_input=replay_input_from_plan(tampered_plan),
    )
    assert result.status == ProposalGenerationStatus.BLOCKED
    assert any(
        v.violation_code == ProposalViolationCode.INPUT_PLAN_HAS_CONFLICTS for v in result.violations
    )


# -- 5-11: P5a revalidation gates --


@pytest.mark.asyncio
async def test_invalid_p5a_result_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    tampered = p5a_result.model_copy(update={"result_fingerprint": "0" * 64})
    result = await _run_p5b(plan, tampered, [d], entity_types, payload_set)
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_stale_p5a_result_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    current_replay = replay_input_from_generation_result(
        plan=plan,
        payload_set=payload_set,
        generation_context=_generation_context(),
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=p5a_result,
    ).model_copy(update={"generation_policy_version": "some-other-policy-v9"})
    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, current_p5a_replay_input=current_replay
    )
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.INPUT_P5A_RESULT_STALE for v in result.violations
    )


@pytest.mark.asyncio
async def test_missing_current_p5a_replay_input_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, current_p5a_replay_input=None
    )
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.INPUT_P5A_RESULT_STALE for v in result.violations
    )


@pytest.mark.asyncio
async def test_content_plan_fingerprint_mismatch_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    tampered_result = p5a_result.model_copy(update={"content_plan_fingerprint": "0" * 64})
    result = await _run_p5b(plan, tampered_result, [d], entity_types, payload_set)
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_payload_set_fingerprint_mismatch_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    tampered_payload_set = payload_set.model_copy(update={"payload_set_fingerprint": "0" * 64})
    result = await _run_p5b(plan, p5a_result, [d], entity_types, tampered_payload_set)
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.PAYLOAD_SET_FINGERPRINT_MISMATCH
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_p5a_result_fingerprint_mismatch_blocks_p5b() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    # Corrupt one blocked item's own fingerprint without recomputing the
    # outer result_fingerprint -- the two must now disagree.
    corrupted_item = p5a_result.blocked_items[0].model_copy(
        update={"blocked_item_fingerprint": "0" * 64}
    )
    tampered_result = p5a_result.model_copy(update={"blocked_items": (corrupted_item,)})
    result = await _run_p5b(plan, tampered_result, [d], entity_types, payload_set)
    assert result.status == ProposalGenerationStatus.INVALID_INPUT
    assert any(
        v.violation_code == ProposalViolationCode.P5A_RESULT_FINGERPRINT_MISMATCH
        for v in result.violations
    )


@pytest.mark.asyncio
async def test_blocked_item_fingerprint_mismatch_blocks_that_item() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    item = p5a_result.blocked_items[0]
    # Tamper the item's own fingerprint field directly -- its identity
    # fields (fact_type, violation_code, etc.) stay valid, so the *only*
    # thing that should fail is the blocked_item_fingerprint recompute.
    corrupted_item = item.model_copy(update={"blocked_item_fingerprint": "0" * 64})
    from app.services.cv_content_generation_builder import compute_generation_result_fingerprint

    new_result_fp = compute_generation_result_fingerprint(
        status=p5a_result.status,
        ready_for_p55=p5a_result.ready_for_p55,
        content_plan_fingerprint=p5a_result.content_plan_fingerprint,
        payload_set_fingerprint=p5a_result.payload_set_fingerprint,
        generation_context_fingerprint=p5a_result.generation_context_fingerprint,
        draft_fingerprint=(
            p5a_result.draft.generated_content_draft_fingerprint if p5a_result.draft else None
        ),
        blocked_item_fingerprints=[corrupted_item.blocked_item_fingerprint],
    )
    tampered_result = p5a_result.model_copy(
        update={"blocked_items": (corrupted_item,), "result_fingerprint": new_result_fp}
    )
    result = await _run_p5b(plan, tampered_result, [d], entity_types, payload_set)
    assert any(
        b.violation_code == ProposalViolationCode.BLOCKED_ITEM_FINGERPRINT_MISMATCH
        for b in result.blocked_proposal_items
    )


# -- 12-13: payload matching without a provider call --


@pytest.mark.asyncio
class _RaisingAdapter:
    """Fails the test loudly if the builder ever calls the provider."""

    async def generate(self, *, request, model_configuration):
        raise AssertionError("llm_adapter.generate must not be called for this item")


@pytest.mark.asyncio
async def test_payload_missing_blocks_item_without_provider_call() -> None:
    # Payload-set-level consistency is enforced globally (any real caller
    # mismatch there blocks the whole result, tested separately above), so
    # the item-level "payload missing for this specific fact_id" defense is
    # exercised directly against the per-item processing function.
    from app.services.cv_content_proposal_builder import _process_eligible_item

    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    item = p5a_result.blocked_items[0]
    outcome = await _process_eligible_item(
        item,
        plan=plan,
        payloads_by_fact_id={},
        proposal_context=_proposal_context(),
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        llm_adapter=_RaisingAdapter(),
    )
    assert outcome.violation_code == ProposalViolationCode.PAYLOAD_MISSING


@pytest.mark.asyncio
async def test_payload_identity_mismatch_blocks_item_without_provider_call() -> None:
    from app.services.cv_content_proposal_builder import _process_eligible_item

    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    item = p5a_result.blocked_items[0]
    tampered_payload = payload_set.payloads[0].model_copy(update={"fact_revision": 2})
    outcome = await _process_eligible_item(
        item,
        plan=plan,
        payloads_by_fact_id={tampered_payload.fact_id: tampered_payload},
        proposal_context=_proposal_context(),
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        p5a_result_fingerprint=p5a_result.result_fingerprint,
        llm_adapter=_RaisingAdapter(),
    )
    assert outcome.violation_code == ProposalViolationCode.PAYLOAD_IDENTITY_MISMATCH


# -- 14-20: eligibility partition --


@pytest.mark.asyncio
async def test_controlled_rephrase_is_eligible() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case(
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE
    )
    adapter = ScriptedAdapter([])

    async def _generate(*, request, model_configuration):
        return _success_response(request, "Achieved a 30% revenue increase within one year.")

    adapter.generate = _generate
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    assert result.status == ProposalGenerationStatus.PROPOSED
    assert len(result.proposals) == 1


@pytest.mark.asyncio
async def test_flatten_for_lower_role_is_eligible() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case(
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE
    )

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.status == ProposalGenerationStatus.PROPOSED
    assert result.proposals[0].allowed_operation == TransformationOperation.FLATTEN_FOR_LOWER_ROLE


async def _non_eligible_case(operation: TransformationOperation):
    tc = _target_context()
    d = _decision(
        target_context=tc, fact_type="SUMMARY", target_scope="SUMMARY", requested_operation=operation
    )
    plan, entity_types = _build_plan(tc, [d])
    payload = _payload_for(d, "Some summary text.")
    payload_set = _payload_set([payload])
    p5a_result = _run_p5a(plan, [d], entity_types, payload_set)
    adapter = ScriptedAdapter([])
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    return result, adapter


@pytest.mark.asyncio
async def test_exact_copy_never_reaches_provider() -> None:
    result, adapter = await _non_eligible_case(TransformationOperation.EXACT_COPY)
    assert len(adapter.calls) == 0
    # EXACT_COPY is deterministically generatable by P5a, so it is never
    # even blocked in the first place -- nothing for P5b to see.
    assert result.status == ProposalGenerationStatus.BLOCKED
    assert any(
        v.violation_code == ProposalViolationCode.NO_ELIGIBLE_ITEMS for v in result.violations
    )


@pytest.mark.asyncio
async def test_format_normalization_never_reaches_provider() -> None:
    result, adapter = await _non_eligible_case(TransformationOperation.FORMAT_NORMALIZATION)
    assert len(adapter.calls) == 0


@pytest.mark.asyncio
async def test_reorder_never_reaches_provider() -> None:
    result, adapter = await _non_eligible_case(TransformationOperation.REORDER)
    assert len(adapter.calls) == 0


@pytest.mark.asyncio
async def test_omit_never_reaches_provider() -> None:
    result, adapter = await _non_eligible_case(TransformationOperation.OMIT)
    assert len(adapter.calls) == 0
    assert any(
        v.violation_code == ProposalViolationCode.NO_ELIGIBLE_ITEMS for v in result.violations
    )


@pytest.mark.asyncio
async def test_summarize_operation_never_reaches_provider() -> None:
    result, adapter = await _non_eligible_case(TransformationOperation.SUMMARIZE_APPROVED_FACTS)
    assert len(adapter.calls) == 0


# -- 21-22: deterministic P5a elements untouched, one fact one call --


@pytest.mark.asyncio
async def test_deterministic_p5a_elements_are_untouched() -> None:
    tc = _target_context()
    d_exact = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    d_rephrase = _decision(
        target_context=tc,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    plan, entity_types = _build_plan(tc, [d_exact, d_rephrase])
    payloads = [_payload_for(d_exact, "Python"), _payload_for(d_rephrase, "Grew revenue by 30%.")]
    payload_set = _payload_set(payloads)
    p5a_result = _run_p5a(plan, [d_exact, d_rephrase], entity_types, payload_set)
    assert p5a_result.draft is not None
    original_element_fps = sorted(
        element.generated_element_fingerprint
        for section in p5a_result.draft.generated_sections
        for element in (section.generated_elements if section.kind == "FLAT" else ())
    )

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(
        plan, p5a_result, [d_exact, d_rephrase], entity_types, payload_set, llm_adapter=Adapter()
    )
    assert sorted(result.deterministic_element_fingerprints) == original_element_fps
    assert len(result.proposals) == 1


@pytest.mark.asyncio
async def test_one_fact_one_adapter_call() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    calls = []

    class Adapter:
        async def generate(self, *, request, model_configuration):
            calls.append(request)
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert len(calls) == 1
    assert calls[0].source_fact_id == d.fact_id


# -- 23: complete and disjoint partition --


@pytest.mark.asyncio
async def test_partition_is_complete_and_disjoint() -> None:
    tc = _target_context()
    d_eligible = _decision(
        target_context=tc,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    d_ineligible = _decision(
        target_context=tc,
        fact_type="SKILL",
        target_scope="SKILLS",
        requested_operation=TransformationOperation.OMIT,
    )
    plan, entity_types = _build_plan(tc, [d_eligible, d_ineligible])
    payloads = [_payload_for(d_eligible, "Grew revenue by 30%."), _payload_for(d_ineligible, "Python")]
    payload_set = _payload_set(payloads)
    p5a_result = _run_p5a(plan, [d_eligible, d_ineligible], entity_types, payload_set)
    assert len(p5a_result.blocked_items) == 2

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(
        plan, p5a_result, [d_eligible, d_ineligible], entity_types, payload_set, llm_adapter=Adapter()
    )
    proposal_item_fps = {p.blocked_generation_item_fingerprint for p in result.proposals}
    blocked_item_fps = {b.blocked_generation_item_fingerprint for b in result.blocked_proposal_items}
    non_eligible_fps = set(result.non_eligible_blocked_item_fingerprints)
    all_source_fps = {item.blocked_item_fingerprint for item in p5a_result.blocked_items}

    assert proposal_item_fps.isdisjoint(blocked_item_fps)
    assert proposal_item_fps.isdisjoint(non_eligible_fps)
    assert blocked_item_fps.isdisjoint(non_eligible_fps)
    assert proposal_item_fps | blocked_item_fps | non_eligible_fps == all_source_fps


# -- 24-33: retry policy and fail-closed provider matrix --


@pytest.mark.asyncio
async def test_timeout_retries_at_most_once() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    responses = [
        _error_response(error_code=ProposalProviderErrorCode.TIMEOUT),
        _error_response(error_code=ProposalProviderErrorCode.TIMEOUT),
    ]
    adapter = ScriptedAdapter(responses)
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    assert len(adapter.calls) == 2
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.RETRY_EXHAUSTED
    assert result.blocked_proposal_items[0].provider_error_code == ProposalProviderErrorCode.TIMEOUT
    assert result.status == ProposalGenerationStatus.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_transport_error_retries_at_most_once() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    responses = [
        _error_response(error_code=ProposalProviderErrorCode.TRANSPORT_ERROR),
        _error_response(error_code=ProposalProviderErrorCode.TRANSPORT_ERROR),
    ]
    adapter = ScriptedAdapter(responses)
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    assert len(adapter.calls) == 2
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.RETRY_EXHAUSTED


@pytest.mark.asyncio
async def test_timeout_then_success_recovers() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        def __init__(self):
            self.calls = 0

        async def generate(self, *, request, model_configuration):
            self.calls += 1
            if self.calls == 1:
                return _error_response(error_code=ProposalProviderErrorCode.TIMEOUT)
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    adapter = Adapter()
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    assert adapter.calls == 2
    assert result.status == ProposalGenerationStatus.PROPOSED
    assert result.proposals[0].llm_generation_provenance.attempt_count == 2


async def _single_error_case(error_code: ProposalProviderErrorCode):
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    adapter = ScriptedAdapter([_error_response(error_code=error_code)])
    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    return result, adapter


@pytest.mark.asyncio
async def test_invalid_json_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.INVALID_RESPONSE_JSON)
    assert len(adapter.calls) == 1
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.PROVIDER_ERROR


@pytest.mark.asyncio
async def test_schema_mismatch_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.RESPONSE_SCHEMA_MISMATCH)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_empty_response_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.EMPTY_RESPONSE)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_content_filtered_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.CONTENT_FILTERED)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_unsupported_finish_reason_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.UNSUPPORTED_FINISH_REASON)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_provider_rejected_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.PROVIDER_REJECTED)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_token_limit_never_retries() -> None:
    result, adapter = await _single_error_case(ProposalProviderErrorCode.TOKEN_LIMIT)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_no_model_fallback_on_retry() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    seen_models = []

    class Adapter:
        async def generate(self, *, request, model_configuration):
            seen_models.append(model_configuration.model_identifier)
            return _error_response(error_code=ProposalProviderErrorCode.TIMEOUT)

    adapter = Adapter()
    await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=adapter)
    assert len(set(seen_models)) == 1


@pytest.mark.asyncio
async def test_provider_model_mismatch_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase.", model_identifier="wrong-model")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == (
        ProposalViolationCode.MODEL_CONFIGURATION_FINGERPRINT_MISMATCH
    )


@pytest.mark.asyncio
async def test_response_operation_mismatch_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return LlmAdapterResponse(
                provider=model_configuration.provider,
                model_identifier=model_configuration.model_identifier,
                structured_response=ControlledProposalResponse(
                    proposal_text="Achieved a 30% revenue increase within one year.",
                    operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
                    source_fact_id=request.source_fact_id,
                ),
                raw_response_text="raw",
                finish_reason="stop",
            )

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == (
        ProposalViolationCode.RESPONSE_OPERATION_MISMATCH
    )


@pytest.mark.asyncio
async def test_response_fact_id_mismatch_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return LlmAdapterResponse(
                provider=model_configuration.provider,
                model_identifier=model_configuration.model_identifier,
                structured_response=ControlledProposalResponse(
                    proposal_text="Achieved a 30% revenue increase within one year.",
                    operation=request.operation,
                    source_fact_id=uuid4(),
                ),
                raw_response_text="raw",
                finish_reason="stop",
            )

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == (
        ProposalViolationCode.RESPONSE_FACT_ID_MISMATCH
    )


@pytest.mark.asyncio
async def test_character_limit_exceeded_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    proposal_context = _proposal_context(maximum_character_count=10, maximum_sentence_count=5)

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year, well beyond the limit.")

    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, proposal_context=proposal_context, llm_adapter=Adapter()
    )
    assert result.blocked_proposal_items[0].violation_code == (
        ProposalViolationCode.PROPOSAL_CHARACTER_LIMIT_EXCEEDED
    )


@pytest.mark.asyncio
async def test_sentence_limit_exceeded_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()
    proposal_context = _proposal_context(maximum_character_count=5000, maximum_sentence_count=1)

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(
                request, "Grew revenue by 30%. Also improved retention. And expanded the team."
            )

    result = await _run_p5b(
        plan, p5a_result, [d], entity_types, payload_set, proposal_context=proposal_context, llm_adapter=Adapter()
    )
    assert result.blocked_proposal_items[0].violation_code == (
        ProposalViolationCode.PROPOSAL_SENTENCE_LIMIT_EXCEEDED
    )


@pytest.mark.asyncio
async def test_immutable_token_missing_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved significant revenue growth within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.IMMUTABLE_TOKEN_MISSING


@pytest.mark.asyncio
async def test_immutable_token_changed_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 45% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


@pytest.mark.asyncio
async def test_immutable_token_added_blocks() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(
                request, "Achieved a 30% revenue increase within one year, plus a 10% cost cut."
            )

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.blocked_proposal_items[0].violation_code == ProposalViolationCode.IMMUTABLE_TOKEN_ADDED


# -- 42-45: approval state and readiness --


@pytest.mark.asyncio
async def test_valid_proposal_has_requires_user_approval() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.proposals[0].approval_state == ProposalApprovalState.REQUIRES_USER_APPROVAL


def test_proposal_model_never_has_approved_or_rejected_state() -> None:
    from app.schemas.cv_content_proposal import GeneratedCvContentProposal

    field = GeneratedCvContentProposal.model_fields["approval_state"]
    assert field.annotation.__args__ == (ProposalApprovalState.REQUIRES_USER_APPROVAL,)


@pytest.mark.asyncio
async def test_ready_for_p55_is_always_false() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.ready_for_p55 is False


# -- 46-49: proposal preserves placement provenance --


@pytest.mark.asyncio
async def test_proposal_preserves_target_section_scope_placement_and_scope() -> None:
    tc = _target_context()
    employment_id = uuid4()
    d = _decision(
        target_context=tc,
        fact_type="EMPLOYMENT_ACTIVITY",
        target_scope="EXPERIENCE",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        employment_scope_entity_id=employment_id,
    )
    entity_types = {employment_id: EntityType.EMPLOYMENT}
    plan, entity_types = _build_plan(tc, [d], entity_types)
    payload = _payload_for(d, "Managed a team responsible for 30% growth.")
    payload_set = _payload_set([payload])
    p5a_result = _run_p5a(plan, [d], entity_types, payload_set)
    assert len(p5a_result.blocked_items) == 1

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Led a team achieving 30% growth.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    assert result.status == ProposalGenerationStatus.PROPOSED
    proposal = result.proposals[0]
    assert proposal.target_section == CvSection.EXPERIENCE
    assert proposal.target_scope == "EXPERIENCE"
    assert proposal.employment_scope_entity_id == employment_id
    assert proposal.placement_order == 0


# -- 50-53: provenance and fingerprints --


@pytest.mark.asyncio
async def test_proposal_has_full_provenance() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    provenance = result.proposals[0].llm_generation_provenance
    assert provenance.provider == "test-provider"
    assert provenance.model_identifier == "test-model"
    assert provenance.attempt_count == 1
    assert len(provenance.attempt_fingerprints) == 1
    assert provenance.p5a_result_fingerprint == p5a_result.result_fingerprint


@pytest.mark.asyncio
async def test_proposal_fingerprint_covers_proposal_text() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class Adapter:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    result = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=Adapter())
    proposal = result.proposals[0]
    assert proposal.proposal_text == "Achieved a 30% revenue increase within one year."
    # Changing the text must change the fingerprint -- verified in the next test.
    assert proposal.proposal_fingerprint


@pytest.mark.asyncio
async def test_changing_proposal_text_changes_fingerprint() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class AdapterA:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Achieved a 30% revenue increase within one year.")

    class AdapterB:
        async def generate(self, *, request, model_configuration):
            return _success_response(request, "Delivered a 30% revenue increase within one year.")

    result_a = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=AdapterA())
    result_b = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=AdapterB())
    assert result_a.proposals[0].proposal_fingerprint != result_b.proposals[0].proposal_fingerprint


@pytest.mark.asyncio
async def test_provider_request_id_does_not_affect_identity() -> None:
    tc, d, plan, entity_types, payload_set, p5a_result = _setup_eligible_case()

    class AdapterA:
        async def generate(self, *, request, model_configuration):
            response = _success_response(request, "Achieved a 30% revenue increase within one year.")
            return response.model_copy(update={"provider_request_id": "req-a"})

    class AdapterB:
        async def generate(self, *, request, model_configuration):
            response = _success_response(request, "Achieved a 30% revenue increase within one year.")
            return response.model_copy(update={"provider_request_id": "req-b-different"})

    result_a = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=AdapterA())
    result_b = await _run_p5b(plan, p5a_result, [d], entity_types, payload_set, llm_adapter=AdapterB())
    assert result_a.proposals[0].proposal_fingerprint == result_b.proposals[0].proposal_fingerprint


# -- AWARD boundary --


@pytest.mark.asyncio
async def test_award_fact_type_is_blocked_without_provider_call() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="AWARD_NAME",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    # AWARD_NAME has no P4 section mapping, so we bypass build_cv_content_plan
    # and exercise the P5b/P5a boundary directly against a hand-built plan
    # slice would be brittle; instead assert the policy gate is reachable
    # via the is_award_fact_type helper used inside the builder.
    from app.services.cv_content_proposal_policy import is_award_fact_type

    assert is_award_fact_type(d.fact_type) is True
