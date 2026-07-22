"""P4 policy tables: closed section mapping, ordering, priority, budgets.

All facts referenced below are synthetic; no real person, company or CV
content is used anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.cv_content_plan import ApprovalType, CvContentBudgetProfile, CvSection
from app.schemas.fact_selection import FactSelectionReasonCode
from app.schemas.truth_entity import EntityType
from app.services import cv_content_plan_policy
from app.services.cv_content_plan_policy import (
    CV_CONTENT_POLICY_VERSION,
    FACT_TYPE_PRIORITY,
    FACT_TYPE_SECTION_MAP,
    OWNER_ENTITY_TYPE_BY_FACT_TYPE,
    SECTION_ORDER,
    requires_employment_entity_grouping,
    requires_employment_scope_grouping,
    resolve_approval_type,
    resolve_section_mapping,
)


SUPPORTED_COMBINATIONS = (
    ("EMPLOYMENT_ROLE", "EMPLOYMENT_HEADER", CvSection.EXPERIENCE, "header"),
    ("COMPANY", "EMPLOYMENT_HEADER", CvSection.EXPERIENCE, "header"),
    ("ROLE", "EMPLOYMENT_HEADER", CvSection.EXPERIENCE, "header"),
    ("EMPLOYMENT_PERIOD", "EMPLOYMENT_HEADER", CvSection.EXPERIENCE, "header"),
    ("EMPLOYMENT_ACTIVITY", "EXPERIENCE", CvSection.EXPERIENCE, "responsibility"),
    ("EMPLOYMENT_RESPONSIBILITY_SCALE", "EXPERIENCE", CvSection.EXPERIENCE, "responsibility"),
    ("RESPONSIBILITY", "EXPERIENCE", CvSection.EXPERIENCE, "responsibility"),
    ("EMPLOYMENT_NUMERIC_RESULT", "EXPERIENCE", CvSection.EXPERIENCE, "achievement"),
    ("ACHIEVEMENT", "EXPERIENCE", CvSection.EXPERIENCE, "achievement"),
    ("ACHIEVEMENT", "PROJECT", CvSection.ACHIEVEMENTS, "flat"),
    ("SUMMARY", "SUMMARY", CvSection.PROFILE, "flat"),
    ("SKILL", "SKILLS", CvSection.COMPETENCIES, "flat"),
    ("TOOL", "SKILLS", CvSection.TOOLS_TECHNOLOGIES, "flat"),
    ("TECHNOLOGY", "SKILLS", CvSection.TOOLS_TECHNOLOGIES, "flat"),
    # -- Stage P4.5a: non-employment CV sections. --
    ("EDUCATION_DEGREE", "EDUCATION", CvSection.EDUCATION, "flat"),
    ("CERTIFICATION_NAME", "CERTIFICATIONS", CvSection.CERTIFICATIONS, "flat"),
    ("COURSE_NAME", "COURSES", CvSection.COURSES, "flat"),
    ("LANGUAGE_NAME", "LANGUAGES", CvSection.LANGUAGES, "flat"),
)

UNSUPPORTED_COMBINATIONS = (
    ("SKILL", "SUMMARY"),
    ("SKILL", "EXPERIENCE"),
    ("TOOL", "EXPERIENCE"),
    ("TECHNOLOGY", "EXPERIENCE"),
    ("EMPLOYMENT_ROLE", "EXPERIENCE"),
    ("COMPANY", "EXPERIENCE"),
    ("ACHIEVEMENT", "SKILLS"),
    ("EDUCATION_DEGREE", "CERTIFICATIONS"),
    ("EDUCATION_DEGREE", "ACHIEVEMENTS"),
    ("CERTIFICATION_NAME", "EDUCATION"),
    ("CERTIFICATION_NAME", "ACHIEVEMENTS"),
    # COURSE_NAME must never be routed anywhere but COURSES.
    ("COURSE_NAME", "EDUCATION"),
    ("COURSE_NAME", "CERTIFICATIONS"),
    ("COURSE_NAME", "ACHIEVEMENTS"),
    ("LANGUAGE_NAME", "EDUCATION"),
    ("LANGUAGE_NAME", "ACHIEVEMENTS"),
    ("AWARD_NAME", "ACHIEVEMENTS"),
    ("UNKNOWN_FACT_TYPE", "EXPERIENCE"),
)


def test_exactly_eighteen_supported_combinations_exist() -> None:
    assert len(FACT_TYPE_SECTION_MAP) == len(SUPPORTED_COMBINATIONS)


@pytest.mark.parametrize("fact_type,target_scope,section,bucket", SUPPORTED_COMBINATIONS)
def test_each_supported_combination_maps_explicitly(fact_type, target_scope, section, bucket) -> None:
    mapping = resolve_section_mapping(fact_type, target_scope)
    assert mapping == (section, bucket)


@pytest.mark.parametrize("fact_type,target_scope", UNSUPPORTED_COMBINATIONS)
def test_unsupported_combinations_fail_closed(fact_type, target_scope) -> None:
    assert resolve_section_mapping(fact_type, target_scope) is None


def test_skill_at_summary_is_rejected_not_routed_to_profile() -> None:
    mapping = resolve_section_mapping("SKILL", "SUMMARY")
    assert mapping is None
    profile_mapping = resolve_section_mapping("SUMMARY", "SUMMARY")
    assert profile_mapping is not None
    assert profile_mapping[0] == CvSection.PROFILE
    assert mapping != profile_mapping


def test_profile_section_is_reachable_only_via_summary_fact_type() -> None:
    profile_producers = [
        (fact_type, target_scope)
        for (fact_type, target_scope), (section, _) in FACT_TYPE_SECTION_MAP.items()
        if section == CvSection.PROFILE
    ]
    assert profile_producers == [("SUMMARY", "SUMMARY")]


def test_no_startswith_or_substring_matching_in_policy_source() -> None:
    """P4 section resolution must be exact-key dict lookup only."""

    source = Path(cv_content_plan_policy.__file__).read_text(encoding="utf-8")
    assert ".startswith(" not in source
    assert ".endswith(" not in source


def test_no_fallback_default_section_in_policy_source() -> None:
    """``resolve_section_mapping`` must be a plain dict lookup with no default
    value -- ``FACT_TYPE_SECTION_MAP.get(key)``, never
    ``FACT_TYPE_SECTION_MAP.get(key, some_default)``."""

    source = Path(cv_content_plan_policy.__file__).read_text(encoding="utf-8")
    assert "FALLBACK_SECTION" not in source
    assert "DEFAULT_SECTION" not in source
    assert "FACT_TYPE_SECTION_MAP.get((fact_type, target_scope))" in source


def test_fact_type_priority_is_explicit_and_covers_every_mapped_fact_type() -> None:
    mapped_fact_types = {fact_type for fact_type, _ in FACT_TYPE_SECTION_MAP}
    assert mapped_fact_types == set(FACT_TYPE_PRIORITY)
    assert all(isinstance(value, int) for value in FACT_TYPE_PRIORITY.values())
    assert len(set(FACT_TYPE_PRIORITY.values())) == len(FACT_TYPE_PRIORITY)


def test_fact_type_priority_source_does_not_reference_transferability_or_cpe() -> None:
    """Priority must never be derived from Transferability, CPE, or free text."""

    source = Path(cv_content_plan_policy.__file__).read_text(encoding="utf-8")
    priority_block_start = source.index("FACT_TYPE_PRIORITY: dict[str, int] = {")
    priority_block_end = source.index("}", priority_block_start)
    priority_block = source[priority_block_start:priority_block_end]
    assert "Transferability" not in priority_block
    assert "CPE" not in priority_block
    assert "evidence_weight" not in priority_block


def test_section_order_is_stable_and_covers_every_cv_section() -> None:
    assert SECTION_ORDER == (
        CvSection.PROFILE,
        CvSection.COMPETENCIES,
        CvSection.EXPERIENCE,
        CvSection.ACHIEVEMENTS,
        CvSection.EDUCATION,
        CvSection.CERTIFICATIONS,
        CvSection.COURSES,
        CvSection.LANGUAGES,
        CvSection.TOOLS_TECHNOLOGIES,
    )
    assert set(SECTION_ORDER) == set(CvSection)
    assert len(SECTION_ORDER) == len(set(SECTION_ORDER))


def test_courses_exists_after_certifications_and_before_languages() -> None:
    assert CvSection.COURSES.value == "COURSES"
    certifications_index = SECTION_ORDER.index(CvSection.CERTIFICATIONS)
    courses_index = SECTION_ORDER.index(CvSection.COURSES)
    languages_index = SECTION_ORDER.index(CvSection.LANGUAGES)
    assert certifications_index < courses_index < languages_index


def test_all_other_sections_keep_their_relative_order() -> None:
    other_sections = [section for section in SECTION_ORDER if section != CvSection.COURSES]
    assert other_sections == [
        CvSection.PROFILE,
        CvSection.COMPETENCIES,
        CvSection.EXPERIENCE,
        CvSection.ACHIEVEMENTS,
        CvSection.EDUCATION,
        CvSection.CERTIFICATIONS,
        CvSection.LANGUAGES,
        CvSection.TOOLS_TECHNOLOGIES,
    ]


def test_content_policy_version_is_its_own_constant() -> None:
    assert CV_CONTENT_POLICY_VERSION == "cv-content-policy-v2"


def test_budget_profile_has_no_default_limits() -> None:
    profile = CvContentBudgetProfile(
        budget_profile_id="empty-profile",
        budget_profile_fingerprint="a" * 64,
    )
    assert profile.section_limits == ()


def test_requires_employment_entity_grouping_only_for_header_bucket() -> None:
    assert requires_employment_entity_grouping("COMPANY", "EMPLOYMENT_HEADER") is True
    assert requires_employment_entity_grouping("EMPLOYMENT_ACTIVITY", "EXPERIENCE") is False
    assert requires_employment_entity_grouping("SKILL", "SKILLS") is False


def test_requires_employment_scope_grouping_only_for_responsibility_and_achievement() -> None:
    assert requires_employment_scope_grouping("EMPLOYMENT_ACTIVITY", "EXPERIENCE") is True
    assert requires_employment_scope_grouping("EMPLOYMENT_NUMERIC_RESULT", "EXPERIENCE") is True
    assert requires_employment_scope_grouping("ACHIEVEMENT", "EXPERIENCE") is True
    assert requires_employment_scope_grouping("ACHIEVEMENT", "PROJECT") is False


# --- Stage P4.5a: owner EntityType policy and priority coverage -------------


def test_owner_entity_type_mapping_is_exact_for_all_four_p45a_fact_types() -> None:
    assert OWNER_ENTITY_TYPE_BY_FACT_TYPE == {
        "EDUCATION_DEGREE": EntityType.EDUCATION,
        "CERTIFICATION_NAME": EntityType.CERTIFICATION,
        "COURSE_NAME": EntityType.COURSE,
        "LANGUAGE_NAME": EntityType.LANGUAGE,
    }


def test_fact_type_priority_includes_all_four_p45a_mappings() -> None:
    for fact_type in (
        "EDUCATION_DEGREE",
        "CERTIFICATION_NAME",
        "COURSE_NAME",
        "LANGUAGE_NAME",
    ):
        assert fact_type in FACT_TYPE_PRIORITY
    values = [
        FACT_TYPE_PRIORITY[ft]
        for ft in ("EDUCATION_DEGREE", "CERTIFICATION_NAME", "COURSE_NAME", "LANGUAGE_NAME")
    ]
    assert len(set(values)) == len(values)


def test_p45a_priorities_do_not_disturb_existing_fact_type_ordering() -> None:
    pre_existing_order = [
        FACT_TYPE_PRIORITY[ft]
        for ft in (
            "EMPLOYMENT_ROLE",
            "COMPANY",
            "ROLE",
            "EMPLOYMENT_PERIOD",
            "EMPLOYMENT_ACTIVITY",
            "EMPLOYMENT_RESPONSIBILITY_SCALE",
            "RESPONSIBILITY",
            "EMPLOYMENT_NUMERIC_RESULT",
            "ACHIEVEMENT",
            "SUMMARY",
            "SKILL",
            "TOOL",
            "TECHNOLOGY",
        )
    ]
    assert pre_existing_order == sorted(pre_existing_order)


def test_course_name_only_maps_to_courses_section() -> None:
    assert resolve_section_mapping("COURSE_NAME", "COURSES") == (CvSection.COURSES, "flat")
    assert resolve_section_mapping("COURSE_NAME", "EDUCATION") is None
    assert resolve_section_mapping("COURSE_NAME", "CERTIFICATIONS") is None
    assert resolve_section_mapping("COURSE_NAME", "ACHIEVEMENTS") is None
    assert requires_employment_scope_grouping("COMPANY", "EMPLOYMENT_HEADER") is False
    assert requires_employment_scope_grouping("SKILL", "SKILLS") is False


@pytest.mark.parametrize(
    "reason_codes,expected",
    [
        (
            (FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED,),
            ApprovalType.ELIGIBILITY_APPROVAL,
        ),
        (
            (FactSelectionReasonCode.FACT_REQUIRES_APPROVAL,),
            ApprovalType.ELIGIBILITY_APPROVAL,
        ),
        (
            (FactSelectionReasonCode.TRANSFERABILITY_ROLE_CONTEXT_MISSING,),
            ApprovalType.TRANSFERABILITY_APPROVAL,
        ),
        (
            (FactSelectionReasonCode.PERMISSION_MISSING_FOR_OPERATION,),
            ApprovalType.PERMISSION_APPROVAL,
        ),
        (
            (FactSelectionReasonCode.CPE_NOT_RELEVANT,),
            ApprovalType.OTHER,
        ),
        ((), ApprovalType.OTHER),
    ],
)
def test_resolve_approval_type_is_closed_and_deterministic(reason_codes, expected) -> None:
    assert resolve_approval_type(reason_codes) == expected
