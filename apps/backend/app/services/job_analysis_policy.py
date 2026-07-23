"""Deterministic, closed policy rules for Job Posting Analysis.

Nothing here calls a provider, touches a clock, or performs open-ended NLP
-- every guardrail is a fixed, closed keyword/pattern set so behavior is
fully deterministic and testable.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from app.schemas.job_posting_analysis import JobAnalysisProviderErrorCode


#: Maximum total attempts the builder will ever make for one analysis call
#: -- one base attempt plus at most one retry.
MAXIMUM_TOTAL_ATTEMPTS = 2

#: Retry is only ever attempted for transient transport-level failures.
RETRYABLE_PROVIDER_ERROR_CODES: frozenset[JobAnalysisProviderErrorCode] = frozenset(
    {
        JobAnalysisProviderErrorCode.TIMEOUT,
        JobAnalysisProviderErrorCode.TRANSPORT_ERROR,
    }
)

#: Closed keyword groups for unsupported, absolute employer-crisis claims.
#: A hypothesis whose ``problem_statement`` matches one of these groups
#: must have that same claim directly evidenced in a supporting segment,
#: or it is rejected by the overclaim guard -- never accepted on inference
#: alone.
OVERCLAIM_GUARD_KEYWORD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("kryzys", "crisis", "in crisis"),
    ("słabe wyniki", "poor performance", "underperforming", "under-performing"),
    ("traci klientów", "losing customers", "losing clients"),
    ("problem finansowy", "financial trouble", "financial problems", "financial difficulty"),
    ("zwalnia", "layoffs", "laying off", "lay off"),
    ("konflikt", "conflict"),
    ("zespół jest nieskuteczny", "team is ineffective", "ineffective team"),
)

_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,]*%?")


def normalize_for_matching(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def matched_overclaim_keyword_groups(text: str) -> tuple[tuple[str, ...], ...]:
    normalized = normalize_for_matching(text)
    matched: list[tuple[str, ...]] = []
    for group in OVERCLAIM_GUARD_KEYWORD_GROUPS:
        if any(keyword in normalized for keyword in group):
            matched.append(group)
    return tuple(matched)


def claim_is_supported_by_evidence(text: str, evidence_texts: Sequence[str]) -> bool:
    """A matched absolute claim group is supported only if at least one
    supporting evidence segment also contains a keyword from the same
    group -- inference alone is never sufficient."""

    matched_groups = matched_overclaim_keyword_groups(text)
    if not matched_groups:
        return True
    normalized_evidence = [normalize_for_matching(t) for t in evidence_texts]
    for group in matched_groups:
        supported = any(
            any(keyword in evidence for keyword in group) for evidence in normalized_evidence
        )
        if not supported:
            return False
    return True


def extract_number_tokens(text: str) -> tuple[str, ...]:
    return tuple(_NUMBER_TOKEN_RE.findall(text))


def _digits_only(token: str) -> str:
    return re.sub(r"[^\d]", "", token)


def numeric_claim_is_supported(claim_text: str, evidence_texts: Sequence[str]) -> bool:
    """If ``claim_text`` carries a number, at least one evidence segment
    must carry that same digit sequence -- never a fabricated KPI."""

    claim_numbers = extract_number_tokens(claim_text)
    if not claim_numbers:
        return True
    evidence_digit_sets = [set(_digits_only(t) for t in extract_number_tokens(e)) for e in evidence_texts]
    for number in claim_numbers:
        digits = _digits_only(number)
        if not digits:
            continue
        if not any(digits in evidence_digits for evidence_digits in evidence_digit_sets):
            return False
    return True


def statement_equals_role_title(statement: str, role_title: str | None) -> bool:
    if role_title is None:
        return False
    return normalize_for_matching(statement) == normalize_for_matching(role_title)


def explicit_claim_is_supported_by_cited_evidence(
    claim_text: str, cited_evidence_texts: Sequence[str]
) -> bool:
    """A claim tagged ``EXPLICIT_IN_POSTING`` is trusted only when its own
    cited evidence segments directly (normalized-containment) support it --
    the model's self-reported ``assertion_basis`` is never accepted on its
    own. Only the segments the item itself cited are considered; a claim
    that appears verbatim in some other, uncited segment is not supported.
    """

    normalized_claim = normalize_for_matching(claim_text)
    if not normalized_claim:
        return False
    return any(
        normalized_claim in normalize_for_matching(text) for text in cited_evidence_texts
    )
