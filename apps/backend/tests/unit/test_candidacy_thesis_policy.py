"""Candidacy Thesis boundary and deterministic safety guardrails:
forbidden candidate-scoring fields, future-outcome guarantees, and
"perfect candidate" language. ``PositioningLevel`` is independent of the
Career Positioning Engine.

All statement text is synthetic; no real candidate or employer data is
used.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.schemas.candidacy_thesis import (
    CandidacyThesisContext,
    ControlledCandidacyThesisResponse,
    PositioningLevel,
)
from app.services import (
    candidacy_thesis_builder,
    candidacy_thesis_llm,
    candidacy_thesis_policy,
    candidacy_thesis_prompt,
)
from app.services.candidacy_thesis_policy import (
    FUTURE_RESULT_VERB_KEYWORDS,
    thesis_text_is_safe,
    violates_forbidden_scoring_language,
    violates_future_outcome_guarantee,
    violates_perfect_candidate_claim,
)


FORBIDDEN_FIELD_NAMES = {
    "match_score",
    "fit_score",
    "suitability_score",
    "probability_of_hire",
    "application_recommendation",
    "apply_or_skip",
    "candidate_risk",
    "candidacy_risk",
    "weaknesses",
    "candidate_gaps",
    "recruiter_concerns",
    "rejection_recommendation",
    "overqualification_risk",
    "missing_critical",
    "excessive_for_role",
    "pending_facts",
    "rejected_facts",
    "truth_library",
    "master_resume",
}


# -- 59-62. No forbidden candidate-scoring fields anywhere in the closed
# thesis context/response models --


@pytest.mark.parametrize("model_cls", [CandidacyThesisContext, ControlledCandidacyThesisResponse])
def test_thesis_models_carry_no_forbidden_fields(model_cls):
    assert set(model_cls.model_fields).isdisjoint(FORBIDDEN_FIELD_NAMES)


def test_thesis_response_schema_rejects_forbidden_scoring_fields():
    with pytest.raises(ValidationError):
        ControlledCandidacyThesisResponse(
            response_schema_version="v1",
            posting_fingerprint="a" * 64,
            analysis_result_fingerprint="b" * 64,
            acknowledged_positioning_level=PositioningLevel.MANAGEMENT,
            target_narrative_emphasis=("BUSINESS_RESULT",),
            match_score=0.9,
        )


# -- 58. No import of Career Positioning anywhere in PRE-P4 Step 2 --


@pytest.mark.parametrize(
    "module",
    [candidacy_thesis_builder, candidacy_thesis_llm, candidacy_thesis_policy, candidacy_thesis_prompt],
)
def test_candidacy_thesis_modules_do_not_import_career_positioning(module):
    source = inspect.getsource(module)
    assert "career_positioning" not in source


# -- 79-81. PositioningLevel is independent of Career Positioning and never
# rewrites history or creates new claims by itself --


def test_positioning_level_is_a_closed_enum_independent_of_cpe():
    assert set(PositioningLevel) == {
        PositioningLevel.EXECUTIVE,
        PositioningLevel.SENIOR_LEADERSHIP,
        PositioningLevel.MANAGEMENT,
        PositioningLevel.EXPERT,
        PositioningLevel.SPECIALIST,
        PositioningLevel.OPERATIONAL,
    }
    for module in (candidacy_thesis_builder, candidacy_thesis_policy):
        source = inspect.getsource(module)
        assert "PositioningDetailsSchema" not in source
        assert "positioning_strategy" not in source


# -- 74-78. Thesis-safety guard: no future-outcome guarantees, no "perfect
# candidate" claims, no scoring language, no application recommendation --


def test_future_outcome_guarantee_is_detected():
    assert violates_future_outcome_guarantee("Marek will definitely improve results") is True
    assert (
        violates_future_outcome_guarantee(
            "Marek's experience is credible evidence he can help here"
        )
        is False
    )


def test_perfect_candidate_language_is_detected():
    assert violates_perfect_candidate_claim("Marek is the perfect candidate for this role") is True
    assert violates_perfect_candidate_claim("Marek's background is directly relevant") is False


def test_forbidden_scoring_language_is_detected():
    assert violates_forbidden_scoring_language("Marek has a high match score for this role") is True
    assert violates_forbidden_scoring_language("Marek should apply immediately") is True
    assert violates_forbidden_scoring_language("Marek's history is relevant here") is False


def test_thesis_text_is_safe_combines_all_guards():
    assert thesis_text_is_safe("Marek's track record is credible evidence of capability") is True
    assert thesis_text_is_safe("Marek is guaranteed to succeed in this role") is False
    assert thesis_text_is_safe("Marek is the ideal candidate") is False
    assert thesis_text_is_safe("Marek has the best fit score among applicants") is False


# -- retry policy constants --


def test_future_claim_guard_never_stores_a_rewritten_statement():
    from app.schemas.candidacy_thesis import BlockedCandidacyThesisItem

    # a blocked item never carries the (possibly-unsafe) original or a
    # rewritten/sanitized statement -- only a fixed violation code/detail.
    assert "statement" not in set(BlockedCandidacyThesisItem.model_fields)


@pytest.mark.parametrize(
    "text",
    [
        "Marek z pewnością zwiększy sprzedaż",
        "Marek osiągnie 96% realizacji planu",
        "Marek gwarantuje poprawę wyników",
        "Marek na pewno poprawi sprzedaż",
        "Marek zapewni wzrost sprzedaży",
        "Marek powtórzy wynik 96%",
        "Marek dowiezie 120% planu",
        "Marek osiągnie identyczny rezultat",
        "Marek zrealizuje zakładane KPI",
        "Marek na pewno odniesie sukces",
    ],
)
def test_future_claim_guard_blocks_required_polish_examples(text):
    assert violates_future_outcome_guarantee(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Marek will increase sales",
        "Marek will achieve 96% of target",
        "Marek guarantees improved results",
    ],
)
def test_future_claim_guard_blocks_required_english_examples(text):
    assert violates_future_outcome_guarantee(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Doświadczenie Marka stanowi wiarygodny dowód zdolności do poprawy wykonania planu",
        "Marek może wykorzystać doświadczenie w poprawie efektywności sprzedaży",
        "Doświadczenie Marka może wspierać realizację celów sprzedażowych",
        "Wynik 96% stanowi historyczny dowód skuteczności podjętych działań",
        "Marek osiągnął wcześniej 96% realizacji planu",
        "Pracodawca może wykorzystać doświadczenie Marka w transformacji sprzedaży",
        "Marek prawdopodobnie może wykorzystać wcześniejsze doświadczenie",
    ],
)
def test_future_claim_guard_allows_required_hedged_and_historical_examples(text):
    assert violates_future_outcome_guarantee(text) is False


def test_future_result_verb_with_percentage_is_blocked():
    assert violates_future_outcome_guarantee("Marek osiągnie 96%") is True


def test_past_tense_with_historical_percentage_is_allowed():
    assert violates_future_outcome_guarantee("Marek osiągnął 96%") is False


def test_future_result_verb_alone_is_blocked():
    assert violates_future_outcome_guarantee("Marek zwiększy sprzedaż") is True


def test_past_tense_result_verb_is_allowed():
    assert violates_future_outcome_guarantee("Marek zwiększył sprzedaż") is False


def test_hedged_modal_future_verb_is_allowed():
    assert violates_future_outcome_guarantee("Marek może zwiększyć sprzedaż") is False


def test_certain_future_verb_with_hedge_word_still_blocks():
    assert violates_future_outcome_guarantee("Marek na pewno zwiększy sprzedaż") is True


def test_future_verb_with_currency_amount_is_blocked():
    assert violates_future_outcome_guarantee("Marek dowiezie 120 tysięcy złotych zysku") is True


def test_future_verb_with_kpi_token_is_blocked():
    assert violates_future_outcome_guarantee("Marek zrealizuje KPI") is True


def test_future_claim_guard_does_not_rely_on_a_single_full_phrase():
    # blocks via the bare future-result verb alone -- none of the
    # explicit certainty phrases ("na pewno"/"z pewnością"/...) is present.
    assert violates_future_outcome_guarantee("Marek zrealizuje zakładane KPI") is True
    assert "zrealizuje" in FUTURE_RESULT_VERB_KEYWORDS


def test_future_claim_guard_is_case_insensitive():
    assert violates_future_outcome_guarantee("MAREK ZWIĘKSZY SPRZEDAŻ") is True


def test_future_claim_guard_normalizes_nfkc():
    # a full-width variant of the same ASCII text should still match after
    # NFKC normalization.
    assert violates_future_outcome_guarantee("Marek ｗｉｌｌ increase sales") is True


def test_future_claim_guard_uses_no_llm_or_provider():
    source = inspect.getsource(candidacy_thesis_policy)
    assert "litellm" not in source
    assert "app.llm" not in source
    assert "import re" in source


def test_retry_policy_allows_only_transient_transport_codes():
    from app.schemas.candidacy_thesis import CandidacyThesisProviderErrorCode

    assert candidacy_thesis_policy.RETRYABLE_PROVIDER_ERROR_CODES == frozenset(
        {
            CandidacyThesisProviderErrorCode.TIMEOUT,
            CandidacyThesisProviderErrorCode.TRANSPORT_ERROR,
        }
    )
    assert candidacy_thesis_policy.MAXIMUM_TOTAL_ATTEMPTS == 2
