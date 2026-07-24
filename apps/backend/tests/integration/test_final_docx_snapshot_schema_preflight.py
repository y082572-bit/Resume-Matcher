"""Additive, fail-closed schema preflight tests for the Explicit Provenance
P6-B1 SQL storage addendum's ``cv_docx_final_snapshots`` table.

Mirrors the P6-B1 preflight conventions (``test_p6b1_schema_preflight.py``),
but exercises the *independent*, hand-written manifest in
``cv_document_final_snapshot_schema_manifest.py`` -- never the
``Base.metadata.create_all``-on-an-in-memory-engine reference technique the
P1/P2/P6-B1 preflights use.
"""

from __future__ import annotations

import json
import hashlib

import pytest

from app.db_engine import (
    init_models_sync,
    make_sync_engine,
    preflight_explicit_provenance_final_docx_snapshot_schema,
)
from app.services.cv_document_final_snapshot_schema_manifest import (
    FINAL_SNAPSHOT_TABLE_NAME,
    collect_actual_final_snapshot_schema,
    final_docx_snapshot_schema_manifest,
    preflight_final_docx_snapshot_schema,
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


def test_missing_addendum_can_be_created(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "empty.db")
    assert preflight_explicit_provenance_final_docx_snapshot_schema(engine) == "ABSENT_CREATE_REQUIRED"

    init_models_sync(engine)

    assert (
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
        == "FINAL_DOCX_SNAPSHOT_SCHEMA_READY"
    )
    with engine.connect() as conn:
        tables = set(conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    assert FINAL_SNAPSHOT_TABLE_NAME in tables
    engine.dispose()


def test_complete_compatible_schema_passes_preflight_idempotently(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "ready.db")
    init_models_sync(engine)
    manifest_first = final_docx_snapshot_schema_manifest(engine)
    before = _sqlite_master_snapshot(engine)

    assert (
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
        == "FINAL_DOCX_SNAPSHOT_SCHEMA_READY"
    )
    assert (
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
        == "FINAL_DOCX_SNAPSHOT_SCHEMA_READY"
    )

    assert _sqlite_master_snapshot(engine) == before
    assert final_docx_snapshot_schema_manifest(engine) == manifest_first
    engine.dispose()


def test_missing_column_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_col.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    # created_at carries no CHECK/FK of its own, so removing it doesn't
    # require also stripping dependent constraints -- keeps this test
    # scoped to exactly one thing (a missing column).
    mutated = sql.replace("created_at VARCHAR NOT NULL, \n\t", "")
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_type_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_type.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace("source_proposal_revision INTEGER NOT NULL", "source_proposal_revision TEXT NOT NULL")
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_nullability_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_null.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "finalization_policy_version VARCHAR(64) NOT NULL", "finalization_policy_version VARCHAR(64)"
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_primary_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_pk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace("PRIMARY KEY (final_snapshot_fingerprint), \n\t", "")
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_missing_foreign_key_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_fk.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        ", \n\tFOREIGN KEY(final_docx_sha256) REFERENCES cv_document_blobs (blob_sha256) ON DELETE RESTRICT",
        "",
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_foreign_key_target_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_fk_target.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "FOREIGN KEY(source_proposal_artifact_fingerprint) REFERENCES cv_docx_proposal_artifacts "
        "(artifact_fingerprint) ON DELETE RESTRICT",
        "FOREIGN KEY(source_proposal_artifact_fingerprint) REFERENCES cv_document_blobs "
        "(blob_sha256) ON DELETE RESTRICT",
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_missing_check_constraint_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_check.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "CONSTRAINT ck_cv_docx_final_snapshot_revision CHECK (source_proposal_revision >= 1), \n\t", ""
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_edited_by_user_bool_check_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_edited_bool.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "CHECK (edited_by_user IN (0, 1))", "CHECK (edited_by_user IN (0, 1, 2))"
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_wrong_edited_by_user_consistency_check_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "wrong_edited_consistency.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "CONSTRAINT ck_cv_docx_final_snapshot_edited_by_user_consistency CHECK "
        "((edited_by_user = 1 AND generated_proposal_sha256 != final_docx_sha256) OR "
        "(edited_by_user = 0 AND generated_proposal_sha256 = final_docx_sha256)), \n\t",
        "",
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_accidental_unique_on_final_docx_sha256_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "accidental_unique.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace(
        "final_docx_sha256 VARCHAR(64) NOT NULL, \n\t",
        "final_docx_sha256 VARCHAR(64) NOT NULL UNIQUE, \n\t",
    )
    assert mutated != sql
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)
    # Re-create the non-unique indexes the addendum still expects, since
    # dropping/recreating the table also dropped its indexes.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX ix_cv_docx_final_snapshot_owner ON cv_docx_final_snapshots (owner_key_fingerprint)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_cv_docx_final_snapshot_source_proposal ON cv_docx_final_snapshots "
            "(source_proposal_artifact_fingerprint)"
        )

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_missing_index_blocks(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "missing_index.db")
    init_models_sync(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_cv_docx_final_snapshot_blob")

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    engine.dispose()


def test_failed_preflight_mutates_nothing(tmp_path) -> None:
    engine = make_sync_engine(tmp_path / "read_only.db")
    init_models_sync(engine)
    sql = _table_sql(engine, FINAL_SNAPSHOT_TABLE_NAME)
    mutated = sql.replace("source_proposal_revision INTEGER NOT NULL", "source_proposal_revision TEXT NOT NULL")
    _replace_table(engine, FINAL_SNAPSHOT_TABLE_NAME, mutated)

    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA"):
        preflight_explicit_provenance_final_docx_snapshot_schema(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)

    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def test_manifest_module_never_imports_orm_base_metadata() -> None:
    """AST-level check (not a raw text scan) that the manifest module never
    imports ``Base``/``cv_document_models``/an ORM table definition."""

    import ast
    import inspect

    import app.services.cv_document_final_snapshot_schema_manifest as manifest_module

    tree = ast.parse(inspect.getsource(manifest_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.models"
            assert node.module != "app.services.cv_document_models"
            for alias in node.names:
                assert alias.name not in {"Base", "CvDocxFinalSnapshotRow"}


def test_collect_actual_schema_reads_only_via_pragma_and_sqlite_master(tmp_path) -> None:
    """Sanity check that the collected actual schema matches the ORM's own
    rendering exactly -- proving the two independently-authored
    descriptions (hand-written manifest vs. ORM model) currently agree."""

    engine = make_sync_engine(tmp_path / "cross_check.db")
    init_models_sync(engine)
    with engine.connect() as conn:
        actual = collect_actual_final_snapshot_schema(conn)
    assert len(actual["columns"]) == 10
    assert len(actual["foreign_keys"]) == 3
    assert len(actual["check_constraints"]) == 9
    assert actual["unique_indexed_columns"] == frozenset()
    assert actual["non_unique_indexed_columns"] == frozenset(
        {"owner_key_fingerprint", "source_proposal_artifact_fingerprint", "final_docx_sha256"}
    )
    engine.dispose()
