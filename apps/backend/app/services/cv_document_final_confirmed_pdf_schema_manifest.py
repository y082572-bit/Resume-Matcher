"""Explicit Provenance Stage P6-B3b-A: an independent, hand-written schema
manifest for ``cv_final_confirmed_pdf_artifacts`` and
``cv_confirmed_pdf_current_authority`` -- and the single source of truth
for the exact DDL text of the 12 triggers this stage installs.

Deliberately never imports ``Base.metadata``, ``cv_document_models``, or any
ORM table/column definition -- every expected column/type/nullability/PK/FK/
CHECK/index/trigger body below is a literal Python value written by hand
(mirroring ``cv_document_final_snapshot_schema_manifest.py``), so a bug in
the ORM model can never silently "pass" preflight by being compared against
its own reflection. The actual schema is read exclusively via
``sqlite_master``/``PRAGMA table_info``/``PRAGMA foreign_key_list``/
``PRAGMA index_list``/``PRAGMA index_xinfo`` -- raw SQLite introspection,
never SQLAlchemy's ``Table``/``Base.metadata`` reflection layer.

``cv_document_final_confirmed_pdf_sql_repository.py``'s cutover installer
executes the exact ``*_TRIGGER_SQL`` constants below as its ``CREATE
TRIGGER`` DDL -- so the manifest and the installer can never independently
drift out of sync.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import Connection, Engine


ARTIFACT_TABLE_NAME = "cv_final_confirmed_pdf_artifacts"
AUTHORITY_TABLE_NAME = "cv_confirmed_pdf_current_authority"
LEGACY_PDF_SLOT_TABLE_NAME = "cv_document_pdf_slots"

FINAL_CONFIRMED_PDF_SCHEMA_MANIFEST_VERSION = "cv-final-confirmed-pdf-sql-schema-manifest-v1"


# -- hand-written expected schema: cv_final_confirmed_pdf_artifacts ---------

ARTIFACT_EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {"position": 0, "name": "artifact_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 1},
    {"position": 1, "name": "owner_key_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 2, "name": "source_final_snapshot_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 3, "name": "source_final_docx_sha256", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 4, "name": "conversion_request_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 5, "name": "runtime_identity_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 6, "name": "runtime_identity_json", "type": "TEXT", "nullable": False, "primary_key_position": 0},
    {"position": 7, "name": "conversion_policy_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 8, "name": "pdf_sha256", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 9, "name": "blob_sha256", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 10, "name": "conversion_request_fingerprint_schema_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 11, "name": "artifact_fingerprint_schema_version", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 12, "name": "created_at", "type": "VARCHAR", "nullable": False, "primary_key_position": 0},
)

ARTIFACT_EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        ("owner_key_fingerprint", "cv_document_artifact_owners", "owner_key_fingerprint", "RESTRICT"),
        (
            "source_final_snapshot_fingerprint",
            "cv_docx_final_snapshots",
            "final_snapshot_fingerprint",
            "RESTRICT",
        ),
        ("blob_sha256", "cv_document_blobs", "blob_sha256", "RESTRICT"),
    }
)

ARTIFACT_EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "length(artifact_fingerprint) = 64 AND lower(artifact_fingerprint) = artifact_fingerprint "
        "AND artifact_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(source_final_snapshot_fingerprint) = 64 AND lower(source_final_snapshot_fingerprint) = "
        "source_final_snapshot_fingerprint AND source_final_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(source_final_docx_sha256) = 64 AND lower(source_final_docx_sha256) = "
        "source_final_docx_sha256 AND source_final_docx_sha256 NOT GLOB '*[^0-9a-f]*'",
        "length(conversion_request_fingerprint) = 64 AND lower(conversion_request_fingerprint) = "
        "conversion_request_fingerprint AND conversion_request_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(runtime_identity_fingerprint) = 64 AND lower(runtime_identity_fingerprint) = "
        "runtime_identity_fingerprint AND runtime_identity_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(pdf_sha256) = 64 AND lower(pdf_sha256) = pdf_sha256 AND pdf_sha256 NOT GLOB '*[^0-9a-f]*'",
        "length(blob_sha256) = 64 AND lower(blob_sha256) = blob_sha256 AND blob_sha256 NOT GLOB '*[^0-9a-f]*'",
        "pdf_sha256 = blob_sha256",
        "trim(conversion_policy_version) != ''",
        "length(conversion_policy_version) <= 64",
        "conversion_request_fingerprint_schema_version = "
        "'cv-final-confirmed-pdf-request-fingerprint-schema-v1'",
        "artifact_fingerprint_schema_version = "
        "'cv-final-confirmed-pdf-artifact-fingerprint-schema-v1'",
        "trim(runtime_identity_json) != ''",
        "length(CAST(runtime_identity_json AS BLOB)) <= 8192",
    }
)

ARTIFACT_EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset({"conversion_request_fingerprint"})
ARTIFACT_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset(
    {"owner_key_fingerprint", "source_final_snapshot_fingerprint", "blob_sha256"}
)


# -- hand-written expected schema: cv_confirmed_pdf_current_authority -------

AUTHORITY_EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {"position": 0, "name": "owner_key_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 1},
    {"position": 1, "name": "lineage_kind", "type": "VARCHAR(32)", "nullable": False, "primary_key_position": 0},
    {"position": 2, "name": "current_artifact_fingerprint", "type": "VARCHAR(64)", "nullable": False, "primary_key_position": 0},
    {"position": 3, "name": "slot_version", "type": "INTEGER", "nullable": False, "primary_key_position": 0},
    {"position": 4, "name": "updated_at", "type": "VARCHAR", "nullable": False, "primary_key_position": 0},
)

AUTHORITY_EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, str, str]] = frozenset(
    {("owner_key_fingerprint", "cv_document_artifact_owners", "owner_key_fingerprint", "RESTRICT")}
)

AUTHORITY_EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "lineage_kind IN ('LEGACY_VALIDATED_DOCX', 'FINAL_DOCX_SNAPSHOT')",
        "length(current_artifact_fingerprint) = 64 AND lower(current_artifact_fingerprint) = "
        "current_artifact_fingerprint AND current_artifact_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "slot_version >= 1",
    }
)

AUTHORITY_EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset()
AUTHORITY_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset({"current_artifact_fingerprint"})


# -- hand-written exact trigger DDL (single source of truth) ----------------

TRG_AUTHORITY_INSERT_FINAL = "trg_cv_confirmed_pdf_authority_insert_final"
TRG_AUTHORITY_INSERT_LEGACY = "trg_cv_confirmed_pdf_authority_insert_legacy"
TRG_AUTHORITY_UPDATE_FINAL = "trg_cv_confirmed_pdf_authority_update_final"
TRG_AUTHORITY_UPDATE_LEGACY = "trg_cv_confirmed_pdf_authority_update_legacy"
TRG_AUTHORITY_OWNER_IMMUTABLE = "trg_cv_confirmed_pdf_authority_owner_immutable"
TRG_AUTHORITY_SLOT_VERSION_INCREMENT = "trg_cv_confirmed_pdf_authority_slot_version_increment"
TRG_LEGACY_SLOT_FROZEN_INSERT = "trg_cv_document_pdf_slot_frozen_insert"
TRG_LEGACY_SLOT_FROZEN_UPDATE = "trg_cv_document_pdf_slot_frozen_update"
TRG_LEGACY_SLOT_FROZEN_DELETE = "trg_cv_document_pdf_slot_frozen_delete"
TRG_ARTIFACT_LINEAGE_INSERT = "trg_cv_final_confirmed_pdf_artifact_lineage_insert"
TRG_ARTIFACT_NO_UPDATE = "trg_cv_final_confirmed_pdf_artifact_no_update"
TRG_ARTIFACT_NO_DELETE = "trg_cv_final_confirmed_pdf_artifact_no_delete"

CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN = "CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER"
CV_FINAL_CONFIRMED_PDF_ARTIFACT_SNAPSHOT_MISSING_TOKEN = "CV_FINAL_CONFIRMED_PDF_ARTIFACT_SNAPSHOT_MISSING"
CV_FINAL_CONFIRMED_PDF_ARTIFACT_OWNER_MISMATCH_TOKEN = "CV_FINAL_CONFIRMED_PDF_ARTIFACT_OWNER_MISMATCH"
CV_FINAL_CONFIRMED_PDF_ARTIFACT_SOURCE_HASH_MISMATCH_TOKEN = (
    "CV_FINAL_CONFIRMED_PDF_ARTIFACT_SOURCE_HASH_MISMATCH"
)
CV_FINAL_CONFIRMED_PDF_ARTIFACT_IMMUTABLE_TOKEN = "CV_FINAL_CONFIRMED_PDF_ARTIFACT_IMMUTABLE"
CV_FINAL_CONFIRMED_PDF_ARTIFACT_NO_DELETE_TOKEN = "CV_FINAL_CONFIRMED_PDF_ARTIFACT_NO_DELETE"


AUTHORITY_INSERT_FINAL_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_INSERT_FINAL}
BEFORE INSERT ON {AUTHORITY_TABLE_NAME}
WHEN NEW.lineage_kind = 'FINAL_DOCX_SNAPSHOT'
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_FINAL_ARTIFACT_MISSING')
    WHERE NOT EXISTS (
        SELECT 1 FROM {ARTIFACT_TABLE_NAME}
        WHERE artifact_fingerprint = NEW.current_artifact_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_INSERT_LEGACY_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_INSERT_LEGACY}
BEFORE INSERT ON {AUTHORITY_TABLE_NAME}
WHEN NEW.lineage_kind = 'LEGACY_VALIDATED_DOCX'
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_LEGACY_ARTIFACT_MISSING')
    WHERE NOT EXISTS (
        SELECT 1 FROM cv_confirmed_pdf_artifacts
        WHERE artifact_fingerprint = NEW.current_artifact_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_UPDATE_FINAL_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_UPDATE_FINAL}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.lineage_kind = 'FINAL_DOCX_SNAPSHOT'
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_FINAL_ARTIFACT_MISSING')
    WHERE NOT EXISTS (
        SELECT 1 FROM {ARTIFACT_TABLE_NAME}
        WHERE artifact_fingerprint = NEW.current_artifact_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_UPDATE_LEGACY_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_UPDATE_LEGACY}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.lineage_kind = 'LEGACY_VALIDATED_DOCX'
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_LEGACY_ARTIFACT_MISSING')
    WHERE NOT EXISTS (
        SELECT 1 FROM cv_confirmed_pdf_artifacts
        WHERE artifact_fingerprint = NEW.current_artifact_fingerprint
          AND owner_key_fingerprint = NEW.owner_key_fingerprint
    );
END
"""

