"""Unit tests for the independent, hand-written
``candidate_identity_binding_schema_manifest`` module: manifest/ORM
independence, absent/exact/partial/incompatible schema classification, and
fail-closed read-only preflight for every drift category (missing/extra
column, missing/extra/drifted trigger, missing UNIQUE, extra manual index).
"""

from __future__ import annotations

import ast
import inspect

import pytest
from sqlalchemy import create_engine

from app.models import Base
from app.db_engine import _install_candidate_identity_binding_triggers
from app.services import candidate_identity_binding_schema_manifest as manifest_module
from app.services.candidate_identity_binding_schema_manifest import (
    CANDIDATE_IDENTITY_BINDING_TABLE_NAME,
    preflight_candidate_identity_binding_schema,
    candidate_identity_binding_schema_manifest,
)


def _engine_with_dependencies_only():
    """Just truth_entities/resumes/etc via the full ORM metadata, minus
    candidate_identity_bindings (dropped immediately after) -- so tests can
    build their own, deliberately non-conforming, version of that one
    table."""

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE {CANDIDATE_IDENTITY_BINDING_TABLE_NAME}")
    return engine


def _correct_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _install_candidate_identity_binding_triggers(engine)
    return engine


# 41: manifest independent from ORM ------------------------------------------------------


def test_manifest_module_never_imports_orm_or_base_metadata() -> None:
    """AST-level check (never a docstring-text scan): no import statement
    references ``app.models``/``app.services.cv_document_models``, and no
    expression anywhere in the code references the ``Base`` identifier or a
    ``.metadata`` attribute access."""

    source = inspect.getsource(manifest_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("app.models")
            assert node.module is None or not node.module.startswith("app.services.cv_document_models")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.models")
        if isinstance(node, ast.Name):
            assert node.id != "Base"
        if isinstance(node, ast.Attribute):
            assert node.attr != "metadata"


# 42-43: exact schema accepted / absence detected --------------------------------------------


def test_exact_schema_is_accepted() -> None:
    engine = _correct_engine()
    assert preflight_candidate_identity_binding_schema(engine) == (
        "CANDIDATE_IDENTITY_BINDING_SCHEMA_READY"
    )


def test_absent_schema_returns_create_required() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    assert preflight_candidate_identity_binding_schema(engine) == "ABSENT_CREATE_REQUIRED"


# 44-45: partial / incompatible schema fail-closed --------------------------------------------


def test_partial_schema_fails_closed() -> None:
    engine = _engine_with_dependencies_only()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE candidate_identity_bindings (
                binding_fingerprint VARCHAR(64) NOT NULL PRIMARY KEY,
                person_entity_id VARCHAR(36) NOT NULL
            )
            """
        )
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_candidate_identity_binding_schema(engine)


def test_incompatible_column_type_fails_closed() -> None:
    engine = _engine_with_dependencies_only()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE candidate_identity_bindings (
                binding_fingerprint VARCHAR(64) NOT NULL PRIMARY KEY,
                person_entity_id VARCHAR(36) NOT NULL,
                master_resume_id INTEGER NOT NULL,
                binding_schema_version VARCHAR(64) NOT NULL,
                created_at VARCHAR NOT NULL,
                CONSTRAINT uq_candidate_identity_bindings_person UNIQUE (person_entity_id),
                CONSTRAINT uq_candidate_identity_bindings_resume UNIQUE (master_resume_id),
                FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE RESTRICT,
                FOREIGN KEY(master_resume_id) REFERENCES resumes (resume_id) ON DELETE RESTRICT
            )
            """
        )
    _install_candidate_identity_binding_triggers(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA"):
        preflight_candidate_identity_binding_schema(engine)


# 46: failed preflight is read-only -----------------------------------------------------------


def test_failed_preflight_never_mutates() -> None:
    engine = _engine_with_dependencies_only()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE candidate_identity_bindings (
                binding_fingerprint VARCHAR(64) NOT NULL PRIMARY KEY,
                person_entity_id VARCHAR(36) NOT NULL
            )
            """
        )
    with pytest.raises(RuntimeError):
        preflight_candidate_identity_binding_schema(engine)

    with engine.connect() as conn:
        columns = [
            row["name"]
            for row in conn.exec_driver_sql(
                f"PRAGMA table_info({CANDIDATE_IDENTITY_BINDING_TABLE_NAME})"
            ).mappings()
        ]
        triggers = list(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (CANDIDATE_IDENTITY_BINDING_TABLE_NAME,),
            )
        )
    # Still exactly the two columns from the partial table -- untouched.
    assert columns == ["binding_fingerprint", "person_entity_id"]
    assert triggers == []


# 47-50: missing trigger fail-closed ------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger_name",
    [
        "trg_candidate_identity_bindings_person_type",
        "trg_candidate_identity_bindings_resume_master",
        "trg_candidate_identity_bindings_immutable",
        "trg_candidate_identity_bindings_no_delete",
    ],
)
def test_missing_trigger_fails_closed(trigger_name: str) -> None:
    engine = _correct_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
    with pytest.raises(RuntimeError, match="triggers"):
        preflight_candidate_identity_binding_schema(engine)


# 51: trigger SQL drift fail-closed ---------------------------------------------------------------


def test_trigger_sql_drift_fails_closed() -> None:
    engine = _correct_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_candidate_identity_bindings_no_delete")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_candidate_identity_bindings_no_delete
            BEFORE DELETE ON candidate_identity_bindings
            BEGIN
                SELECT RAISE(ABORT, 'SOMETHING_ELSE_ENTIRELY');
            END
            """
        )
    with pytest.raises(RuntimeError, match="triggers"):
        preflight_candidate_identity_binding_schema(engine)


