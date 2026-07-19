"""Pure, deterministic claim-boundary classification shared by planning and generation."""

from __future__ import annotations

import re


NUMBER_PATTERN = re.compile(
    r"(?<!\w)(?:[$€£]\s*)?\d+(?:[.,]\d+)?(?:\s*(?:%|pln|zł|eur|usd|gbp))?",
    re.IGNORECASE,
)

PROHIBITED_CLAIM_BOUNDARIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PNL_WITHOUT_EVIDENCE", re.compile(r"\b(?:p\s*&\s*l|pnl|profit\s+and\s+loss|odpowiedzialno\w*\s+p\s*&\s*l)\b", re.I)),
    ("BUDGET_WITHOUT_EVIDENCE", re.compile(r"\b(?:budżet\w*|budzet\w*|budget\w*)\b", re.I)),
    ("BOARD_WITHOUT_EVIDENCE", re.compile(r"\b(?:board|c[- ]?level|zarząd(?:u|em|owi|zie)?|zarzad(?:u|em|owi|zie)?)\b", re.I)),
    ("MANAGING_MANAGERS_WITHOUT_EVIDENCE", re.compile(r"\b(?:zarządz\w+\s+menedżer\w*|zarzadz\w+\s+menedzer\w*|manag\w+\s+managers?)\b", re.I)),
    ("TEAM_SIZE_WITHOUT_EVIDENCE", re.compile(r"\b(?:zesp[oó]ł|team)\s+(?:of\s+)?\d+\b|\b\d+[- ]osobow\w*\s+zesp[oó]ł\b|\b\d+\s*(?:osób|osob|pracowników|pracownikow|podwładnych|podwladnych|people|reports)\b", re.I)),
    ("PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE", re.compile(r"\b(?:people\s+management|people\s+manager|zarządz\w+\s+(?:ludź\w*|zespoł\w*|pracownik\w*|podwładn\w*)|zarzadz\w+\s+(?:ludz\w*|zespol\w*|pracownik\w*|podwladn\w*)|kierowa\w+\s+zespoł\w*)\b", re.I)),
    ("CERTIFICATION_WITHOUT_EVIDENCE", re.compile(r"\b(?:certyfikat\w*|certyfikac\w*|certified|certification)\b", re.I)),
    ("LANGUAGE_LEVEL_WITHOUT_EVIDENCE", re.compile(r"\b(?:angielsk\w*|english|niemieck\w*|german|francusk\w*|french)\s+(?:na\s+poziomie\s+)?(?:a1|a2|b1|b2|c1|c2|native|fluent|zaawansowan\w*)\b", re.I)),
    ("TECHNICAL_SKILL_WITHOUT_EVIDENCE", re.compile(r"\b(?:python|java|typescript|javascript|react|kubernetes|aws|azure|sql)\b", re.I)),
    ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE", NUMBER_PATTERN),
)

_STRATEGY_PATTERN = re.compile(
    r"\b(?:odpowiedzialno(?:ść|sc)\s+za\s+)?strategi(?:a|ę|e|ą|i)|strategic\s+responsibility\b",
    re.I,
)
_ORGANIZATIONAL_SCALE_PATTERN = re.compile(
    r"\b\d+\s*(?:jednostk(?:a|i|ami)|oddział(?:y|ami)|oddzial(?:y|ami)|region(?:y|ami)|countries|units)\b",
    re.I,
)


def detect_claim_codes(text: str) -> tuple[str, ...]:
    """Return prohibited claim categories present in one exact text boundary."""

    return tuple(
        code for code, pattern in PROHIBITED_CLAIM_BOUNDARIES if pattern.search(text)
    )


def detect_claim_values(text: str, claim_code: str) -> frozenset[str]:
    """Return normalized exact regex matches for one claim category."""

    pattern = next(
        (value for code, value in PROHIBITED_CLAIM_BOUNDARIES if code == claim_code),
        None,
    )
    if pattern is None:
        return frozenset()
    return frozenset(" ".join(match.group(0).casefold().split()) for match in pattern.finditer(text))


def classify_approved_fact(text: str, fact_type: str) -> tuple[str, ...]:
    """Derive permissions only from one exact, approved, employment-scoped fact."""

    codes = set(detect_claim_codes(text))
    if fact_type == "numeric_result":
        codes.add("QUANTIFIED_RESULT_WITHOUT_EVIDENCE")
    if _STRATEGY_PATTERN.search(text):
        codes.add("STRATEGY_RESPONSIBILITY_EVIDENCE")
    if fact_type == "responsibility_scale" and _ORGANIZATIONAL_SCALE_PATTERN.search(text):
        codes.add("ORGANIZATIONAL_SCALE_EVIDENCE")
    return tuple(sorted(codes))
