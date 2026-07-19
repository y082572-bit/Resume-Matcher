"""Unit coverage for the deterministic Stage 10A planner."""

from datetime import datetime, timezone
import inspect
import re
from typing import get_args

import pytest
from pydantic import ValidationError

from app.services.career_positioning_report import build_career_positioning_report
from app.services.cv_transformation_plan import build_cv_transformation_plan
from app.services.career_positioning import PositioningStrategy, RoleLevel
from app.schemas.cv_transformation_plan import (
    CVTransformationPlanItem,
    PROHIBITED_CLAIM_CODES,
    PositioningStrategyName,
    RoleLevelName,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def truth_library(entries=None, **categories):
    return {
        "meta": {"wersja": "test"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": entries
                if entries is not None
                else [
                    {
                        "id": "private-employment-id",
                        "firma": "Example Company",
                        "stanowisko": "Engineering Manager",
                        "okresOd": "2021-01",
                        "okresDo": "2025-12",
                        "aktywnosci": [
                            "zarządzanie zespołem 8 osób",
                            "odpowiedzialność za budżet",
                            "współpraca z Board",
                            "zarządzanie menedżerami",
                            "strategia organizacyjna",
                            "Python i SQL",
                        ],
                        "wynikLiczbowy": "Wzrost wyniku o 20%",
                        "skalaOdpowiedzialnosci": "3 jednostki organizacyjne",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    },
                    {
                        "id": "private-specialist-id",
                        "firma": "Example Lab",
                        "stanowisko": "Senior Python Specialist",
                        "okresOd": "2018-01",
                        "okresDo": "2020-12",
                        "aktywnosci": ["Python", "SQL", "analiza danych"],
                        "status": APPROVED,
                        "uzywacWCV": True,
                    },
                ]
            },
            "kompetencje": {"wpisy": categories.get("kompetencje", [])},
            "narzedzia": {"wpisy": categories.get("narzedzia", [])},
            "jezyki": {"wpisy": categories.get("jezyki", [])},
            "certyfikaty": {"wpisy": categories.get("certyfikaty", [])},
            "osiagnieciaLiczbowe": {"wpisy": categories.get("osiagniecia", [])},
            "tranferowalneKompetencje": {
                "mapowania": {"analiza danych": "business intelligence"}
            },
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": ["DO_AKCEPTACJI"],
                "statusyZablokowane": ["ZABLOKOWANE"],
            },
            "branze": {"zatwierdzoneBranze": []},
        },
    }


def resume_data(order=False):
    experiences = [
        {
            "id": 1,
            "title": "Engineering Manager",
            "company": "Example Company",
            "years": "2021-2025",
            "description": ["Wzrost wyniku o 20%", "Led Python delivery"],
        },
        {
            "id": 2,
            "title": "Senior Specialist",
            "company": "Example Lab",
            "years": "2018-2020",
            "description": ["Built analytics workflows"],
        },
    ]
    tools = ["SQL", "Python", "Docker"]
    if order:
        experiences.reverse()
        tools.reverse()
    return {
        "resume_id": "resume-1",
        "processing_status": "ready",
        "processed_data": {
            "summary": "Engineering leader and Python specialist",
            "workExperience": experiences,
            "additional": {
                "technicalSkills": tools,
                "languages": ["English C1"],
                "certificationsTraining": ["Example Certificate"],
                "awards": [],
            },
        },
    }


