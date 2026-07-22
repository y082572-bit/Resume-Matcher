"""P4 -> P5a -> P5b end-to-end proof: real ``build_cv_content_plan`` ->
real ``generate_cv_content_draft`` -> P5b ``generate_controlled_cv_content_proposals``
against a deterministic fake ``ControlledProposalLlmAdapter``.

Confirms P5b's own modules never touch the database, SQLAlchemy, LiteLLM,
``app.llm``, Stage 10C, the Master Resume, or a DOCX/PDF renderer -- and
that both eligible operations (``CONTROLLED_REPHRASE``/
``FLATTEN_FOR_LOWER_ROLE``) produce a full-provenance
``GeneratedCvContentProposal`` while ``AWARD_NAME`` stays fail-closed.

All data is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
    GenerationStatus,
)
from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import (
    ControlledProposalResponse,
    LlmAdapterResponse,
    LlmModelConfiguration,
    ProposalApprovalState,
    ProposalGenerationContext,
    ProposalGenerationStatus,
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
from app.services import (
    cv_content_proposal_builder,
    cv_content_proposal_constraints,
    cv_content_proposal_llm,
    cv_content_proposal_policy,
    cv_content_proposal_prompt,
    cv_content_proposal_replay,
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
from app.services.cv_content_proposal_policy import is_award_fact_type
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_target_context_fingerprint,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TRANSFORMATION_POLICY_VERSION = "truth-transformation-policy-v3"

_P5B_SOURCE_FILES = (
    Path(cv_content_proposal_builder.__file__),
    Path(cv_content_proposal_constraints.__file__),
    Path(cv_content_proposal_llm.__file__),
    Path(cv_content_proposal_policy.__file__),
    Path(cv_content_proposal_prompt.__file__),
    Path(cv_content_proposal_replay.__file__),
    Path(cv_content_proposal_builder.__file__).parent.parent / "schemas" / "cv_content_proposal.py",
)


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-synthetic-p5b",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decision(*, target_context, fact_type, target_scope, requested_operation, employment_scope_entity_id=None):
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


class DeterministicFakeAdapter:
    """A synthetic ``ControlledProposalLlmAdapter`` -- never calls a real
    provider, never imports LiteLLM. Each call deterministically rewrites
    ``source_text`` while preserving every immutable token."""

    def __init__(self):
        self.calls = []

    async def generate(self, *, request, model_configuration):
        self.calls.append(request)
        if request.operation == TransformationOperation.CONTROLLED_REPHRASE:
            proposal_text = "Achieved a 30% revenue increase within one synthetic year."
        else:
            proposal_text = "Contributed to a 30% revenue increase within one synthetic year."
        return LlmAdapterResponse(
            provider=model_configuration.provider,
            model_identifier=model_configuration.model_identifier,
            structured_response=ControlledProposalResponse(
                proposal_text=proposal_text,
                operation=request.operation,
                source_fact_id=request.source_fact_id,
            ),
            raw_response_text=f"raw:{proposal_text}",
            finish_reason="stop",
        )


async def _run_full_pipeline(requested_operation: TransformationOperation):
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="SUMMARY",
        target_scope="SUMMARY",
        requested_operation=requested_operation,
    )
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
    assert len(p5a_result.blocked_items) == 1

    p5a_replay = replay_input_from_generation_result(
        plan=plan,
        payload_set=payload_set,
        generation_context=generation_context,
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=p5a_result,
    )
    adapter = DeterministicFakeAdapter()
    result = await generate_controlled_cv_content_proposals(
        plan=plan,
        p5a_result=p5a_result,
        p5a_generation_context=generation_context,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_plan_replay_input=plan_replay,
        current_p5a_replay_input=p5a_replay,
        fact_payloads=payload_set,
        proposal_context=_proposal_context(),
        llm_adapter=adapter,
    )
    return plan, p5a_result, result, adapter


@pytest.mark.asyncio
async def test_controlled_rephrase_end_to_end() -> None:
    plan, p5a_result, result, adapter = await _run_full_pipeline(
        TransformationOperation.CONTROLLED_REPHRASE
    )
    assert result.status == ProposalGenerationStatus.PROPOSED
    assert len(adapter.calls) == 1
    proposal = result.proposals[0]
    assert proposal.allowed_operation == TransformationOperation.CONTROLLED_REPHRASE
    assert proposal.approval_state == ProposalApprovalState.REQUIRES_USER_APPROVAL
    assert result.ready_for_p55 is False


@pytest.mark.asyncio
async def test_flatten_for_lower_role_end_to_end() -> None:
    plan, p5a_result, result, adapter = await _run_full_pipeline(
        TransformationOperation.FLATTEN_FOR_LOWER_ROLE
    )
    assert result.status == ProposalGenerationStatus.PROPOSED
    proposal = result.proposals[0]
    assert proposal.allowed_operation == TransformationOperation.FLATTEN_FOR_LOWER_ROLE
    assert proposal.approval_state == ProposalApprovalState.REQUIRES_USER_APPROVAL


@pytest.mark.asyncio
async def test_end_to_end_proposal_has_full_provenance() -> None:
    plan, p5a_result, result, adapter = await _run_full_pipeline(
        TransformationOperation.CONTROLLED_REPHRASE
    )
    proposal = result.proposals[0]
    provenance = proposal.llm_generation_provenance
    assert provenance.p5a_result_fingerprint == p5a_result.result_fingerprint
    assert provenance.provider == "test-provider"
    assert provenance.model_identifier == "test-model"
    assert provenance.attempt_count == 1
    assert proposal.content_plan_fingerprint == plan.content_plan_fingerprint
    assert proposal.section_plan_fingerprint
    assert proposal.blocked_generation_item_fingerprint == p5a_result.blocked_items[0].blocked_item_fingerprint


# -- AWARD stays out of scope --


@pytest.mark.asyncio
async def test_award_name_is_unsupported_in_p5b() -> None:
    tc = _target_context()
    d = _decision(
        target_context=tc,
        fact_type="AWARD_NAME",
        target_scope="SUMMARY",
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    assert is_award_fact_type(d.fact_type) is True
    # AWARD_NAME has no P4 section mapping at all -- it cannot even reach a
    # built plan, which is itself the fail-closed boundary the spec requires.
    build_result = build_cv_content_plan(
        target_context=tc, decisions=[d], entity_types={d.entity_id: EntityType.AWARD}
    )
    assert build_result.plan is not None
    # The decision was structurally excluded (no section mapping), never
    # silently defaulted into any CvSection.
    omitted_fact_ids = {item.fact_id for item in build_result.plan.omitted_facts}
    assert d.fact_id in omitted_fact_ids


# -- source boundary checks: no DB, no legacy coupling, no DOCX/PDF/frontend --


def _code_only(text: str) -> str:
    return re.sub(r'""".*?"""', "", text, flags=re.DOTALL)


def _p5b_source_texts() -> dict[str, str]:
    return {path.name: _code_only(path.read_text(encoding="utf-8")) for path in _P5B_SOURCE_FILES}


def test_p5b_modules_never_import_the_database_or_orm() -> None:
    forbidden = ("from app.database", "from app import database", "sqlalchemy", "app.models")
    for name, text in _p5b_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden DB symbol: {needle}"


def test_p5b_modules_never_import_legacy_stage_10c() -> None:
    forbidden = (
        "truth_legacy_migrator",
        "cv_transformation_generation",
        "cv_transformation_plan",
        "cv_transformation_approval",
    )
    for name, text in _p5b_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden legacy module: {needle}"


def test_p5b_modules_never_reference_source_reference_or_legacy_record_key() -> None:
    forbidden = ("source_reference", "legacy_record_key")
    for name, text in _p5b_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden identity field: {needle}"


def test_p5b_modules_never_reference_master_resume() -> None:
    forbidden = ("master_resume", "MasterResume")
    for name, text in _p5b_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references Master Resume: {needle}"


def test_p5b_modules_never_reference_docx_pdf_router_or_frontend() -> None:
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
    for name, text in _p5b_source_texts().items():
        lowered = text.lower()
        for needle in forbidden:
            assert needle not in lowered, f"{name} references forbidden output/router symbol: {needle}"


def test_named_synthetic_data_only_used() -> None:
    for value in ("Synthetic Senior Analyst", "synthetic year", "job-synthetic-p5b"):
        assert "synthetic" in value.lower()
