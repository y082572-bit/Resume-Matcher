"""Integration tests for P2 apply(): entity/fact materialization end to end."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap
from app.schemas.truth_entity import EntityType, TruthEntityCreate
from app.services.truth_legacy_migrator import (
    PersonEntityTypeMismatchError,
    apply,
)

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _write_library(tmp_path: Path, kategorie: dict, *, suffix: str = "") -> Path:
    payload = {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": kategorie,
    }
    path = tmp_path / f"truth-library{suffix}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _base_kategorie(**extra) -> dict:
    kategorie = {
        "doswiadczenieZawodowe": {"wpisy": []},
        "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
        "reguly": {
            "statusyDoAutomatycznegoCV": [APPROVED],
            "statusyWymagajaceAkceptacji": [],
            "statusyZablokowane": ["NIEJASNE", "USUNIĘTE"],
        },
    }
    kategorie.update(extra)
    return kategorie


async def _entity(database, entity_id: str) -> TruthEntity | None:
    async with database._session() as session:
        return await session.get(TruthEntity, entity_id)


async def _fact(database, fact_id: str) -> TruthFact | None:
    async with database._session() as session:
        return await session.get(TruthFact, fact_id)


async def _map_rows(database, *, category: str | None = None) -> list[TruthLegacyMigrationMap]:
    async with database._session() as session:
        stmt = select(TruthLegacyMigrationMap)
        if category is not None:
            stmt = stmt.where(TruthLegacyMigrationMap.legacy_category == category)
        return list((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_create_person_with_exact_uuid(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(tmp_path, _base_kategorie())
    report = await apply(
        database=database, truth_library_path=path,
        person_entity_id=person_id, legacy_source_id=SOURCE_ID,
    )
    person = await _entity(database, person_id)
    assert person is not None
    assert person.entity_type == "PERSON"
    assert str(report.person_entity_id) == person_id


@pytest.mark.asyncio
async def test_reuse_existing_person(database, tmp_path) -> None:
    person_id = str(uuid4())
    async with database.truth_service.transaction() as repo:
        await repo.create_entity(
            TruthEntityCreate(entity_id=person_id, entity_type=EntityType.PERSON, display_name="Existing Person")
        )
    path = _write_library(tmp_path, _base_kategorie())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    person = await _entity(database, person_id)
    assert person is not None
    assert person.display_name == "Existing Person"  # untouched, not recreated


@pytest.mark.asyncio
async def test_person_type_mismatch_before_any_write(database, tmp_path) -> None:
    async with database.truth_service.transaction() as repo:
        created = await repo.create_entity(
            TruthEntityCreate(entity_type=EntityType.EMPLOYMENT, display_name="Not a person")
        )
        not_person_id = str(created.entity_id)
    path = _write_library(
        tmp_path,
        _base_kategorie(kompetencje={"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]}),
    )
    with pytest.raises(PersonEntityTypeMismatchError):
        await apply(database=database, truth_library_path=path, person_entity_id=not_person_id, legacy_source_id=SOURCE_ID)
    async with database._session() as session:
        entity_count = len((await session.execute(select(TruthEntity))).scalars().all())
        fact_count = len((await session.execute(select(TruthFact))).scalars().all())
        map_count = len((await session.execute(select(TruthLegacyMigrationMap))).scalars().all())
    assert entity_count == 1  # only the pre-seeded EMPLOYMENT entity
    assert fact_count == 0
    assert map_count == 0


@pytest.mark.asyncio
async def test_create_employment_entity_and_facts(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            doswiadczenieZawodowe={
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True,
                        "aktywnosci": ["Built dashboards"],
                        "wynikLiczbowy": "+30%",
                        "skalaOdpowiedzialnosci": "Budget 1M PLN",
                    }
                ]
            }
        ),
    )
    report = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    employment_rows = await _map_rows(database, category="doswiadczenieZawodowe")
    root_row = next(r for r in employment_rows if r.target_entity_id and r.legacy_record_key.count(":") == 2)
    employment_entity = await _entity(database, root_row.target_entity_id)
    assert employment_entity is not None
    assert employment_entity.entity_type == "EMPLOYMENT"
    assert employment_entity.parent_entity_id == person_id

    fact_types = set()
    for row in employment_rows:
        if row.target_fact_id:
            fact = await _fact(database, row.target_fact_id)
            fact_types.add(fact.fact_type)
            # Stage P3.5: every employment fact gets employment_scope_entity_id
            # set to its own owning employment entity on first apply.
            assert fact.entity_id == root_row.target_entity_id
            assert fact.employment_scope_entity_id == root_row.target_entity_id
    assert {"EMPLOYMENT_ROLE", "EMPLOYMENT_ACTIVITY", "EMPLOYMENT_NUMERIC_RESULT", "EMPLOYMENT_RESPONSIBILITY_SCALE"} <= fact_types
    assert report.counts.MIGRATABLE == len(employment_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,entry,expected_entity_type",
    [
        ("kompetencje", {"nazwa": "SQL"}, "SKILL"),
        ("narzedzia", {"nazwa": "Jira"}, "TOOL"),
        ("technologie", {"nazwa": "Python"}, "TECHNOLOGY"),
        ("certyfikaty", {"nazwa": "PMP"}, "CERTIFICATION"),
        ("kursy", {"nazwa": "Data Analysis", "organizator": "Coursera"}, "COURSE"),
        ("wyksztalcenie", {"kierunek": "Informatyka", "uczelnia": "AGH"}, "EDUCATION"),
        ("jezyki", {"jezyk": "Angielski", "poziom": "C1"}, "LANGUAGE"),
    ],
)
async def test_create_named_category_entities(database, tmp_path, category, entry, expected_entity_type) -> None:
    person_id = str(uuid4())
    entry_full = {**entry, "status": APPROVED, "uzywacWCV": True}
    path = _write_library(tmp_path, _base_kategorie(**{category: {"wpisy": [entry_full]}}))
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    rows = await _map_rows(database, category=category)
    assert len(rows) == 1
    entity = await _entity(database, rows[0].target_entity_id)
    assert entity is not None
    assert entity.entity_type == expected_entity_type
    assert entity.parent_entity_id == person_id


@pytest.mark.asyncio
async def test_confirmed_mapping(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(kompetencje={"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]}),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="kompetencje")
    assert rows[0].classification == "MIGRATABLE"
    fact = await _fact(database, rows[0].target_fact_id)
    assert fact.status == "CONFIRMED"
    assert fact.use_in_cv is True
    assert fact.requires_approval is False


@pytest.mark.asyncio
async def test_review_required_mapping(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            kompetencje={"wpisy": [{"nazwa": "SQL", "status": "TRANSFEROWALNE_RYZYKOWNE"}]}
        ),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="kompetencje")
    assert rows[0].classification == "REQUIRES_REVIEW"
    fact = await _fact(database, rows[0].target_fact_id)
    assert fact.status == "REVIEW_REQUIRED"
    assert fact.use_in_cv is False
    assert fact.requires_approval is True


@pytest.mark.asyncio
async def test_rejected_ledger_only_no_entity_no_fact(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(kompetencje={"wpisy": [{"nazwa": "Obsolete", "status": "USUNIĘTE"}]}),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="kompetencje")
    assert len(rows) == 1
    assert rows[0].classification == "REJECTED"
    assert rows[0].target_entity_id is None
    assert rows[0].target_fact_id is None


@pytest.mark.asyncio
async def test_unsupported_ledger_only(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(tmp_path, _base_kategorie(daneOsobowe={"wpisy": [{"pole": "wartość"}]}))
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="daneOsobowe")
    assert len(rows) == 1
    assert rows[0].classification == "UNSUPPORTED"
    assert rows[0].target_entity_id is None
    assert rows[0].target_fact_id is None


@pytest.mark.asyncio
async def test_deferred_policy_categories_produce_no_ledger_row(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(tranferowalneKompetencje={"mapowania": {"a": "b"}}),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    async with database._session() as session:
        all_rows = (await session.execute(select(TruthLegacyMigrationMap))).scalars().all()
    assert all(row.legacy_category != "tranferowalneKompetencje" for row in all_rows)
    assert all(row.legacy_category != "zakazy" for row in all_rows)
    assert all(row.legacy_category != "reguly" for row in all_rows)


@pytest.mark.asyncio
async def test_employment_scope_and_parent_entity_id_correct(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            doswiadczenieZawodowe={
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True, "aktywnosci": ["Did X"],
                    }
                ]
            }
        ),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="doswiadczenieZawodowe")
    root_row = next(r for r in rows if r.legacy_record_key == "doswiadczenieZawodowe:dz-1:1.0")
    employment_entity_id = root_row.target_entity_id
    employment_entity = await _entity(database, employment_entity_id)
    assert employment_entity.parent_entity_id == person_id

    activity_row = next(r for r in rows if r.legacy_record_key != root_row.legacy_record_key)
    activity_fact = await _fact(database, activity_row.target_fact_id)
    assert activity_fact.entity_id == employment_entity_id
    assert activity_fact.transferability == "EMPLOYMENT_SCOPED"
    assert activity_fact.employment_scope_entity_id == employment_entity_id


@pytest.mark.asyncio
async def test_employment_scope_entity_id_stable_across_idempotent_replay(database, tmp_path) -> None:
    """Stage P3.5: re-applying the exact same legacy record must reuse the
    previously assigned target ids and must not touch employment_scope_entity_id
    a second time (no duplicate facts, no duplicate map rows)."""

    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            doswiadczenieZawodowe={
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True,
                    }
                ]
            }
        ),
    )
    first = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    second = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    assert len(first.created_fact_ids) == 1
    assert second.created_fact_ids == []  # reused, not recreated
    rows = await _map_rows(database, category="doswiadczenieZawodowe")
    root_row = next(r for r in rows if r.legacy_record_key == "doswiadczenieZawodowe:dz-1:1.0")
    fact = await _fact(database, root_row.target_fact_id)
    assert fact.employment_scope_entity_id == root_row.target_entity_id
    assert fact.revision == 1


@pytest.mark.asyncio
async def test_source_reference_matches_legacy_record_key(database, tmp_path) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(kompetencje={"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]}),
    )
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    rows = await _map_rows(database, category="kompetencje")
    fact = await _fact(database, rows[0].target_fact_id)
    assert fact.source_reference == f"truth_library:{rows[0].legacy_record_key}"