def make_plan(strategy="BALANCED", truth=None, resume=None, job_content=None, now=NOW):
    library = truth or truth_library()
    job = {
        "job_id": "job-1",
        "resume_id": "resume-1",
        "role": "Python Engineering Manager",
        "content": job_content
        or "Python Engineering Manager\nWymagane Python SQL zarządzanie zespołem budżet",
    }
    report = build_career_positioning_report(job, library, now)
    report.positioning.positioning_strategy = strategy
    if strategy in {"REVIEW_REQUIRED", "CONTROLLED_ELEVATION", "EXECUTIVE_ENTERPRISE"}:
        report.positioning.requires_human_review = True
    return build_cv_transformation_plan(
        resume_id="resume-1",
        job_id="job-1",
        resume=resume or resume_data(),
        job=job,
        truth_library=library,
        career_report=report,
        now=now,
    )


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("REVIEW_REQUIRED", "HUMAN_REVIEW"),
        ("JUNIOR_ENTRY", "KEEP"),
        ("SPECIALIST_DELIVERY", "EMPHASIZE"),
        ("EXPERT_AUTHORITY", "EMPHASIZE"),
        ("MANAGER_LEADERSHIP", "REPHRASE"),
        ("DIRECTOR_SCALE", "REPHRASE"),
        ("EXECUTIVE_ENTERPRISE", "HUMAN_REVIEW"),
        ("CONTROLLED_FLATTENING", "REPHRASE"),
        ("CONTROLLED_ELEVATION", "HUMAN_REVIEW"),
        ("BALANCED", "KEEP"),
    ],
)
def test_all_positioning_strategies_are_mapped(strategy, expected):
    assert make_plan(strategy).summary_plan[0].action == expected


@pytest.mark.parametrize(
    ("code", "evidence_text"),
    [
        ("PNL_WITHOUT_EVIDENCE", "odpowiedzialność P&L"),
        ("BUDGET_WITHOUT_EVIDENCE", "odpowiedzialność za budżet"),
        ("BOARD_WITHOUT_EVIDENCE", "współpraca z Board"),
        ("PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE", "zarządzanie zespołem"),
        ("MANAGING_MANAGERS_WITHOUT_EVIDENCE", "zarządzanie menedżerami"),
        ("TEAM_SIZE_WITHOUT_EVIDENCE", "zespół 8 osób"),
        ("TECHNICAL_SKILL_WITHOUT_EVIDENCE", "Python"),
        ("LANGUAGE_LEVEL_WITHOUT_EVIDENCE", "English C1"),
        ("CERTIFICATION_WITHOUT_EVIDENCE", "certyfikat zawodowy"),
        ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE", "Wzrost o 20%"),
    ],
)
def test_approved_evidence_never_removes_universal_guardrail(code, evidence_text):
    entry = {
        "id": "evidence-id",
        "firma": "Example Company",
        "stanowisko": "Engineering Manager",
        "okresOd": "2021-01",
        "okresDo": "2025-12",
        "aktywnosci": [evidence_text],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    plan = make_plan(truth=truth_library(entries=[entry]))
    assert code in {item.code for item in plan.prohibited_claims}
    assert tuple(item.code for item in plan.prohibited_claims) == PROHIBITED_CLAIM_CODES


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("NIEJASNE", {}),
        ("USUNIĘTE", {}),
        ("PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA", {}),
        ("DO_AKCEPTACJI", {}),
        ("ZABLOKOWANE", {}),
        (APPROVED, {"uzywacWCV": False}),
        (APPROVED, {"wymagaAkceptacji": True}),
    ],
)
def test_untrusted_truth_entries_never_affect_plan(status, extra):
    entry = {
        "id": "untrusted-id",
        "firma": "Example",
        "stanowisko": "Specialist",
        "aktywnosci": ["odpowiedzialność P&L"],
        "status": status,
        "uzywacWCV": True,
        **extra,
    }
    library = truth_library(entries=[entry])
    library["kategorie"]["reguly"]["statusyDoAutomatycznegoCV"].append(status)
    plan = make_plan(truth=library)
    assert "PNL_WITHOUT_EVIDENCE" in {item.code for item in plan.prohibited_claims}
    assert plan.source_summary.approved_truth_items_count == 0


def test_empty_resume_produces_empty_content_sections():
    empty_resume = {"processing_status": "ready", "processed_data": {}}
    plan = make_plan(resume=empty_resume)
    assert plan.summary_plan == []
    assert plan.experience_plan == []
    assert plan.achievements_plan == []
    assert plan.tools_plan == []


def test_expert_scenario_emphasizes_summary():
    assert make_plan("EXPERT_AUTHORITY").summary_plan[0].action == "EMPHASIZE"


def test_specialist_scenario_emphasizes_delivery():
    assert make_plan("SPECIALIST_DELIVERY").summary_plan[0].reason_code == "SPECIALIST_DELIVERY_FOCUS"


