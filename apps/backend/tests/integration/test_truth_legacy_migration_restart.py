"""Restart safety and idempotency: repeated --apply runs create zero new
records, IDs stay identical, and a changed parent employment record aborts
the whole run with a full rollback (children can never bypass the gate)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap
from app.services.truth_legacy_migrator import LegacyRecordChangedReviewRequiredError, apply

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _library(*, stanowisko: str = "Analyst", activity: str = "Did X") -> dict:
    return {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": stanowisko,
                        "status": APPROVED, "uzywacWCV": True, "aktywnosci": [activity],
                    }
                ]
            },
            "kompetencje": {"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }


def _write(path: Path, library: dict) -> None:
    path.write_text(json.dumps(library, ensure_ascii=False), encoding="utf-8")


async def _all_ids(database) -> tuple[set[str], set[str], set[str]]:
    async with database._session() as session:
        entity_ids = {row.entity_id for row in (await session.execute(select(TruthEntity))).scalars().all()}
        fact_ids = {row.fact_id for row in (await session.execute(select(TruthFact))).scalars().all()}
        map_ids = {row.migration_map_id for row in (await session.execute(select(TruthLegacyMigrationMap))).scalars().all()}
    return entity_ids, fact_ids, map_ids


@pytest.mark.asyncio
async def test_first_apply_creates_records(database, tmp_path) -> None:
    path = tmp_path / "truth.json"
    _write(path, _library())
    person_id = str(uuid4())
    report = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    assert len(report.created_entity_ids) > 0
    assert len(report.created_fact_ids) > 0
    assert len(report.created_map_row_ids) > 0


@pytest.mark.asyncio
async def test_second_and_third_apply_create_zero_new_records(database, tmp_path) -> None:
    path = tmp_path / "truth.json"
    _write(path, _library())
    person_id = str(uuid4())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    report2 = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    report3 = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    for report in (report2, report3):
        assert report.created_entity_ids == []
        assert report.created_fact_ids == []
        assert report.created_map_row_ids == []
        assert report.reused_map_row_count > 0


@pytest.mark.asyncio
async def test_ids_identical_across_repeated_applies(database, tmp_path) -> None:
    path = tmp_path / "truth.json"
    _write(path, _library())
    person_id = str(uuid4())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    ids_after_first = await _all_ids(database)

    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    ids_after_second = await _all_ids(database)

    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    ids_after_third = await _all_ids(database)

    assert ids_after_first == ids_after_second == ids_after_third


@pytest.mark.asyncio
async def test_changed_parent_employment_fingerprint_aborts_migration(database, tmp_path) -> None:
    path = tmp_path / "truth.json"
    _write(path, _library(stanowisko="Analyst"))
    person_id = str(uuid4())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    ids_before = await _all_ids(database)

    _write(path, _library(stanowisko="Senior Analyst"))  # parent content changed
    with pytest.raises(LegacyRecordChangedReviewRequiredError):
        await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    ids_after = await _all_ids(database)
    assert ids_after == ids_before  # full rollback: nothing added, nothing removed


@pytest.mark.asyncio
async def test_changed_child_activity_text_cannot_bypass_parent_gate(database, tmp_path) -> None:
    """Even though a changed activity alone would just become a *new* content
    identity (see test_truth_legacy_identity), the parent's own root record
    fingerprint still gates the whole employment when ITS content changed
    too — a child change never gets to "quietly" apply on a stale parent."""

    path = tmp_path / "truth.json"
    _write(path, _library(stanowisko="Analyst", activity="Original activity"))
    person_id = str(uuid4())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    ids_before = await _all_ids(database)

    # Change BOTH the parent (stanowisko) and the child (activity) at once.
    _write(path, _library(stanowisko="Senior Analyst", activity="New activity"))
    with pytest.raises(LegacyRecordChangedReviewRequiredError):
        await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    ids_after = await _all_ids(database)
    assert ids_after == ids_before


@pytest.mark.asyncio
async def test_full_rollback_on_conflict_leaves_other_categories_untouched(database, tmp_path) -> None:
    path = tmp_path / "truth.json"
    _write(path, _library())
    person_id = str(uuid4())
    await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    async with database._session() as session:
        skill_rows_before = list(
            (await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.legacy_category == "kompetencje")
            )).scalars()
        )
    assert len(skill_rows_before) == 1

    _write(path, _library(stanowisko="Changed"))
    with pytest.raises(LegacyRecordChangedReviewRequiredError):
        await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)

    async with database._session() as session:
        skill_rows_after = list(
            (await session.execute(
                select(TruthLegacyMigrationMap).where(TruthLegacyMigrationMap.legacy_category == "kompetencje")
            )).scalars()
        )
    # Even though kompetencje itself didn't change, the abort happened inside
    # the SAME transaction, so it is also unaffected — no duplicate/extra rows.
    assert len(skill_rows_after) == 1
    assert {r.migration_map_id for r in skill_rows_before} == {r.migration_map_id for r in skill_rows_after}
