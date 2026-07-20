"""Stable identity and strict Pydantic contracts for Explicit Provenance P1."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.truth_entity import EntityType, TruthEntityCreate, TruthEntityUpdate
from app.schemas.truth_fact import (
    Transferability,
    TransformationOperation,
    TruthFactCreate,
    TruthPermissionCreate,
    parse_permission_datetime,
    serialize_permission_datetime,
)
from app.services.truth_identity import new_truth_id, require_uuid4


def test_uuid_is_independent_of_text_source_and_order() -> None:
    first = TruthEntityCreate(
        entity_type=EntityType.SKILL,
        display_name="Python",
        source_reference="legacy:skills:1",
    )
    second = TruthEntityCreate(
        entity_type=EntityType.SKILL,
        display_name="Python",
        source_reference="legacy:skills:1",
    )
    assert first.entity_id != second.entity_id
    assert first.model_dump(exclude={"entity_id"}) == second.model_dump(
        exclude={"entity_id"}
    )
    assert UUID(new_truth_id()).version == 4


def test_split_creates_new_fact_ids() -> None:
    entity_id = uuid4()
    first = TruthFactCreate(
        entity_id=entity_id,
        fact_type="RESPONSIBILITY",
        value_json={"text": "One combined fact"},
        normalized_value_json={"text": "One combined fact"},
        transferability=Transferability.EMPLOYMENT_SCOPED,
    )
    left = first.model_copy(update={"fact_id": uuid4(), "value_json": {"text": "One"}})
    right = first.model_copy(update={"fact_id": uuid4(), "value_json": {"text": "Two"}})
    assert len({first.fact_id, left.fact_id, right.fact_id}) == 3


def test_all_references_require_uuid4() -> None:
    with pytest.raises(ValidationError):
        TruthFactCreate(
            entity_id="00000000-0000-1000-8000-000000000000",
            fact_type="SKILL",
            value_json="Python",
            normalized_value_json="Python",
            transferability=Transferability.GLOBAL_TRANSFERABLE,
        )
    with pytest.raises(Exception):
        require_uuid4("00000000-0000-1000-8000-000000000000")


def test_entity_identity_and_type_are_not_update_fields() -> None:
    update = TruthEntityUpdate(expected_revision=1, display_name="New name")
    assert "entity_id" not in type(update).model_fields
    assert "entity_type" not in type(update).model_fields
    with pytest.raises(ValidationError):
        TruthEntityUpdate(
            expected_revision=1,
            display_name="New name",
            entity_type="PROJECT",
        )


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TruthEntityCreate(
            entity_type=EntityType.SKILL,
            display_name="Python",
            schema_version="2.0",
        )


def test_permission_operations_are_sorted_and_unique() -> None:
    permission = TruthPermissionCreate(
        fact_id=uuid4(),
        target_scope="SKILLS",
        allowed_operations=[
            TransformationOperation.REORDER,
            TransformationOperation.EXACT_COPY,
            TransformationOperation.EXACT_COPY,
        ],
        approved_at=datetime.now(timezone.utc),
    )
    assert permission.allowed_operations == [
        TransformationOperation.EXACT_COPY,
        TransformationOperation.REORDER,
    ]


def test_naive_evidence_timestamp_is_rejected() -> None:
    from app.schemas.truth_fact import EvidenceType, TruthEvidenceCreate

    with pytest.raises(ValidationError):
        TruthEvidenceCreate(
            fact_id=uuid4(),
            evidence_type=EvidenceType.DOCUMENT,
            content_hash="a" * 64,
            captured_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize("year", [1, 99, 999, 1000, 9999])
def test_permission_timestamp_year_is_always_four_digits(year: int) -> None:
    value = datetime(year, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    serialized = serialize_permission_datetime(value)
    assert serialized is not None
    assert serialized == f"{year:04d}-01-02T03:04:05.000006Z"
    assert len(serialized) == 27
    assert parse_permission_datetime(serialized) == value


@pytest.mark.parametrize("microsecond", [1, 999999])
def test_permission_timestamp_microseconds_round_trip_exactly(microsecond: int) -> None:
    value = datetime(2026, 2, 3, 4, 5, 6, microsecond, tzinfo=timezone.utc)
    serialized = serialize_permission_datetime(value)
    assert serialized is not None
    assert serialized.endswith(f".{microsecond:06d}Z")
    assert parse_permission_datetime(serialized) == value


def test_permission_timestamp_offset_normalizes_and_round_trips() -> None:
    value = datetime(
        2026, 2, 3, 4, 5, 6, 123456,
        tzinfo=timezone(timedelta(hours=2)),
    )
    serialized = serialize_permission_datetime(value)
    assert serialized == "2026-02-03T02:05:06.123456Z"
    assert parse_permission_datetime(serialized) == value.astimezone(timezone.utc)


def test_permission_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        serialize_permission_datetime(datetime(2026, 1, 1))
