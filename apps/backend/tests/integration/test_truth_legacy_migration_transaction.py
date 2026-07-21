"""P2 apply() runs in exactly one transaction: any error anywhere causes a
full rollback with zero partial writes, including a freshly created PERSON."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap
from app.services.truth_legacy_migrator import apply
from app.services.truth_repository import TruthRepository

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _write_library(tmp_path: Path, kategorie: dict) -> Path:
    payload = {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": kategorie,
    }
    path = tmp_path / "truth-library.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _base_kategorie(**extra) -> dict:
    kategorie = {
        "doswiadczenieZawodowe": {"wpisy": []},
        "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
        "reguly": {
            "statusyDoAutomatycznegoCV": [APPROVED],
            "statusyWymagajaceAkceptacji": [],
            "statusyZablokowane": [],
        },
    }
    kategorie.update(extra)
    return kategorie


async def _counts(database) -> tuple[int, int, int]:
    async with database._session() as session:
        entities = len((await session.execute(select(TruthEntity))).scalars().all())
        facts = len((await session.execute(select(TruthFact))).scalars().all())
        maps = len((await session.execute(select(TruthLegacyMigrationMap))).scalars().all())
    return entities, facts, maps


@pytest.mark.asyncio
async def test_injected_error_mid_transaction_zero_partial_writes(database, tmp_path, monkeypatch) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            kompetencje={
                "wpisy": [
                    {"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True},
                    {"nazwa": "TRIGGER_FAILURE", "status": APPROVED, "uzywacWCV": True},
                    {"nazwa": "Excel", "status": APPROVED, "uzywacWCV": True},
                ]
            }
        ),
    )

    original_create_entity = TruthRepository.create_entity

    async def flaky_create_entity(self, value):
        if value.display_name == "TRIGGER_FAILURE":
            raise RuntimeError("INJECTED_FAILURE")
        return await original_create_entity(self, value)

    monkeypatch.setattr(TruthRepository, "create_entity", flaky_create_entity)

    with pytest.raises(RuntimeError, match="INJECTED_FAILURE"):
        await apply(
            database=database, truth_library_path=path,
            person_entity_id=person_id, legacy_source_id=SOURCE_ID,
        )

    entities, facts, maps = await _counts(database)
    assert entities == 0  # includes the PERSON entity created earlier in the SAME transaction
    assert facts == 0
    assert maps == 0


@pytest.mark.asyncio
async def test_person_creation_rolled_back_when_later_write_fails(database, tmp_path, monkeypatch) -> None:
    """The PERSON entity is created first, inside the same transaction as
    everything else — a later failure must undo it too, not just the record
    that actually failed."""

    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            kompetencje={"wpisy": [{"nazwa": "TRIGGER_FAILURE", "status": APPROVED, "uzywacWCV": True}]}
        ),
    )

    original_create_entity = TruthRepository.create_entity

    async def flaky_create_entity(self, value):
        if value.display_name == "TRIGGER_FAILURE":
            raise RuntimeError("INJECTED_FAILURE")
        return await original_create_entity(self, value)

    monkeypatch.setattr(TruthRepository, "create_entity", flaky_create_entity)

    with pytest.raises(RuntimeError, match="INJECTED_FAILURE"):
        await apply(
            database=database, truth_library_path=path,
            person_entity_id=person_id, legacy_source_id=SOURCE_ID,
        )

    async with database._session() as session:
        person = await session.get(TruthEntity, person_id)
    assert person is None


@pytest.mark.asyncio
async def test_injected_error_on_fact_creation_rolls_back_its_own_entity_too(database, tmp_path, monkeypatch) -> None:
    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            kompetencje={"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]}
        ),
    )

    original_create_fact = TruthRepository.create_fact

    async def flaky_create_fact(self, value):
        raise RuntimeError("INJECTED_FACT_FAILURE")

    monkeypatch.setattr(TruthRepository, "create_fact", flaky_create_fact)

    with pytest.raises(RuntimeError, match="INJECTED_FACT_FAILURE"):
        await apply(
            database=database, truth_library_path=path,
            person_entity_id=person_id, legacy_source_id=SOURCE_ID,
        )

    entities, facts, maps = await _counts(database)
    assert entities == 0  # PERSON + the SKILL entity created just before the fact call
    assert facts == 0
    assert maps == 0


@pytest.mark.asyncio
async def test_successful_apply_after_a_prior_failed_attempt_starts_clean(database, tmp_path, monkeypatch) -> None:
    """A rolled-back attempt must leave no residue that changes the outcome
    of a subsequent, successful attempt."""

    person_id = str(uuid4())
    path = _write_library(
        tmp_path,
        _base_kategorie(
            kompetencje={"wpisy": [{"nazwa": "TRIGGER_FAILURE", "status": APPROVED, "uzywacWCV": True}]}
        ),
    )

    original_create_entity = TruthRepository.create_entity

    async def flaky_create_entity(self, value):
        if value.display_name == "TRIGGER_FAILURE":
            raise RuntimeError("INJECTED_FAILURE")
        return await original_create_entity(self, value)

    monkeypatch.setattr(TruthRepository, "create_entity", flaky_create_entity)
    with pytest.raises(RuntimeError, match="INJECTED_FAILURE"):
        await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    monkeypatch.undo()

    report = await apply(database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID)
    assert report.counts.MIGRATABLE == 1
    entities, facts, maps = await _counts(database)
    assert entities == 2  # PERSON + SKILL
    assert facts == 1
    assert maps == 1
