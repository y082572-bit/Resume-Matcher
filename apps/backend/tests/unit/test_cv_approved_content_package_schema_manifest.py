"""Unit tests for Explicit Provenance Stage P6-C1b-A: the hand-written
``cv_approved_content_package_schema_manifest`` -- exact columns, CHECKs,
FKs (including the composite authority->package FK), UNIQUE constraint,
indexes, and all six trigger names/bodies. Also proves the manifest is
wholly independent of ``cv_document_models``'s ORM metadata.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from sqlalchemy import create_engine

import app.services.cv_document_models as models
from app.models import Base
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_schema_manifest import (
    ALL_TRIGGER_NAMES,
    AUTHORITY_EXPECTED_CHECK_CONSTRAINTS,
    AUTHORITY_EXPECTED_COLUMNS,
    AUTHORITY_EXPECTED_FOREIGN_KEYS,
    AUTHORITY_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS,
    AUTHORITY_EXPECTED_UNIQUE_CONSTRAINTS,
    AUTHORITY_TABLE_NAME,
    AUTHORITY_TRIGGER_NAMES,
    EXPECTED_TRIGGER_BODIES,
    PACKAGE_EXPECTED_CHECK_CONSTRAINTS,
    PACKAGE_EXPECTED_COLUMNS,
    PACKAGE_EXPECTED_FOREIGN_KEYS,
    PACKAGE_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS,
    PACKAGE_EXPECTED_UNIQUE_CONSTRAINTS,
    PACKAGE_TABLE_NAME,
    PACKAGE_TRIGGER_NAMES,
    collect_actual_approved_content_package_schema,
    collect_existing_trigger_bodies,
    table_matches_manifest,
)


def _fresh_ready_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    tables = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=tables)
    run_cutover_installer(engine)
    return engine


def test_package_table_exact_columns() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[PACKAGE_TABLE_NAME]["columns"] == PACKAGE_EXPECTED_COLUMNS


def test_authority_table_exact_columns() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[AUTHORITY_TABLE_NAME]["columns"] == AUTHORITY_EXPECTED_COLUMNS


def test_exact_check_constraints() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[PACKAGE_TABLE_NAME]["check_constraints"] == PACKAGE_EXPECTED_CHECK_CONSTRAINTS
    assert actual[AUTHORITY_TABLE_NAME]["check_constraints"] == AUTHORITY_EXPECTED_CHECK_CONSTRAINTS


def test_exact_foreign_keys_including_the_composite_authority_fk() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[PACKAGE_TABLE_NAME]["foreign_keys"] == PACKAGE_EXPECTED_FOREIGN_KEYS

    authority_fks = actual[AUTHORITY_TABLE_NAME]["foreign_keys"]
    assert authority_fks == AUTHORITY_EXPECTED_FOREIGN_KEYS
    # The composite FK is a single grouped entry with two column pairs --
    # never two independent single-column FKs that merely share a target.
    composite = [fk for fk in authority_fks if len(fk[2]) > 1]
    assert len(composite) == 1
    assert composite[0][0] == "cv_approved_content_packages"
    assert set(composite[0][2]) == {
        ("owner_key_fingerprint", "owner_key_fingerprint"),
        ("current_package_fingerprint", "package_fingerprint"),
    }


def test_exact_unique_constraint() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[PACKAGE_TABLE_NAME]["unique_constraints"] == PACKAGE_EXPECTED_UNIQUE_CONSTRAINTS
    assert actual[AUTHORITY_TABLE_NAME]["unique_constraints"] == AUTHORITY_EXPECTED_UNIQUE_CONSTRAINTS


def test_exact_indexes() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert actual[PACKAGE_TABLE_NAME]["non_unique_indexed_columns"] == PACKAGE_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS
    assert actual[AUTHORITY_TABLE_NAME]["non_unique_indexed_columns"] == AUTHORITY_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS


def test_table_matches_manifest_end_to_end() -> None:
    engine = _fresh_ready_engine()
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert table_matches_manifest(actual[PACKAGE_TABLE_NAME], table_name=PACKAGE_TABLE_NAME)
    assert table_matches_manifest(actual[AUTHORITY_TABLE_NAME], table_name=AUTHORITY_TABLE_NAME)


def test_manifest_detects_a_tampered_check_constraint() -> None:
    engine = _fresh_ready_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE cv_approved_content_packages")
        conn.exec_driver_sql(
            """
            CREATE TABLE cv_approved_content_packages (
                package_fingerprint VARCHAR(64) NOT NULL,
                owner_key_fingerprint VARCHAR(64) NOT NULL,
                owner_kind VARCHAR(16) NOT NULL,
                document_input_fingerprint VARCHAR(64) NOT NULL,
                approval_decisions_fingerprint VARCHAR(64) NOT NULL,
                fact_selection_identity_fingerprint VARCHAR(64) NOT NULL,
                payload_sha256 VARCHAR(64) NOT NULL,
                payload_byte_size INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                package_fingerprint_schema_version VARCHAR(64) NOT NULL,
                package_schema_version VARCHAR(64) NOT NULL,
                package_policy_version VARCHAR(64) NOT NULL,
                created_at VARCHAR NOT NULL,
                PRIMARY KEY (package_fingerprint),
                CONSTRAINT ck_owner_kind_wrong CHECK (owner_kind IN ('JOB'))
            )
            """
        )
    actual = collect_actual_approved_content_package_schema(engine.connect())
    assert not table_matches_manifest(actual[PACKAGE_TABLE_NAME], table_name=PACKAGE_TABLE_NAME)


def test_exact_trigger_names() -> None:
    assert len(PACKAGE_TRIGGER_NAMES) == 2
    assert len(AUTHORITY_TRIGGER_NAMES) == 4
    assert len(ALL_TRIGGER_NAMES) == 6
    assert PACKAGE_TRIGGER_NAMES == {
        "trg_cv_approved_content_packages_no_update",
        "trg_cv_approved_content_packages_no_delete",
    }
    assert AUTHORITY_TRIGGER_NAMES == {
        "trg_cv_approved_content_authority_insert_package_match",
        "trg_cv_approved_content_authority_update_package_match",
        "trg_cv_approved_content_authority_owner_immutable",
        "trg_cv_approved_content_authority_slot_version_increment",
    }


def test_exact_trigger_bodies() -> None:
    engine = _fresh_ready_engine()
    with engine.connect() as conn:
        package_triggers = collect_existing_trigger_bodies(conn, PACKAGE_TABLE_NAME)
        authority_triggers = collect_existing_trigger_bodies(conn, AUTHORITY_TABLE_NAME)

    for name in PACKAGE_TRIGGER_NAMES:
        assert package_triggers[name] == EXPECTED_TRIGGER_BODIES[name]
    for name in AUTHORITY_TRIGGER_NAMES:
        assert authority_triggers[name] == EXPECTED_TRIGGER_BODIES[name]


def test_manifest_detects_a_tampered_trigger_body() -> None:
    engine = _fresh_ready_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_cv_approved_content_packages_no_delete")
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_cv_approved_content_packages_no_delete
            BEFORE DELETE ON cv_approved_content_packages
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )
    with engine.connect() as conn:
        package_triggers = collect_existing_trigger_bodies(conn, PACKAGE_TABLE_NAME)
    assert package_triggers["trg_cv_approved_content_packages_no_delete"] != EXPECTED_TRIGGER_BODIES[
        "trg_cv_approved_content_packages_no_delete"
    ]


def test_manifest_module_is_independent_of_orm_metadata() -> None:
    """The manifest module must never *import* ``cv_document_models``,
    ``app.models``, or ``Base`` -- every expected value is a hand-written
    literal, so a bug in the ORM ``CvApprovedContentPackageRow``/
    ``CvApprovedContentCurrentAuthorityRow`` classes can never silently
    "pass" by being compared against its own reflection. Checked via the
    real import graph (``ast``), not a raw substring match, since the
    module's own docstring legitimately *mentions* ``cv_document_models``
    in prose."""

    import ast

    import app.services.cv_approved_content_package_schema_manifest as manifest_module

    source = Path(inspect.getfile(manifest_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {"app.services.cv_document_models", "app.models"}
    assert not (imported_modules & forbidden)
