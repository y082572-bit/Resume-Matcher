"""Additive, fail-closed and restart-safe P1 schema migration tests."""

from __future__ import annotations

import hashlib
import json

import pytest

from app.database import Database
from app.db_engine import (
    explicit_provenance_p1_schema_manifest,
    init_models_sync,
    make_sync_engine,
    preflight_explicit_provenance_p1_schema,
)


P1_TABLES = {
    "truth_entities",
    "truth_facts",
    "truth_fact_variants",
    "truth_evidence",
    "truth_permissions",
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


def _reference_manifest(tmp_path):
    engine = make_sync_engine(tmp_path / "reference.db")
    init_models_sync(engine)
    manifest = explicit_provenance_p1_schema_manifest(engine)
    engine.dispose()
    return manifest


def _columns_only_sql(table, table_manifest):
    definitions = []
    for column in table_manifest["columns"]:
        definition = f'"{column["name"]}" {column["type"]}'
        if not column["nullable"]:
            definition += " NOT NULL"
        if column["default"] is not None:
            definition += f' DEFAULT {column["default"]}'
        if column["primary_key_position"]:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    return f'CREATE TABLE "{table}" ({", ".join(definitions)})'


def _replace_named_check(sql, name, replacement):
    marker = f"CONSTRAINT {name} CHECK ("
    marker_start = sql.index(marker)
    expression_start = marker_start + len(marker)
    depth = 1
    quote = None
    index = expression_start
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[:expression_start] + replacement + sql[index:]
        index += 1
    raise AssertionError(f"unterminated CHECK constraint: {name}")


def _r1_timestamp_check(field):
    return (
        f"({field} IS NULL OR (length({field}) = 27 "
        f"AND {field} GLOB "
        "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
        "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
        "[0-9][0-9][0-9][0-9][0-9][0-9]Z' "
        f"AND julianday({field}) IS NOT NULL))"
    )


def _build_fake_schema(tmp_path, manifest, mutation):
    engine = make_sync_engine(tmp_path / f"fake-{mutation}.db")
    with engine.begin() as connection:
        for table, table_manifest in manifest["tables"].items():
            sql = table_manifest["sql"]
            if mutation == "columns_only":
                sql = _columns_only_sql(table, table_manifest)
            elif table == "truth_facts" and mutation == "missing_fk":
                sql = sql.replace(
                    ", FOREIGN KEY(entity_id) REFERENCES truth_entities (entity_id) "
                    "ON DELETE RESTRICT",
                    "",
                )
            elif table == "truth_facts" and mutation == "wrong_on_delete":
                sql = sql.replace("ON DELETE RESTRICT", "ON DELETE CASCADE", 1)
            elif table == "truth_entities" and mutation == "wrong_type":
                sql = sql.replace(
                    "entity_type VARCHAR(32) NOT NULL",
                    "entity_type TEXT NOT NULL",
                )
            elif table == "truth_entities" and mutation == "wrong_nullable":
                sql = sql.replace(
                    "display_name VARCHAR(500) NOT NULL",
                    "display_name VARCHAR(500)",
                )
            elif table == "truth_entities" and mutation == "missing_check":
                sql = sql.replace(
                    ", CONSTRAINT ck_truth_entities_revision CHECK (revision >= 1)",
                    "",
                )
            elif table == "truth_entities" and mutation == "changed_check":
                sql = sql.replace(
                    "CONSTRAINT ck_truth_entities_revision CHECK (revision >= 1)",
                    "CONSTRAINT ck_truth_entities_revision CHECK (revision >= 0)",
                )
            elif table == "truth_permissions" and mutation == "r1_timestamp_check":
                sql = _replace_named_check(
                    sql,
                    "ck_truth_permissions_valid_from_utc",
                    _r1_timestamp_check("valid_from"),
                )
                sql = _replace_named_check(
                    sql,
                    "ck_truth_permissions_valid_until_utc",
                    _r1_timestamp_check("valid_until"),
                )
            connection.exec_driver_sql(sql)

        for table_manifest in manifest["tables"].values():
            for index in table_manifest["indexes"]:
                sql = index["sql"]
                if sql is None:
                    continue
                if mutation == "missing_partial_index" and index["name"] == (
                    "uq_truth_permissions_active_scope"
                ):
                    continue
                if mutation == "wrong_partial_predicate" and index["name"] == (
                    "uq_truth_permissions_active_scope"
                ):
                    sql = sql.replace(
                        "status = 'ACTIVE'", "status = 'REVIEW_REQUIRED'"
                    )
                if mutation == "wrong_expression_index" and index["name"] == (
                    "uq_truth_evidence_identity"
                ):
                    sql = sql.replace("content_hash", "confirmed_by")
                connection.exec_driver_sql(sql)

        for table_manifest in manifest["tables"].values():
            for trigger in table_manifest["triggers"]:
                if mutation == "missing_trigger" and trigger["name"] == (
                    "trg_truth_entities_type_immutable"
                ):
                    continue
                sql = trigger["sql"]
                if mutation == "changed_trigger" and trigger["name"] == (
                    "trg_truth_entities_type_immutable"
                ):
                    sql = sql.replace(
                        "TRUTH_ENTITY_TYPE_IMMUTABLE",
                        "TRUTH_ENTITY_TYPE_CHANGED",
                    )
                connection.exec_driver_sql(sql)
    return engine


def _legacy_snapshot(engine):
    with engine.begin() as connection:
        tables = sorted(
            name
            for name in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
            if not name.startswith("sqlite_") and name not in P1_TABLES
        )
        snapshot = {}
        for table in tables:
            rows = [
                dict(row)
                for row in connection.exec_driver_sql(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).mappings()
            ]
            payload = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
            snapshot[table] = {
                "count": len(rows),
                "hash": hashlib.sha256(payload.encode()).hexdigest(),
            }
        return snapshot


async def test_upgrade_is_additive_restart_safe_and_does_not_backfill(tmp_path):
    path = tmp_path / "legacy.db"
    database = Database(db_path=path)
    resume = await database.create_resume("CV content", processing_status="ready")
    await database.create_job("Job content", resume_id=resume["resume_id"])
    await database.close()

    engine = make_sync_engine(path)
    with engine.begin() as connection:
        for table in (
            "truth_permissions",
            "truth_evidence",
            "truth_fact_variants",
            "truth_facts",
            "truth_entities",
        ):
            connection.exec_driver_sql(f"DROP TABLE {table}")
    assert preflight_explicit_provenance_p1_schema(engine) == "ABSENT_CREATE_REQUIRED"
    before = _legacy_snapshot(engine)

    init_models_sync(engine)
    first = explicit_provenance_p1_schema_manifest(engine)
    assert preflight_explicit_provenance_p1_schema(engine) == "P1_SCHEMA_READY"
    assert _legacy_snapshot(engine) == before
    with engine.begin() as connection:
        assert set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'truth_%'"
            ).scalars()
        ) == P1_TABLES
        assert all(
            connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one() == 0
            for table in P1_TABLES
        )

    schema_before_restarts = _sqlite_master_snapshot(engine)
    init_models_sync(engine)
    second = explicit_provenance_p1_schema_manifest(engine)
    init_models_sync(engine)
    third = explicit_provenance_p1_schema_manifest(engine)
    assert first == second == third
    assert _sqlite_master_snapshot(engine) == schema_before_restarts
    assert _legacy_snapshot(engine) == before
    assert before["jobs"]["count"] == 1
    assert before["resumes"]["count"] == 1
    assert before["cv_transformation_generations"]["count"] == 0
    engine.dispose()