# 52: extra trigger fail-closed ---------------------------------------------------------------------


def test_extra_trigger_fails_closed() -> None:
    engine = _correct_engine()
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
    with pytest.raises(RuntimeError, match="triggers"):
        preflight_candidate_identity_binding_schema(engine)


# 53-54: missing UNIQUE fail-closed -------------------------------------------------------------------


_MATCHING_CHECKS_SQL = """
                CONSTRAINT ck_candidate_identity_bindings_fingerprint CHECK (
                    length(binding_fingerprint) = 64 AND lower(binding_fingerprint) = binding_fingerprint
                    AND binding_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_candidate_identity_bindings_person_uuid CHECK (
                    length(person_entity_id) = 36 AND substr(person_entity_id, 9, 1) = '-'
                    AND substr(person_entity_id, 14, 1) = '-' AND substr(person_entity_id, 19, 1) = '-'
                    AND substr(person_entity_id, 24, 1) = '-'
                    AND length(replace(person_entity_id, '-', '')) = 32
                    AND replace(person_entity_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                    AND substr(person_entity_id, 15, 1) = '4'
                    AND substr(person_entity_id, 20, 1) IN ('8','9','a','b')
                ),
                CONSTRAINT ck_candidate_identity_bindings_schema_version CHECK (
                    binding_schema_version = 'candidate-identity-binding-schema-v1'
                ),
"""


def test_missing_unique_on_person_fails_closed() -> None:
    engine = _engine_with_dependencies_only()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TABLE candidate_identity_bindings (
                binding_fingerprint VARCHAR(64) NOT NULL PRIMARY KEY,
                person_entity_id VARCHAR(36) NOT NULL,
                master_resume_id VARCHAR NOT NULL,
                binding_schema_version VARCHAR(64) NOT NULL,
                created_at VARCHAR NOT NULL,
                {_MATCHING_CHECKS_SQL}
                CONSTRAINT uq_candidate_identity_bindings_resume UNIQUE (master_resume_id),
                FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE RESTRICT,
                FOREIGN KEY(master_resume_id) REFERENCES resumes (resume_id) ON DELETE RESTRICT
            )
            """
        )
    _install_candidate_identity_binding_triggers(engine)
    with pytest.raises(RuntimeError, match="unique_indexed_columns"):
        preflight_candidate_identity_binding_schema(engine)


def test_missing_unique_on_resume_fails_closed() -> None:
    engine = _engine_with_dependencies_only()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TABLE candidate_identity_bindings (
                binding_fingerprint VARCHAR(64) NOT NULL PRIMARY KEY,
                person_entity_id VARCHAR(36) NOT NULL,
                master_resume_id VARCHAR NOT NULL,
                binding_schema_version VARCHAR(64) NOT NULL,
                created_at VARCHAR NOT NULL,
                {_MATCHING_CHECKS_SQL}
                CONSTRAINT uq_candidate_identity_bindings_person UNIQUE (person_entity_id),
                FOREIGN KEY(person_entity_id) REFERENCES truth_entities (entity_id) ON DELETE RESTRICT,
                FOREIGN KEY(master_resume_id) REFERENCES resumes (resume_id) ON DELETE RESTRICT
            )
            """
        )
    _install_candidate_identity_binding_triggers(engine)
    with pytest.raises(RuntimeError, match="unique_indexed_columns"):
        preflight_candidate_identity_binding_schema(engine)


# 55: extra column fail-closed -----------------------------------------------------------------------


def test_extra_column_fails_closed() -> None:
    engine = _correct_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE candidate_identity_bindings ADD COLUMN extra_column VARCHAR"
        )
    with pytest.raises(RuntimeError, match="columns"):
        preflight_candidate_identity_binding_schema(engine)


# 56: extra manual index fail-closed -------------------------------------------------------------------


def test_extra_manual_index_fails_closed() -> None:
    engine = _correct_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX ix_candidate_identity_bindings_created_at "
            "ON candidate_identity_bindings (created_at)"
        )
    with pytest.raises(RuntimeError, match="unexpected_manual_index"):
        preflight_candidate_identity_binding_schema(engine)


# diagnostics accessor ------------------------------------------------------------------------------------


def test_manifest_accessor_raises_when_table_absent() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    with pytest.raises(RuntimeError, match="ERROR_CANDIDATE_IDENTITY_BINDING_TABLE_MISSING"):
        candidate_identity_binding_schema_manifest(engine)


def test_manifest_accessor_returns_full_manifest_when_ready() -> None:
    engine = _correct_engine()
    manifest = candidate_identity_binding_schema_manifest(engine)
    assert len(manifest["columns"]) == 5
    assert len(manifest["triggers"]) == 4
