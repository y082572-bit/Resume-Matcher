"""Unit tests for Explicit Provenance Stage P6-A:
``validate_approved_content_document_input``.

Builds a real PRE-P4 -> P3(strategy) -> P4(``ROLE_STRATEGY_INTEGRATED``) ->
P5a -> P5.5 pipeline (``EXACT_COPY`` only, never requiring a P5b/semantic-
validator call) to produce a genuine, fresh, ``READY``/``ready_for_p6``
``ApprovedCvContentResult`` whose ``source_content_plan`` is a real
``CvContentPlanMode.ROLE_STRATEGY_INTEGRATED`` plan built by
``build_role_strategy_integrated_cv_content_plan`` -- then exercises P6-A's
independent re-validation of the envelope around it.

``build_ready_integrated_context``/``build_document_input`` here are the
shared factory every other P6-A unit/integration test module imports (same
cross-module-import convention as
``tests.unit.test_role_strategy_prep4_revalidation.build_ready_fixture``):
P6-A accepts **only** P5.5 content whose plan is
``ROLE_STRATEGY_INTEGRATED`` -- a plan faked via ``model_copy(update=...)``,
``model_construct(...)``, or a hand-edited ``LEGACY`` plan is never an
acceptable substitute in a positive test.

A small ``build_ready_legacy_context`` (the original, pre-remediation
fixture pattern) is kept, but used *only* by the explicit LEGACY-rejection
tests below -- it must never again be used to build a "positive" test.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.cv_content_approval import ApprovalAssemblyStatus
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
)
from app.schemas.cv_content_plan import CvContentPlanBuildOutcome, CvContentPlanMode
from app.schemas.cv_content_proposal import LlmModelConfiguration, ProposalGenerationContext, TargetRoleContext
from app.schemas.cv_document_artifact import ApprovedContentDocumentInput, ArtifactOwnerKind, DocumentInputValidationStatus, DocumentInputViolationCode
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.cv_content_approval_builder import build_approved_cv_content
from app.services.cv_content_approval_replay import (
    compute_approved_content_replay_input_fingerprint,
    replay_input_from_approved_content_result,
    replay_input_from_semantic_validation_result,
)
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    generate_cv_content_draft,
)
from app.services.cv_content_generation_policy import CV_CONTENT_GENERATION_POLICY_VERSION, DEFAULT_DETERMINISTIC_TEMPLATE_VERSION
from app.services.cv_content_generation_replay import replay_input_from_generation_result
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_integrated_builder import build_role_strategy_integrated_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.cv_content_proposal_builder import generate_controlled_cv_content_proposals
from app.services.cv_content_proposal_replay import replay_input_from_proposal_result
from app.services.cv_content_semantic_validation_builder import validate_cv_content_proposals
from app.services.cv_document_input_validation import compute_document_input_fingerprint, recompute_content_plan_fingerprint, validate_approved_content_document_input
from app.services.cv_document_owner_identity import build_owner_key
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, compute_target_context_fingerprint
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_candidate, make_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"
PROPOSAL_SCHEMA_VERSION = "cv-content-proposal-schema-v1"
PROPOSAL_POLICY_VERSION = "cv-content-proposal-policy-v1"


class _NoCallProposalAdapter:
    async def generate(self, *, request, model_configuration):
        raise AssertionError("EXACT_COPY facts never require a P5b LLM call")


class _NoCallSemanticValidator:
    async def validate(self, *, request, model_configuration):
        raise AssertionError("EXACT_COPY facts never require a semantic-validator call")


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-p6a-input-validation",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(target_context: TargetContext) -> FactSelectionDecision:
    fact_id = uuid4()
    entity_id = uuid4()
    seed = f"{fact_id}{entity_id}{uuid4()}"
    return FactSelectionDecision(
        decision_fingerprint=hashlib.sha256(seed.encode()).hexdigest(),
        selection_policy_version=target_context.selection_policy_version,
        transformation_policy_version=TRANSFORMATION_POLICY_VERSION,
        target_context_id=target_context.target_context_id,
        target_context_fingerprint=compute_target_context_fingerprint(target_context),
        person_entity_id=target_context.person_entity_id,
        entity_id=entity_id,
        fact_id=fact_id,
        fact_type="SUMMARY",
        fact_revision=1,
        fact_content_fingerprint="b" * 64,
        decision=SelectionDecisionOutcome.SELECTED,
        reason_codes=(FactSelectionReasonCode.PERMISSION_NOT_REQUIRED_BASE_OPERATION,),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SUMMARY",
        allowed_operation=TransformationOperation.EXACT_COPY,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        permission_ids=(),
        permission_snapshot_fingerprint="c" * 64,
        requires_user_approval=False,
        evaluation_time=NOW,
        advisory_flags=(),
    )


def _integrated_candidate(payload: ApprovedFactPayloadSnapshot):
    fact = make_fact(
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        content_fingerprint=payload.fact_content_fingerprint,
    )
    return make_candidate(fact)


def _effective_decision(decision) -> FactSelectionDecision:
    """Mirrors ``cv_content_plan_integrated_builder._project_effective_decision``:
    the plan itself is built from the *effective* (possibly
    NOT_RELEVANT -> SELECTED overridden) decision, never the raw
    ``base_decision`` alone -- so ``decisions_by_fingerprint`` handed to P5a
    must reflect the very same projection or its own independent
    downstream re-validation would see a stale/mismatched decision."""

    from app.schemas.strategy_fact_selection import StrategyOverrideOutcome

    if decision.override_outcome == StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED:
        return decision.base_decision.model_copy(
            update={
                "decision": SelectionDecisionOutcome.SELECTED,
                "allowed_operation": decision.base_decision.requested_operation,
            }
        )
    return decision.base_decision


def _p5a_payload_set(payloads: list[ApprovedFactPayloadSnapshot]) -> ApprovedFactPayloadSet:
    """Rebuild an ``ApprovedFactPayloadSet`` whose ``payload_fingerprint``/
    ``payload_set_fingerprint`` are computed via ``cv_content_generation_builder``'s
    own functions -- ``tests.unit.test_role_strategy_prep4_revalidation.build_ready_fixture``'s
    ``payload_set`` is fingerprinted via the *candidacy-thesis* payload-set
    fingerprint (a different, incompatible fingerprint version string), so it
    can never be reused directly as P5a's ``fact_payloads`` input."""

    rebuilt = [
        ApprovedFactPayloadSnapshot(
            person_entity_id=payload.person_entity_id,
            entity_id=payload.entity_id,
            fact_id=payload.fact_id,
            fact_type=payload.fact_type,
            fact_revision=payload.fact_revision,
            fact_content_fingerprint=payload.fact_content_fingerprint,
            value_json=payload.value_json,
            normalized_value_json=payload.normalized_value_json,
            payload_fingerprint=compute_fact_payload_fingerprint(
                person_entity_id=payload.person_entity_id,
                entity_id=payload.entity_id,
                fact_id=payload.fact_id,
                fact_type=payload.fact_type,
                fact_revision=payload.fact_revision,
                fact_content_fingerprint=payload.fact_content_fingerprint,
                value_json=payload.value_json,
                normalized_value_json=payload.normalized_value_json,
                generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
            ),
        )
        for payload in payloads
    ]
    return ApprovedFactPayloadSet(
        payloads=tuple(rebuilt), payload_set_fingerprint=compute_fact_payload_set_fingerprint(rebuilt)
    )


