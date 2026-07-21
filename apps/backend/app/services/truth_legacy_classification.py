"""Pure legacy-status classification for Explicit Provenance P2.

No I/O, no database, no identity/fingerprint computation. Given a legacy
record's status/booleans and category, decides whether it is eligible for
migration into P1 (MIGRATABLE), needs a human decision (REQUIRES_REVIEW), was
explicitly rejected by the legacy lifecycle (REJECTED), or is out of scope for
P2 entirely (UNSUPPORTED / DEFERRED_POLICY). See
docs/explicit-provenance-stage-p2.md for the full decision table.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.truth_legacy_migration import CategoryDisposition, LegacyClassification


# Legacy status literals (Polish, as they appear verbatim in the legacy JSON).
STATUS_CONFIRMED = frozenset(
    {
        "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
        "PRAWDA_BEZPOŚREDNIA",
    }
)
STATUS_REVIEW = frozenset(
    {
        "TRANSFEROWALNE_RYZYKOWNE",
        "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA",
    }
)
STATUS_REJECTED = frozenset(
    {
        "NIEPOTWIERDZONE_ZABLOKOWAĆ",
        "NIEJASNE",
        "USUNIĘTE",
    }
)
# This status must never be downgraded to REJECTED regardless of booleans or
# unknown-status handling — it always resolves to REQUIRES_REVIEW.
STATUS_NEVER_REJECT = frozenset({"PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA"})

KNOWN_STATUSES = STATUS_CONFIRMED | STATUS_REVIEW | STATUS_REJECTED

# Categories that receive no ledger row and no TruthFact at all — policy
# decisions deferred entirely, out of scope for P2.
DEFERRED_POLICY_CATEGORIES = frozenset({"zakazy", "reguly", "tranferowalneKompetencje"})
# Ledger row created, but never a TruthFact, regardless of status/booleans.
UNSUPPORTED_CATEGORIES = frozenset({"daneOsobowe"})
# Ledger row created, always REQUIRES_REVIEW, never an auto-confirmed fact.
FORCED_REVIEW_CATEGORIES = frozenset({"branze"})
# Categories where a record without a stable legacy id must never resolve to
# MIGRATABLE (content-fingerprint identity alone is not durable/unambiguous
# enough for free-text achievement/scale descriptions).
STABLE_ID_REQUIRED_CATEGORIES = frozenset({"osiagnieciaLiczbowe", "skalaOdpowiedzialnosci"})

TRUTH_FACT_STATUS_BY_CLASSIFICATION = {
    LegacyClassification.MIGRATABLE: "CONFIRMED",
    LegacyClassification.REQUIRES_REVIEW: "REVIEW_REQUIRED",
}


@dataclass(frozen=True)
class ClassificationDecision:
    classification: LegacyClassification
    reason: str
    fact_status: str | None
    use_in_cv: bool
    requires_approval: bool


def _decision(
    classification: LegacyClassification, reason: str
) -> ClassificationDecision:
    if classification == LegacyClassification.MIGRATABLE:
        return ClassificationDecision(
            classification=classification,
            reason=reason,
            fact_status="CONFIRMED",
            use_in_cv=True,
            requires_approval=False,
        )
    if classification == LegacyClassification.REQUIRES_REVIEW:
        return ClassificationDecision(
            classification=classification,
            reason=reason,
            fact_status="REVIEW_REQUIRED",
            use_in_cv=False,
            requires_approval=True,
        )
    return ClassificationDecision(
        classification=classification,
        reason=reason,
        fact_status=None,
        use_in_cv=False,
        requires_approval=True,
    )


def classify_status(
    status: str | None, *, use_in_cv: bool | None, requires_approval: bool | None
) -> ClassificationDecision:
    """Classify a single legacy record from its status and lifecycle booleans.

    Pure status/boolean logic only — no category-specific overrides (stable-id
    requirement, forced review, unsupported, deferred) live in
    :func:`classify_category`.
    """

    normalized_status = status.strip() if isinstance(status, str) else status

    if not normalized_status:
        return _decision(LegacyClassification.REQUIRES_REVIEW, "status missing")

    if normalized_status not in KNOWN_STATUSES:
        return _decision(LegacyClassification.REQUIRES_REVIEW, "unknown status")

    if normalized_status in STATUS_REJECTED:
        return _decision(LegacyClassification.REJECTED, f"status={normalized_status}")

    if normalized_status in STATUS_REVIEW:
        return _decision(LegacyClassification.REQUIRES_REVIEW, f"status={normalized_status}")

    # normalized_status in STATUS_CONFIRMED.
    if normalized_status in STATUS_NEVER_REJECT:  # defensive; disjoint from STATUS_CONFIRMED today
        return _decision(LegacyClassification.REQUIRES_REVIEW, f"status={normalized_status}")

    effective_use_in_cv = True if use_in_cv is None else bool(use_in_cv)
    effective_requires_approval = False if requires_approval is None else bool(requires_approval)

    if effective_use_in_cv is False or effective_requires_approval is True:
        # A confirmed status can only ever be downgraded to REQUIRES_REVIEW by
        # inconsistent legacy booleans — it can never be upgraded, and an
        # inconsistency must never resolve to REJECTED.
        return _decision(
            LegacyClassification.REQUIRES_REVIEW,
            f"status={normalized_status} but uzywacWCV/wymagaAkceptacji inconsistent",
        )

    return _decision(LegacyClassification.MIGRATABLE, f"status={normalized_status}")


def category_disposition(category: str) -> CategoryDisposition:
    if category in DEFERRED_POLICY_CATEGORIES:
        return CategoryDisposition.DEFERRED_POLICY
    return CategoryDisposition.PROCESSED


def classify_category(
    category: str,
    *,
    status: str | None,
    use_in_cv: bool | None,
    requires_approval: bool | None,
    has_stable_id: bool,
) -> ClassificationDecision:
    """Classify one record within its category, applying category overrides.

    ``has_stable_id`` must reflect the identity anchor actually used for this
    record's ``legacy_record_key`` (an explicit ``id`` field, or — for the
    named-identity categories — a non-blank name field). Categories in
    :data:`DEFERRED_POLICY_CATEGORIES` must never reach this function; callers
    are expected to skip them entirely (see :func:`category_disposition`).
    """

    if category in DEFERRED_POLICY_CATEGORIES:
        raise ValueError(f"category '{category}' is DEFERRED_POLICY and must not be classified")

    if category in UNSUPPORTED_CATEGORIES:
        return _decision(LegacyClassification.UNSUPPORTED, "category is UNSUPPORTED (daneOsobowe)")

    if category in FORCED_REVIEW_CATEGORIES:
        return _decision(
            LegacyClassification.REQUIRES_REVIEW, "category always REQUIRES_REVIEW (branze)"
        )

    decision = classify_status(
        status, use_in_cv=use_in_cv, requires_approval=requires_approval
    )

    if not has_stable_id and decision.classification == LegacyClassification.MIGRATABLE:
        # General identity rule (explicit for osiagnieciaLiczbowe/
        # skalaOdpowiedzialnosci, applied uniformly): content-fingerprint-only
        # identity is never sufficient for an auto-CONFIRMED fact.
        return _decision(
            LegacyClassification.REQUIRES_REVIEW,
            f"{decision.reason}; no stable legacy id for {category}",
        )

    return decision
