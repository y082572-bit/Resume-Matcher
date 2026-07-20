"""Database and repository contract for the inactive Explicit Provenance P1 store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_DNS, uuid1, uuid3, uuid4, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.models import TruthEntity, TruthEvidence, TruthFact, TruthFactVariant, TruthPermission
from app.schemas.truth_entity import EntityType, TruthEntityCreate, TruthEntityUpdate
from app.schemas.truth_fact import (
    EvidenceType,
    FactStatus,
    PermissionStatus,
    Transferability,
    TransformationOperation,
    TruthEvidenceCreate,
    TruthFactCreate,
    TruthFactUpdate,
    TruthFactVariantCreate,
    TruthPermissionCreate,
    TruthPermissionUpdate,
)
from app.services.truth_identity import (
    TruthArchivedError,
    TruthIdentityConflictError,
    TruthIntegrityError,
    TruthNotFoundError,
    TruthRevisionConflictError,
)


NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def entity(*, entity_id=None, entity_type=EntityType.PERSON, parent_entity_id=None):
    return TruthEntityCreate(
        entity_id=entity_id or uuid4(),
        entity_type=entity_type,
        parent_entity_id=parent_entity_id,
        display_name="Candidate",
    )


def fact(entity_id, *, fact_id=None, fact_type="SKILL", source_reference=None):
    return TruthFactCreate(
        fact_id=fact_id or uuid4(),
        entity_id=entity_id,
        fact_type=fact_type,
        value_json={"name": "Python", "years": 8},
        normalized_value_json={"name": "Python", "years": 8},
        status=FactStatus.CONFIRMED,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        source_reference=source_reference,
    )


async def seed_entity_fact(database):
    entity_value = entity()
    fact_value = fact(entity_value.entity_id)
    async with database.truth_service.transaction() as repository:
        saved_entity = await repository.create_entity(entity_value)
        saved_fact = await repository.create_fact(fact_value)
    return saved_entity, saved_fact


@pytest.mark.parametrize(
    ("factory", "missing_code"),
    [
        (lambda missing: fact(missing), "TRUTH_ENTITY_NOT_FOUND"),
        (
            lambda missing: TruthFactVariantCreate(
                fact_id=missing,
                text="Python",
                normalized_text="Python",
                operation="EXACT_COPY",
            ),
            "TRUTH_FACT_NOT_FOUND",
        ),
        (
            lambda missing: TruthEvidenceCreate(
                fact_id=missing,
                evidence_type=EvidenceType.DOCUMENT,
                content_hash=HASH,
                captured_at=NOW,
            ),
            "TRUTH_FACT_NOT_FOUND",
        ),
        (
            lambda missing: TruthPermissionCreate(
                fact_id=missing,
                target_scope="SKILLS",
                allowed_operations=[TransformationOperation.EXACT_COPY],
            ),
            "TRUTH_FACT_NOT_FOUND",
        ),
    ],
)
async def test_children_require_existing_parent(isolated_db, factory, missing_code):
    value = factory(uuid4())
    method = {
        TruthFactCreate: "create_fact",
        TruthFactVariantCreate: "create_variant",
        TruthEvidenceCreate: "create_evidence",
        TruthPermissionCreate: "create_permission",
    }[type(value)]
    with pytest.raises(TruthNotFoundError, match=missing_code):
        async with isolated_db.truth_service.transaction() as repository:
            await getattr(repository, method)(value)


async def test_parent_must_exist_and_cycles_are_rejected(isolated_db):
    first = entity()
    second = entity(parent_entity_id=first.entity_id)
    async with isolated_db.truth_service.transaction() as repository:
        await repository.create_entity(first)
        await repository.create_entity(second)
        with pytest.raises(TruthIntegrityError, match="TRUTH_ENTITY_PARENT_CYCLE"):
            await repository.update_entity(
                str(first.entity_id),
                TruthEntityUpdate(expected_revision=1, parent_entity_id=second.entity_id),
            )
        with pytest.raises(TruthNotFoundError, match="TRUTH_ENTITY_NOT_FOUND"):
            await repository.create_entity(entity(parent_entity_id=uuid4()))
    with pytest.raises(ValidationError):
        entity_value = entity()
        TruthEntityCreate(
            entity_id=entity_value.entity_id,
            entity_type=EntityType.PERSON,
            parent_entity_id=entity_value.entity_id,
            display_name="Self",
        )


def test_pydantic_rejects_invalid_identity_status_hash_and_revision():
    with pytest.raises(ValidationError):
        TruthEntityCreate(entity_id="not-a-uuid", entity_type="PERSON", display_name="X")
    with pytest.raises(ValidationError):
        TruthEntityCreate(entity_type="UNKNOWN", display_name="X")
    with pytest.raises(ValidationError):
        TruthEvidenceCreate(
            fact_id=uuid4(), evidence_type="DOCUMENT", content_hash="bad", captured_at=NOW
        )
    with pytest.raises(ValidationError):
        TruthFactUpdate(expected_revision=0, source_reference="x")


async def test_fact_update_preserves_identity_revises_and_changes_snapshot(isolated_db):
    _, saved = await seed_entity_fact(isolated_db)
    async with isolated_db.truth_service.transaction() as repository:
        before = await repository.compute_truth_snapshot()
        metadata_only = await repository.update_fact(
            str(saved.fact_id),
            TruthFactUpdate(expected_revision=1, source_reference="document:2"),
        )
        after_metadata = await repository.compute_truth_snapshot()
        changed = await repository.update_fact(
            str(saved.fact_id),
            TruthFactUpdate(
                expected_revision=2,
                value_json={"name": "Python", "years": 9},
            ),
        )
    assert changed.fact_id == saved.fact_id
    assert metadata_only.revision == 2
    assert metadata_only.content_fingerprint == saved.content_fingerprint
    assert before.fingerprint != after_metadata.fingerprint
    assert changed.revision == 3
    assert changed.content_fingerprint != saved.content_fingerprint


async def test_create_replay_is_idempotent_but_conflicting_content_is_denied(isolated_db):
    entity_value = entity()
    async with isolated_db.truth_service.transaction() as repository:
        first = await repository.create_entity(entity_value)
        replay = await repository.create_entity(entity_value)
        assert replay == first
        changed = entity_value.model_copy(update={"display_name": "Different"})
        with pytest.raises(TruthIdentityConflictError, match="TRUTH_IDENTITY_CONFLICT"):
            await repository.create_entity(changed)


async def test_archived_identity_cannot_be_reused(isolated_db):
    entity_value = entity()
    async with isolated_db.truth_service.transaction() as repository:
        await repository.create_entity(entity_value)
        await repository.archive_entity(str(entity_value.entity_id), expected_revision=1)
        with pytest.raises(TruthIdentityConflictError):
            await repository.create_entity(entity_value)
        with pytest.raises(TruthArchivedError):
            await repository.update_entity(
                str(entity_value.entity_id),
                TruthEntityUpdate(expected_revision=2, display_name="No"),
            )


async def test_stale_and_concurrent_updates_have_exactly_one_winner(isolated_db):
    _, saved = await seed_entity_fact(isolated_db)
    with pytest.raises(TruthRevisionConflictError):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.update_fact(
                str(saved.fact_id), TruthFactUpdate(expected_revision=2, source_reference="stale")
            )

    async def change(reference):
        try:
            async with isolated_db.truth_service.transaction() as repository:
                await repository.update_fact(
                    str(saved.fact_id),
                    TruthFactUpdate(expected_revision=1, source_reference=reference),
                )
            return "winner"
        except TruthRevisionConflictError:
            return "conflict"

    assert sorted(await asyncio.gather(change("a"), change("b"))) == [
        "conflict",
        "winner",
    ]


async def test_employment_scope_and_entity_type_are_enforced(isolated_db):
    person = entity()
    employment = entity(entity_type=EntityType.EMPLOYMENT)
    async with isolated_db.truth_service.transaction() as repository:
        await repository.create_entity(person)
        await repository.create_entity(employment)
        bad = fact(person.entity_id).model_copy(
            update={
                "transferability": Transferability.EMPLOYMENT_SCOPED,
                "employment_scope_entity_id": person.entity_id,
            }
        )
        with pytest.raises(TruthIntegrityError, match="TRUTH_EMPLOYMENT_SCOPE"):
            await repository.create_fact(bad)
        good = fact(person.entity_id).model_copy(
            update={
                "transferability": Transferability.EMPLOYMENT_SCOPED,
                "employment_scope_entity_id": employment.entity_id,
            }
        )
        await repository.create_fact(good)

    with pytest.raises(IntegrityError):
        with isolated_db._sync.begin() as session:
            session.execute(
                TruthEntity.__table__.update()
                .where(TruthEntity.entity_id == str(employment.entity_id))
                .values(entity_type="PERSON")
            )


async def test_unique_variant_evidence_and_active_permission(isolated_db):
    _, saved = await seed_entity_fact(isolated_db)
    variant_one = TruthFactVariantCreate(
        fact_id=saved.fact_id,
        text="Python",
        normalized_text="python",
        operation="EXACT_COPY",
    )
    variant_two = variant_one.model_copy(update={"variant_id": uuid4()})
    evidence_one = TruthEvidenceCreate(
        fact_id=saved.fact_id,
        evidence_type=EvidenceType.DOCUMENT,
        source_reference="cv",
        source_locator="skills[0]",
        content_hash=HASH,
        captured_at=NOW,
    )
    evidence_two = evidence_one.model_copy(update={"evidence_id": uuid4()})
    permission_one = TruthPermissionCreate(
        fact_id=saved.fact_id,
        target_scope="SKILLS",
        allowed_operations=[TransformationOperation.EXACT_COPY],
        status=PermissionStatus.ACTIVE,
    )
    permission_two = permission_one.model_copy(update={"permission_id": uuid4()})
    with pytest.raises(TruthIntegrityError, match="TRUTH_DATABASE_INTEGRITY_ERROR"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.create_variant(variant_one)
            await repository.create_variant(variant_two)
    with pytest.raises(TruthIntegrityError, match="TRUTH_DATABASE_INTEGRITY_ERROR"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.create_evidence(evidence_one)
            await repository.create_evidence(evidence_two)
    with pytest.raises(TruthIntegrityError, match="TRUTH_DATABASE_INTEGRITY_ERROR"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.create_permission(permission_one)
            await repository.create_permission(permission_two)


async def test_foreign_keys_restrict_physical_delete_and_archive_keeps_rows(isolated_db):
    saved_entity, saved_fact = await seed_entity_fact(isolated_db)
    variant = TruthFactVariantCreate(
        fact_id=saved_fact.fact_id,
        text="Python",
        normalized_text="python",
        operation="EXACT_COPY",
    )
    evidence = TruthEvidenceCreate(
        fact_id=saved_fact.fact_id,
        evidence_type=EvidenceType.DOCUMENT,
        content_hash=HASH,
        captured_at=NOW,
    )
    permission = TruthPermissionCreate(
        fact_id=saved_fact.fact_id,
        target_scope="SKILLS",
        allowed_operations=[TransformationOperation.EXACT_COPY],
    )
    async with isolated_db.truth_service.transaction() as repository:
        await repository.create_variant(variant)
        await repository.create_evidence(evidence)
        await repository.create_permission(permission)
        archived = await repository.archive_fact(str(saved_fact.fact_id), expected_revision=1)
        assert archived.archived_at is not None
    async with isolated_db._session() as session:
        assert await session.get(TruthFact, str(saved_fact.fact_id)) is not None
        with pytest.raises(IntegrityError):
            await session.execute(delete(TruthEntity).where(TruthEntity.entity_id == str(saved_entity.entity_id)))
            await session.flush()
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(delete(TruthFact).where(TruthFact.fact_id == str(saved_fact.fact_id)))
            await session.flush()


async def test_transaction_rollback_leaves_no_partial_records(isolated_db):
    entity_value = entity()
    fact_value = fact(entity_value.entity_id)
    with pytest.raises(RuntimeError, match="force rollback"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.create_entity(entity_value)
            await repository.create_fact(fact_value)
            raise RuntimeError("force rollback")
    async with isolated_db._session() as session:
        assert await session.get(TruthEntity, str(entity_value.entity_id)) is None
        assert await session.get(TruthFact, str(fact_value.fact_id)) is None


async def test_foreign_keys_remain_enabled_after_database_restart(isolated_db):
    await seed_entity_fact(isolated_db)
    await isolated_db.close()
    with pytest.raises(TruthNotFoundError, match="TRUTH_ENTITY_NOT_FOUND"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.create_fact(fact(uuid4()))


async def test_database_checks_revision_status_hash_and_json(isolated_db):
    saved_entity, saved_fact = await seed_entity_fact(isolated_db)
    statements = (
        (
            "UPDATE truth_entities SET revision = 0 WHERE entity_id = :identity",
            str(saved_entity.entity_id),
        ),
        (
            "UPDATE truth_entities SET status = 'UNKNOWN' WHERE entity_id = :identity",
            str(saved_entity.entity_id),
        ),
        (
            "UPDATE truth_entities SET content_fingerprint = 'BAD' WHERE entity_id = :identity",
            str(saved_entity.entity_id),
        ),
        (
            "UPDATE truth_facts SET value_json = '{bad' WHERE fact_id = :identity",
            str(saved_fact.fact_id),
        ),
    )
    for statement, identity in statements:
        with isolated_db._sync() as session:
            with pytest.raises(IntegrityError):
                session.execute(text(statement), {"identity": identity})
                session.flush()
            session.rollback()


async def _seed_identity_target(database, table):
    entity_value = entity()
    fact_value = fact(entity_value.entity_id)
    async with database.truth_service.transaction() as repository:
        await repository.create_entity(entity_value)
        if table == "truth_entities":
            return str(entity_value.entity_id)
        await repository.create_fact(fact_value)
        if table == "truth_facts":
            return str(fact_value.fact_id)
        if table == "truth_fact_variants":
            value = TruthFactVariantCreate(
                fact_id=fact_value.fact_id,
                text="Python",
                normalized_text="python",
                operation="EXACT_COPY",
            )
            await repository.create_variant(value)
            return str(value.variant_id)
        if table == "truth_evidence":
            value = TruthEvidenceCreate(
                fact_id=fact_value.fact_id,
                evidence_type=EvidenceType.DOCUMENT,
                content_hash=HASH,
                captured_at=NOW,
            )
            await repository.create_evidence(value)
            return str(value.evidence_id)
        value = TruthPermissionCreate(
            fact_id=fact_value.fact_id,
            target_scope="SKILLS",
            allowed_operations=[TransformationOperation.EXACT_COPY],
        )
        await repository.create_permission(value)
        return str(value.permission_id)


@pytest.mark.parametrize(
    ("table", "identity_column"),
    [
        ("truth_entities", "entity_id"),
        ("truth_facts", "fact_id"),
        ("truth_fact_variants", "variant_id"),
        ("truth_evidence", "evidence_id"),
        ("truth_permissions", "permission_id"),
    ],
)
async def test_all_identity_constraints_require_canonical_uuid4(
    isolated_db, table, identity_column
):
    current = await _seed_identity_target(isolated_db, table)
    valid = str(uuid4())
    with isolated_db._sync() as session:
        session.execute(
            text(
                f"UPDATE {table} SET {identity_column} = :replacement "
                f"WHERE {identity_column} = :current"
            ),
            {"replacement": valid, "current": current},
        )
        session.commit()

    invalid_values = (
        str(uuid1()),
        str(uuid3(NAMESPACE_DNS, "truth.example")),
        str(uuid5(NAMESPACE_DNS, "truth.example")),
        valid.upper(),
        valid[:8] + "--" + valid[9:],
        valid.replace("-", "", 1),
        valid[:19] + "7" + valid[20:],
        valid[:-1] + "g",
    )
    for invalid in invalid_values:
        with isolated_db._sync() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        f"UPDATE {table} SET {identity_column} = :replacement "
                        f"WHERE {identity_column} = :current"
                    ),
                    {"replacement": invalid, "current": valid},
                )
                session.flush()
            session.rollback()


async def _seed_permission(database, *, start, end, target="SKILLS"):
    _, saved_fact = await seed_entity_fact(database)
    permission = TruthPermissionCreate(
        fact_id=saved_fact.fact_id,
        target_scope=target,
        allowed_operations=[TransformationOperation.EXACT_COPY],
        valid_from=start,
        valid_until=end,
    )
    async with database.truth_service.transaction() as repository:
        saved = await repository.create_permission(permission)
    return saved


async def test_permission_window_is_normalized_to_fixed_utc(isolated_db):
    plus_two = timezone(timedelta(hours=2))
    saved = await _seed_permission(
        isolated_db,
        start=datetime(2026, 1, 2, 12, 0, tzinfo=plus_two),
        end=datetime(2026, 1, 2, 13, 30, tzinfo=plus_two),
    )
    assert saved.valid_from == datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)
    assert saved.valid_until == datetime(2026, 1, 2, 11, 30, tzinfo=timezone.utc)
    async with isolated_db._session() as session:
        row = await session.get(TruthPermission, str(saved.permission_id))
        assert row.valid_from == "2026-01-02T10:00:00.000000Z"
        assert row.valid_until == "2026-01-02T11:30:00.000000Z"


def test_permission_create_rejects_non_chronological_window():
    with pytest.raises(ValidationError):
        TruthPermissionCreate(
            fact_id=uuid4(),
            target_scope="SKILLS",
            allowed_operations=[TransformationOperation.EXACT_COPY],
            valid_from=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
            valid_until=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
        )


async def test_permission_update_validates_merged_window(isolated_db):
    saved = await _seed_permission(
        isolated_db,
        start=datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
    )
    with pytest.raises(TruthIntegrityError, match="TRUTH_PERMISSION_INVALID_TIME_WINDOW"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.update_permission(
                str(saved.permission_id),
                TruthPermissionUpdate(
                    expected_revision=1,
                    valid_until=datetime(2026, 1, 2, 9, tzinfo=timezone.utc),
                ),
            )
    with pytest.raises(TruthIntegrityError, match="TRUTH_PERMISSION_INVALID_TIME_WINDOW"):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.update_permission(
                str(saved.permission_id),
                TruthPermissionUpdate(
                    expected_revision=1,
                    valid_from=datetime(2026, 1, 2, 13, tzinfo=timezone.utc),
                ),
            )


async def test_permission_window_uses_chronology_not_input_lexical_order(isolated_db):
    with pytest.raises(ValidationError):
        TruthPermissionCreate(
            fact_id=uuid4(),
            target_scope="SKILLS",
            allowed_operations=[TransformationOperation.EXACT_COPY],
            valid_from=datetime.fromisoformat("2026-01-01T20:00:00-05:00"),
            valid_until=datetime.fromisoformat("2026-01-01T23:00:00+02:00"),
        )
    saved = await _seed_permission(
        isolated_db,
        start=datetime.fromisoformat("2026-01-01T23:00:00+02:00"),
        end=datetime.fromisoformat("2026-01-01T20:30:00-02:00"),
    )
    assert saved.valid_from == datetime(2026, 1, 1, 21, tzinfo=timezone.utc)
    assert saved.valid_until == datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc)


async def test_permission_direct_sql_rejects_noncanonical_offset(isolated_db):
    saved = await _seed_permission(
        isolated_db,
        start=datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
    )
    with isolated_db._sync() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "UPDATE truth_permissions SET valid_from = :value "
                    "WHERE permission_id = :identity"
                ),
                {
                    "value": "2026-01-02T10:00:00.000000+00:00",
                    "identity": str(saved.permission_id),
                },
            )
            session.flush()
        session.rollback()


@pytest.mark.parametrize("field", ["valid_from", "valid_until"])
async def test_permission_direct_sql_enforces_real_calendar_and_fixed_utc(
    isolated_db, field
):
    saved = await _seed_permission(
        isolated_db,
        start=datetime(1, 1, 1, tzinfo=timezone.utc),
        end=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
    )
    identity = str(saved.permission_id)
    valid = "2024-02-29T10:00:00.000001Z"
    with isolated_db._sync() as session:
        session.execute(
            text(
                f"UPDATE truth_permissions SET {field} = :value "
                "WHERE permission_id = :identity"
            ),
            {"value": valid, "identity": identity},
        )
        session.commit()

    invalid_values = (
        "2026-02-29T10:00:00.000000Z",
        "2026-02-30T10:00:00.000000Z",
        "2026-02-31T10:00:00.000000Z",
        "2026-04-31T10:00:00.000000Z",
        "2026-00-01T10:00:00.000000Z",
        "2026-13-01T10:00:00.000000Z",
        "2026-01-00T10:00:00.000000Z",
        "2026-01-01T24:00:00.000000Z",
        "2026-01-01T25:00:00.000000Z",
        "2026-01-01T10:60:00.000000Z",
        "2026-01-01T10:00:60.000000Z",
        "2026-01-01T10:00:00.000000+00:00",
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:00.00001Z",
        "2026-01-01T10:00:00.0000000Z",
    )
    for invalid in invalid_values:
        with isolated_db._sync() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        f"UPDATE truth_permissions SET {field} = :value "
                        "WHERE permission_id = :identity"
                    ),
                    {"value": invalid, "identity": identity},
                )
                session.flush()
            session.rollback()


async def test_permission_insert_rejects_february_31_reproduction(isolated_db):
    _, saved_fact = await seed_entity_fact(isolated_db)
    with isolated_db._sync() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    """
                    INSERT INTO truth_permissions (
                        permission_id, fact_id, target_scope, allowed_operations,
                        constraints_json, status, approved_by, approved_at,
                        valid_from, valid_until, schema_version, revision,
                        content_fingerprint, created_at, updated_at, archived_at
                    ) VALUES (
                        :permission_id, :fact_id, 'SKILLS', '[\"EXACT_COPY\"]',
                        '{}', 'REVIEW_REQUIRED', NULL, NULL,
                        '2026-02-31T10:00:00.000000Z',
                        '2026-03-04T10:00:00.000000Z',
                        '1.0', 1, :fingerprint,
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00', NULL
                    )
                    """
                ),
                {
                    "permission_id": str(uuid4()),
                    "fact_id": str(saved_fact.fact_id),
                    "fingerprint": "b" * 64,
                },
            )
            session.flush()
        session.rollback()


async def test_permission_window_update_revises_fingerprint_and_keeps_locking(isolated_db):
    saved = await _seed_permission(
        isolated_db,
        start=datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
    )
    async with isolated_db.truth_service.transaction() as repository:
        updated = await repository.update_permission(
            str(saved.permission_id),
            TruthPermissionUpdate(
                expected_revision=1,
                valid_until=datetime(2026, 1, 2, 13, tzinfo=timezone.utc),
            ),
        )
    assert updated.revision == 2
    assert updated.content_fingerprint != saved.content_fingerprint
    with pytest.raises(TruthRevisionConflictError):
        async with isolated_db.truth_service.transaction() as repository:
            await repository.update_permission(
                str(saved.permission_id),
                TruthPermissionUpdate(
                    expected_revision=1,
                    valid_until=datetime(2026, 1, 2, 14, tzinfo=timezone.utc),
                ),
            )
