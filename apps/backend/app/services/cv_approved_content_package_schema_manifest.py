"""Explicit Provenance Stage P6-C1b-A: an independent, hand-written schema
manifest for ``cv_approved_content_packages`` and
``cv_approved_content_current_authority`` -- and the single source of truth
for the exact DDL text of the six triggers this stage installs.

Deliberately never imports ``Base.metadata``, ``cv_document_models``, or any
ORM table/column definition -- every expected column/type/nullability/PK/FK/
CHECK/UNIQUE/index/trigger body below is a literal Python value written by
hand (mirroring ``cv_document_final_confirmed_pdf_schema_manifest.py``), so a
bug in the ORM model can never silently "pass" preflight by being compared
against its own reflection. The actual schema is read exclusively via
``sqlite_master``/``PRAGMA table_info``/``PRAGMA foreign_key_list``/
``PRAGMA index_list``/``PRAGMA index_xinfo`` -- raw SQLite introspection,
never SQLAlchemy's ``Table``/``Base.metadata`` reflection layer.

``cv_approved_content_package_cutover_state.py``'s cutover installer executes
the exact ``*_TRIGGER_SQL`` constants below as its ``CREATE TRIGGER`` DDL --
so the manifest and the installer can never independently drift out of sync.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import Connection, Engine


PACKAGE_TABLE_NAME = "cv_approved_content_packages"
AUTHORITY_TABLE_NAME = "cv_approved_content_current_authority"

APPROVED_CONTENT_PACKAGE_SCHEMA_MANIFEST_VERSION = "cv-approved-content-package-sql-schema-manifest-v1"


# -- hand-written expected schema: cv_approved_content_packages ---------------

PACKAGE_EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {"position": 0, "name": "package_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 1},
    {"position": 1, "name": "owner_key_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 2, "name": "owner_kind", "type": "VARCHAR(16)", "nullable": False, "primary_key_position": 0},
    {"position": 3, "name": "document_input_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 4, "name": "approval_decisions_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 5, "name": "fact_selection_identity_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 6, "name": "payload_sha256", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 7, "name": "payload_byte_size", "type": "INTEGER", "nullable": False, "primary_key_position": 0},
    {"position": 8, "name": "payload_json", "type": "TEXT", "nullable": False, "primary_key_position": 0},
    {"position": 9, "name": "package_fingerprint_schema_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 10, "name": "package_schema_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 11, "name": "package_policy_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 12, "name": "created_at", "type": "VARCHAR", "nullable": False, "primary_key_position": 0},
)

#: Each entry: (target_table, on_delete, ((source_column, target_column), ...))
#: -- grouped exactly as SQLite's own ``PRAGMA foreign_key_list`` groups a
#: single (possibly multi-column) FK definition by its ``id``, so a genuine
#: composite FK is never indistinguishable from two independent single-
#: column FKs that happen to point at the same table.
PACKAGE_EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, tuple[tuple[str, str], ...]]] = frozenset(
    {
        (
            "cv_document_artifact_owners",
            "RESTRICT",
            (("owner_key_fingerprint", "owner_key_fingerprint"),),
        ),
    }
)

PACKAGE_EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "length(package_fingerprint) = 64 AND lower(package_fingerprint) = package_fingerprint "
        "AND package_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(owner_key_fingerprint) = 64 AND lower(owner_key_fingerprint) = owner_key_fingerprint "
        "AND owner_key_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "owner_kind IN ('JOB', 'APPLICATION')",
        "length(document_input_fingerprint) = 64 AND lower(document_input_fingerprint) = "
        "document_input_fingerprint AND document_input_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(approval_decisions_fingerprint) = 64 AND lower(approval_decisions_fingerprint) = "
        "approval_decisions_fingerprint AND approval_decisions_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(fact_selection_identity_fingerprint) = 64 AND lower(fact_selection_identity_fingerprint) = "
        "fact_selection_identity_fingerprint AND fact_selection_identity_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(payload_sha256) = 64 AND lower(payload_sha256) = payload_sha256 "
        "AND payload_sha256 NOT GLOB '*[^0-9a-f]*'",
        "payload_byte_size > 0 AND payload_byte_size <= 1048576",
        "payload_byte_size = length(CAST(payload_json AS BLOB))",
        "package_fingerprint_schema_version = 'cv-approved-content-package-fingerprint-v1'",
        "package_schema_version = 'cv-approved-content-package-schema-v1'",
        "package_policy_version = 'cv-approved-content-package-policy-v1'",
    }
)

PACKAGE_EXPECTED_UNIQUE_CONSTRAINTS: frozenset[str] = frozenset(
    {"owner_key_fingerprint, package_fingerprint"}
)

PACKAGE_EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset()
PACKAGE_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset({"owner_key_fingerprint"})


# -- hand-written expected schema: cv_approved_content_current_authority -----

AUTHORITY_EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {"position": 0, "name": "owner_key_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 1},
    {"position": 1, "name": "current_package_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 2, "name": "slot_version", "type": "INTEGER", "nullable": False, "primary_key_position": 0},
    {"position": 3, "name": "updated_at", "type": "VARCHAR", "nullable": False, "primary_key_position": 0},
)

AUTHORITY_EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, tuple[tuple[str, str], ...]]] = frozenset(
    {
        (
            "cv_document_artifact_owners",
            "RESTRICT",
            (("owner_key_fingerprint", "owner_key_fingerprint"),),
        ),
        (
            "cv_approved_content_packages",
            "RESTRICT",
            (
                ("owner_key_fingerprint", "owner_key_fingerprint"),
                ("current_package_fingerprint", "package_fingerprint"),
            ),
        ),
    }
)

AUTHORITY_EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "length(current_package_fingerprint) = 64 AND lower(current_package_fingerprint) = "
        "current_package_fingerprint AND current_package_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "slot_version >= 1",
    }
)

AUTHORITY_EXPECTED_UNIQUE_CONSTRAINTS: frozenset[str] = frozenset()
AUTHORITY_EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset()
AUTHORITY_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset({"current_package_fingerprint"})


# -- hand-written exact trigger DDL (single source of truth) -----------------

TRG_PACKAGE_NO_UPDATE = "trg_cv_approved_content_packages_no_update"
TRG_PACKAGE_NO_DELETE = "trg_cv_approved_content_packages_no_delete"
TRG_AUTHORITY_INSERT_PACKAGE_MATCH = "trg_cv_approved_content_authority_insert_package_match"
TRG_AUTHORITY_UPDATE_PACKAGE_MATCH = "trg_cv_approved_content_authority_update_package_match"
TRG_AUTHORITY_OWNER_IMMUTABLE = "trg_cv_approved_content_authority_owner_immutable"
TRG_AUTHORITY_SLOT_VERSION_INCREMENT = "trg_cv_approved_content_authority_slot_version_increment"

CV_APPROVED_CONTENT_PACKAGE_IMMUTABLE_TOKEN = "CV_APPROVED_CONTENT_PACKAGE_IMMUTABLE"
CV_APPROVED_CONTENT_PACKAGE_NO_DELETE_TOKEN = "CV_APPROVED_CONTENT_PACKAGE_NO_DELETE"
CV_APPROVED_CONTENT_AUTHORITY_PACKAGE_OWNER_MISMATCH_TOKEN = "CV_APPROVED_CONTENT_AUTHORITY_PACKAGE_OWNER_MISMATCH"
CV_APPROVED_CONTENT_AUTHORITY_OWNER_IMMUTABLE_TOKEN = "CV_APPROVED_CONTENT_AUTHORITY_OWNER_IMMUTABLE"
CV_APPROVED_CONTENT_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT_TOKEN = (
    "CV_APPROVED_CONTENT_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT"
)


PACKAGE_NO_UPDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_PACKAGE_NO_UPDATE}
BEFORE UPDATE ON {PACKAGE_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_PACKAGE_IMMUTABLE_TOKEN}');
END
"""

