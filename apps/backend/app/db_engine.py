"""SQLite engine/session plumbing for the SQLAlchemy data layer.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a temp-file database.
"""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base, MetricEvent

__all__ = [
    "Base",
    "make_async_engine",
    "make_sync_engine",
    "init_models_sync",
    "preflight_explicit_provenance_p1_schema",
]


_P1_MANIFEST_VERSION = "explicit-provenance-p1-schema-manifest-v1"
_P1_TABLE_COLUMNS = {
    "truth_entities": {
        "entity_id", "entity_type", "parent_entity_id", "display_name", "status",
        "source_reference", "schema_version", "revision", "content_fingerprint",
        "created_at", "updated_at", "archived_at",
    },
    "truth_facts": {
        "fact_id", "entity_id", "fact_type", "value_json", "normalized_value_json",
        "status", "use_in_cv", "requires_approval", "transferability",
        "employment_scope_entity_id", "source_reference", "schema_version", "revision",
        "content_fingerprint", "created_at", "updated_at", "archived_at",
    },
    "truth_fact_variants": {
        "variant_id", "fact_id", "text", "normalized_text", "status", "operation",
        "schema_version", "revision", "content_fingerprint", "created_at", "updated_at",
        "archived_at",
    },
    "truth_evidence": {
        "evidence_id", "fact_id", "evidence_type", "source_entity_id",
        "source_reference", "source_locator", "content_hash", "status", "captured_at",
        "confirmed_at", "confirmed_by", "schema_version", "revision",
        "content_fingerprint", "created_at", "updated_at", "archived_at",
    },
    "truth_permissions": {
        "permission_id", "fact_id", "target_scope", "allowed_operations",
        "constraints_json", "status", "approved_by", "approved_at", "valid_from",
        "valid_until", "schema_version", "revision", "content_fingerprint", "created_at",
        "updated_at", "archived_at",
    },
}


_P1_CANONICAL_LOWER: dict[str, str] = {name.lower(): name for name in _P1_TABLE_COLUMNS}


def _p1_existing_tables(conn: Any) -> set[str]:
    all_tables = list(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).scalars()
    )
    matched: list[tuple[str, str]] = []
    for actual in all_tables:
        canonical = _P1_CANONICAL_LOWER.get(actual.lower())
        if canonical is not None:
            matched.append((actual, canonical))
    for actual, canonical in matched:
        if actual != canonical:
            raise RuntimeError(
                f"ERROR_INCOMPATIBLE_P1_SCHEMA_NAME:"
                f"actual={actual}:expected={canonical}"
            )
    return {actual for actual, _ in matched}


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip())


