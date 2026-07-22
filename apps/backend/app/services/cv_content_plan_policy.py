"""Closed, versioned P4 policy tables: section mapping, ordering, approval typing.

Every table here is a plain, explicit dict or tuple -- no ``startswith``, no
substring matching, no text classification, and no fallback to a default
section. A ``(fact_type, target_scope)`` combination outside
``FACT_TYPE_SECTION_MAP`` is unsupported by definition, never approximated.
"""

from __future__ import annotations

from app.schemas.cv_content_plan import ApprovalType, CvSection
from app.schemas.fact_selection import FactSelectionReasonCode
from app.schemas.truth_entity import EntityType


CV_CONTENT_POLICY_VERSION = "cv-content-policy-v2"

#: Plan-wide section ordering. ``CvContentPlan.sections`` always contains
#: exactly one entry per member, in this order. Versioned by
#: ``CV_CONTENT_POLICY_VERSION``: reordering, adding or removing a member
#: requires a version bump. ``COURSES`` was added in Stage P4.5a, between
#: ``CERTIFICATIONS`` and ``LANGUAGES``.
SECTION_ORDER: tuple[CvSection, ...] = (
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

#: The bucket a fact_type/target_scope pair is grouped into once it is
#: mapped into a ``CvSection``. ``"header"``/``"responsibility"``/
#: ``"achievement"`` are the three ``CvExperienceEntryPlan`` buckets;
#: ``"flat"`` is a non-EXPERIENCE section's ``planned_fact_uses`` list.
_FactTypeSectionMapping = tuple[CvSection, str]

#: The sole, closed source of truth mapping an existing P3
#: ``(fact_type, requested_target_scope)`` pair onto a P4 section slot. Any
#: combination absent from this dict is unsupported and fails closed to
#: ``SECTION_NOT_ALLOWED_FOR_FACT_TYPE`` -- there is no default bucket.
FACT_TYPE_SECTION_MAP: dict[tuple[str, str], _FactTypeSectionMapping] = {
    ("EMPLOYMENT_ROLE", "EMPLOYMENT_HEADER"): (CvSection.EXPERIENCE, "header"),
    ("COMPANY", "EMPLOYMENT_HEADER"): (CvSection.EXPERIENCE, "header"),
    ("ROLE", "EMPLOYMENT_HEADER"): (CvSection.EXPERIENCE, "header"),
    ("EMPLOYMENT_PERIOD", "EMPLOYMENT_HEADER"): (CvSection.EXPERIENCE, "header"),
    ("EMPLOYMENT_ACTIVITY", "EXPERIENCE"): (CvSection.EXPERIENCE, "responsibility"),
    ("EMPLOYMENT_RESPONSIBILITY_SCALE", "EXPERIENCE"): (
        CvSection.EXPERIENCE,
        "responsibility",
    ),
    ("RESPONSIBILITY", "EXPERIENCE"): (CvSection.EXPERIENCE, "responsibility"),
    ("EMPLOYMENT_NUMERIC_RESULT", "EXPERIENCE"): (CvSection.EXPERIENCE, "achievement"),
    ("ACHIEVEMENT", "EXPERIENCE"): (CvSection.EXPERIENCE, "achievement"),
    ("ACHIEVEMENT", "PROJECT"): (CvSection.ACHIEVEMENTS, "flat"),
    ("SUMMARY", "SUMMARY"): (CvSection.PROFILE, "flat"),
    ("SKILL", "SKILLS"): (CvSection.COMPETENCIES, "flat"),
    ("TOOL", "SKILLS"): (CvSection.TOOLS_TECHNOLOGIES, "flat"),
    ("TECHNOLOGY", "SKILLS"): (CvSection.TOOLS_TECHNOLOGIES, "flat"),
    # -- Stage P4.5a: non-employment CV sections. --
    ("EDUCATION_DEGREE", "EDUCATION"): (CvSection.EDUCATION, "flat"),
    ("CERTIFICATION_NAME", "CERTIFICATIONS"): (CvSection.CERTIFICATIONS, "flat"),
    ("COURSE_NAME", "COURSES"): (CvSection.COURSES, "flat"),
    ("LANGUAGE_NAME", "LANGUAGES"): (CvSection.LANGUAGES, "flat"),
}

#: fact_type values whose header bucket placement is keyed by
#: ``decision.entity_id`` (the employment entity itself), never by
#: ``decision.employment_scope_entity_id``.
HEADER_FACT_TYPES: frozenset[str] = frozenset(
    {"EMPLOYMENT_ROLE", "COMPANY", "ROLE", "EMPLOYMENT_PERIOD"}
)

#: Explicit, versioned priority used only to order facts *within* a single
#: bucket (header/responsibility/achievement/flat). Never derived from
#: ``Transferability``, advisory flags, reason codes, free text or an LLM.
FACT_TYPE_PRIORITY: dict[str, int] = {
    "EMPLOYMENT_ROLE": 10,
    "COMPANY": 20,
    "ROLE": 30,
    "EMPLOYMENT_PERIOD": 40,
    "EMPLOYMENT_ACTIVITY": 50,
    "EMPLOYMENT_RESPONSIBILITY_SCALE": 60,
    "RESPONSIBILITY": 70,
    "EMPLOYMENT_NUMERIC_RESULT": 80,
    "ACHIEVEMENT": 90,
    "SUMMARY": 100,
    "SKILL": 110,
    "TOOL": 120,
    "TECHNOLOGY": 130,
    # -- Stage P4.5a: non-employment CV sections. --
    "EDUCATION_DEGREE": 140,
    "CERTIFICATION_NAME": 150,
    "COURSE_NAME": 160,
    "LANGUAGE_NAME": 170,
}

#: Stage P4.5a: closed, versioned mapping from the four non-employment
#: fact_types to the ``EntityType`` their owner (``decision.entity_id``) must
#: resolve to in the caller-supplied ``entity_types`` snapshot. This is the
#: single shared source of truth for the P4 builder and validator -- never
#: inferred from ``fact_type`` outside this dict, and never re-declared as a
#: local literal in either module.
OWNER_ENTITY_TYPE_BY_FACT_TYPE: dict[str, EntityType] = {
    "EDUCATION_DEGREE": EntityType.EDUCATION,
    "CERTIFICATION_NAME": EntityType.CERTIFICATION,
    "COURSE_NAME": EntityType.COURSE,
    "LANGUAGE_NAME": EntityType.LANGUAGE,
}


def resolve_section_mapping(fact_type: str, target_scope: str) -> _FactTypeSectionMapping | None:
    """Look up the closed ``(fact_type, target_scope)`` -> section mapping.

    Returns ``None`` when the combination is unsupported. Never falls back
    to ``EXPERIENCE``/``COMPETENCIES`` or any other default.
    """

    return FACT_TYPE_SECTION_MAP.get((fact_type, target_scope))


def requires_employment_scope_grouping(fact_type: str, target_scope: str) -> bool:
    """True when this fact/scope pair groups by ``employment_scope_entity_id``."""

    mapping = resolve_section_mapping(fact_type, target_scope)
    return mapping is not None and mapping[1] in ("responsibility", "achievement")


def requires_employment_entity_grouping(fact_type: str, target_scope: str) -> bool:
    """True when this fact/scope pair groups by ``entity_id`` (header bucket)."""

    mapping = resolve_section_mapping(fact_type, target_scope)
    return mapping is not None and mapping[1] == "header"


#: Closed reason-code sets used only to classify *why* a pending approval
#: exists -- mirrors the exact P3 gate pipeline order (eligibility, then
#: transferability, then permission). Never text or similarity matched.
_ELIGIBILITY_REASON_CODES: frozenset[FactSelectionReasonCode] = frozenset(
    {
        FactSelectionReasonCode.FACT_STATUS_REVIEW_REQUIRED,
        FactSelectionReasonCode.FACT_REQUIRES_APPROVAL,
    }
)
_TRANSFERABILITY_REASON_CODES: frozenset[FactSelectionReasonCode] = frozenset(
    {
        FactSelectionReasonCode.TRANSFERABILITY_ENTITY_SCOPE_UNVERIFIED,
        FactSelectionReasonCode.TRANSFERABILITY_ROLE_CONTEXT_MISSING,
        FactSelectionReasonCode.TRANSFERABILITY_INDUSTRY_CONTEXT_MISSING,
    }
)
_PERMISSION_REASON_CODES: frozenset[FactSelectionReasonCode] = frozenset(
    {
        FactSelectionReasonCode.PERMISSION_SCOPE_MISMATCH,
        FactSelectionReasonCode.PERMISSION_MISSING_FOR_OPERATION,
        FactSelectionReasonCode.PERMISSION_REVIEW_REQUIRED,
        FactSelectionReasonCode.PERMISSION_NOT_YET_VALID,
        FactSelectionReasonCode.PERMISSION_EXPIRED,
        FactSelectionReasonCode.PERMISSION_CONSTRAINTS_MISMATCH,
    }
)


def resolve_approval_type(
    reason_codes: tuple[FactSelectionReasonCode, ...],
) -> ApprovalType:
    """Classify a P3 ``APPROVAL_REQUIRED`` decision's reason codes.

    Fails closed to ``ApprovalType.OTHER`` when no known gate's reason
    codes are present -- never guesses.
    """

    codes = set(reason_codes)
    if codes & _ELIGIBILITY_REASON_CODES:
        return ApprovalType.ELIGIBILITY_APPROVAL
    if codes & _TRANSFERABILITY_REASON_CODES:
        return ApprovalType.TRANSFERABILITY_APPROVAL
    if codes & _PERMISSION_REASON_CODES:
        return ApprovalType.PERMISSION_APPROVAL
    return ApprovalType.OTHER