PACKAGE_NO_DELETE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_PACKAGE_NO_DELETE}
BEFORE DELETE ON {PACKAGE_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_PACKAGE_NO_DELETE_TOKEN}');
END
"""

AUTHORITY_INSERT_PACKAGE_MATCH_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_INSERT_PACKAGE_MATCH}
BEFORE INSERT ON {AUTHORITY_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_AUTHORITY_PACKAGE_OWNER_MISMATCH_TOKEN}')
    WHERE NOT EXISTS (
        SELECT 1 FROM {PACKAGE_TABLE_NAME}
        WHERE package_fingerprint = NEW.current_package_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_UPDATE_PACKAGE_MATCH_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_UPDATE_PACKAGE_MATCH}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_AUTHORITY_PACKAGE_OWNER_MISMATCH_TOKEN}')
    WHERE NOT EXISTS (
        SELECT 1 FROM {PACKAGE_TABLE_NAME}
        WHERE package_fingerprint = NEW.current_package_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_OWNER_IMMUTABLE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_OWNER_IMMUTABLE}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.owner_key_fingerprint != OLD.owner_key_fingerprint
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_AUTHORITY_OWNER_IMMUTABLE_TOKEN}');
END
"""

AUTHORITY_SLOT_VERSION_INCREMENT_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_SLOT_VERSION_INCREMENT}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.slot_version != OLD.slot_version + 1
BEGIN
    SELECT RAISE(ABORT, '{CV_APPROVED_CONTENT_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT_TOKEN}');