AUTHORITY_OWNER_IMMUTABLE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_OWNER_IMMUTABLE}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.owner_key_fingerprint != OLD.owner_key_fingerprint
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_OWNER_IMMUTABLE');
END
"""

AUTHORITY_SLOT_VERSION_INCREMENT_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_AUTHORITY_SLOT_VERSION_INCREMENT}
BEFORE UPDATE ON {AUTHORITY_TABLE_NAME}
WHEN NEW.slot_version != OLD.slot_version + 1
BEGIN
    SELECT RAISE(ABORT, 'CV_CONFIRMED_PDF_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT');
END
"""

LEGACY_SLOT_FROZEN_INSERT_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_LEGACY_SLOT_FROZEN_INSERT}
BEFORE INSERT ON {LEGACY_PDF_SLOT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN}');
END
"""

LEGACY_SLOT_FROZEN_UPDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_LEGACY_SLOT_FROZEN_UPDATE}
BEFORE UPDATE ON {LEGACY_PDF_SLOT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN}');
END
"""

LEGACY_SLOT_FROZEN_DELETE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_LEGACY_SLOT_FROZEN_DELETE}
BEFORE DELETE ON {LEGACY_PDF_SLOT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN}');
END
"""

ARTIFACT_LINEAGE_INSERT_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_ARTIFACT_LINEAGE_INSERT}
BEFORE INSERT ON {ARTIFACT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_FINAL_CONFIRMED_PDF_ARTIFACT_SNAPSHOT_MISSING_TOKEN}')
    WHERE NOT EXISTS (
        SELECT 1 FROM cv_docx_final_snapshots
        WHERE final_snapshot_fingerprint = NEW.source_final_snapshot_fingerprint
    );
    SELECT RAISE(ABORT, '{CV_FINAL_CONFIRMED_PDF_ARTIFACT_OWNER_MISMATCH_TOKEN}')
    WHERE (
        SELECT owner_key_fingerprint FROM cv_docx_final_snapshots
        WHERE final_snapshot_fingerprint = NEW.source_final_snapshot_fingerprint
    ) != NEW.owner_key_fingerprint;
    SELECT RAISE(ABORT, '{CV_FINAL_CONFIRMED_PDF_ARTIFACT_SOURCE_HASH_MISMATCH_TOKEN}')
    WHERE (
        SELECT final_docx_sha256 FROM cv_docx_final_snapshots
        WHERE final_snapshot_fingerprint = NEW.source_final_snapshot_fingerprint
    ) != NEW.source_final_docx_sha256;
END
"""

