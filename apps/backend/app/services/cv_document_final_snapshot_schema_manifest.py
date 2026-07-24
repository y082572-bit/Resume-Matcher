"""Explicit Provenance Stage P6-B1 SQL storage addendum: an independent,
hand-written schema manifest for ``cv_docx_final_snapshots``.

Deliberately never imports ``Base.metadata``, ``cv_document_models``, or any
ORM table/column definition -- every expected column/type/nullability/PK/FK/
CHECK/index below is a literal Python value written by hand, so a bug in the
ORM model (``CvDocxFinalSnapshotRow``) can never silently "pass" preflight by
being compared against its own reflection (the technique the P1/P2/P6-B1
preflights in ``db_engine.py`` use). The actual schema is read exclusively
via ``sqlite_master``/``PRAGMA table_info``/``PRAGMA foreign_key_list``/
``PRAGMA index_list``/``PRAGMA index_xinfo`` -- raw SQLite introspection,
never SQLAlchemy's ``Table``/``Base.metadata`` reflection layer.

Preflight is fail-closed and strictly read-only: a fully absent table
returns ``ABSENT_CREATE_REQUIRED`` (the only state a caller may act on, by
issuing its own ``CREATE TABLE``); any structural difference -- missing/
extra column, wrong type, wrong nullability, wrong primary key, a missing or
mistargeted foreign key, a missing or altered CHECK constraint, an
unexpected ``UNIQUE`` on ``final_docx_sha256``, or a missing index -- always
raises before anything is mutated, and calling this module's functions never
executes a single ``CREATE``/``ALTER``/``DROP`` statement itself.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import Connection, Engine


FINAL_SNAPSHOT_TABLE_NAME = "cv_docx_final_snapshots"
FINAL_SNAPSHOT_SCHEMA_MANIFEST_VERSION = "cv-document-final-snapshot-sql-schema-manifest-v1"


# -- hand-written expected schema (never derived from the ORM) --------------

EXPECTED_COLUMNS: tuple[dict[str, Any], ...] = (
    {
        "position": 0,
        "name": "final_snapshot_fingerprint",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 1,
    },
    {
        "position": 1,
        "name": "owner_key_fingerprint",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 2,
        "name": "source_proposal_artifact_fingerprint",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 3,
        "name": "source_proposal_revision",
        "type": "INTEGER",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 4,
        "name": "generated_proposal_sha256",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 5,
        "name": "final_docx_sha256",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 6,
        "name": "edited_by_user",
        "type": "INTEGER",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 7,
        "name": "finalization_policy_version",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 8,
        "name": "snapshot_schema_version",
        "type": "VARCHAR(64)",
        "nullable": False,
        "primary_key_position": 0,
    },
    {
        "position": 9,
        "name": "created_at",
        "type": "VARCHAR",
        "nullable": False,
        "primary_key_position": 0,
    },
)

# (source_column, target_table, target_column, on_delete)
EXPECTED_FOREIGN_KEYS: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        ("owner_key_fingerprint", "cv_document_artifact_owners", "owner_key_fingerprint", "RESTRICT"),
        (
            "source_proposal_artifact_fingerprint",
            "cv_docx_proposal_artifacts",
            "artifact_fingerprint",
            "RESTRICT",
        ),
        ("final_docx_sha256", "cv_document_blobs", "blob_sha256", "RESTRICT"),
    }
)

EXPECTED_CHECK_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "length(final_snapshot_fingerprint) = 64 AND lower(final_snapshot_fingerprint) = "
        "final_snapshot_fingerprint AND final_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'",
        "length(generated_proposal_sha256) = 64 AND lower(generated_proposal_sha256) = "
        "generated_proposal_sha256 AND generated_proposal_sha256 NOT GLOB '*[^0-9a-f]*'",
        "length(final_docx_sha256) = 64 AND lower(final_docx_sha256) = final_docx_sha256 "
        "AND final_docx_sha256 NOT GLOB '*[^0-9a-f]*'",
        "source_proposal_revision >= 1",
        "snapshot_schema_version = 'cv-document-final-snapshot-schema-v1'",
        "trim(finalization_policy_version) != ''",
        "length(finalization_policy_version) <= 64",
        "edited_by_user IN (0, 1)",
        "(edited_by_user = 1 AND generated_proposal_sha256 != final_docx_sha256) OR "
        "(edited_by_user = 0 AND generated_proposal_sha256 = final_docx_sha256)",
    }
)

# Deliberately empty: ``final_docx_sha256`` must never carry a UNIQUE
# constraint -- many snapshot metadata rows may share identical bytes.
EXPECTED_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset()

EXPECTED_NON_UNIQUE_INDEXED_COLUMNS: frozenset[str] = frozenset(
    {"owner_key_fingerprint", "source_proposal_artifact_fingerprint", "final_docx_sha256"}
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
    ``CREATE TABLE`` DDL text -- SQLite exposes CHECK constraints nowhere
    else (not in ``PRAGMA table_info``, ``foreign_key_list``, or
    ``index_list``). A small, self-contained, quote-aware paren scanner --
    duplicated in spirit from (never imported from) ``db_engine.py``'s
    identical-purpose helper, so this manifest module stays independent of
    every other schema-manifest module in this codebase.
    """

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


