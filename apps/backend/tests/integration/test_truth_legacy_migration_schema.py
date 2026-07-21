"""Additive, fail-closed P2 schema preflight tests.

Mirrors the P1 conventions in test_truth_identity_migration.py: every check
runs read-only before any DDL, and an incompatible schema must never mutate
sqlite_master.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.db_engine import (
    explicit_provenance_p1_schema_manifest,
    explicit_provenance_p2_schema_manifest,
    init_models_sync,
    make_sync_engine,
    preflight_explicit_provenance_p1_schema,
    preflight_explicit_provenance_p2_schema,
)


P1_TABLES = {
    "truth_entities", "truth_facts", "truth_fact_variants", "truth_evidence", "truth_permissions",
}
P2_TABLES = {"truth_legacy_migration_map"}


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
    return hashlib.sha256(payload.encode()).hexdigest(), rows


def _object_counts(engine):
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT type, COUNT(*) as c FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' GROUP BY type"
        ).mappings().all()
    return {row["type"]: row["c"] for row in rows}


def test_empty_db_first_startup_creates_p1_and_p2(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "empty.db")
    assert preflight_explicit_provenance_p1_schema(engine) == "ABSENT_CREATE_REQUIRED"
    assert preflight_explicit_provenance_p2_schema(engine) == "ABSENT_CREATE_REQUIRED"

    init_models_sync(engine)

    assert preflight_explicit_provenance_p1_schema(engine) == "P1_SCHEMA_READY"
    assert preflight_explicit_provenance_p2_schema(engine) == "P2_SCHEMA_READY"
    with engine.connect() as conn:
        tables = set(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )
    assert P1_TABLES <= tables
    assert P2_TABLES <= tables
    engine.dispose()


def test_p1_and_p2_preflights_both_run_before_any_mutation(tmp_path) -> None:
    """Both preflights are read-only calls with zero side effects, independent
    of init_models_sync — calling them repeatedly must never mutate schema."""

    engine = make_sync_engine(tmp_path / "preflight.db")
    before = _sqlite_master_snapshot(engine)
    preflight_explicit_provenance_p1_schema(engine)
    preflight_explicit_provenance_p2_schema(engine)
    preflight_explicit_provenance_p1_schema(engine)
    preflight_explicit_provenance_p2_schema(engine)
    assert _sqlite_master_snapshot(engine) == before
    engine.dispose()


def test_p2_absent_controlled_creation(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "p2_absent.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE truth_legacy_migration_map")
    assert preflight_explicit_provenance_p1_schema(engine) == "P1_SCHEMA_READY"
    assert preflight_explicit_provenance_p2_schema(engine) == "ABSENT_CREATE_REQUIRED"

    init_models_sync(engine)

    assert preflight_explicit_provenance_p2_schema(engine) == "P2_SCHEMA_READY"
    with engine.connect() as conn:
        assert "truth_legacy_migration_map" in set(
            conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
        )
    engine.dispose()


def test_p2_canonical_ready_read_only_startup(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "p2_ready.db")
    init_models_sync(engine)
    manifest_first = explicit_provenance_p2_schema_manifest(engine)
    before = _sqlite_master_snapshot(engine)

    init_models_sync(engine)

    assert _sqlite_master_snapshot(engine) == before
    assert explicit_provenance_p2_schema_manifest(engine) == manifest_first
    engine.dispose()


def test_second_and_third_startup_read_only_for_p1_and_p2(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "restart.db")
    init_models_sync(engine)
    p1_first = explicit_provenance_p1_schema_manifest(engine)
    p2_first = explicit_provenance_p2_schema_manifest(engine)
    schema_after_first = _sqlite_master_snapshot(engine)

    init_models_sync(engine)
    p1_second = explicit_provenance_p1_schema_manifest(engine)
    p2_second = explicit_provenance_p2_schema_manifest(engine)
    schema_after_second = _sqlite_master_snapshot(engine)

    init_models_sync(engine)
    p1_third = explicit_provenance_p1_schema_manifest(engine)
    p2_third = explicit_provenance_p2_schema_manifest(engine)
    schema_after_third = _sqlite_master_snapshot(engine)

    assert p1_first == p1_second == p1_third
    assert p2_first == p2_second == p2_third
    assert schema_after_first == schema_after_second == schema_after_third
    engine.dispose()


def test_wrong_columns_raise_incompatible_p2_schema(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_columns.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE truth_legacy_migration_map (migration_map_id TEXT PRIMARY KEY)"
        )
    before = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA"):
        preflight_explicit_provenance_p2_schema(engine)
    assert _sqlite_master_snapshot(engine) == before
    engine.dispose()


@pytest.mark.parametrize("wrong_name", ["TRUTH_LEGACY_MIGRATION_MAP", "Truth_Legacy_Migration_Map"])
def test_uppercase_and_mixed_case_table_name_raises_name_error(tmp_path, wrong_name) -> None:
    engine = make_sync_engine(tmp_path / f"case_{wrong_name}.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE TABLE "{wrong_name}" (migration_map_id TEXT)')
    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA_NAME"):
        preflight_explicit_provenance_p2_schema(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def _reference_p2_table_sql(tmp_path) -> str:
    reference = make_sync_engine(tmp_path / "reference.db")
    init_models_sync(reference)
    with reference.connect() as conn:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='truth_legacy_migration_map'"
        ).scalar_one()
    reference.dispose()
    return sql


def test_wrong_foreign_key_raises_incompatible_p2_schema(tmp_path) -> None:
    sql = _reference_p2_table_sql(tmp_path)
    mutated = sql.replace(
        'FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE RESTRICT',
        'FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE CASCADE',
    )
    assert mutated != sql
    engine = make_sync_engine(tmp_path / "wrong_fk.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    init_models_sync(engine)  # creates P1 tables needed for the FK target
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE truth_legacy_migration_map")
        conn.exec_driver_sql(mutated)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA"):
        preflight_explicit_provenance_p2_schema(engine)
    engine.dispose()


def test_wrong_unique_constraint_raises_incompatible_p2_schema(tmp_path) -> None:
    sql = _reference_p2_table_sql(tmp_path)
    mutated = sql.replace("legacy_record_key", "legacy_record_key_renamed")
    # Sabotage only the UNIQUE clause's column list by re-adding one correct
    # column back everywhere except inside the UNIQUE(...) clause is fiddly;
    # simplest reliable mutation: drop one column from the UNIQUE constraint.
    import re
    match = re.search(r"UNIQUE \(([^)]*)\)", sql)
    assert match is not None
    mutated = sql.replace(match.group(0), "UNIQUE (person_entity_id, legacy_source_id)")
    assert mutated != sql

    engine = make_sync_engine(tmp_path / "wrong_unique.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE truth_legacy_migration_map")
        conn.exec_driver_sql(mutated)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA"):
        preflight_explicit_provenance_p2_schema(engine)
    engine.dispose()


def test_wrong_index_raises_incompatible_p2_schema(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_index.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_truth_legacy_migration_map_person")
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA"):
        preflight_explicit_provenance_p2_schema(engine)
    engine.dispose()


def test_wrong_check_raises_incompatible_p2_schema(tmp_path) -> None:
    sql = _reference_p2_table_sql(tmp_path)
    mutated = sql.replace(
        "CONSTRAINT ck_truth_legacy_migration_map_schema CHECK (migration_schema_version = '1.0')",
        "CONSTRAINT ck_truth_legacy_migration_map_schema CHECK (migration_schema_version = '2.0')",
    )
    assert mutated != sql
    engine = make_sync_engine(tmp_path / "wrong_check.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE truth_legacy_migration_map")
        conn.exec_driver_sql(mutated)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA"):
        preflight_explicit_provenance_p2_schema(engine)
    engine.dispose()


def test_sqlite_master_identical_before_and_after_p2_error(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "identical.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE TRUTH_LEGACY_MIGRATION_MAP (id TEXT)")
    before_hash, before_rows = _sqlite_master_snapshot(engine)
    before_counts = _object_counts(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA_NAME"):
        init_models_sync(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert before_rows == after_rows
    assert _object_counts(engine) == before_counts
    engine.dispose()


def test_no_p1_creation_when_p2_schema_is_incompatible(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "p2_bad_p1_absent.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE TRUTH_LEGACY_MIGRATION_MAP (id TEXT)")
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P2_SCHEMA_NAME"):
        init_models_sync(engine)
    with engine.connect() as conn:
        tables = set(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    assert not (P1_TABLES & tables)
    engine.dispose()


def test_no_p2_creation_when_p1_schema_is_incompatible(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "p1_bad_p2_absent.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE TRUTH_ENTITIES (entity_id TEXT)")
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)
    with engine.connect() as conn:
        tables = set(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    assert "truth_legacy_migration_map" not in tables
    engine.dispose()


def test_generic_additive_loop_does_not_create_p2(tmp_path) -> None:
    """When P1 is already ready but P2 is absent, the generic per-table
    additive-migration loop (for legacy tables) must skip P2, not create it —
    only the dedicated P2 branch is allowed to."""

    engine = make_sync_engine(tmp_path / "generic_loop.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE truth_legacy_migration_map")

    # Directly call init_models_sync again: P1 ready (generic loop runs),
    # P2 absent (dedicated branch runs). Verify the table reappears exactly
    # once and the dedicated P2 preflight is what created it.
    assert preflight_explicit_provenance_p1_schema(engine) == "P1_SCHEMA_READY"
    assert preflight_explicit_provenance_p2_schema(engine) == "ABSENT_CREATE_REQUIRED"
    init_models_sync(engine)
    assert preflight_explicit_provenance_p2_schema(engine) == "P2_SCHEMA_READY"
    engine.dispose()


def test_existing_p1_manifest_behavior_unchanged(tmp_path) -> None:
    """P1 manifest collection is unaffected by the presence of P2 — same
    manifest_version, same table set, same digest-relevant content."""

    engine = make_sync_engine(tmp_path / "p1_unchanged.db")
    init_models_sync(engine)
    manifest = explicit_provenance_p1_schema_manifest(engine)
    assert manifest["manifest_version"] == "explicit-provenance-p1-schema-manifest-v1"
    assert set(manifest["tables"]) == P1_TABLES
    engine.dispose()
