"""Unit tests for the closed P5.5 policy tables in ``cv_content_approval_policy``."""

from __future__ import annotations

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    FinalDispositionCode,
    ProposalApprovalDecisionValue,
    ProposalSemanticVerdict,
    SemanticValidationStatus,
)
from app.services.cv_content_approval_policy import (
    APPROVABLE_VERDICT,
    APPROVAL_DECISION_POLICY_VERSION,
    APPROVED_CONTENT_POLICY_VERSION,
    DECISION_REQUIRES_PASS,
    DISPOSITION_BY_NON_APPROVABLE_VERDICT,
    NON_APPROVABLE_VERDICTS,
    SEMANTIC_VALIDATION_POLICY_VERSION,
    can_call_semantic_validator,
    default_ready_for_p6,
    is_verdict_approvable,
)


def test_only_pass_is_approvable() -> None:
    assert APPROVABLE_VERDICT == ProposalSemanticVerdict.PASS
    assert is_verdict_approvable(ProposalSemanticVerdict.PASS) is True
    for verdict in (
        ProposalSemanticVerdict.FAIL,
        ProposalSemanticVerdict.INCONCLUSIVE,
        ProposalSemanticVerdict.PROVIDER_ERROR,
        ProposalSemanticVerdict.INVALID_INPUT,
    ):
        assert is_verdict_approvable(verdict) is False


def test_non_approvable_verdicts_is_closed_and_disjoint_from_pass() -> None:
    assert ProposalSemanticVerdict.PASS not in NON_APPROVABLE_VERDICTS
    assert NON_APPROVABLE_VERDICTS == {
        ProposalSemanticVerdict.FAIL,
        ProposalSemanticVerdict.INCONCLUSIVE,
        ProposalSemanticVerdict.PROVIDER_ERROR,
        ProposalSemanticVerdict.INVALID_INPUT,
    }


def test_disposition_by_non_approvable_verdict_is_total_and_closed() -> None:
    assert set(DISPOSITION_BY_NON_APPROVABLE_VERDICT) == NON_APPROVABLE_VERDICTS
    assert DISPOSITION_BY_NON_APPROVABLE_VERDICT[ProposalSemanticVerdict.FAIL] == (
        FinalDispositionCode.SEMANTIC_VALIDATION_FAILED
    )
    assert DISPOSITION_BY_NON_APPROVABLE_VERDICT[ProposalSemanticVerdict.INCONCLUSIVE] == (
        FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE
    )
    assert DISPOSITION_BY_NON_APPROVABLE_VERDICT[ProposalSemanticVerdict.PROVIDER_ERROR] == (
        FinalDispositionCode.SEMANTIC_VALIDATION_PROVIDER_ERROR
    )
    assert DISPOSITION_BY_NON_APPROVABLE_VERDICT[ProposalSemanticVerdict.INVALID_INPUT] == (
        FinalDispositionCode.INPUT_INVALID
    )


def test_can_call_semantic_validator_only_false_for_invalid_input() -> None:
    assert can_call_semantic_validator(SemanticValidationStatus.INVALID_INPUT) is False
    assert can_call_semantic_validator(SemanticValidationStatus.VALIDATED) is True
    assert can_call_semantic_validator(SemanticValidationStatus.BLOCKED) is True
    assert can_call_semantic_validator(SemanticValidationStatus.PROVIDER_ERROR) is True


def test_default_ready_for_p6_only_true_for_ready_status() -> None:
    assert default_ready_for_p6(ApprovalAssemblyStatus.READY) is True
    for status in (
        ApprovalAssemblyStatus.PENDING_APPROVAL,
        ApprovalAssemblyStatus.BLOCKED,
        ApprovalAssemblyStatus.REQUIRES_REPLAN,
        ApprovalAssemblyStatus.INVALID_INPUT,
    ):
        assert default_ready_for_p6(status) is False


def test_decision_requires_pass_table_is_closed() -> None:
    assert DECISION_REQUIRES_PASS == {
        ProposalApprovalDecisionValue.APPROVED: True,
        ProposalApprovalDecisionValue.REJECTED: False,
    }


def test_policy_versions_are_non_empty_strings() -> None:
    assert SEMANTIC_VALIDATION_POLICY_VERSION
    assert APPROVAL_DECISION_POLICY_VERSION
    assert APPROVED_CONTENT_POLICY_VERSION
    assert isinstance(SEMANTIC_VALIDATION_POLICY_VERSION, str)
    assert isinstance(APPROVAL_DECISION_POLICY_VERSION, str)
    assert isinstance(APPROVED_CONTENT_POLICY_VERSION, str)


def test_award_fact_type_remains_unsupported_via_shared_p5b_policy() -> None:
    # P5.5 deliberately reuses P5b's own AWARD boundary rather than declaring
    # a second, potentially-drifting AWARD table.
    from app.services.cv_content_proposal_policy import is_award_fact_type

    assert is_award_fact_type("AWARD_NAME") is True
    assert is_award_fact_type("SUMMARY") is False