def collect_actual_final_snapshot_schema(conn: Connection) -> dict[str, Any]:
    """Read the live ``cv_docx_final_snapshots`` schema from ``conn``,
    exclusively via ``sqlite_master``/``PRAGMA`` -- never via SQLAlchemy
    ``Table`` reflection. Raises if the table does not exist; callers must
    check :func:`preflight_final_docx_snapshot_schema`'s
    ``ABSENT_CREATE_REQUIRED`` state first.
    """

    quoted = _quoted_identifier(FINAL_SNAPSHOT_TABLE_NAME)
    table_sql_row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (FINAL_SNAPSHOT_TABLE_NAME,),
    ).first()
    if table_sql_row is None:
        raise RuntimeError("ERROR_FINAL_DOCX_SNAPSHOT_TABLE_MISSING")
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
            # The primary key's own implicit autoindex -- never a
            # caller-declared index or UNIQUE constraint of its own.
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

    return {
        "manifest_version": FINAL_SNAPSHOT_SCHEMA_MANIFEST_VERSION,
        "columns": columns,
        "foreign_keys": foreign_keys,
        "check_constraints": check_constraints,
        "unique_indexed_columns": frozenset(unique_indexed_columns),
        "non_unique_indexed_columns": frozenset(non_unique_indexed_columns),
    }


def preflight_final_docx_snapshot_schema(engine: Engine) -> str:
    """Fully validate an existing ``cv_docx_final_snapshots`` table against
    the hand-written manifest above, without mutating or repairing the
    target. Returns ``ABSENT_CREATE_REQUIRED`` if the table does not exist
    at all (the caller may then create it); returns
    ``FINAL_DOCX_SNAPSHOT_SCHEMA_READY`` if it exists and matches exactly;
    otherwise raises ``RuntimeError`` before this function (or any caller
    that respects its result) performs a single mutation.
    """

    with engine.connect() as conn:
        if not _table_exists(conn, FINAL_SNAPSHOT_TABLE_NAME):
            return "ABSENT_CREATE_REQUIRED"
        actual = collect_actual_final_snapshot_schema(conn)

    if actual["columns"] != EXPECTED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA:columns:"
            f"expected={EXPECTED_COLUMNS}:actual={actual['columns']}"
        )
    if actual["foreign_keys"] != EXPECTED_FOREIGN_KEYS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA:foreign_keys:"
            f"expected={sorted(EXPECTED_FOREIGN_KEYS)}:actual={sorted(actual['foreign_keys'])}"
        )
    if actual["check_constraints"] != EXPECTED_CHECK_CONSTRAINTS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA:check_constraints:"
            f"expected={sorted(EXPECTED_CHECK_CONSTRAINTS)}:actual={sorted(actual['check_constraints'])}"
        )
    if actual["unique_indexed_columns"] != EXPECTED_UNIQUE_INDEXED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA:unexpected_unique_index:"
            f"actual={sorted(actual['unique_indexed_columns'])}"
        )
    if actual["non_unique_indexed_columns"] != EXPECTED_NON_UNIQUE_INDEXED_COLUMNS:
        raise RuntimeError(
            f"ERROR_INCOMPATIBLE_FINAL_DOCX_SNAPSHOT_SCHEMA:indexes:"
            f"expected={sorted(EXPECTED_NON_UNIQUE_INDEXED_COLUMNS)}:"
            f"actual={sorted(actual['non_unique_indexed_columns'])}"
        )
    return "FINAL_DOCX_SNAPSHOT_SCHEMA_READY"


def final_docx_snapshot_schema_manifest(engine: Engine) -> dict[str, Any]:
    """Return the complete actual manifest, for diagnostics/idempotency
    tests -- raises if the table is absent (callers must run
    :func:`preflight_final_docx_snapshot_schema` first)."""

    with engine.connect() as conn:
        if not _table_exists(conn, FINAL_SNAPSHOT_TABLE_NAME):
            raise RuntimeError("ERROR_FINAL_DOCX_SNAPSHOT_TABLE_MISSING:manifest_unavailable")
        return collect_actual_final_snapshot_schema(conn)