def _constraint_expressions(table_sql: str, keyword: str) -> list[str]:
    """Extract balanced CHECK/UNIQUE bodies; full DDL remains in the manifest."""

    expressions: list[str] = []
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
                    expressions.append(_normalize_sql(table_sql[start + 1:index]) or "")
                    break
            index += 1
    return sorted(expressions)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _collect_p1_schema_manifest(conn: Any) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table in sorted(_P1_TABLE_COLUMNS):
        quoted_table = _quoted_identifier(table)
        table_sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).scalar_one()
        indexes = []
        for index_row in conn.exec_driver_sql(
            f"PRAGMA index_list({quoted_table})"
        ).mappings():
            index_name = index_row["name"]
            index_sql_row = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).first()
            index_sql = _normalize_sql(index_sql_row[0] if index_sql_row else None)
            predicate = None
            if index_sql is not None:
                where_match = re.search(r"\bWHERE\b(.*)$", index_sql, re.IGNORECASE)
                predicate = _normalize_sql(where_match.group(1)) if where_match else None
            indexes.append(
                {
                    "name": index_name,
                    "unique": bool(index_row["unique"]),
                    "origin": index_row["origin"],
                    "partial": bool(index_row["partial"]),
                    "predicate": predicate,
                    "sql": index_sql,
                    "xinfo": [
                        {
                            "seqno": row["seqno"],
                            "cid": row["cid"],
                            "name": row["name"],
                            "desc": bool(row["desc"]),
                            "coll": row["coll"],
                            "key": bool(row["key"]),
                        }
                        for row in conn.exec_driver_sql(
                            f"PRAGMA index_xinfo({_quoted_identifier(index_name)})"
                        ).mappings()
                    ],
                }
            )
        triggers = [
            {
                "name": row["name"],
                "sql": _normalize_sql(row["sql"]),
            }
            for row in conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            ).mappings()
        ]
        tables[table] = {
            "name": table,
            "sql": _normalize_sql(table_sql),
            "columns": [
                {
                    "position": row["cid"],
                    "name": row["name"],
                    "type": row["type"],
                    "nullable": not bool(row["notnull"]),
                    "default": row["dflt_value"],
                    "primary_key_position": row["pk"],
                }
                for row in conn.exec_driver_sql(
                    f"PRAGMA table_info({quoted_table})"
                ).mappings()
            ],
            "foreign_keys": sorted(
                (
                    {
                        "id": row["id"],
                        "sequence": row["seq"],
                        "source_table": table,
                        "source_column": row["from"],
                        "target_table": row["table"],
                        "target_column": row["to"],
                        "on_update": row["on_update"],
                        "on_delete": row["on_delete"],
                        "match": row["match"],
                    }
                    for row in conn.exec_driver_sql(
                        f"PRAGMA foreign_key_list({quoted_table})"
                    ).mappings()
                ),
                key=lambda item: (
                    item["source_column"], item["target_table"], item["target_column"]
                ),
            ),
            "check_constraints": _constraint_expressions(table_sql, "CHECK"),
            "unique_constraints": _constraint_expressions(table_sql, "UNIQUE"),
            "indexes": sorted(indexes, key=lambda item: item["name"]),
            "triggers": triggers,
        }
    return {"manifest_version": _P1_MANIFEST_VERSION, "tables": tables}


