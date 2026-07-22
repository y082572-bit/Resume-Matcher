"""Deterministic immutable-token extraction and proposal-vs-source validation.

Extraction never attempts full NLP or named-entity recognition: numbers,
dates, currencies, percentages, ranges, inequality signs, units directly
attached to a number, and a closed negation-word list are pulled with plain
regexes over normalized text. Company/role identity is never regex-mined
from free text -- it comes only from the structured payload fields already
validated by P5a's payload-shape contract. Any proper noun outside that
structured payload remains P5.5's semantic-validation responsibility.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.schemas.cv_content_proposal import (
    ImmutableConstraintSet,
    ImmutableConstraintType,
    ImmutableTokenConstraint,
    ProposalViolationCode,
)
from app.services.truth_fingerprint import canonical_json_bytes


IMMUTABLE_CONSTRAINT_POLICY_VERSION = "immutable-constraint-policy-v1"

_CONSTRAINT_FINGERPRINT_VERSION = "cv-content-proposal-immutable-constraint-v1"
_CONSTRAINT_SET_FINGERPRINT_VERSION = "cv-content-proposal-immutable-constraint-set-v1"

#: Constraint types compared against a fresh extraction over the proposal
#: text (source vs proposal, symmetric). Never compared via substring.
_TEXT_DERIVED_TYPES: tuple[ImmutableConstraintType, ...] = (
    ImmutableConstraintType.CURRENCY_AMOUNT,
    ImmutableConstraintType.PERCENTAGE,
    ImmutableConstraintType.NUMERIC_RANGE,
    ImmutableConstraintType.PERIOD,
    ImmutableConstraintType.DATE,
    ImmutableConstraintType.DECIMAL,
    ImmutableConstraintType.INTEGER,
    ImmutableConstraintType.INEQUALITY,
    ImmutableConstraintType.UNIT,
    ImmutableConstraintType.NEGATION,
)

#: Constraint types validated only by literal substring presence in the
#: proposal text -- always sourced from the structured payload, never
#: re-mined from free text.
_LITERAL_TYPES: tuple[ImmutableConstraintType, ...] = (
    ImmutableConstraintType.COMPANY_NAME,
    ImmutableConstraintType.ROLE_NAME,
)

#: Closed structured-field -> constraint-type map. Only ``EMPLOYMENT_ROLE``
#: carries both a company and a role name in one payload contract (see
#: ``cv_content_generation_policy._STRUCTURED_FIELD_CONTRACTS``).
_STRUCTURED_IDENTITY_FIELDS: dict[str, tuple[str, ImmutableConstraintType]] = {
    "firma": ("firma", ImmutableConstraintType.COMPANY_NAME),
    "stanowisko": ("stanowisko", ImmutableConstraintType.ROLE_NAME),
}

_PERIOD_RE = re.compile(
    r"\b(\d{4}(?:-\d{2}(?:-\d{2})?)?|\d{2}\.\d{4})\s?[-–—]\s?"
    r"(\d{4}(?:-\d{2}(?:-\d{2})?)?|\d{2}\.\d{4}|obecnie|present)\b",
    re.IGNORECASE,
)
_DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DATE_YM_RE = re.compile(r"\b\d{4}-\d{2}\b")
_DATE_DMY_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
_DATE_MY_RE = re.compile(r"\b\d{2}[./]\d{4}\b")
_CURRENCY_CODE_RE = re.compile(
    r"\b(?P<amount>\d[\d ,.]*\d|\d)\s?(?P<currency>PLN|USD|EUR|GBP|zł)\b", re.IGNORECASE
)
_CURRENCY_SYMBOL_RE = re.compile(r"(?P<currency>[$€£])\s?(?P<amount>\d[\d ,.]*\d|\d)")

#: Closed list of explicit amount multipliers. Order matters: the dotted
#: "tys." form must be tried before the bare "tys" so the dot is consumed
#: as part of the multiplier match rather than left dangling.
_CURRENCY_MULTIPLIERS: tuple[str, ...] = ("tys.", "tys", "mln", "mld")
_CURRENCY_MULTIPLIER_ALTERNATION = "|".join(re.escape(word) for word in _CURRENCY_MULTIPLIERS)
_CURRENCY_AMOUNT_MULTIPLIER_RE = re.compile(
    rf"\b(?P<amount>\d[\d ,.]*\d|\d)\s+(?P<multiplier>{_CURRENCY_MULTIPLIER_ALTERNATION})"
    rf"\s+(?P<currency>PLN|USD|EUR|GBP|zł)\b",
    re.IGNORECASE,
)
_PERCENTAGE_RE = re.compile(r"\b\d[\d.,]*\s?%")
_NUMERIC_RANGE_RE = re.compile(r"\b\d[\d.,]*\s?[-–—]\s?\d[\d.,]*\b")
_DECIMAL_RE = re.compile(r"\b\d+[.,]\d+\b")
_INTEGER_RE = re.compile(r"\b\d+\b")
_INEQUALITY_RE = re.compile(r">=|<=|≥|≤|>|<")

_UNIT_WORDS: frozenset[str] = frozenset(
    {
        "h",
        "godz",
        "godzin",
        "godziny",
        "lat",
        "rok",
        "lata",
        "miesiecy",
        "miesiace",
        "dni",
        "dzien",
        "osob",
        "klientow",
        "km",
        "kg",
        "mln",
        "tys",
        "hours",
        "hour",
        "years",
        "year",
        "months",
        "month",
        "days",
        "day",
        "people",
        "clients",
        "users",
    }
)
_UNIT_ATTACHED_RE = re.compile(r"\b(\d[\d.,]*)\s+([a-zA-Ząćęłńóśźż]+)\b")

_NEGATION_RE = re.compile(
    r"\b(nie|brak|bez|żaden|żadna|żadne|nigdy|not|never|without|none|no)\b",
    re.IGNORECASE,
)

#: Deasciify-free normalization: NFKC only, matching P5a's own text
#: normalization contract -- never a meaning-changing edit.
def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _strip_currency_word_chars(word: str) -> str:
    return word.lower().replace("ł", "l")


def _overlaps(span: tuple[int, int], consumed: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    return any(not (end <= s or start >= e) for s, e in consumed)


def _clean_amount(raw: str) -> str:
    return re.sub(r"[ ,]", "", raw)


def _canonical_decimal_string(raw: str) -> str:
    """Canonicalize a digit string with an optional decimal separator.

    A single comma or a single dot between digits is the decimal
    separator; the canonical output always uses a dot. The comma is never
    treated as a thousands separator here, so ``12,99`` and ``12.99``
    canonicalize to the same value while ``1,299`` does not. Parsing goes
    through ``Decimal`` for exact digit-preserving arithmetic -- never
    ``float``, which could silently perturb the compared digits.
    """

    text = re.sub(r"\s", "", raw)
    comma_count = text.count(",")
    dot_count = text.count(".")
    if comma_count == 1 and dot_count == 0:
        text = text.replace(",", ".")
    elif comma_count >= 1 and dot_count >= 1:
        decimal_index = max(text.rfind(","), text.rfind("."))
        integer_part = re.sub(r"[.,]", "", text[:decimal_index])
        fraction_part = text[decimal_index + 1 :]
        text = f"{integer_part}.{fraction_part}"
    try:
        value = Decimal(text)
    except InvalidOperation:
        return raw
    return format(value, "f")


def extract_text_constraints(text: str) -> tuple[ImmutableTokenConstraint, ...]:
    """Deterministically extract every text-derived immutable token.

    Matches are consumed left-to-right in a fixed priority order so a
    single numeric span is never double-counted (e.g. a percentage is
    never also reported as a bare integer).
    """

    normalized = _normalize(text)
    consumed: list[tuple[int, int]] = []
    found: list[ImmutableTokenConstraint] = []

    for match in _PERIOD_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        raw = match.group(0)
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.PERIOD,
                raw_text=raw,
                normalized_value=f"{match.group(1).lower()}~{match.group(2).lower()}",
            )
        )

    for date_re in (_DATE_ISO_RE, _DATE_DMY_RE, _DATE_MY_RE, _DATE_YM_RE):
        for match in date_re.finditer(normalized):
            if _overlaps(match.span(), consumed):
                continue
            consumed.append(match.span())
            found.append(
                ImmutableTokenConstraint(
                    constraint_type=ImmutableConstraintType.DATE,
                    raw_text=match.group(0),
                    normalized_value=match.group(0),
                )
            )

    for match in _CURRENCY_AMOUNT_MULTIPLIER_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        amount = _clean_amount(match.group("amount"))
        multiplier = match.group("multiplier").lower().rstrip(".")
        currency = _strip_currency_word_chars(match.group("currency"))
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.CURRENCY_AMOUNT,
                raw_text=match.group(0),
                normalized_value=f"{amount}|{multiplier}|{currency}",
            )
        )

    for match in _CURRENCY_CODE_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        amount = _clean_amount(match.group("amount"))
        currency = _strip_currency_word_chars(match.group("currency"))
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.CURRENCY_AMOUNT,
                raw_text=match.group(0),
                normalized_value=f"{amount}{currency}",
            )
        )
    for match in _CURRENCY_SYMBOL_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        amount = _clean_amount(match.group("amount"))
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.CURRENCY_AMOUNT,
                raw_text=match.group(0),
                normalized_value=f"{amount}{match.group('currency')}",
            )
        )

    for match in _PERCENTAGE_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        digits = _canonical_decimal_string(match.group(0).rstrip("%").strip())
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.PERCENTAGE,
                raw_text=match.group(0),
                normalized_value=f"{digits}%",
            )
        )

    for match in _NUMERIC_RANGE_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.NUMERIC_RANGE,
                raw_text=match.group(0),
                normalized_value=re.sub(r"\s", "", match.group(0)),
            )
        )

    for match in _DECIMAL_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.DECIMAL,
                raw_text=match.group(0),
                normalized_value=_canonical_decimal_string(match.group(0)),
            )
        )

    for match in _INTEGER_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.INTEGER,
                raw_text=match.group(0),
                normalized_value=str(int(match.group(0))),
            )
        )

    for match in _UNIT_ATTACHED_RE.finditer(normalized):
        word = match.group(2).lower()
        if word not in _UNIT_WORDS:
            continue
        span = match.span(2)
        if _overlaps(span, consumed):
            continue
        consumed.append(span)
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.UNIT,
                raw_text=match.group(2),
                normalized_value=word,
            )
        )

    for match in _INEQUALITY_RE.finditer(normalized):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.INEQUALITY,
                raw_text=match.group(0),
                normalized_value=match.group(0),
            )
        )

    if _NEGATION_RE.search(normalized) is not None:
        first = _NEGATION_RE.search(normalized)
        assert first is not None
        found.append(
            ImmutableTokenConstraint(
                constraint_type=ImmutableConstraintType.NEGATION,
                raw_text=first.group(0),
                normalized_value="negation_present",
            )
        )

    return tuple(found)


def extract_structured_identity_constraints(
    fact_type: str, structured_fields: Mapping[str, str] | None
) -> tuple[ImmutableTokenConstraint, ...]:
    """Company/role identity constraints sourced only from a structured
    payload field already validated by P5a's payload-shape contract."""

    if not structured_fields:
        return ()
    found: list[ImmutableTokenConstraint] = []
    for field_key, (source_key, constraint_type) in _STRUCTURED_IDENTITY_FIELDS.items():
        value = structured_fields.get(source_key)
        if value is None or not value.strip():
            continue
        found.append(
            ImmutableTokenConstraint(
                constraint_type=constraint_type,
                raw_text=value,
                normalized_value=_normalize(value),
            )
        )
    return tuple(found)


