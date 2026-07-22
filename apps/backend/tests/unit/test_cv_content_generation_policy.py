"""Closed P5a policy tables: supported operations, payload contracts.

All facts/entities are synthetic; no real person or CV data is used.
"""

from __future__ import annotations

from app.schemas.cv_content_generation import GenerationViolationCode
from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_generation_policy import (
    NARRATIVE_TEXT_FACT_TYPES,
    STRUCTURED_FACT_TYPES,
    SUPPORTED_FACT_TYPES,
    SUPPORTED_OPERATIONS,
    classify_operation,
    resolve_payload_shape,
)


# -- Supported operations --


def test_supported_operations_are_exactly_three() -> None:
    assert SUPPORTED_OPERATIONS == {
        TransformationOperation.EXACT_COPY,
        TransformationOperation.FORMAT_NORMALIZATION,
        TransformationOperation.REORDER,
    }


def test_exact_copy_format_normalization_reorder_are_supported() -> None:
    for operation in (
        TransformationOperation.EXACT_COPY,
        TransformationOperation.FORMAT_NORMALIZATION,
        TransformationOperation.REORDER,
    ):
        assert classify_operation(operation) is None


def test_omit_is_not_generatable() -> None:
    assert (
        classify_operation(TransformationOperation.OMIT)
        == GenerationViolationCode.OMIT_NOT_GENERATABLE
    )


def test_controlled_rephrase_and_flatten_are_unsupported() -> None:
    for operation in (
        TransformationOperation.CONTROLLED_REPHRASE,
        TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        TransformationOperation.ELEVATE_PRESENTATION_FOR_SENIOR_ROLE,
        TransformationOperation.COMBINE_APPROVED_FACTS,
        TransformationOperation.SPLIT_APPROVED_FACT,
        TransformationOperation.SUMMARIZE_APPROVED_FACTS,
    ):
        assert (
            classify_operation(operation)
            == GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A
        )


def test_every_transformation_operation_is_classified() -> None:
    for operation in TransformationOperation:
        # Must not raise KeyError -- every operation has a closed outcome.
        classify_operation(operation)


# -- Payload contract: closed, versioned, no fallback --


def test_unknown_fact_type_fails_closed() -> None:
    result = resolve_payload_shape("AWARD_NAME", "irrelevant")
    assert result.violation_code == GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED


def test_award_name_is_not_in_supported_fact_types() -> None:
    assert "AWARD_NAME" not in SUPPORTED_FACT_TYPES


def test_supported_fact_types_is_union_of_narrative_and_structured() -> None:
    assert SUPPORTED_FACT_TYPES == NARRATIVE_TEXT_FACT_TYPES | STRUCTURED_FACT_TYPES
    assert NARRATIVE_TEXT_FACT_TYPES.isdisjoint(STRUCTURED_FACT_TYPES)


def test_no_startswith_or_prefix_fallback_for_similar_fact_type() -> None:
    # "EMPLOYMENT_ROLE_EXTRA" is not a registered fact_type even though it
    # starts with a supported one -- there is no prefix/startswith fallback.
    result = resolve_payload_shape("EMPLOYMENT_ROLE_EXTRA", {"firma": "X", "stanowisko": "Y"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED


def test_narrative_scalar_string_passes() -> None:
    result = resolve_payload_shape("SUMMARY", "Some summary text")
    assert result.is_valid
    assert result.narrative_text == "Some summary text"


def test_narrative_object_with_text_key_passes() -> None:
    result = resolve_payload_shape("SUMMARY", {"text": "Some summary text"})
    assert result.is_valid
    assert result.narrative_text == "Some summary text"


def test_narrative_missing_text_key_fails_closed() -> None:
    result = resolve_payload_shape("SUMMARY", {"other": "value"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_narrative_blank_scalar_fails_closed() -> None:
    result = resolve_payload_shape("SUMMARY", "   ")
    assert result.violation_code == GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED


def test_narrative_non_string_non_object_fails_closed() -> None:
    result = resolve_payload_shape("SUMMARY", 42)
    assert result.violation_code == GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED


def test_employment_role_requires_firma_and_stanowisko() -> None:
    result = resolve_payload_shape("EMPLOYMENT_ROLE", {"firma": "Co"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_employment_role_accepts_optional_fields() -> None:
    result = resolve_payload_shape(
        "EMPLOYMENT_ROLE",
        {
            "firma": "Co",
            "stanowisko": "Analyst",
            "stanowiskoAlt": "Senior Analyst",
            "okresOd": "2020",
            "okresDo": "2022",
            "legacy_source_label": "legacy",
        },
    )
    assert result.is_valid
    assert result.structured_fields is not None
    assert result.structured_fields["stanowisko"] == "Analyst"
    assert result.structured_fields["firma"] == "Co"


def test_education_degree_requires_kierunek() -> None:
    result = resolve_payload_shape("EDUCATION_DEGREE", {"uczelnia": "University"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_certification_name_requires_nazwa() -> None:
    result = resolve_payload_shape("CERTIFICATION_NAME", {})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_course_name_requires_nazwa_organizator_optional() -> None:
    result = resolve_payload_shape("COURSE_NAME", {"nazwa": "Course"})
    assert result.is_valid
    assert result.structured_fields == {"nazwa": "Course"}


def test_language_name_requires_jezyk_poziom_optional() -> None:
    result = resolve_payload_shape("LANGUAGE_NAME", {"jezyk": "English"})
    assert result.is_valid
    assert result.structured_fields == {"jezyk": "English"}


def test_structured_payload_must_be_object() -> None:
    result = resolve_payload_shape("EDUCATION_DEGREE", "not an object")
    assert result.violation_code == GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED


def test_unknown_extra_keys_are_ignored_not_rendered() -> None:
    result = resolve_payload_shape(
        "CERTIFICATION_NAME", {"nazwa": "AWS Certified", "unexpected_key": "should be ignored"}
    )
    assert result.is_valid
    assert result.structured_fields == {"nazwa": "AWS Certified"}
    assert "unexpected_key" not in result.structured_fields


def test_blank_required_structured_field_fails_closed() -> None:
    result = resolve_payload_shape("CERTIFICATION_NAME", {"nazwa": "   "})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING
