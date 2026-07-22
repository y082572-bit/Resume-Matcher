"""Deterministic P5a renderers: ``value_json`` -> ``generated_text``.

All facts are synthetic; no real person or CV data is used.
"""

from __future__ import annotations

from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_generation_policy import resolve_payload_shape
from app.services.cv_content_generation_renderer import render_generated_text


def _render(fact_type: str, value_json, operation=TransformationOperation.EXACT_COPY) -> str:
    shape = resolve_payload_shape(fact_type, value_json)
    assert shape.is_valid, shape.detail
    return render_generated_text(fact_type=fact_type, operation=operation, shape=shape)


# -- Operation semantics --


def test_exact_copy_returns_exact_source_text() -> None:
    text = "  Text with   irregular   spacing  "
    assert _render("SUMMARY", text, TransformationOperation.EXACT_COPY) == text


def test_reorder_returns_exact_source_text_like_exact_copy() -> None:
    text = "Some achievement text"
    assert _render("ACHIEVEMENT", text, TransformationOperation.REORDER) == text


def test_format_normalization_applies_only_nfkc_and_whitespace() -> None:
    text = "Text   with\nnewlines\tand   tabs"
    result = _render("SUMMARY", text, TransformationOperation.FORMAT_NORMALIZATION)
    assert result == "Text with newlines and tabs"


def test_format_normalization_applies_nfkc_compatibility_form() -> None:
    # U+FB01 LATIN SMALL LIGATURE FI normalizes under NFKC to "fi".
    text = "efﬁcient workflow"
    result = _render("SUMMARY", text, TransformationOperation.FORMAT_NORMALIZATION)
    assert result == "efficient workflow"


def test_no_str_dict_or_repr_leaks_into_generated_text() -> None:
    text = "Plain text value"
    result = _render("SUMMARY", {"text": text}, TransformationOperation.EXACT_COPY)
    assert result == text
    assert "{" not in result and "}" not in result


# -- Narrative fact types --


def test_summary_renders_exact_text() -> None:
    assert _render("SUMMARY", "Experienced analyst.") == "Experienced analyst."


def test_skill_renders_exact_text() -> None:
    assert _render("SKILL", "Python") == "Python"


def test_tool_renders_exact_text() -> None:
    assert _render("TOOL", "Git") == "Git"


def test_technology_renders_exact_text() -> None:
    assert _render("TECHNOLOGY", "PostgreSQL") == "PostgreSQL"


def test_company_renders_exact_text() -> None:
    assert _render("COMPANY", "Synthetic Co") == "Synthetic Co"


def test_role_renders_exact_text() -> None:
    assert _render("ROLE", "Senior Analyst") == "Senior Analyst"


def test_employment_period_renders_exact_text() -> None:
    assert _render("EMPLOYMENT_PERIOD", "2020 - 2022") == "2020 - 2022"


def test_responsibility_renders_exact_text() -> None:
    assert _render("RESPONSIBILITY", "Managed a team of 5") == "Managed a team of 5"


def test_employment_activity_renders_exact_text() -> None:
    assert _render("EMPLOYMENT_ACTIVITY", "Did synthetic analysis work") == (
        "Did synthetic analysis work"
    )


def test_employment_responsibility_scale_renders_exact_text() -> None:
    assert _render("EMPLOYMENT_RESPONSIBILITY_SCALE", "Budget 1M PLN") == "Budget 1M PLN"


def test_achievement_renders_exact_text() -> None:
    assert _render("ACHIEVEMENT", "Reduced churn by 15%") == "Reduced churn by 15%"


def test_employment_numeric_result_renders_exact_text() -> None:
    assert _render("EMPLOYMENT_NUMERIC_RESULT", "+30%") == "+30%"


# -- Structured fact types --


def test_employment_role_composes_stanowisko_firma() -> None:
    result = _render("EMPLOYMENT_ROLE", {"stanowisko": "Analyst", "firma": "Synthetic Co"})
    assert result == "Analyst, Synthetic Co"


