"""Deterministic Truth Validator for resume claims.

The validator treats the Truth Library as the only source of automatic PASS
results. Previous resume text is not trusted evidence by itself.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional

from app.services.truth_index import (
    EmploymentTruthScope,
    TruthFact,
    TruthIndex,
    normalize_truth_text,
)


class TruthDecision(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class NumericToken:
    """A non-overlapping numeric token extracted from a claim."""

    raw: str
    canonical: str
    kind: str
    start: int
    end: int


@dataclass
class TruthViolation:
    code: str
    decision: TruthDecision
    message: str
    claim_text: str
    employment_id: Optional[str] = None
    company: Optional[str] = None
    source_path: Optional[str] = None
    evidence_references: list[str] = field(default_factory=list)
    detected_values: list[str] = field(default_factory=list)


@dataclass
class ResumeClaim:
    claim_id: str
    section: str
    employment_id: str
    company: str
    role: str
    text: str
    source_path: Optional[str] = None


@dataclass
class TruthAuditResult:
    passed: bool
    requires_review: bool
    violations: list[TruthViolation] = field(default_factory=list)
    blocking_errors: list[TruthViolation] = field(default_factory=list)
    warnings: list[TruthViolation] = field(default_factory=list)


_DATE_OR_NUMBER_RE = re.compile(
    r"(?P<date_range>"
    r"(?<!\d)(?:0?[1-9]|1[0-2])[./](?:19|20)\d{2}"
    r"\s*[-–—]\s*"
    r"(?:0?[1-9]|1[0-2])[./](?:19|20)\d{2}(?!\d)"
    r")"
    r"|(?P<iso_date>(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d))"
    r"|(?P<month_year>(?<!\d)(?:0?[1-9]|1[0-2])[./](?:19|20)\d{2}(?!\d))"
    r"|(?P<year_range>(?<!\d)(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}(?!\d))"
    r"|(?P<currency>"
    r"(?<![\w])(?:\d{1,3}(?:[\s\u00A0.]\d{3})+|\d+)(?:[.,]\d+)?"
    r"\s*(?:zł|pln|eur|usd|gbp|€|\$)(?!\w)"
    r")"
    r"|(?P<percent_range>"
    r"(?<!\d)\d+(?:[.,]\d+)?\s*%\s*[-–—]\s*"
    r"\d+(?:[.,]\d+)?\s*%(?!\d)"
    r")"
    r"|(?P<range>"
    r"(?<!\d)\d+(?:[.,]\d+)?\s*[-–—]\s*\d+(?:[.,]\d+)?(?!\d)"
    r")"
    r"|(?P<percentage>(?<!\d)\d+(?:[.,]\d+)?\s*%(?!\d))"
    r"|(?P<number>"
    r"(?<![\w])(?:\d{1,3}(?:[\s\u00A0]\d{3})+|\d+)(?:[.,]\d+)?\+?(?![\w%])"
    r")",
    flags=re.IGNORECASE,
)

_POLICY_WORDS = {
    "bez",
    "zatwierdzenia",
    "zatwierdzenie",
    "użytkownika",
    "uzytkownika",
    "nie",
    "ani",
    "lub",
    "oraz",
    "jako",
    "własna",
    "wlasna",
    "konkretne",
    "nazwy",
    "itp",
    "fikcyjne",
    "niezatwierdzone",
    "niezatwierdzonych",
    "przez",
}


def audit_resume_claims(
    claims: list[ResumeClaim],
    truth_index: TruthIndex,
    original_claims: Optional[list[ResumeClaim]] = None,
) -> TruthAuditResult:
    """Audit claims against the indexed Truth Library.

    Every claim receives one primary decision. PASS is possible only for an
    exact eligible Truth Library fact in the correct scope.
    """

    original_keys = {
        (claim.employment_id, normalize_truth_text(claim.company), normalize_truth_text(claim.text))
        for claim in (original_claims or [])
    }
    seen: set[tuple[str, str]] = set()
    violations: list[TruthViolation] = []

    for claim in claims:
        violation = _audit_single_claim(claim, truth_index, original_keys, seen)
        violations.append(violation)

    blocking_errors = [v for v in violations if v.decision == TruthDecision.BLOCK]
    warnings = [v for v in violations if v.decision == TruthDecision.REVIEW]
    return TruthAuditResult(
        passed=not blocking_errors,
        requires_review=bool(warnings),
        violations=violations,
        blocking_errors=blocking_errors,
        warnings=warnings,
    )


def _audit_single_claim(
    claim: ResumeClaim,
    truth_index: TruthIndex,
    original_keys: set[tuple[str, str, str]],
    seen: set[tuple[str, str]],
) -> TruthViolation:
    normalized_text = normalize_truth_text(claim.text)
    normalized_company = normalize_truth_text(claim.company)

    if not normalized_text:
        return _violation(claim, "TRUTH_EMPTY_CLAIM", TruthDecision.BLOCK, "Claim text is empty")

    scope: EmploymentTruthScope | None = None
    if claim.employment_id:
        scope = truth_index.employment_by_id.get(claim.employment_id)
        if scope is None:
            return _violation(
                claim,
                "TRUTH_UNKNOWN_EMPLOYMENT",
                TruthDecision.BLOCK,
                f"Unknown employment_id: {claim.employment_id}",
            )
        if normalized_company != normalize_truth_text(scope.company):
            return _violation(
                claim,
                "TRUTH_EMPLOYMENT_SCOPE_MISMATCH",
                TruthDecision.BLOCK,
                f"Claim company does not match employment scope {scope.company}",
            )

    duplicate_key = (claim.employment_id, normalized_text)
    if duplicate_key in seen:
        return _violation(
            claim,
            "TRUTH_DUPLICATE_CLAIM",
            TruthDecision.BLOCK,
            "Duplicate claim in the same employment scope",
        )
    seen.add(duplicate_key)

    supported_fact = _find_exact_supported_fact(claim, scope, truth_index)
    if supported_fact is not None:
        return _decision_for_fact(claim, supported_fact, truth_index)

    numeric_tokens = _extract_numeric_tokens(claim.text)
    if numeric_tokens:
        numeric_violation = _check_numeric_tokens(claim, scope, numeric_tokens, truth_index)
        if numeric_violation is not None:
            return numeric_violation

    blocked_rule = _find_matching_blocked_rule(claim.text, truth_index.blocked_rules)
    if blocked_rule is not None:
        return _violation(
            claim,
            "TRUTH_BLOCKED_RULE",
            TruthDecision.BLOCK,
            f"Claim matches blocked rule: {blocked_rule}",
        )

    original_key = (claim.employment_id, normalized_company, normalized_text)
    if original_key in original_keys:
        return _violation(
            claim,
            "TRUTH_ORIGINAL_CLAIM",
            TruthDecision.REVIEW,
            "Unchanged original claim still requires Truth Library verification",
        )

    if numeric_tokens:
        return _violation(
            claim,
            "TRUTH_NUMBER_CONTEXT_REVIEW",
            TruthDecision.REVIEW,
            "Numbers are known in this scope, but the surrounding claim text is not an exact approved fact",
            detected_values=[token.raw for token in numeric_tokens],
        )

    return _violation(
        claim,
        "TRUTH_UNGROUNDED_TEXT",
        TruthDecision.REVIEW,
        "Claim lacks explicit source verification in the Truth Library",
    )


def _decision_for_fact(
    claim: ResumeClaim, fact: TruthFact, truth_index: TruthIndex
) -> TruthViolation:
    evidence = [fact.source_reference]
    blocked_by_status = fact.status in truth_index.blocked_statuses
    if blocked_by_status or not fact.use_in_cv:
        return _violation(
            claim,
            "TRUTH_FACT_NOT_ALLOWED",
            TruthDecision.BLOCK,
            "Matching fact is not allowed for CV use",
            source_path=fact.source_reference,
            evidence_references=evidence,
        )

    requires_review = (
        fact.requires_approval
        or fact.status in truth_index.approval_statuses
        or (
            bool(truth_index.auto_cv_statuses)
            and fact.status not in truth_index.auto_cv_statuses
        )
    )
    if requires_review:
        return _violation(
            claim,
            "TRUTH_FACT_REQUIRES_APPROVAL",
            TruthDecision.REVIEW,
            "Matching Truth Library fact requires explicit approval",
            source_path=fact.source_reference,
            evidence_references=evidence,
        )

    return _violation(
        claim,
        "TRUTH_SUPPORTED_FACT",
        TruthDecision.PASS,
        "Fact verified against the Truth Library",
        source_path=fact.source_reference,
        evidence_references=evidence,
    )


def _find_exact_supported_fact(
    claim: ResumeClaim,
    scope: EmploymentTruthScope | None,
    truth_index: TruthIndex,
) -> TruthFact | None:
    normalized_claim = normalize_truth_text(claim.text)
    facts = scope.allowed_facts if scope is not None else truth_index.globally_allowed_facts
    for fact in facts:
        if normalized_claim == fact.normalized_text:
            return fact
    return None


def _extract_numeric_tokens(text: str) -> list[NumericToken]:
    tokens: list[NumericToken] = []
    for match in _DATE_OR_NUMBER_RE.finditer(unicodedata.normalize("NFKC", text)):
        kind = match.lastgroup or ""
        raw = match.group(0).strip()
        if kind in {"date_range", "iso_date", "month_year", "year_range"}:
            continue
        canonical = _canonicalize_numeric(raw, kind)
        tokens.append(
            NumericToken(
                raw=raw,
                canonical=canonical,
                kind=kind,
                start=match.start(),
                end=match.end(),
            )
        )
    return tokens


def _extract_numbers(text: str) -> list[str]:
    """Backward-compatible public helper returning non-overlapping raw values."""

    return [token.raw for token in _extract_numeric_tokens(text)]


def _canonicalize_numeric(raw: str, kind: str) -> str:
    normalized = raw.lower().replace("\u00a0", " ").strip()
    normalized = normalized.replace("–", "-").replace("—", "-")

    if kind == "currency":
        currency = ""
        if re.search(r"(?:zł|pln)\s*$", normalized):
            currency = "PLN"
        elif re.search(r"(?:eur|€)\s*$", normalized):
            currency = "EUR"
        elif re.search(r"(?:usd|\$)\s*$", normalized):
            currency = "USD"
        elif re.search(r"gbp\s*$", normalized):
            currency = "GBP"
        number_text = re.sub(r"\s*(?:zł|pln|eur|usd|gbp|€|\$)\s*$", "", normalized)
        return f"currency:{currency}:{_canonical_decimal(number_text)}"

    if kind == "percent_range":
        parts = re.split(r"\s*-\s*", normalized)
        left = _canonical_decimal(parts[0].replace("%", ""))
        right = _canonical_decimal(parts[1].replace("%", ""))
        return f"percent_range:{left}:{right}"

    if kind == "range":
        parts = re.split(r"\s*-\s*", normalized)
        return f"range:{_canonical_decimal(parts[0])}:{_canonical_decimal(parts[1])}"

    if kind == "percentage":
        return f"percentage:{_canonical_decimal(normalized.replace('%', ''))}"

    return f"number:{_canonical_decimal(normalized.rstrip('+'))}{'+' if normalized.endswith('+') else ''}"


def _canonical_decimal(value: str) -> str:
    compact = value.replace(" ", "")
    if "." in compact and "," not in compact:
        # A dot may be a thousands separator in currency, unless followed by
        # one or two decimal digits.
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", compact):
            compact = compact.replace(".", "")
    compact = compact.replace(",", ".")
    try:
        decimal = Decimal(compact)
    except InvalidOperation:
        return compact
    normalized = format(decimal.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _eligible_numeric_facts(scope: EmploymentTruthScope, truth_index: TruthIndex) -> list[TruthFact]:
    return [
        fact
        for fact in scope.allowed_facts
        if fact.use_in_cv and fact.status not in truth_index.blocked_statuses
    ]


def _numeric_canonicals(facts: list[TruthFact]) -> set[str]:
    values: set[str] = set()
    for fact in facts:
        values.update(token.canonical for token in _extract_numeric_tokens(fact.original_text))
    return values


def _check_numeric_tokens(
    claim: ResumeClaim,
    scope: EmploymentTruthScope | None,
    tokens: list[NumericToken],
    truth_index: TruthIndex,
) -> TruthViolation | None:
    if scope is None:
        same_scope_values = _numeric_canonicals(truth_index.globally_allowed_facts)
    else:
        same_scope_values = _numeric_canonicals(_eligible_numeric_facts(scope, truth_index))

    for token in tokens:
        if token.canonical in same_scope_values:
            continue

        if scope is not None:
            for other_id, other_scope in truth_index.employment_by_id.items():
                if other_id == claim.employment_id:
                    continue
                other_values = _numeric_canonicals(
                    _eligible_numeric_facts(other_scope, truth_index)
                )
                if token.canonical in other_values:
                    return _violation(
                        claim,
                        "TRUTH_CROSS_EMPLOYMENT_NUMBER",
                        TruthDecision.BLOCK,
                        f"Numeric value '{token.raw}' belongs to {other_scope.company}, not {claim.company}",
                        detected_values=[token.raw],
                    )

        return _violation(
            claim,
            "TRUTH_UNSUPPORTED_NUMBER",
            TruthDecision.BLOCK,
            f"Numeric value '{token.raw}' is not approved in this scope",
            detected_values=[token.raw],
        )

    return None


def _find_matching_blocked_rule(text: str, blocked_rules: list[str]) -> str | None:
    for rule in blocked_rules:
        if _blocked_rule_matches(text, rule):
            return rule
    return None


def _blocked_rule_matches(text: str, rule: str) -> bool:
    claim_normalized = normalize_truth_text(text)
    core = re.sub(r"\([^)]*\)", " ", normalize_truth_text(rule))
    core = " ".join(core.split()).strip(" -:;")
    if core and core in claim_normalized:
        return True

    # Lists after a colon name specific forbidden tools. Any listed item is a
    # deterministic match, without fuzzy or semantic inference.
    if ":" in rule:
        listed = rule.split(":", 1)[1]
        for item in re.split(r"[,;/]", listed):
            candidate = normalize_truth_text(item.replace("itp.", ""))
            if len(candidate) >= 3 and candidate in claim_normalized:
                return True

    rule_roots = _significant_roots(core)
    claim_roots = _significant_roots(claim_normalized)
    return bool(rule_roots) and rule_roots.issubset(claim_roots)


def _significant_roots(text: str) -> set[str]:
    roots: set[str] = set()
    for token in re.findall(r"[a-ząćęłńóśźż0-9&+]+", text.lower()):
        if token in _POLICY_WORDS or len(token) < 4:
            continue
        roots.add(token[:5])
    return roots


def _violation(
    claim: ResumeClaim,
    code: str,
    decision: TruthDecision,
    message: str,
    *,
    source_path: str | None = None,
    evidence_references: list[str] | None = None,
    detected_values: list[str] | None = None,
) -> TruthViolation:
    return TruthViolation(
        code=code,
        decision=decision,
        message=message,
        claim_text=claim.text,
        employment_id=claim.employment_id or None,
        company=claim.company or None,
        source_path=source_path if source_path is not None else claim.source_path,
        evidence_references=list(evidence_references or []),
        detected_values=list(detected_values or []),
    )

# Backward-compatible private helpers retained for the existing unit-test and
# service import surface. New code should call audit_resume_claims().
def _check_numeric_violation(
    claim: ResumeClaim,
    numbers: list[str],
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    del numbers
    scope = truth_index.employment_by_id.get(claim.employment_id) if claim.employment_id else None
    return _check_numeric_tokens(claim, scope, _extract_numeric_tokens(claim.text), truth_index)


def _check_blocked_rule(
    claim: ResumeClaim,
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    rule = _find_matching_blocked_rule(claim.text, truth_index.blocked_rules)
    if rule is None:
        return None
    return _violation(
        claim,
        "TRUTH_BLOCKED_RULE",
        TruthDecision.BLOCK,
        f"Claim matches blocked rule: {rule}",
    )


def _check_supported_fact(
    claim: ResumeClaim,
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    scope = truth_index.employment_by_id.get(claim.employment_id) if claim.employment_id else None
    fact = _find_exact_supported_fact(claim, scope, truth_index)
    if fact is None:
        return None
    return _decision_for_fact(claim, fact, truth_index)
