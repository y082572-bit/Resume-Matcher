"""P2 never mutates the legacy Truth Library JSON, in either mode, and
dry-run never touches the database at all."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import Database
from app.db_engine import make_sync_engine
from app.services.truth_legacy_migrator import apply, dry_run

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _library() -> dict:
    return {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True, "aktywnosci": ["Did X"],
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


def _write(path: Path) -> None:
    path.write_text(json.dumps(_library(), ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_master_snapshot(engine):
    with engine.connect() as connection:
        rows = [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def test_json_sha_before_and_after_dry_run_identical(tmp_path: Path) -> None:
    path = tmp_path / "truth.json"
    _write(path)
    before = _sha256(path)
    before_mtime = path.stat().st_mtime_ns

    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)

    assert _sha256(path) == before
    assert path.stat().st_mtime_ns == before_mtime


@pytest.mark.asyncio
async def test_json_sha_before_and_after_apply_identical(database, tmp_path: Path) -> None:
    path = tmp_path / "truth.json"
    _write(path)
    before = _sha256(path)

    await apply(
        database=database, truth_library_path=path,
        person_entity_id=str(uuid4()), legacy_source_id=SOURCE_ID,
    )

    assert _sha256(path) == before


def test_dry_run_never_creates_a_database(tmp_path: Path) -> None:
    """dry_run() takes no database/engine argument at all — there is no
    sqlite file for it to create, by construction."""

    path = tmp_path / "truth.json"
    _write(path)
    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)
    assert not (tmp_path / "app.db").exists()
    assert list(tmp_path.iterdir()) == [path]


def test_dry_run_db_sqlite_master_identical_when_engine_exists_untouched(tmp_path: Path) -> None:
    """If a database file already exists nearby, dry-run must not touch it —
    simulated here by snapshotting an unrelated engine before/after dry-run."""

    path = tmp_path / "truth.json"
    _write(path)
    engine = make_sync_engine(tmp_path / "unrelated.db")
    from app.db_engine import init_models_sync
    init_models_sync(engine)
    before = _sqlite_master_snapshot(engine)

    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)

    assert _sqlite_master_snapshot(engine) == before
    engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_row_counts_identical_to_pre_existing_db(database, tmp_path: Path) -> None:
    path = tmp_path / "truth.json"
    _write(path)
    # Force the database to initialize (creates schema) without running any
    # migration, to prove dry-run leaves row counts untouched afterwards.
    async with database.truth_service.transaction():
        pass
    from sqlalchemy import select, func
    from app.models import TruthEntity, TruthFact, TruthLegacyMigrationMap

    async def counts():
        async with database._session() as session:
            entities = (await session.execute(select(func.count()).select_from(TruthEntity))).scalar_one()
            facts = (await session.execute(select(func.count()).select_from(TruthFact))).scalar_one()
            maps = (await session.execute(select(func.count()).select_from(TruthLegacyMigrationMap))).scalar_one()
        return entities, facts, maps

    before = await counts()
    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)
    after = await counts()
    assert before == after == (0, 0, 0)


def test_json_file_mtime_unchanged_after_dry_run(tmp_path: Path) -> None:
    path = tmp_path / "truth.json"
    _write(path)
    before_mtime = path.stat().st_mtime_ns
    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)
    dry_run(truth_library_path=path, legacy_source_id=SOURCE_ID)
    assert path.stat().st_mtime_ns == before_mtime
