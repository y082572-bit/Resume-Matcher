"""Closed, versioned P5.5 policy tables and pure classification helpers.

Every table here is a plain, explicit dict, tuple or frozenset -- no
``startswith``, no substring matching, no text classification. Nothing here
calls a provider, reads a clock, or touches a database.
"""

from __future__ import annotations

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    FinalDispositionCode,
    ProposalApprovalDecisionValue,
    ProposalSemanticVerdict,
    SemanticValidationStatus,
)


SEMANTIC_VALIDATION_POLICY_VERSION = "cv-content-semantic-validation-policy-v1"
APPROVAL_DECISION_POLICY_VERSION = "proposal-approval-decision-policy-v1"
APPROVED_CONTENT_POLICY_VERSION = "approved-cv-content-policy-v1"

#: The only semantic verdict an approval decision may ever be built on top
#: of. Every other verdict (FAIL/INCONCLUSIVE/PROVIDER_ERROR/INVALID_INPUT)
#: is fail-closed to "cannot be approved" -- see
#: ``cv_content_approval_builder``.
APPROVABLE_VERDICT = ProposalSemanticVerdict.PASS

#: Verdicts that can never be approved, regardless of a user's decision.
NON_APPROVABLE_VERDICTS: frozenset[ProposalSemanticVerdict] = frozenset(
    {
        ProposalSemanticVerdict.FAIL,
        ProposalSemanticVerdict.INCONCLUSIVE,
        ProposalSemanticVerdict.PROVIDER_ERROR,
        ProposalSemanticVerdict.INVALID_INPUT,
    }
)

#: Closed verdict -> disposition map used only when a proposal was NOT
#: approved (or has no decision yet) -- APPROVED+PASS always maps to
#: INCLUDED_APPROVED_PROPOSAL, handled separately by the builder.
DISPOSITION_BY_NON_APPROVABLE_VERDICT: dict[ProposalSemanticVerdict, FinalDispositionCode] = {
    ProposalSemanticVerdict.FAIL: FinalDispositionCode.SEMANTIC_VALIDATION_FAILED,
    ProposalSemanticVerdict.INCONCLUSIVE: FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE,
    ProposalSemanticVerdict.PROVIDER_ERROR: FinalDispositionCode.SEMANTIC_VALIDATION_PROVIDER_ERROR,
    ProposalSemanticVerdict.INVALID_INPUT: FinalDispositionCode.INPUT_INVALID,
}


def is_verdict_approvable(verdict: ProposalSemanticVerdict) -> bool:
    """``True`` only for ``ProposalSemanticVerdict.PASS``.

    A ``PASS`` verdict is a necessary, never sufficient, condition for
    approval -- the caller must still supply an explicit
    ``ProposalApprovalDecisionValue.APPROVED``.
    """

    return verdict == APPROVABLE_VERDICT


def can_call_semantic_validator(status: SemanticValidationStatus) -> bool:
    """Whether Step 1 was allowed to reach the point of calling a validator.

    Never used to gate an individual item -- only documents that a
    fail-closed result-level status (``INVALID_INPUT``/``BLOCKED`` from an
    unready P4/P5a/P5b) means zero validator calls were made.
    """

    return status not in (SemanticValidationStatus.INVALID_INPUT,)


def default_ready_for_p6(status: ApprovalAssemblyStatus) -> bool:
    return status == ApprovalAssemblyStatus.READY


#: Whether a ``ProposalApprovalDecisionValue`` requires the underlying
#: proposal to have a ``PASS`` semantic verdict before it can be honored.
DECISION_REQUIRES_PASS: dict[ProposalApprovalDecisionValue, bool] = {
    ProposalApprovalDecisionValue.APPROVED: True,
    ProposalApprovalDecisionValue.REJECTED: False,
}