def test_partial_p1_schema_stops_without_repair(tmp_path):
    engine = make_sync_engine(tmp_path / "partial.db")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE truth_entities (entity_id TEXT PRIMARY KEY)"
        )
    with pytest.raises(RuntimeError, match="ERROR_PARTIAL_P1_SCHEMA"):
        init_models_sync(engine)
    with engine.begin() as connection:
        tables = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )
    assert tables & P1_TABLES == {"truth_entities"}
    engine.dispose()


def test_incompatible_p1_schema_stops_without_repair(tmp_path):
    engine = make_sync_engine(tmp_path / "incompatible.db")
    init_models_sync(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE truth_entities ADD COLUMN unexpected TEXT")
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA"):
        init_models_sync(engine)
    with engine.begin() as connection:
        assert "unexpected" in {
            row["name"]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(truth_entities)"
            ).mappings()
        }
    engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "columns_only",
        "missing_fk",
        "wrong_on_delete",
        "wrong_type",
        "wrong_nullable",
        "missing_check",
        "changed_check",
        "missing_trigger",
        "changed_trigger",
        "missing_partial_index",
        "wrong_partial_predicate",
        "wrong_expression_index",
        "r1_timestamp_check",
    ],
)
def test_full_preflight_rejects_fake_schema_without_mutation(tmp_path, mutation):
    manifest = _reference_manifest(tmp_path)
    engine = _build_fake_schema(tmp_path, manifest, mutation)
    before = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA"):
        init_models_sync(engine)
    assert _sqlite_master_snapshot(engine) == before
    engine.dispose()