async def _run_pipeline_from_plan(plan, decisions_by_fp: dict, entity_types: dict, payload_set: ApprovedFactPayloadSet) -> dict:
    """Shared P5a -> P5b -> P5.5 tail, identical regardless of whether
    ``plan`` is LEGACY or ROLE_STRATEGY_INTEGRATED -- P6-A's mandatory-mode
    check happens only at the input-validation boundary, never inside this
    upstream pipeline."""

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
    proposal_context = ProposalGenerationContext(
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
        llm_adapter=_NoCallProposalAdapter(),
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

    from app.schemas.cv_content_approval import SemanticValidationContext, SemanticValidatorModelConfiguration

    validation_model_configuration = SemanticValidatorModelConfiguration(
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
    validation_context = SemanticValidationContext(
        locale="pl-PL",
        validation_prompt_version="controlled-semantic-validation-prompt-v1",
        validation_policy_version="cv-content-semantic-validation-policy-v1",
        retry_policy_version="semantic-validator-retry-policy-v1",
        validator_model_configuration=validation_model_configuration,
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
        validation_context=validation_context,
        semantic_validator=_NoCallSemanticValidator(),
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

    result = build_approved_cv_content(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        p5b_result=p5b_result,
        proposal_generation_context=proposal_context,
        semantic_validation_result=semantic_result,
        approval_decisions=[],
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        fact_payloads=payload_set,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        current_p5b_replay_input=p5b_replay,
        current_semantic_validation_replay_input=semantic_replay,
    )
    assert result.status == ApprovalAssemblyStatus.READY
    assert result.ready_for_p6 is True

    current_p55_replay_input = replay_input_from_approved_content_result(
        semantic_validation_replay_input=semantic_replay, approval_decisions=[], result=result
    )

    return dict(plan=plan, result=result, semantic_replay=semantic_replay, current_p55_replay_input=current_p55_replay_input)


async def build_ready_integrated_context() -> dict:
    """Build a genuine, fresh, READY/ready_for_p6 P5.5 result whose
    ``source_content_plan`` is a real ``ROLE_STRATEGY_INTEGRATED``
    ``CvContentPlan`` -- built only by
    ``build_role_strategy_integrated_cv_content_plan``, never faked. This
    is the shared factory every P6-A positive test must use.

    Uses only ``fixture.fact0``/``fixture.fact2`` (``SKILL``/``LANGUAGE_NAME``,
    both flat-section fact types): ``fixture.fact1`` is
    ``EMPLOYMENT_ACTIVITY``, which P4 policy requires an explicit
    ``employment_scope_entity_id`` to place -- the shared
    ``tests.unit.test_role_strategy_prep4_revalidation`` fixture facts carry
    none, so it would always be structurally omitted
    (``EMPLOYMENT_SCOPE_REQUIRED``) and must never be fed to P5a as a
    payload for a fact that was never actually planned.
    """

    fixture = await build_ready_fixture()
    candidates = [
        _integrated_candidate(fixture.fact0),
        _integrated_candidate(fixture.fact2),
    ]
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    entity_types = {
        fixture.fact0.entity_id: EntityType.SKILL,
        fixture.fact2.entity_id: EntityType.LANGUAGE,
    }
    plan_result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
    )
    assert plan_result.outcome == CvContentPlanBuildOutcome.BUILT
    plan = plan_result.plan
    assert plan.plan_mode == CvContentPlanMode.ROLE_STRATEGY_INTEGRATED

    decisions_by_fp = {
        d.base_decision.decision_fingerprint: _effective_decision(d) for d in strategy_result.decisions
    }
    payload_set = _p5a_payload_set([fixture.fact0, fixture.fact2])
    return await _run_pipeline_from_plan(plan, decisions_by_fp, entity_types, payload_set)


async def build_ready_legacy_context(*, job_or_application_id: str = "job-p6a-input-validation") -> dict:
    """The original, pre-remediation LEGACY-mode fixture pattern -- kept
    **only** for the explicit LEGACY-rejection tests below. Never use this
    to build a positive/VALID-path test: P6-A accepts no LEGACY plan."""

    tc = _target_context(job_or_application_id=job_or_application_id)
    d = _decision(tc)
    entity_types = {d.entity_id: EntityType.SKILL}
    build_result = build_cv_content_plan(target_context=tc, decisions=[d], entity_types=entity_types)
    assert build_result.plan is not None
    plan = build_result.plan
    assert plan.plan_mode == CvContentPlanMode.LEGACY
    decisions_by_fp = {d.decision_fingerprint: d}

    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=d.person_entity_id,
        entity_id=d.entity_id,
        fact_id=d.fact_id,
        fact_type=d.fact_type,
        fact_revision=d.fact_revision,
        fact_content_fingerprint=d.fact_content_fingerprint,
        value_json="Experienced professional.",
        normalized_value_json="Experienced professional.",
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    payload = ApprovedFactPayloadSnapshot(
        person_entity_id=d.person_entity_id,
        entity_id=d.entity_id,
        fact_id=d.fact_id,
        fact_type=d.fact_type,
        fact_revision=d.fact_revision,
        fact_content_fingerprint=d.fact_content_fingerprint,
        value_json="Experienced professional.",
        normalized_value_json="Experienced professional.",
        payload_fingerprint=fingerprint,
    )
    payload_set = ApprovedFactPayloadSet(
        payloads=(payload,), payload_set_fingerprint=compute_fact_payload_set_fingerprint([payload])
    )
    return await _run_pipeline_from_plan(plan, decisions_by_fp, entity_types, payload_set)


def build_document_input(
    ctx: dict, *, owner_kind: ArtifactOwnerKind = ArtifactOwnerKind.JOB
) -> ApprovedContentDocumentInput:
    plan = ctx["plan"]
    result = ctx["result"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id, owner_kind=owner_kind, owner_reference_id=plan.job_or_application_id
    )
    current_p55_fp = compute_approved_content_replay_input_fingerprint(ctx["current_p55_replay_input"])
    document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        owner_kind=owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_fp,
    )
    return ApprovedContentDocumentInput(
        approved_content_result=result,
        source_content_plan=plan,
        current_p55_replay_input=ctx["current_p55_replay_input"],
        semantic_validation_replay_input=ctx["semantic_replay"],
        approval_decisions=(),
        owner_kind=owner_kind,
        document_input_fingerprint=document_input_fingerprint,
    )


@pytest.mark.asyncio
async def test_role_strategy_integrated_plan_is_accepted() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    result = validate_approved_content_document_input(document_input)
    assert result.status == DocumentInputValidationStatus.VALID
    assert result.owner_key is not None
    assert result.owner_key.owner_kind == ArtifactOwnerKind.JOB
    assert result.violations == ()


@pytest.mark.asyncio
async def test_owner_reference_id_matches_plan_job_or_application_id() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    result = validate_approved_content_document_input(document_input)
    assert result.status == DocumentInputValidationStatus.VALID
    assert result.owner_key.owner_reference_id == ctx["plan"].job_or_application_id


@pytest.mark.asyncio
async def test_tampered_result_fingerprint_blocks() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    tampered = document_input.model_copy(
        update={"approved_content_result": ctx["result"].model_copy(update={"result_fingerprint": "f" * 64})}
    )
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.RESULT_FINGERPRINT_MISMATCH in codes


@pytest.mark.asyncio
async def test_tampered_content_plan_fingerprint_blocks() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    tampered_plan = ctx["plan"].model_copy(update={"content_plan_fingerprint": "e" * 64})
    tampered = document_input.model_copy(update={"source_content_plan": tampered_plan})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH in codes


@pytest.mark.asyncio
async def test_stale_approved_content_blocks() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    stale_current = ctx["current_p55_replay_input"].model_copy(
        update={"approval_decision_fingerprints": ("0" * 64,)}
    )
    tampered = document_input.model_copy(update={"current_p55_replay_input": stale_current})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.RESULT_NOT_FRESH in codes


@pytest.mark.asyncio
async def test_document_input_fingerprint_tamper_blocks() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    tampered = document_input.model_copy(update={"document_input_fingerprint": "a" * 64})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.DOCUMENT_INPUT_FINGERPRINT_MISMATCH in codes


@pytest.mark.asyncio
async def test_arbitrary_dict_is_rejected_by_construction() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    with pytest.raises(Exception):
        ApprovedContentDocumentInput(
            **{
                **document_input.model_dump(),
                "approved_content_result": {"not": "a valid result"},
            }
        )


@pytest.mark.asyncio
async def test_p5a_draft_is_rejected_as_approved_content_result() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    dump = document_input.model_dump()
    dump["approved_content_result"] = ctx["result"].draft.model_dump()
    with pytest.raises(Exception):
        ApprovedContentDocumentInput(**dump)


# -- Mandatory ROLE_STRATEGY_INTEGRATED pipeline (P6_BYPASSES_MANDATORY_ROLE_STRATEGY_PIPELINE) --


@pytest.mark.asyncio
async def test_legacy_content_plan_is_rejected() -> None:
    ctx = await build_ready_legacy_context()
    document_input = build_document_input(ctx)
    result = validate_approved_content_document_input(document_input)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.LEGACY_CONTENT_PLAN_NOT_ALLOWED in codes


@pytest.mark.asyncio
async def test_p55_result_tied_to_legacy_plan_cannot_enter_p6a() -> None:
    """An otherwise fully valid, fresh, READY/ready_for_p6 P5.5 result is
    still rejected -- fail-closed -- for the sole reason that its plan is
    LEGACY. No other invariant is violated: every other check (result
    fingerprint, plan fingerprint, freshness, document_input_fingerprint)
    independently passes, so ``LEGACY_CONTENT_PLAN_NOT_ALLOWED`` is the
    *only* violation raised."""

    ctx = await build_ready_legacy_context()
    document_input = build_document_input(ctx)
    result = validate_approved_content_document_input(document_input)
    assert result.status == DocumentInputValidationStatus.INVALID
    assert {v.violation_code for v in result.violations} == {
        DocumentInputViolationCode.LEGACY_CONTENT_PLAN_NOT_ALLOWED
    }


@pytest.mark.asyncio
async def test_integrated_plan_missing_one_strategy_field_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    # model_copy(update=...) bypasses CvContentPlan's own model validator
    # (_strategy_fields_match_mode), which a normal constructor call would
    # never allow through for plan_mode=ROLE_STRATEGY_INTEGRATED.
    tampered_plan = ctx["plan"].model_copy(update={"role_strategy_context_fingerprint": None})
    tampered = document_input.model_copy(update={"source_content_plan": tampered_plan})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE in codes


@pytest.mark.asyncio
async def test_integrated_plan_tampered_strategy_fingerprint_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    tampered_plan = ctx["plan"].model_copy(update={"strategy_selection_result_fingerprint": "9" * 64})
    tampered = document_input.model_copy(update={"source_content_plan": tampered_plan})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH in codes


@pytest.mark.asyncio
async def test_model_copy_partial_integrated_state_is_rejected_by_input_validator() -> None:
    """Constructs a ``model_copy``-created plan that is internally
    *self-consistent*: its own ``content_plan_fingerprint`` is recomputed
    to match its (tampered) fields, so the recompute-based
    ``CONTENT_PLAN_FINGERPRINT_MISMATCH`` check alone would **not** catch
    it. Only the input validator's independent, explicit non-None check on
    every strategy field (``CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE``)
    catches this -- proving P6-A does not rely solely on
    ``CvContentPlan``'s own model validator (which a normal constructor
    call would have rejected outright, see below) nor solely on fingerprint
    recomputation.
    """

    ctx = await build_ready_integrated_context()
    plan = ctx["plan"]

    # A normal constructor call can never produce this state.
    with pytest.raises(Exception):
        plan.__class__(**{**plan.model_dump(), "strategy_ranking_input_fingerprint": None})

    # model_copy(update=...) bypasses that validator entirely.
    tampered_plan = plan.model_copy(update={"strategy_ranking_input_fingerprint": None})
    # Recompute content_plan_fingerprint against the tampered plan's own
    # fields, so the tampered plan is self-consistent -- fingerprint
    # recompute alone would report no mismatch for the plan in isolation.
    self_consistent_fingerprint = recompute_content_plan_fingerprint(tampered_plan)
    tampered_plan = tampered_plan.model_copy(update={"content_plan_fingerprint": self_consistent_fingerprint})

    document_input = build_document_input(ctx)
    tampered = document_input.model_copy(update={"source_content_plan": tampered_plan})
    result = validate_approved_content_document_input(tampered)
    assert result.status == DocumentInputValidationStatus.INVALID
    codes = {v.violation_code for v in result.violations}
    assert DocumentInputViolationCode.CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE in codes
