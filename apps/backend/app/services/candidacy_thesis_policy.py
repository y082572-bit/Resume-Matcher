"""Deterministic, closed policy rules for Candidacy Thesis safety.

Nothing here calls a provider, touches a clock, or performs open-ended
NLP -- every guardrail is a fixed, closed keyword set so behavior is fully
deterministic and testable.
"""

from __future__ import annotations

import re
import unicodedata

from app.schemas.candidacy_thesis import CandidacyThesisProviderErrorCode


#: Maximum total attempts the builder will ever make for one thesis call --
#: one base attempt plus at most one retry.
MAXIMUM_TOTAL_ATTEMPTS = 2

#: Retry is only ever attempted for transient transport-level failures.
RETRYABLE_PROVIDER_ERROR_CODES: frozenset[CandidacyThesisProviderErrorCode] = frozenset(
    {
        CandidacyThesisProviderErrorCode.TIMEOUT,
        CandidacyThesisProviderErrorCode.TRANSPORT_ERROR,
    }
)

#: Closed keyword set for guaranteed-future-outcome language. A thesis
#: statement or strategic contribution statement may never promise a
#: specific future result -- only that Marek's approved history is
#: credible evidence of capability.
FUTURE_OUTCOME_GUARANTEE_KEYWORDS: tuple[str, ...] = (
    "na pewno poprawi",
    "z pewnością osiągnie",
    "gwarantuje",
    "gwarantujemy",
    "gwarantowany",
    "zapewni identyczny wynik",
    "z pewnością",
    "na pewno",
    "zapewni",
    "pewne jest",
    "will definitely",
    "will certainly",
    "is guaranteed",
    "guarantees",
    "guaranteed to",
    "will achieve the same result",
    "will replicate the same result",
    "will deliver the exact same",
    "is certain to",
    "promises to deliver",
    "will",
    "certainly",
    "definitely",
)

#: Closed set of unambiguous future-tense result verbs. In this domain's
#: grammar convention a bare perfective/simple-future verb with no modal
#: hedge (no "może"/"can") always asserts a *definite* future outcome --
#: e.g. Polish "zwiększy" ("will increase", certain) versus the
#: infinitive-after-modal "może zwiększyć" ("may increase", hedged), or
#: versus the past tense "zwiększył" ("increased", a historical fact).
#: Matched as whole words only, so this never collides with an unrelated
#: inflected form that merely shares a prefix (e.g. "poprawie"/"poprawy").
FUTURE_RESULT_VERB_KEYWORDS: tuple[str, ...] = (
    "osiągnie",
    "zwiększy",
    "poprawi",
    "powtórzy",
    "dowiezie",
    "zrealizuje",
    "zapewni",
    "odniesie",
    "achieve",
    "increase",
    "improve",
    "deliver",
    "repeat",
)

#: Closed keyword set for "perfect candidate" / superiority language --
#: never permitted, since this module never ranks or scores a candidate.
PERFECT_CANDIDATE_KEYWORDS: tuple[str, ...] = (
    "idealny kandydat",
    "idealnym kandydatem",
    "perfect candidate",
    "ideal candidate",
    "best candidate",
    "najlepszy kandydat",
    "the perfect fit",
)

#: Closed keyword set for forbidden candidate-scoring/recommendation
#: language that must never appear in generated thesis text.
FORBIDDEN_SCORING_LANGUAGE_KEYWORDS: tuple[str, ...] = (
    "match score",
    "fit score",
    "suitability score",
    "probability of hire",
    "should apply",
    "should not apply",
    "recommend applying",
    "recommend against applying",
    "should skip this",
)


def normalize_for_matching(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _keyword_present_as_whole_phrase(normalized_text: str, keyword: str) -> bool:
    """Word-boundary match so a keyword never collides with an unrelated
    word that merely shares a prefix/suffix (e.g. the verb ``poprawi``
    inside the unrelated noun form ``poprawie``)."""

    return re.search(rf"\b{re.escape(keyword)}\b", normalized_text) is not None


def contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = normalize_for_matching(text)
    return any(_keyword_present_as_whole_phrase(normalized, keyword) for keyword in keywords)


def violates_future_outcome_guarantee(text: str) -> bool:
    return contains_any_keyword(text, FUTURE_OUTCOME_GUARANTEE_KEYWORDS) or contains_any_keyword(
        text, FUTURE_RESULT_VERB_KEYWORDS
    )


def violates_perfect_candidate_claim(text: str) -> bool:
    return contains_any_keyword(text, PERFECT_CANDIDATE_KEYWORDS)


def violates_forbidden_scoring_language(text: str) -> bool:
    return contains_any_keyword(text, FORBIDDEN_SCORING_LANGUAGE_KEYWORDS)


def thesis_text_is_safe(text: str) -> bool:
    return not (
        violates_future_outcome_guarantee(text)
        or violates_perfect_candidate_claim(text)
        or violates_forbidden_scoring_language(text)
    )
