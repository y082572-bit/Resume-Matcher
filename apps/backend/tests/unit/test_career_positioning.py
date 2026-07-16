import pytest
from app.services.career_positioning import (
    RoleLevel,
    RiskLevel,
    PositioningStrategy,
    TitleTreatment,
    CandidatePositioningProfile,
    JobPositioningInput,
    RoleSignal,
    PositioningResult,
    classify_job_level,
    assess_positioning,
)


def make_candidate(
    level=RoleLevel.SPECIALIST,
    total_years=5.0,
    management_years=0.0,
    managed_people_max=0,
    managed_units_max=0,
    has_strategy_responsibility=False,
    has_budget_responsibility=False,
    has_sales_result_responsibility=False,
    evidence_references=("ref-1",),
):
    return CandidatePositioningProfile(
        candidate_level=level,
        total_years=total_years,
        management_years=management_years,
        managed_people_max=managed_people_max,
        managed_units_max=managed_units_max,
        has_strategy_responsibility=has_strategy_responsibility,
        has_budget_responsibility=has_budget_responsibility,
        has_sales_result_responsibility=has_sales_result_responsibility,
        evidence_references=evidence_references,
    )


# 1-7. RoleLevel Classification Tests
def test_role_level_classification_unknown():
    job = JobPositioningInput(title="xyz abc", description="brak znanych słów")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence == 0.0


def test_role_level_classification_junior():
    job = JobPositioningInput(title="młodszy programista", description="nauka i praca")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.JUNIOR


def test_role_level_classification_specialist():
    job = JobPositioningInput(title="specjalista ds. analiz", description="samodzielne zadania")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_role_level_classification_expert():
    job = JobPositioningInput(title="starszy architekt systemowy", description="projektowanie systemów")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_role_level_classification_manager():
    job = JobPositioningInput(title="kierownik zespołu", description="zarządzanie zespołem")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_role_level_classification_director():
    job = JobPositioningInput(title="dyrektor finansowy", description="tworzenie strategii")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.DIRECTOR


def test_role_level_classification_executive():
    job = JobPositioningInput(title="członek zarządu", description="reprezentacja spółki")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXECUTIVE


# 8-10. Excluded / Different Manager Roles
def test_account_manager_classification():
    job = JobPositioningInput(title="Account Manager", description="obsługa klientów")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_key_account_manager_classification():
    job = JobPositioningInput(title="Key Account Manager", description="obsługa klientów")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_product_manager_classification():
    job = JobPositioningInput(title="Product Manager", description="odpowiedzialność za produkt")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


# 11-12. Manager alone vs Manager with team
def test_manager_alone():
    job = JobPositioningInput(title="Manager", description="zarządzanie zadaniami")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence <= 0.59


def test_manager_with_team():
    job = JobPositioningInput(title="Manager", description="zarządzanie zespołem")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


