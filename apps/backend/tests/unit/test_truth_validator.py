"""Unit tests for Truth Validator service."""

import pytest
from app.services.truth_index import (
    TruthIndex,
    EmploymentTruthScope,
    TruthFact,
    build_truth_index,
    normalize_truth_text,
)
from app.services.truth_validator import (
    TruthDecision,
    TruthViolation,
    ResumeClaim,
    TruthAuditResult,
    audit_resume_claims,
    _extract_numbers,
    _check_numeric_violation,
    _check_blocked_rule,
    _check_supported_fact,
)


# Fixtures

@pytest.fixture
def ergo_hestia_fact():
    """ERGO Hestia 116% fact from Truth Library."""
    return TruthFact(
        fact_id="dz-2:wynikLiczbowy",
        section="doswiadczenieZawodowe",
        employment_id="dz-2",
        company="ERGO Hestia",
        role="Regionalny Menedżer Sprzedaży",
        fact_type="wynikLiczbowy",
        original_text="116% wolumenu sprzedaży przy średnim wyniku przedstawicielstwa 96%",
        normalized_text=normalize_truth_text("116% wolumenu sprzedaży przy średnim wyniku przedstawicielstwa 96%"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-2:wynikLiczbowy",
    )


@pytest.fixture
def warta_scope():
    """Warta employment scope from Truth Library."""
    warta_96_fact = TruthFact(
        fact_id="dz-3:wynikLiczbowy",
        section="doswiadczenieZawodowe",
        employment_id="dz-3",
        company="Warta",
        role="Menedżer Dystrybucji",
        fact_type="wynikLiczbowy",
        original_text="96% wolumenu",
        normalized_text=normalize_truth_text("96% wolumenu"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-3:wynikLiczbowy",
    )

    growth_fact = TruthFact(
        fact_id="dz-3:aktywnosci:0",
        section="doswiadczenieZawodowe",
        employment_id="dz-3",
        company="Warta",
        role="Menedżer Dystrybucji",
        fact_type="aktywnosci",
        original_text="wzrost efektywności ze 36% do 96%",
        normalized_text=normalize_truth_text("wzrost efektywności ze 36% do 96%"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-3:aktywnosci:0",
    )

    scope = EmploymentTruthScope(
        employment_id="dz-3",
        company="Warta",
        role="Menedżer Dystrybucji",
        role_alt="Kierownik Dystrybucji",
    )
    scope.numeric_results = [warta_96_fact, growth_fact]
    scope.allowed_facts = [warta_96_fact, growth_fact]
    return scope


@pytest.fixture
def ergo_hestia_scope(ergo_hestia_fact):
    """ERGO Hestia employment scope from Truth Library."""
    budget_fact = TruthFact(
        fact_id="dz-2:aktywnosci:2",
        section="doswiadczenieZawodowe",
        employment_id="dz-2",
        company="ERGO Hestia",
        role="Regionalny Menedżer Sprzedaży",
        fact_type="aktywnosci",
        original_text="odpowiedzialność za budżet kontraktowy oraz ustalanie poziomów kontraktowych",
        normalized_text=normalize_truth_text("odpowiedzialność za budżet kontraktowy oraz ustalanie poziomów kontraktowych"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-2:aktywnosci:2",
    )

    marketing_budget_fact = TruthFact(
        fact_id="dz-2:aktywnosci:3",
        section="doswiadczenieZawodowe",
        employment_id="dz-2",
        company="ERGO Hestia",
        role="Regionalny Menedżer Sprzedaży",
        fact_type="aktywnosci",
        original_text="odpowiedzialność za budżet marketingowy wspierający realizację celów sprzedażowych",
        normalized_text=normalize_truth_text("odpowiedzialność za budżet marketingowy wspierający realizację celów sprzedażowych"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-2:aktywnosci:3",
    )

    portfolio_fact = TruthFact(
        fact_id="dz-2:skalaOdpowiedzialnosci",
        section="doswiadczenieZawodowe",
        employment_id="dz-2",
        company="ERGO Hestia",
        role="Regionalny Menedżer Sprzedaży",
        fact_type="skalaOdpowiedzialnosci",
        original_text="Portfel finansowy kilkudziesięciu milionów złotych",
        normalized_text=normalize_truth_text("Portfel finansowy kilkudziesięciu milionów złotych"),
        status="PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        use_in_cv=True,
        requires_approval=False,
        source_reference="dz-2:skalaOdpowiedzialnosci",
    )

    scope = EmploymentTruthScope(
        employment_id="dz-2",
        company="ERGO Hestia",
        role="Regionalny Menedżer Sprzedaży",
        role_alt="Regionalny Menedżer",
    )
    scope.numeric_results = [ergo_hestia_fact]
    scope.responsibility_scale = [portfolio_fact]
    scope.activities = [budget_fact, marketing_budget_fact]
    scope.allowed_facts = [ergo_hestia_fact, budget_fact, marketing_budget_fact, portfolio_fact]
    return scope


@pytest.fixture
def truth_index_with_employment(ergo_hestia_scope, warta_scope):
    """TruthIndex with ERGO Hestia and Warta entries."""
    index = TruthIndex()
    index.employment_by_id["dz-2"] = ergo_hestia_scope
    index.employment_by_id["dz-3"] = warta_scope
    index.employment_by_normalized_company["ergo hestia"] = ergo_hestia_scope
    index.employment_by_normalized_company["warta"] = warta_scope
    index.blocked_rules = [
        "ogólna odpowiedzialność budżetowa (bez zatwierdzenia)"
    ]
    index.exceptions = [
        "budżet kontraktowy DOZWOLONE w ERGO Hestii",
        "budżet marketingowy DOZWOLONE w ERGO Hestii",
    ]
    return index


# Tests: Core extraction functions

def test_extract_numbers_percentage():
    """Test extraction of percentages."""
    numbers = _extract_numbers("116% wolumenu sprzedaży")
    assert "116%" in numbers


def test_extract_numbers_range_endash():
    """Test extraction of ranges with en-dash."""
    numbers = _extract_numbers("10–15 doradców")
    assert any("–" in n or "-" in n for n in numbers)


def test_extract_numbers_range_hyphen():
    """Test extraction of ranges with hyphen."""
    numbers = _extract_numbers("10-15 doradców")
    assert any("-" in n for n in numbers)


def test_extract_numbers_decimal():
    """Test extraction of decimals."""
    numbers = _extract_numbers("1,5 miliarda złotych")
    assert any("," in n for n in numbers)


def test_extract_numbers_spaced():
    """Test extraction of spaced numbers."""
    numbers = _extract_numbers("85 agencji")
    assert "85" in numbers


# Test 1: ERGO Hestia, 116% → PASS

def test_ergo_hestia_116_percent_pass(truth_index_with_employment):
    """Test 1: ERGO Hestia claim with 116% should PASS."""
    claims = [
        ResumeClaim(
            claim_id="claim-1",
            section="numeric_result",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="116% wolumenu sprzedaży przy średnim wyniku przedstawicielstwa 96%",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert len(result.blocking_errors) == 0
    assert any(v.code == "TRUTH_SUPPORTED_FACT" for v in result.violations)


# Test 2: Warta, 116% → BLOCK, TRUTH_CROSS_EMPLOYMENT_NUMBER

def test_warta_116_percent_block(truth_index_with_employment):
    """Test 2: Warta claim with 116% should BLOCK (cross-employment)."""
    claims = [
        ResumeClaim(
            claim_id="claim-2",
            section="numeric_result",
            employment_id="dz-3",
            company="Warta",
            role="Menedżer Dystrybucji",
            text="116% wzrostu wolumenu",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert not result.passed
    assert len(result.blocking_errors) > 0
    assert any(
        v.code == "TRUTH_CROSS_EMPLOYMENT_NUMBER"
        for v in result.blocking_errors
    )


# Test 3: ERGO Hestia, 10–15 doradców → BLOCK, TRUTH_UNSUPPORTED_NUMBER

def test_ergo_hestia_unsupported_range_block(truth_index_with_employment):
    """Test 3: ERGO Hestia claim with unsupported 10–15 → BLOCK."""
    claims = [
        ResumeClaim(
            claim_id="claim-3",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="zarządzanie zespołem 10–15 doradców",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert not result.passed
    assert len(result.blocking_errors) > 0
    assert any(
        v.code == "TRUTH_UNSUPPORTED_NUMBER"
        for v in result.blocking_errors
    )


# Test 4: Warta, wzrost z 36% do 96% → PASS

def test_warta_growth_pass(truth_index_with_employment):
    """Test 4: Warta claim about growth 36% → 96% should PASS."""
    claims = [
        ResumeClaim(
            claim_id="claim-4",
            section="activities",
            employment_id="dz-3",
            company="Warta",
            role="Menedżer Dystrybucji",
            text="wzrost efektywności ze 36% do 96%",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert any(v.code == "TRUTH_SUPPORTED_FACT" for v in result.violations)


# Test 5: ERGO Hestia, budżet kontraktowy → PASS

def test_ergo_hestia_budget_kontraktowy_pass(truth_index_with_employment):
    """Test 5: ERGO Hestia claim about contract budget should PASS."""
    claims = [
        ResumeClaim(
            claim_id="claim-5",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="odpowiedzialność za budżet kontraktowy oraz ustalanie poziomów kontraktowych",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert any(v.code == "TRUTH_SUPPORTED_FACT" for v in result.violations)


# Test 6: ERGO Hestia, budżet marketingowy → PASS

def test_ergo_hestia_budget_marketing_pass(truth_index_with_employment):
    """Test 6: ERGO Hestia claim about marketing budget should PASS."""
    claims = [
        ResumeClaim(
            claim_id="claim-6",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="odpowiedzialność za budżet marketingowy wspierający realizację celów sprzedażowych",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert any(v.code == "TRUTH_SUPPORTED_FACT" for v in result.violations)


# Test 7: ERGO Hestia, portfel kilkudziesięciu milionów → PASS

def test_ergo_hestia_portfolio_pass(truth_index_with_employment):
    """Test 7: ERGO Hestia claim about portfolio should PASS."""
    claims = [
        ResumeClaim(
            claim_id="claim-7",
            section="responsibility_scale",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="Portfel finansowy kilkudziesięciu milionów złotych",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert any(v.code == "TRUTH_SUPPORTED_FACT" for v in result.violations)


# Test 8: Same point twice in ERGO Hestia → BLOCK, TRUTH_DUPLICATE_CLAIM

def test_duplicate_claim_same_employment_block(truth_index_with_employment):
    """Test 8: Duplicate claim in same employment should BLOCK."""
    claims = [
        ResumeClaim(
            claim_id="claim-8a",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="odpowiedzialność za budżet kontraktowy",
        ),
        ResumeClaim(
            claim_id="claim-8b",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="odpowiedzialność za budżet kontraktowy",
        ),
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert not result.passed
    assert len(result.blocking_errors) > 0
    assert any(v.code == "TRUTH_DUPLICATE_CLAIM" for v in result.blocking_errors)


# Test 9: Duplicate differing only in capitalization and period → BLOCK

def test_duplicate_different_case_punctuation_block(truth_index_with_employment):
    """Test 9: Duplicates differing only in case/punctuation should BLOCK."""
    claims = [
        ResumeClaim(
            claim_id="claim-9a",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="ODPOWIEDZIALNOŚĆ ZA BUDŻET KONTRAKTOWY",
        ),
        ResumeClaim(
            claim_id="claim-9b",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="odpowiedzialność za budżet kontraktowy.",
        ),
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert not result.passed
    assert any(v.code == "TRUTH_DUPLICATE_CLAIM" for v in result.blocking_errors)


# Test 10: Unchanged text from original_claims → PASS

def test_unchanged_original_claim_pass(truth_index_with_employment):
    """Test 10: Unchanged fact from original should PASS."""
    original = [
        ResumeClaim(
            claim_id="orig-1",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="zarządzanie sprzedażą",
        )
    ]
    claims = [
        ResumeClaim(
            claim_id="claim-10",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="zarządzanie sprzedażą",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment, original)
    violations = [v for v in result.violations if v.code == "TRUTH_ORIGINAL_CLAIM"]
    assert len(violations) > 0
    assert violations[0].decision == TruthDecision.REVIEW
    assert result.requires_review


# Test 11: New sentence without number and unverified → REVIEW, TRUTH_UNGROUNDED_TEXT

def test_ungrounded_text_review(truth_index_with_employment):
    """Test 11: Ungrounded text without number should REVIEW."""
    claims = [
        ResumeClaim(
            claim_id="claim-11",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="wspieranie rozwoju zespołu sprzedażowego",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed  # No blocking errors
    assert result.requires_review
    assert any(v.code == "TRUTH_UNGROUNDED_TEXT" for v in result.warnings)


# Test 12: Budget responsibility general prohibition does not block ERGO Hestii exceptions

def test_blocked_rule_with_exceptions(truth_index_with_employment):
    """Test 12: General budget prohibition should not block approved exceptions."""
    claims = [
        ResumeClaim(
            claim_id="claim-12a",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="budżet kontraktowy DOZWOLONE w ERGO Hestii",
        ),
        ResumeClaim(
            claim_id="claim-12b",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="budżet marketingowy DOZWOLONE w ERGO Hestii",
        ),
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    # Should PASS due to exceptions
    assert not any(v.code == "TRUTH_BLOCKED_RULE" for v in result.blocking_errors)


# Test 13: Budget responsibility assigned to another company → BLOCK or REVIEW

def test_budget_different_company_block(truth_index_with_employment):
    """Test 13: Budget responsibility for different company → BLOCK/REVIEW."""
    claims = [
        ResumeClaim(
            claim_id="claim-13",
            section="activities",
            employment_id="dz-3",
            company="Warta",
            role="Menedżer Dystrybucji",
            text="ogólna odpowiedzialność budżetowa",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    # Should have blocking error or require review due to blocked rule without exception
    blocking_or_review = (
        len(result.blocking_errors) > 0 or result.requires_review
    )
    assert blocking_or_review


# Test 14: BLOCK result has passed=false

def test_block_result_passed_false(truth_index_with_employment):
    """Test 14: Result with BLOCK should have passed=false."""
    claims = [
        ResumeClaim(
            claim_id="claim-14",
            section="activities",
            employment_id="dz-3",
            company="Warta",
            role="Menedżer Dystrybucji",
            text="116% wzrostu",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert not result.passed


# Test 15: REVIEW-only result has passed=true and requires_review=true

def test_review_result_passed_true(truth_index_with_employment):
    """Test 15: Result with only REVIEW should have passed=true, requires_review=true."""
    claims = [
        ResumeClaim(
            claim_id="claim-15",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="nowy nieznany opis działania",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert result.passed
    assert result.requires_review


# Test 16: evidence_references points to correct source_reference

def test_evidence_references_correct(truth_index_with_employment):
    """Test 16: evidence_references should point to source."""
    claims = [
        ResumeClaim(
            claim_id="claim-16",
            section="numeric_result",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="116% wolumenu sprzedaży przy średnim wyniku przedstawicielstwa 96%",
        )
    ]
    result = audit_resume_claims(claims, truth_index_with_employment)
    supported = [v for v in result.violations if v.code == "TRUTH_SUPPORTED_FACT"]
    assert len(supported) > 0
    assert len(supported[0].evidence_references) > 0
    assert "dz-2:wynikLiczbowy" in supported[0].evidence_references[0]


# Test 17: Validator does not modify TruthIndex or input claims

def test_validator_does_not_mutate(truth_index_with_employment):
    """Test 17: Validator should not mutate inputs."""
    import copy
    original_index = copy.deepcopy(truth_index_with_employment)
    original_claims = [
        ResumeClaim(
            claim_id="claim-17",
            section="activities",
            employment_id="dz-2",
            company="ERGO Hestia",
            role="Regionalny Menedżer Sprzedaży",
            text="test claim",
        )
    ]
    claims_copy = copy.deepcopy(original_claims)

    audit_resume_claims(original_claims, truth_index_with_employment)

    # Check that inputs are not modified
    assert original_index == truth_index_with_employment
    assert original_claims == claims_copy

# Security regression tests

def test_numeric_extraction_is_non_overlapping():
    assert _extract_numbers("116%") == ["116%"]
    assert _extract_numbers("10–15 doradców") == ["10–15"]
    assert _extract_numbers("1,5 mln") == ["1,5"]
    assert _extract_numbers("12 500 osób") == ["12 500"]


def test_dates_are_not_business_numbers():
    assert _extract_numbers("08.2024–07.2025") == []
    assert _extract_numbers("2023-2024") == []
    assert _extract_numbers("2026-07-16") == []


def test_percentage_does_not_authorize_plain_count(truth_index_with_employment):
    claim = ResumeClaim(
        claim_id="reg-1", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="zarządzanie zespołem 116 osób",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_UNSUPPORTED_NUMBER"


def test_known_number_with_changed_meaning_requires_review(truth_index_with_employment):
    claim = ResumeClaim(
        claim_id="reg-2", section="numeric_result", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="116% wzrostu zysku przy wyniku 96%",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert result.passed
    assert result.requires_review
    assert result.warnings[0].code == "TRUTH_NUMBER_CONTEXT_REVIEW"


def test_unknown_employment_with_number_blocks(truth_index_with_employment):
    claim = ResumeClaim(
        claim_id="reg-3", section="activities", employment_id="dz-999",
        company="Nieznana firma", role="Menedżer", text="zarządzanie 999 doradcami",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_UNKNOWN_EMPLOYMENT"


def test_company_scope_mismatch_blocks(truth_index_with_employment):
    claim = ResumeClaim(
        claim_id="reg-4", section="numeric_result", employment_id="dz-2",
        company="Warta", role="Regionalny Menedżer Sprzedaży",
        text="116% wolumenu sprzedaży przy średnim wyniku przedstawicielstwa 96%",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_EMPLOYMENT_SCOPE_MISMATCH"


def test_original_claim_cannot_bypass_unsupported_number(truth_index_with_employment):
    original = [ResumeClaim(
        claim_id="orig-reg-5", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="zarządzanie 999 doradcami",
    )]
    result = audit_resume_claims(list(original), truth_index_with_employment, original)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_UNSUPPORTED_NUMBER"


def test_original_claim_does_not_bypass_duplicate_detection(truth_index_with_employment):
    original = [ResumeClaim(
        claim_id="orig-reg-6", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="zarządzanie sprzedażą",
    )]
    claims = [original[0], ResumeClaim(
        claim_id="reg-6b", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="ZARZĄDZANIE SPRZEDAŻĄ.",
    )]
    result = audit_resume_claims(claims, truth_index_with_employment, original)
    assert not result.passed
    assert any(v.code == "TRUTH_DUPLICATE_CLAIM" for v in result.blocking_errors)


def test_number_in_activity_fact_is_authorized_but_changed_text_reviewed(
    truth_index_with_employment,
):
    claim = ResumeClaim(
        claim_id="reg-7", section="activities", employment_id="dz-3",
        company="Warta", role="Menedżer Dystrybucji",
        text="poprawa wskaźnika z 36% do 96%",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert result.passed
    assert result.requires_review
    assert result.warnings[0].code == "TRUTH_NUMBER_CONTEXT_REVIEW"


def test_blocked_budget_rule_matches_adjectival_form(truth_index_with_employment):
    truth_index_with_employment.blocked_rules = [
        "odpowiedzialność za budżet (bez zatwierdzenia)"
    ]
    claim = ResumeClaim(
        claim_id="reg-8", section="activities", employment_id="dz-3",
        company="Warta", role="Menedżer Dystrybucji",
        text="ogólna odpowiedzialność budżetowa",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_BLOCKED_RULE"


def test_exact_scoped_budget_fact_overrides_global_block(truth_index_with_employment):
    truth_index_with_employment.blocked_rules = [
        "odpowiedzialność za budżet (bez zatwierdzenia)"
    ]
    claim = ResumeClaim(
        claim_id="reg-9", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="odpowiedzialność za budżet kontraktowy oraz ustalanie poziomów kontraktowych",
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert result.passed
    assert not result.requires_review
    assert result.violations[0].code == "TRUTH_SUPPORTED_FACT"


def test_fact_requiring_approval_returns_review(truth_index_with_employment):
    fact = truth_index_with_employment.employment_by_id["dz-2"].allowed_facts[0]
    fact.requires_approval = True
    fact.status = "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA"
    truth_index_with_employment.approval_statuses = [fact.status]
    claim = ResumeClaim(
        claim_id="reg-10", section="numeric_result", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text=fact.original_text,
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert result.passed
    assert result.requires_review
    assert result.warnings[0].code == "TRUTH_FACT_REQUIRES_APPROVAL"


def test_fact_blocked_by_status_cannot_pass(truth_index_with_employment):
    fact = truth_index_with_employment.employment_by_id["dz-2"].allowed_facts[0]
    fact.status = "USUNIĘTE_PRZEZ_UŻYTKOWNIKA"
    truth_index_with_employment.blocked_statuses = [fact.status]
    claim = ResumeClaim(
        claim_id="reg-11", section="numeric_result", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text=fact.original_text,
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_FACT_NOT_ALLOWED"


def test_fact_disabled_for_cv_cannot_pass(truth_index_with_employment):
    fact = truth_index_with_employment.employment_by_id["dz-2"].allowed_facts[0]
    fact.use_in_cv = False
    claim = ResumeClaim(
        claim_id="reg-12", section="numeric_result", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text=fact.original_text,
    )
    result = audit_resume_claims([claim], truth_index_with_employment)
    assert not result.passed
    assert result.blocking_errors[0].code == "TRUTH_FACT_NOT_ALLOWED"


def test_every_claim_gets_explicit_decision(truth_index_with_employment):
    claims = [ResumeClaim(
        claim_id="reg-13", section="activities", employment_id="dz-2",
        company="ERGO Hestia", role="Regionalny Menedżer Sprzedaży",
        text="116% wzrostu zysku przy wyniku 96%",
    )]
    result = audit_resume_claims(claims, truth_index_with_employment)
    assert len(result.violations) == len(claims)
    assert result.violations[0].decision in set(TruthDecision)
