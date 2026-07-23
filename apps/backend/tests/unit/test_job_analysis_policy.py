"""Job Posting Analysis boundary and deterministic policy guardrails:
Marek-isolation, forbidden candidate-scoring fields, the overclaim guard,
and the numeric-outcome support check.

All posting text and evidence is synthetic; no real job posting or
employer data is used.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.schemas.job_posting_analysis import (
    ControlledJobAnalysisRequest,
    ControlledJobAnalysisResponse,
    JobAnalysisContext,
)
from app.services import (
    job_analysis_builder,
    job_analysis_llm,
    job_analysis_policy,
    job_analysis_prompt,
)
from app.services.job_analysis_policy import (
    claim_is_supported_by_evidence,
    explicit_claim_is_supported_by_cited_evidence,
    numeric_claim_is_supported,
    statement_equals_role_title,
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
    "cv",
    "cv_text",
    "master_resume",
    "candidate_profile",
    "facts",
    "truth_library",
    "prior_applications",
    "other_postings",
}


# -- 11-14. Job Analysis never carries Marek's facts, CV, Master Resume, or
# other postings -- verified structurally: none of these closed models has
# a field for any of them. --


@pytest.mark.parametrize(
    "model_cls", [ControlledJobAnalysisRequest, ControlledJobAnalysisResponse, JobAnalysisContext]
)
def test_job_analysis_models_carry_no_marek_or_forbidden_fields(model_cls):
    field_names = set(model_cls.model_fields)
    assert field_names.isdisjoint(FORBIDDEN_FIELD_NAMES)


def test_controlled_job_analysis_request_is_closed_to_extra_fields():
    with pytest.raises(ValidationError):
        ControlledJobAnalysisRequest(
            prompt_template_version="v1",
            posting_fingerprint="a" * 64,
            source_language="en",
            segments=(),
            response_schema_version="v1",
            candidate_cv_text="should never be accepted",
        )


# -- 15. Job Analysis never imports Career Positioning --


@pytest.mark.parametrize(
    "module", [job_analysis_builder, job_analysis_llm, job_analysis_policy, job_analysis_prompt]
)
def test_job_analysis_modules_do_not_import_career_positioning(module):
    source = inspect.getsource(module)
    assert "career_positioning" not in source


# -- 16-20. No forbidden candidate-scoring/recommendation fields anywhere
# in the closed response schema -- enforced structurally by
# ``extra="forbid"``: no client can ever attach one. --


def test_response_schema_rejects_forbidden_scoring_fields():
    with pytest.raises(ValidationError):
        ControlledJobAnalysisResponse(
            response_schema_version="v1",
            posting_fingerprint="a" * 64,
            match_score=0.9,
        )
    with pytest.raises(ValidationError):
        ControlledJobAnalysisResponse(
            response_schema_version="v1",
            posting_fingerprint="a" * 64,
            apply_recommendation="APPLY",
        )


def test_response_schema_field_names_never_include_forbidden_terms():
    field_names = set(ControlledJobAnalysisResponse.model_fields)
    assert field_names.isdisjoint(FORBIDDEN_FIELD_NAMES)


# -- overclaim guard --


def test_overclaim_guard_blocks_unsupported_crisis_claim():
    assert claim_is_supported_by_evidence("The company is in crisis", []) is False


def test_overclaim_guard_passes_when_claim_has_no_absolute_keyword():
    assert claim_is_supported_by_evidence("The team is scaling a new product line", []) is True


def test_overclaim_guard_passes_when_evidence_directly_supports_the_claim():
    evidence = ["due to recent layoffs, the team needs additional capacity"]
    assert claim_is_supported_by_evidence("The company recently had layoffs", evidence) is True


def test_overclaim_guard_blocks_when_evidence_does_not_mention_the_claim():
    evidence = ["we are hiring for a growing team"]
    assert claim_is_supported_by_evidence("The company is losing customers", evidence) is False


# -- numeric outcome support --


def test_numeric_claim_requires_matching_digits_in_evidence():
    assert numeric_claim_is_supported("Improve conversion from 36% to 96%", []) is False


def test_numeric_claim_supported_when_evidence_carries_the_same_digits():
    evidence = ["plan realization improved from 36% to 96% last year"]
    assert numeric_claim_is_supported("Improve conversion from 36% to 96%", evidence) is True


def test_numeric_claim_with_no_numbers_is_always_supported():
    assert numeric_claim_is_supported("Improve team collaboration", []) is True


def test_numeric_claim_partially_unsupported_number_fails():
    evidence = ["plan realization improved to 96%"]
    assert numeric_claim_is_supported("Improve conversion from 36% to 96%", evidence) is False


# -- role objective vs role title --


def test_statement_equal_to_role_title_after_normalization_is_detected():
    assert statement_equals_role_title("  Senior Sales Manager ", "Senior Sales Manager") is True
    assert statement_equals_role_title("senior sales manager", "Senior Sales Manager") is True


def test_statement_distinct_from_role_title_is_not_flagged():
    assert (
        statement_equals_role_title("Drive enterprise sales execution", "Senior Sales Manager")
        is False
    )


def test_statement_equals_role_title_handles_missing_role_title():
    assert statement_equals_role_title("Anything", None) is False


# -- explicit assertion-basis validation: EXPLICIT_IN_POSTING is never
# trusted on its own -- it must be independently supported by the item's
# own cited evidence segments. --


def test_explicit_claim_supported_by_normalized_containment_in_cited_evidence():
    assert (
        explicit_claim_is_supported_by_cited_evidence(
            "Lead the sales team", ["Lead the sales team to improve plan realization"]
        )
        is True
    )


def test_explicit_claim_not_supported_when_absent_from_cited_evidence():
    assert (
        explicit_claim_is_supported_by_cited_evidence(
            "Drive enterprise sales execution",
            ["Lead the sales team to improve plan realization"],
        )
        is False
    )


def test_explicit_claim_not_supported_by_an_uncited_segment():
    # the claim text is present verbatim in *some* segment, but that
    # segment was never cited by this item -- only cited evidence counts.
    assert (
        explicit_claim_is_supported_by_cited_evidence(
            "Own the CRM pipeline", ["Lead the sales team to improve plan realization"]
        )
        is False
    )


def test_explicit_claim_matching_is_case_and_whitespace_insensitive():
    assert (
        explicit_claim_is_supported_by_cited_evidence(
            "  LEAD   the Sales Team  ", ["Lead the sales team to improve plan realization"]
        )
        is True
    )


def test_explicit_claim_matching_normalizes_nfkc():
    assert (
        explicit_claim_is_supported_by_cited_evidence(
            "lead the sales team", ["Ｌead the sales team to improve plan realization"]
        )
        is True
    )


def test_explicit_claim_with_no_cited_evidence_texts_is_unsupported():
    assert explicit_claim_is_supported_by_cited_evidence("Lead the sales team", []) is False


def test_builder_wires_in_the_explicit_assertion_basis_guard():
    from app.services import job_analysis_builder

    source = inspect.getsource(job_analysis_builder)
    assert "explicit_claim_is_supported_by_cited_evidence" in source
