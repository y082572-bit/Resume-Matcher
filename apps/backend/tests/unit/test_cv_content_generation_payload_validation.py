"""``ApprovedFactPayloadSet`` snapshot validation: fingerprints, identity.

All facts/entities are synthetic; no real person or CV data is used.
"""

from __future__ import annotations

from uuid import uuid4

from app.schemas.cv_content_generation import (
    ApprovedFactPayloadSnapshot,
    GenerationViolationCode,
)
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
)
from app.services.cv_content_generation_policy import (
    CV_CONTENT_GENERATION_POLICY_VERSION,
    resolve_payload_shape,
)


def _snapshot(**overrides) -> ApprovedFactPayloadSnapshot:
    values = dict(
        person_entity_id=uuid4(),
        entity_id=uuid4(),
        fact_id=uuid4(),
        fact_type="SUMMARY",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        value_json="Some text",
        normalized_value_json="Some text",
    )
    values.update(overrides)
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=values["person_entity_id"],
        entity_id=values["entity_id"],
        fact_id=values["fact_id"],
        fact_type=values["fact_type"],
        fact_revision=values["fact_revision"],
        fact_content_fingerprint=values["fact_content_fingerprint"],
        value_json=values["value_json"],
        normalized_value_json=values["normalized_value_json"],
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    return ApprovedFactPayloadSnapshot(**values, payload_fingerprint=fingerprint)


# -- Payload fingerprint determinism --


def test_payload_fingerprint_is_deterministic() -> None:
    kwargs = dict(
        person_entity_id=uuid4(),
        entity_id=uuid4(),
        fact_id=uuid4(),
        fact_type="SUMMARY",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        value_json="Some text",
        normalized_value_json="Some text",
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    assert compute_fact_payload_fingerprint(**kwargs) == compute_fact_payload_fingerprint(**kwargs)


def test_payload_fingerprint_changes_with_value_json() -> None:
    kwargs = dict(
        person_entity_id=uuid4(),
        entity_id=uuid4(),
        fact_id=uuid4(),
        fact_type="SUMMARY",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        normalized_value_json="Some text",
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    fp1 = compute_fact_payload_fingerprint(value_json="Some text", **kwargs)
    fp2 = compute_fact_payload_fingerprint(value_json="Different text", **kwargs)
    assert fp1 != fp2


def test_payload_fingerprint_changes_with_generation_policy_version() -> None:
    kwargs = dict(
        person_entity_id=uuid4(),
        entity_id=uuid4(),
        fact_id=uuid4(),
        fact_type="SUMMARY",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        value_json="Some text",
        normalized_value_json="Some text",
    )
    fp1 = compute_fact_payload_fingerprint(generation_policy_version="v1", **kwargs)
    fp2 = compute_fact_payload_fingerprint(generation_policy_version="v2", **kwargs)
    assert fp1 != fp2


def test_payload_fingerprint_excludes_source_reference_style_fields() -> None:
    # ApprovedFactPayloadSnapshot has no source_reference/legacy_record_key
    # field at all -- confirmed structurally via model_fields.
    assert "source_reference" not in ApprovedFactPayloadSnapshot.model_fields
    assert "legacy_record_key" not in ApprovedFactPayloadSnapshot.model_fields


# -- Payload set fingerprint order independence --


def test_payload_set_fingerprint_is_order_independent() -> None:
    p1 = _snapshot()
    p2 = _snapshot()
    fp_forward = compute_fact_payload_set_fingerprint([p1, p2])
    fp_reversed = compute_fact_payload_set_fingerprint([p2, p1])
    assert fp_forward == fp_reversed


def test_payload_set_fingerprint_changes_with_membership() -> None:
    p1 = _snapshot()
    p2 = _snapshot()
    p3 = _snapshot()
    fp_two = compute_fact_payload_set_fingerprint([p1, p2])
    fp_three = compute_fact_payload_set_fingerprint([p1, p2, p3])
    assert fp_two != fp_three


# -- Snapshot-level identity/shape checks (via the shared policy resolver) --


def test_scalar_string_narrative_passes() -> None:
    result = resolve_payload_shape("SUMMARY", "A valid narrative value")
    assert result.is_valid


def test_object_text_key_narrative_passes() -> None:
    result = resolve_payload_shape("SUMMARY", {"text": "A valid narrative value"})
    assert result.is_valid


def test_missing_text_key_fails_closed() -> None:
    result = resolve_payload_shape("SUMMARY", {"not_text": "value"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_missing_required_structured_key_fails_closed() -> None:
    result = resolve_payload_shape("LANGUAGE_NAME", {"poziom": "C1"})
    assert result.violation_code == GenerationViolationCode.PAYLOAD_REQUIRED_FIELD_MISSING


def test_extra_keys_do_not_reach_generated_text() -> None:
    result = resolve_payload_shape(
        "LANGUAGE_NAME", {"jezyk": "English", "poziom": "C1", "unexpected": "ignored"}
    )
    assert result.is_valid
    assert result.structured_fields == {"jezyk": "English", "poziom": "C1"}
