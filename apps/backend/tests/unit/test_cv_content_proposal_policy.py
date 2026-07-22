"""``cv_content_proposal_policy``: closed eligibility, AWARD boundary."""

from __future__ import annotations

from app.schemas.cv_content_proposal import ProposalProviderErrorCode, ProposalViolationCode
from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_proposal_policy import (
    ELIGIBLE_OPERATIONS,
    RETRYABLE_PROVIDER_ERROR_CODES,
    classify_eligibility,
    is_award_fact_type,
)


def test_only_controlled_rephrase_and_flatten_are_eligible() -> None:
    assert ELIGIBLE_OPERATIONS == frozenset(
        {
            TransformationOperation.CONTROLLED_REPHRASE,
            TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        }
    )
    assert classify_eligibility(TransformationOperation.CONTROLLED_REPHRASE) is None
    assert classify_eligibility(TransformationOperation.FLATTEN_FOR_LOWER_ROLE) is None


def test_deterministic_operations_are_not_eligible() -> None:
    for operation in (
        TransformationOperation.EXACT_COPY,
        TransformationOperation.FORMAT_NORMALIZATION,
        TransformationOperation.REORDER,
    ):
        assert classify_eligibility(operation) == ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B


def test_other_llm_operations_are_not_eligible() -> None:
    for operation in (
        TransformationOperation.ELEVATE_PRESENTATION_FOR_SENIOR_ROLE,
        TransformationOperation.COMBINE_APPROVED_FACTS,
        TransformationOperation.SPLIT_APPROVED_FACT,
        TransformationOperation.SUMMARIZE_APPROVED_FACTS,
    ):
        assert classify_eligibility(operation) == ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B


def test_omit_is_not_generatable() -> None:
    assert classify_eligibility(TransformationOperation.OMIT) == ProposalViolationCode.OMIT_NOT_GENERATABLE


def test_every_transformation_operation_is_classified() -> None:
    for operation in TransformationOperation:
        # Must never raise KeyError -- classify_eligibility is total over
        # the closed TransformationOperation enum.
        result = classify_eligibility(operation)
        if operation in ELIGIBLE_OPERATIONS:
            assert result is None
        else:
            assert isinstance(result, ProposalViolationCode)


def test_award_name_is_out_of_scope() -> None:
    assert is_award_fact_type("AWARD_NAME") is True
    assert is_award_fact_type("EMPLOYMENT_ACTIVITY") is False
    assert is_award_fact_type("SUMMARY") is False


def test_retry_is_only_allowed_for_timeout_and_transport_error() -> None:
    assert RETRYABLE_PROVIDER_ERROR_CODES == frozenset(
        {ProposalProviderErrorCode.TIMEOUT, ProposalProviderErrorCode.TRANSPORT_ERROR}
    )
    for code in ProposalProviderErrorCode:
        if code in RETRYABLE_PROVIDER_ERROR_CODES:
            continue
        assert code in (
            ProposalProviderErrorCode.PROVIDER_REJECTED,
            ProposalProviderErrorCode.INVALID_RESPONSE_JSON,
            ProposalProviderErrorCode.RESPONSE_SCHEMA_MISMATCH,
            ProposalProviderErrorCode.EMPTY_RESPONSE,
            ProposalProviderErrorCode.TOKEN_LIMIT,
            ProposalProviderErrorCode.CONTENT_FILTERED,
            ProposalProviderErrorCode.UNSUPPORTED_FINISH_REASON,
            ProposalProviderErrorCode.RETRY_EXHAUSTED,
        )