def test_manager_evidence_creates_permission_without_removing_guardrail():
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"].append(
        "zarządzanie zespołem 8 osób"
    )
    plan = make_plan("MANAGER_LEADERSHIP", resume=source)
    assert "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE" in {
        item.code for item in plan.prohibited_claims
    }
    assert {
        "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE",
        "TEAM_SIZE_WITHOUT_EVIDENCE",
    } <= {permission.claim_code for permission in plan.evidence_permissions}


def test_manager_without_people_evidence_is_prohibited():
    plan = make_plan("MANAGER_LEADERSHIP", truth=truth_library(entries=[]))
    assert "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE" in {item.code for item in plan.prohibited_claims}
    assert "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE" not in {
        permission.claim_code for permission in plan.evidence_permissions
    }


def test_director_signals_are_evaluated_independently():
    only_budget = {
        "id": "budget-id",
        "firma": "Example Company",
        "stanowisko": "Engineering Manager",
        "okresOd": "2021-01",
        "okresDo": "2025-12",
        "aktywnosci": ["odpowiedzialność za budżet"],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"] = [
        "odpowiedzialność za budżet"
    ]
    plan = make_plan(
        "DIRECTOR_SCALE", truth=truth_library(entries=[only_budget]), resume=source
    )
    assert tuple(item.code for item in plan.prohibited_claims) == PROHIBITED_CLAIM_CODES
    permission_codes = {item.claim_code for item in plan.evidence_permissions}
    assert "BUDGET_WITHOUT_EVIDENCE" in permission_codes
    assert {
        "PNL_WITHOUT_EVIDENCE",
        "BOARD_WITHOUT_EVIDENCE",
        "MANAGING_MANAGERS_WITHOUT_EVIDENCE",
    }.isdisjoint(permission_codes)


def test_organizational_scale_is_approved_evidence():
    assert make_plan().source_summary.approved_truth_items_count == 2


def test_direct_and_transferable_competencies_have_distinct_reasons():
    direct = {item.reason_code for item in make_plan().competencies_plan}
    transferable = {
        item.reason_code
        for item in make_plan(job_content="Partner role\nWymagane business intelligence").competencies_plan
    }
    assert "DIRECT_COMPETENCY" in direct
    assert "TRANSFERABLE_COMPETENCY" in transferable


def test_missing_critical_competency_requires_review():
    plan = make_plan(job_content="Manager\nWymagane Excel i Tableau")
    missing = [item for item in plan.competencies_plan if item.reason_code == "MISSING_CRITICAL_COMPETENCY"]
    assert missing and all(item.action == "HUMAN_REVIEW" for item in missing)


def test_tools_are_sorted_and_relevant_tools_emphasized():
    plan = make_plan()
    assert plan.tools_plan == sorted(plan.tools_plan, key=lambda item: item.source_reference)
    assert any(item.action == "EMPHASIZE" for item in plan.tools_plan)


def test_approved_numeric_result_is_preserved_and_emphasized():
    result = make_plan().achievements_plan
    assert result and result[0].action == "EMPHASIZE"


def test_unapproved_numeric_result_requires_human_review():
    plan = make_plan(truth=truth_library(entries=[]))
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"
    assert plan.requires_human_review is True


def test_controlled_flattening_deemphasizes_director_context_but_keeps_achievement():
    source = resume_data()
    source["processed_data"]["workExperience"][0]["title"] = "Director of Engineering"
    plan = make_plan("CONTROLLED_FLATTENING", resume=source)
    assert any(item.action == "DEEMPHASIZE" for item in plan.experience_plan)
    assert all(item.action != "OMIT" for item in plan.achievements_plan)


def test_reordering_inputs_does_not_change_semantic_plan():
    first = make_plan().model_dump(exclude={"generated_at"})
    second = make_plan(resume=resume_data(order=True), truth={
        **truth_library(),
        "kategorie": {
            **truth_library()["kategorie"],
            "doswiadczenieZawodowe": {"wpisy": list(reversed(truth_library()["kategorie"]["doswiadczenieZawodowe"]["wpisy"]))},
        },
    }).model_dump(exclude={"generated_at"})
    assert first == second


def test_repeated_calls_are_identical_except_generated_at():
    one = make_plan(now=NOW).model_dump(exclude={"generated_at"})
    two = make_plan(now=NOW.replace(hour=13)).model_dump(exclude={"generated_at"})
    assert one == two