ARTIFACT_NO_UPDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_ARTIFACT_NO_UPDATE}
BEFORE UPDATE ON {ARTIFACT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_FINAL_CONFIRMED_PDF_ARTIFACT_IMMUTABLE_TOKEN}');
END
"""

ARTIFACT_NO_DELETE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {TRG_ARTIFACT_NO_DELETE}
BEFORE DELETE ON {ARTIFACT_TABLE_NAME}
BEGIN
    SELECT RAISE(ABORT, '{CV_FINAL_CONFIRMED_PDF_ARTIFACT_NO_DELETE_TOKEN}');
END
"""

#: Ordered exactly as installed by the cutover installer: authority
#: validation triggers first, then legacy freeze, then new-artifact-line
#: triggers. 12 entries total.
ALL_TRIGGER_SQL: tuple[str, ...] = (
    AUTHORITY_INSERT_FINAL_SQL,
    AUTHORITY_INSERT_LEGACY_SQL,
    AUTHORITY_UPDATE_FINAL_SQL,
    AUTHORITY_UPDATE_LEGACY_SQL,
    AUTHORITY_OWNER_IMMUTABLE_SQL,
    AUTHORITY_SLOT_VERSION_INCREMENT_SQL,
    LEGACY_SLOT_FROZEN_INSERT_SQL,
    LEGACY_SLOT_FROZEN_UPDATE_SQL,
    LEGACY_SLOT_FROZEN_DELETE_SQL,
    ARTIFACT_LINEAGE_INSERT_SQL,
    ARTIFACT_NO_UPDATE_SQL,
    ARTIFACT_NO_DELETE_SQL,
)

