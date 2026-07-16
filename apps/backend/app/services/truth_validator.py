"""Truth Validator service.

Deterministic audit of resume claims against the Truth Library.
Phase 2B1A: Core models and numeric value extraction.

Provides:
- TruthDecision: Audit decision enum (PASS, REVIEW, BLOCK)
- TruthViolation: Details of a single violation
- TruthAuditResult: Complete audit result
- ResumeClaim: A claim made in the resume
- extract_numeric_values(): Extract numbers from text
- audit_resume_claims(): Main validation function (Phase 2B1B, not yet implemented)
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.truth_index import TruthIndex


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


def extract_numeric_values(text: str) -> list[str]:
    """Extract numeric values from text deterministically.

    Detects:
    - Integers (e.g., 100, 5)
    - Decimals with dot (e.g., 1.5, 99.9)
    - Decimals with comma (e.g., 1,5)
    - Percentages (e.g., 50%, 116%)
    - Ranges with hyphen (e.g., 10-15, 10 - 15)
    - Ranges with en-dash (e.g., 10–15, 10 – 15)
    - Numbers with spaces (e.g., 85 agencies, 1 000 000)
    - Amounts/quantities

    Preserves percentages and ranges as single values.

    Args:
        text: Input text to extract numbers from

    Returns:
        List of detected numeric patterns, ordered by appearance
    """
    patterns = [
        r"\d+%",  # Percentages (e.g., 116%, 50%)
        r"\d+\s*–\s*\d+",  # En-dash ranges (e.g., 10–15, 10 – 15)
        r"\d+\s*-\s*\d+",  # Hyphen ranges (e.g., 10-15, 10 - 15)
        r"\d+\s*,\s*\d+",  # Comma-separated decimals (e.g., 1,5)
        r"\d+\.\d+",  # Dot-separated decimals (e.g., 1.5, 99.99)
        r"\d+\s+\d+",  # Spaced numbers (e.g., 85 agencies, 1 000 000)
        r"\d+",  # Plain integers (e.g., 5, 100, 2024)
    ]

    found = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        found.extend(matches)

    return found


def audit_resume_claims(
    claims: list[ResumeClaim],
    truth_index: TruthIndex,
    original_claims: Optional[list[ResumeClaim]] = None,
) -> TruthAuditResult:
    """Audit resume claims against the Truth Library.

    This is Phase 2B1B - full implementation placeholder.

    Args:
        claims: List of claims to audit
        truth_index: Indexed Truth Library for validation
        original_claims: Original claims before modification (for unchanged detection)

    Returns:
        TruthAuditResult with violations and final decision

    Raises:
        NotImplementedError: Phase 2B1B implementation not yet complete
    """
    raise NotImplementedError(
        "Truth Validator Phase 2B1B is not implemented. "
        "Phase 2B1A (models and numeric extraction) is complete."
    )
