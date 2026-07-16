"""Truth Validator service.

Deterministic audit of resume claims against the Truth Library.
Enforces strict scoping rules and detects violations including unsupported numbers,
cross-employment claims, duplicates, and blocked facts.

Provides:
- TruthDecision: Audit decision enum (PASS, REVIEW, BLOCK)
- TruthViolation: Details of a single violation
- TruthAuditResult: Complete audit result
- ResumeClaim: A claim made in the resume
- audit_resume_claims(): Main validation function
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.truth_index import (
    TruthIndex,
    EmploymentTruthScope,
    normalize_truth_text,
)


class TruthDecision(str, Enum):
    """Audit decision for a claim."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


@dataclass
class TruthViolation:
    """A single violation detected in a resume claim.

    Attributes:
        code: Violation code (e.g., TRUTH_UNSUPPORTED_NUMBER)
        decision: TruthDecision (PASS, REVIEW, BLOCK)
        message: Human-readable description
        claim_text: The original claim text
        employment_id: Employment scope where claim was made
        company: Company name
        source_path: Path to fact in Truth Library (if applicable)
        evidence_references: List of matching fact references from Truth Library
        detected_values: List of detected numbers/values that triggered the violation
    """

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
    """A claim made in the resume."""

    claim_id: str
    section: str  # e.g., "activities", "numeric_result"
    employment_id: str
    company: str
    role: str
    text: str
    source_path: Optional[str] = None


@dataclass
class TruthAuditResult:
    """Complete audit result for resume claims.

    Attributes:
        passed: True if no blocking violations found
        requires_review: True if any REVIEW decisions exist
        violations: All detected violations
        blocking_errors: Violations with decision == BLOCK
        warnings: Violations with decision == REVIEW
    """

    passed: bool
    requires_review: bool
    violations: list[TruthViolation] = field(default_factory=list)
    blocking_errors: list[TruthViolation] = field(default_factory=list)
    warnings: list[TruthViolation] = field(default_factory=list)


def audit_resume_claims(
    claims: list[ResumeClaim],
    truth_index: TruthIndex,
    original_claims: Optional[list[ResumeClaim]] = None,
) -> TruthAuditResult:
    """Audit resume claims against the Truth Library.

    Enforces strict employment scoping, detects unsupported numbers,
    cross-employment anomalies, duplicates, and blocked rules.

    Args:
        claims: List of claims to audit
        truth_index: Indexed Truth Library for validation
        original_claims: Original claims before modification (for unchanged detection)

    Returns:
        TruthAuditResult with violations and final decision
    """
    result = TruthAuditResult(passed=True, requires_review=False)

    # Build original claims map for unchanged detection
    original_map = {}
    if original_claims:
        for claim in original_claims:
            key = (claim.employment_id, normalize_truth_text(claim.text))
            original_map[key] = claim

    # Track seen normalized texts per employment to detect duplicates
    seen_per_employment = {}

    for claim in claims:
        claim_violations = []

        # Rule A: Unchanged facts from original_claims
        claim_key = (claim.employment_id, normalize_truth_text(claim.text))
        if claim_key in original_map:
            violation = TruthViolation(
                code="TRUTH_ORIGINAL_CLAIM",
                decision=TruthDecision.PASS,
                message="Unchanged fact from original resume",
                claim_text=claim.text,
                employment_id=claim.employment_id,
                company=claim.company,
                source_path=claim.source_path,
            )
            claim_violations.append(violation)
        else:
            # Rule D: Check for duplicates within same employment
            normalized_text = normalize_truth_text(claim.text)
            employment_key = claim.employment_id

            if employment_key not in seen_per_employment:
                seen_per_employment[employment_key] = []

            # Check if this normalized text was already seen in this employment
            if normalized_text in seen_per_employment[employment_key]:
                violation = TruthViolation(
                    code="TRUTH_DUPLICATE_CLAIM",
                    decision=TruthDecision.BLOCK,
                    message=f"Duplicate claim in {claim.company}: identical text already present",
                    claim_text=claim.text,
                    employment_id=claim.employment_id,
                    company=claim.company,
                )
                claim_violations.append(violation)
            else:
                seen_per_employment[employment_key].append(normalized_text)

                # Rule C: Detect new numbers
                numbers = _extract_numbers(claim.text)
                numeric_violation = _check_numeric_violation(
                    claim, numbers, truth_index
                )
                if numeric_violation:
                    claim_violations.append(numeric_violation)
                else:
                    # Rule E: Check blocked rules
                    blocked_violation = _check_blocked_rule(claim, truth_index)
                    if blocked_violation:
                        claim_violations.append(blocked_violation)
                    else:
                        # Rule B: Check exact match in Truth Library
                        supported_violation = _check_supported_fact(
                            claim, truth_index
                        )
                        if supported_violation:
                            claim_violations.append(supported_violation)
                        else:
                            # Rule F: Ungrounded text
                            if not numbers:  # Has no numbers
                                violation = TruthViolation(
                                    code="TRUTH_UNGROUNDED_TEXT",
                                    decision=TruthDecision.REVIEW,
                                    message="Claim lacks explicit source verification in Truth Library",
                                    claim_text=claim.text,
                                    employment_id=claim.employment_id,
                                    company=claim.company,
                                )
                                claim_violations.append(violation)

        # Add violations to result using the first (highest priority) violation
        # for final decision, or collect all if needed
        for violation in claim_violations:
            result.violations.append(violation)

            if violation.decision == TruthDecision.BLOCK:
                result.blocking_errors.append(violation)
            elif violation.decision == TruthDecision.REVIEW:
                result.warnings.append(violation)
                result.requires_review = True

    # Rule 6: Final decision
    result.passed = len(result.blocking_errors) == 0

    return result