#: name -> exact DDL text executed by the cutover installer for that trigger.
TRIGGER_SQL_BY_NAME: dict[str, str] = {
    TRG_AUTHORITY_INSERT_FINAL: AUTHORITY_INSERT_FINAL_SQL,
    TRG_AUTHORITY_INSERT_LEGACY: AUTHORITY_INSERT_LEGACY_SQL,
    TRG_AUTHORITY_UPDATE_FINAL: AUTHORITY_UPDATE_FINAL_SQL,
    TRG_AUTHORITY_UPDATE_LEGACY: AUTHORITY_UPDATE_LEGACY_SQL,
    TRG_AUTHORITY_OWNER_IMMUTABLE: AUTHORITY_OWNER_IMMUTABLE_SQL,
    TRG_AUTHORITY_SLOT_VERSION_INCREMENT: AUTHORITY_SLOT_VERSION_INCREMENT_SQL,
    TRG_LEGACY_SLOT_FROZEN_INSERT: LEGACY_SLOT_FROZEN_INSERT_SQL,
    TRG_LEGACY_SLOT_FROZEN_UPDATE: LEGACY_SLOT_FROZEN_UPDATE_SQL,
    TRG_LEGACY_SLOT_FROZEN_DELETE: LEGACY_SLOT_FROZEN_DELETE_SQL,
    TRG_ARTIFACT_LINEAGE_INSERT: ARTIFACT_LINEAGE_INSERT_SQL,
    TRG_ARTIFACT_NO_UPDATE: ARTIFACT_NO_UPDATE_SQL,
    TRG_ARTIFACT_NO_DELETE: ARTIFACT_NO_DELETE_SQL,
}

