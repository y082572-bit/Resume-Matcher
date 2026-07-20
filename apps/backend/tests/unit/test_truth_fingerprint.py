"""Versioned canonical JSON and content/snapshot fingerprints."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.truth_fingerprint import (
    canonical_json_bytes,
    compute_entity_fingerprint,
    compute_evidence_fingerprint,
    compute_fact_fingerprint,
    compute_permission_fingerprint,
    compute_truth_snapshot_fingerprint,
    compute_variant_fingerprint,
)


def test_canonical_fixture_has_exact_bytes() -> None:
    value = {"z": "  A\u00a0 B\r\nC  ", "a": 1, "null": None}
    assert canonical_json_bytes(value) == (
        b'{"a":1,"null":null,"z":"A B\\nC"}'
    )


def test_semantically_same_mapping_and_nfkc_have_same_bytes() -> None:
    left = {"name": "Ｐｙｔｈｏｎ", "count": 1}
    right = {"count": 1, "name": "Python"}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)


def test_ordered_array_changes_but_explicit_set_like_array_does_not() -> None:
    left = {"items": ["b", "a"]}
    right = {"items": ["a", "b"]}
    assert canonical_json_bytes(left) != canonical_json_bytes(right)
    policy = frozenset({"items"})
    assert canonical_json_bytes(left, set_like_paths=policy) == canonical_json_bytes(
        right, set_like_paths=policy
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"number": value})


def _base_fact() -> dict[str, object]:
    return {
        "fact_id": "ignored",
        "entity_id": "entity-1",
        "fact_type": "SKILL",
        "value_json": "Python",
        "normalized_value_json": "Python",
        "status": "CONFIRMED",
        "use_in_cv": True,
        "requires_approval": False,
        "transferability": "GLOBAL_TRANSFERABLE",
        "employment_scope_entity_id": None,
        "source_reference": "legacy:1",
        "schema_version": "1.0",
        "revision": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "archived_at": None,
    }


def test_source_reference_revision_and_timestamps_do_not_change_content_fingerprint() -> None:
    original = _base_fact()
    changed = {
        **original,
        "source_reference": "legacy:2",
        "revision": 2,
        "updated_at": "2026-02-01T00:00:00+00:00",
    }
    assert compute_fact_fingerprint(original) == compute_fact_fingerprint(changed)


def test_semantic_change_changes_content_fingerprint() -> None:
    original = _base_fact()
    changed = {**original, "value_json": "Rust", "normalized_value_json": "Rust"}
    assert compute_fact_fingerprint(original) != compute_fact_fingerprint(changed)


def test_all_content_fingerprints_are_sha256_and_ignore_own_identity() -> None:
    base = {
        "status": "ACTIVE",
        "schema_version": "1.0",
        "revision": 1,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc) + timedelta(seconds=1),
        "archived_at": None,
        "source_reference": "metadata",
    }
    values = (
        compute_entity_fingerprint({**base, "entity_id": "x", "entity_type": "SKILL"}),
        compute_variant_fingerprint({**base, "variant_id": "x", "text": "Python"}),
        compute_evidence_fingerprint(
            {**base, "evidence_id": "x", "content_hash": "a" * 64, "confirmed_by": "u"}
        ),
        compute_permission_fingerprint(
            {
                **base,
                "permission_id": "x",
                "allowed_operations": ["REORDER", "EXACT_COPY"],
                "constraints_json": {},
            }
        ),
    )
    assert all(len(value) == 64 for value in values)


def test_source_reference_revision_change_changes_snapshot() -> None:
    fact = {**_base_fact(), "content_fingerprint": compute_fact_fingerprint(_base_fact())}
    changed = {**fact, "source_reference": "legacy:2", "revision": 2}
    empty: list[dict[str, object]] = []
    first = compute_truth_snapshot_fingerprint(
        entities=empty, facts=[fact], variants=empty, evidence=empty, permissions=empty
    )
    second = compute_truth_snapshot_fingerprint(
        entities=empty, facts=[changed], variants=empty, evidence=empty, permissions=empty
    )
    assert first != second
