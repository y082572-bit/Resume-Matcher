"""Additive, fail-closed P6-B1 schema preflight tests.

Mirrors the P1/P2 conventions in test_truth_legacy_migration_schema.py:
every check runs read-only before any DDL, and an incompatible schema must
never mutate sqlite_master.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.db_engine import (
    explicit_provenance_p6b1_schema_manifest,
    init_models_sync,
    make_sync_engine,
    preflight_explicit_provenance_p1_schema,
    preflight_explicit_provenance_p2_schema,
    preflight_explicit_provenance_p6b1_schema,
)


P6B1_TABLES = {
    "cv_document_artifact_owners", "cv_document_blobs", "cv_docx_proposal_artifacts",
    "cv_docx_validated_snapshots", "cv_confirmed_pdf_artifacts", "cv_document_proposal_slots",
    "cv_document_pdf_slots",
}


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


def test_missing_p6b1_schema_can_be_created(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "empty.db")
    assert preflight_explicit_provenance_p6b1_schema(engine) == "ABSENT_CREATE_REQUIRED"

    init_models_sync(engine)

    assert preflight_explicit_provenance_p6b1_schema(engine) == "P6B1_SCHEMA_READY"
    with engine.connect() as conn:
        tables = set(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    assert P6B1_TABLES <= tables
    engine.dispose()


def test_complete_compatible_schema_passes_preflight_idempotently(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "ready.db")
    init_models_sync(engine)
    manifest_first = explicit_provenance_p6b1_schema_manifest(engine)
    before = _sqlite_master_snapshot(engine)

    assert preflight_explicit_provenance_p6b1_schema(engine) == "P6B1_SCHEMA_READY"
    assert preflight_explicit_provenance_p6b1_schema(engine) == "P6B1_SCHEMA_READY"

    assert _sqlite_master_snapshot(engine) == before
    assert explicit_provenance_p6b1_schema_manifest(engine) == manifest_first
    engine.dispose()


def test_partial_schema_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "partial.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("DROP TABLE cv_document_pdf_slots")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    with pytest.raises(RuntimeError, match="ERROR_PARTIAL_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_column_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_col.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace("media_type VARCHAR(255) NOT NULL, \n\t", "")
    assert mutated != sql
    _replace_table(engine, "cv_document_blobs", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_wrong_type_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_type.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace("byte_size INTEGER NOT NULL", "byte_size TEXT NOT NULL")
    assert mutated != sql
    _replace_table(engine, "cv_document_blobs", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_wrong_nullability_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_null.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace("media_type VARCHAR(255) NOT NULL", "media_type VARCHAR(255)")
    assert mutated != sql
    _replace_table(engine, "cv_document_blobs", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_foreign_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_fk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_docx_proposal_artifacts")
    mutated = sql.replace(
        ", \n\tFOREIGN KEY(blob_sha256) REFERENCES cv_document_blobs (blob_sha256) ON DELETE RESTRICT",
        "",
    )
    assert mutated != sql
    _replace_table(engine, "cv_docx_proposal_artifacts", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_check_constraint_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_check.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace(
        "CONSTRAINT ck_cv_document_blobs_size CHECK (byte_size >= 0), \n\t", ""
    )
    assert mutated != sql
    _replace_table(engine, "cv_document_blobs", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_unique_constraint_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_unique.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_docx_proposal_artifacts")
    mutated = sql.replace(
        "CONSTRAINT uq_cv_docx_proposal_owner_revision UNIQUE (owner_key_fingerprint, proposal_revision), \n\t",
        "",
    )
    assert mutated != sql
    _replace_table(engine, "cv_docx_proposal_artifacts", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_proposal_slot_primary_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_proposal_pk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_proposal_slots")
    mutated = sql.replace("PRIMARY KEY (owner_key_fingerprint), \n\t", "")
    assert mutated != sql
    _replace_table(engine, "cv_document_proposal_slots", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_missing_pdf_slot_primary_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_pdf_pk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_pdf_slots")
    mutated = sql.replace("PRIMARY KEY (owner_key_fingerprint), \n\t", "")
    assert mutated != sql
    _replace_table(engine, "cv_document_pdf_slots", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_wrong_owner_kind_check_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_owner_kind.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_artifact_owners")
    mutated = sql.replace(
        "CHECK (owner_kind IN ('JOB', 'APPLICATION'))",
        "CHECK (owner_kind IN ('JOB', 'APPLICATION', 'OTHER'))",
    )
    assert mutated != sql
    _replace_table(engine, "cv_document_artifact_owners", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_wrong_storage_status_check_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_storage_status.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace(
        "CHECK (storage_status IN ('OK', 'MISSING', 'HASH_MISMATCH', 'QUARANTINED'))",
        "CHECK (storage_status IN ('OK', 'MISSING'))",
    )
    assert mutated != sql
    _replace_table(engine, "cv_document_blobs", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_hash_equals_blob_check_is_verified(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "hash_eq_blob.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_docx_proposal_artifacts")
    mutated = sql.replace(
        "CONSTRAINT ck_cv_docx_proposal_hash_eq_blob CHECK (generated_docx_content_hash = blob_sha256), \n\t",
        "",
    )
    assert mutated != sql
    _replace_table(engine, "cv_docx_proposal_artifacts", mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    engine.dispose()


def test_preflight_is_read_only_on_incompatibility(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "read_only.db")
    init_models_sync(engine)
    sql = _table_sql(engine, "cv_document_blobs")
    mutated = sql.replace("byte_size INTEGER NOT NULL", "byte_size TEXT NOT NULL")
    _replace_table(engine, "cv_document_blobs", mutated)

    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P6B1_SCHEMA"):
        preflight_explicit_provenance_p6b1_schema(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)

    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def test_existing_p1_and_p2_preflight_still_work_after_p6b1(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "p1_p2_unaffected.db")
    init_models_sync(engine)

    assert preflight_explicit_provenance_p1_schema(engine) == "P1_SCHEMA_READY"
    assert preflight_explicit_provenance_p2_schema(engine) == "P2_SCHEMA_READY"
    assert preflight_explicit_provenance_p6b1_schema(engine) == "P6B1_SCHEMA_READY"

    # a second full init_models_sync stays fully read-only/idempotent for all three
    before = _sqlite_master_snapshot(engine)
    init_models_sync(engine)
    after = _sqlite_master_snapshot(engine)
    assert before == after
    engine.dispose()
