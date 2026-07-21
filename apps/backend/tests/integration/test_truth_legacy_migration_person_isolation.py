"""Two PERSON identities migrating the same logical legacy data must remain
fully isolated: no shared entities, no cross-employment scope leakage, and
ledger rows partitioned by person_entity_id."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap
from app.services.truth_legacy_migrator import apply

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _library(*, company: str) -> dict:
    return {
        "meta": {"kandidat": "Synthetic Candidate", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1", "firma": company, "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True, "aktywnosci": ["Did synthetic work"],
                    }
                ]
            },
            "kompetencje": {"wpisy": [{"nazwa": "SyntheticSkill", "status": APPROVED, "uzywacWCV": True}]},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }


def _write(tmp_path: Path, name: str, library: dict) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(library, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_two_persons_same_logical_data_stay_isolated(database, tmp_path) -> None:
    person_a = str(uuid4())
    person_b = str(uuid4())
    path_a = _write(tmp_path, "lib_a", _library(company="Company Alpha"))
    path_b = _write(tmp_path, "lib_b", _library(company="Company Beta"))

    report_a = await apply(database=database, truth_library_path=path_a, person_entity_id=person_a, legacy_source_id=SOURCE_ID)
    report_b = await apply(database=database, truth_library_path=path_b, person_entity_id=person_b, legacy_source_id=SOURCE_ID)

    assert set(str(x) for x in report_a.created_entity_ids).isdisjoint(
        set(str(x) for x in report_b.created_entity_ids)
    )
    assert set(str(x) for x in report_a.created_fact_ids).isdisjoint(
        set(str(x) for x in report_b.created_fact_ids)
    )
    assert set(str(x) for x in report_a.created_map_row_ids).isdisjoint(
        set(str(x) for x in report_b.created_map_row_ids)
    )


@pytest.mark.asyncio
async def test_no_cross_entity_facts_between_persons(database, tmp_path) -> None:
    person_a = str(uuid4())
    person_b = str(uuid4())
    path_a = _write(tmp_path, "lib_a", _library(company="Company Alpha"))
    path_b = _write(tmp_path, "lib_b", _library(company="Company Beta"))

    await apply(database=database, truth_library_path=path_a, person_entity_id=person_a, legacy_source_id=SOURCE_ID)
    await apply(database=database, truth_library_path=path_b, person_entity_id=person_b, legacy_source_id=SOURCE_ID)

    async with database._session() as session:
        entities = (await session.execute(select(TruthEntity))).scalars().all()
        facts = (await session.execute(select(TruthFact))).scalars().all()

    entities_by_id = {e.entity_id: e for e in entities}
    for fact in facts:
        owner = entities_by_id[fact.entity_id]
        root = owner
        while root.parent_entity_id is not None:
            root = entities_by_id[root.parent_entity_id]
        assert root.entity_id in (person_a, person_b)
        # The fact's ultimate root PERSON must be the same one that owns the
        # employment/skill entity — never the other person's.


@pytest.mark.asyncio
async def test_no_cross_employment_scope_between_persons(database, tmp_path) -> None:
    person_a = str(uuid4())
    person_b = str(uuid4())
    path_a = _write(tmp_path, "lib_a", _library(company="Company Alpha"))
    path_b = _write(tmp_path, "lib_b", _library(company="Company Beta"))

    await apply(database=database, truth_library_path=path_a, person_entity_id=person_a, legacy_source_id=SOURCE_ID)
    await apply(database=database, truth_library_path=path_b, person_entity_id=person_b, legacy_source_id=SOURCE_ID)

    async with database._session() as session:
        employments = (
            await session.execute(select(TruthEntity).where(TruthEntity.entity_type == "EMPLOYMENT"))
        ).scalars().all()
    assert len(employments) == 2
    parents = {e.parent_entity_id for e in employments}
    assert parents == {person_a, person_b}


@pytest.mark.asyncio
async def test_map_rows_partitioned_by_person(database, tmp_path) -> None:
    person_a = str(uuid4())
    person_b = str(uuid4())
    path_a = _write(tmp_path, "lib_a", _library(company="Company Alpha"))
    path_b = _write(tmp_path, "lib_b", _library(company="Company Beta"))

    await apply(database=database, truth_library_path=path_a, person_entity_id=person_a, legacy_source_id=SOURCE_ID)
    await apply(database=database, truth_library_path=path_b, person_entity_id=person_b, legacy_source_id=SOURCE_ID)

    async with database._session() as session:
        rows_a = (
            await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.person_entity_id == person_a)
            )
        ).scalars().all()
        rows_b = (
            await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.person_entity_id == person_b)
            )
        ).scalars().all()
    assert len(rows_a) > 0
    assert len(rows_b) > 0
    assert {r.migration_map_id for r in rows_a}.isdisjoint({r.migration_map_id for r in rows_b})


@pytest.mark.asyncio
async def test_identical_logical_legacy_data_under_different_persons_remains_isolated(database, tmp_path) -> None:
    """Same company/role/skill text under two different person ids must
    still produce two fully separate sets of entities/facts/ledger rows —
    content equality never implies identity equality across persons."""

    person_a = str(uuid4())
    person_b = str(uuid4())
    identical_library = _library(company="Same Company")
    path_a = _write(tmp_path, "lib_identical_a", identical_library)
    path_b = _write(tmp_path, "lib_identical_b", identical_library)

    report_a = await apply(database=database, truth_library_path=path_a, person_entity_id=person_a, legacy_source_id=SOURCE_ID)
    report_b = await apply(database=database, truth_library_path=path_b, person_entity_id=person_b, legacy_source_id=SOURCE_ID)

    assert report_a.counts.MIGRATABLE == report_b.counts.MIGRATABLE
    assert set(str(x) for x in report_a.created_entity_ids).isdisjoint(
        set(str(x) for x in report_b.created_entity_ids)
    )

    async with database._session() as session:
        maps_a = (
            await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.person_entity_id == person_a)
            )
        ).scalars().all()
        maps_b = (
            await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.person_entity_id == person_b)
            )
        ).scalars().all()
    # Same identical content -> same legacy_record_key set -> same fingerprints,
    # but distinct migration_map_id and target ids per person.
    assert {r.legacy_record_key for r in maps_a} == {r.legacy_record_key for r in maps_b}
    assert {r.migration_map_id for r in maps_a}.isdisjoint({r.migration_map_id for r in maps_b})
    # Only rows that actually own an entity (owner == "SELF") set
    # target_entity_id; children reuse their parent's and leave it NULL, so
    # NULL legitimately appears on both sides and must be excluded here.
    entity_ids_a = {r.target_entity_id for r in maps_a if r.target_entity_id is not None}
    entity_ids_b = {r.target_entity_id for r in maps_b if r.target_entity_id is not None}
    assert entity_ids_a and entity_ids_b
    assert entity_ids_a.isdisjoint(entity_ids_b)
