"""Stage P3.5: ``FactSelectionDecision.fact_type`` is a direct, explicit,
non-inferred field sourced only from ``TruthFactRead.fact_type``.
"""

from dataclasses import fields
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.fact_selection import FactSelectionDecision, TargetContext
from app.schemas.truth_fact import (
    FactStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
)
from app.services.fact_selection_policy import (
    SELECTION_POLICY_VERSION,
    compute_decision_fingerprint,
    select_fact,
)
from app.services.fact_selection_replay import (
    FactSelectionReplayInput,
    is_decision_stale,
    replay_input_from_decision,
    stale_fields,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fact(**overrides) -> TruthFactRead:
    values = dict(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type="SKILL",
        value_json={"text": "Python"},
        normalized_value_json={"text": "Python"},
        status=FactStatus.CONFIRMED,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        source_reference=None,
    )
    values.update(overrides)
    return TruthFactRead(**values)


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id=None,
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def _decide(*, fact=None, target_context=None, **kwargs) -> FactSelectionDecision:
    call_kwargs = dict(
        fact=fact or _fact(),
        target_context=target_context or _target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=NOW,
    )
    call_kwargs.update(kwargs)
    return select_fact(**call_kwargs)


@pytest.mark.parametrize(
    "fact_type", ["SKILL", "TOOL", "TECHNOLOGY", "EMPLOYMENT_ACTIVITY", "CUSTOM_FACT_TYPE"]
)
def test_decision_fact_type_comes_directly_from_the_fact_not_a_lookup(fact_type: str) -> None:
    fact = _fact(fact_type=fact_type)
    decision = _decide(fact=fact, requested_target_scope="EXPERIENCE")
    assert decision.fact_type == fact_type


def test_fact_type_is_validated_by_the_existing_fact_type_pattern() -> None:
    with pytest.raises(ValidationError):
        FactSelectionDecision(
            decision_fingerprint="a" * 64,
            selection_policy_version=SELECTION_POLICY_VERSION,
            transformation_policy_version="truth-transformation-policy-v3",
            target_context_id=uuid4(),
            target_context_fingerprint="b" * 64,
            person_entity_id=uuid4(),
            entity_id=uuid4(),
            fact_id=uuid4(),
            fact_type="not-a-valid-fact-type",
            fact_revision=1,
            fact_content_fingerprint="c" * 64,
            decision="BLOCKED",
            reason_codes=("FACT_STATUS_DRAFT_NOT_ELIGIBLE",),
            requested_operation=TransformationOperation.EXACT_COPY,
            requested_target_scope="SKILLS",
            allowed_operation=None,
            transferability=Transferability.GLOBAL_TRANSFERABLE,
            employment_scope_entity_id=None,
            permission_ids=(),
            permission_snapshot_fingerprint="d" * 64,
            requires_user_approval=False,
            evaluation_time=NOW,
        )


def test_fact_type_changes_decision_fingerprint_directly() -> None:
    common = dict(
        selection_policy_version=SELECTION_POLICY_VERSION,
        transformation_policy_version="truth-transformation-policy-v3",
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        target_context_fingerprint="b" * 64,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permission_snapshot_fingerprint="c" * 64,
        evaluation_time=NOW,
    )
    fingerprint_skill = compute_decision_fingerprint(fact_type="SKILL", **common)
    fingerprint_tool = compute_decision_fingerprint(fact_type="TOOL", **common)
    assert fingerprint_skill != fingerprint_tool


def test_fact_selection_replay_input_has_an_explicit_fact_type_field() -> None:
    names = {field.name for field in fields(FactSelectionReplayInput)}
    assert "fact_type" in names


def test_fact_type_change_alone_marks_decision_stale() -> None:
    fact = _fact(fact_type="SKILL")
    target_context = _target_context()
    original = _decide(fact=fact, target_context=target_context)
    changed = _fact(
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        fact_type="TECHNOLOGY",
        content_fingerprint="f" * 64,
    )
    replayed = _decide(fact=changed, target_context=target_context)

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert stale_fields(previous, current) == (
        "fact_content_fingerprint",
        "fact_type",
    )


def test_identical_fact_type_does_not_by_itself_cause_staleness() -> None:
    fact = _fact(fact_type="EMPLOYMENT_ACTIVITY")
    target_context = _target_context()
    first = _decide(fact=fact, target_context=target_context, requested_target_scope="EXPERIENCE")
    second = _decide(fact=fact, target_context=target_context, requested_target_scope="EXPERIENCE")
    previous = replay_input_from_decision(first)
    current = replay_input_from_decision(second)
    assert not is_decision_stale(previous, current)