def _install_explicit_provenance_p1_runtime_objects(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_truth_evidence_identity
            ON truth_evidence (
                fact_id,
                evidence_type,
                COALESCE(source_reference, ''),
                COALESCE(source_locator, ''),
                content_hash
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_truth_entities_type_immutable
            BEFORE UPDATE OF entity_type ON truth_entities
            WHEN NEW.entity_type != OLD.entity_type
            BEGIN
                SELECT RAISE(ABORT, 'TRUTH_ENTITY_TYPE_IMMUTABLE');
            END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_truth_facts_employment_scope_insert
            BEFORE INSERT ON truth_facts
            WHEN NEW.employment_scope_entity_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM truth_entities
                WHERE entity_id = NEW.employment_scope_entity_id
                  AND entity_type = 'EMPLOYMENT'
             )
            BEGIN
                SELECT RAISE(ABORT, 'TRUTH_EMPLOYMENT_SCOPE_NOT_EMPLOYMENT');
            END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_truth_facts_employment_scope_update
            BEFORE UPDATE OF employment_scope_entity_id ON truth_facts
            WHEN NEW.employment_scope_entity_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM truth_entities
                WHERE entity_id = NEW.employment_scope_entity_id
                  AND entity_type = 'EMPLOYMENT'
             )
            BEGIN
                SELECT RAISE(ABORT, 'TRUTH_EMPLOYMENT_SCOPE_NOT_EMPLOYMENT');
            END
            """
        )


@lru_cache(maxsize=1)
def _expected_p1_schema_manifest() -> dict[str, Any]:
    """Build the approved manifest in an isolated reference database."""

    reference = create_engine("sqlite:///:memory:", future=True)
    try:
        Base.metadata.create_all(reference)
        _install_explicit_provenance_p1_runtime_objects(reference)
        with reference.connect() as conn:
            return _collect_p1_schema_manifest(conn)
    finally:
        reference.dispose()


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preflight_explicit_provenance_p1_schema(engine: Engine) -> str:
    """Fully validate existing P1 DDL without mutating or repairing the target."""

    with engine.connect() as conn:
        existing = _p1_existing_tables(conn)
        if not existing:
            return "ABSENT_CREATE_REQUIRED"
        if existing != set(_P1_TABLE_COLUMNS):
            names = ",".join(sorted(existing))
            raise RuntimeError(f"ERROR_PARTIAL_P1_SCHEMA:existing={names}")
        actual = _collect_p1_schema_manifest(conn)
    expected = _expected_p1_schema_manifest()
    if actual != expected:
        raise RuntimeError(
            "ERROR_INCOMPATIBLE_P1_SCHEMA:"
            f"expected={_manifest_digest(expected)}:actual={_manifest_digest(actual)}"
        )
    return "P1_SCHEMA_READY"


def _validate_p1_runtime_manifest(engine: Engine) -> None:
    state = preflight_explicit_provenance_p1_schema(engine)
    if state != "P1_SCHEMA_READY":
        raise RuntimeError(f"ERROR_INCOMPATIBLE_P1_SCHEMA:state={state}")


def explicit_provenance_p1_schema_manifest(engine: Engine) -> dict[str, Any]:
    """Return the complete versioned manifest used by fail-closed preflight."""

    with engine.connect() as conn:
        existing = _p1_existing_tables(conn)
        if existing != set(_P1_TABLE_COLUMNS):
            raise RuntimeError("ERROR_PARTIAL_P1_SCHEMA:manifest_unavailable")
        return _collect_p1_schema_manifest(conn)


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL improves concurrent read/write between the async (doc tables) and sync
    (api_keys) engines pointed at the same file; ``busy_timeout`` rides out the
    brief lock contention that creates; ``foreign_keys`` enforces relational
    integrity (off by default in SQLite).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _url(path: Path, *, driver: str) -> str:
    """Build a SQLite URL. Absolute paths yield the required four slashes."""
    return f"sqlite+{driver}:///{path}" if driver else f"sqlite:///{path}"


def make_async_engine(path: Path) -> AsyncEngine:
    """Create the async engine (``aiosqlite``) for the document tables."""
    engine = create_async_engine(_url(path, driver="aiosqlite"), future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(path: Path) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same file as the async engine.
    """
    engine = create_engine(_url(path, driver=""), future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def init_models_sync(engine: Engine) -> None:
    """Create all tables (idempotent) using a sync engine connection."""
    p1_state = preflight_explicit_provenance_p1_schema(engine)
    if p1_state == "ABSENT_CREATE_REQUIRED":
        Base.metadata.create_all(engine)
        _install_explicit_provenance_p1_runtime_objects(engine)
        _validate_p1_runtime_manifest(engine)
    elif p1_state == "P1_SCHEMA_READY":
        # Deliberately read-only for existing P1 DDL: no create_all, IF NOT
        # EXISTS repair, trigger installation, or P1 sqlite_master mutation.
        _validate_p1_runtime_manifest(engine)
        # Preserve the pre-existing additive migration contract for legacy
        # tables without ever invoking create_all or create on a P1 table.
        for table in Base.metadata.sorted_tables:
            if table.name not in _P1_TABLE_COLUMNS:
                table.create(engine, checkfirst=True)
    else:  # Defensive: preflight raises for every non-ready existing state.
        raise RuntimeError(f"ERROR_INCOMPATIBLE_P1_SCHEMA:state={p1_state}")

    # ``create_all`` does not ALTER existing SQLite tables. Keep this additive
    # migration idempotent so older local databases can load resumes safely.
    with engine.begin() as conn:
        metric_sql_row = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'metric_events'"
        ).first()
        metric_sql = metric_sql_row[0] if metric_sql_row else ""
        if metric_sql and "TRANSFORMATION_PLAN_DECIDED" not in metric_sql:
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS metric_events_no_update")
            conn.exec_driver_sql("DROP TRIGGER IF EXISTS metric_events_no_delete")
            conn.exec_driver_sql(
                "ALTER TABLE metric_events RENAME TO metric_events_stage10b_old"
            )
            for index_name in (
                "ix_metric_events_event_type",
                "ix_metric_events_entity_type",
                "ix_metric_events_entity_id",
                "ix_metric_events_lifecycle_id",
            ):
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
            MetricEvent.__table__.create(conn)
            conn.exec_driver_sql(
                """
                INSERT INTO metric_events (
                    sequence_id, event_id, operation_key, event_type, entity_type,
                    entity_id, lifecycle_id, from_state, to_state, occurred_at,
                    recorded_at, source, metadata_json
                )
                SELECT
                    sequence_id, event_id, operation_key, event_type, entity_type,
                    entity_id, lifecycle_id, from_state, to_state, occurred_at,
                    recorded_at, source, metadata_json
                FROM metric_events_stage10b_old
                """
            )
            conn.exec_driver_sql("DROP TABLE metric_events_stage10b_old")

        columns = conn.exec_driver_sql("PRAGMA table_info(resumes)").mappings().all()
        if columns and "interview_prep" not in {column["name"] for column in columns}:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN interview_prep TEXT")

        # Idempotent migration for resumes table
        if columns:
            existing_res_cols = {column["name"] for column in columns}
            if "lifecycle_token" not in existing_res_cols:
                conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN lifecycle_token TEXT")

            # Backfill/repair NULL, empty, or duplicate lifecycle_tokens on resumes
            rows = conn.exec_driver_sql("SELECT resume_id, lifecycle_token FROM resumes").mappings().all()
            token_counts = {}
            for r in rows:
                tok = r["lifecycle_token"]
                if tok is not None and len(tok.strip()) > 0:
                    token_counts[tok] = token_counts.get(tok, 0) + 1

            processed_tokens = set()
            from uuid import uuid4
            for r in rows:
                res_id = r["resume_id"]
                tok = r["lifecycle_token"]

                needs_backfill = False
                if tok is None or len(tok.strip()) == 0:
                    needs_backfill = True
                elif token_counts.get(tok, 0) > 1:
                    if tok in processed_tokens:
                        needs_backfill = True
                    else:
                        processed_tokens.add(tok)
                else:
                    processed_tokens.add(tok)

                if needs_backfill:
                    new_token = str(uuid4())
                    while new_token in processed_tokens:
                        new_token = str(uuid4())
                    conn.exec_driver_sql(
                        "UPDATE resumes SET lifecycle_token = ? WHERE resume_id = ?",
                        (new_token, res_id)
                    )
                    processed_tokens.add(new_token)

            # Always ensure the unique index exists
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_resumes_lifecycle_token ON resumes(lifecycle_token)"
            )

        # Idempotent migration for applications table
        app_cols = conn.exec_driver_sql("PRAGMA table_info(applications)").mappings().all()
        if app_cols:
            existing_app_cols = {column["name"] for column in app_cols}
            if "status_version" not in existing_app_cols:
                conn.exec_driver_sql("ALTER TABLE applications ADD COLUMN status_version INTEGER NOT NULL DEFAULT 0")
            if "lifecycle_token" not in existing_app_cols:
                conn.exec_driver_sql("ALTER TABLE applications ADD COLUMN lifecycle_token TEXT")

            # Backfill/repair NULL, empty, or duplicate lifecycle_tokens
            rows = conn.exec_driver_sql("SELECT application_id, lifecycle_token FROM applications").mappings().all()
            token_counts = {}
            for r in rows:
                tok = r["lifecycle_token"]
                if tok is not None and len(tok.strip()) > 0:
                    token_counts[tok] = token_counts.get(tok, 0) + 1

            processed_tokens = set()
            from uuid import uuid4
            for r in rows:
                app_id = r["application_id"]
                tok = r["lifecycle_token"]

                needs_backfill = False
                if tok is None or len(tok.strip()) == 0:
                    needs_backfill = True
                elif token_counts.get(tok, 0) > 1:
                    if tok in processed_tokens:
                        needs_backfill = True
                    else:
                        processed_tokens.add(tok)
                else:
                    processed_tokens.add(tok)

                if needs_backfill:
                    new_token = str(uuid4())
                    while new_token in processed_tokens:
                        new_token = str(uuid4())
                    conn.exec_driver_sql(
                        "UPDATE applications SET lifecycle_token = ? WHERE application_id = ?",
                        (new_token, app_id)
                    )
                    processed_tokens.add(new_token)

            # Always ensure the unique index exists
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_lifecycle_token ON applications(lifecycle_token)"
            )

        # Idempotent migration for jobs table
        job_cols = conn.exec_driver_sql("PRAGMA table_info(jobs)").mappings().all()
        if job_cols:
            existing_job_cols = {column["name"] for column in job_cols}
            if "lifecycle_token" not in existing_job_cols:
                conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN lifecycle_token TEXT")

            # Backfill/repair NULL, empty, or duplicate lifecycle_tokens on jobs
            rows = conn.exec_driver_sql("SELECT job_id, lifecycle_token FROM jobs").mappings().all()
            token_counts = {}
            for r in rows:
                tok = r["lifecycle_token"]
                if tok is not None and len(tok.strip()) > 0:
                    token_counts[tok] = token_counts.get(tok, 0) + 1

            processed_tokens = set()
            from uuid import uuid4
            for r in rows:
                j_id = r["job_id"]
                tok = r["lifecycle_token"]

                needs_backfill = False
                if tok is None or len(tok.strip()) == 0:
                    needs_backfill = True
                elif token_counts.get(tok, 0) > 1:
                    if tok in processed_tokens:
                        needs_backfill = True
                    else:
                        processed_tokens.add(tok)
                else:
                    processed_tokens.add(tok)

                if needs_backfill:
                    new_token = str(uuid4())
                    while new_token in processed_tokens:
                        new_token = str(uuid4())
                    conn.exec_driver_sql(
                        "UPDATE jobs SET lifecycle_token = ? WHERE job_id = ?",
                        (new_token, j_id)
                    )
                    processed_tokens.add(new_token)

            # Always ensure the unique index exists
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_lifecycle_token ON jobs(lifecycle_token)"
            )

        # Create triggers for applications lifecycle token checks
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_required_insert
            BEFORE INSERT ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_required_update
            BEFORE UPDATE ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS applications_lifecycle_immutable
            BEFORE UPDATE ON applications
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token is immutable')
                WHERE OLD.lifecycle_token IS NOT NULL AND length(trim(OLD.lifecycle_token)) > 0
                  AND NEW.lifecycle_token != OLD.lifecycle_token;
            END;
            """
        )

        # Create triggers for jobs lifecycle token checks
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS jobs_lifecycle_required_insert
            BEFORE INSERT ON jobs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS jobs_lifecycle_required_update
            BEFORE UPDATE ON jobs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS jobs_lifecycle_immutable
            BEFORE UPDATE ON jobs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token is immutable')
                WHERE OLD.lifecycle_token IS NOT NULL AND length(trim(OLD.lifecycle_token)) > 0
                  AND NEW.lifecycle_token != OLD.lifecycle_token;
            END;
            """
        )

        # Create triggers for resumes lifecycle token checks
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS resumes_lifecycle_required_insert
            BEFORE INSERT ON resumes
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS resumes_lifecycle_required_update
            BEFORE UPDATE ON resumes
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token cannot be NULL or empty')
                WHERE NEW.lifecycle_token IS NULL OR length(trim(NEW.lifecycle_token)) = 0;
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS resumes_lifecycle_immutable
            BEFORE UPDATE ON resumes
            FOR EACH ROW
            BEGIN
                SELECT RAISE(FAIL, 'lifecycle_token is immutable')
                WHERE OLD.lifecycle_token IS NOT NULL AND length(trim(OLD.lifecycle_token)) > 0
                  AND NEW.lifecycle_token != OLD.lifecycle_token;
            END;
            """
        )

        # Create append-only triggers on metric_events table idempotent-ly
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_events_no_update
            BEFORE UPDATE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Updates on metric_events are prohibited');
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_events_no_delete
            BEFORE DELETE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Deletes on metric_events are prohibited');
            END;
            """
        )

        # Create triggers for metric_bootstrap_state immutability idempotent-ly
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_bootstrap_state_no_update
            BEFORE UPDATE ON metric_bootstrap_state
            BEGIN
                SELECT RAISE(FAIL, 'Updates on metric_bootstrap_state are prohibited');
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS metric_bootstrap_state_no_delete
            BEFORE DELETE ON metric_bootstrap_state
            BEGIN
                SELECT RAISE(FAIL, 'Deletes on metric_bootstrap_state are prohibited');
            END;
            """
        )
