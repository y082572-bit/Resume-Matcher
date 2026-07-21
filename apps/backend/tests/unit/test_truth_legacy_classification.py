"""Unit tests for pure legacy status/category classification (P2)."""

from __future__ import annotations

import pytest

from app.schemas.truth_legacy_migration import CategoryDisposition, LegacyClassification
from app.services.truth_legacy_classification import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_REVIEW,
    category_disposition,
    classify_category,
    classify_status,
)


@pytest.mark.parametrize("status", sorted(STATUS_CONFIRMED))
def test_confirmed_statuses_are_migratable(status: str) -> None:
    decision = classify_status(status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.MIGRATABLE
    assert decision.fact_status == "CONFIRMED"
    assert decision.use_in_cv is True
    assert decision.requires_approval is False


@pytest.mark.parametrize("status", sorted(STATUS_REVIEW))
def test_review_statuses_require_review(status: str) -> None:
    decision = classify_status(status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW
    assert decision.fact_status == "REVIEW_REQUIRED"


@pytest.mark.parametrize("status", sorted(STATUS_REJECTED))
def test_rejected_statuses_are_rejected(status: str) -> None:
    decision = classify_status(status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REJECTED
    assert decision.fact_status is None


def test_unknown_status_requires_review() -> None:
    decision = classify_status("SOME_UNKNOWN_STATUS", use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW
    assert "unknown status" in decision.reason


@pytest.mark.parametrize("status", [None, "", "   "])
def test_missing_status_requires_review(status) -> None:
    decision = classify_status(status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW
    assert "status missing" in decision.reason


@pytest.mark.parametrize(
    "use_in_cv,requires_approval",
    [(False, False), (True, True), (False, True)],
)
def test_boolean_inconsistency_downgrades_confirmed_to_review_never_rejected(
    use_in_cv, requires_approval
) -> None:
    status = sorted(STATUS_CONFIRMED)[0]
    decision = classify_status(status, use_in_cv=use_in_cv, requires_approval=requires_approval)
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW


def test_booleans_never_upgrade_review_or_rejected() -> None:
    review_status = sorted(STATUS_REVIEW)[0]
    decision = classify_status(review_status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW

    rejected_status = sorted(STATUS_REJECTED)[0]
    decision = classify_status(rejected_status, use_in_cv=True, requires_approval=False)
    assert decision.classification == LegacyClassification.REJECTED


def test_prawda_z_dokumentu_do_zatwierdzenia_never_rejected() -> None:
    for use_in_cv in (True, False):
        for requires_approval in (True, False):
            decision = classify_status(
                "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA",
                use_in_cv=use_in_cv,
                requires_approval=requires_approval,
            )
            assert decision.classification == LegacyClassification.REQUIRES_REVIEW


# ---------------------------------------------------------------------------
# Category-level decisions
# ---------------------------------------------------------------------------


def test_branze_is_always_requires_review_regardless_of_status() -> None:
    decision = classify_category(
        "branze", status=None, use_in_cv=None, requires_approval=None, has_stable_id=False
    )
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW

    decision = classify_category(
        "branze",
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW


def test_dane_osobowe_is_always_unsupported() -> None:
    decision = classify_category(
        "daneOsobowe",
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert decision.classification == LegacyClassification.UNSUPPORTED


@pytest.mark.parametrize("category", ["zakazy", "reguly", "tranferowalneKompetencje"])
def test_deferred_policy_categories_raise_if_classified(category: str) -> None:
    assert category_disposition(category) == CategoryDisposition.DEFERRED_POLICY
    with pytest.raises(ValueError, match="DEFERRED_POLICY"):
        classify_category(
            category, status=None, use_in_cv=None, requires_approval=None, has_stable_id=False
        )


def test_processed_category_disposition() -> None:
    assert category_disposition("kompetencje") == CategoryDisposition.PROCESSED
    assert category_disposition("doswiadczenieZawodowe") == CategoryDisposition.PROCESSED


@pytest.mark.parametrize("category", ["osiagnieciaLiczbowe", "skalaOdpowiedzialnosci"])
def test_global_achievement_categories_require_review_without_stable_id(category: str) -> None:
    decision = classify_category(
        category,
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=False,
    )
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW
    assert "no stable legacy id" in decision.reason


@pytest.mark.parametrize("category", ["osiagnieciaLiczbowe", "skalaOdpowiedzialnosci"])
def test_global_achievement_categories_migratable_with_stable_id(category: str) -> None:
    decision = classify_category(
        category,
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert decision.classification == LegacyClassification.MIGRATABLE


@pytest.mark.parametrize(
    "category",
    ["kompetencje", "narzedzia", "technologie", "certyfikaty", "kursy", "wyksztalcenie", "jezyki"],
)
def test_named_categories_per_record_classification(category: str) -> None:
    migratable = classify_category(
        category,
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert migratable.classification == LegacyClassification.MIGRATABLE

    rejected = classify_category(
        category,
        status=sorted(STATUS_REJECTED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert rejected.classification == LegacyClassification.REJECTED


def test_no_stable_id_never_yields_migratable_for_any_category() -> None:
    decision = classify_category(
        "kompetencje",
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=False,
    )
    assert decision.classification == LegacyClassification.REQUIRES_REVIEW


def test_doswiadczenie_zawodowe_per_record_classification() -> None:
    decision = classify_category(
        "doswiadczenieZawodowe",
        status=sorted(STATUS_CONFIRMED)[0],
        use_in_cv=True,
        requires_approval=False,
        has_stable_id=True,
    )
    assert decision.classification == LegacyClassification.MIGRATABLE
