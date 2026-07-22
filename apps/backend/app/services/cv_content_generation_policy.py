"""Closed, versioned P5a policy tables: supported operations, payload contracts.

Every table here is a plain, explicit dict, tuple or frozenset -- no
``startswith``, no substring matching, no text classification, and no
fallback fact_type or default shape. A fact_type outside the closed
contract tables is unsupported by definition and fails closed to
``GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.schemas.cv_content_generation import GenerationViolationCode
from app.schemas.truth_fact import TransformationOperation


CV_CONTENT_GENERATION_POLICY_VERSION = "cv-content-generation-policy-v1"

#: Deterministic renderer template identity used when a caller does not
#: override ``GenerationContext.deterministic_template_version``.
DEFAULT_DETERMINISTIC_TEMPLATE_VERSION = "cv-content-deterministic-template-v1"

#: The closed set of ``TransformationOperation`` values P5a can render.
#: Never derived from a permission, a transferability tier, or free text.
SUPPORTED_OPERATIONS: frozenset[TransformationOperation] = frozenset(
    {
        TransformationOperation.EXACT_COPY,
        TransformationOperation.FORMAT_NORMALIZATION,
        TransformationOperation.REORDER,
    }
)

#: Every unsupported operation's closed, versioned violation code. Absence
#: from this dict for an operation not in ``SUPPORTED_OPERATIONS`` is a
#: programming error, never silently ignored -- see ``classify_operation``.
_UNSUPPORTED_OPERATION_VIOLATIONS: dict[TransformationOperation, GenerationViolationCode] = {
    TransformationOperation.CONTROLLED_REPHRASE: GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A,
    TransformationOperation.FLATTEN_FOR_LOWER_ROLE: GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A,
    TransformationOperation.ELEVATE_PRESENTATION_FOR_SENIOR_ROLE: (
        GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A
    ),
    TransformationOperation.COMBINE_APPROVED_FACTS: GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A,
    TransformationOperation.SPLIT_APPROVED_FACT: GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A,
    TransformationOperation.SUMMARIZE_APPROVED_FACTS: GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A,
    TransformationOperation.OMIT: GenerationViolationCode.OMIT_NOT_GENERATABLE,
}


def classify_operation(operation: TransformationOperation) -> GenerationViolationCode | None:
    """Return ``None`` when P5a can render ``operation``, else its violation code."""

    if operation in SUPPORTED_OPERATIONS:
        return None
    return _UNSUPPORTED_OPERATION_VIOLATIONS[operation]


#: fact_types whose payload contract is a narrative scalar/text value:
#: either a non-blank string, or an object with a non-blank string "text" key.
NARRATIVE_TEXT_FACT_TYPES: frozenset[str] = frozenset(
    {
        "SUMMARY",
        "SKILL",
        "TOOL",
        "TECHNOLOGY",
        "COMPANY",
        "ROLE",
        "EMPLOYMENT_PERIOD",
        "RESPONSIBILITY",
        "EMPLOYMENT_ACTIVITY",
        "EMPLOYMENT_RESPONSIBILITY_SCALE",
        "ACHIEVEMENT",
        "EMPLOYMENT_NUMERIC_RESULT",
    }
)

#: fact_types whose payload contract is a closed structured object with
#: required/optional string keys. Keys outside ``required | optional`` are
#: tolerated (they may still be part of the source payload fingerprint) but
#: are never read, rendered, or used to order content.
_STRUCTURED_FIELD_CONTRACTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "EMPLOYMENT_ROLE": (
        frozenset({"firma", "stanowisko"}),
        frozenset({"stanowiskoAlt", "okresOd", "okresDo", "legacy_source_label"}),
    ),
    "EDUCATION_DEGREE": (
        frozenset({"kierunek"}),
        frozenset({"uczelnia", "stopien", "legacy_source_label"}),
    ),
    "CERTIFICATION_NAME": (frozenset({"nazwa"}), frozenset()),
    "COURSE_NAME": (frozenset({"nazwa"}), frozenset({"organizator"})),
    "LANGUAGE_NAME": (frozenset({"jezyk"}), frozenset({"poziom"})),
}

STRUCTURED_FACT_TYPES: frozenset[str] = frozenset(_STRUCTURED_FIELD_CONTRACTS)

#: The complete closed set of fact_types P5a has a payload contract for.
#: Any other fact_type -- including the out-of-scope ``AWARD_NAME`` -- fails
#: closed to ``PAYLOAD_SHAPE_NOT_ALLOWED``; there is no fallback contract.
SUPPORTED_FACT_TYPES: frozenset[str] = NARRATIVE_TEXT_FACT_TYPES | STRUCTURED_FACT_TYPES


@dataclass(frozen=True)
class PayloadShapeResult:
    """The outcome of validating one payload's ``value_json`` shape.

    ``narrative_text``/``structured_fields`` always carry the *raw* source
    strings -- never stripped or otherwise mutated -- so a caller rendering
    ``EXACT_COPY``/``REORDER`` gets back the exact source text.
    """

    violation_code: GenerationViolationCode | None
    detail: str
    narrative_text: str | None = None
    structured_fields: Mapping[str, str] | None = None

    @property
    def is_valid(self) -> bool:
        return self.violation_code is None


def _resolve_narrative_shape(value_json: Any) -> PayloadShapeResult:
    if isinstance(value_json, str):
        if not value_json.strip():
            return PayloadShapeResult(
                violation_code=GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED,
                detail="scalar narrative text must be non-blank",
            )
        return PayloadShapeResult(violation_code=None, detail="ok", narrative_text=value_json)
    if isinstance(value_json, Mapping):
        if "text" not in value_json:
            return PayloadShapeResult(
                violation_code=GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING,
                detail="object narrative payload is missing required key: text",
            )
        text_value = value_json["text"]
        if not (isinstance(text_value, str) and text_value.strip()):
            return PayloadShapeResult(
                violation_code=GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING,
                detail="object narrative payload's text key must be a non-blank string",
            )
        return PayloadShapeResult(violation_code=None, detail="ok", narrative_text=text_value)
    return PayloadShapeResult(
        violation_code=GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED,
        detail="narrative payload must be a string or an object with a text key",
    )


def _resolve_structured_shape(fact_type: str, value_json: Any) -> PayloadShapeResult:
    if not isinstance(value_json, Mapping):
        return PayloadShapeResult(
            violation_code=GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED,
            detail=f"{fact_type} payload must be an object",
        )
    required, optional = _STRUCTURED_FIELD_CONTRACTS[fact_type]
    missing = sorted(
        key
        for key in required
        if not (isinstance(value_json.get(key), str) and value_json[key].strip())
    )
    if missing:
        return PayloadShapeResult(
            violation_code=GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING,
            detail=f"{fact_type} payload is missing required keys: {', '.join(missing)}",
        )
    fields: dict[str, str] = {key: value_json[key] for key in required}
    for key in optional:
        value = value_json.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value
    return PayloadShapeResult(
        violation_code=None, detail="ok", structured_fields=fields
    )


def resolve_payload_shape(fact_type: str, value_json: Any) -> PayloadShapeResult:
    """Validate ``value_json`` against the closed contract for ``fact_type``.

    Returns a fail-closed ``PayloadShapeResult`` (``violation_code`` set,
    ``narrative_text``/``structured_fields`` both ``None``) for any
    fact_type outside ``SUPPORTED_FACT_TYPES`` -- there is no default
    contract and no text-similarity fallback.
    """

    if fact_type in NARRATIVE_TEXT_FACT_TYPES:
        return _resolve_narrative_shape(value_json)
    if fact_type in STRUCTURED_FACT_TYPES:
        return _resolve_structured_shape(fact_type, value_json)
    return PayloadShapeResult(
        violation_code=GenerationViolationCode.PAYLOAD_SHAPE_NOT_ALLOWED,
        detail=f"unsupported fact_type for P5a generation: {fact_type}",
    )
