"""P3 transferability gate: every real Transferability value, exhaustively."""

from uuid import uuid4

import pytest

from app.schemas.fact_selection import FactSelectionReasonCode, SelectionDecisionOutcome, TargetContext
from app.schemas.truth_fact import Transferability, TransformationOperation
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, evaluate_transferability


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


ALL_TRANSFERABILITY_VALUES = list(Transferability)


def test_every_real_transferability_value_is_handled() -> None:
    """Every actual enum member must resolve to a defined gate outcome
    (either pass-through ``None`` or a terminal decision), never raise."""

    person_entity_id = uuid4()
    for value in ALL_TRANSFERABILITY_VALUES:
        result = evaluate_transferability(
            transferability=value,
            fact_entity_id=person_entity_id,
            fact_employment_scope_entity_id=None,
            target_context=_target_context(person_entity_id=person_entity_id),
            requested_operation=TransformationOperation.EXACT_COPY,
        )
        assert result.outcome is None or isinstance(result.outcome, SelectionDecisionOutcome)


def test_non_transferable_allows_only_original_entity_scope() -> None:
    person_entity_id = uuid4()
    match = evaluate_transferability(
        transferability=Transferability.NON_TRANSFERABLE,
        fact_entity_id=person_entity_id,
        fact_employment_scope_entity_id=None,
        target_context=_target_context(person_entity_id=person_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert match.outcome is None

    mismatch = evaluate_transferability(
        transferability=Transferability.NON_TRANSFERABLE,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(person_entity_id=person_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert mismatch.outcome == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TRANSFERABILITY_NON_TRANSFERABLE_SCOPE_MISMATCH
        in mismatch.reason_codes
    )


def test_non_transferable_permission_cannot_change_scope_outcome() -> None:
    """Even for the most privileged operation request, a scope mismatch on a
    NON_TRANSFERABLE fact is still BLOCKED -- transferability truth is not
    something a permission grant can override."""

    mismatch = evaluate_transferability(
        transferability=Transferability.NON_TRANSFERABLE,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert mismatch.outcome == SelectionDecisionOutcome.BLOCKED


def test_employment_scoped_requires_matching_employment_entity() -> None:
    employment_entity_id = uuid4()
    ok = evaluate_transferability(
        transferability=Transferability.EMPLOYMENT_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=employment_entity_id,
        target_context=_target_context(employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert ok.outcome is None


def test_employment_scoped_blocks_a_different_employment() -> None:
    other_employment_entity_id = uuid4()
    result = evaluate_transferability(
        transferability=Transferability.EMPLOYMENT_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=uuid4(),
        target_context=_target_context(employment_entity_id=other_employment_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TRANSFERABILITY_EMPLOYMENT_SCOPE_MISMATCH
        in result.reason_codes
    )


def test_employment_scoped_blocks_when_employment_context_is_missing() -> None:
    result = evaluate_transferability(
        transferability=Transferability.EMPLOYMENT_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(employment_entity_id=uuid4()),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TRANSFERABILITY_EMPLOYMENT_SCOPE_MISSING
        in result.reason_codes
    )

    result_no_target = evaluate_transferability(
        transferability=Transferability.EMPLOYMENT_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=uuid4(),
        target_context=_target_context(employment_entity_id=None),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result_no_target.outcome == SelectionDecisionOutcome.BLOCKED


def test_entity_scoped_valid_relation_passes() -> None:
    person_entity_id = uuid4()
    result = evaluate_transferability(
        transferability=Transferability.ENTITY_SCOPED,
        fact_entity_id=person_entity_id,
        fact_employment_scope_entity_id=None,
        target_context=_target_context(person_entity_id=person_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome is None


def test_entity_scoped_unverified_relation_requires_approval() -> None:
    result = evaluate_transferability(
        transferability=Transferability.ENTITY_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert (
        FactSelectionReasonCode.TRANSFERABILITY_ENTITY_SCOPE_UNVERIFIED
        in result.reason_codes
    )


@pytest.mark.parametrize(
    "transferability,context_field,reason",
    [
        (
            Transferability.ROLE_TRANSFERABLE,
            "target_role_family",
            FactSelectionReasonCode.TRANSFERABILITY_ROLE_CONTEXT_MISSING,
        ),
        (
            Transferability.INDUSTRY_TRANSFERABLE,
            "target_organization_or_industry",
            FactSelectionReasonCode.TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING,
        ),
    ],
)
def test_role_and_industry_metadata_present_passes(
    transferability: Transferability, context_field: str, reason: FactSelectionReasonCode
) -> None:
    result = evaluate_transferability(
        transferability=transferability,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(**{context_field: "PRESENT"}),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome is None


@pytest.mark.parametrize(
    "transferability,context_field,reason",
    [
        (
            Transferability.ROLE_TRANSFERABLE,
            "target_role_family",
            FactSelectionReasonCode.TRANSFERABILITY_ROLE_CONTEXT_MISSING,
        ),
        (
            Transferability.INDUSTRY_TRANSFERABLE,
            "target_organization_or_industry",
            FactSelectionReasonCode.TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING,
        ),
    ],
)
def test_role_and_industry_metadata_missing_requires_approval(
    transferability: Transferability, context_field: str, reason: FactSelectionReasonCode
) -> None:
    result = evaluate_transferability(
        transferability=transferability,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(**{context_field: None}),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert reason in result.reason_codes


def test_global_transferable_has_no_extra_restriction() -> None:
    result = evaluate_transferability(
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome is None


def test_exact_only_blocks_controlled_rephrase_but_allows_exact_copy() -> None:
    blocked = evaluate_transferability(
        transferability=Transferability.EXACT_ONLY,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
    )
    assert blocked.outcome == SelectionDecisionOutcome.BLOCKED
    assert (
        FactSelectionReasonCode.TRANSFERABILITY_EXACT_ONLY_REPHRASE_BLOCKED
        in blocked.reason_codes
    )

    allowed = evaluate_transferability(
        transferability=Transferability.EXACT_ONLY,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=None,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert allowed.outcome is None


def test_no_textual_fallback_is_used_for_scope_matching() -> None:
    """Changing free-text job_or_application_id must never change a
    NON_TRANSFERABLE or ENTITY_SCOPED decision -- only entity ids matter."""

    person_entity_id = uuid4()
    fact_entity_id = uuid4()
    for job_text in ("job-alpha", "job-beta completely different text"):
        result = evaluate_transferability(
            transferability=Transferability.ENTITY_SCOPED,
            fact_entity_id=fact_entity_id,
            fact_employment_scope_entity_id=None,
            target_context=_target_context(
                person_entity_id=person_entity_id, job_or_application_id=job_text
            ),
            requested_operation=TransformationOperation.EXACT_COPY,
        )
        assert result.outcome == SelectionDecisionOutcome.APPROVAL_REQUIRED


def test_no_cross_employment_attribution() -> None:
    """A fact scoped to one employment can never be silently attributed to a
    different employment context, regardless of how similar the two look."""

    fact_employment = uuid4()
    other_employment = uuid4()
    result = evaluate_transferability(
        transferability=Transferability.EMPLOYMENT_SCOPED,
        fact_entity_id=uuid4(),
        fact_employment_scope_entity_id=fact_employment,
        target_context=_target_context(employment_entity_id=other_employment),
        requested_operation=TransformationOperation.EXACT_COPY,
    )
    assert result.outcome == SelectionDecisionOutcome.BLOCKED
