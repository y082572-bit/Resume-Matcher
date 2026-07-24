"""Additive, fail-closed schema preflight tests for Explicit Provenance
Stage P6-B2A-I1's ``candidate_identity_bindings`` table, exercised through
the real production startup path (``make_sync_engine`` + ``init_models_sync``)
-- never a hand-rolled engine.

Mirrors ``test_final_docx_snapshot_schema_preflight.py``'s conventions:
mutates the table's *actual*, ORM-generated ``CREATE TABLE`` SQL text
in-place, re-installs it, and proves the independent, hand-written manifest
in ``candidate_identity_binding_schema_manifest.py`` still catches the
drift -- without ever importing that manifest's constants from the ORM.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.db_engine import (
    init_models_sync,
    make_sync_engine,
    preflight_explicit_provenance_candidate_identity_binding_schema,
)
from app.services.candidate_identity_binding_schema_manifest import (
    CANDIDATE_IDENTITY_BINDING_TABLE_NAME,
    candidate_identity_binding_schema_manifest,
    collect_actual_candidate_identity_binding_schema,
)


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


def _table_sql(engine, table_name: str) -> str:
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).scalar_one()


def _replace_table(engine, table_name: str, new_sql: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(f"DROP TABLE {table_name}")
        conn.exec_driver_sql(new_sql)
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_missing_table_can_be_created(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "empty.db")
    assert (
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
        == "ABSENT_CREATE_REQUIRED"
    )

    init_models_sync(engine)

    assert (
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
        == "CANDIDATE_IDENTITY_BINDING_SCHEMA_READY"
    )
    with engine.connect() as conn:
        tables = set(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    assert CANDIDATE_IDENTITY_BINDING_TABLE_NAME in tables
    engine.dispose()


def test_complete_compatible_schema_passes_preflight_idempotently(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "ready.db")
    init_models_sync(engine)
    manifest_first = candidate_identity_binding_schema_manifest(engine)
    before = _sqlite_master_snapshot(engine)

    assert (
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
        == "CANDIDATE_IDENTITY_BINDING_SCHEMA_READY"
    )
    assert (
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
        == "CANDIDATE_IDENTITY_BINDING_SCHEMA_READY"
    )

    assert _sqlite_master_snapshot(engine) == before
    assert candidate_identity_binding_schema_manifest(engine) == manifest_first
    engine.dispose()


def test_missing_column_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_col.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace("created_at VARCHAR NOT NULL, \n\t", "")
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_wrong_type_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_type.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace("master_resume_id VARCHAR NOT NULL", "master_resume_id INTEGER NOT NULL")
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_wrong_nullability_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_null.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace(
        "binding_schema_version VARCHAR(64) NOT NULL", "binding_schema_version VARCHAR(64)"
    )
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_wrong_primary_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_pk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace("PRIMARY KEY (binding_fingerprint), \n\t", "")
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_missing_foreign_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_fk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace(
        ", \n\tFOREIGN KEY(master_resume_id) REFERENCES resumes (resume_id) ON DELETE RESTRICT",
        "",
    )
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_wrong_foreign_key_target_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_fk_target.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace(
        "FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE RESTRICT",
        "FOREIGN KEY(person_entity_id) REFERENCES resumes (resume_id) ON DELETE RESTRICT",
    )
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_missing_check_constraint_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_check.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace(
        "CONSTRAINT ck_candidate_identity_bindings_schema_version CHECK "
        "(binding_schema_version = 'candidate-identity-binding-schema-v1'), \n\t",
        "",
    )
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_missing_unique_on_person_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_unique_person.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace(
        "CONSTRAINT uq_candidate_identity_bindings_person UNIQUE (person_entity_id), \n\t", ""
    )
    assert mutated != sql
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_extra_column_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "extra_col.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"ALTER TABLE {CANDIDATE_IDENTITY_BINDING_TABLE_NAME} ADD COLUMN extra_column VARCHAR"
        )

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_missing_trigger_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_trigger.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_candidate_identity_bindings_no_delete")

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_trigger_drift_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "trigger_drift.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_candidate_identity_bindings_immutable")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_candidate_identity_bindings_immutable
            BEFORE UPDATE ON candidate_identity_bindings
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_extra_trigger_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "extra_trigger.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_candidate_identity_bindings_extra
            AFTER INSERT ON candidate_identity_bindings
            BEGIN
                SELECT 1;
            END
            """
        )

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    engine.dispose()


def test_failed_preflight_mutates_nothing(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "read_only.db")
    init_models_sync(engine)
    sql = _table_sql(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    mutated = sql.replace("master_resume_id VARCHAR NOT NULL", "master_resume_id INTEGER NOT NULL")
    _replace_table(engine, CANDIDATE_IDENTITY_BINDING_TABLE_NAME, mutated)

    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_explicit_provenance_candidate_identity_binding_schema(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)

    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def test_collect_actual_schema_matches_orm_rendering(tmp_path) -> None:
    """Sanity check that the collected actual schema matches the ORM's own
    rendering exactly -- proving the two independently-authored
    descriptions (hand-written manifest vs. ORM model) currently agree."""

    engine = make_sync_engine(tmp_path / "cross_check.db")
    init_models_sync(engine)
    with engine.connect() as conn:
        actual = collect_actual_candidate_identity_binding_schema(conn)
    assert len(actual["columns"]) == 5
    assert len(actual["foreign_keys"]) == 2
    assert len(actual["check_constraints"]) == 3
    assert actual["unique_indexed_columns"] == frozenset({"person_entity_id", "master_resume_id"})
    assert actual["non_unique_indexed_columns"] == frozenset()
    assert len(actual["triggers"]) == 4
    engine.dispose()
