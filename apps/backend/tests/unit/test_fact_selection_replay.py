"""P3 replay: identical input -> identical decision_fingerprint; else stale."""

import inspect
from datetime import datetime, timezone
from uuid import uuid4

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
from app.services.truth_policy import TruthTransformationPolicyRegistry
from app.services.fact_selection_replay import (
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
        job_or_application_id="job-1",
        target_role_title="Senior Engineer",
        target_role_family="ENGINEERING",
        target_seniority="SENIOR",
        target_organization_or_industry="SOFTWARE",
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
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
    )
    call_kwargs.update(kwargs)
    return select_fact(**call_kwargs)


def test_identical_input_gives_identical_decision_fingerprint() -> None:
    fact = _fact()
    target_context = _target_context()
    first = _decide(fact=fact, target_context=target_context)
    second = _decide(fact=fact, target_context=target_context)
    assert first.decision_fingerprint == second.decision_fingerprint
    assert first == second


def test_revision_change_is_stale() -> None:
    fact = _fact()
    original = _decide(fact=fact)
    bumped_fact = _fact(
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        revision=fact.revision + 1,
        content_fingerprint="c" * 64,
    )
    replayed = _decide(fact=bumped_fact)

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "fact_revision" in stale_fields(previous, current)


def test_fact_content_fingerprint_change_is_stale() -> None:
    fact = _fact()
    original = _decide(fact=fact)
    changed_fact = _fact(
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        revision=fact.revision,
        content_fingerprint="d" * 64,
    )
    replayed = _decide(fact=changed_fact)

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "fact_content_fingerprint" in stale_fields(previous, current)


def test_fact_type_change_is_stale() -> None:
    """Stage P3.5: fact_type is its own explicit replay field -- a fact_type
    change must invalidate a previously produced decision even if every
    other field on the fact is unchanged."""

    fact = _fact(fact_type="SKILL")
    original = _decide(fact=fact)
    retyped_fact = _fact(
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        fact_type="TOOL",
        content_fingerprint="e" * 64,
    )
    replayed = _decide(fact=retyped_fact)

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "fact_type" in stale_fields(previous, current)


def test_identical_fact_type_preserves_replay() -> None:
    fact = _fact(fact_type="SKILL")
    target_context = _target_context()
    first = _decide(fact=fact, target_context=target_context)
    second = _decide(fact=fact, target_context=target_context)

    previous = replay_input_from_decision(first)
    current = replay_input_from_decision(second)
    assert not is_decision_stale(previous, current)
    assert stale_fields(previous, current) == ()


def test_permission_snapshot_change_is_stale() -> None:
    from app.schemas.truth_fact import PermissionStatus, TruthPermissionRead

    fact = _fact()
    original = _decide(
        fact=fact, requested_operation=TransformationOperation.CONTROLLED_REPHRASE
    )
    permission = TruthPermissionRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="b" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        permission_id=uuid4(),
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        allowed_operations=[TransformationOperation.CONTROLLED_REPHRASE],
        constraints_json={},
        status=PermissionStatus.ACTIVE,
        approved_by=None,
        approved_at=None,
        valid_from=None,
        valid_until=None,
    )
    replayed = _decide(
        fact=fact,
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        permissions=[permission],
    )

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "permission_snapshot_fingerprint" in stale_fields(previous, current)


def test_target_context_change_is_stale() -> None:
    fact = _fact()
    original = _decide(fact=fact, target_context=_target_context())
    replayed = _decide(
        fact=fact, target_context=_target_context(target_role_family="SALES")
    )

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "target_context_fingerprint" in stale_fields(previous, current)


def test_policy_version_change_is_stale() -> None:
    fact = _fact()
    original = _decide(
        fact=fact, target_context=_target_context(selection_policy_version="v1")
    )
    replayed = _decide(
        fact=fact, target_context=_target_context(selection_policy_version="v2")
    )

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "selection_policy_version" in stale_fields(previous, current)


def test_requested_operation_change_is_stale() -> None:
    fact = _fact()
    original = _decide(fact=fact, requested_operation=TransformationOperation.EXACT_COPY)
    replayed = _decide(
        fact=fact, requested_operation=TransformationOperation.FORMAT_NORMALIZATION
    )

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(replayed)
    assert is_decision_stale(previous, current)
    assert "requested_operation" in stale_fields(previous, current)


def test_no_random_uuid_identity_on_the_decision() -> None:
    fields = FactSelectionDecision.model_fields
    assert "decision_fingerprint" in fields
    for name in ("selection_decision_id", "id"):
        assert name not in fields


def test_no_hidden_current_time_in_select_fact_or_stale_check() -> None:
    select_fact_params = inspect.signature(select_fact).parameters
    assert select_fact_params["evaluation_time"].default is inspect.Parameter.empty

    stale_params = inspect.signature(is_decision_stale).parameters
    assert "evaluation_time" not in stale_params
    for parameter in stale_params.values():
        assert parameter.default is inspect.Parameter.empty


def test_decision_carries_fact_type_directly_from_the_fact() -> None:
    fact = _fact(fact_type="TECHNOLOGY")
    decision = _decide(fact=fact)
    assert decision.fact_type == "TECHNOLOGY"


def test_selection_policy_version_v3_is_used_by_default() -> None:
    assert SELECTION_POLICY_VERSION == "fact-selection-policy-v3"
    decision = _decide(fact=_fact())
    assert decision.selection_policy_version == "fact-selection-policy-v3"


def test_transformation_policy_version_is_populated_from_registry() -> None:
    decision = _decide(fact=_fact())
    assert decision.transformation_policy_version == TruthTransformationPolicyRegistry().version


def test_decision_fingerprint_changes_with_transformation_policy_version() -> None:
    """A P1 registry content change must invalidate a previously computed
    decision_fingerprint even when selection_policy_version (P3's own
    decision-logic version) is unchanged -- see remediation R2."""

    common = dict(
        selection_policy_version=SELECTION_POLICY_VERSION,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type="SKILL",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        target_context_fingerprint="b" * 64,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permission_snapshot_fingerprint="c" * 64,
        evaluation_time=NOW,
    )
    fingerprint_v1 = compute_decision_fingerprint(
        transformation_policy_version="truth-transformation-policy-v1", **common
    )
    fingerprint_v2 = compute_decision_fingerprint(
        transformation_policy_version="truth-transformation-policy-v2", **common
    )
    assert fingerprint_v1 != fingerprint_v2


def test_transformation_policy_version_change_is_stale() -> None:
    fact = _fact()
    original = _decide(fact=fact)
    bumped = original.model_copy(
        update={"transformation_policy_version": "truth-transformation-policy-v999"}
    )

    previous = replay_input_from_decision(original)
    current = replay_input_from_decision(bumped)
    assert is_decision_stale(previous, current)
    assert "transformation_policy_version" in stale_fields(previous, current)


def test_reason_codes_and_advisory_flags_are_sorted() -> None:
    decision = _decide(
        fact=_fact(status=FactStatus.REVIEW_REQUIRED, requires_approval=True),
    )
    assert list(decision.reason_codes) == sorted(decision.reason_codes, key=lambda r: r.value)
    assert list(decision.advisory_flags) == sorted(decision.advisory_flags)