# 13-14. Lead Keyword
def test_lead_ambiguous():
    job = JobPositioningInput(title="Lead Developer", description="programowanie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT
    assert confidence <= 0.59


def test_lead_resolved():
    job = JobPositioningInput(title="Lead Developer", description="zarządzanie zespołem")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


# 15. Senior alone without context
def test_senior_alone_without_context():
    job = JobPositioningInput(title="Senior", description="ogólne opisy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN


# 16. Polish characters
def test_polish_characters():
    job = JobPositioningInput(title="młodszy asystent", description="staż")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.JUNIOR


# 17. Different dashes
def test_different_hyphens():
    job = JobPositioningInput(title="starszy - projektant — architekt", description="zadania")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


# 18-24. Gap 0 cases and strategies
def test_assess_gap_zero_junior():
    candidate = make_candidate(level=RoleLevel.JUNIOR)
    job = JobPositioningInput(title="junior developer", description="nauka")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.overqualification_risk == RiskLevel.NONE
    assert res.underqualification_risk == RiskLevel.NONE
    assert res.strategy == PositioningStrategy.JUNIOR_ENTRY
    assert res.title_treatment == TitleTreatment.KEEP


def test_assess_gap_zero_specialist():
    candidate = make_candidate(level=RoleLevel.SPECIALIST)
    job = JobPositioningInput(title="developer", description="samodzielne zadania")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.strategy == PositioningStrategy.SPECIALIST_DELIVERY


def test_assess_gap_zero_expert():
    candidate = make_candidate(level=RoleLevel.EXPERT)
    job = JobPositioningInput(title="senior developer", description="projektowanie")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.strategy == PositioningStrategy.EXPERT_AUTHORITY


def test_assess_gap_zero_manager():
    candidate = make_candidate(level=RoleLevel.MANAGER)
    job = JobPositioningInput(title="kierownik zespołu", description="zarządzanie zespołem")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.strategy == PositioningStrategy.MANAGER_LEADERSHIP


def test_assess_gap_zero_director():
    candidate = make_candidate(level=RoleLevel.DIRECTOR)
    job = JobPositioningInput(title="dyrektor", description="tworzenie strategii")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.strategy == PositioningStrategy.DIRECTOR_SCALE


def test_assess_gap_zero_executive():
    candidate = make_candidate(level=RoleLevel.EXECUTIVE)
    job = JobPositioningInput(title="prezes zarządu", description="reprezentacja zarządu")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 0
    assert res.strategy == PositioningStrategy.EXECUTIVE_ENTERPRISE


# 25-31. Gap 1 cases, risks and scales
def test_assess_gap_one_low_risk_small_scale():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=5.0)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.LOW
    assert res.strategy == PositioningStrategy.BALANCED
    assert res.title_treatment == TitleTreatment.CONTEXTUALIZE


def test_assess_gap_one_medium_risk_large_people():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, managed_people_max=50)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM
    assert res.strategy == PositioningStrategy.CONTROLLED_FLATTENING


def test_assess_gap_one_medium_risk_large_years():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, management_years=8.0)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_one_medium_risk_large_units():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, managed_units_max=20)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_one_medium_risk_strategy():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, has_strategy_responsibility=True)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_one_medium_risk_budget():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, has_budget_responsibility=True)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_one_medium_risk_sales():
    candidate = make_candidate(level=RoleLevel.EXPERT, total_years=10.0, has_sales_result_responsibility=True)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 1
    assert res.overqualification_risk == RiskLevel.MEDIUM


# 32-33. Gap 2 and Gap 3
def test_assess_gap_two_medium_risk():
    candidate = make_candidate(level=RoleLevel.MANAGER)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap == 2
    assert res.overqualification_risk == RiskLevel.MEDIUM
    assert res.strategy == PositioningStrategy.CONTROLLED_FLATTENING
    assert res.title_treatment == TitleTreatment.DEEMPHASIZE
    assert len(res.emphasize) > 0
    assert len(res.deemphasize) > 0


def test_assess_gap_three_plus_high_risk():
    candidate = make_candidate(level=RoleLevel.DIRECTOR)
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res = assess_positioning(candidate, job)
    assert res.level_gap >= 3
    assert res.overqualification_risk == RiskLevel.HIGH


# 34-37. Gap -1, -2, -3
def test_assess_gap_minus_one_low_risk():
    candidate = make_candidate(level=RoleLevel.SPECIALIST, total_years=4.0)
    job = JobPositioningInput(title="senior developer", description="projektowanie", required_years=3.0)
    res = assess_positioning(candidate, job)
    assert res.level_gap == -1
    assert res.underqualification_risk == RiskLevel.LOW
    assert res.strategy == PositioningStrategy.CONTROLLED_ELEVATION
    assert res.title_treatment == TitleTreatment.KEEP


def test_assess_gap_minus_one_medium_risk():
    candidate = make_candidate(level=RoleLevel.SPECIALIST, total_years=1.0)
    job = JobPositioningInput(title="senior developer", description="projektowanie", required_years=4.0)
    res = assess_positioning(candidate, job)
    assert res.level_gap == -1
    assert res.underqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_minus_two_medium_risk():
    candidate = make_candidate(level=RoleLevel.SPECIALIST)
    job = JobPositioningInput(title="kierownik zespołu", description="zarządzanie zespołem")
    res = assess_positioning(candidate, job)
    assert res.level_gap == -2
    assert res.underqualification_risk == RiskLevel.MEDIUM