def test_overqualification_uses_controlled_flattening_without_deleting_results():
    plan = make_plan("CONTROLLED_FLATTENING")
    assert plan.summary_plan[0].action == "REPHRASE"
    assert all(item.action != "OMIT" for item in plan.achievements_plan)


def test_underqualification_uses_controlled_elevation_with_review():
    plan = make_plan("CONTROLLED_ELEVATION")
    assert plan.summary_plan[0].action == "HUMAN_REVIEW"
    assert plan.requires_human_review is True


def test_exact_duplicate_experience_is_the_only_safe_omit_case():
    source = resume_data()
    source["processed_data"]["workExperience"].append(
        dict(source["processed_data"]["workExperience"][0])
    )
    plan = make_plan(resume=source)
    omitted = [item for item in plan.experience_plan if item.action == "OMIT"]
    assert len(omitted) == 1
    assert omitted[0].reason_code == "DUPLICATE_RESUME_ITEM"


def test_public_dto_contains_no_private_truth_identifiers():
    payload = make_plan().model_dump_json()
    assert "private-employment-id" not in payload
    assert "truth_item_id" not in payload
    assert "metadata_json" not in payload
    assert "lifecycle_token" not in payload


def _plan_for_scoped_fact(claim, fact_type="activity"):
    entry = {
        "id": "scoped-evidence-id",
        "firma": "Example Company",
        "stanowisko": "Engineering Manager",
        "okresOd": "2021-01",
        "okresDo": "2025-12",
        "aktywnosci": [],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    if fact_type == "numeric_result":
        entry["wynikLiczbowy"] = claim
    elif fact_type == "responsibility_scale":
        entry["skalaOdpowiedzialnosci"] = claim
    else:
        entry["aktywnosci"] = [claim]
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"] = [claim]
    return make_plan(truth=truth_library(entries=[entry]), resume=source)


@pytest.mark.parametrize(
    ("claim", "fact_type", "expected_permission", "forbidden_permission"),
    [
        ("zarządzanie zespołem", "activity", "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE", "BOARD_WITHOUT_EVIDENCE"),
        ("zespół 8 osób", "responsibility_scale", "TEAM_SIZE_WITHOUT_EVIDENCE", "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE"),
        ("odpowiedzialność za budżet", "activity", "BUDGET_WITHOUT_EVIDENCE", "PNL_WITHOUT_EVIDENCE"),
        ("strategia organizacyjna", "activity", "STRATEGY_RESPONSIBILITY_EVIDENCE", "ORGANIZATIONAL_SCALE_EVIDENCE"),
        ("zarządzanie menedżerami", "activity", "MANAGING_MANAGERS_WITHOUT_EVIDENCE", "BOARD_WITHOUT_EVIDENCE"),
        ("zarządzanie operacyjne", "activity", None, "BOARD_WITHOUT_EVIDENCE"),
        ("współpraca z Board", "activity", "BOARD_WITHOUT_EVIDENCE", "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE"),
        ("odpowiedzialność P&L", "activity", "PNL_WITHOUT_EVIDENCE", "BUDGET_WITHOUT_EVIDENCE"),
        ("strategia sprzedaży", "activity", "STRATEGY_RESPONSIBILITY_EVIDENCE", "BUDGET_WITHOUT_EVIDENCE"),
        ("3 jednostki organizacyjne", "responsibility_scale", "ORGANIZATIONAL_SCALE_EVIDENCE", "STRATEGY_RESPONSIBILITY_EVIDENCE"),
        ("zespół 12 osób", "responsibility_scale", "TEAM_SIZE_WITHOUT_EVIDENCE", "BOARD_WITHOUT_EVIDENCE"),
        ("relacje z zarządem", "activity", "BOARD_WITHOUT_EVIDENCE", "MANAGING_MANAGERS_WITHOUT_EVIDENCE"),
        ("people management", "activity", "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE", "TEAM_SIZE_WITHOUT_EVIDENCE"),
    ],
)
def test_cross_category_facts_never_authorize_another_signal(
    claim, fact_type, expected_permission, forbidden_permission
):
    codes = {
        permission.claim_code
        for permission in _plan_for_scoped_fact(claim, fact_type).evidence_permissions
    }
    if expected_permission:
        assert expected_permission in codes
    assert forbidden_permission not in codes


def test_one_technical_skill_never_authorizes_another_skill():
    entry = {
        "id": "python-id",
        "firma": "Example Company",
        "stanowisko": "Engineering Manager",
        "aktywnosci": ["Python"],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    source = resume_data()
    source["processed_data"]["additional"]["technicalSkills"] = ["SQL"]
    plan = make_plan(truth=truth_library(entries=[entry]), resume=source)
    assert not any(
        permission.claim_code == "TECHNICAL_SKILL_WITHOUT_EVIDENCE"
        for permission in plan.evidence_permissions
    )


def test_english_speaking_market_is_not_language_level_evidence():
    plan = _plan_for_scoped_fact("English-speaking market")
    assert "LANGUAGE_LEVEL_WITHOUT_EVIDENCE" not in {
        permission.claim_code for permission in plan.evidence_permissions
    }


def test_arbitrary_number_is_not_quantified_result_evidence():
    plan = _plan_for_scoped_fact("Obsłużono 20 klientów", "activity")
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"
    assert "QUANTIFIED_RESULT_WITHOUT_EVIDENCE" not in {
        permission.claim_code for permission in plan.evidence_permissions
    }


def test_budget_word_in_job_title_is_not_budget_responsibility_evidence():
    entry = {
        "id": "budget-title-id",
        "firma": "Example Company",
        "stanowisko": "Budget Manager",
        "aktywnosci": [],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    source = resume_data()
    source["processed_data"]["workExperience"][0]["title"] = "Budget Manager"
    source["processed_data"]["workExperience"][0]["description"] = []
    plan = make_plan(truth=truth_library(entries=[entry]), resume=source)
    assert "BUDGET_WITHOUT_EVIDENCE" not in {
        permission.claim_code for permission in plan.evidence_permissions
    }


def test_budget_evidence_in_other_company_does_not_authorize_experience():
    entry = {
        "id": "other-company-budget-id",
        "firma": "Other Company",
        "stanowisko": "Engineering Manager",
        "okresOd": "2021-01",
        "okresDo": "2025-12",
        "aktywnosci": ["odpowiedzialność za budżet"],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"] = [
        "odpowiedzialność za budżet"
    ]
    plan = make_plan(truth=truth_library(entries=[entry]), resume=source)
    assert "BUDGET_WITHOUT_EVIDENCE" not in {
        permission.claim_code for permission in plan.evidence_permissions
    }


def _numeric_scope_plan(
    *,
    resume_role="Engineering Manager",
    resume_years="2021-2025",
    truth_role="Engineering Manager",
    truth_start="2021-01",
    truth_end="2025-12",
):
    entry = {
        "id": "numeric-scope-id",
        "firma": "Example Company",
        "wynikLiczbowy": "Wzrost wyniku o 20%",
        "status": APPROVED,
        "uzywacWCV": True,
    }
    if truth_role is not None:
        entry["stanowisko"] = truth_role
    if truth_start is not None:
        entry["okresOd"] = truth_start
    if truth_end is not None:
        entry["okresDo"] = truth_end

    source = resume_data()
    experience = source["processed_data"]["workExperience"][0]
    experience["description"] = ["Wzrost wyniku o 20%"]
    if resume_role is None:
        experience.pop("title", None)
    else:
        experience["title"] = resume_role
    if resume_years is None:
        experience.pop("years", None)
    else:
        experience["years"] = resume_years
    return make_plan(truth=truth_library(entries=[entry]), resume=source)


def _has_quantified_permission(plan, source_reference):
    return any(
        permission.claim_code == "QUANTIFIED_RESULT_WITHOUT_EVIDENCE"
        and permission.source_reference == source_reference
        for permission in plan.evidence_permissions
    )


def test_exact_result_same_company_without_truth_role_or_period_requires_review():
    plan = _numeric_scope_plan(truth_role=None, truth_start=None, truth_end=None)
    item = plan.achievements_plan[0]
    assert item.action == "HUMAN_REVIEW"
    assert not _has_quantified_permission(plan, item.source_reference)


def test_exact_result_same_company_without_resume_role_or_period_requires_review():
    plan = _numeric_scope_plan(resume_role=None, resume_years=None)
    item = plan.achievements_plan[0]
    assert item.action == "HUMAN_REVIEW"
    assert not _has_quantified_permission(plan, item.source_reference)


def test_different_roles_without_comparable_period_do_not_create_permission():
    plan = _numeric_scope_plan(
        resume_role="Engineering Manager",
        resume_years=None,
        truth_role="Finance Manager",
        truth_start=None,
        truth_end=None,
    )
    item = plan.achievements_plan[0]
    assert item.action == "HUMAN_REVIEW"
    assert not _has_quantified_permission(plan, item.source_reference)


def test_matching_roles_without_periods_create_permission():
    plan = _numeric_scope_plan(resume_years=None, truth_start=None, truth_end=None)
    item = plan.achievements_plan[0]
    assert item.action == "EMPHASIZE"
    assert _has_quantified_permission(plan, item.source_reference)


def test_matching_full_periods_without_roles_create_permission():
    plan = _numeric_scope_plan(resume_role=None, truth_role=None)
    item = plan.achievements_plan[0]
    assert item.action == "EMPHASIZE"
    assert _has_quantified_permission(plan, item.source_reference)


def test_different_periods_without_roles_do_not_create_permission():
    plan = _numeric_scope_plan(
        resume_role=None,
        resume_years="2021-2025",
        truth_role=None,
        truth_start="2020-01",
        truth_end="2024-12",
    )
    item = plan.achievements_plan[0]
    assert item.action == "HUMAN_REVIEW"
    assert not _has_quantified_permission(plan, item.source_reference)


def test_budget_evidence_same_company_without_role_or_period_has_no_permission():
    entry = {
        "id": "unscoped-budget-id",
        "firma": "Example Company",
        "aktywnosci": ["odpowiedzialność za budżet"],
        "status": APPROVED,
        "uzywacWCV": True,
    }
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"] = [
        "odpowiedzialność za budżet"
    ]
    plan = make_plan(truth=truth_library(entries=[entry]), resume=source)
    item = next(
        candidate
        for candidate in plan.experience_plan
        if candidate.display_label == "Engineering Manager — Example Company"
    )
    assert item.action == "EMPHASIZE"
    assert not any(
        permission.claim_code == "BUDGET_WITHOUT_EVIDENCE"
        and permission.source_reference == item.source_reference
        for permission in plan.evidence_permissions
    )


def test_numeric_result_same_company_without_role_or_period_requires_review():
    plan = _numeric_scope_plan(truth_role=None, truth_start=None, truth_end=None)
    item = plan.achievements_plan[0]
    assert item.action == "HUMAN_REVIEW"
    assert not _has_quantified_permission(plan, item.source_reference)


def test_exact_achievement_same_company_and_period_has_permission():
    plan = make_plan()
    assert plan.achievements_plan[0].action == "EMPHASIZE"
    assert any(
        permission.claim_code == "QUANTIFIED_RESULT_WITHOUT_EVIDENCE"
        and permission.source_reference == plan.achievements_plan[0].source_reference
        for permission in plan.evidence_permissions
    )


def test_same_achievement_in_different_company_requires_review():
    library = truth_library()
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["firma"] = "Other Company"
    plan = make_plan(truth=library)
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"


def test_achievement_partial_substring_requires_review():
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"][0] = (
        "Wzrost wyniku o 20% w dodatkowym kanale"
    )
    plan = make_plan(resume=source)
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"


def test_achievement_with_different_percentage_requires_review():
    source = resume_data()
    source["processed_data"]["workExperience"][0]["description"][0] = (
        "Wzrost wyniku o 21%"
    )
    plan = make_plan(resume=source)
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"


def test_identical_untrusted_achievement_requires_review():
    entry = {
        "id": "untrusted-result-id",
        "firma": "Example Company",
        "stanowisko": "Engineering Manager",
        "okresOd": "2021-01",
        "okresDo": "2025-12",
        "wynikLiczbowy": "Wzrost wyniku o 20%",
        "status": "NIEJASNE",
        "uzywacWCV": True,
    }
    library = truth_library(entries=[entry])
    library["kategorie"]["reguly"]["statusyDoAutomatycznegoCV"].append("NIEJASNE")
    plan = make_plan(truth=library)
    assert plan.achievements_plan[0].action == "HUMAN_REVIEW"


def test_source_references_are_structural_ordinals_not_content_hashes():
    plan = make_plan()
    references = [
        item.source_reference
        for items in (
            plan.summary_plan,
            plan.experience_plan,
            plan.competencies_plan,
            plan.achievements_plan,
            plan.tools_plan,
        )
        for item in items
    ]
    assert all(
        re.fullmatch(
            r"(?:resume|positioning):(?:summary|experience|competencies|achievements|tools):\d{4}",
            reference,
        )
        for reference in references
    )
    assert len(references) == len(set(references))


def test_display_labels_are_short_and_come_from_resume_or_public_report():
    plan = make_plan()
    items = (
        plan.summary_plan
        + plan.experience_plan
        + plan.competencies_plan
        + plan.achievements_plan
        + plan.tools_plan
    )
    assert all(item.display_label.strip() and len(item.display_label) <= 120 for item in items)
    assert "Engineering Manager — Example Company" in {
        item.display_label for item in plan.experience_plan
    }
    assert "Wzrost wyniku o 20%" in {
        item.display_label for item in plan.achievements_plan
    }


def test_reordered_input_keeps_structural_references_and_labels():
    first = make_plan()
    second = make_plan(resume=resume_data(order=True))
    first_pairs = sorted(
        (item.source_reference, item.display_label)
        for items in (
            first.summary_plan,
            first.experience_plan,
            first.competencies_plan,
            first.achievements_plan,
            first.tools_plan,
        )
        for item in items
    )
    second_pairs = sorted(
        (item.source_reference, item.display_label)
        for items in (
            second.summary_plan,
            second.experience_plan,
            second.competencies_plan,
            second.achievements_plan,
            second.tools_plan,
        )
        for item in items
    )
    assert first_pairs == second_pairs


def test_evidence_permissions_allow_only_existing_operations():
    operations = {permission.allowed_operation for permission in make_plan().evidence_permissions}
    assert operations <= {
        "KEEP_EXISTING",
        "EMPHASIZE_EXISTING",
        "REPHRASE_EXISTING",
    }
    assert "GENERATE_NEW" not in operations


def test_evidence_permissions_reference_existing_plan_items():
    plan = make_plan()
    item_references = {
        item.source_reference
        for items in (
            plan.summary_plan,
            plan.experience_plan,
            plan.competencies_plan,
            plan.achievements_plan,
            plan.tools_plan,
        )
        for item in items
    }
    assert plan.evidence_permissions
    assert all(
        permission.source_reference in item_references
        for permission in plan.evidence_permissions
    )


def test_python_permission_is_specific_to_python_plan_item():
    plan = make_plan()
    labels_by_reference = {
        item.source_reference: item.display_label for item in plan.tools_plan
    }
    technical_labels = {
        labels_by_reference[permission.source_reference]
        for permission in plan.evidence_permissions
        if permission.claim_code == "TECHNICAL_SKILL_WITHOUT_EVIDENCE"
    }
    assert technical_labels == {"Python", "SQL"}
    assert "Docker" not in technical_labels


def test_transformation_schema_enums_follow_canonical_engine_enums():
    assert set(get_args(RoleLevelName)) == {level.name for level in RoleLevel}
    assert set(get_args(PositioningStrategyName)) == {
        strategy.value for strategy in PositioningStrategy
    }


def test_schema_rejects_human_review_action_with_false_item_flag():
    with pytest.raises(
        ValidationError,
        match="HUMAN_REVIEW action requires human_review_required=true",
    ):
        CVTransformationPlanItem(
            action="HUMAN_REVIEW",
            section="SUMMARY",
            source_reference="resume:summary:0001",
            display_label="Professional summary",
            reason_code="REVIEW_REQUIRED",
            reason="Review required.",
            evidence_strength="WEAK",
            human_review_required=False,
        )


def test_planner_source_has_no_llm_or_text_generation_dependency():
    source = inspect.getsource(__import__("app.services.cv_transformation_plan", fromlist=["x"]))
    forbidden = ("litellm", "ollama", "openai", "generate_resume_diffs", "improve_resume(")
    assert all(name not in source.lower() for name in forbidden)
