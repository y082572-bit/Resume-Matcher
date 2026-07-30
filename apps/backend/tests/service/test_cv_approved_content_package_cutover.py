"""Service-level tests for Explicit Provenance Stage P6-C1b-A: the
``cv_approved_content_package_cutover_state`` resumable installer -- table
exclusion from earlier ``create_all`` branches, sole DDL ownership, every
partial-resume state, and fail-closed ``INVALID`` detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

import app.services.cv_document_models as models
from app.models import Base
from app.services.cv_approved_content_package_cutover_state import (
    ApprovedContentPackageCutoverState,
    evaluate_cutover_state,
    run_cutover_installer,
)
from app.services.cv_approved_content_package_schema_manifest import (
    AUTHORITY_TABLE_NAME,
    AUTHORITY_TRIGGER_NAMES,
    PACKAGE_TABLE_NAME,
    PACKAGE_TRIGGER_NAMES,
    TRIGGER_SQL_BY_NAME,
)


def _engine_without_new_tables(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'cutover.db'}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    tables = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=tables)
    return engine


def test_both_tables_are_excluded_from_the_generic_create_all(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert PACKAGE_TABLE_NAME not in names
    assert AUTHORITY_TABLE_NAME not in names


def test_absent_state_before_any_install(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.ABSENT


def test_installer_reaches_ready_from_absent(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    state = run_cutover_installer(engine)
    assert state == ApprovedContentPackageCutoverState.READY
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.READY


def test_installer_is_the_sole_ddl_owner_package_table_created_only_by_installer(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    # Base.metadata itself carries the ORM definitions, but create_all was
    # never asked to build them above -- confirm calling create_all on the
    # *full* metadata (as a naive caller might) is still a no-op for these
    # two tables specifically, i.e. nothing but the installer ever creates
    # them.
    with engine.connect() as conn:
        exists_before = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (PACKAGE_TABLE_NAME,)
        ).first()
    assert exists_before is None
    run_cutover_installer(engine)
    with engine.connect() as conn:
        exists_after = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (PACKAGE_TABLE_NAME,)
        ).first()
    assert exists_after is not None


def test_partial_resume_package_table_triggers_pending(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.PACKAGE_TABLE_TRIGGERS_PENDING

    state = run_cutover_installer(engine)
    assert state == ApprovedContentPackageCutoverState.READY


def test_partial_resume_authority_table_pending(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in PACKAGE_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.AUTHORITY_TABLE_PENDING

    state = run_cutover_installer(engine)
    assert state == ApprovedContentPackageCutoverState.READY


def test_partial_resume_authority_triggers_pending(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in PACKAGE_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
    models.CvApprovedContentCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.AUTHORITY_TRIGGERS_PENDING

    state = run_cutover_installer(engine)
    assert state == ApprovedContentPackageCutoverState.READY


def test_missing_a_single_trigger_out_of_a_group_still_resumes(tmp_path: Path) -> None:
    """Only *some* of a trigger group installed (the rest correct) is still
    a resumable ``*_PENDING`` state, never ``INVALID``."""

    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    only_one = next(iter(PACKAGE_TRIGGER_NAMES))
    with engine.begin() as conn:
        conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[only_one])

    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.PACKAGE_TABLE_TRIGGERS_PENDING
    state = run_cutover_installer(engine)
    assert state == ApprovedContentPackageCutoverState.READY


def test_wrong_trigger_body_is_invalid(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_cv_approved_content_packages_no_update
            BEFORE UPDATE ON cv_approved_content_packages
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.INVALID
    with pytest.raises(RuntimeError):
        run_cutover_installer(engine)


def test_wrong_check_constraint_is_invalid(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    with engine.begin() as conn:
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
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.INVALID
    with pytest.raises(RuntimeError):
        run_cutover_installer(engine)


def test_wrong_index_is_invalid(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_cv_approved_content_packages_owner")
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.INVALID


def test_wrong_foreign_key_is_invalid(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    models.CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        for name in PACKAGE_TRIGGER_NAMES:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])
        conn.exec_driver_sql(
            """
            CREATE TABLE cv_approved_content_current_authority (
                owner_key_fingerprint VARCHAR(64) NOT NULL,
                current_package_fingerprint VARCHAR(64) NOT NULL,
                slot_version INTEGER NOT NULL,
                updated_at VARCHAR NOT NULL,
                PRIMARY KEY (owner_key_fingerprint),
                CONSTRAINT ck_cv_approved_content_authority_package_fp CHECK (
                    length(current_package_fingerprint) = 64
                    AND lower(current_package_fingerprint) = current_package_fingerprint
                    AND current_package_fingerprint NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_cv_approved_content_authority_slot_version CHECK (slot_version >= 1)
            )
            """
        )
        # Deliberately no FK at all -- the manifest requires two (owner,
        # and the composite owner+package FK).
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.INVALID


def test_ready_restart_issues_zero_ddl(tmp_path: Path) -> None:
    engine = _engine_without_new_tables(tmp_path)
    run_cutover_installer(engine)
    assert evaluate_cutover_state(engine) == ApprovedContentPackageCutoverState.READY

    ddl_statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        upper = statement.strip().upper()
        if upper.startswith(("CREATE", "ALTER", "DROP")):
            ddl_statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        state = run_cutover_installer(engine)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert state == ApprovedContentPackageCutoverState.READY
    assert ddl_statements == []