def build_immutable_constraint_set(
    *,
    fact_id: UUID,
    fact_type: str,
    source_text: str,
    structured_fields: Mapping[str, str] | None = None,
) -> ImmutableConstraintSet:
    constraints = tuple(
        sorted(
            (
                *extract_text_constraints(source_text),
                *extract_structured_identity_constraints(fact_type, structured_fields),
            ),
            key=lambda c: (c.constraint_type.value, c.normalized_value, c.raw_text),
        )
    )
    return ImmutableConstraintSet(
        fact_id=fact_id,
        constraints=constraints,
        immutable_constraint_set_fingerprint=compute_immutable_constraint_set_fingerprint(
            fact_id=fact_id, constraints=constraints
        ),
    )


def compute_immutable_constraint_fingerprint(constraint: ImmutableTokenConstraint) -> str:
    content = {
        "fingerprint_version": _CONSTRAINT_FINGERPRINT_VERSION,
        "content": {
            "constraint_type": constraint.constraint_type.value,
            "raw_text": constraint.raw_text,
            "normalized_value": constraint.normalized_value,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def compute_immutable_constraint_set_fingerprint(
    *, fact_id: UUID, constraints: Sequence[ImmutableTokenConstraint]
) -> str:
    content = {
        "fingerprint_version": _CONSTRAINT_SET_FINGERPRINT_VERSION,
        "content": {
            "fact_id": str(fact_id),
            "constraint_fingerprints": sorted(
                compute_immutable_constraint_fingerprint(c) for c in constraints
            ),
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def _values_by_type(
    constraints: Sequence[ImmutableTokenConstraint], constraint_type: ImmutableConstraintType
) -> list[str]:
    return sorted(c.normalized_value for c in constraints if c.constraint_type == constraint_type)


def check_immutable_constraints(
    source: ImmutableConstraintSet, proposal_text: str
) -> tuple[ProposalViolationCode, str] | None:
    """Compare ``source`` against a fresh extraction over ``proposal_text``.

    Returns the first violated constraint as ``(violation_code, detail)``,
    or ``None`` when every constraint type is preserved. Never mutates or
    auto-repairs a proposal.
    """

    proposal_text_constraints = extract_text_constraints(proposal_text)

    for constraint_type in _LITERAL_TYPES:
        for constraint in source.constraints:
            if constraint.constraint_type != constraint_type:
                continue
            if constraint.raw_text not in proposal_text:
                return (
                    ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED,
                    f"{constraint_type.value} literal not found in proposal text: "
                    f"{constraint.raw_text!r}",
                )

    for constraint_type in _TEXT_DERIVED_TYPES:
        source_values = _values_by_type(source.constraints, constraint_type)
        proposal_values = _values_by_type(proposal_text_constraints, constraint_type)
        if source_values == proposal_values:
            continue
        missing = [v for v in source_values if v not in proposal_values]
        added = [v for v in proposal_values if v not in source_values]
        if missing and not added:
            return (
                ProposalViolationCode.IMMUTABLE_TOKEN_MISSING,
                f"{constraint_type.value} constraint missing from proposal: {missing}",
            )
        if added and not missing:
            return (
                ProposalViolationCode.IMMUTABLE_TOKEN_ADDED,
                f"{constraint_type.value} constraint added in proposal: {added}",
            )
        return (
            ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED,
            f"{constraint_type.value} constraint changed: source={source_values} "
            f"proposal={proposal_values}",
        )

    return None