def test_assess_gap_minus_three_plus_high_risk():
    candidate = make_candidate(level=RoleLevel.JUNIOR)
    job = JobPositioningInput(title="kierownik zespołu", description="zarządzanie zespołem")
    res = assess_positioning(candidate, job)
    assert res.level_gap <= -3
    assert res.underqualification_risk == RiskLevel.HIGH


# 38. 20 years experience without auto overqualification
def test_20_years_experience_no_auto_overqualification():
    candidate = make_candidate(level=RoleLevel.SPECIALIST, total_years=20.0)
    job = JobPositioningInput(title="starszy specjalista ds analiz", description="samodzielne analizy")
    res = assess_positioning(candidate, job)
    assert res.overqualification_risk == RiskLevel.NONE


# 39. Determinism
def test_determinism():
    candidate = make_candidate()
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    res1 = assess_positioning(candidate, job)
    res2 = assess_positioning(candidate, job)
    assert res1 == res2


# 40. Immutability
def test_immutability():
    candidate = make_candidate()
    job = JobPositioningInput(title="developer", description="samodzielna praca")
    assess_positioning(candidate, job)
    assert candidate.total_years == 5.0
    assert job.title == "developer"


# 41-49. Validation Tests
def test_validation_negative_total_years():
    with pytest.raises(ValueError):
        make_candidate(total_years=-1.0)


def test_validation_negative_management_years():
    with pytest.raises(ValueError):
        make_candidate(management_years=-1.0)


def test_validation_management_years_greater_than_total():
    with pytest.raises(ValueError):
        make_candidate(total_years=3.0, management_years=4.0)


def test_validation_negative_people():
    with pytest.raises(ValueError):
        make_candidate(managed_people_max=-5)


def test_validation_negative_units():
    with pytest.raises(ValueError):
        make_candidate(managed_units_max=-2)


def test_validation_empty_whitespace_title():
    with pytest.raises(ValueError):
        JobPositioningInput(title="   ", description="opisy")


def test_validation_negative_required_years():
    with pytest.raises(ValueError):
        JobPositioningInput(title="Tytuł", description="Opis", required_years=-2.0)


def test_validation_evidence_references_empty():
    with pytest.raises(ValueError):
        make_candidate(evidence_references=("",))


def test_validation_evidence_references_non_string():
    with pytest.raises(TypeError):
        make_candidate(evidence_references=(123,))


# 50. Optional required_years
def test_optional_required_years_default():
    job = JobPositioningInput(title="Tytuł", description="Opis")
    assert job.required_years is None


# 51-52. Explicit people management
def test_explicit_people_management_true():
    job = JobPositioningInput(title="developer", description="praca", explicit_people_management=True)
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_explicit_people_management_false():
    job = JobPositioningInput(title="Manager", description="zarządzanie zespołem", explicit_people_management=False)
    level, confidence, signals, reasons = classify_job_level(job)
    assert level != RoleLevel.MANAGER


# 53. Matched signals and reasons structure
def test_result_matched_signals_and_reasons():
    candidate = make_candidate()
    job = JobPositioningInput(title="młodszy programista", description="nauka")
    res = assess_positioning(candidate, job)
    assert isinstance(res.matched_signals, tuple)
    assert isinstance(res.reasons, tuple)
    assert len(res.reasons) > 0


# 54. Confidence in range
def test_confidence_within_range():
    candidate = make_candidate()
    job = JobPositioningInput(title="kierownik zespołu", description="zarządzanie zespołem")
    res = assess_positioning(candidate, job)
    assert 0.0 <= res.confidence <= 1.0


# --- 10. NEW REGRESSION TESTS ---