# ---------------------------------------------------------------------------
# P1-R3: case-insensitive SQLite schema name identity
# ---------------------------------------------------------------------------


def _count_sqlite_master_objects(engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT type, COUNT(*) as c FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' GROUP BY type"
        ).mappings().all()
    return {row["type"]: row["c"] for row in rows}


@pytest.mark.parametrize(
    "wrong_name",
    ["TRUTH_ENTITIES", "Truth_Entities", "truth_Entities"],
)
def test_r3_single_wrong_case_table_raises_schema_name_error(tmp_path, wrong_name):
    engine = make_sync_engine(tmp_path / f"case_{wrong_name}.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE TABLE "{wrong_name}" (entity_id TEXT)')
    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def test_r3_all_five_uppercase_raises_before_mutation(tmp_path):
    engine = make_sync_engine(tmp_path / "all_upper.db")
    with engine.begin() as conn:
        for name in (
            "TRUTH_ENTITIES",
            "TRUTH_FACTS",
            "TRUTH_FACT_VARIANTS",
            "TRUTH_EVIDENCE",
            "TRUTH_PERMISSIONS",
        ):
            conn.exec_driver_sql(f'CREATE TABLE "{name}" (id TEXT)')
    before_hash, before_rows = _sqlite_master_snapshot(engine)
    before_counts = _count_sqlite_master_objects(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert before_rows == after_rows
    assert _count_sqlite_master_objects(engine) == before_counts
    engine.dispose()


def test_r3_canonical_plus_uppercase_raises_before_mutation(tmp_path):
    """truth_entities (canonical) + TRUTH_FACTS (uppercase) → error before any mutation."""
    engine = make_sync_engine(tmp_path / "mixed.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE truth_entities (entity_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE TRUTH_FACTS (fact_id TEXT)")
    before_hash, before_rows = _sqlite_master_snapshot(engine)
    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)
    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert before_rows == after_rows
    engine.dispose()


def test_r3_no_mutation_full_init_models_sync_with_wrong_case(tmp_path):
    """Full init_models_sync with TRUTH_ENTITIES must not mutate sqlite_master at all."""
    engine = make_sync_engine(tmp_path / "no_mut.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE TRUTH_ENTITIES (entity_id TEXT)")

    before_hash, before_rows = _sqlite_master_snapshot(engine)
    before_counts = _count_sqlite_master_objects(engine)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)

    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash, "sqlite_master mutated after ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"
    assert before_rows == after_rows
    assert _count_sqlite_master_objects(engine) == before_counts

    # No canonical P1 tables created, no indexes, no triggers
    with engine.connect() as conn:
        tables = set(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )
        indexes = list(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).scalars()
        )
        triggers = list(
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).scalars()
        )

    assert "truth_entities" not in tables
    assert "truth_facts" not in tables
    assert "truth_fact_variants" not in tables
    assert "truth_evidence" not in tables
    assert "truth_permissions" not in tables
    assert indexes == []
    assert triggers == []

    engine.dispose()


def test_r3_unrelated_table_not_treated_as_p1(tmp_path):
    """truth_entities_backup, truth_entity, truth_entities_old are not P1 tables."""
    engine = make_sync_engine(tmp_path / "unrelated.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE truth_entities_backup (entity_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE truth_entity (entity_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE my_truth_entities (entity_id TEXT)")
        conn.exec_driver_sql("CREATE TABLE truth_entities_old (entity_id TEXT)")
    result = preflight_explicit_provenance_p1_schema(engine)
    assert result == "ABSENT_CREATE_REQUIRED"
    engine.dispose()


def test_r3_sqlite_master_before_after_identical(tmp_path):
    """Full sqlite_master snapshot (all types) unchanged after ERROR_INCOMPATIBLE_P1_SCHEMA_NAME."""
    engine = make_sync_engine(tmp_path / "master_check.db")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE Truth_Permissions (id TEXT)")
        conn.exec_driver_sql("CREATE INDEX my_idx ON Truth_Permissions(id)")

    before_hash, before_rows = _sqlite_master_snapshot(engine)

    with pytest.raises(RuntimeError, match="ERROR_INCOMPATIBLE_P1_SCHEMA_NAME"):
        init_models_sync(engine)

    after_hash, after_rows = _sqlite_master_snapshot(engine)
    assert before_hash == after_hash
    assert len(before_rows) == len(after_rows)
    engine.dispose()