AUTHORITY_VALIDATION_TRIGGER_NAMES: frozenset[str] = frozenset(
    {
        TRG_AUTHORITY_INSERT_FINAL,
        TRG_AUTHORITY_INSERT_LEGACY,
        TRG_AUTHORITY_UPDATE_FINAL,
        TRG_AUTHORITY_UPDATE_LEGACY,
        TRG_AUTHORITY_OWNER_IMMUTABLE,
        TRG_AUTHORITY_SLOT_VERSION_INCREMENT,
    }
)
LEGACY_FREEZE_TRIGGER_NAMES: frozenset[str] = frozenset(
    {TRG_LEGACY_SLOT_FROZEN_INSERT, TRG_LEGACY_SLOT_FROZEN_UPDATE, TRG_LEGACY_SLOT_FROZEN_DELETE}
)
ARTIFACT_TRIGGER_NAMES: frozenset[str] = frozenset(
    {TRG_ARTIFACT_LINEAGE_INSERT, TRG_ARTIFACT_NO_UPDATE, TRG_ARTIFACT_NO_DELETE}
)
ALL_TRIGGER_NAMES: frozenset[str] = (
    AUTHORITY_VALIDATION_TRIGGER_NAMES | LEGACY_FREEZE_TRIGGER_NAMES | ARTIFACT_TRIGGER_NAMES
)


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


#: name -> expected normalized trigger body, used by the cutover evaluator to
#: detect an existing trigger with the right name but a tampered/older body.
EXPECTED_TRIGGER_BODIES: dict[str, str] = {
    TRG_AUTHORITY_INSERT_FINAL: _normalize_sql(AUTHORITY_INSERT_FINAL_SQL),
    TRG_AUTHORITY_INSERT_LEGACY: _normalize_sql(AUTHORITY_INSERT_LEGACY_SQL),
    TRG_AUTHORITY_UPDATE_FINAL: _normalize_sql(AUTHORITY_UPDATE_FINAL_SQL),
    TRG_AUTHORITY_UPDATE_LEGACY: _normalize_sql(AUTHORITY_UPDATE_LEGACY_SQL),
    TRG_AUTHORITY_OWNER_IMMUTABLE: _normalize_sql(AUTHORITY_OWNER_IMMUTABLE_SQL),
    TRG_AUTHORITY_SLOT_VERSION_INCREMENT: _normalize_sql(AUTHORITY_SLOT_VERSION_INCREMENT_SQL),
    TRG_LEGACY_SLOT_FROZEN_INSERT: _normalize_sql(LEGACY_SLOT_FROZEN_INSERT_SQL),
    TRG_LEGACY_SLOT_FROZEN_UPDATE: _normalize_sql(LEGACY_SLOT_FROZEN_UPDATE_SQL),
    TRG_LEGACY_SLOT_FROZEN_DELETE: _normalize_sql(LEGACY_SLOT_FROZEN_DELETE_SQL),
    TRG_ARTIFACT_LINEAGE_INSERT: _normalize_sql(ARTIFACT_LINEAGE_INSERT_SQL),
    TRG_ARTIFACT_NO_UPDATE: _normalize_sql(ARTIFACT_NO_UPDATE_SQL),
    TRG_ARTIFACT_NO_DELETE: _normalize_sql(ARTIFACT_NO_DELETE_SQL),
}


# -- raw SQLite introspection (self-contained; no ORM/Base.metadata) --------


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _extract_check_constraint_bodies(table_sql: str) -> frozenset[str]:
    """Extract balanced ``CHECK(...)`` bodies directly from raw
    ``CREATE TABLE`` DDL text -- duplicated in spirit from (never imported
    from) ``db_engine.py``/``cv_document_final_snapshot_schema_manifest.py``'s
    identical-purpose helpers, so this manifest module stays independent."""

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
                    bodies.append(_normalize_sql(table_sql[start + 1 : index]))
                    break
            index += 1
    return frozenset(bodies)


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).first()
    return row is not None