END
"""

#: Ordered exactly as installed by the cutover installer: package
#: immutability triggers first, then authority triggers. 6 entries total.
ALL_TRIGGER_SQL: tuple[str, ...] = (
    PACKAGE_NO_UPDATE_SQL,
    PACKAGE_NO_DELETE_SQL,
    AUTHORITY_INSERT_PACKAGE_MATCH_SQL,
    AUTHORITY_UPDATE_PACKAGE_MATCH_SQL,
    AUTHORITY_OWNER_IMMUTABLE_SQL,
    AUTHORITY_SLOT_VERSION_INCREMENT_SQL,
)

TRIGGER_SQL_BY_NAME: dict[str, str] = {
    TRG_PACKAGE_NO_UPDATE: PACKAGE_NO_UPDATE_SQL,
    TRG_PACKAGE_NO_DELETE: PACKAGE_NO_DELETE_SQL,
    TRG_AUTHORITY_INSERT_PACKAGE_MATCH: AUTHORITY_INSERT_PACKAGE_MATCH_SQL,
    TRG_AUTHORITY_UPDATE_PACKAGE_MATCH: AUTHORITY_UPDATE_PACKAGE_MATCH_SQL,
    TRG_AUTHORITY_OWNER_IMMUTABLE: AUTHORITY_OWNER_IMMUTABLE_SQL,
    TRG_AUTHORITY_SLOT_VERSION_INCREMENT: AUTHORITY_SLOT_VERSION_INCREMENT_SQL,
}

PACKAGE_TRIGGER_NAMES: frozenset[str] = frozenset({TRG_PACKAGE_NO_UPDATE, TRG_PACKAGE_NO_DELETE})
AUTHORITY_TRIGGER_NAMES: frozenset[str] = frozenset(
    {
        TRG_AUTHORITY_INSERT_PACKAGE_MATCH,
        TRG_AUTHORITY_UPDATE_PACKAGE_MATCH,
        TRG_AUTHORITY_OWNER_IMMUTABLE,
        TRG_AUTHORITY_SLOT_VERSION_INCREMENT,
    }
)
ALL_TRIGGER_NAMES: frozenset[str] = PACKAGE_TRIGGER_NAMES | AUTHORITY_TRIGGER_NAMES


def _normalize_sql(value: str | None) -> str:
    if value is None:
        return ""
    normalized = re.sub(r"\s+", " ", value.strip())
    # SQLite itself drops "IF NOT EXISTS" from the DDL text it persists in
    # sqlite_master.sql -- normalize the same way here so a trigger installed
    # via our own idempotent ``CREATE TRIGGER IF NOT EXISTS`` DDL compares
    # equal to the body this module expects, exactly as SQLite would report it.
    normalized = re.sub(
        r"^CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", normalized, flags=re.IGNORECASE
    )
    return normalized


EXPECTED_TRIGGER_BODIES: dict[str, str] = {
    TRG_PACKAGE_NO_UPDATE: _normalize_sql(PACKAGE_NO_UPDATE_SQL),
    TRG_PACKAGE_NO_DELETE: _normalize_sql(PACKAGE_NO_DELETE_SQL),
    TRG_AUTHORITY_INSERT_PACKAGE_MATCH: _normalize_sql(AUTHORITY_INSERT_PACKAGE_MATCH_SQL),
    TRG_AUTHORITY_UPDATE_PACKAGE_MATCH: _normalize_sql(AUTHORITY_UPDATE_PACKAGE_MATCH_SQL),
    TRG_AUTHORITY_OWNER_IMMUTABLE: _normalize_sql(AUTHORITY_OWNER_IMMUTABLE_SQL),
    TRG_AUTHORITY_SLOT_VERSION_INCREMENT: _normalize_sql(AUTHORITY_SLOT_VERSION_INCREMENT_SQL),
}


# -- raw SQLite introspection (self-contained; no ORM/Base.metadata) --------


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _extract_constraint_bodies(table_sql: str, *, keyword: str) -> frozenset[str]:
    """Extract balanced ``KEYWORD(...)`` bodies directly from raw ``CREATE
    TABLE`` DDL text -- duplicated in spirit from (never imported from)
    ``db_engine.py``/``cv_document_final_confirmed_pdf_schema_manifest.py``'s
    identical-purpose helpers, so this manifest module stays independent."""

    bodies: list[str] = []
    pattern = re.compile(rf"\b{keyword}\s*\(", re.IGNORECASE)
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
                    bodies.append(_normalize_sql(table_sql[start + 1 : index]))
                    break
            index += 1
    return frozenset(bodies)


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).first()
    return row is not None


def _collect_foreign_key_groups(
    conn: Connection, quoted_table: str
) -> frozenset[tuple[str, str, tuple[tuple[str, str], ...]]]:
    rows = list(conn.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table})").mappings())
    groups: dict[int, list[tuple[int, str, str]]] = {}
    on_delete_by_id: dict[int, str] = {}
    table_by_id: dict[int, str] = {}
    for row in rows:
        groups.setdefault(row["id"], []).append((row["seq"], row["from"], row["to"]))
        on_delete_by_id[row["id"]] = row["on_delete"]
        table_by_id[row["id"]] = row["table"]

    result: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for fk_id, columns in groups.items():
        ordered = tuple(sorted(columns, key=lambda item: item[0]))
        column_pairs = tuple((frm, to) for _, frm, to in ordered)
        result.add((table_by_id[fk_id], on_delete_by_id[fk_id], column_pairs))
    return frozenset(result)


def _collect_table_schema(conn: Connection, table_name: str) -> dict[str, Any]:
    quoted = _quoted_identifier(table_name)
    table_sql_row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).first()
    if table_sql_row is None:
        raise RuntimeError(f"ERROR_APPROVED_CONTENT_PACKAGE_TABLE_MISSING:{table_name}")
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

    foreign_keys = _collect_foreign_key_groups(conn, quoted)
    check_constraints = _extract_constraint_bodies(table_sql, keyword="CHECK")
    unique_constraints = _extract_constraint_bodies(table_sql, keyword="UNIQUE")

    unique_indexed_columns: set[str] = set()
    non_unique_indexed_columns: set[str] = set()
    for index_row in conn.exec_driver_sql(f"PRAGMA index_list({quoted})").mappings():
        if index_row["origin"] == "pk":
            continue
        xinfo = list(
            conn.exec_driver_sql(f"PRAGMA index_xinfo({_quoted_identifier(index_row['name'])})").mappings()
        )
        key_columns = tuple(x["name"] for x in xinfo if x["key"])
        if len(key_columns) != 1:
            continue
        target = unique_indexed_columns if index_row["unique"] else non_unique_indexed_columns
        target.add(key_columns[0])

    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "check_constraints": check_constraints,
        "unique_constraints": unique_constraints,
        "unique_indexed_columns": frozenset(unique_indexed_columns),
        "non_unique_indexed_columns": frozenset(non_unique_indexed_columns),
    }


def collect_actual_approved_content_package_schema(conn: Connection) -> dict[str, Any]:
    """Read the live schema of both new tables from ``conn``, exclusively
    via ``sqlite_master``/``PRAGMA``. Raises if either table does not
    exist; callers must check the cutover state first."""

    return {
        "manifest_version": APPROVED_CONTENT_PACKAGE_SCHEMA_MANIFEST_VERSION,
        PACKAGE_TABLE_NAME: _collect_table_schema(conn, PACKAGE_TABLE_NAME),
        AUTHORITY_TABLE_NAME: _collect_table_schema(conn, AUTHORITY_TABLE_NAME),
    }


def table_matches_manifest(actual: dict[str, Any], *, table_name: str) -> bool:
    if table_name == PACKAGE_TABLE_NAME:
        return (
            actual["columns"] == PACKAGE_EXPECTED_COLUMNS
            and actual["foreign_keys"] == PACKAGE_EXPECTED_FOREIGN_KEYS
            and actual["check_constraints"] == PACKAGE_EXPECTED_CHECK_CONSTRAINTS
            and actual["unique_constraints"] == PACKAGE_EXPECTED_UNIQUE_CONSTRAINTS
            and actual["unique_indexed_columns"] == PACKAGE_EXPECTED_UNIQUE_INDEXED_COLUMNS
            and actual["non_unique_indexed_columns"] == PACKAGE_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS
        )
    if table_name == AUTHORITY_TABLE_NAME:
        return (
            actual["columns"] == AUTHORITY_EXPECTED_COLUMNS
            and actual["foreign_keys"] == AUTHORITY_EXPECTED_FOREIGN_KEYS
            and actual["check_constraints"] == AUTHORITY_EXPECTED_CHECK_CONSTRAINTS
            and actual["unique_constraints"] == AUTHORITY_EXPECTED_UNIQUE_CONSTRAINTS
            and actual["unique_indexed_columns"] == AUTHORITY_EXPECTED_UNIQUE_INDEXED_COLUMNS
            and actual["non_unique_indexed_columns"] == AUTHORITY_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS
        )
    raise ValueError(f"unknown table_name={table_name}")


def collect_existing_trigger_bodies(conn: Connection, table_name: str) -> dict[str, str]:
    """name -> normalized body, for every trigger currently attached to
    ``table_name``."""

    return {
        row["name"]: _normalize_sql(row["sql"])
        for row in conn.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table_name,),
        ).mappings()
    }


def preflight_approved_content_package_schema(engine: Engine) -> dict[str, Any]:
    """Read-only inspection of both tables and all trigger bodies attached
    to either of them. Never mutates anything -- the cutover state evaluator
    (``cv_approved_content_package_cutover_state.py``) is the only consumer,
    and interprets the returned dict itself."""

    with engine.connect() as conn:
        package_table_exists = _table_exists(conn, PACKAGE_TABLE_NAME)
        authority_table_exists = _table_exists(conn, AUTHORITY_TABLE_NAME)

        package_schema = _collect_table_schema(conn, PACKAGE_TABLE_NAME) if package_table_exists else None
        authority_schema = (
            _collect_table_schema(conn, AUTHORITY_TABLE_NAME) if authority_table_exists else None
        )

        package_triggers = collect_existing_trigger_bodies(conn, PACKAGE_TABLE_NAME)
        authority_triggers = collect_existing_trigger_bodies(conn, AUTHORITY_TABLE_NAME)

    return {
        "package_table_exists": package_table_exists,
        "authority_table_exists": authority_table_exists,
        "package_schema": package_schema,
        "authority_schema": authority_schema,
        "package_triggers": package_triggers,
        "authority_triggers": authority_triggers,
    }
