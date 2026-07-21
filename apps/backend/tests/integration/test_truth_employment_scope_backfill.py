"""Integration tests for the Stage P3.5 employment_scope_entity_id backfill.

All data is synthetic (no real people or companies). Facts are seeded
directly against the P1 TruthRepository, independent of the P2 legacy
migrator, since the backfill tool must repair pre-existing P1 data
regardless of how it originally got there.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap
from app.schemas.truth_entity import EntityType, TruthEntityCreate
from app.schemas.truth_fact import FactStatus, Transferability, TruthFactCreate
from app.services.truth_employment_scope_backfill import (
    EMPLOYMENT_SCOPE_COMPATIBILITY_VERSION,
    Classification,
    EmploymentScopeConflictError,
    PersonEntityNotFoundError,
    PersonEntityTypeMismatchError,
    ReasonCode,
    apply,
    dry_run,
)


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


async def _create_entity(database, *, entity_type: EntityType, parent_entity_id: str | None = None, name: str = "Synthetic Entity") -> str:
    async with database.truth_service.transaction() as repo:
        entity = await repo.create_entity(
            TruthEntityCreate(entity_type=entity_type, parent_entity_id=parent_entity_id, display_name=name)
        )
        return str(entity.entity_id)


async def _create_fact(
    database,
    *,
    entity_id: str,
    fact_type: str = "EMPLOYMENT_ACTIVITY",
    transferability: Transferability = Transferability.EMPLOYMENT_SCOPED,
    employment_scope_entity_id: str | None = None,
    source_reference: str | None = None,
) -> str:
    async with database.truth_service.transaction() as repo:
        fact = await repo.create_fact(
            TruthFactCreate(
                entity_id=entity_id,
                fact_type=fact_type,
                value_json={"text": "Did synthetic work"},
                normalized_value_json={"text": "did synthetic work"},
                status=FactStatus.CONFIRMED,
                use_in_cv=True,
                requires_approval=False,
                transferability=transferability,
                employment_scope_entity_id=employment_scope_entity_id,
                source_reference=source_reference,
            )
        )
        return str(fact.fact_id)


async def _get_fact(database, fact_id: str) -> TruthFact | None:
    async with database._session() as session:
        return await session.get(TruthFact, fact_id)




async def _person_and_employment(database, *, person_name="Synthetic Person A", employment_name="Synthetic Employer A"):
    person_id = await _create_entity(database, entity_type=EntityType.PERSON, name=person_name)
    employment_id = await _create_entity(
        database, entity_type=EntityType.EMPLOYMENT, parent_entity_id=person_id, name=employment_name
    )
    return person_id, employment_id


def _item_for(report, fact_id: str):
    return next(item for item in report.items if item.fact_id == fact_id)


@pytest.mark.asyncio
async def test_dry_run_classifies_eligible_without_mutation(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id)

    before = await _get_fact(database, fact_id)
    report = await dry_run(database=database, person_entity_id=person_id)
    after = await _get_fact(database, fact_id)

    item = _item_for(report, fact_id)
    assert item.classification == Classification.ELIGIBLE
    assert item.reason_code == ReasonCode.ELIGIBLE_SCOPE_MISSING
    assert item.expected_employment_scope_entity_id == employment_id
    assert report.compatibility_version == EMPLOYMENT_SCOPE_COMPATIBILITY_VERSION
    assert report.mode == "DRY_RUN"

    assert after.revision == before.revision
    assert after.content_fingerprint == before.content_fingerprint
    assert after.updated_at == before.updated_at
    assert after.employment_scope_entity_id is None


@pytest.mark.asyncio
async def test_apply_repairs_eligible_facts(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id)
    before = await _get_fact(database, fact_id)

    report = await apply(database=database, person_entity_id=person_id)

    item = _item_for(report, fact_id)
    assert item.classification == Classification.REPAIRED
    assert fact_id in report.repaired_fact_ids

    after = await _get_fact(database, fact_id)
    assert after.employment_scope_entity_id == employment_id
    assert after.revision == before.revision + 1
    assert after.content_fingerprint != before.content_fingerprint
    # Untouched fields.
    assert after.fact_id == before.fact_id
    assert after.entity_id == before.entity_id
    assert after.fact_type == before.fact_type
    assert after.value_json == before.value_json
    assert after.normalized_value_json == before.normalized_value_json
    assert after.source_reference == before.source_reference
    assert after.status == before.status
    assert after.use_in_cv == before.use_in_cv
    assert after.requires_approval == before.requires_approval
    assert after.transferability == before.transferability


@pytest.mark.asyncio
async def test_migration_map_target_fact_id_is_untouched_by_backfill(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id, source_reference="truth_library:doswiadczenieZawodowe:dz-1:1.0")

    now = "2026-01-01T00:00:00+00:00"
    async with database._session() as session:
        session.add(
            TruthLegacyMigrationMap(
                migration_map_id=str(uuid4()),
                person_entity_id=person_id,
                legacy_source_id="truth_library_primary",
                legacy_source_path="/synthetic/path.json",
                legacy_category="doswiadczenieZawodowe",
                legacy_record_key="doswiadczenieZawodowe:dz-1:1.0",
                legacy_record_fingerprint="a" * 64,
                target_entity_id=employment_id,
                target_fact_id=fact_id,
                classification="MIGRATABLE",
                migration_schema_version="1.0",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    await apply(database=database, person_entity_id=person_id)

    async with database._session() as session:
        row = (
            await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.person_entity_id == person_id)
            )
        ).scalar_one()
    assert row.target_fact_id == fact_id


@pytest.mark.asyncio
async def test_already_correct_fact_is_never_mutated(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id, employment_scope_entity_id=employment_id)
    before = await _get_fact(database, fact_id)

    dry_report = await dry_run(database=database, person_entity_id=person_id)
    item = _item_for(dry_report, fact_id)
    assert item.classification == Classification.ALREADY_CORRECT
    assert item.reason_code == ReasonCode.ALREADY_CORRECT_SELF_SCOPE

    apply_report = await apply(database=database, person_entity_id=person_id)
    apply_item = _item_for(apply_report, fact_id)
    assert apply_item.classification == Classification.ALREADY_CORRECT
    assert apply_report.repaired_fact_ids == ()

    after = await _get_fact(database, fact_id)
    assert after.revision == before.revision
    assert after.content_fingerprint == before.content_fingerprint


@pytest.mark.asyncio
async def test_apply_is_idempotent_second_run_has_zero_repaired(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id)

    first = await apply(database=database, person_entity_id=person_id)
    assert fact_id in first.repaired_fact_ids

    second = await apply(database=database, person_entity_id=person_id)
    assert second.repaired_fact_ids == ()
    item = _item_for(second, fact_id)
    assert item.classification == Classification.ALREADY_CORRECT
    assert item.reason_code == ReasonCode.ALREADY_CORRECT_SELF_SCOPE

    async with database._session() as session:
        facts = (await session.execute(select(TruthFact))).scalars().all()
        entities = (await session.execute(select(TruthEntity))).scalars().all()
    assert len(facts) == 1  # no second fact created
    assert len(entities) == 2  # person + employment only, no duplicates


@pytest.mark.asyncio
async def test_wrong_owner_entity_type_fails_closed_with_zero_mutations(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    skill_id = await _create_entity(database, entity_type=EntityType.SKILL, parent_entity_id=person_id, name="Synthetic Skill")

    eligible_fact_id = await _create_fact(database, entity_id=employment_id)
    # fact_type is approved but its owner entity is not EMPLOYMENT.
    broken_fact_id = await _create_fact(database, entity_id=skill_id, fact_type="EMPLOYMENT_ROLE")

    before_eligible = await _get_fact(database, eligible_fact_id)

    with pytest.raises(EmploymentScopeConflictError):
        await apply(database=database, person_entity_id=person_id)

    after_eligible = await _get_fact(database, eligible_fact_id)
    assert after_eligible.revision == before_eligible.revision
    assert after_eligible.employment_scope_entity_id is None

    dry_report = await dry_run(database=database, person_entity_id=person_id)
    broken_item = _item_for(dry_report, broken_fact_id)
    assert broken_item.classification == Classification.BLOCKED
    assert broken_item.reason_code == ReasonCode.OWNER_ENTITY_NOT_EMPLOYMENT


@pytest.mark.asyncio
async def test_wrong_person_entity_id_fails_closed_before_classification(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    await _create_fact(database, entity_id=employment_id)

    with pytest.raises(PersonEntityNotFoundError):
        await apply(database=database, person_entity_id=str(uuid4()))

    with pytest.raises(PersonEntityTypeMismatchError):
        await apply(database=database, person_entity_id=employment_id)  # EMPLOYMENT, not PERSON


@pytest.mark.asyncio
async def test_person_isolation_with_two_persons(database) -> None:
    person_a, employment_a = await _person_and_employment(database, person_name="Person A", employment_name="Employer A")
    person_b, employment_b = await _person_and_employment(database, person_name="Person B", employment_name="Employer B")

    fact_a = await _create_fact(database, entity_id=employment_a)
    fact_b = await _create_fact(database, entity_id=employment_b)

    report_a = await apply(database=database, person_entity_id=person_a)

    item_a = _item_for(report_a, fact_a)
    assert item_a.classification == Classification.REPAIRED

    # Person B's fact must not appear as eligible/repaired for person A's run.
    item_b = next((item for item in report_a.items if item.fact_id == fact_b), None)
    if item_b is not None:
        assert item_b.classification == Classification.BLOCKED
        assert item_b.reason_code == ReasonCode.OWNER_NOT_CHILD_OF_PERSON
    assert fact_b not in report_a.repaired_fact_ids

    fact_b_row = await _get_fact(database, fact_b)
    assert fact_b_row.employment_scope_entity_id is None  # untouched

    # Now backfill person B independently; person A's fact must be unaffected.
    fact_a_before_b_run = await _get_fact(database, fact_a)
    report_b = await apply(database=database, person_entity_id=person_b)
    item_b_own_run = _item_for(report_b, fact_b)
    assert item_b_own_run.classification == Classification.REPAIRED
    fact_a_after_b_run = await _get_fact(database, fact_a)
    assert fact_a_after_b_run.revision == fact_a_before_b_run.revision
    assert fact_a_after_b_run.employment_scope_entity_id == employment_a


@pytest.mark.asyncio
async def test_existing_scope_pointing_to_other_employment_is_conflict(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    other_employment_id = await _create_entity(
        database, entity_type=EntityType.EMPLOYMENT, parent_entity_id=person_id, name="Other Employer"
    )
    fact_id = await _create_fact(database, entity_id=employment_id, employment_scope_entity_id=other_employment_id)

    report = await dry_run(database=database, person_entity_id=person_id)
    item = _item_for(report, fact_id)
    assert item.classification == Classification.CONFLICT
    assert item.reason_code == ReasonCode.EXISTING_SCOPE_POINTS_TO_OTHER_EMPLOYMENT

    with pytest.raises(EmploymentScopeConflictError):
        await apply(database=database, person_entity_id=person_id)


def test_existing_scope_pointing_to_non_employment_is_conflict() -> None:
    """The DB itself already enforces (via ``trg_truth_facts_employment_scope_insert``
    / ``_update``) that employment_scope_entity_id can never point at a
    non-EMPLOYMENT entity, so this state cannot be produced through the real
    write path. This white-box call exercises the classifier's own defensive
    branch directly, with lightweight duck-typed stand-ins for the ORM rows,
    to prove it still fails safe if that invariant were ever violated by
    some other write path (e.g. a future schema without the trigger)."""

    from types import SimpleNamespace

    from app.services.truth_employment_scope_backfill import _classify_fact

    person_id = str(uuid4())
    employment_id = str(uuid4())
    skill_id = str(uuid4())
    fact_id = str(uuid4())

    fact = SimpleNamespace(
        fact_id=fact_id,
        entity_id=employment_id,
        fact_type="EMPLOYMENT_ACTIVITY",
        employment_scope_entity_id=skill_id,
        revision=1,
        content_fingerprint="a" * 64,
        transferability="EMPLOYMENT_SCOPED",
    )
    owner = SimpleNamespace(entity_type="EMPLOYMENT", parent_entity_id=person_id)
    existing_scope_entity = SimpleNamespace(entity_type="SKILL", parent_entity_id=person_id)

    item = _classify_fact(
        fact,
        person_entity_id=person_id,
        owner=owner,
        existing_scope_entity=existing_scope_entity,
    )
    assert item.classification == Classification.CONFLICT
    assert item.reason_code == ReasonCode.EXISTING_SCOPE_POINTS_TO_NON_EMPLOYMENT


@pytest.mark.asyncio
async def test_transferability_mismatch_fails_closed(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(
        database, entity_id=employment_id, fact_type="EMPLOYMENT_ACTIVITY",
        transferability=Transferability.GLOBAL_TRANSFERABLE,
    )

    report = await dry_run(database=database, person_entity_id=person_id)
    item = _item_for(report, fact_id)
    assert item.classification == Classification.BLOCKED
    assert item.reason_code == ReasonCode.TRANSFERABILITY_MISMATCH

    with pytest.raises(EmploymentScopeConflictError):
        await apply(database=database, person_entity_id=person_id)


@pytest.mark.asyncio
async def test_batch_rollback_leaves_eligible_fact_unrepaired_when_batch_has_a_structural_blocker(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    eligible_fact_id = await _create_fact(database, entity_id=employment_id)
    skill_id = await _create_entity(database, entity_type=EntityType.SKILL, parent_entity_id=person_id, name="Synthetic Skill")
    broken_fact_id = await _create_fact(database, entity_id=skill_id, fact_type="EMPLOYMENT_ACTIVITY")

    before = await _get_fact(database, eligible_fact_id)

    with pytest.raises(EmploymentScopeConflictError):
        await apply(database=database, person_entity_id=person_id)

    after = await _get_fact(database, eligible_fact_id)
    assert after.revision == before.revision
    assert after.employment_scope_entity_id is None
    broken_after = await _get_fact(database, broken_fact_id)
    assert broken_after.employment_scope_entity_id is None


@pytest.mark.asyncio
async def test_unknown_employment_fact_type_is_not_automatically_repaired(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    fact_id = await _create_fact(database, entity_id=employment_id, fact_type="EMPLOYMENT_UNKNOWN_TYPE")

    report = await dry_run(database=database, person_entity_id=person_id)
    item = _item_for(report, fact_id)
    assert item.classification == Classification.BLOCKED
    assert item.reason_code == ReasonCode.UNKNOWN_EMPLOYMENT_FACT_TYPE

    # This unknown-type fact does not block apply() for other eligible facts,
    # and it is never itself repaired.
    apply_report = await apply(database=database, person_entity_id=person_id)
    apply_item = _item_for(apply_report, fact_id)
    assert apply_item.classification == Classification.BLOCKED
    assert fact_id not in apply_report.repaired_fact_ids

    after = await _get_fact(database, fact_id)
    assert after.employment_scope_entity_id is None


@pytest.mark.asyncio
async def test_source_reference_is_not_used_as_identity(database) -> None:
    person_id, employment_id = await _person_and_employment(database)
    shared_reference = "legacy:shared:reference"
    fact_one = await _create_fact(database, entity_id=employment_id, source_reference=shared_reference)
    other_employment_id = await _create_entity(
        database, entity_type=EntityType.EMPLOYMENT, parent_entity_id=person_id, name="Other Employer"
    )
    fact_two = await _create_fact(database, entity_id=other_employment_id, source_reference=shared_reference)

    report = await apply(database=database, person_entity_id=person_id)

    item_one = _item_for(report, fact_one)
    item_two = _item_for(report, fact_two)
    assert item_one.classification == Classification.REPAIRED
    assert item_two.classification == Classification.REPAIRED
    assert item_one.entity_id != item_two.entity_id

    fact_one_row = await _get_fact(database, fact_one)
    fact_two_row = await _get_fact(database, fact_two)
    assert fact_one_row.employment_scope_entity_id == employment_id
    assert fact_two_row.employment_scope_entity_id == other_employment_id
