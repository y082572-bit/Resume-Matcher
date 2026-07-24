"""Explicit Provenance Stage P6-B2A-I1: an independent, hand-written schema
manifest for ``candidate_identity_bindings``.

Deliberately never imports ``Base.metadata``, the ``CandidateIdentityBinding``
ORM model, or ``app.models``'s table definition -- every expected column/
type/nullability/PK/FK/CHECK/UNIQUE/trigger/index below is a literal Python
value written by hand (mirroring
``cv_document_final_snapshot_schema_manifest.py``), so a bug in the ORM
model can never silently "pass" preflight by being compared against its own
reflection. The actual schema is read exclusively via ``sqlite_master``/
``PRAGMA table_info``/``PRAGMA foreign_key_list``/``PRAGMA index_list``/
``PRAGMA index_xinfo`` -- raw SQLite introspection, never SQLAlchemy's
``Table``/``Base.metadata`` reflection layer.

Preflight is fail-closed and strictly read-only: a fully absent table
returns ``ABSENT_CREATE_REQUIRED`` (the only state a caller may act on, by
issuing its own ``CREATE TABLE`` and trigger installation); any structural
difference -- missing/extra column, wrong type, wrong nullability, wrong
primary key, a missing or mistargeted foreign key, a missing or altered
CHECK constraint, a missing ``UNIQUE`` on ``person_entity_id`` or
``master_resume_id``, a missing/extra/drifted one of the four
``trg_candidate_identity_bindings_*`` triggers, an extra column, or an extra
manual index -- always raises before anything is mutated. Calling this
module's functions never executes a single ``CREATE``/``ALTER``/``DROP``
statement itself.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import Connection, Engine


CANDIDATE_IDENTITY_BINDING_TABLE_NAME = "candidate_identity_bindings"
CANDIDATE_IDENTITY_BINDING_SCHEMA_MANIFEST_VERSION = (
    "candidate-identity-binding-sql-schema-manifest-v1"
)


# -- hand-written expected schema (never derived from the ORM) --------------

EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {
        "position": 0,
        "name": "binding_fingerprint",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 1,
    },
    {
        "position": 1,
        "name": "person_entity_id",
        "type": "VARCHAR(36)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 2,
        "name": "master_resume_id",
        "type": "VARCHAR",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 3,
        "name": "binding_schema_version",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 4,
        "name": "created_at",
        "type": "VARCHAR",
        "nullable": False,
        "primary_key_position": 0,
    },
)

# (source_column, target_table, target_column, on_delete)
EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        ("person_entity_id", "truth_entities", "entity_id", "RESTRICT"),
        ("master_resume_id", "resumes", "resume_id", "RESTRICT"),
    }
)

EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "length(binding_fingerprint) = 64 AND lower(binding_fingerprint) = binding_fingerprint "
        "AND binding_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(person_entity_id) = 36 AND substr(person_entity_id, 9, 1) = '-' AND "
        "substr(person_entity_id, 14, 1) = '-' AND substr(person_entity_id, 19, 1) = '-' AND "
        "substr(person_entity_id, 24, 1) = '-' AND length(replace(person_entity_id, '-', '')) = 32 "
        "AND replace(person_entity_id, '-', '') NOT GLOB '*[^0-9a-f]*' AND "
        "substr(person_entity_id, 15, 1) = '4' AND substr(person_entity_id, 20, 1) IN "
        "('8','9','a','b')",
        "binding_schema_version = 'candidate-identity-binding-schema-v1'",
    }
)

EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset({"person_entity_id", "master_resume_id"})

# No manually-created index is ever expected beyond the PK's and the two
# UNIQUE constraints' own implicit indexes.
EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset()

# (trigger_name, normalized_sql) -- exactly four, matching the CREATE
# TRIGGER statements installed in app/db_engine.py (minus "IF NOT EXISTS",
# which SQLite itself never persists into sqlite_master.sql).
EXPECTED_TRIGGERS: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "trg_candidate_identity_bindings_person_type",
            "CREATE TRIGGER trg_candidate_identity_bindings_person_type "
            "BEFORE INSERT ON candidate_identity_bindings BEGIN SELECT RAISE(ABORT, "
            "'CANDIDATE_IDENTITY_BINDING_PERSON_ENTITY_INVALID') WHERE NOT EXISTS ( SELECT 1 "
            "FROM truth_entities WHERE entity_id = NEW.person_entity_id AND entity_type = "
            "'PERSON' ); END",
        ),
        (
            "trg_candidate_identity_bindings_resume_master",
            "CREATE TRIGGER trg_candidate_identity_bindings_resume_master "
            "BEFORE INSERT ON candidate_identity_bindings BEGIN SELECT RAISE(ABORT, "
            "'CANDIDATE_IDENTITY_BINDING_RESUME_NOT_MASTER') WHERE NOT EXISTS ( SELECT 1 FROM "
            "resumes WHERE resume_id = NEW.master_resume_id AND is_master = 1 ); END",
        ),
        (
            "trg_candidate_identity_bindings_immutable",
            "CREATE TRIGGER trg_candidate_identity_bindings_immutable "
            "BEFORE UPDATE ON candidate_identity_bindings BEGIN SELECT RAISE(ABORT, "
            "'CANDIDATE_IDENTITY_BINDING_IMMUTABLE'); END",
        ),
        (
            "trg_candidate_identity_bindings_no_delete",
            "CREATE TRIGGER trg_candidate_identity_bindings_no_delete "
            "BEFORE DELETE ON candidate_identity_bindings BEGIN SELECT RAISE(ABORT, "
            "'CANDIDATE_IDENTITY_BINDING_NO_DELETE'); END",
        ),
    }
)


# -- raw SQLite introspection (self-contained; no ORM/Base.metadata) --------


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip())


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _extract_check_constraint_bodies(table_sql: str) -> frozenset[str]:
    """Extract balanced ``CHECK(...)`` bodies directly from raw
    ``CREATE TABLE`` DDL text -- duplicated in spirit from (never imported
    from) ``cv_document_final_snapshot_schema_manifest.py``'s
    identical-purpose helper, so this manifest module stays fully
    independent of every other schema-manifest module in this codebase."""

    bodies: list[str] = []
    pattern = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
    for match in pattern.finditer(table_sql):
        start = match.end() - 1
        depth = 0
        quote: str | None = None
        index = start
        while index < len(table_sql):
            char = table_sql[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(table_sql) and table_sql[index + 1] == quote:
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
                    bodies.append(_normalize_sql(table_sql[start + 1 : index]) or "")
                    break
            index += 1
    return frozenset(bodies)


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).first()
    return row is not None


def collect_actual_candidate_identity_binding_schema(conn: Connection) -> dict[str, Any]:
    """Read the live ``candidate_identity_bindings`` schema from ``conn``,
    exclusively via ``sqlite_master``/``PRAGMA`` -- never via SQLAlchemy
    ``Table`` reflection. Raises if the table does not exist; callers must
    check :func:`preflight_candidate_identity_binding_schema`'s
    ``ABSENT_CREATE_REQUIRED`` state first."""

    quoted = _quoted_identifier(CANDIDATE_IDENTITY_BINDING_TABLE_NAME)
    table_sql_row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (CANDIDATE_IDENTITY_BINDING_TABLE_NAME,),
    ).first()
    if table_sql_row is None:
        raise RuntimeError("ERROR_CANDIDATE_IDENTITY_BINDING_TABLE_MISSING")
    table_sql = table_sql_row[0]

    columns = tuple(
        {
            "position": row["cid"],
            "name": row["name"],
            "type": row["type"],
            "nullable": not bool(row["notnull"]),
            "primary_key_position": row["pk"],
        }
        for row in conn.exec_driver_sql(f"PRAGMA table_info({quoted})").mappings()
    )

    foreign_keys = frozenset(
        (row["from"], row["table"], row["to"], row["on_delete"])
        for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list({quoted})").mappings()
    )

    check_constraints = _extract_check_constraint_bodies(table_sql)

    unique_indexed_columns: set[str] = set()
    non_unique_indexed_columns: set[str] = set()
    for index_row in conn.exec_driver_sql(f"PRAGMA index_list({quoted})").mappings():
        if index_row["origin"] == "pk":
            continue
        xinfo = list(
            conn.exec_driver_sql(
                f"PRAGMA index_xinfo({_quoted_identifier(index_row['name'])})"
            ).mappings()
        )
        key_columns = tuple(x["name"] for x in xinfo if x["key"])
        if len(key_columns) != 1:
            continue
        target = unique_indexed_columns if index_row["unique"] else non_unique_indexed_columns
        target.add(key_columns[0])

    triggers = frozenset(
        (row["name"], _normalize_sql(row["sql"]))
        for row in conn.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
            "ORDER BY name",
            (CANDIDATE_IDENTITY_BINDING_TABLE_NAME,),
        ).mappings()
    )

    return {
        "manifest_version": CANDIDATE_IDENTITY_BINDING_SCHEMA_MANIFEST_VERSION,
        "columns": columns,
        "foreign_keys": foreign_keys,
        "check_constraints": check_constraints,
        "unique_indexed_columns": frozenset(unique_indexed_columns),
        "non_unique_indexed_columns": frozenset(non_unique_indexed_columns),
        "triggers": triggers,
    }


def preflight_candidate_identity_binding_schema(engine: Engine) -> str:
    """Fully validate an existing ``candidate_identity_bindings`` table
    (and its four triggers) against the hand-written manifest above,
    without mutating or repairing the target. Returns
    ``ABSENT_CREATE_REQUIRED`` if the table does not exist at all (the
    caller may then create it and install the triggers); returns
    ``CANDIDATE_IDENTITY_BINDING_SCHEMA_READY`` if it exists and matches
    exactly; otherwise raises ``RuntimeError`` before this function (or any
    caller that respects its result) performs a single mutation."""

    with engine.connect() as conn:
        if not _table_exists(conn, CANDIDATE_IDENTITY_BINDING_TABLE_NAME):
            return "ABSENT_CREATE_REQUIRED"
        actual = collect_actual_candidate_identity_binding_schema(conn)

    if actual["columns"] != EXPECTED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:columns:"
            f"expected={EXPECTED_COLUMNS}:actual={actual['columns']}"
        )
    if actual["foreign_keys"] != EXPECTED_FOREIGN_KEYS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:foreign_keys:"
            f"expected={sorted(EXPECTED_FOREIGN_KEYS)}:actual={sorted(actual['foreign_keys'])}"
        )
    if actual["check_constraints"] != EXPECTED_CHECK_CONSTRAINTS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:check_constraints:"
            f"expected={sorted(EXPECTED_CHECK_CONSTRAINTS)}:actual={sorted(actual['check_constraints'])}"
        )
    if actual["unique_indexed_columns"] != EXPECTED_UNIQUE_INDEXED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:unique_indexed_columns:"
            f"expected={sorted(EXPECTED_UNIQUE_INDEXED_COLUMNS)}:"
            f"actual={sorted(actual['unique_indexed_columns'])}"
        )
    if actual["non_unique_indexed_columns"] != EXPECTED_NON_UNIQUE_INDEXED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:unexpected_manual_index:"
            f"actual={sorted(actual['non_unique_indexed_columns'])}"
        )
    if actual["triggers"] != EXPECTED_TRIGGERS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_CANDIDATE_IDENTITY_BINDING_SCHEMA:triggers:"
            f"expected={sorted(EXPECTED_TRIGGERS)}:actual={sorted(actual['triggers'])}"
        )
    return "CANDIDATE_IDENTITY_BINDING_SCHEMA_READY"


def candidate_identity_binding_schema_manifest(engine: Engine) -> dict[str, Any]:
    """Return the complete actual manifest, for diagnostics/idempotency
    tests -- raises if the table is absent (callers must run
    :func:`preflight_candidate_identity_binding_schema` first)."""

    with engine.connect() as conn:
        if not _table_exists(conn, CANDIDATE_IDENTITY_BINDING_TABLE_NAME):
            raise RuntimeError(
                "ERROR_CANDIDATE_IDENTITY_BINDING_TABLE_MISSING:manifest_unavailable"
            )
        return collect_actual_candidate_identity_binding_schema(conn)
