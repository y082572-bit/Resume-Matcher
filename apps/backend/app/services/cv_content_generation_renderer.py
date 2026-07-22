"""Deterministic P5a renderers: ``value_json`` -> ``generated_text``.

No renderer here calls an LLM, uses ``str(value_json)``/``repr(value_json)``,
serializes with ``json.dumps`` for display, or iterates a plain
``dict.items()`` order. Every structured composition reads named keys only,
in a fixed, versioned order. ``EXACT_COPY``/``REORDER`` return the exact
source text; ``FORMAT_NORMALIZATION`` applies only Unicode NFKC and
whitespace normalization, never a meaning-changing edit.
"""

from __future__ import annotations

import unicodedata

from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_generation_policy import (
    NARRATIVE_TEXT_FACT_TYPES,
    PayloadShapeResult,
)


RENDERER_CONTRACT_VERSION = "cv-content-generation-renderer-v1"
RENDERER_NAME = "cv_content_generation_renderer"


def _normalize(text: str) -> str:
    """Unicode NFKC plus whitespace collapse -- nothing else.

    Never touches Polish diacritics, digits, percent signs, currency
    symbols, dates, proper names, or negations: NFKC is a compatibility
    normal form, not a text rewrite, and whitespace collapsing only affects
    runs of whitespace characters.
    """

    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


def _finalize(text: str, operation: TransformationOperation) -> str:
    if operation == TransformationOperation.FORMAT_NORMALIZATION:
        return _normalize(text)
    return text


def _field(fields: dict[str, str], operation: TransformationOperation, key: str) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    return _finalize(value, operation)


def _render_employment_role(fields: dict[str, str], operation: TransformationOperation) -> str:
    stanowisko = _field(fields, operation, "stanowisko")
    firma = _field(fields, operation, "firma")
    okres_od = _field(fields, operation, "okresOd")
    okres_do = _field(fields, operation, "okresDo")
    assert stanowisko is not None and firma is not None
    text = f"{stanowisko}, {firma}"
    if okres_od and okres_do:
        text += f" | {okres_od} - {okres_do}"
    elif okres_od:
        text += f" | {okres_od}"
    elif okres_do:
        text += f" | {okres_do}"
    return text


def _render_education_degree(fields: dict[str, str], operation: TransformationOperation) -> str:
    stopien = _field(fields, operation, "stopien")
    kierunek = _field(fields, operation, "kierunek")
    uczelnia = _field(fields, operation, "uczelnia")
    assert kierunek is not None
    parts = [part for part in (stopien, kierunek, uczelnia) if part]
    return ", ".join(parts)


def _render_certification_name(fields: dict[str, str], operation: TransformationOperation) -> str:
    nazwa = _field(fields, operation, "nazwa")
    assert nazwa is not None
    return nazwa


def _render_course_name(fields: dict[str, str], operation: TransformationOperation) -> str:
    nazwa = _field(fields, operation, "nazwa")
    organizator = _field(fields, operation, "organizator")
    assert nazwa is not None
    return f"{nazwa}, {organizator}" if organizator else nazwa


def _render_language_name(fields: dict[str, str], operation: TransformationOperation) -> str:
    jezyk = _field(fields, operation, "jezyk")
    poziom = _field(fields, operation, "poziom")
    assert jezyk is not None
    return f"{jezyk} - {poziom}" if poziom else jezyk


_STRUCTURED_RENDERERS = {
    "EMPLOYMENT_ROLE": _render_employment_role,
    "EDUCATION_DEGREE": _render_education_degree,
    "CERTIFICATION_NAME": _render_certification_name,
    "COURSE_NAME": _render_course_name,
    "LANGUAGE_NAME": _render_language_name,
}


def render_generated_text(
    *, fact_type: str, operation: TransformationOperation, shape: PayloadShapeResult
) -> str:
    """Render ``generated_text`` for one already shape-validated payload.

    Raises ``ValueError`` for a fact_type with no registered renderer --
    this can only happen if a caller invokes the renderer directly for a
    fact_type ``resolve_payload_shape`` never approved; the builder always
    validates shape first and treats this as ``RENDERING_FAILED``.
    """

    if fact_type in NARRATIVE_TEXT_FACT_TYPES:
        if shape.narrative_text is None:
            raise ValueError("narrative fact_type requires shape.narrative_text")
        return _finalize(shape.narrative_text, operation)

    renderer = _STRUCTURED_RENDERERS.get(fact_type)
    if renderer is None:
        raise ValueError(f"no deterministic renderer registered for fact_type={fact_type}")
    if shape.structured_fields is None:
        raise ValueError("structured fact_type requires shape.structured_fields")
    return renderer(dict(shape.structured_fields), operation)