def test_ekspert_alone_is_expert():
    # Rule 1
    job = JobPositioningInput(title="Ekspert", description="analizy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_expert_alone_is_expert():
    # Rule 1
    job = JobPositioningInput(title="Expert", description="analizy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_konsultant_biznesowy_is_expert():
    # Rule 6
    job = JobPositioningInput(title="Konsultant biznesowy", description="analizy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_consultant_is_expert():
    # Rule 6
    job = JobPositioningInput(title="Consultant", description="analizy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_team_leader_is_manager():
    # Rule 6
    job = JobPositioningInput(title="Team Leader", description="zarządzanie zadaniami")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_lider_zespolu_is_manager():
    # Rule 6
    job = JobPositioningInput(title="Lider zespołu", description="zarządzanie zadaniami")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_vp_sales_is_executive():
    # Rule 6
    job = JobPositioningInput(title="VP Sales", description="dyrektor")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXECUTIVE


def test_glowny_specjalista_is_expert():
    # Rule 6
    job = JobPositioningInput(title="Główny specjalista", description="analizy")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_head_hunter_is_specialist():
    # Rule 4
    job = JobPositioningInput(title="Head Hunter", description="rekrutacja")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_ceo_assistant_is_specialist():
    # Rule 5
    job = JobPositioningInput(title="CEO Assistant", description="wsparcie CEO")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_asystent_ceo_is_specialist():
    # Rule 5
    job = JobPositioningInput(title="Asystent CEO", description="wsparcie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_executive_assistant_is_specialist():
    # Rule 5
    job = JobPositioningInput(title="Executive Assistant", description="wsparcie zarządu")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_project_manager_without_people_mgmt_is_expert():
    # Rule 7
    job = JobPositioningInput(title="Kierownik projektu", description="prowadzenie projektu bez zespołu")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT
    assert confidence <= 0.59


def test_project_manager_with_people_mgmt_is_manager():
    # Rule 7
    job = JobPositioningInput(title="Kierownik projektu", description="zarządzanie zespołem ludzi")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_kierownik_zespolu_conflict_explicit_false():
    # Rule 2 Conflict B
    job = JobPositioningInput(title="Kierownik zespołu", description="kierowanie zespołem", explicit_people_management=False)
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence <= 0.49

    candidate = make_candidate()
    res = assess_positioning(candidate, job)
    assert res.requires_human_review is True
    assert res.strategy == PositioningStrategy.REVIEW_REQUIRED
    assert res.title_treatment == TitleTreatment.REVIEW


def test_kierownik_zespolu_conflict_negation():
    # Rule 2 Conflict B
    job = JobPositioningInput(title="Kierownik zespołu", description="kierowanie zespołem, brak podległych pracowników")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence <= 0.49

    candidate = make_candidate()
    res = assess_positioning(candidate, job)
    assert res.requires_human_review is True


def test_strong_conflict_blocks_risk_assessment():
    # Rule 3
    job = JobPositioningInput(title="Kierownik zespołu", description="brak podległych pracowników")
    candidate = make_candidate(level=RoleLevel.EXECUTIVE)  # gap should be huge if classified, but conflict blocks it
    res = assess_positioning(candidate, job)
    assert res.overqualification_risk == RiskLevel.NONE
    assert res.underqualification_risk == RiskLevel.NONE
    assert res.strategy == PositioningStrategy.REVIEW_REQUIRED


def test_candidate_boolean_validation_raises_type_error():
    # Rule 8
    with pytest.raises(TypeError):
        make_candidate(has_strategy_responsibility=1)  # type: ignore
    with pytest.raises(TypeError):
        make_candidate(has_budget_responsibility="yes")  # type: ignore
    with pytest.raises(TypeError):
        make_candidate(has_sales_result_responsibility=None)  # type: ignore


def test_director_scale_stabilize_elements():
    # Rule 10
    candidate = make_candidate(level=RoleLevel.DIRECTOR)
    job = JobPositioningInput(title="dyrektor", description="tworzenie strategii")
    res = assess_positioning(candidate, job)
    assert "strategia" in res.emphasize
    assert "odpowiedzialność za budżet" in res.emphasize


def test_expert_authority_stabilize_elements():
    # Rule 10
    candidate = make_candidate(level=RoleLevel.EXPERT)
    job = JobPositioningInput(title="senior developer", description="projektowanie")
    res = assess_positioning(candidate, job)
    assert "wiedza ekspercka" in res.emphasize
    assert "szkolenia i rozwój kompetencji" in res.emphasize


# --- Additional Regression and Mandatory Tests ---

def test_strategic_partnership_manager_without_team():
    job = JobPositioningInput(title="Strategic Partnership Manager", description="zarządzanie relacjami")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence <= 0.59


def test_senior_account_manager_classification():
    job = JobPositioningInput(title="Senior Account Manager", description="zarządzanie klientami")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_trener_sprzedazy_is_expert():
    job = JobPositioningInput(title="Trener sprzedaży", description="szkolenia")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_sales_trainer_is_expert():
    job = JobPositioningInput(title="Sales Trainer", description="szkolenia")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_doradca_ubezpieczeniowy_is_specialist():
    job = JobPositioningInput(title="Doradca ubezpieczeniowy", description="doradztwo")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_koordynator_projektu_is_specialist():
    job = JobPositioningInput(title="Koordynator projektu", description="koordynacja")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_analyst_reports_to_ceo():
    job = JobPositioningInput(title="Analyst", description="reports to CEO")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_coordinator_works_with_vice_president():
    job = JobPositioningInput(title="Coordinator", description="works with vice president")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_mlodszy_specjalista_hyphen():
    job = JobPositioningInput(title="młodszy-specjalista", description="praca")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.JUNIOR


def test_head_of_sales_hyphen():
    job = JobPositioningInput(title="head-of-sales", description="zarządzanie regionem")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.DIRECTOR


def test_c_level_hyphen():
    job = JobPositioningInput(title="c-level", description="reprezentacja zarządu")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXECUTIVE


def test_lead_developer_without_people_mgmt():
    job = JobPositioningInput(title="Lead Developer", description="programowanie bez zespołu")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT


def test_lead_developer_with_people_mgmt():
    job = JobPositioningInput(title="Lead Developer", description="zarządzanie zespołem")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.MANAGER


def test_confidence_unresolved_manager():
    job = JobPositioningInput(title="Manager", description="praca")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.UNKNOWN
    assert confidence <= 0.59


def test_confidence_unresolved_lead():
    job = JobPositioningInput(title="Lead", description="praca")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.EXPERT
    assert confidence <= 0.59


def test_nan_and_infinity_raises():
    with pytest.raises(TypeError):
        make_candidate(total_years=float("nan"))
    with pytest.raises(TypeError):
        make_candidate(total_years=float("inf"))


def test_bool_in_managed_people_max_raises():
    with pytest.raises(TypeError):
        make_candidate(managed_people_max=True)  # type: ignore
    with pytest.raises(TypeError):
        make_candidate(managed_units_max=False)  # type: ignore


def test_invalid_candidate_level_raises():
    with pytest.raises(TypeError):
        make_candidate(level="INVALID")  # type: ignore


def test_assess_ambiguous_lead_does_not_raise():
    # Rule 6
    candidate = make_candidate(level=RoleLevel.EXPERT)
    job = JobPositioningInput(title="Lead Developer", description="programowanie")
    res = assess_positioning(candidate, job)
    assert res.job_level == RoleLevel.EXPERT
    assert res.requires_human_review is True


def test_assess_resolved_lead_does_not_raise():
    # Rule 6
    candidate = make_candidate(level=RoleLevel.MANAGER)
    job = JobPositioningInput(title="Lead Developer", description="zarządzanie zespołem")
    res = assess_positioning(candidate, job)
    assert res.job_level == RoleLevel.MANAGER
    assert res.requires_human_review is False


def test_assess_unresolved_project_manager_requires_review():
    # Rule 6
    candidate = make_candidate(level=RoleLevel.EXPERT)
    job = JobPositioningInput(title="Kierownik projektu", description="prowadzenie projektu bez zespołu")
    res = assess_positioning(candidate, job)
    assert res.job_level == RoleLevel.EXPERT
    assert res.requires_human_review is True


def test_assistant_to_the_ceo_is_specialist():
    # Rule 6
    job = JobPositioningInput(title="Assistant to the CEO", description="wsparcie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_executive_assistant_to_the_ceo_is_specialist():
    # Rule 6
    job = JobPositioningInput(title="Executive Assistant to the CEO", description="wsparcie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_asystentka_prezesa_is_specialist():
    # Rule 6
    job = JobPositioningInput(title="Asystentka prezesa", description="wsparcie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST


def test_asystentka_zarzadu_is_specialist():
    # Rule 6
    job = JobPositioningInput(title="Asystentka zarządu", description="wsparcie")
    level, confidence, signals, reasons = classify_job_level(job)
    assert level == RoleLevel.SPECIALIST