def _collect_table_schema(conn: Connection, table_name: str) -> dict[str, Any]:
    quoted = _quoted_identifier(table_name)
    table_sql_row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).first()
    if table_sql_row is None:
        raise RuntimeError(f"ERROR_FINAL_CONFIRMED_PDF_TABLE_MISSING:{table_name}")
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
        "unique_indexed_columns": frozenset(unique_indexed_columns),
        "non_unique_indexed_columns": frozenset(non_unique_indexed_columns),
    }


def collect_actual_final_confirmed_pdf_schema(conn: Connection) -> dict[str, Any]:
    """Read the live schema of both new tables from ``conn``, exclusively
    via ``sqlite_master``/``PRAGMA``. Raises if either table does not
    exist; callers must check the cutover state first."""

    return {
        "manifest_version": FINAL_CONFIRMED_PDF_SCHEMA_MANIFEST_VERSION,
        ARTIFACT_TABLE_NAME: _collect_table_schema(conn, ARTIFACT_TABLE_NAME),
        AUTHORITY_TABLE_NAME: _collect_table_schema(conn, AUTHORITY_TABLE_NAME),
    }


def table_matches_manifest(actual: dict[str, Any], *, table_name: str) -> bool:
    if table_name == ARTIFACT_TABLE_NAME:
        return (
            actual["columns"] == ARTIFACT_EXPECTED_COLUMNS
            and actual["foreign_keys"] == ARTIFACT_EXPECTED_FOREIGN_KEYS
            and actual["check_constraints"] == ARTIFACT_EXPECTED_CHECK_CONSTRAINTS
            and actual["unique_indexed_columns"] == ARTIFACT_EXPECTED_UNIQUE_INDEXED_COLUMNS
            and actual["non_unique_indexed_columns"] == ARTIFACT_EXPECTED_NON_UNIQUE_INDEXED_COLUMNS
        )
    if table_name == AUTHORITY_TABLE_NAME:
        return (
            actual["columns"] == AUTHORITY_EXPECTED_COLUMNS
            and actual["foreign_keys"] == AUTHORITY_EXPECTED_FOREIGN_KEYS
            and actual["check_constraints"] == AUTHORITY_EXPECTED_CHECK_CONSTRAINTS
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


def preflight_final_confirmed_pdf_schema(engine: Engine) -> dict[str, Any]:
    """Read-only inspection of both tables and all trigger bodies attached
    to any of the three tables this cutover touches
    (``cv_final_confirmed_pdf_artifacts``, ``cv_confirmed_pdf_current_authority``,
    ``cv_document_pdf_slots``). Never mutates anything -- the cutover state
    evaluator (``cv_document_final_confirmed_pdf_cutover_state.py``) is the
    only consumer, and interprets the returned dict itself."""

    with engine.connect() as conn:
        artifact_table_exists = _table_exists(conn, ARTIFACT_TABLE_NAME)
        authority_table_exists = _table_exists(conn, AUTHORITY_TABLE_NAME)

        artifact_schema = _collect_table_schema(conn, ARTIFACT_TABLE_NAME) if artifact_table_exists else None
        authority_schema = (
            _collect_table_schema(conn, AUTHORITY_TABLE_NAME) if authority_table_exists else None
        )

        artifact_triggers = collect_existing_trigger_bodies(conn, ARTIFACT_TABLE_NAME)
        authority_triggers = collect_existing_trigger_bodies(conn, AUTHORITY_TABLE_NAME)
        legacy_slot_triggers = collect_existing_trigger_bodies(conn, LEGACY_PDF_SLOT_TABLE_NAME)

    return {
        "artifact_table_exists": artifact_table_exists,
        "authority_table_exists": authority_table_exists,
        "artifact_schema": artifact_schema,
        "authority_schema": authority_schema,
        "artifact_triggers": artifact_triggers,
        "authority_triggers": authority_triggers,
        "legacy_slot_triggers": legacy_slot_triggers,
    }