def test_employment_role_appends_full_period() -> None:
    result = _render(
        "EMPLOYMENT_ROLE",
        {
            "stanowisko": "Analyst",
            "firma": "Synthetic Co",
            "okresOd": "2020",
            "okresDo": "2022",
        },
    )
    assert result == "Analyst, Synthetic Co | 2020 - 2022"


def test_employment_role_appends_open_start_only() -> None:
    result = _render(
        "EMPLOYMENT_ROLE", {"stanowisko": "Analyst", "firma": "Synthetic Co", "okresOd": "2020"}
    )
    assert result == "Analyst, Synthetic Co | 2020"
    assert "obecnie" not in result.lower()
    assert "present" not in result.lower()


def test_employment_role_never_invents_missing_date() -> None:
    result = _render("EMPLOYMENT_ROLE", {"stanowisko": "Analyst", "firma": "Synthetic Co"})
    assert result == "Analyst, Synthetic Co"


def test_employment_role_stanowisko_alt_not_auto_appended() -> None:
    result = _render(
        "EMPLOYMENT_ROLE",
        {"stanowisko": "Analyst", "firma": "Synthetic Co", "stanowiskoAlt": "Senior Analyst"},
    )
    assert "Senior Analyst" not in result
    assert result == "Analyst, Synthetic Co"


def test_education_degree_composes_stopien_kierunek_uczelnia() -> None:
    result = _render(
        "EDUCATION_DEGREE",
        {"stopien": "Magister", "kierunek": "Informatyka", "uczelnia": "AGH"},
    )
    assert result == "Magister, Informatyka, AGH"


def test_education_degree_skips_missing_optional_fields() -> None:
    result = _render("EDUCATION_DEGREE", {"kierunek": "Informatyka"})
    assert result == "Informatyka"


def test_certification_name_renders_nazwa() -> None:
    result = _render("CERTIFICATION_NAME", {"nazwa": "AWS Certified Solutions Architect"})
    assert result == "AWS Certified Solutions Architect"


def test_course_name_renders_nazwa_with_organizator() -> None:
    result = _render("COURSE_NAME", {"nazwa": "Deep Learning", "organizator": "Coursera"})
    assert result == "Deep Learning, Coursera"


def test_course_name_renders_nazwa_without_organizator() -> None:
    result = _render("COURSE_NAME", {"nazwa": "Deep Learning"})
    assert result == "Deep Learning"


def test_language_name_renders_jezyk_with_poziom() -> None:
    result = _render("LANGUAGE_NAME", {"jezyk": "English", "poziom": "C1"})
    assert result == "English - C1"


def test_language_name_renders_jezyk_without_poziom() -> None:
    result = _render("LANGUAGE_NAME", {"jezyk": "English"})
    assert result == "English"


# -- Content preservation --


def test_polish_diacritics_are_preserved() -> None:
    text = "Zażółć gęślą jaźń"
    assert _render("SUMMARY", text) == text
    assert _render("SUMMARY", text, TransformationOperation.FORMAT_NORMALIZATION) == text


def test_numbers_are_preserved() -> None:
    assert _render("EMPLOYMENT_NUMERIC_RESULT", "12345") == "12345"


def test_percentages_are_preserved() -> None:
    assert _render("ACHIEVEMENT", "Increased revenue by 42%") == "Increased revenue by 42%"


def test_currency_is_preserved() -> None:
    assert _render("EMPLOYMENT_RESPONSIBILITY_SCALE", "Budget 1,000,000 PLN") == (
        "Budget 1,000,000 PLN"
    )


def test_dates_are_preserved() -> None:
    result = _render(
        "EMPLOYMENT_ROLE",
        {"stanowisko": "Analyst", "firma": "Co", "okresOd": "2020-01-01", "okresDo": "2022-12-31"},
    )
    assert "2020-01-01" in result
    assert "2022-12-31" in result


def test_company_names_are_preserved() -> None:
    assert _render("COMPANY", "Acme & Sons Sp. z o.o.") == "Acme & Sons Sp. z o.o."


def test_negations_are_preserved() -> None:
    text = "Did not exceed the budget"
    assert _render("RESPONSIBILITY", text) == text