def _extract_numbers(text: str) -> list[str]:
    """Extract all numbers from claim text.

    Detects:
    - Integers
    - Decimals
    - Percentages
    - Ranges (10–15, 10-15)
    - Quantities with units

    Returns:
        List of detected numeric patterns
    """
    # Patterns: percentages, ranges, decimals, integers, spaced numbers
    patterns = [
        r"\d+%",  # Percentages (e.g., 116%)
        r"\d+\s*–\s*\d+",  # En-dash ranges (e.g., 10–15)
        r"\d+\s*-\s*\d+",  # Hyphen ranges (e.g., 10-15)
        r"\d+\s*,\s*\d+",  # Comma decimals (e.g., 1,5)
        r"\d+\.\d+",  # Dot decimals (e.g., 1.5)
        r"\d+\s+\d+",  # Spaced numbers (e.g., 85 agencji)
        r"\d+",  # Plain integers
    ]

    found = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        found.extend(matches)

    return found


def _check_numeric_violation(
    claim: ResumeClaim,
    numbers: list[str],
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    """Check if claim contains unsupported or cross-employment numbers.

    Returns:
        TruthViolation if violation found, None otherwise
    """
    if not numbers:
        return None

    # Get employment scope from truth_index
    scope = truth_index.employment_by_id.get(claim.employment_id)
    if not scope:
        return None

    # Normalize numeric facts from this scope
    scope_numbers = set()
    for fact in scope.numeric_results:
        extracted = _extract_numbers(fact.original_text)
        scope_numbers.update(extracted)

    normalized_claim_text = normalize_truth_text(claim.text)

    # Check each number in claim
    for number in numbers:
        if number not in scope_numbers:
            # Number not in this employment's facts
            # Check if it exists in other employments
            for other_id, other_scope in truth_index.employment_by_id.items():
                if other_id != claim.employment_id:
                    for other_fact in other_scope.numeric_results:
                        other_extracted = _extract_numbers(other_fact.original_text)
                        if number in other_extracted:
                            return TruthViolation(
                                code="TRUTH_CROSS_EMPLOYMENT_NUMBER",
                                decision=TruthDecision.BLOCK,
                                message=f"Number '{number}' belongs to {other_scope.company}, not {claim.company}",
                                claim_text=claim.text,
                                employment_id=claim.employment_id,
                                company=claim.company,
                                detected_values=[number],
                            )

            # Number not found in any employment's numeric results
            return TruthViolation(
                code="TRUTH_UNSUPPORTED_NUMBER",
                decision=TruthDecision.BLOCK,
                message=f"Number '{number}' not found in {claim.company} Truth Library facts",
                claim_text=claim.text,
                employment_id=claim.employment_id,
                company=claim.company,
                detected_values=[number],
            )

    return None


def _check_blocked_rule(
    claim: ResumeClaim,
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    """Check if claim matches a blocked rule.

    Exceptions in truth_index.exceptions must be honored.

    Returns:
        TruthViolation if blocked, None otherwise
    """
    normalized_claim = normalize_truth_text(claim.text)

    for blocked_rule in truth_index.blocked_rules:
        normalized_rule = normalize_truth_text(blocked_rule)

        # Check if claim contains the blocked rule text
        if normalized_rule in normalized_claim:
            # Check for exceptions
            is_exception = False
            for exception in truth_index.exceptions:
                normalized_exception = normalize_truth_text(exception)
                # Exception applies to this employment if it mentions the company
                # or if it's a global exception
                if normalized_exception in normalized_claim:
                    is_exception = True
                    break

            if not is_exception:
                return TruthViolation(
                    code="TRUTH_BLOCKED_RULE",
                    decision=TruthDecision.BLOCK,
                    message=f"Claim matches blocked rule: {blocked_rule}",
                    claim_text=claim.text,
                    employment_id=claim.employment_id,
                    company=claim.company,
                )

    return None


def _check_supported_fact(
    claim: ResumeClaim,
    truth_index: TruthIndex,
) -> Optional[TruthViolation]:
    """Check if claim matches exactly a fact in Truth Library for same employment.

    Returns:
        TruthViolation with PASS decision if match found, None if no exact match
    """
    normalized_claim = normalize_truth_text(claim.text)

    # Get employment scope from truth_index
    scope = truth_index.employment_by_id.get(claim.employment_id)
    if not scope:
        return None

    # Check all facts in this employment scope
    for fact in scope.allowed_facts:
        if normalized_claim == fact.normalized_text:
            return TruthViolation(
                code="TRUTH_SUPPORTED_FACT",
                decision=TruthDecision.PASS,
                message="Fact verified against Truth Library",
                claim_text=claim.text,
                employment_id=claim.employment_id,
                company=claim.company,
                source_path=fact.source_reference,
                evidence_references=[fact.source_reference],
            )

    # Also check global facts (not employment-scoped)
    for fact in truth_index.globally_allowed_facts:
        if normalized_claim == fact.normalized_text:
            return TruthViolation(
                code="TRUTH_SUPPORTED_FACT",
                decision=TruthDecision.PASS,
                message="Fact verified against global Truth Library",
                claim_text=claim.text,
                employment_id=claim.employment_id,
                company=claim.company,
                source_path=fact.source_reference,
                evidence_references=[fact.source_reference],
            )

    return None
