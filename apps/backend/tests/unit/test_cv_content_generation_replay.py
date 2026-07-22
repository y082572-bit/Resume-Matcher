"""Pure replay/staleness detection for P5a generation results.

All facts/entities are synthetic UUIDs; no real person or CV data is used.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
    GenerationFreshnessStatus,
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
from app.services.cv_content_generation_replay import (
    evaluate_generation_freshness,
    is_cv_content_generation_stale,
    replay_input_from_generation_result,
    stale_fields,
)
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
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


def _decision(*, target_context, fact_type="SUMMARY", target_scope="SUMMARY") -> FactSelectionDecision:
    fact_id = uuid4()
    entity_id = uuid4()
    seed = f"{fact_id}{entity_id}{fact_type}{target_scope}{uuid4()}"
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
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope=target_scope,
        allowed_operation=TransformationOperation.EXACT_COPY,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        permission_ids=(),
        permission_snapshot_fingerprint="c" * 64,
        requires_user_approval=False,
        evaluation_time=NOW,
        advisory_flags=(),
    )


def _payload(decision, value_json):
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


def _setup(*, template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION, value_json="Summary text"):
    tc = _target_context()
    d = _decision(target_context=tc)
    build_result = build_cv_content_plan(
        target_context=tc, decisions=[d], entity_types={d.entity_id: EntityType.SKILL}
    )
    plan = build_result.plan
    payload = _payload(d, value_json)
    payload_set = ApprovedFactPayloadSet(
        payloads=(payload,), payload_set_fingerprint=compute_fact_payload_set_fingerprint([payload])
    )
    context = GenerationContext(
        locale="pl-PL",
        deterministic_template_version=template_version,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    result = generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint={d.decision_fingerprint: d},
        entity_types={d.entity_id: EntityType.SKILL},
        current_replay_input=replay_input_from_plan(plan),
        fact_payloads=payload_set,
        generation_context=context,
    )
    return plan, payload_set, context, result


def _replay_input(plan, payload_set, context, result):
    return replay_input_from_generation_result(
        plan=plan,
        payload_set=payload_set,
        generation_context=context,
        generation_schema_version=CV_CONTENT_GENERATION_SCHEMA_VERSION,
        result=result,
    )


# -- Freshness classification --


def test_identical_replay_is_fresh() -> None:
    plan, payload_set, context, result = _setup()
    replay_input = _replay_input(plan, payload_set, context, result)
    outcome = evaluate_generation_freshness(replay_input, replay_input)
    assert outcome.freshness_status == GenerationFreshnessStatus.FRESH
    assert outcome.stale_fields == ()


def test_no_current_replay_is_freshness_not_verified() -> None:
    plan, payload_set, context, result = _setup()
    replay_input = _replay_input(plan, payload_set, context, result)
    outcome = evaluate_generation_freshness(replay_input, None)
    assert outcome.freshness_status == GenerationFreshnessStatus.FRESHNESS_NOT_VERIFIED
    assert outcome.stale_fields == ()


def test_changed_payload_set_is_stale() -> None:
    plan, payload_set, context, result = _setup(value_json="Original text")
    previous = _replay_input(plan, payload_set, context, result)

    _, other_payload_set, _, other_result = _setup(value_json="Changed text")
    current = previous.model_copy(
        update={"fact_payload_set_fingerprint": other_payload_set.payload_set_fingerprint}
    )
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "fact_payload_set_fingerprint" in outcome.stale_fields


def test_changed_plan_is_stale() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"content_plan_fingerprint": "0" * 64})
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "content_plan_fingerprint" in outcome.stale_fields


def test_changed_generation_policy_version_is_stale() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"generation_policy_version": "cv-content-generation-policy-v2"})
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "generation_policy_version" in outcome.stale_fields


def test_changed_template_version_is_stale() -> None:
    plan, payload_set, context, result = _setup(template_version="template-v1")
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"deterministic_template_version": "template-v2"})
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "deterministic_template_version" in outcome.stale_fields


def test_changed_generated_element_fingerprint_is_stale() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"generated_element_fingerprints": ("f" * 64,)})
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "generated_element_fingerprints" in outcome.stale_fields


def test_changed_blocked_item_fingerprint_is_stale() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"blocked_item_fingerprints": ("e" * 64,)})
    outcome = evaluate_generation_freshness(previous, current)
    assert outcome.freshness_status == GenerationFreshnessStatus.STALE
    assert "blocked_item_fingerprints" in outcome.stale_fields


def test_is_cv_content_generation_stale_matches_freshness_classification() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(update={"content_plan_fingerprint": "0" * 64})
    assert is_cv_content_generation_stale(previous, previous) is False
    assert is_cv_content_generation_stale(previous, current) is True


def test_stale_fields_is_deterministic() -> None:
    plan, payload_set, context, result = _setup()
    previous = _replay_input(plan, payload_set, context, result)
    current = previous.model_copy(
        update={"content_plan_fingerprint": "0" * 64, "generation_policy_version": "other"}
    )
    first = stale_fields(previous, current)
    second = stale_fields(previous, current)
    assert first == second
    assert first == tuple(sorted(first))
    assert "content_plan_fingerprint" in first
    assert "generation_policy_version" in first
