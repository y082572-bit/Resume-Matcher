"""Closed, versioned P5b policy tables: eligible operations only.

Every table here is a plain, explicit dict or frozenset -- no
``startswith``, no substring matching, no text classification. Eligibility
is decided only from the closed ``TransformationOperation`` enum, never
from a violation message, a status, a fact_type, or a target section.
"""

from __future__ import annotations

from app.schemas.cv_content_proposal import ProposalProviderErrorCode, ProposalViolationCode
from app.schemas.truth_fact import TransformationOperation


PROPOSAL_GENERATION_POLICY_VERSION = "cv-content-proposal-policy-v1"

#: The closed set of ``TransformationOperation`` values P5b will generate a
#: controlled LLM proposal for. Never derived from a permission, a
#: transferability tier, or free text.
ELIGIBLE_OPERATIONS: frozenset[TransformationOperation] = frozenset(
    {
        TransformationOperation.CONTROLLED_REPHRASE,
        TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
    }
)

#: Every non-eligible operation's closed, versioned violation code. Absence
#: from this dict for an operation not in ``ELIGIBLE_OPERATIONS`` is a
#: programming error, never silently ignored -- see ``classify_eligibility``.
_NON_ELIGIBLE_OPERATION_VIOLATIONS: dict[TransformationOperation, ProposalViolationCode] = {
    TransformationOperation.EXACT_COPY: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.FORMAT_NORMALIZATION: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.REORDER: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.ELEVATE_PRESENTATION_FOR_SENIOR_ROLE: (
        ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B
    ),
    TransformationOperation.COMBINE_APPROVED_FACTS: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.SPLIT_APPROVED_FACT: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.SUMMARIZE_APPROVED_FACTS: ProposalViolationCode.OPERATION_NOT_ELIGIBLE_FOR_P5B,
    TransformationOperation.OMIT: ProposalViolationCode.OMIT_NOT_GENERATABLE,
}


def classify_eligibility(operation: TransformationOperation) -> ProposalViolationCode | None:
    """Return ``None`` when P5b can propose a rewrite for ``operation``,
    else its closed violation code."""

    if operation in ELIGIBLE_OPERATIONS:
        return None
    return _NON_ELIGIBLE_OPERATION_VIOLATIONS[operation]


#: fact_types explicitly out of scope for P5b, regardless of operation.
#: AWARD stays unimplemented: no prompt, no renderer, no fallback to
#: ACHIEVEMENTS, no text lookup -- fails closed before any adapter call.
AWARD_FACT_TYPES: frozenset[str] = frozenset({"AWARD_NAME"})


def is_award_fact_type(fact_type: str) -> bool:
    return fact_type in AWARD_FACT_TYPES


#: Retry is allowed only for these two closed error codes -- never for a
#: content/shape/identity failure. See ``cv_content_proposal_llm``.
RETRYABLE_PROVIDER_ERROR_CODES: frozenset[ProposalProviderErrorCode] = frozenset(
    {
        ProposalProviderErrorCode.TIMEOUT,
        ProposalProviderErrorCode.TRANSPORT_ERROR,
    }
)
